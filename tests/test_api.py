"""REST-API: Authentifizierung, Berechtigungen, Uploads, Idempotenz, Versionierung."""

from __future__ import annotations

import json
import re

import pytest

from ebookapp import schemas
from tests.conftest import ALL_SCOPES, BASE_URL, Env, make_epub, make_pdf, make_png


def _published(api, title: str = "API-Buch", pdf: bytes | None = None) -> dict:
    book = api.post("/api/v1/books", json={"title": title}).json()
    edition_id = book["draft_edition"]["id"]
    upload = api.post(
        f"/api/v1/books/{book['id']}/editions/{edition_id}/files",
        files={
            "pdf": ("buch.pdf", pdf or make_pdf(title), "application/pdf"),
            "epub": ("buch.epub", make_epub(title), "application/epub+zip"),
        },
    )
    assert upload.status_code == 200, upload.text
    publish = api.post(f"/api/v1/books/{book['id']}/editions/{edition_id}/publish")
    assert publish.status_code == 200, publish.text
    return api.get(f"/api/v1/books/{book['id']}").json()


# --------------------------------------------------------------------------- Authentifizierung


def test_missing_and_invalid_keys(env: Env) -> None:
    anon = env.new_client()
    response = anon.get("/api/v1/books")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"

    for header in (
        "Bearer falsch",
        "Bearer ebk_000000000000_" + "a" * 43,
        "Basic YWRtaW46YWRtaW4=",
        "ebk_000000000000_" + "a" * 43,
    ):
        assert anon.get("/api/v1/books", headers={"Authorization": header}).status_code == 401


def test_key_in_url_is_rejected(env: Env) -> None:
    key = env.api_key()
    anon = env.new_client()
    for name in ("api_key", "access_token", "token", "key"):
        response = anon.get(f"/api/v1/books?{name}={key}")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "credentials_in_url"
    # Auch mit gültigem Header wird ein Schlüssel in der URL nicht akzeptiert.
    both = anon.get(f"/api/v1/books?api_key={key}", headers={"Authorization": f"Bearer {key}"})
    assert both.status_code == 400


def test_keys_are_stored_hashed_and_shown_once(env: Env, admin) -> None:
    created = admin.post(
        "/admin/settings/api-keys", {"name": "Automatisierung", "scopes": ["books:read"]}
    )
    assert created.status_code == 201
    key = re.search(r'id="api-key"[^>]*value="([^"]+)"', created.text).group(1)
    assert re.fullmatch(r"ebk_[0-9a-f]{12}_[A-Za-z0-9_-]{43}", key)

    with env.db() as conn:
        dump = "\n".join(conn.iterdump())
    assert key not in dump
    assert key.rsplit("_", 1)[1] not in dump
    settings_page = env.client.get("/admin/settings").text
    assert key not in settings_page
    assert key[:16] in settings_page  # nur die öffentliche Kennung

    api = env.new_client()
    api.headers["Authorization"] = f"Bearer {key}"
    assert api.get("/api/v1/books").status_code == 200


def test_revoked_key_stops_working(env: Env, admin) -> None:
    created = admin.post("/admin/settings/api-keys", {"name": "kurz", "scopes": ALL_SCOPES})
    key = re.search(r'id="api-key"[^>]*value="([^"]+)"', created.text).group(1)
    api = env.new_client()
    api.headers["Authorization"] = f"Bearer {key}"
    assert api.get("/api/v1/books").status_code == 200

    with env.db() as conn:
        key_id = conn.execute("SELECT id FROM api_keys").fetchone()[0]
    assert admin.post(f"/admin/settings/api-keys/{key_id}/revoke").status_code == 422
    assert (
        admin.post(f"/admin/settings/api-keys/{key_id}/revoke", {"confirm": "1"}).status_code == 303
    )
    assert api.get("/api/v1/books").status_code == 401


def test_api_key_requires_scopes(env: Env, admin) -> None:
    response = admin.post("/admin/settings/api-keys", {"name": "ohne"})
    assert response.status_code == 422
    unknown = admin.post("/admin/settings/api-keys", {"name": "x", "scopes": ["admin:all"]})
    assert unknown.status_code == 422


def test_failed_authentication_is_rate_limited(env: Env) -> None:
    anon = env.new_client()
    bad = {"Authorization": "Bearer ebk_000000000000_" + "a" * 43}
    for _ in range(20):
        assert anon.get("/api/v1/books", headers=bad).status_code == 401
    blocked = anon.get("/api/v1/books", headers=bad)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


def test_request_rate_limit_per_key(make_env) -> None:
    env = make_env(API_RATE_PER_MINUTE="5")
    api = env.api()
    statuses = [api.get("/api/v1/books").status_code for _ in range(7)]
    assert statuses == [200] * 5 + [429] * 2
    other = env.api()  # anderer Schlüssel, eigenes Kontingent
    assert other.get("/api/v1/books").status_code == 200


# --------------------------------------------------------------------------- Berechtigungen


def test_scopes_are_enforced(env: Env) -> None:
    full = env.api()
    book = _published(full)
    book_id, edition_id = book["id"], book["current_edition"]["id"]
    link = full.post("/api/v1/links", json={"book_id": book_id}).json()

    pdf = {"pdf": ("b.pdf", make_pdf(), "application/pdf")}
    cover = {"file": ("c.png", make_png(), "image/png")}
    requests = {
        "books:read": [
            ("GET", "/api/v1/books", {}),
            ("GET", f"/api/v1/books/{book_id}", {}),
            ("GET", f"/api/v1/books/{book_id}/editions", {}),
            ("GET", f"/api/v1/books/{book_id}/editions/{edition_id}", {}),
            ("GET", "/api/v1/stats/downloads", {}),
        ],
        "books:write": [
            ("POST", "/api/v1/books", {"json": {"title": "x"}}),
            ("PATCH", f"/api/v1/books/{book_id}", {"json": {"title": "y"}}),
            ("POST", f"/api/v1/books/{book_id}/archive", {}),
            ("POST", f"/api/v1/books/{book_id}/unarchive", {}),
            ("GET", f"/api/v1/books/{book_id}/deletion-preview", {}),
            ("POST", f"/api/v1/books/{book_id}/editions", {"json": {}}),
            ("POST", f"/api/v1/books/{book_id}/editions/{edition_id}/publish", {}),
            ("DELETE", f"/api/v1/books/{book_id}/editions/{edition_id}", {}),
            ("DELETE", f"/api/v1/books/{book_id}?expected_link_count=1", {}),
        ],
        "files:write": [
            ("POST", f"/api/v1/books/{book_id}/editions/{edition_id}/files", {"files": pdf}),
            ("DELETE", f"/api/v1/books/{book_id}/editions/{edition_id}/files/pdf", {}),
            ("PUT", f"/api/v1/books/{book_id}/cover", {"files": cover}),
            ("DELETE", f"/api/v1/books/{book_id}/cover", {}),
        ],
        "links:manage": [
            ("GET", "/api/v1/links", {}),
            ("POST", "/api/v1/links", {"json": {"book_id": book_id}}),
            ("POST", "/api/v1/links/lookup", {"json": {"code": link["code"]}}),
            ("GET", f"/api/v1/links/{link['id']}", {}),
            ("PATCH", f"/api/v1/links/{link['id']}", {"json": {"label": "z"}}),
            ("GET", f"/api/v1/links/{link['id']}/stats", {}),
            ("POST", f"/api/v1/links/{link['id']}/revoke", {}),
            ("DELETE", f"/api/v1/links/{link['id']}", {}),
            (
                "POST",
                f"/api/v1/books/{book_id}/editions/{edition_id}/migrate-links",
                {"json": {"confirm": True}},
            ),
        ],
    }
    for needed, calls in requests.items():
        others = [scope for scope in ALL_SCOPES if scope != needed]
        limited = env.api(others)
        for method, url, kwargs in calls:
            response = limited.request(method, url, **kwargs)
            assert response.status_code == 403, (needed, method, url, response.text)
            error = response.json()["error"]
            assert error["code"] == "insufficient_scope"
            assert error["details"]["required_scope"] == needed

    # Nichts davon hat etwas verändert.
    after = full.get(f"/api/v1/books/{book_id}").json()
    assert after["title"] == "API-Buch"
    assert after["status"] == "active"
    assert full.get(f"/api/v1/links/{link['id']}").json()["state"] == "active"

    # Mit genau der passenden Berechtigung funktioniert der Zugriff.
    assert env.api(["books:read"]).get("/api/v1/books").status_code == 200
    assert env.api(["links:manage"]).get("/api/v1/links").status_code == 200


# --------------------------------------------------------------------------- Gesamtablauf


def test_full_flow(env: Env) -> None:
    api = env.api()
    created = api.post(
        "/api/v1/books",
        json={"title": "Ablaufbuch", "description": "Text", "whop_product_id": "prod_Flow123"},
    )
    assert created.status_code == 201
    book = created.json()
    schemas.BookOut.model_validate(book)
    assert book["whop_product_id"] == "prod_Flow123"
    assert book["current_edition"] is None
    edition_id = book["draft_edition"]["id"]

    pdf, epub = make_pdf("Ablauf", 30_000), make_epub("Ablauf")
    upload = api.post(
        f"/api/v1/books/{book['id']}/editions/{edition_id}/files",
        files={
            "pdf": ("a.pdf", pdf, "application/pdf"),
            "epub": ("a.epub", epub, "application/epub+zip"),
        },
    )
    assert upload.status_code == 200
    edition = upload.json()
    schemas.EditionOut.model_validate(edition)
    assert {item["format"] for item in edition["files"]} == {"pdf", "epub"}

    cover = api.put(
        f"/api/v1/books/{book['id']}/cover", files={"file": ("c.png", make_png(), "image/png")}
    )
    assert cover.status_code == 200
    assert cover.json()["has_cover"] is True

    published = api.post(f"/api/v1/books/{book['id']}/editions/{edition_id}/publish")
    assert published.status_code == 200
    schemas.PublishResult.model_validate(published.json())
    assert published.json()["edition"]["is_current"] is True

    link_response = api.post(
        "/api/v1/links", json={"book_id": book["id"], "label": "Bestellung 77"}
    )
    assert link_response.status_code == 201
    link = link_response.json()
    schemas.LinkCreated.model_validate(link)
    assert link["expires_at"] is None
    assert link["max_downloads"] is None
    assert link["state"] == "active"
    assert link["url"] == f"{BASE_URL}/d/{link['code']}"

    buyer = env.new_client()
    path = link["url"][len(BASE_URL) :]
    assert "Ablaufbuch" in buyer.get(path).text
    assert buyer.get(f"{path}/pdf").content == pdf
    assert buyer.get(f"{path}/epub").content == epub
    assert buyer.get(f"{path}/cover").status_code == 200

    checked = api.get(f"/api/v1/links/{link['id']}").json()
    assert checked["download_count"] == 2
    assert checked["last_download_at"] is not None
    looked_up = api.post("/api/v1/links/lookup", json={"code": link["url"]}).json()
    assert looked_up["id"] == link["id"]

    revoked = api.post(f"/api/v1/links/{link['id']}/revoke")
    assert revoked.status_code == 200
    assert revoked.json()["state"] == "revoked"
    assert api.post(f"/api/v1/links/{link['id']}/revoke").status_code == 200  # wiederholbar
    assert buyer.get(path).status_code == 410
    assert buyer.get(f"{path}/pdf").status_code == 410


def test_codes_never_appear_after_creation(env: Env) -> None:
    api = env.api()
    book = _published(api)
    link = api.post("/api/v1/links", json={"book_id": book["id"], "label": "geheim"}).json()
    code = link["code"]
    env.new_client().get(f"/d/{code}/pdf")

    bodies = [
        api.get("/api/v1/links").text,
        api.get(f"/api/v1/links/{link['id']}").text,
        api.get(f"/api/v1/links?book_id={book['id']}").text,
        api.patch(f"/api/v1/links/{link['id']}", json={"label": "neu"}).text,
        api.get("/api/v1/stats/downloads").text,
        api.get(f"/api/v1/links/{link['id']}/stats").text,
        api.get(f"/api/v1/books/{book['id']}").text,
        api.get(f"/api/v1/books/{book['id']}/deletion-preview").text,
        api.post(f"/api/v1/links/{link['id']}/revoke").text,
    ]
    for body in bodies:
        assert code not in body
        data = json.loads(body)
        assert "code" not in data
        assert "url" not in data
        nested = data.get("links", [])
        for item in data.get("items", []) + (nested if isinstance(nested, list) else []):
            assert "code" not in item
            assert "code_hash" not in item

    stats = api.get("/api/v1/stats/downloads").json()
    schemas.DownloadStats.model_validate(stats)
    assert stats["total"] == 1
    assert stats["by_format"] == [{"format": "pdf", "downloads": 1}]
    assert stats["by_book"][0]["book_id"] == book["id"]
    assert set(stats) == {"date_from", "date_to", "total", "by_format", "by_day", "by_book"}


def test_link_options_and_update(env: Env) -> None:
    api = env.api()
    book = _published(api)
    link = api.post(
        "/api/v1/links",
        json={
            "book_id": book["id"],
            "formats": ["pdf"],
            "max_downloads": 1,
            "expires_at": "2099-01-01T00:00:00+01:00",
        },
    ).json()
    assert link["formats"] == ["pdf"]
    assert link["expires_at"] == "2098-12-31T23:00:00Z"
    path = f"/d/{link['code']}"
    buyer = env.new_client()
    assert buyer.get(f"{path}/epub").status_code == 404
    assert buyer.get(f"{path}/pdf").status_code == 200
    assert api.get(f"/api/v1/links/{link['id']}").json()["state"] == "exhausted"
    assert api.get("/api/v1/links?state=exhausted").json()["total"] == 1
    assert api.get("/api/v1/links?state=active").json()["total"] == 0

    updated = api.patch(
        f"/api/v1/links/{link['id']}",
        json={"max_downloads": None, "expires_at": None, "formats": ["pdf", "epub"]},
    ).json()
    assert updated["state"] == "active"
    assert updated["max_downloads"] is None
    assert updated["expires_at"] is None
    assert buyer.get(f"{path}/epub").status_code == 200

    disabled = api.patch(f"/api/v1/links/{link['id']}", json={"status": "disabled"}).json()
    assert disabled["state"] == "disabled"
    assert buyer.get(path).status_code == 403
    assert (
        api.patch(f"/api/v1/links/{link['id']}", json={"status": "active"}).json()["state"]
        == "active"
    )

    api.post(f"/api/v1/links/{link['id']}/revoke")
    assert api.patch(f"/api/v1/links/{link['id']}", json={"status": "active"}).status_code == 409
    assert api.delete(f"/api/v1/links/{link['id']}").status_code == 204
    assert api.get(f"/api/v1/links/{link['id']}").status_code == 404


# --------------------------------------------------------------------------- Validierung


def test_validation_errors_are_structured_and_german(env: Env) -> None:
    api = env.api()
    response = api.post(
        "/api/v1/books", json={"title": "", "whop_product_id": "nope", "unbekannt": 1}
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    fields = {item["field"]: item["message"] for item in error["fields"]}
    assert set(fields) == {"title", "whop_product_id", "unbekannt"}
    assert "prod_" in fields["whop_product_id"]

    book = _published(api)
    for payload in (
        {"book_id": book["id"], "max_downloads": 0},
        {"book_id": book["id"], "formats": []},
        {"book_id": book["id"], "formats": ["mobi"]},
        {"book_id": book["id"], "expires_at": "2099-01-01T00:00:00"},  # ohne Zeitzone
        {"book_id": book["id"], "expires_at": "2001-01-01T00:00:00Z"},  # Vergangenheit
        {"book_id": "bk_gibt_es_nicht"},
    ):
        response = api.post("/api/v1/links", json=payload)
        assert response.status_code == 422, payload
        assert response.json()["error"]["fields"]
    assert api.get("/api/v1/links").json()["total"] == 0

    assert api.get("/api/v1/books/bk_nope").status_code == 404
    assert api.get("/api/v1/gibt-es-nicht").json()["error"]["code"] == "not_found"
    not_json = api.post(
        "/api/v1/books", content=b"{kaputt", headers={"Content-Type": "application/json"}
    )
    assert not_json.status_code == 422


@pytest.mark.parametrize(
    ("field", "filename", "content_type", "data"),
    [
        ("pdf", "x.pdf", "application/pdf", b"<html>kein pdf</html>" * 20),
        ("pdf", "x.exe", "application/octet-stream", make_pdf()),
        ("pdf", "x.pdf", "text/plain", make_pdf()),
        ("epub", "x.epub", "application/epub+zip", make_pdf()),
        (
            "epub",
            "x.epub",
            "application/epub+zip",
            make_epub(extra={"b.bin": b"\x00" * (30 * 1024 * 1024)}),
        ),
    ],
)
def test_upload_validation_matches_gui(
    env: Env, field: str, filename: str, content_type: str, data: bytes
) -> None:
    api = env.api()
    book = api.post("/api/v1/books", json={"title": "Upload"}).json()
    response = api.post(
        f"/api/v1/books/{book['id']}/editions/{book['draft_edition']['id']}/files",
        files={field: (filename, data, content_type)},
    )
    assert response.status_code == 422
    assert response.json()["error"]["fields"][0]["field"] == field
    assert api.get(f"/api/v1/books/{book['id']}").json()["draft_edition"]["files"] == []
    assert not [p for p in env.settings.books_dir.rglob("*") if p.is_file()]


def test_upload_size_limit_is_configurable(env: Env, admin) -> None:
    admin.post(
        "/admin/settings",
        {"max_pdf_mb": "1", "max_epub_mb": "1", "max_cover_mb": "1", "message_template": "{link}"},
    )
    api = env.api()
    book = api.post("/api/v1/books", json={"title": "Groß"}).json()
    url = f"/api/v1/books/{book['id']}/editions/{book['draft_edition']['id']}/files"
    too_big = api.post(
        url, files={"pdf": ("g.pdf", make_pdf(size=1024 * 1024 + 10), "application/pdf")}
    )
    assert too_big.status_code == 413
    assert too_big.json()["error"]["code"] == "payload_too_large"
    assert api.post(url, files={}).status_code == 422


def test_upload_requires_draft(env: Env) -> None:
    api = env.api()
    book = _published(api)
    response = api.post(
        f"/api/v1/books/{book['id']}/editions/{book['current_edition']['id']}/files",
        files={"pdf": ("x.pdf", make_pdf("heimlich"), "application/pdf")},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "edition_published"


# --------------------------------------------------------------------------- Idempotenz


def test_idempotent_book_creation(env: Env) -> None:
    api = env.api()
    headers = {"Idempotency-Key": "buch-4711-0000000001"}
    first = api.post("/api/v1/books", json={"title": "Einmalig"}, headers=headers)
    second = api.post("/api/v1/books", json={"title": "Einmalig"}, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert "idempotent-replayed" not in first.headers
    assert second.headers["idempotent-replayed"] == "true"
    assert api.get("/api/v1/books").json()["total"] == 1

    conflict = api.post("/api/v1/books", json={"title": "Anderer Inhalt"}, headers=headers)
    assert conflict.status_code == 422
    assert conflict.json()["error"]["code"] == "idempotency_key_reused"

    # Ohne Schlüssel entstehen wie erwartet zwei Bücher.
    api.post("/api/v1/books", json={"title": "Doppelt"})
    api.post("/api/v1/books", json={"title": "Doppelt"})
    assert api.get("/api/v1/books").json()["total"] == 3

    # Derselbe Schlüssel eines anderen API-Schlüssels ist unabhängig.
    other = env.api()
    assert (
        "idempotent-replayed"
        not in other.post("/api/v1/books", json={"title": "Einmalig"}, headers=headers).headers
    )


def test_idempotent_upload(env: Env) -> None:
    api = env.api()
    book = api.post("/api/v1/books", json={"title": "Upload"}).json()
    url = f"/api/v1/books/{book['id']}/editions/{book['draft_edition']['id']}/files"
    pdf = make_pdf("idempotent")
    headers = {"Idempotency-Key": "upload-0000000000001"}
    first = api.post(url, files={"pdf": ("a.pdf", pdf, "application/pdf")}, headers=headers)
    second = api.post(url, files={"pdf": ("a.pdf", pdf, "application/pdf")}, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert second.headers["idempotent-replayed"] == "true"
    assert len(list(env.settings.books_dir.rglob("*.pdf"))) == 1

    different = api.post(
        url, files={"pdf": ("a.pdf", make_pdf("anders"), "application/pdf")}, headers=headers
    )
    assert different.status_code == 422


def test_idempotent_link_creation_returns_same_code(env: Env) -> None:
    api = env.api()
    book = _published(api)
    headers = {"Idempotency-Key": "9d2f1c1e-3d5b-4b7e-9f66-0b1d5a1c7e11"}
    first = api.post("/api/v1/links", json={"book_id": book["id"]}, headers=headers)
    second = api.post("/api/v1/links", json={"book_id": book["id"]}, headers=headers)
    assert first.status_code == second.status_code == 201
    assert second.headers["idempotent-replayed"] == "true"
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["code"] == second.json()["code"]
    assert first.json()["url"] == second.json()["url"]
    assert api.get("/api/v1/links").json()["total"] == 1
    assert env.new_client().get(f"/d/{second.json()['code']}").status_code == 200

    # Der Code liegt auch für die Wiederholung nicht im Klartext in der Datenbank.
    code = first.json()["code"]
    with env.db() as conn:
        dump = "\n".join(conn.iterdump())
    assert code not in dump
    assert headers["Idempotency-Key"] not in dump

    assert (
        api.post(
            "/api/v1/links", json={"book_id": book["id"], "label": "x"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        api.post(
            "/api/v1/links", json={"book_id": book["id"]}, headers={"Idempotency-Key": "x" * 201}
        ).status_code
        == 422
    )
    short = api.post(
        "/api/v1/links", json={"book_id": book["id"]}, headers={"Idempotency-Key": "bestellung-1"}
    )
    assert short.status_code == 422  # zu kurz, also leicht zu erraten
    assert short.json()["error"]["code"] == "idempotency_key_invalid"


def test_failed_request_does_not_burn_the_idempotency_key(env: Env) -> None:
    api = env.api()
    book = api.post("/api/v1/books", json={"title": "Später"}).json()
    headers = {"Idempotency-Key": "spaeter-000000000001"}
    early = api.post("/api/v1/links", json={"book_id": book["id"]}, headers=headers)
    assert early.status_code == 422  # noch nichts veröffentlicht
    edition_id = book["draft_edition"]["id"]
    api.post(
        f"/api/v1/books/{book['id']}/editions/{edition_id}/files",
        files={"pdf": ("a.pdf", make_pdf(), "application/pdf")},
    )
    api.post(f"/api/v1/books/{book['id']}/editions/{edition_id}/publish")
    retry = api.post("/api/v1/links", json={"book_id": book["id"]}, headers=headers)
    assert retry.status_code == 201
    assert "idempotent-replayed" not in retry.headers


# --------------------------------------------------------------------------- Versionierung


def test_versioning(env: Env) -> None:
    api = env.api()
    book = _published(api, "Versionen", make_pdf("Fassung 1"))
    book_id = book["id"]
    first_edition = book["current_edition"]["id"]
    old = api.post("/api/v1/links", json={"book_id": book_id}).json()

    draft = api.post(
        f"/api/v1/books/{book_id}/editions", json={"note": "v2", "copy_formats": ["epub"]}
    )
    assert draft.status_code == 201
    draft_id = draft.json()["id"]
    assert draft.json()["number"] == 2
    assert [item["format"] for item in draft.json()["files"]] == ["epub"]
    assert api.post(f"/api/v1/books/{book_id}/editions", json={}).status_code == 409

    api.post(
        f"/api/v1/books/{book_id}/editions/{draft_id}/files",
        files={"pdf": ("b.pdf", make_pdf("Fassung 2"), "application/pdf")},
    )

    # Ohne ausdrückliche Wahl: abgelehnt, nichts verändert.
    for body in (None, {}, {"existing_links": None}):
        refused = api.post(f"/api/v1/books/{book_id}/editions/{draft_id}/publish", json=body)
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "existing_links_required"
        assert refused.json()["error"]["details"]["existing_links"] == 1
    assert (
        api.post(
            f"/api/v1/books/{book_id}/editions/{draft_id}/publish", json={"existing_links": "auto"}
        ).status_code
        == 422
    )
    assert api.get(f"/api/v1/books/{book_id}").json()["current_edition"]["id"] == first_edition

    kept = api.post(
        f"/api/v1/books/{book_id}/editions/{draft_id}/publish", json={"existing_links": "keep"}
    )
    assert kept.status_code == 200
    assert kept.json()["links_migrated"] == 0
    assert kept.json()["links_kept"] == 1
    assert (
        api.post(
            f"/api/v1/books/{book_id}/editions/{draft_id}/publish", json={"existing_links": "keep"}
        ).status_code
        == 409
    )  # bereits veröffentlicht

    new = api.post("/api/v1/links", json={"book_id": book_id}).json()
    assert new["edition_number"] == 2
    assert api.get(f"/api/v1/links/{old['id']}").json()["edition_number"] == 1
    buyer = env.new_client()
    assert b"Fassung 1" in buyer.get(f"/d/{old['code']}/pdf").content
    assert b"Fassung 2" in buyer.get(f"/d/{new['code']}/pdf").content

    # Einzelnen Link ausdrücklich umstellen
    moved = api.patch(f"/api/v1/links/{old['id']}", json={"edition_id": draft_id})
    assert moved.json()["edition_number"] == 2
    assert b"Fassung 2" in buyer.get(f"/d/{old['code']}/pdf").content
    # ... und wieder zurück an die alte Ausgabe binden
    api.patch(f"/api/v1/links/{old['id']}", json={"edition_id": first_edition})

    # Sammelumstellung nur mit Bestätigung
    assert (
        api.post(
            f"/api/v1/books/{book_id}/editions/{draft_id}/migrate-links", json={"confirm": False}
        ).status_code
        == 422
    )
    migrated = api.post(
        f"/api/v1/books/{book_id}/editions/{draft_id}/migrate-links", json={"confirm": True}
    )
    assert migrated.json() == {"links_migrated": 1, "links_without_matching_format": 0}

    # Dritte Ausgabe mit "migrate"
    third = api.post(f"/api/v1/books/{book_id}/editions", json={}).json()
    api.post(
        f"/api/v1/books/{book_id}/editions/{third['id']}/files",
        files={"pdf": ("c.pdf", make_pdf("Fassung 3"), "application/pdf")},
    )
    result = api.post(
        f"/api/v1/books/{book_id}/editions/{third['id']}/publish",
        json={"existing_links": "migrate"},
    ).json()
    assert result["links_migrated"] == 2
    assert result["links_kept"] == 0
    assert b"Fassung 3" in buyer.get(f"/d/{old['code']}/pdf").content

    editions = api.get(f"/api/v1/books/{book_id}/editions").json()["items"]
    assert [item["number"] for item in editions] == [3, 2, 1]
    assert [item["is_current"] for item in editions] == [True, False, False]

    # Links an eine fremde oder unveröffentlichte Ausgabe zu binden, ist nicht möglich.
    other = _published(api, "Anderes Buch")
    wrong = api.patch(
        f"/api/v1/links/{old['id']}", json={"edition_id": other["current_edition"]["id"]}
    )
    assert wrong.status_code == 422


def test_book_update_archive_delete(env: Env) -> None:
    api = env.api()
    book = _published(api, "Verwaltung")
    patched = api.patch(
        f"/api/v1/books/{book['id']}", json={"description": "Neu", "whop_product_id": "prod_New1"}
    ).json()
    assert patched["title"] == "Verwaltung"
    assert patched["description"] == "Neu"
    assert patched["whop_product_id"] == "prod_New1"
    assert api.patch(f"/api/v1/books/{book['id']}", json={"title": None}).status_code == 422
    assert (
        api.patch(f"/api/v1/books/{book['id']}", json={"whop_product_id": None}).json()[
            "whop_product_id"
        ]
        is None
    )

    link = api.post("/api/v1/links", json={"book_id": book["id"]}).json()
    archived = api.post(f"/api/v1/books/{book['id']}/archive").json()
    assert archived["status"] == "archived"
    assert api.get("/api/v1/books").json()["total"] == 0
    assert api.get("/api/v1/books?status=archived").json()["total"] == 1
    assert api.get("/api/v1/books?status=all&q=verwalt").json()["total"] == 1
    assert api.post("/api/v1/links", json={"book_id": book["id"]}).status_code == 422
    assert env.new_client().get(f"/d/{link['code']}").status_code == 200
    api.post(f"/api/v1/books/{book['id']}/unarchive")

    preview = api.get(f"/api/v1/books/{book['id']}/deletion-preview").json()
    schemas.DeletionPreview.model_validate(preview)
    assert preview["links_total"] == 1
    assert preview["links"][0]["id"] == link["id"]

    assert api.delete(f"/api/v1/books/{book['id']}").status_code == 422  # ohne Bestätigung
    mismatch = api.delete(f"/api/v1/books/{book['id']}?expected_link_count=0")
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["details"] == {"links_total": 1}
    assert api.get(f"/api/v1/books/{book['id']}").status_code == 200

    deleted = api.delete(f"/api/v1/books/{book['id']}?expected_link_count=1")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "links_invalidated": 1, "files_deleted": 2}
    assert api.get(f"/api/v1/books/{book['id']}").status_code == 404
    assert env.new_client().get(f"/d/{link['code']}").status_code == 404
    assert not [p for p in env.settings.books_dir.rglob("*") if p.is_file()]


# --------------------------------------------------------------------------- Dokumentation


def test_openapi_and_docs(env: Env, admin) -> None:
    anon = env.new_client()
    assert anon.get("/api/v1/openapi.json").status_code == 401
    assert anon.get("/api/v1/docs").status_code == 303

    spec = env.api(["books:read"]).get("/api/v1/openapi.json").json()
    assert spec["openapi"].startswith("3.")
    assert spec["servers"][0]["url"] == "/api/v1"
    assert spec["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    for path in (
        "/books",
        "/books/{book_id}",
        "/books/{book_id}/archive",
        "/books/{book_id}/cover",
        "/books/{book_id}/editions",
        "/books/{book_id}/editions/{edition_id}/files",
        "/books/{book_id}/editions/{edition_id}/publish",
        "/links",
        "/links/{link_id}",
        "/links/{link_id}/revoke",
        "/links/lookup",
        "/stats/downloads",
    ):
        assert path in spec["paths"], path
    upload = spec["paths"]["/books/{book_id}/editions/{edition_id}/files"]["post"]
    assert "multipart/form-data" in upload["requestBody"]["content"]
    assert any(
        p["name"] == "Idempotency-Key" for p in spec["paths"]["/links"]["post"]["parameters"]
    )
    for operations in spec["paths"].values():
        for operation in operations.values():
            assert operation["security"] == [{"HTTPBearer": []}]
    # Kein Server- oder Domainname in der Spezifikation
    assert "ebooks.example.com" not in json.dumps(spec)

    page = env.client.get("/api/v1/docs")
    assert page.status_code == 200
    assert "/static/vendor/swagger-ui/swagger-ui-bundle.js" in page.text
    assert not re.findall(r"""(?:src|href)=["'](?:https?:)?//""", page.text)  # kein CDN
    assert env.client.get("/static/vendor/swagger-ui/swagger-ui-bundle.js").status_code == 200
    assert env.client.get("/api/v1/openapi.json").status_code == 200  # mit Admin-Sitzung


def test_checked_in_openapi_file_is_up_to_date(env: Env) -> None:
    from pathlib import Path

    # Die eingecheckte Datei ist die englische Standardfassung.
    client = env.new_client(language=None)
    client.headers["Authorization"] = f"Bearer {env.api_key(['books:read'])}"
    spec = client.get("/api/v1/openapi.json").json()
    stored = json.loads(
        (Path(__file__).parent.parent / "docs" / "openapi.json").read_text(encoding="utf-8")
    )
    assert stored == spec, "docs/openapi.json ist veraltet: scripts/export-openapi.sh ausführen"


def test_api_logs_contain_no_keys(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    key = env.api_key()
    client = env.new_client()
    with caplog.at_level(logging.DEBUG):
        client.get("/api/v1/books", headers={"Authorization": f"Bearer {key}"})
        client.get(f"/api/v1/books?api_key={key}")
        book = client.post(
            "/api/v1/books", json={"title": "Log"}, headers={"Authorization": f"Bearer {key}"}
        ).json()
    assert book["title"] == "Log"
    for record in caplog.records:
        if record.name.startswith("ebookapp"):
            assert key not in record.getMessage()
