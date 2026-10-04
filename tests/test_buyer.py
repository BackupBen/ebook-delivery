"""Käuferseite, Dateiauslieferung, Zählweise und Zugriffsschutz."""

from __future__ import annotations

import logging
import re
import threading

import pytest

from ebookapp.db import iso_in, open_db
from ebookapp.services import links as link_service
from tests.conftest import Env, make_epub, make_pdf, make_png

# Drei Käufer an verschiedenen Internetanschlüssen (Adresse laut Reverse Proxy).
UA_A = {"X-Forwarded-For": "198.51.100.10"}
UA_B = {"X-Forwarded-For": "203.0.113.20"}
UA_C = {"X-Forwarded-For": "192.0.2.30"}


def _count(env: Env, link_id: str) -> int:
    with env.db() as conn:
        return conn.execute("SELECT download_count FROM links WHERE id = ?", (link_id,)).fetchone()[
            0
        ]


def _events(env: Env) -> int:
    with env.db() as conn:
        return conn.execute("SELECT COUNT(*) FROM download_events").fetchone()[0]


# --------------------------------------------------------------------------- Gesamtablauf


def test_upload_link_download_revoke_flow(env: Env, admin) -> None:
    pdf, epub = make_pdf("Inhalt A", 50_000), make_epub("Inhalt A")
    book_id = admin.published_book("Mein großes Buch", pdf, epub)
    link_id, path = admin.create_link(book_id, label="Bestellung 1")

    buyer = env.new_client()  # ohne Anmeldung, ohne Cookies
    page = buyer.get(path)
    assert page.status_code == 200
    assert "Mein großes Buch" in page.text
    assert f"{path}/pdf" in page.text
    assert f"{path}/epub" in page.text

    got_pdf = buyer.get(f"{path}/pdf")
    assert got_pdf.status_code == 200
    assert got_pdf.content == pdf
    assert got_pdf.headers["content-type"] == "application/pdf"
    assert got_pdf.headers["content-disposition"].startswith("attachment;")
    assert "Mein%20gro%C3%9Fes%20Buch.pdf" in got_pdf.headers["content-disposition"]
    assert got_pdf.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in got_pdf.headers["content-security-policy"]
    assert got_pdf.headers["accept-ranges"] == "bytes"

    got_epub = buyer.get(f"{path}/epub")
    assert got_epub.status_code == 200
    assert got_epub.content == epub
    assert got_epub.headers["content-type"] == "application/epub+zip"
    assert _count(env, link_id) == 2

    revoke = admin.post(f"/admin/links/{link_id}/revoke", {"confirm": "1"})
    assert revoke.status_code == 303
    assert buyer.get(path).status_code == 410
    assert "widerrufen" in buyer.get(path).text
    assert buyer.get(f"{path}/pdf").status_code == 410
    assert buyer.get(f"{path}/epub").status_code == 410
    assert buyer.head(f"{path}/pdf").status_code == 410


def test_revoke_requires_confirmation(env: Env, admin) -> None:
    book_id = admin.published_book()
    link_id, path = admin.create_link(book_id)
    response = admin.post(f"/admin/links/{link_id}/revoke")
    assert response.status_code == 422
    assert env.new_client().get(path).status_code == 200


def test_disable_and_enable(env: Env, admin) -> None:
    book_id = admin.published_book()
    link_id, path = admin.create_link(book_id)
    buyer = env.new_client()
    assert admin.post(f"/admin/links/{link_id}/status", {"status": "disabled"}).status_code == 303
    assert buyer.get(path).status_code == 403
    assert "deaktiviert" in buyer.get(path).text
    assert buyer.get(f"{path}/pdf").status_code == 403
    assert admin.post(f"/admin/links/{link_id}/status", {"status": "active"}).status_code == 303
    assert buyer.get(f"{path}/pdf").status_code == 200


# --------------------------------------------------------------------------- Seite und Header


def test_buyer_page_privacy_headers_and_no_third_parties(env: Env, admin) -> None:
    book_id = admin.published_book()
    cover = admin.post(
        f"/admin/books/{book_id}/cover", files={"cover": ("c.png", make_png(), "image/png")}
    )
    assert cover.status_code == 303
    _, path = admin.create_link(book_id)
    buyer = env.new_client()
    page = buyer.get(path)

    assert page.headers["referrer-policy"] == "no-referrer"
    assert "noindex" in page.headers["x-robots-tag"]
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-frame-options"] == "DENY"
    assert "max-age" in page.headers["strict-transport-security"]
    csp = page.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src" not in csp  # keine Skripte erlaubt
    assert 'name="robots" content="noindex' in page.text
    assert 'name="referrer" content="no-referrer"' in page.text

    # Keine Drittanbieter: keine absoluten URLs, keine Skripte, keine Formulare.
    assert not re.findall(r"""(?:src|href|action)=["'](?:https?:)?//""", page.text)
    assert "<script" not in page.text
    assert "<form" not in page.text
    assert "set-cookie" not in page.headers

    image = buyer.get(f"{path}/cover")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"
    # Hinweis ohne Versprechen eines Kopierschutzes
    assert "gib ihn nicht weiter" in page.text
    assert "Kopierschutz" not in page.text


def test_robots_txt_and_home_reveal_nothing(env: Env, admin) -> None:
    admin.published_book("Streng geheimer Titel")
    anon = env.new_client()
    assert anon.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"
    home = anon.get("/")
    assert home.status_code == 200
    assert "Streng geheimer Titel" not in home.text
    for path in ("/d", "/d/", "/books", "/links", "/api/v1/books", "/admin/books", "/sitemap.xml"):
        response = anon.get(path)
        assert response.status_code in (303, 401, 404), path
        assert "Streng geheimer Titel" not in response.text


def test_unknown_and_malformed_codes(env: Env, admin) -> None:
    admin.published_book()
    anon = env.new_client()
    for code in ("a" * 43, "kurz", "a" * 500, "x" * 42 + "!"):
        response = anon.get(f"/d/{code}")
        assert response.status_code == 404
        assert "Link nicht gefunden" in response.text
    assert anon.get("/d/..%2f..%2fetc%2fpasswd").status_code == 404
    assert anon.get(f"/d/{'a' * 43}/pdf").status_code == 404
    assert anon.get(f"/d/{'a' * 43}/cover").status_code == 404


def test_guessing_codes_is_rate_limited(make_env) -> None:
    env = make_env(INVALID_LINK_MAX="5")
    admin = env.login()
    book_id = admin.published_book()
    _, path = admin.create_link(book_id)
    attacker = env.new_client()
    for index in range(5):
        assert attacker.get(f"/d/{str(index) * 43}").status_code == 404
    blocked = attacker.get(f"/d/{'z' * 43}")
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    # Auch gültige Links sind für diese Adresse vorübergehend gesperrt.
    assert attacker.get(path).status_code == 429


def test_download_request_rate_limit(make_env) -> None:
    env = make_env(DOWNLOAD_RATE_PER_MINUTE="4")
    admin = env.login()
    book_id = admin.published_book()
    _, path = admin.create_link(book_id)
    buyer = env.new_client()
    statuses = [buyer.get(f"{path}/pdf").status_code for _ in range(6)]
    assert statuses[:4] == [200] * 4
    assert statuses[4:] == [429, 429]


def test_format_restriction(env: Env, admin) -> None:
    book_id = admin.published_book()
    _, path = admin.create_link(book_id, formats=["epub"])
    buyer = env.new_client()
    page = buyer.get(path)
    assert f"{path}/epub" in page.text
    assert f"{path}/pdf" not in page.text
    assert buyer.get(f"{path}/pdf").status_code == 404
    assert buyer.get(f"{path}/epub").status_code == 200
    assert buyer.get(f"{path}/exe").status_code == 404
    assert buyer.get(f"{path}/../../admin").status_code in (303, 404)


def test_link_grants_access_to_exactly_one_book(env: Env, admin) -> None:
    first = admin.published_book("Buch Eins", make_pdf("eins"))
    admin.published_book("Buch Zwei", make_pdf("zwei"))
    _, path = admin.create_link(first)
    buyer = env.new_client()
    page = buyer.get(path)
    assert "Buch Eins" in page.text
    assert "Buch Zwei" not in page.text
    assert b"eins" in buyer.get(f"{path}/pdf").content


# --------------------------------------------------------------------------- Ablauf


def test_expiry(env: Env, admin) -> None:
    book_id = admin.published_book()
    link_id, path = admin.create_link(book_id, expires_at="2099-12-31T23:59")
    buyer = env.new_client()
    page = buyer.get(path)
    assert page.status_code == 200
    assert "31.12.2099" in page.text
    assert buyer.get(f"{path}/pdf").status_code == 200

    with env.db() as conn:
        conn.execute("UPDATE links SET expires_at = ? WHERE id = ?", (iso_in(seconds=-5), link_id))
    expired = buyer.get(path)
    assert expired.status_code == 410
    assert "abgelaufen" in expired.text
    # Auch ein bereits begonnener Download lässt sich nach Ablauf nicht fortsetzen.
    assert buyer.get(f"{path}/pdf", headers={"Range": "bytes=100-"}).status_code == 410
    assert buyer.head(f"{path}/pdf").status_code == 410


def test_expiry_in_the_past_is_rejected(env: Env, admin) -> None:
    book_id = admin.published_book()
    new = env.client.get(f"/admin/links/new?book_id={book_id}")
    token = re.search(r'name="form_token" value="([^"]+)"', new.text).group(1)
    response = admin.post(
        "/admin/links",
        {
            "book_id": book_id,
            "formats": ["pdf"],
            "form_token": token,
            "expires_at": "2001-01-01T00:00",
        },
    )
    assert response.status_code == 422
    assert "in der Zukunft" in response.text


# --------------------------------------------------------------------------- Downloadlimit


def test_limit_with_head_range_and_resume(env: Env, admin) -> None:
    pdf = make_pdf("Bereich", 100_000)
    book_id = admin.published_book("Limitbuch", pdf)
    link_id, path = admin.create_link(book_id, max_downloads="2")
    url = f"{path}/pdf"
    buyer = env.new_client()

    # HEAD zählt nie.
    for _ in range(5):
        head = buyer.head(url, headers=UA_A)
        assert head.status_code == 200
        assert head.headers["content-length"] == str(len(pdf))
        assert head.content == b""
    assert _count(env, link_id) == 0

    # Erste Teilanfrage: zählt einmal.
    first = buyer.get(url, headers={**UA_A, "Range": "bytes=0-999"})
    assert first.status_code == 206
    assert first.headers["content-range"] == f"bytes 0-999/{len(pdf)}"
    assert first.content == pdf[:1000]
    assert _count(env, link_id) == 1

    # Parallele Segmente, Wiederaufnahme und vollständiger Abruf desselben Geräts: kein Zählen.
    segments = [
        buyer.get(url, headers={**UA_A, "Range": f"bytes={start}-{start + 9999}"})
        for start in range(1000, 91_000, 10_000)
    ]
    assert all(item.status_code == 206 for item in segments)
    resume = buyer.get(url, headers={**UA_A, "Range": "bytes=91000-"})
    assert resume.status_code == 206
    rebuilt = first.content + b"".join(item.content for item in segments) + resume.content
    assert rebuilt == pdf
    assert buyer.get(url, headers=UA_A).content == pdf
    assert buyer.get(url, headers={**UA_A, "Range": "bytes=-500"}).content == pdf[-500:]
    assert _count(env, link_id) == 1
    assert _events(env) == 1

    # Ein anderes Gerät zählt neu, auch wenn es mit einer Teilanfrage beginnt.
    other = buyer.get(url, headers={**UA_B, "Range": "bytes=5000-5999"})
    assert other.status_code == 206
    assert _count(env, link_id) == 2

    # Limit erreicht: ein drittes Gerät wird abgewiesen, auch per HEAD.
    third = buyer.get(url, headers=UA_C)
    assert third.status_code == 410
    assert "Downloadlimit erreicht" in third.text
    assert buyer.head(url, headers=UA_C).status_code == 410
    assert buyer.get(path, headers=UA_C).status_code == 410

    # Die beiden bisherigen Geräte dürfen ihren Download fortsetzen.
    assert buyer.get(url, headers={**UA_A, "Range": "bytes=50000-"}).status_code == 206
    assert buyer.head(url, headers=UA_B).status_code == 200
    assert _count(env, link_id) == 2
    assert _events(env) == 2


def test_resume_after_window_counts_again_or_is_denied(make_env) -> None:
    env = make_env(DOWNLOAD_WINDOW_MINUTES="30")
    admin = env.login()
    book_id = admin.published_book()
    link_id, path = admin.create_link(book_id, max_downloads="2")
    url = f"{path}/pdf"
    buyer = env.new_client()

    assert buyer.get(url, headers=UA_A).status_code == 200
    with env.db() as conn:  # 31 Minuten Pause
        conn.execute("UPDATE download_grants SET last_seen_at = ?", (iso_in(minutes=-31),))
    assert buyer.get(url, headers={**UA_A, "Range": "bytes=10-"}).status_code == 206
    assert _count(env, link_id) == 2
    with env.db() as conn:
        conn.execute("UPDATE download_grants SET last_seen_at = ?", (iso_in(minutes=-31),))
    assert buyer.get(url, headers=UA_A).status_code == 410
    assert _count(env, link_id) == 2


def test_window_has_absolute_maximum(make_env) -> None:
    env = make_env(DOWNLOAD_WINDOW_MAX_HOURS="2")
    admin = env.login()
    book_id = admin.published_book()
    link_id, path = admin.create_link(book_id)
    buyer = env.new_client()
    assert buyer.get(f"{path}/pdf", headers=UA_A).status_code == 200
    with env.db() as conn:  # durchgehend aktiv, aber vor über 2 Stunden begonnen
        conn.execute("UPDATE download_grants SET first_seen_at = ?", (iso_in(hours=-3),))
    assert buyer.get(f"{path}/pdf", headers=UA_A).status_code == 200
    assert _count(env, link_id) == 2


def test_limit_counts_each_format(env: Env, admin) -> None:
    book_id = admin.published_book()
    link_id, path = admin.create_link(book_id, max_downloads="2")
    buyer = env.new_client()
    assert buyer.get(f"{path}/pdf").status_code == 200
    assert buyer.get(f"{path}/epub").status_code == 200
    assert _count(env, link_id) == 2
    assert buyer.get(f"{path}/pdf").status_code == 200  # Wiederholung im Zeitfenster
    assert env.new_client().get(f"{path}/pdf", headers=UA_B).status_code == 410


def test_unlimited_link_counts_distinct_downloads_only(env: Env, admin) -> None:
    book_id = admin.published_book()
    link_id, path = admin.create_link(book_id)
    buyer = env.new_client()
    for _ in range(4):
        assert buyer.get(f"{path}/pdf", headers=UA_A).status_code == 200
    assert _count(env, link_id) == 1


def test_parallel_requests_count_once(env: Env, admin) -> None:
    """Gleichzeitige Anfragen eines Geräts verbrauchen das Limit nur einmal."""
    book_id = admin.published_book()
    link_id, path = admin.create_link(book_id, max_downloads="1")
    code = path.rsplit("/", 1)[-1]
    results: list[object] = []
    barrier = threading.Barrier(12)

    def worker() -> None:
        with open_db(env.settings.db_path) as conn:
            view = link_service.resolve(conn, code)
            barrier.wait()
            try:
                _, counted = link_service.authorize_download(
                    conn,
                    view,
                    "pdf",
                    "geraet-1",
                    head=False,
                    window_minutes=60,
                    window_max_hours=24,
                )
                results.append(counted)
            except link_service.DownloadDenied as denied:
                results.append(denied.state)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1
    assert results.count(False) == 11
    assert _count(env, link_id) == 1


def test_parallel_devices_never_exceed_limit(env: Env, admin) -> None:
    book_id = admin.published_book()
    link_id, path = admin.create_link(book_id, max_downloads="3")
    code = path.rsplit("/", 1)[-1]
    results: list[object] = []
    barrier = threading.Barrier(10)

    def worker(index: int) -> None:
        with open_db(env.settings.db_path) as conn:
            view = link_service.resolve(conn, code)
            barrier.wait()
            try:
                link_service.authorize_download(
                    conn,
                    view,
                    "pdf",
                    f"geraet-{index}",
                    head=False,
                    window_minutes=60,
                    window_max_hours=24,
                )
                results.append("ok")
            except link_service.DownloadDenied as denied:
                results.append(denied.state)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("ok") == 3
    assert results.count("exhausted") == 7
    assert _count(env, link_id) == 3


def test_fingerprint_is_not_reversible_and_gets_removed(env: Env, admin) -> None:
    book_id = admin.published_book()
    _, path = admin.create_link(book_id)
    buyer = env.new_client()
    buyer.get(f"{path}/pdf", headers={"User-Agent": "Sehr-Eindeutiger-Browser/9"})
    with env.db() as conn:
        grant = conn.execute("SELECT * FROM download_grants").fetchone()
        assert re.fullmatch(r"[0-9a-f]{32}", grant["client_hash"])
        dump = "\n".join(conn.iterdump())
        # Das Sicherheitsprotokoll enthält bewusst Adresse und Browser von Anmeldungen an
        # der Verwaltung, nie von Käufern.
        buyer_tables = "\n".join(
            line for line in conn.iterdump() if not line.startswith('INSERT INTO "security_events"')
        )
    assert "Sehr-Eindeutiger-Browser" not in dump
    assert "testclient" not in buyer_tables

    from ebookapp.services import misc

    with env.db() as conn:
        conn.execute("UPDATE download_grants SET last_seen_at = ?", (iso_in(hours=-2),))
        misc.housekeeping(conn, env.settings)
        assert conn.execute("SELECT COUNT(*) FROM download_grants").fetchone()[0] == 0


# --------------------------------------------------------------------------- Range-Details


def test_range_semantics(env: Env, admin) -> None:
    pdf = make_pdf("Range", 20_000)
    book_id = admin.published_book("R", pdf)
    _, path = admin.create_link(book_id)
    url = f"{path}/pdf"
    buyer = env.new_client()

    full = buyer.get(url)
    etag = full.headers["etag"]
    assert full.headers["content-length"] == str(len(pdf))

    invalid = buyer.get(url, headers={"Range": f"bytes={len(pdf) + 10}-"})
    assert invalid.status_code == 416
    assert invalid.headers["content-range"] == f"bytes */{len(pdf)}"

    matching = buyer.get(url, headers={"Range": "bytes=0-9", "If-Range": etag})
    assert matching.status_code == 206
    assert matching.content == pdf[:10]

    # Hat sich die Datei geändert (anderes ETag), wird vollständig neu geliefert.
    stale = buyer.get(url, headers={"Range": "bytes=0-9", "If-Range": '"anderes-etag"'})
    assert stale.status_code == 200
    assert stale.content == pdf


# --------------------------------------------------------------------------- Geheimnisse


def test_code_is_only_stored_as_hash(env: Env, admin) -> None:
    book_id = admin.published_book()
    link_id, path = admin.create_link(book_id, label="Hashtest")
    code = path.rsplit("/", 1)[-1]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", code)  # 256 Bit
    with env.db() as conn:
        dump = "\n".join(conn.iterdump())
    assert code not in dump
    assert code[:16] not in dump

    # Auch die Verwaltungsseiten zeigen den Code später nicht mehr an.
    for page in ("/admin/links", f"/admin/links/{link_id}", f"/admin/books/{book_id}"):
        assert code not in env.client.get(page).text


def test_two_links_have_independent_random_codes(env: Env, admin) -> None:
    book_id = admin.published_book()
    codes = {admin.create_link(book_id)[1] for _ in range(5)}
    assert len(codes) == 5


def test_resubmitting_the_form_does_not_duplicate_the_link(env: Env, admin) -> None:
    book_id = admin.published_book()
    new = env.client.get(f"/admin/links/new?book_id={book_id}")
    token = re.search(r'name="form_token" value="([^"]+)"', new.text).group(1)
    data = {"book_id": book_id, "formats": ["pdf", "epub"], "form_token": token, "label": "x"}
    first = admin.post("/admin/links", data)
    second = admin.post("/admin/links", data)  # z. B. Seite neu geladen
    assert first.status_code == second.status_code == 201
    url = re.search(r'id="link-url"[^>]*value="([^"]+)"', first.text).group(1)
    assert url == re.search(r'id="link-url"[^>]*value="([^"]+)"', second.text).group(1)
    with env.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 1


def test_logs_contain_no_link_codes(env: Env, admin, caplog: pytest.LogCaptureFixture) -> None:
    from ebookapp.logging_setup import RedactingFormatter

    book_id = admin.published_book()
    with caplog.at_level(logging.DEBUG):
        _, path = admin.create_link(book_id)
        code = path.rsplit("/", 1)[-1]
        buyer = env.new_client()
        buyer.get(path)
        buyer.get(f"{path}/pdf")
        buyer.get(f"{path}/pdf", headers={"Range": "bytes=0-10"})
        buyer.get(f"/d/{'b' * 43}")
    app_records = [r for r in caplog.records if r.name.startswith("ebookapp")]
    assert any("/d/[link]/pdf" in r.getMessage() for r in app_records)
    for record in app_records:
        assert code not in record.getMessage()
        assert "b" * 43 not in record.getMessage()
    # Zweite Verteidigungslinie: der Formatter schwärzt Codes in jeder Logzeile.
    formatter = RedactingFormatter("%(message)s")
    line = formatter.format(
        logging.LogRecord("x", logging.INFO, "", 0, f"GET https://h/d/{code}/pdf", None, None)
    )
    assert code not in line


def test_message_template(env: Env, admin) -> None:
    saved = admin.post(
        "/admin/settings",
        {
            "max_pdf_mb": "300",
            "max_epub_mb": "100",
            "max_cover_mb": "10",
            "message_template": "Moin!\nDein Buch {titel}: {link}\n{gueltigkeit}",
        },
    )
    assert saved.status_code == 303
    book_id = admin.published_book("Vorlagenbuch")
    new = env.client.get(f"/admin/links/new?book_id={book_id}")
    token = re.search(r'name="form_token" value="([^"]+)"', new.text).group(1)
    response = admin.post(
        "/admin/links",
        {"book_id": book_id, "formats": ["pdf"], "form_token": token, "max_downloads": "3"},
    )
    assert "Moin!" in response.text
    assert "Dein Buch Vorlagenbuch: https://ebooks.example.com/d/" in response.text
    assert "dauerhaft gültig" in response.text
    assert "höchstens 3 Downloads" in response.text

    bad = admin.post(
        "/admin/settings",
        {
            "max_pdf_mb": "300",
            "max_epub_mb": "100",
            "max_cover_mb": "10",
            "message_template": "ohne Platzhalter",
        },
    )
    assert bad.status_code == 422
