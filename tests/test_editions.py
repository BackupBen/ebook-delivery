"""Ausgaben, Versionierung, Archivieren und Löschen."""

from __future__ import annotations

import re

from tests.conftest import Env, make_epub, make_pdf


def _current_edition(env: Env, book_id: str) -> str:
    with env.db() as conn:
        return conn.execute(
            "SELECT current_edition_id FROM books WHERE id = ?", (book_id,)
        ).fetchone()[0]


def _new_draft(admin, book_id: str, copy: list[str] | None = None) -> str:
    response = admin.post(
        f"/admin/books/{book_id}/editions", {"note": "Korrektur", "copy_formats": copy or []}
    )
    assert response.status_code == 303, response.text
    return admin.draft_id(book_id)


def test_cannot_publish_without_files(env: Env, admin) -> None:
    book_id = admin.create_book()
    response = admin.publish(book_id, admin.draft_id(book_id))
    assert response.status_code == 422
    assert "noch keine Datei" in response.text
    assert _current_edition(env, book_id) is None


def test_no_link_before_first_publication(env: Env, admin) -> None:
    book_id = admin.create_book()
    new = env.client.get(f"/admin/links/new?book_id={book_id}")
    assert "kein aktives Buch mit veröffentlichter Ausgabe" in new.text
    response = admin.post(
        "/admin/links",
        {"book_id": book_id, "formats": ["pdf"], "form_token": "token-1234567890123456"},
    )
    assert response.status_code == 422


def test_new_edition_requires_explicit_choice_for_existing_links(env: Env, admin) -> None:
    book_id = admin.published_book("Versionen", make_pdf("Fassung 1"))
    first = _current_edition(env, book_id)
    _, path = admin.create_link(book_id)

    draft = _new_draft(admin, book_id)
    assert admin.upload(book_id, draft, make_pdf("Fassung 2")).status_code == 303

    # Ohne ausdrückliche Wahl wird nicht veröffentlicht und nichts umgestellt.
    refused = admin.publish(book_id, draft)
    assert refused.status_code == 422
    assert "Wähle ausdrücklich" in refused.text
    assert _current_edition(env, book_id) == first
    assert b"Fassung 1" in env.new_client().get(f"{path}/pdf").content

    # Die Seite bietet beide Möglichkeiten ohne Vorauswahl an.
    page = env.client.get(f"/admin/books/{book_id}").text
    radios = re.findall(r'<input type="radio" name="existing_links"[^>]*>', page)
    assert len(radios) == 2
    assert not any("checked" in radio for radio in radios)

    invalid = admin.publish(book_id, draft, "vielleicht")
    assert invalid.status_code == 422
    assert _current_edition(env, book_id) == first


def test_keep_binds_existing_links_to_their_edition(env: Env, admin) -> None:
    book_id = admin.published_book("Versionen", make_pdf("Fassung 1"), make_epub("Fassung 1"))
    old_link, old_path = admin.create_link(book_id)

    draft = _new_draft(admin, book_id)
    admin.upload(book_id, draft, make_pdf("Fassung 2"), make_epub("Fassung 2"))
    assert admin.publish(book_id, draft, "keep").status_code == 303
    assert _current_edition(env, book_id) == draft

    _, new_path = admin.create_link(book_id)
    buyer = env.new_client()
    assert b"Fassung 1" in buyer.get(f"{old_path}/pdf").content
    assert b"Fassung 2" in buyer.get(f"{new_path}/pdf").content

    # Später lässt sich ein einzelner Link ausdrücklich umstellen.
    edit = admin.post(
        f"/admin/links/{old_link}/edit",
        {
            "label": "",
            "formats": ["pdf", "epub"],
            "edition_id": draft,
            "expires_at": "",
            "max_downloads": "",
        },
    )
    assert edit.status_code == 303
    assert b"Fassung 2" in buyer.get(f"{old_path}/pdf").content


def test_migrate_moves_existing_links(env: Env, admin) -> None:
    book_id = admin.published_book("Versionen", make_pdf("Fassung 1"))
    link_ids = [admin.create_link(book_id) for _ in range(3)]
    revoked_id, revoked_path = link_ids[2]
    admin.post(f"/admin/links/{revoked_id}/revoke", {"confirm": "1"})

    draft = _new_draft(admin, book_id)
    admin.upload(book_id, draft, make_pdf("Fassung 2"))
    response = admin.publish(book_id, draft, "migrate")
    assert response.status_code == 303
    page = env.client.get(response.headers["location"]).text
    assert "2 Links wurden auf die neue Ausgabe umgestellt" in page

    buyer = env.new_client()
    for _, path in link_ids[:2]:
        assert b"Fassung 2" in buyer.get(f"{path}/pdf").content
    assert buyer.get(f"{revoked_path}/pdf").status_code == 410
    with env.db() as conn:
        bound = conn.execute("SELECT edition_id FROM links WHERE id = ?", (revoked_id,)).fetchone()[
            0
        ]
    assert bound != draft  # widerrufene Links bleiben unverändert


def test_bulk_migration_after_keep(env: Env, admin) -> None:
    book_id = admin.published_book("Versionen", make_pdf("Fassung 1"))
    _, path = admin.create_link(book_id)
    draft = _new_draft(admin, book_id)
    admin.upload(book_id, draft, make_pdf("Fassung 2"))
    admin.publish(book_id, draft, "keep")

    unconfirmed = admin.post(f"/admin/books/{book_id}/editions/{draft}/migrate-links")
    assert unconfirmed.status_code == 422
    assert b"Fassung 1" in env.new_client().get(f"{path}/pdf").content

    confirmed = admin.post(
        f"/admin/books/{book_id}/editions/{draft}/migrate-links", {"confirm": "1"}
    )
    assert confirmed.status_code == 303
    assert b"Fassung 2" in env.new_client().get(f"{path}/pdf").content


def test_migration_drops_missing_formats(env: Env, admin) -> None:
    book_id = admin.published_book("Formate", make_pdf("alt"), make_epub("alt"))
    _, path = admin.create_link(book_id)
    draft = _new_draft(admin, book_id)
    admin.upload(book_id, draft, make_pdf("neu"))  # neue Ausgabe nur als PDF
    admin.publish(book_id, draft, "migrate")
    buyer = env.new_client()
    page = buyer.get(path).text
    assert f"{path}/pdf" in page
    assert f"{path}/epub" not in page
    assert buyer.get(f"{path}/epub").status_code == 404


def test_copied_files_are_shared_and_survive_deletion_of_old_edition(env: Env, admin) -> None:
    epub = make_epub("unverändert")
    book_id = admin.published_book("Kopie", make_pdf("alt"), epub)
    first = _current_edition(env, book_id)
    draft = _new_draft(admin, book_id, copy=["epub"])
    admin.upload(book_id, draft, make_pdf("neu"))
    admin.publish(book_id, draft)  # keine Links vorhanden, keine Wahl nötig

    with env.db() as conn:
        keys = [
            row[0]
            for row in conn.execute("SELECT storage_key FROM edition_files WHERE format = 'epub'")
        ]
    assert len(keys) == 2
    assert keys[0] == keys[1]  # dieselbe Datei, nicht doppelt gespeichert

    deleted = admin.post(f"/admin/books/{book_id}/editions/{first}/delete", {"confirm": "1"})
    assert deleted.status_code == 303
    assert (env.settings.books_dir / keys[0]).exists()
    assert len(list(env.settings.books_dir.rglob("*.pdf"))) == 1
    _, path = admin.create_link(book_id)
    assert env.new_client().get(f"{path}/epub").content == epub


def test_edition_deletion_rules(env: Env, admin) -> None:
    book_id = admin.published_book("Regeln", make_pdf("eins"))
    first = _current_edition(env, book_id)
    link_id, _ = admin.create_link(book_id)

    current = admin.post(f"/admin/books/{book_id}/editions/{first}/delete", {"confirm": "1"})
    assert current.status_code == 409
    assert "aktuelle Ausgabe" in current.text

    draft = _new_draft(admin, book_id)
    second_draft = admin.post(f"/admin/books/{book_id}/editions", {"note": ""})
    assert second_draft.status_code == 409  # nur ein Entwurf gleichzeitig

    admin.upload(book_id, draft, make_pdf("zwei"))
    admin.publish(book_id, draft, "keep")
    bound = admin.post(f"/admin/books/{book_id}/editions/{first}/delete", {"confirm": "1"})
    assert bound.status_code == 409
    assert "noch 1 Käuferlinks gebunden" in bound.text

    admin.post(f"/admin/links/{link_id}/revoke", {"confirm": "1"})
    admin.post(f"/admin/links/{link_id}/delete", {"confirm": "1"})
    assert (
        admin.post(f"/admin/books/{book_id}/editions/{first}/delete", {"confirm": "1"}).status_code
        == 303
    )
    assert len(list(env.settings.books_dir.rglob("*.pdf"))) == 1


def test_discarding_a_draft_removes_its_files(env: Env, admin) -> None:
    book_id = admin.published_book()
    draft = _new_draft(admin, book_id)
    admin.upload(book_id, draft, make_pdf("verworfen"))
    assert len(list(env.settings.books_dir.rglob("*.pdf"))) == 2
    assert (
        admin.post(f"/admin/books/{book_id}/editions/{draft}/delete", {"confirm": "1"}).status_code
        == 303
    )
    assert len(list(env.settings.books_dir.rglob("*.pdf"))) == 1


def test_archive_blocks_new_links_but_keeps_existing(env: Env, admin) -> None:
    book_id = admin.published_book("Archivbuch")
    _, path = admin.create_link(book_id)
    assert admin.post(f"/admin/books/{book_id}/archive", {"archived": "1"}).status_code == 303

    assert "Archivbuch" not in env.client.get("/admin/books").text
    assert "Archivbuch" in env.client.get("/admin/books?status=archived").text
    assert env.new_client().get(f"{path}/pdf").status_code == 200

    response = admin.post(
        "/admin/links",
        {"book_id": book_id, "formats": ["pdf"], "form_token": "tok-archiv-1234567890"},
    )
    assert response.status_code == 422
    assert "archivierte Bücher" in response.text

    assert admin.post(f"/admin/books/{book_id}/archive", {"archived": "0"}).status_code == 303
    assert "Archivbuch" in env.client.get("/admin/books").text


def test_delete_book_shows_affected_links_and_needs_confirmation(env: Env, admin) -> None:
    book_id = admin.published_book("Löschbuch")
    paths = [admin.create_link(book_id, label=f"Bestellung {i}")[1] for i in range(3)]
    files_before = [p for p in env.settings.books_dir.rglob("*") if p.is_file()]
    assert len(files_before) == 2

    confirm_page = env.client.get(f"/admin/books/{book_id}/delete")
    assert confirm_page.status_code == 200
    assert "3 Downloadlinks" in confirm_page.text
    for index in range(3):
        assert f"Bestellung {index}" in confirm_page.text

    unconfirmed = admin.post(f"/admin/books/{book_id}/delete", {"expected_link_count": "3"})
    assert unconfirmed.status_code == 422

    # Zwischenzeitlich kam ein Link dazu: die Bestätigung passt nicht mehr.
    stale = admin.post(
        f"/admin/books/{book_id}/delete", {"confirm": "1", "expected_link_count": "2"}
    )
    assert stale.status_code == 409
    assert "3 Käuferlinks ungültig" in stale.text
    assert env.new_client().get(paths[0]).status_code == 200

    done = admin.post(
        f"/admin/books/{book_id}/delete", {"confirm": "1", "expected_link_count": "3"}
    )
    assert done.status_code == 303
    assert "3 Links sind damit ungültig" in env.client.get("/admin/books").text
    buyer = env.new_client()
    for path in paths:
        assert buyer.get(path).status_code == 404
        assert buyer.get(f"{path}/pdf").status_code == 404
    assert not [p for p in env.settings.books_dir.rglob("*") if p.is_file()]
    with env.db() as conn:
        for table in ("books", "editions", "edition_files", "links", "download_events"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608


def test_search_and_filter(env: Env, admin) -> None:
    alpha = admin.published_book("Alpha Kochbuch")
    admin.published_book("Beta Reiseführer")
    admin.post(
        f"/admin/books/{alpha}/edit",
        {"title": "Alpha Kochbuch", "description": "", "whop_product_id": "prod_Alpha123"},
    )
    listing = env.client.get("/admin/books?q=koch").text
    assert "Alpha Kochbuch" in listing
    assert "Beta Reiseführer" not in listing
    assert "Alpha Kochbuch" in env.client.get("/admin/books?q=prod_Alpha").text
    assert "Keine Bücher gefunden" in env.client.get("/admin/books?q=xyz%25").text

    active_id, _ = admin.create_link(alpha, label="Kunde Nord")
    revoked_id, _ = admin.create_link(alpha, label="Kunde Süd")
    admin.post(f"/admin/links/{revoked_id}/revoke", {"confirm": "1"})
    by_label = env.client.get("/admin/links?q=Nord").text
    assert "Kunde Nord" in by_label
    assert "Kunde Süd" not in by_label
    by_state = env.client.get("/admin/links?state=revoked").text
    assert "Kunde Süd" in by_state
    assert "Kunde Nord" not in by_state
    by_book = env.client.get(f"/admin/links?book_id={alpha}&state=active").text
    assert "Kunde Nord" in by_book
    assert active_id in env.client.get(f"/admin/links/{active_id}").text


def test_link_lookup_finds_entry_from_pasted_url(env: Env, admin) -> None:
    book_id = admin.published_book()
    link_id, path = admin.create_link(book_id, label="Suchlink")
    found = admin.post("/admin/links/lookup", {"code": f"https://ebooks.example.com{path}"})
    assert found.status_code == 303
    assert found.headers["location"] == f"/admin/links/{link_id}"
    missing = admin.post(
        "/admin/links/lookup", {"code": "https://ebooks.example.com/d/" + "q" * 43}
    )
    assert missing.status_code == 404
    assert "keinen Link" in missing.text


def test_whop_product_id_validation(env: Env, admin) -> None:
    bad = admin.post("/admin/books", {"title": "Whop", "whop_product_id": "12345"})
    assert bad.status_code == 422
    assert "prod_" in bad.text
    good = admin.post("/admin/books", {"title": "Whop", "whop_product_id": "prod_XyZ123"})
    assert good.status_code == 303
    assert "prod_XyZ123" in env.client.get(good.headers["location"]).text
