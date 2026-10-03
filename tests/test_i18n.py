"""Sprache der Verwaltung: Englisch als Standard, Deutsch umschaltbar."""

from __future__ import annotations

import re

from ebookapp import i18n
from ebookapp.translations_en import EN
from tests.conftest import ADMIN_PASSWORD, Env
from tests.i18n_extract import all_strings, missing

PLACEHOLDER = re.compile(r"%\((\w+)\)s|\{(\w+)\}")
TAG = re.compile(r"</?([a-z]+)[^>]*>")


def english_admin(env: Env) -> object:
    client = env.new_client(language=None)
    page = client.get("/admin/login")
    token = re.search(r'name="login_token" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/admin/login",
        data={"username": "admin", "password": ADMIN_PASSWORD, "login_token": token},
    )
    assert response.status_code == 303
    return client


def test_every_marked_string_has_a_translation() -> None:
    gaps = missing()
    assert not gaps, "Fehlende Übersetzungen:\n" + "\n".join(
        f"{where}: {text!r}" for text, where in sorted(gaps.items(), key=lambda i: i[1])
    )


def test_translations_keep_placeholders_and_markup() -> None:
    for german, english in EN.items():
        assert sorted(PLACEHOLDER.findall(german)) == sorted(PLACEHOLDER.findall(english)), german
        assert sorted(TAG.findall(german)) == sorted(TAG.findall(english)), german


def test_catalog_has_no_stale_entries() -> None:
    used = set(all_strings())
    stale = sorted(set(EN) - used)
    assert not stale, f"Nicht mehr verwendete Übersetzungen: {stale[:20]}"


def test_admin_is_english_by_default(env: Env) -> None:
    client = env.new_client(language=None)
    login = client.get("/admin/login")
    assert '<html lang="en">' in login.text
    assert "Sign in" in login.text
    assert "Anmelden" not in login.text
    assert 'href="/admin/language/de?next=/admin/login"' in login.text

    client = english_admin(env)
    for path, expected in (
        ("/admin/books", "Books"),
        ("/admin/links", "Download links"),
        ("/admin/orders", "Orders"),
        ("/admin/settings", "Settings"),
        ("/admin/books/new", "New book"),
        ("/admin/links/new", "New"),
        ("/api/v1/docs", "API"),
    ):
        page = client.get(path)
        assert page.status_code == 200, path
        assert '<html lang="en">' in page.text, path
        assert expected in page.text, path
        for german in ("Einstellungen", "Abmelden", "Bücher", "Downloadlinks"):
            assert german not in page.text, (path, german)


def test_switch_to_german_and_back(env: Env) -> None:
    client = english_admin(env)
    switched = client.get("/admin/language/de?next=/admin/settings")
    assert switched.status_code == 303
    assert switched.headers["location"] == "/admin/settings"
    assert "ebook_lang=de" in switched.headers["set-cookie"]
    assert "HttpOnly" in switched.headers["set-cookie"]
    page = client.get("/admin/settings")
    assert '<html lang="de">' in page.text
    assert "Einstellungen" in page.text
    assert 'href="/admin/language/en?next=/admin/settings"' in page.text

    client.get("/admin/language/en?next=/admin/books")
    assert "Books" in client.get("/admin/books").text


def test_language_switch_rejects_foreign_targets(env: Env) -> None:
    client = env.new_client(language=None)
    for target in ("https://evil.example/", "//evil.example/admin", "/\\evil", "/d/abc", ""):
        response = client.get("/admin/language/de", params={"next": target})
        assert response.headers["location"] == "/admin/books", target
    unknown = client.get("/admin/language/xx?next=/admin/login")
    assert "ebook_lang=en" in unknown.headers["set-cookie"]


def test_messages_and_errors_follow_the_language(env: Env) -> None:
    client = english_admin(env)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/admin/books/new").text)
    response = client.post("/admin/books", data={"csrf_token": csrf.group(1), "title": ""})
    assert response.status_code == 422
    assert "Pflichtangabe" not in response.text

    # API-Fehlermeldungen: Englisch ohne Cookie, Deutsch mit Cookie.
    english_api = env.new_client(language=None)
    english_api.headers["Authorization"] = f"Bearer {env.api_key()}"
    error = english_api.get("/api/v1/books/bk_0000000000000000").json()["error"]
    assert error["code"] == "book_not_found"
    assert error["message"] == EN["Dieses Buch gibt es nicht."]
    german = env.api().get("/api/v1/books/bk_0000000000000000").json()["error"]
    assert german["message"] == "Dieses Buch gibt es nicht."


def test_openapi_follows_the_language(env: Env, admin) -> None:
    english = env.new_client(language=None)
    english.headers["Authorization"] = f"Bearer {env.api_key()}"
    spec = english.get("/api/v1/openapi.json").json()
    assert spec["info"]["title"] == "E-book delivery API"
    assert "Books" in [tag["name"] for tag in spec["tags"]]
    assert spec["paths"]["/books"]["get"]["tags"] == ["Books"]
    assert spec["paths"]["/books"]["get"]["summary"] == "List books"

    german = env.api().get("/api/v1/openapi.json").json()
    assert german["info"]["title"] == "E-Book-Auslieferung API"
    assert german["paths"]["/books"]["get"]["tags"] == ["Bücher"]


def test_buyer_pages_are_independent_of_the_admin_language(env: Env, admin) -> None:
    book_id = admin.published_book("Unabhängig")
    _link_id, path = admin.create_link(book_id)
    page = env.new_client(language="en").get(path)
    assert "Dein E-Book" in page.text  # deutsches Buch bleibt deutsch


def test_gettext_formats_values() -> None:
    with i18n.language("en"):
        assert i18n.gettext("Unbekannter Text %(x)s", x=1) == "Unbekannter Text 1"
    with i18n.language("de"):
        assert i18n.gettext("Abmelden (%(user)s)", user="admin") == "Abmelden (admin)"
    assert i18n.from_cookie_header("a=1; ebook_lang=de; b=2") == "de"
    assert i18n.from_cookie_header("ebook_lang=fr") == "en"
    assert i18n.from_cookie_header("") == "en"


def test_no_function_shadows_the_translation_function() -> None:
    """``x, _, y = …`` oder ``for _ in …`` macht ``_`` lokal; ``_()`` scheitert dann."""
    import ast

    from tests.i18n_extract import PACKAGE

    problems = []
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            assigned = any(
                isinstance(node, ast.Name) and node.id == "_" and isinstance(node.ctx, ast.Store)
                for node in ast.walk(func)
            )
            called = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_"
                for node in ast.walk(func)
            )
            if assigned and called:
                problems.append(f"{path.name}:{func.lineno} {func.name}")
    assert not problems, problems
