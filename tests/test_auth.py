"""Anmeldung, Sitzungen, CSRF und Rate Limits des Verwaltungsbereichs."""

from __future__ import annotations

import re

import pytest

from ebookapp.db import iso_in
from tests.conftest import ADMIN_PASSWORD, BASE_URL, Env


def _login_token(client) -> str:
    page = client.get("/admin/login")
    return re.search(r'name="login_token" value="([^"]+)"', page.text).group(1)


@pytest.mark.parametrize(
    "path",
    [
        "/admin",
        "/admin/books",
        "/admin/books/new",
        "/admin/links",
        "/admin/links/new",
        "/admin/settings",
        "/admin/books/bk_x",
        "/admin/links/lnk_x",
        "/api/v1/docs",
    ],
)
def test_admin_pages_require_login(env: Env, path: str) -> None:
    response = env.client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


@pytest.mark.parametrize("path", ["/register", "/signup", "/admin/register", "/admin/signup"])
def test_no_public_registration(env: Env, path: str) -> None:
    assert env.client.get(path).status_code in (303, 404)
    assert env.client.post(path, data={"username": "x", "password": "y" * 20}).status_code in (
        303,
        403,
        404,
        405,
    )
    with env.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_login_sets_hardened_cookie(env: Env) -> None:
    token = _login_token(env.client)
    response = env.client.post(
        "/admin/login",
        data={"username": "admin", "password": ADMIN_PASSWORD, "login_token": token},
    )
    assert response.status_code == 303
    cookies = response.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith("__Host-ebook_session="))
    lower = session.lower()
    assert "httponly" in lower
    assert "secure" in lower
    assert "samesite=lax" in lower
    assert "path=/" in lower
    assert "domain=" not in lower
    assert env.client.get("/admin/books").status_code == 200


def test_session_token_is_stored_hashed(env: Env) -> None:
    env.login()
    cookie = env.client.cookies.get("__Host-ebook_session")
    with env.db() as conn:
        stored = [row[0] for row in conn.execute("SELECT token_hash FROM sessions")]
    assert cookie and cookie not in stored
    assert all(len(value) == 64 for value in stored)


def test_password_is_hashed_with_scrypt(env: Env) -> None:
    with env.db() as conn:
        stored = conn.execute("SELECT password_hash FROM users").fetchone()[0]
    assert stored.startswith("scrypt$")
    assert ADMIN_PASSWORD not in stored


def test_wrong_password_is_rejected_without_details(env: Env) -> None:
    for username in ("admin", "gibt-es-nicht"):
        token = _login_token(env.client)
        response = env.client.post(
            "/admin/login",
            data={"username": username, "password": "falsch-falsch-falsch", "login_token": token},
        )
        assert response.status_code == 401
        assert "Benutzername oder Passwort ist falsch." in response.text
    assert env.client.get("/admin/books").status_code == 303


def test_login_requires_form_token(env: Env) -> None:
    response = env.client.post(
        "/admin/login", data={"username": "admin", "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 403
    assert env.client.get("/admin/books").status_code == 303


def test_login_rate_limit(make_env) -> None:
    env = make_env(LOGIN_MAX_FAILURES="3")
    for _ in range(3):
        token = _login_token(env.client)
        response = env.client.post(
            "/admin/login",
            data={"username": "admin", "password": "falsch-falsch-falsch", "login_token": token},
        )
        assert response.status_code == 401
    token = _login_token(env.client)
    blocked = env.client.post(
        "/admin/login",
        data={"username": "admin", "password": ADMIN_PASSWORD, "login_token": token},
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    assert env.client.get("/admin/books").status_code == 303


def test_post_without_csrf_token_is_rejected(env: Env, admin) -> None:
    response = env.client.post("/admin/books", data={"title": "Ohne Token"})
    assert response.status_code == 403
    wrong = env.client.post("/admin/books", data={"title": "x", "csrf_token": "falsch"})
    assert wrong.status_code == 403
    with env.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_cross_origin_post_is_rejected(env: Env, admin, headers: dict[str, str]) -> None:
    response = admin.post("/admin/books", {"title": "Fremd"}, headers=headers)
    assert response.status_code == 403
    with env.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0


def test_same_origin_post_is_accepted(env: Env, admin) -> None:
    response = admin.post(
        "/admin/books",
        {"title": "Eigen"},
        headers={"Origin": BASE_URL, "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 303


def test_logout_ends_session(env: Env, admin) -> None:
    cookie = env.client.cookies.get("__Host-ebook_session")
    assert admin.post("/admin/logout").status_code == 303
    env.client.cookies.set("__Host-ebook_session", cookie)
    assert env.client.get("/admin/books").status_code == 303


def test_idle_session_expires(env: Env, admin) -> None:
    with env.db() as conn:
        conn.execute("UPDATE sessions SET last_seen_at = ?", (iso_in(hours=-3),))
    assert env.client.get("/admin/books").status_code == 303


def test_absolute_session_lifetime(env: Env, admin) -> None:
    with env.db() as conn:
        conn.execute("UPDATE sessions SET expires_at = ?", (iso_in(minutes=-1),))
    assert env.client.get("/admin/books").status_code == 303


def test_password_change(env: Env, admin) -> None:
    other = env.new_client()
    env.login(other)

    wrong = admin.post(
        "/admin/settings/password",
        {
            "current_password": "falsch",
            "new_password": "neues-langes-passwort",
            "new_password_repeat": "neues-langes-passwort",
        },
    )
    assert wrong.status_code == 422
    short = admin.post(
        "/admin/settings/password",
        {"current_password": ADMIN_PASSWORD, "new_password": "kurz", "new_password_repeat": "kurz"},
    )
    assert short.status_code == 422

    ok = admin.post(
        "/admin/settings/password",
        {
            "current_password": ADMIN_PASSWORD,
            "new_password": "neues-langes-passwort",
            "new_password_repeat": "neues-langes-passwort",
        },
    )
    assert ok.status_code == 303
    assert env.client.get("/admin/books").status_code == 200  # eigene Sitzung bleibt
    assert other.get("/admin/books").status_code == 303  # andere Sitzungen enden

    fresh = env.new_client()
    token = _login_token(fresh)
    old = fresh.post(
        "/admin/login",
        data={"username": "admin", "password": ADMIN_PASSWORD, "login_token": token},
    )
    assert old.status_code == 401
    env.login(env.new_client(), password="neues-langes-passwort")


def test_redeploy_does_not_reset_password(make_env) -> None:
    env = make_env()
    admin = env.login()
    admin.post(
        "/admin/settings/password",
        {
            "current_password": ADMIN_PASSWORD,
            "new_password": "neues-langes-passwort",
            "new_password_repeat": "neues-langes-passwort",
        },
    )
    # Neustart mit unverändertem ADMIN_PASSWORD in der Umgebung
    again = make_env()
    again.login(password="neues-langes-passwort")


def test_short_bootstrap_password_creates_no_admin(make_env) -> None:
    env = make_env(ADMIN_PASSWORD="kurz")
    with env.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert "noch kein Administrator" in env.client.get("/admin/login").text


def test_session_cookie_does_not_authenticate_api(env: Env, admin) -> None:
    assert env.client.get("/api/v1/books").status_code == 401
