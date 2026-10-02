"""Sicherheits-Header, Host-Prüfung, Wartungsmodus, Persistenz und Hilfsfunktionen."""

from __future__ import annotations

import io
import json
import re

import pytest
from starlette.requests import Request

from ebookapp import cli
from ebookapp.config import ConfigError, load_settings
from ebookapp.logging_setup import redact, redact_path
from ebookapp.ratelimit import RateLimiter
from ebookapp.security import (
    client_fingerprint,
    hash_password,
    new_link_code,
    rate_key,
    unwrap_code,
    verify_password,
    wrap_code,
)
from ebookapp.web.common import client_ip
from tests.conftest import ADMIN_PASSWORD, BASE_URL, Env, make_epub, make_pdf, make_png


def test_admin_security_headers(env: Env, admin) -> None:
    page = env.client.get("/admin/books")
    headers = page.headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "same-origin"
    assert headers["cache-control"] == "no-store"
    assert "noindex" in headers["x-robots-tag"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert "unsafe-inline" not in headers["content-security-policy"]
    assert "max-age=31536000" in headers["strict-transport-security"]
    assert "server" not in headers
    assert re.fullmatch(r"[0-9a-f]{16}", headers["x-request-id"])
    # Die Seiten kommen ohne Inline-Skripte, Inline-Styles und fremde Quellen aus.
    for path in ("/admin/books", "/admin/links", "/admin/settings", "/admin/books/new"):
        text = env.client.get(path).text
        assert not re.findall(r"<script(?![^>]*\bsrc=)", text), path
        assert 'style="' not in text, path
        assert not re.findall(r"""(?:src|href|action)=["'](?:https?:)?//""", text), path


def test_static_assets_are_public_but_contain_no_books(env: Env, admin) -> None:
    admin.published_book()
    anon = env.new_client()
    css = anon.get("/static/app.css")
    assert css.status_code == 200
    assert css.headers["cache-control"] == "public, max-age=3600"
    assert anon.get("/static/").status_code == 404
    assert anon.get("/static/../config.py").status_code == 404
    assert anon.get("/static/%2e%2e/%2e%2e/config.py").status_code == 404


def test_unknown_host_is_rejected(env: Env) -> None:
    response = env.client.get("/", headers={"Host": "evil.example"})
    assert response.status_code == 400
    assert env.client.get("/healthz", headers={"Host": "127.0.0.1:8000"}).status_code == 200
    assert env.client.get("/healthz", headers={"Host": "localhost"}).status_code == 200


def test_healthcheck(env: Env) -> None:
    response = env.client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_maintenance_mode(env: Env, admin) -> None:
    book_id = admin.published_book()
    _, path = admin.create_link(book_id)
    env.settings.maintenance_flag.write_text("restore\n")
    try:
        for url in (path, f"{path}/pdf", "/admin/books", "/"):
            response = env.client.get(url)
            assert response.status_code == 503
            assert response.headers["retry-after"] == "60"
        api = env.new_client().get("/api/v1/books")
        assert api.status_code == 503
        assert api.json()["error"]["code"] == "maintenance"
        assert env.client.get("/healthz").json() == {"status": "maintenance"}
    finally:
        env.settings.maintenance_flag.unlink()
    assert env.client.get(path).status_code == 200


def test_unexpected_errors_show_copyable_error_id(env: Env, admin, monkeypatch) -> None:
    from ebookapp.services import books

    def boom(*args, **kwargs):
        raise RuntimeError("absichtlich")

    monkeypatch.setattr(books, "list_books", boom)
    page = env.client.get("/admin/books")
    assert page.status_code == 500
    assert "Fehler-ID" in page.text
    assert page.headers["x-request-id"] in page.text
    assert "absichtlich" not in page.text  # keine internen Details nach außen
    api = env.api().get("/api/v1/books")
    assert api.status_code == 500
    assert api.json()["error"]["code"] == "internal_error"
    assert api.json()["error"]["request_id"] == api.headers["x-request-id"]


def test_data_survives_restart(make_env) -> None:
    """Simuliert einen Redeploy: neue App-Instanz auf denselben Volumes."""
    first = make_env()
    admin = first.login()
    pdf = make_pdf("bleibt", 40_000)
    book_id = admin.published_book("Dauerhaft", pdf)
    admin.post(f"/admin/books/{book_id}/cover", files={"cover": ("c.png", make_png(), "image/png")})
    link_id, path = admin.create_link(book_id, label="bleibt", max_downloads="5")
    admin.post(
        "/admin/settings",
        {
            "max_pdf_mb": "123",
            "max_epub_mb": "45",
            "max_cover_mb": "6",
            "message_template": "Eigene Vorlage {link}",
        },
    )
    api_key = first.api_key(["books:read"])
    first.new_client().get(f"{path}/pdf")
    session_cookie = first.client.cookies.get("__Host-ebook_session")

    second = make_env()  # gleicher Datenordner, neuer Prozesszustand
    assert second.app is not first.app
    buyer = second.new_client()
    assert "Dauerhaft" in buyer.get(path).text
    assert buyer.get(f"{path}/pdf").content == pdf
    assert buyer.get(f"{path}/cover").status_code == 200

    api = second.new_client()
    api.headers["Authorization"] = f"Bearer {api_key}"
    assert api.get("/api/v1/books").json()["items"][0]["id"] == book_id

    second.client.cookies.set("__Host-ebook_session", session_cookie)
    settings_page = second.client.get("/admin/settings")
    assert settings_page.status_code == 200  # Sitzung bleibt gültig
    assert 'value="123"' in settings_page.text
    assert "Eigene Vorlage {link}" in settings_page.text
    detail = second.client.get(f"/admin/links/{link_id}").text
    assert "bleibt" in detail
    assert "1 von 5" in detail


def test_schema_migration_is_idempotent(make_env) -> None:
    from ebookapp.db import MIGRATIONS, migrate

    env = make_env()
    assert migrate(env.settings.db_path) == len(MIGRATIONS)
    assert migrate(env.settings.db_path) == len(MIGRATIONS)
    with env.db() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_configuration_validation() -> None:
    base = {"SECRET_KEY": "x" * 40}
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        load_settings({})
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        load_settings({"SECRET_KEY": "zu-kurz"})
    with pytest.raises(ConfigError, match="PUBLIC_BASE_URL"):
        load_settings({**base, "PUBLIC_BASE_URL": "ebooks.example.com"})
    with pytest.raises(ConfigError, match="Pfad"):
        load_settings({**base, "PUBLIC_BASE_URL": "https://ebooks.example.com/shop"})
    with pytest.raises(ConfigError, match="APP_TIMEZONE"):
        load_settings({**base, "APP_TIMEZONE": "Mars/Olympus"})
    with pytest.raises(ConfigError, match="MAX_UPLOAD_MB"):
        load_settings({**base, "MAX_UPLOAD_MB": "viel"})

    settings = load_settings({**base, "PUBLIC_BASE_URL": "https://ebooks.example.com/"})
    assert settings.public_base_url == "https://ebooks.example.com"
    assert settings.cookie_secure is True
    assert settings.session_cookie_name.startswith("__Host-")
    assert "ebooks.example.com" in settings.allowed_hosts
    assert (
        load_settings({**base, "PUBLIC_BASE_URL": "http://localhost:8000"}).cookie_secure is False
    )
    assert "SECRET" not in repr(settings.backup_offsite_env)


def test_no_hostnames_or_secrets_in_source() -> None:
    """Im Paket sind keine Server-, Domain- oder Kontonamen fest verdrahtet."""
    from pathlib import Path

    root = Path(__file__).parent.parent
    forbidden = re.compile(
        r"your-ebook|benito|backupben|136\.243\.|vps3|avatarforge|vidforge|spaceship",
        re.IGNORECASE,
    )
    checked = 0
    for path in [
        *root.glob("src/**/*"),
        root / "Dockerfile",
        root / "docker-compose.yaml",
        *root.glob(".github/**/*"),
        *root.glob("docs/*"),
        root / "README.md",
        root / ".env.example",
        root / "pyproject.toml",
        *root.glob("scripts/*"),
        *[path for path in root.glob("tests/*.py") if path.name != "test_misc.py"],
    ]:
        if not path.is_file() or "vendor" in path.parts or path.suffix in {".pyc", ".png"}:
            continue
        checked += 1
        match = forbidden.search(path.read_text(encoding="utf-8", errors="ignore"))
        assert match is None, f"{path}: {match.group(0) if match else ''}"
    assert checked > 30


def _request(peer: str, headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request(
        {
            "type": "http",
            "client": (peer, 1234),
            "headers": raw,
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "scheme": "http",
            "server": ("x", 80),
        }
    )


def test_client_ip_only_trusts_configured_proxies() -> None:
    settings = load_settings({"SECRET_KEY": "x" * 40})
    # Direkter Zugriff: der Header wird ignoriert.
    assert (
        client_ip(_request("203.0.113.9", {"X-Forwarded-For": "1.2.3.4"}), settings)
        == "203.0.113.9"
    )
    # Über den Proxy: der letzte nicht vertrauenswürdige Eintrag zählt.
    assert (
        client_ip(_request("10.0.1.5", {"X-Forwarded-For": "198.51.100.7"}), settings)
        == "198.51.100.7"
    )
    spoofed = _request("10.0.1.5", {"X-Forwarded-For": "6.6.6.6, 198.51.100.7, 10.0.1.4"})
    assert client_ip(spoofed, settings) == "198.51.100.7"
    assert client_ip(_request("10.0.1.5", {}), settings) == "10.0.1.5"
    assert client_ip(_request("10.0.1.5", {"X-Forwarded-For": "kein-ip"}), settings) == "10.0.1.5"
    # Mehrere Header-Zeilen: Die vom Proxy angehängte (letzte) Zeile zählt.
    two_lines = Request(
        {
            "type": "http",
            "client": ("10.0.1.5", 1),
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "scheme": "http",
            "server": ("x", 80),
            "headers": [(b"x-forwarded-for", b"6.6.6.6"), (b"x-forwarded-for", b"203.0.113.9")],
        }
    )
    assert client_ip(two_lines, settings) == "203.0.113.9"
    mapped = _request("10.0.1.5", {"X-Forwarded-For": "::ffff:198.51.100.7"})
    assert client_ip(mapped, settings) == "198.51.100.7"


def test_download_counting_uses_forwarded_client_address(env: Env, admin) -> None:
    book_id = admin.published_book()
    _, path = admin.create_link(book_id)
    proxy = env.new_client(client=("10.0.0.2", 40000))
    for address, expected in (("198.51.100.1", 1), ("198.51.100.99", 1), ("203.0.113.5", 2)):
        proxy.get(f"{path}/pdf", headers={"X-Forwarded-For": address, "User-Agent": "UA"})
        with env.db() as conn:
            count = conn.execute("SELECT download_count FROM links").fetchone()[0]
        assert count == expected, address  # gleiche /24 = gleiches Gerät


def test_rate_limiter() -> None:
    limiter = RateLimiter()
    assert [limiter.hit("a", 3, 60) for _ in range(3)] == [0, 0, 0]
    assert limiter.hit("a", 3, 60) > 0
    assert limiter.hit("b", 3, 60) == 0
    assert limiter.retry_after("a", 3, 60) > 0
    limiter.reset("a")
    assert limiter.retry_after("a", 3, 60) == 0
    limiter.record("c", 60)
    assert limiter.retry_after("c", 1, 60) > 0
    assert limiter.retry_after("c", 1, 0.0) == 0


def test_redaction() -> None:
    code = new_link_code()
    assert code not in redact(f"GET /d/{code}/pdf HTTP/1.1")
    assert redact_path(f"/d/{code}/pdf") == "/d/[link]/pdf"
    assert redact_path(f"/d/{code}/pdf/noch/mehr") == "/d/[link]/pdf"
    assert redact_path("/admin/books") == "/admin/books"
    # Auch ungewöhnlich geformte Pfade dürfen den Code nicht ins Log tragen.
    for odd in (
        f"/d//{code}",
        f"/d/x/{code}",
        f"/D/{code}",
        f"/d/{code}/{code}",
        f"//d/{code}/pdf",
        f"/irgendwo/{code}",
        f"/d/{code}/PDF",
    ):
        assert code not in redact_path(odd), odd
    assert redact_path(f"/d/{code}/PDF") == "/d/[link]/pdf"
    assert code not in redact(f"token={code} ende")
    key = "ebk_0123456789ab_" + "A" * 43
    assert key not in redact(f"Authorization: Bearer {key}")
    assert "geheim123456" not in redact("authorization: bearer geheim123456")


def test_crypto_helpers() -> None:
    secret = b"s" * 40
    stored = hash_password("ein-langes-passwort")
    assert verify_password("ein-langes-passwort", stored)
    assert not verify_password("ein-anderes-passwort", stored)
    assert not verify_password("x", "kaputt")
    assert stored != hash_password("ein-langes-passwort")  # zufälliges Salt

    code = new_link_code()
    wrapped = wrap_code(secret, "api:key_1", "idem", "lnk_1", code)
    assert wrapped != code
    assert unwrap_code(secret, "api:key_1", "idem", "lnk_1", wrapped) == code
    assert unwrap_code(secret, "api:key_1", "anderer-key", "lnk_1", wrapped) != code
    assert unwrap_code(b"t" * 40, "api:key_1", "idem", "lnk_1", wrapped) != code

    one = client_fingerprint(secret, "198.51.100.10")
    assert one == client_fingerprint(secret, "198.51.100.200")  # gleiche /24
    assert one != client_fingerprint(secret, "198.51.101.10")
    assert one != client_fingerprint(b"t" * 40, "198.51.100.10")
    assert client_fingerprint(secret, "2001:db8:1:2::1") == client_fingerprint(
        secret, "2001:db8:1:ff::9"
    )
    assert client_fingerprint(secret, "2001:db8:1:2::1") != client_fingerprint(
        secret, "2001:db8:1:2ff::9"
    )
    assert "198.51" not in one
    # IPv4-Adressen in IPv6-Schreibweise zählen wie die IPv4-Adresse selbst und fallen
    # nicht alle in dasselbe IPv6-Netz.
    assert client_fingerprint(secret, "::ffff:198.51.100.10") == one
    assert client_fingerprint(secret, "64:ff9b::c633:640a") == one
    assert client_fingerprint(secret, "::ffff:1.2.3.4") != client_fingerprint(
        secret, "::ffff:203.0.113.77"
    )
    assert rate_key("2001:db8:1:2:aaaa::1") == rate_key("2001:db8:1:2:bbbb::2")
    assert rate_key("198.51.100.10") != rate_key("198.51.100.11")


def test_cli_set_password_and_openapi(make_env, monkeypatch, capsys) -> None:
    env = make_env()
    for name, value in (
        ("DATA_DIR", str(env.settings.data_dir)),
        ("BACKUP_DIR", str(env.settings.backup_dir)),
        ("SECRET_KEY", "x" * 40),
        ("PUBLIC_BASE_URL", BASE_URL),
    ):
        monkeypatch.setenv(name, value)
    env.login()

    monkeypatch.setattr("sys.stdin", io.StringIO("kurz\n"))
    assert cli.main(["set-password"]) == 1
    monkeypatch.setattr("sys.stdin", io.StringIO("neues-passwort-per-cli\n"))
    assert cli.main(["set-password"]) == 0
    assert env.client.get("/admin/books").status_code == 303  # Sitzungen beendet
    env.login(env.new_client(), password="neues-passwort-per-cli")
    with pytest.raises(AssertionError):
        env.login(env.new_client(), password=ADMIN_PASSWORD)

    capsys.readouterr()
    assert cli.main(["openapi"]) == 0
    spec = json.loads(capsys.readouterr().out)
    assert "/links" in spec["paths"]

    monkeypatch.delenv("SECRET_KEY")
    assert cli.main(["gc"]) == 2  # Konfigurationsfehler


def test_orphan_cleanup(env: Env, admin) -> None:
    import os
    import time

    from ebookapp.services.books import collect_orphans

    book_id = admin.published_book("Bleibt", make_pdf("bleibt"), make_epub())
    orphan = env.settings.books_dir / book_id / "verwaist.pdf"
    orphan.write_bytes(b"alt")
    fresh = env.settings.books_dir / book_id / "frisch.pdf"
    fresh.write_bytes(b"neu")
    old = time.time() - 7200
    os.utime(orphan, (old, old))
    for path in env.settings.books_dir.rglob("*"):
        if path.is_file() and path != fresh:
            os.utime(path, (old, old))

    with env.db() as conn:
        env.app.state.ctx.storage.backup_running.set()
        assert collect_orphans(conn, env.app.state.ctx.storage) == []  # nie während eines Backups
        env.app.state.ctx.storage.backup_running.clear()
        removed = collect_orphans(conn, env.app.state.ctx.storage)
    assert removed == [f"{book_id}/verwaist.pdf"]
    assert fresh.exists()  # junge Dateien bleiben (laufende Uploads)
    assert len([p for p in env.settings.books_dir.rglob("*") if p.is_file()]) == 3
