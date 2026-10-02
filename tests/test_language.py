"""Sprache je Buch: Käuferseite, Statusseiten und Versandnachricht."""

from __future__ import annotations

import re

from ebookapp.web.buyer_texts import format_datetime, from_accept_language
from tests.conftest import Env, make_epub, make_pdf


def _english_book(admin, title: str = "The Big Book") -> str:
    book_id = admin.create_book(title, language="en")
    edition_id = admin.draft_id(book_id)
    assert admin.upload(book_id, edition_id, make_pdf(title), make_epub()).status_code == 303
    assert admin.publish(book_id, edition_id).status_code == 303
    return book_id


def _created_link_page(env: Env, admin, book_id: str, **extra: str) -> str:
    new = env.client.get(f"/admin/links/new?book_id={book_id}")
    token = re.search(r'name="form_token" value="([^"]+)"', new.text).group(1)
    response = admin.post(
        "/admin/links", {"book_id": book_id, "formats": ["pdf"], "form_token": token, **extra}
    )
    assert response.status_code == 201, response.text
    return response.text


def test_new_books_default_to_german(env: Env, admin) -> None:
    book_id = admin.published_book("Deutsches Buch")
    _link_id, path = admin.create_link(book_id)
    page = env.new_client().get(path)
    assert '<html lang="de">' in page.text
    assert page.headers["content-language"] == "de"
    assert "Dein E-Book" in page.text
    assert "PDF herunterladen" in page.text


def test_english_book_page_is_english(env: Env, admin) -> None:
    book_id = _english_book(admin)
    _link_id, path = admin.create_link(book_id, max_downloads="5")
    page = env.new_client().get(path)
    assert page.status_code == 200
    assert '<html lang="en">' in page.text
    assert page.headers["content-language"] == "en"
    assert "Your e-book" in page.text
    assert "Download PDF" in page.text
    assert "Download EPUB" in page.text
    assert "for e-readers and reading apps" in page.text
    assert "Downloads remaining: 5." in page.text
    assert "Dein E-Book" not in page.text
    assert "herunterladen" not in page.text
    # Dateigrößen mit Dezimalpunkt
    assert re.search(r"\d+\.\d (KB|MB)", page.text)


def test_english_state_pages(env: Env, admin) -> None:
    book_id = _english_book(admin)
    link_id, path = admin.create_link(book_id)
    assert admin.post(f"/admin/links/{link_id}/status", {"status": "disabled"}).status_code == 303
    buyer = env.new_client()
    page = buyer.get(path)
    assert page.status_code == 403
    assert "Link currently disabled" in page.text
    assert "Please contact the seller." in page.text
    assert buyer.get(f"{path}/pdf").status_code == 403
    assert "Link currently disabled" in buyer.get(f"{path}/pdf").text


def test_language_can_be_changed_for_existing_links(env: Env, admin) -> None:
    book_id = admin.published_book("Umstellbuch")
    _link_id, path = admin.create_link(book_id)
    assert "Dein E-Book" in env.new_client().get(path).text
    edited = admin.post(f"/admin/books/{book_id}/edit", {"title": "Umstellbuch", "language": "en"})
    assert edited.status_code == 303
    assert "Your e-book" in env.new_client().get(path).text
    # Ohne Angabe bleibt die Sprache erhalten.
    assert admin.post(f"/admin/books/{book_id}/edit", {"title": "Neu"}).status_code == 303
    assert "Your e-book" in env.new_client().get(path).text


def test_invalid_language_is_rejected(env: Env, admin) -> None:
    response = admin.post("/admin/books", {"title": "X", "language": "fr"})
    assert response.status_code == 422
    assert "Sprache" in response.text


def test_unknown_link_follows_browser_language(env: Env) -> None:
    code = "A" * 43
    english = env.new_client().get(f"/d/{code}", headers={"Accept-Language": "en-US,en;q=0.9"})
    assert english.status_code == 404
    assert "Link not found" in english.text
    german = env.new_client().get(f"/d/{code}", headers={"Accept-Language": "de-DE,de;q=0.9"})
    assert "Link nicht gefunden" in german.text
    default = env.new_client().get(f"/d/{code}")
    assert "Link nicht gefunden" in default.text
    home = env.new_client().get("/", headers={"Accept-Language": "en"})
    assert "E-book download" in home.text


def test_accept_language_parsing() -> None:
    assert from_accept_language(None) == "de"
    assert from_accept_language("") == "de"
    assert from_accept_language("fr-FR,fr;q=0.9") == "de"
    assert from_accept_language("en-GB") == "en"
    assert from_accept_language("fr,en;q=0.8,de;q=0.9") == "de"
    assert from_accept_language("de;q=0.5,en;q=0.7") == "en"
    assert from_accept_language("en;q=0,de") == "de"
    assert from_accept_language("en;q=abc,de;q=0.1") == "de"


def test_english_message_template(env: Env, admin) -> None:
    book_id = _english_book(admin, "Message Book")
    text = _created_link_page(env, admin, book_id, max_downloads="3")
    assert "Versandnachricht <span" in text and "(Englisch)" in text
    assert "thank you for your purchase!" in text
    assert "&#34;Message Book&#34;" in text or '"Message Book"' in text
    assert "The link does not expire." in text
    assert "It can be used for up to 3 downloads." in text
    assert "Downloadlink" in text  # Verwaltung bleibt deutsch
    assert "vielen Dank" not in text

    saved = admin.post(
        "/admin/settings",
        {
            "max_pdf_mb": "300",
            "max_epub_mb": "100",
            "max_cover_mb": "10",
            "message_template": "Hallo {link}",
            "message_template_en": "Hey!\nYour book {titel}: {link}\n{gueltigkeit}",
        },
    )
    assert saved.status_code == 303
    text = _created_link_page(env, admin, book_id, expires_at="2030-01-15T10:30")
    assert "Hey!" in text
    assert "Your book Message Book: https://ebooks.example.com/d/" in text
    assert "The link is valid until January 15, 2030, 10:30." in text

    german = _created_link_page(env, admin, admin.published_book("Deutsch"))
    assert "Hallo https://ebooks.example.com/d/" in german

    bad = admin.post(
        "/admin/settings",
        {
            "max_pdf_mb": "300",
            "max_epub_mb": "100",
            "max_cover_mb": "10",
            "message_template": "Hallo {link}",
            "message_template_en": "no placeholder",
        },
    )
    assert bad.status_code == 422


def test_api_language(env: Env) -> None:
    api = env.api(["books:read", "books:write"])
    created = api.post("/api/v1/books", json={"title": "API Book", "language": "en"})
    assert created.status_code == 201, created.text
    book = created.json()
    assert book["language"] == "en"
    assert api.post("/api/v1/books", json={"title": "Default"}).json()["language"] == "de"
    patched = api.patch(f"/api/v1/books/{book['id']}", json={"language": "de"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["language"] == "de"
    assert api.post("/api/v1/books", json={"title": "X", "language": "fr"}).status_code == 422


def test_format_datetime() -> None:
    from datetime import datetime

    value = datetime(2026, 3, 7, 9, 5)
    assert format_datetime(value, "de") == "07.03.2026, 09:05 Uhr"
    assert format_datetime(value, "en") == "March 7, 2026, 09:05"
