"""Regressionstests zu den Befunden der Sicherheitsprüfung."""

from __future__ import annotations

import fcntl
import io
import os
import re
import shutil
import struct
import threading
import time
import zipfile

import pytest

from ebookapp.errors import Conflict, Invalid
from ebookapp.validation import EpubLimits, validate_epub
from tests.conftest import ADMIN_PASSWORD, ALL_SCOPES, Env, make_epub, make_pdf

LIMITS = EpubLimits(max_uncompressed_bytes=50 * 1024 * 1024, max_entries=200, max_ratio=100)


def _tmp_files(env: Env) -> list:
    return list(env.settings.tmp_dir.glob("*"))


def _login_token(client) -> str:
    page = client.get("/admin/login")
    return re.search(r'name="login_token" value="([^"]+)"', page.text).group(1)


def _wrong_login(env: Env, address: str, password: str = "falsch-falsch-falsch") -> int:
    client = env.new_client()
    client.headers["X-Forwarded-For"] = address
    token = _login_token(client)
    return client.post(
        "/admin/login", data={"username": "admin", "password": password, "login_token": token}
    ).status_code


# --------------------------------------------------------------------------- API-Uploads


def test_unauthenticated_upload_is_rejected_before_the_body_is_read(env: Env) -> None:
    """Ohne gültigen Schlüssel wird kein Byte eines Uploads zwischengespeichert."""
    received: list[int] = []
    app = env.app

    async def counting_app(scope, receive, send):
        async def counting_receive():
            message = await receive()
            if message["type"] == "http.request":
                received.append(len(message.get("body", b"")))
            return message

        await app(scope, counting_receive, send)

    from starlette.testclient import TestClient

    client = TestClient(counting_app, base_url="https://ebooks.example.com")
    big = make_pdf(size=3 * 1024 * 1024)
    url = "/api/v1/books/bk_x/editions/ed_y/files"

    for headers in ({}, {"Authorization": "Bearer ebk_000000000000_" + "a" * 43}):
        received.clear()
        response = client.post(
            url, files={"pdf": ("a.pdf", big, "application/pdf")}, headers=headers
        )
        assert response.status_code == 401
        assert sum(received) == 0
        assert _tmp_files(env) == []

    # Ein gültiger Schlüssel ohne Upload-Berechtigung wird ebenfalls vorab abgewiesen.
    received.clear()
    key = env.api_key(["books:read", "books:write", "links:manage"])
    response = client.post(
        url,
        files={"pdf": ("a.pdf", big, "application/pdf")},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["details"]["required_scope"] == "files:write"
    assert sum(received) == 0

    received.clear()
    cover = client.put("/api/v1/books/bk_x/cover", files={"file": ("c.png", big, "image/png")})
    assert cover.status_code == 401
    assert sum(received) == 0


def test_upload_of_two_files_is_all_or_nothing(env: Env, admin) -> None:
    api = env.api()
    book = api.post("/api/v1/books", json={"title": "Atomar"}).json()
    url = f"/api/v1/books/{book['id']}/editions/{book['draft_edition']['id']}/files"
    response = api.post(
        url,
        files={
            "pdf": ("a.pdf", make_pdf("gültig"), "application/pdf"),
            "epub": ("a.epub", b"kein epub" * 100, "application/epub+zip"),
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["fields"][0]["field"] == "epub"
    assert api.get(f"/api/v1/books/{book['id']}").json()["draft_edition"]["files"] == []
    assert not [p for p in env.settings.books_dir.rglob("*") if p.is_file()]
    assert _tmp_files(env) == []

    # Dasselbe über die Oberfläche
    book_id = admin.create_book("Atomar GUI")
    edition_id = admin.draft_id(book_id)
    gui = admin.upload(book_id, edition_id, make_pdf("gültig"), b"kein epub" * 100)
    assert gui.status_code == 422
    with env.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM edition_files").fetchone()[0] == 0


def test_pagination_bounds(env: Env, admin) -> None:
    api = env.api()
    assert api.get(f"/api/v1/books?offset={10**30}").status_code == 422
    assert api.get(f"/api/v1/links?offset={10**30}").status_code == 422
    assert env.client.get(f"/admin/books?page={10**30}").status_code == 200
    assert env.client.get("/admin/links?page=abc").status_code == 200


# --------------------------------------------------------------------------- Anmeldung


def test_attacker_cannot_lock_out_the_admin(env: Env) -> None:
    for _ in range(5):
        assert _wrong_login(env, "203.0.113.50") == 401
    assert _wrong_login(env, "203.0.113.50", ADMIN_PASSWORD) == 429  # Angreifer gesperrt
    assert _wrong_login(env, "198.51.100.7", ADMIN_PASSWORD) == 303  # Administrator nicht


def test_login_limit_holds_under_concurrency(env: Env) -> None:
    """Gleichzeitige Versuche können das Limit nicht unterlaufen."""
    results: list[int] = []
    barrier = threading.Barrier(16)

    def worker() -> None:
        client = env.new_client()
        client.headers["X-Forwarded-For"] = "203.0.113.60"
        token = _login_token(client)
        barrier.wait()
        results.append(
            client.post(
                "/admin/login",
                data={"username": "admin", "password": "falsch-falsch-1", "login_token": token},
            ).status_code
        )

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert set(results) <= {401, 429}
    assert results.count(401) <= env.settings.login_max_failures
    assert results.count(429) >= 16 - env.settings.login_max_failures


def test_concurrent_password_checks_are_capped(env: Env, monkeypatch) -> None:
    from ebookapp.services import auth

    running = 0
    peak = 0
    lock = threading.Lock()
    original = auth.authenticate

    def slow(conn, username, password):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.3)
        try:
            return original(conn, username, password)
        finally:
            with lock:
                running -= 1

    monkeypatch.setattr(auth, "authenticate", slow)
    statuses: list[int] = []

    def worker(index: int) -> None:
        statuses.append(_wrong_login(env, f"198.51.{index}.9"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak <= 2
    assert 429 in statuses  # überzählige Versuche werden sofort abgewiesen


# --------------------------------------------------------------------------- Downloads


def test_ipv4_mapped_clients_are_distinct(env: Env, admin) -> None:
    book_id = admin.published_book()
    _, path = admin.create_link(book_id, max_downloads="1")
    buyer = env.new_client()
    first = buyer.get(f"{path}/pdf", headers={"X-Forwarded-For": "::ffff:1.2.3.4"})
    second = buyer.get(f"{path}/pdf", headers={"X-Forwarded-For": "::ffff:203.0.113.77"})
    assert first.status_code == 200
    assert second.status_code == 410


def test_handoff_to_a_download_manager_counts_once(env: Env, admin) -> None:
    """Browser und Download-Manager desselben Anschlusses haben verschiedene User-Agents."""
    book_id = admin.published_book()
    _, path = admin.create_link(book_id, max_downloads="1")
    buyer = env.new_client()
    address = {"X-Forwarded-For": "198.51.100.44"}
    webview = buyer.get(f"{path}/pdf", headers={**address, "User-Agent": "Mozilla/5.0 (wv)"})
    manager = buyer.get(
        f"{path}/pdf", headers={**address, "User-Agent": "AndroidDownloadManager/14"}
    )
    assert webview.status_code == manager.status_code == 200
    with env.db() as conn:
        assert conn.execute("SELECT download_count FROM links").fetchone()[0] == 1


def test_undeliverable_requests_are_not_counted(env: Env, admin) -> None:
    pdf = make_pdf("zählt nicht", 5000)
    book_id = admin.published_book("Zähler", pdf)
    _, path = admin.create_link(book_id, max_downloads="1")
    buyer = env.new_client()

    beyond = buyer.get(f"{path}/pdf", headers={"Range": f"bytes={len(pdf) + 100}-"})
    assert beyond.status_code == 416
    assert beyond.headers["content-range"] == f"bytes */{len(pdf)}"

    stored = next(env.settings.books_dir.rglob("*.pdf"))
    hidden = stored.with_suffix(".weg")
    stored.rename(hidden)
    missing = buyer.get(f"{path}/pdf")
    assert missing.status_code == 503
    assert "vorübergehend nicht verfügbar" in missing.text
    hidden.rename(stored)

    with env.db() as conn:
        assert conn.execute("SELECT download_count FROM links").fetchone()[0] == 0
    assert buyer.get(f"{path}/pdf").content == pdf  # das Limit ist noch unverbraucht


def test_trailing_slash_does_not_redirect_with_the_code(env: Env, admin) -> None:
    book_id = admin.published_book()
    _, path = admin.create_link(book_id)
    for url in (f"{path}/", f"{path}/pdf/"):
        response = env.new_client().get(url)
        assert response.status_code == 404
        assert "location" not in response.headers


def test_access_log_never_contains_codes_for_odd_paths(env: Env, admin, caplog) -> None:
    import logging

    book_id = admin.published_book()
    _, path = admin.create_link(book_id)
    code = path.rsplit("/", 1)[-1]
    buyer = env.new_client()
    with caplog.at_level(logging.DEBUG):
        for url in (f"/d//{code}", f"/d/x/{code}", f"/D/{code}", f"/d/{code}/", f"/x/{code}"):
            buyer.get(url)
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("ebookapp")]
    assert len(lines) >= 5
    assert not [line for line in lines if code in line]


# --------------------------------------------------------------------------- Ausgaben


def test_migration_never_leaves_a_link_without_files(env: Env, admin) -> None:
    book_id = admin.published_book("Formate", make_pdf("alt"), make_epub("alt"))
    epub_link, epub_path = admin.create_link(book_id, formats=["epub"], label="nur EPUB")
    both_link, both_path = admin.create_link(book_id, label="beide")

    admin.post(f"/admin/books/{book_id}/editions", {"note": ""})
    draft = admin.draft_id(book_id)
    admin.upload(book_id, draft, make_pdf("neu"))  # neue Ausgabe ohne EPUB
    response = admin.publish(book_id, draft, "migrate")
    assert response.status_code == 303
    page = env.client.get(response.headers["location"]).text
    assert "1 Links wurden auf die neue Ausgabe umgestellt" in page
    assert "1 nicht umgestellt" in page

    buyer = env.new_client()
    assert b"neu" in buyer.get(f"{both_path}/pdf").content
    assert buyer.get(f"{epub_path}/epub").status_code == 200  # weiterhin die alte Ausgabe
    assert "EPUB herunterladen" in buyer.get(epub_path).text

    # Auch eine einzelne Änderung darf einen Link nicht leeren.
    refused = admin.post(
        f"/admin/links/{epub_link}/edit",
        {
            "label": "nur EPUB",
            "formats": ["epub"],
            "edition_id": draft,
            "expires_at": "",
            "max_downloads": "",
        },
    )
    assert refused.status_code == 422
    assert "würde nichts mehr anbieten" in refused.text
    api = env.api()
    assert api.patch(f"/api/v1/links/{both_link}", json={"formats": ["epub"]}).status_code == 422
    migrate = api.post(
        f"/api/v1/books/{book_id}/editions/{draft}/migrate-links", json={"confirm": True}
    )
    assert migrate.json() == {"links_migrated": 0, "links_without_matching_format": 1}


def test_scopes_for_link_related_side_effects(env: Env) -> None:
    full = env.api()
    book = full.post("/api/v1/books", json={"title": "Rechte"}).json()
    edition_id = book["draft_edition"]["id"]
    files = {"pdf": ("a.pdf", make_pdf("eins"), "application/pdf")}
    full.post(f"/api/v1/books/{book['id']}/editions/{edition_id}/files", files=files)
    full.post(f"/api/v1/books/{book['id']}/editions/{edition_id}/publish")
    full.post("/api/v1/links", json={"book_id": book["id"], "label": "vertraulich"})

    writer = env.api(["books:read", "books:write", "files:write"])  # ohne links:manage
    preview = writer.get(f"/api/v1/books/{book['id']}/deletion-preview").json()
    assert preview["links_total"] == 1
    assert preview["links"] == []
    assert "vertraulich" not in str(preview)

    draft = writer.post(f"/api/v1/books/{book['id']}/editions", json={}).json()
    writer.post(
        f"/api/v1/books/{book['id']}/editions/{draft['id']}/files",
        files={"pdf": ("b.pdf", make_pdf("zwei"), "application/pdf")},
    )
    url = f"/api/v1/books/{book['id']}/editions/{draft['id']}/publish"
    denied = writer.post(url, json={"existing_links": "migrate"})
    assert denied.status_code == 403
    assert denied.json()["error"]["details"]["required_scope"] == "links:manage"
    assert writer.post(url, json={"existing_links": "keep"}).status_code == 200

    assert full.get(f"/api/v1/books/{book['id']}/deletion-preview").json()["links"]


def test_deletion_counts_do_not_depend_on_the_listing(env: Env, admin, monkeypatch) -> None:
    from ebookapp.services import books

    monkeypatch.setattr(books, "PREVIEW_LINK_LIMIT", 2)
    book_id = admin.published_book("Viele Links")
    for index in range(5):
        admin.create_link(book_id, label=f"Nr. {index}")
    page = env.client.get(f"/admin/books/{book_id}/delete").text
    assert "5 Downloadlinks" in page
    assert "neuesten 2 von 5" in page
    done = admin.post(
        f"/admin/books/{book_id}/delete", {"confirm": "1", "expected_link_count": "5"}
    )
    assert done.status_code == 303


def test_pagination_keeps_filters(env: Env, admin, monkeypatch) -> None:
    from ebookapp.web import admin as admin_module

    monkeypatch.setattr(admin_module, "PAGE_SIZE", 2)
    book_id = admin.published_book("Seiten")
    for index in range(5):
        admin.create_link(book_id, label=f"Kunde {index}")
    page = env.client.get(f"/admin/links?q=Kunde&state=active&book_id={book_id}").text
    href = re.search(r'href="(\?[^"]*page=2)"', page).group(1)
    assert href == f"?q=Kunde&amp;state=active&amp;book_id={book_id}&amp;page=2"
    second = env.client.get("/admin/links" + href.replace("&amp;", "&")).text
    assert "Seite 2 von 3" in second
    assert second.count("Kunde ") == 2


# --------------------------------------------------------------------------- EPUB


def test_container_xml_cannot_stall_the_server(tmp_path) -> None:
    """Eine präparierte container.xml darf die Prüfung nicht minutenlang blockieren."""
    evil = b"<container>" + b"<rootfile " * 5000 + b"</container>"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", evil, compress_type=zipfile.ZIP_DEFLATED)
    path = tmp_path / "evil.epub"
    path.write_bytes(buffer.getvalue())
    started = time.perf_counter()
    with pytest.raises(Invalid):
        validate_epub(path, EpubLimits(50 * 1024 * 1024, 200, 10_000))
    assert time.perf_counter() - started < 1.0

    huge = b"<container>" + b"<!-- x -->" * 20_000 + b"</container>"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", huge, compress_type=zipfile.ZIP_STORED)
    path.write_bytes(buffer.getvalue())
    with pytest.raises(Invalid, match="Steuerdateien"):
        validate_epub(path, LIMITS)


def test_entry_count_is_checked_before_the_directory_is_parsed(tmp_path, monkeypatch) -> None:
    data = bytearray(make_epub())
    index = data.rfind(b"PK\x05\x06")
    # Anzahl der Einträge im Verzeichnisende auf 60000 setzen.
    data[index + 8 : index + 12] = struct.pack("<HH", 60000, 60000)
    path = tmp_path / "viele.epub"
    path.write_bytes(bytes(data))

    def must_not_parse(*args, **kwargs):
        raise AssertionError("Das Verzeichnis wurde trotz zu vieler Einträge eingelesen")

    monkeypatch.setattr(zipfile, "ZipFile", must_not_parse)
    with pytest.raises(Invalid, match="zu viele"):
        validate_epub(path, LIMITS)

    data[index + 8 : index + 12] = struct.pack("<HH", 4, 4)
    data[index + 12 : index + 16] = struct.pack("<I", 300 * 1024 * 1024)
    path.write_bytes(bytes(data))
    with pytest.raises(Invalid, match="Inhaltsverzeichnis"):
        validate_epub(path, LIMITS)


# --------------------------------------------------------------------------- Backups

needs_restic = pytest.mark.skipif(shutil.which("restic") is None, reason="restic fehlt")


@needs_restic
def test_backup_contains_no_short_lived_secrets(make_env) -> None:
    import sqlite3

    env = make_env(BACKUP_PASSWORD="ein-backup-passwort-fuer-tests")
    admin = env.login()
    book_id = admin.published_book()
    api = env.api()
    created = api.post(
        "/api/v1/links",
        json={"book_id": book_id},
        headers={"Idempotency-Key": "c0ffee00-1111-2222-3333-444455556666"},
    ).json()
    env.new_client().get(f"/d/{created['code']}/pdf")
    with env.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM download_grants").fetchone()[0] == 1
        wrapped = conn.execute("SELECT wrapped_secret FROM idempotency_keys").fetchone()[0]
    assert wrapped

    assert env.app.state.ctx.backups.run("manual")[0]["status"] == "ok"
    staged = env.settings.data_dir / "backup-staging" / "db.sqlite3"
    conn = sqlite3.connect(staged)
    try:
        for table in ("sessions", "download_grants", "idempotency_keys"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608
        assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 1
    finally:
        conn.close()
    assert wrapped.encode() not in staged.read_bytes()  # auch nicht in freien Seiten
    # Die laufende App behält ihre Daten.
    with env.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


@needs_restic
def test_backup_is_exclusive_across_processes(make_env) -> None:
    env = make_env(BACKUP_PASSWORD="ein-backup-passwort-fuer-tests")
    backups = env.app.state.ctx.backups
    env.settings.data_dir.mkdir(parents=True, exist_ok=True)
    # Ein anderer Prozess (z. B. `ebookctl backup`) hält die Sperre.
    with open(env.settings.data_dir / "backup.lock", "w") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(Conflict, match="läuft bereits"):
            backups.run("manual")
    assert backups.run("manual")[0]["status"] == "ok"
    assert not env.app.state.ctx.storage.backup_running.is_set()


def test_backup_flag_is_visible_to_other_processes_and_expires(env: Env) -> None:
    from ebookapp.storage import Storage

    storage = env.app.state.ctx.storage
    other_process = Storage(env.settings.books_dir, env.settings.tmp_dir)
    storage.backup_running.set()
    assert other_process.backup_running.is_set()
    old = time.time() - 13 * 3600
    os.utime(storage.backup_running.path, (old, old))
    assert not other_process.backup_running.is_set()  # liegen gebliebene Markierung verfällt
    storage.backup_running.clear()


@needs_restic
def test_failed_restore_puts_the_previous_state_back(make_env, monkeypatch) -> None:
    from ebookapp import backup as backup_module

    env = make_env(BACKUP_PASSWORD="ein-backup-passwort-fuer-tests")
    admin = env.login()
    pdf = make_pdf("vor dem Fehlversuch")
    book_id = admin.published_book("Bleibt erhalten", pdf)
    _, path = admin.create_link(book_id)
    backups = env.app.state.ctx.backups
    assert backups.run("manual")[0]["status"] == "ok"
    newer = admin.published_book("Nach dem Backup")

    real_replace = os.replace

    def failing_replace(source, target):
        # Nur das Ablegen der wiederhergestellten Bücher schlägt fehl.
        if str(target) == str(env.settings.books_dir) and "restore-tmp" in str(source):
            raise OSError("Platte voll")
        return real_replace(source, target)

    monkeypatch.setattr(backup_module.os, "replace", failing_replace)
    monkeypatch.setattr(backup_module.time, "sleep", lambda seconds: None)
    with pytest.raises(OSError, match="Platte voll"):
        backups.restore("local", "latest", apply=True)
    monkeypatch.undo()

    assert not env.settings.maintenance_flag.exists()
    assert not list(env.settings.data_dir.glob("pre-restore-*"))
    assert env.new_client().get(f"{path}/pdf").content == pdf
    with env.db() as conn:  # der neuere Stand ist unverändert vorhanden
        assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 2
    assert (env.settings.books_dir / newer).is_dir()


def test_all_scopes_constant_matches_schema() -> None:
    from ebookapp.schemas import SCOPES

    assert list(SCOPES) == ALL_SCOPES
