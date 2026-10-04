"""Zwei-Faktor-Anmeldung, Sicherheitsprotokoll und Benachrichtigungen."""

from __future__ import annotations

import base64
import re
from typing import Any

import pytest

from ebookapp.services import mail, mfa, security_log
from tests.conftest import ADMIN_PASSWORD, Env

MAIL_ENV = {"BREVO_API_KEY": "xkeysib-test", "MAIL_FROM_EMAIL": "noreply@ebooks.example.com"}


# --------------------------------------------------------------------------- Hilfen


def secret_from_setup(page: str) -> bytes:
    value = re.search(r'id="mfa-secret"[^>]*value="([^"]+)"', page).group(1).replace(" ", "")
    return base64.b32decode(value + "=" * (-len(value) % 8))


def code(secret: bytes, offset: int = 0) -> str:
    return mfa.code_at(secret, mfa.current_step() + offset)


def enable_mfa(env: Env, admin) -> tuple[bytes, list[str]]:
    assert admin.post("/admin/security/mfa/setup").status_code == 303
    page = admin.client.get("/admin/security/mfa/setup")
    assert page.status_code == 200
    assert "<svg" in page.text
    secret = secret_from_setup(page.text)
    response = admin.post(
        "/admin/security/mfa/enable",
        {"code": code(secret), "current_password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    codes = re.search(r'id="recovery-codes"[^>]*>([^<]+)</textarea>', response.text).group(1)
    return secret, codes.split()


def password_step(client) -> Any:
    page = client.get("/admin/login")
    token = re.search(r'name="login_token" value="([^"]+)"', page.text).group(1)
    return client.post(
        "/admin/login",
        data={"username": "admin", "password": ADMIN_PASSWORD, "login_token": token},
    )


def events(env: Env, event: str | None = None) -> list[Any]:
    with env.db() as conn:
        if event:
            return conn.execute(
                "SELECT * FROM security_events WHERE event = ? ORDER BY id", (event,)
            ).fetchall()
        return conn.execute("SELECT * FROM security_events ORDER BY id").fetchall()


class SyncThread:
    """Ersetzt threading.Thread, damit Benachrichtigungen im Test sofort laufen."""

    def __init__(self, target, args=(), **_kw: Any) -> None:
        self.target, self.args = target, args

    def start(self) -> None:
        self.target(*self.args)


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> list[mail.Email]:
    sent: list[mail.Email] = []
    monkeypatch.setattr(mail, "send", lambda settings, email, tags=None: sent.append(email) or "")
    monkeypatch.setattr(security_log.threading, "Thread", SyncThread)
    return sent


# --------------------------------------------------------------------------- TOTP


def test_totp_matches_rfc_6238_vectors() -> None:
    secret = b"12345678901234567890"
    # RFC 6238, Anhang B (SHA-1), auf 6 Stellen gekürzt.
    for timestamp, expected in (
        (59, "287082"),
        (1111111109, "081804"),
        (1111111111, "050471"),
        (1234567890, "005924"),
        (2000000000, "279037"),
    ):
        assert mfa.code_at(secret, timestamp // 30) == expected


def test_provisioning_uri(env: Env) -> None:
    uri = mfa.provisioning_uri(env.settings, "admin", "ABCDEFGH")
    assert uri.startswith("otpauth://totp/E-Book%20Delivery%20%28ebooks.example.com%29%3Aadmin?")
    assert "secret=ABCDEFGH" in uri
    assert "digits=6" in uri and "period=30" in uri


def test_secret_is_stored_encrypted(env: Env, admin) -> None:
    secret, _codes = enable_mfa(env, admin)
    with env.db() as conn:
        row = conn.execute("SELECT totp_secret, totp_pending FROM users").fetchone()
    assert row["totp_pending"] is None
    assert base64.b32encode(secret).decode().rstrip("=") not in row["totp_secret"]
    assert secret not in base64.b64decode(row["totp_secret"])


# --------------------------------------------------------------------------- Einrichtung


def test_setup_requires_valid_code_and_password(env: Env, admin) -> None:
    admin.post("/admin/security/mfa/setup")
    page = admin.client.get("/admin/security/mfa/setup")
    secret = secret_from_setup(page.text)

    wrong_code = admin.post(
        "/admin/security/mfa/enable", {"code": "000000", "current_password": ADMIN_PASSWORD}
    )
    assert wrong_code.status_code == 422
    wrong_password = admin.post(
        "/admin/security/mfa/enable", {"code": code(secret), "current_password": "falsch"}
    )
    assert wrong_password.status_code == 422
    assert "Das Passwort ist falsch." in wrong_password.text
    with env.db() as conn:
        assert conn.execute("SELECT totp_secret FROM users").fetchone()[0] is None

    _secret, codes = enable_mfa(env, admin)
    assert len(codes) == 10
    assert all(re.fullmatch(r"[A-Z2-9]{5}-[A-Z2-9]{5}", item) for item in codes)
    page = admin.client.get("/admin/security")
    assert "eingeschaltet" in page.text
    assert len(events(env, "mfa_enabled")) == 1


# --------------------------------------------------------------------------- Anmeldung


def test_login_requires_second_factor(env: Env, admin) -> None:
    secret, _codes = enable_mfa(env, admin)
    client = env.new_client()

    first = password_step(client)
    assert first.status_code == 303
    assert first.headers["location"] == "/admin/login/verify"
    assert "ebook_session" not in first.headers.get("set-cookie", "")
    # Ohne zweiten Faktor kein Zugang
    assert client.get("/admin/books").status_code == 303

    page = client.get("/admin/login/verify")
    assert page.status_code == 200
    assert 'autocomplete="one-time-code"' in page.text

    wrong = client.post("/admin/login/verify", data={"code": "123456"})
    assert wrong.status_code == 401
    assert "Noch 4 Versuche" in wrong.text
    assert len(events(env, "mfa_failed")) == 1

    ok = client.post("/admin/login/verify", data={"code": code(secret, 1)})
    assert ok.status_code == 303
    assert ok.headers["location"] == "/admin/books"
    assert client.get("/admin/books").status_code == 200
    assert events(env, "login_success")[-1]["detail"] == "2FA"


def test_code_cannot_be_reused(env: Env, admin) -> None:
    secret, _codes = enable_mfa(env, admin)
    used = code(secret, 1)
    client = env.new_client()
    password_step(client)
    assert client.post("/admin/login/verify", data={"code": used}).status_code == 303

    other = env.new_client()
    password_step(other)
    assert other.post("/admin/login/verify", data={"code": used}).status_code == 401


def test_recovery_code_works_once(env: Env, admin) -> None:
    _secret, codes = enable_mfa(env, admin)
    client = env.new_client()
    password_step(client)
    response = client.post("/admin/login/verify", data={"code": codes[0].lower()})
    assert response.status_code == 303
    assert len(events(env, "recovery_code_used")) == 1

    again = env.new_client()
    password_step(again)
    assert again.post("/admin/login/verify", data={"code": codes[0]}).status_code == 401
    assert "9" in admin.client.get("/admin/security").text  # 9 unbenutzte Codes


def test_challenge_ends_after_five_wrong_codes(env: Env, admin) -> None:
    secret, _codes = enable_mfa(env, admin)
    client = env.new_client()
    password_step(client)
    for _attempt in range(4):
        assert client.post("/admin/login/verify", data={"code": "000000"}).status_code == 401
    last = client.post("/admin/login/verify", data={"code": "000000"})
    assert "Bitte melde dich erneut an" in last.text
    # Auch der richtige Code hilft dann nicht mehr: neue Anmeldung nötig.
    assert client.post("/admin/login/verify", data={"code": code(secret)}).status_code == 401


def test_codes_cannot_be_brute_forced_with_new_challenges(env: Env, admin) -> None:
    """Wer das Passwort kennt, kann mit immer neuen Anmeldungen keine Codes durchprobieren."""
    secret, _codes = enable_mfa(env, admin)
    blocked = False
    for attempt in range(4):
        client = env.new_client(headers={"X-Forwarded-For": f"198.51.{attempt}.1"})
        if password_step(client).status_code != 303:
            continue
        for _try in range(5):
            response = client.post("/admin/login/verify", data={"code": "000000"})
            if response.status_code == 429:
                blocked = True
                break
        if blocked:
            break
    assert blocked
    assert events(env, "login_blocked")
    # Während der Sperre gilt auch ein richtiger Code nicht.
    client = env.new_client(headers={"X-Forwarded-For": "203.0.113.99"})
    password_step(client)
    assert client.post("/admin/login/verify", data={"code": code(secret, 1)}).status_code == 429


def test_expired_challenge(env: Env, admin) -> None:
    enable_mfa(env, admin)
    client = env.new_client()
    password_step(client)
    with env.db() as conn:
        conn.execute("UPDATE login_challenges SET expires_at = '2000-01-01T00:00:00Z'")
    page = client.get("/admin/login/verify")
    assert page.status_code == 401
    assert "abgelaufen" in page.text


def test_verify_without_challenge(env: Env) -> None:
    client = env.new_client()
    assert client.get("/admin/login/verify").status_code == 401
    assert client.post("/admin/login/verify", data={"code": "123456"}).status_code == 401


# --------------------------------------------------------------------------- Ausschalten


def test_disable_requires_password_and_code(env: Env, admin) -> None:
    secret, _codes = enable_mfa(env, admin)
    no_code = admin.post(
        "/admin/security/mfa/disable", {"current_password": ADMIN_PASSWORD, "code": "000000"}
    )
    assert no_code.status_code == 422
    with env.db() as conn:
        assert mfa.is_enabled(conn, 1)
    ok = admin.post(
        "/admin/security/mfa/disable",
        {"current_password": ADMIN_PASSWORD, "code": code(secret, 1)},
    )
    assert ok.status_code == 303
    with env.db() as conn:
        assert not mfa.is_enabled(conn, 1)
        assert conn.execute("SELECT COUNT(*) FROM recovery_codes").fetchone()[0] == 0
    assert events(env, "mfa_disabled")
    # Danach wieder Anmeldung nur mit Passwort
    assert password_step(env.new_client()).headers["location"] == "/admin/books"


def test_new_recovery_codes_invalidate_old_ones(env: Env, admin) -> None:
    secret, old = enable_mfa(env, admin)
    response = admin.post(
        "/admin/security/mfa/recovery",
        {"current_password": ADMIN_PASSWORD, "code": code(secret, 1)},
    )
    assert response.status_code == 200
    new = re.search(r'id="recovery-codes"[^>]*>([^<]+)</textarea>', response.text).group(1).split()
    assert set(new).isdisjoint(old)
    client = env.new_client()
    password_step(client)
    assert client.post("/admin/login/verify", data={"code": old[0]}).status_code == 401
    assert client.post("/admin/login/verify", data={"code": new[0]}).status_code == 303


def test_cli_disable_2fa(env: Env, admin, monkeypatch: pytest.MonkeyPatch) -> None:
    from ebookapp import cli

    enable_mfa(env, admin)
    monkeypatch.setattr(cli, "_context", lambda: (env.settings, None, None))
    assert cli.main(["disable-2fa"]) == 0
    with env.db() as conn:
        assert not mfa.is_enabled(conn, 1)
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert events(env, "mfa_disabled")[-1]["detail"] == "ebookctl disable-2fa"


# --------------------------------------------------------------------------- Protokoll


def test_security_log_records_events(env: Env, admin) -> None:
    client = env.new_client()
    page = client.get("/admin/login")
    token = re.search(r'name="login_token" value="([^"]+)"', page.text).group(1)
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "falsch", "login_token": token},
        headers={"User-Agent": "Testbrowser/1.0"},
    )
    failed = events(env, "login_failed")
    assert failed and failed[-1]["username"] == "admin"
    assert failed[-1]["user_agent"] == "Testbrowser/1.0"
    assert failed[-1]["ip"]

    admin.post("/admin/settings/api-keys", {"name": "n8n", "scopes": ["books:read"]})
    assert "n8n (books:read)" in events(env, "api_key_created")[-1]["detail"]
    assert events(env, "login_success")

    page = admin.client.get("/admin/security")
    assert "Anmeldung fehlgeschlagen" in page.text
    assert "API-Schlüssel erstellt" in page.text
    warnings = admin.client.get("/admin/security?filter=warnings")
    assert "Anmeldung fehlgeschlagen" in warnings.text
    assert "API-Schlüssel erstellt" not in warnings.text


def test_rejected_api_keys_and_webhooks_are_logged(make_env) -> None:
    env = make_env(WHOP_WEBHOOK_SECRET="ws_" + "a" * 32)
    client = env.new_client()
    client.headers["Authorization"] = "Bearer ebk_falsch"
    assert client.get("/api/v1/books").status_code == 401
    assert events(env, "api_key_rejected")

    response = env.new_client().post(
        "/webhooks/whop",
        content=b"{}",
        headers={"webhook-id": "m", "webhook-timestamp": "1", "webhook-signature": "v1,x"},
    )
    assert response.status_code == 401
    assert events(env, "webhook_rejected")


def test_noisy_warnings_are_throttled(make_env) -> None:
    env = make_env(WHOP_WEBHOOK_SECRET="ws_" + "a" * 32)
    for _attempt in range(40):
        env.new_client().post(
            "/webhooks/whop",
            content=b"{}",
            headers={"webhook-id": "m", "webhook-timestamp": "1", "webhook-signature": "v1,x"},
        )
    assert len(events(env, "webhook_rejected")) == 20


def test_security_log_retention(env: Env) -> None:
    from ebookapp.services import misc

    with env.db() as conn:
        security_log.record(conn, env.settings, "logout", username="alt")
        conn.execute("UPDATE security_events SET created_at = '2000-01-01T00:00:00Z'")
        security_log.record(conn, env.settings, "logout", username="neu")
        misc.housekeeping(conn, env.settings)
        names = [row["username"] for row in conn.execute("SELECT username FROM security_events")]
    assert names == ["neu"]


# --------------------------------------------------------------------------- Benachrichtigungen


def test_login_notification(make_env, outbox: list[mail.Email]) -> None:
    env = make_env(**MAIL_ENV)
    admin = env.login()
    saved = admin.post(
        "/admin/security/alerts", {"alert_email": "ich@example.com", "on_login": "1"}
    )
    assert saved.status_code == 303
    outbox.clear()
    env.login(env.new_client(headers={"User-Agent": "Neues Geraet"}))
    assert len(outbox) == 1
    email = outbox[0]
    assert email.to_email == "ich@example.com"
    assert email.subject == "[Sicherheit] Anmeldung erfolgreich"
    assert "https://ebooks.example.com/admin/security" in email.text


def test_warning_notification_is_throttled(make_env, outbox: list[mail.Email]) -> None:
    env = make_env(**MAIL_ENV)
    admin = env.login()
    enable_mfa(env, admin)
    admin.post("/admin/security/alerts", {"alert_email": "ich@example.com", "on_warning": "1"})
    outbox.clear()
    for _attempt in range(3):
        client = env.new_client()
        password_step(client)
        client.post("/admin/login/verify", data={"code": "000000"})
    subjects = [email.subject for email in outbox]
    assert subjects.count("[Sicherheit] Falscher Bestätigungscode") == 1


def test_no_notification_when_disabled(make_env, outbox: list[mail.Email]) -> None:
    env = make_env(**MAIL_ENV)
    env.login()
    env.login(env.new_client())
    assert outbox == []


def test_alert_settings_validation_and_test_mail(make_env, outbox: list[mail.Email]) -> None:
    env = make_env(**MAIL_ENV)
    admin = env.login()
    missing = admin.post("/admin/security/alerts", {"on_login": "1"})
    assert missing.status_code == 422
    invalid = admin.post("/admin/security/alerts", {"alert_email": "kein-at", "on_login": "1"})
    assert invalid.status_code == 422
    admin.post("/admin/security/alerts", {"alert_email": "ich@example.com"})
    assert admin.post("/admin/security/alerts/test").status_code == 303
    assert outbox[-1].to_email == "ich@example.com"


def test_english_notification(make_env, outbox: list[mail.Email]) -> None:
    env = make_env(**MAIL_ENV)
    admin = env.login()
    admin.client.cookies.set("ebook_lang", "en")
    admin.post("/admin/security/alerts", {"alert_email": "me@example.com", "on_login": "1"})
    outbox.clear()
    env.login(env.new_client())  # deutsche Oberfläche, Benachrichtigung bleibt englisch
    assert outbox[0].subject == "[Security] Signed in"
