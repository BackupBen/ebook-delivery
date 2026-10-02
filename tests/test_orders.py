"""Whop-Webhook, Bestellungen und E-Mail-Versand über Brevo."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import re
import time
import urllib.error
from typing import Any

import pytest

from ebookapp.db import now_iso
from ebookapp.services import mail, orders
from tests.conftest import BASE_URL, Env, make_epub, make_pdf

SECRET = "ws_" + "ab12" * 16
MAIL_ENV = {
    "WHOP_WEBHOOK_SECRET": SECRET,
    "BREVO_API_KEY": "xkeysib-test",
    "MAIL_FROM_EMAIL": "noreply@ebooks.example.com",
    "MAIL_FROM_NAME": "Your E-Book",
}


class Outbox:
    """Ersetzt den Versand über Brevo und sammelt die E-Mails."""

    def __init__(self) -> None:
        self.sent: list[mail.Email] = []
        self.fail: mail.MailError | None = None

    def __call__(self, settings: Any, email: mail.Email, *, tags: list[str] | None = None) -> str:
        if self.fail is not None:
            raise self.fail
        self.sent.append(email)
        return f"<msg-{len(self.sent)}@brevo>"


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> Outbox:
    box = Outbox()
    monkeypatch.setattr(mail, "send", box)
    return box


@pytest.fixture
def shop(make_env) -> Env:
    return make_env(**MAIL_ENV)


def sign(body: bytes, webhook_id: str, timestamp: int | None = None, secret: str = SECRET):
    timestamp = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(
        secret.encode(), f"{webhook_id}.{timestamp}.".encode() + body, hashlib.sha256
    ).digest()
    return {
        "webhook-id": webhook_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": "v1," + base64.b64encode(digest).decode(),
        "content-type": "application/json",
    }


def payment_event(
    payment_id: str = "pay_123",
    product_id: str = "prod_AbC123xyz",
    email: str = "Kaeufer@Example.com",
    name: str = "Max Muster",
    *,
    legacy: bool = True,
) -> dict[str, Any]:
    if legacy:
        data = {
            "id": payment_id,
            "status": "paid",
            "user": {"id": "user_1", "email": email, "name": name},
            "product": {"id": product_id, "title": "Whop-Titel"},
        }
    else:
        data = {
            "id": payment_id,
            "status": "paid",
            "product_id": product_id,
            "customer_email": email,
            "user": {"id": "user_1", "name": name},
        }
    return {"id": "msg_x", "type": "payment.succeeded", "api_version": "v1", "data": data}


def post_event(env: Env, event: dict[str, Any], webhook_id: str = "msg_1", **kw: Any):
    body = json.dumps(event).encode()
    return env.new_client().post(
        "/webhooks/whop", content=body, headers=sign(body, webhook_id, **kw)
    )


def book_for_product(admin, title: str, product_id: str, language: str = "de") -> str:
    book_id = admin.create_book(title, whop_product_id=product_id, language=language)
    edition_id = admin.draft_id(book_id)
    assert admin.upload(book_id, edition_id, make_pdf(title), make_epub()).status_code == 303
    assert admin.publish(book_id, edition_id).status_code == 303
    return book_id


def deliver(env: Env) -> int:
    return env.app.state.ctx.mailer.tick()


def order_row(env: Env, payment_id: str = "pay_123"):
    with env.db() as conn:
        return conn.execute("SELECT * FROM orders WHERE payment_id = ?", (payment_id,)).fetchone()


def link_path(email: mail.Email) -> str:
    url = re.search(rf"{re.escape(BASE_URL)}/d/[A-Za-z0-9_-]+", email.text).group(0)
    assert url in email.html
    return url[len(BASE_URL) :]


# --------------------------------------------------------------------------- Gesamtablauf


def test_payment_creates_link_and_sends_email(shop: Env, outbox: Outbox) -> None:
    admin = shop.login()
    book_id = book_for_product(admin, "Gartenbuch", "prod_AbC123xyz")

    response = post_event(shop, payment_event())
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok", "result": "pending"}
    row = order_row(shop)
    assert row["status"] == "pending"
    assert row["email"] == "kaeufer@example.com"
    assert row["book_id"] == book_id
    assert row["wrapped_code"]

    assert deliver(shop) == 1
    assert len(outbox.sent) == 1
    email = outbox.sent[0]
    assert email.to_email == "kaeufer@example.com"
    assert email.to_name == "Max Muster"
    assert email.subject == "Dein E-Book: Gartenbuch"
    assert email.text.startswith("Hallo Max Muster,")
    assert "„Gartenbuch“" in email.text
    assert "Der Link ist dauerhaft gültig." in email.text
    assert "E-Book herunterladen" in email.html

    row = order_row(shop)
    assert row["status"] == "sent"
    assert row["wrapped_code"] is None  # Code wird nach dem Versand nicht aufbewahrt
    assert row["message_id"] == "<msg-1@brevo>"

    # Der Link in der E-Mail funktioniert.
    path = link_path(email)
    page = shop.new_client().get(path)
    assert page.status_code == 200
    assert "Gartenbuch" in page.text
    assert shop.new_client().get(f"{path}/pdf").status_code == 200

    # Ein weiterer Durchlauf versendet nichts doppelt.
    assert deliver(shop) == 0
    assert len(outbox.sent) == 1

    # Bestellung in der Verwaltung
    listing = admin.client.get("/admin/orders")
    assert "kaeufer@example.com" in listing.text
    assert "Versendet" in listing.text
    detail = admin.client.get(f"/admin/orders/{row['id']}")
    assert "pay_123" in detail.text
    assert "Neuen Link senden" in detail.text


def test_english_book_gets_english_email(shop: Env, outbox: Outbox) -> None:
    admin = shop.login()
    book_for_product(admin, "Nordic Home", "prod_Nordic1", language="en")
    assert post_event(shop, payment_event(product_id="prod_Nordic1", name="")).status_code == 200
    deliver(shop)
    email = outbox.sent[0]
    assert email.subject == "Your e-book: Nordic Home"
    assert email.text.startswith("Hi,")
    assert "The link does not expire." in email.text
    assert "Download your e-book" in email.html
    assert 'lang="en"' in email.html
    assert "Your e-book" in shop.new_client().get(link_path(email)).text


def test_current_whop_payload_format(shop: Env, outbox: Outbox) -> None:
    admin = shop.login()
    book_for_product(admin, "Neu", "prod_New123")
    event = payment_event(product_id="prod_New123", email="neu@example.com", legacy=False)
    assert post_event(shop, event).json()["result"] == "pending"
    deliver(shop)
    assert outbox.sent[0].to_email == "neu@example.com"


# --------------------------------------------------------------------------- Signatur


def test_signature_is_required(shop: Env, outbox: Outbox) -> None:
    body = json.dumps(payment_event()).encode()
    client = shop.new_client()

    missing = client.post("/webhooks/whop", content=body)
    assert missing.status_code == 400

    wrong = sign(body, "msg_1", secret="ws_falsch")
    assert client.post("/webhooks/whop", content=body, headers=wrong).status_code == 401

    tampered = sign(body, "msg_1")
    changed = body.replace(b"pay_123", b"pay_999")
    assert client.post("/webhooks/whop", content=changed, headers=tampered).status_code == 401

    old = sign(body, "msg_1", timestamp=int(time.time()) - 600)
    assert client.post("/webhooks/whop", content=body, headers=old).status_code == 401

    other_id = sign(body, "msg_1")
    other_id["webhook-id"] = "msg_2"
    assert client.post("/webhooks/whop", content=body, headers=other_id).status_code == 401

    with shop.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_multiple_signatures_in_header() -> None:
    body = b'{"type":"x"}'
    headers = sign(body, "msg_1")
    headers["webhook-signature"] = "v1,AAAA " + headers["webhook-signature"]
    assert orders.verify_whop_signature(SECRET, headers, body) == "msg_1"
    headers["webhook-signature"] = "v2," + headers["webhook-signature"].split(",", 2)[-1]
    with pytest.raises(orders.WebhookRejected):
        orders.verify_whop_signature(SECRET, headers, body)


def test_webhook_rejected_without_secret(env: Env) -> None:
    response = post_event(env, payment_event())
    assert response.status_code == 503


def test_invalid_json_is_rejected(shop: Env) -> None:
    body = b"kein json"
    response = shop.new_client().post("/webhooks/whop", content=body, headers=sign(body, "m"))
    assert response.status_code == 400


# --------------------------------------------------------------------------- Wiederholungen


def test_duplicate_deliveries_create_one_order(shop: Env, outbox: Outbox) -> None:
    admin = shop.login()
    book_for_product(admin, "Einmal", "prod_AbC123xyz")
    assert post_event(shop, payment_event(), "msg_1").json()["result"] == "pending"
    assert post_event(shop, payment_event(), "msg_1").json()["result"] == "duplicate"
    # Gleiche Zahlung, andere Zustellung (z. B. erneut ausgelöst): kein zweiter Link.
    assert post_event(shop, payment_event(), "msg_2").json()["result"] == "duplicate"
    deliver(shop)
    assert len(outbox.sent) == 1
    with shop.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 1


def test_other_events_are_ignored(shop: Env) -> None:
    event = {"id": "msg_9", "type": "membership.activated", "data": {"id": "mem_1"}}
    response = post_event(shop, event, "msg_9")
    assert response.status_code == 200
    assert response.json()["result"] == "ignored"
    with shop.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


# --------------------------------------------------------------------------- Sonderfälle


def test_unknown_product_can_be_processed_later(shop: Env, outbox: Outbox) -> None:
    admin = shop.login()
    assert post_event(shop, payment_event(product_id="prod_Spaeter")).json()["result"] == (
        "unmatched"
    )
    row = order_row(shop)
    assert row["status"] == "unmatched"
    assert "prod_Spaeter" in row["detail"]
    page = admin.client.get(f"/admin/orders/{row['id']}")
    assert "Erneut verarbeiten" in page.text

    book_for_product(admin, "Später", "prod_Spaeter")
    assert admin.post(f"/admin/orders/{row['id']}/retry").status_code == 303
    assert order_row(shop)["status"] == "pending"
    deliver(shop)
    assert order_row(shop)["status"] == "sent"
    assert outbox.sent[0].subject == "Dein E-Book: Später"


def test_book_without_published_edition_is_unmatched(shop: Env) -> None:
    admin = shop.login()
    admin.create_book("Entwurf", whop_product_id="prod_Draft1")
    post_event(shop, payment_event(product_id="prod_Draft1"))
    row = order_row(shop)
    assert row["status"] == "unmatched"
    assert "keine veröffentlichte Ausgabe" in row["detail"]


def test_missing_email(shop: Env) -> None:
    admin = shop.login()
    book_for_product(admin, "Ohne", "prod_AbC123xyz")
    post_event(shop, payment_event(email=""))
    assert order_row(shop)["status"] == "no_email"
    assert order_row(shop)["link_id"] is None


def test_mail_not_configured_keeps_order_pending(make_env, outbox: Outbox) -> None:
    env = make_env(WHOP_WEBHOOK_SECRET=SECRET)
    admin = env.login()
    book_for_product(admin, "Warten", "prod_AbC123xyz")
    assert post_event(env, payment_event()).json()["result"] == "pending"
    assert deliver(env) == 0
    assert outbox.sent == []
    assert order_row(env)["status"] == "pending"
    assert "nicht vollständig eingerichtet" in admin.client.get("/admin/orders").text


# --------------------------------------------------------------------------- Fehler beim Versand


def test_temporary_mail_errors_are_retried(shop: Env, outbox: Outbox) -> None:
    admin = shop.login()
    book_for_product(admin, "Wackelig", "prod_AbC123xyz")
    post_event(shop, payment_event())
    outbox.fail = mail.MailError("Brevo nicht erreichbar", permanent=False)
    assert deliver(shop) == 0
    row = order_row(shop)
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] > now_iso()  # in der Zukunft
    assert "nicht erreichbar" in row["detail"]
    # Noch nicht fällig: kein neuer Versuch.
    assert deliver(shop) == 0
    assert order_row(shop)["attempts"] == 1

    # Fälligkeit vorziehen, Brevo antwortet wieder.
    with shop.db() as conn:
        conn.execute("UPDATE orders SET next_attempt_at = '2000-01-01T00:00:00Z'")
    outbox.fail = None
    assert deliver(shop) == 1
    assert order_row(shop)["status"] == "sent"
    assert order_row(shop)["attempts"] == 2


def test_permanent_mail_error_and_manual_retry(shop: Env, outbox: Outbox) -> None:
    admin = shop.login()
    book_for_product(admin, "Kaputt", "prod_AbC123xyz")
    post_event(shop, payment_event(email="tippfehler@example.con"))
    outbox.fail = mail.MailError("Brevo antwortete mit 400: invalid email", permanent=True)
    deliver(shop)
    row = order_row(shop)
    assert row["status"] == "failed"
    assert row["wrapped_code"]  # bleibt für einen neuen Versuch erhalten

    outbox.fail = None
    page = admin.client.get(f"/admin/orders/{row['id']}")
    assert "invalid email" in page.text
    saved = admin.post(f"/admin/orders/{row['id']}/email", {"email": "richtig@example.com"})
    assert saved.status_code == 303
    assert order_row(shop)["status"] == "pending"
    deliver(shop)
    assert order_row(shop)["status"] == "sent"
    assert outbox.sent[0].to_email == "richtig@example.com"

    bad = admin.post(f"/admin/orders/{row['id']}/email", {"email": "kein-at"})
    assert bad.status_code == 422


def test_gives_up_after_all_retries(shop: Env, outbox: Outbox) -> None:
    admin = shop.login()
    book_for_product(admin, "Nie", "prod_AbC123xyz")
    post_event(shop, payment_event())
    outbox.fail = mail.MailError("503", permanent=False)
    for _ in range(len(orders.RETRY_MINUTES) + 1):
        with shop.db() as conn:
            conn.execute("UPDATE orders SET next_attempt_at = '2000-01-01T00:00:00Z'")
        deliver(shop)
    row = order_row(shop)
    assert row["status"] == "failed"
    assert row["attempts"] == len(orders.RETRY_MINUTES) + 1


def test_resend_new_link_revokes_old_one(shop: Env, outbox: Outbox) -> None:
    admin = shop.login()
    book_for_product(admin, "Verloren", "prod_AbC123xyz")
    post_event(shop, payment_event())
    deliver(shop)
    first = link_path(outbox.sent[0])
    row = order_row(shop)

    retry = admin.post(f"/admin/orders/{row['id']}/retry")
    assert retry.status_code == 409  # bereits versendet

    assert admin.post(f"/admin/orders/{row['id']}/resend").status_code == 303
    deliver(shop)
    assert len(outbox.sent) == 2
    second = link_path(outbox.sent[1])
    assert second != first
    assert shop.new_client().get(first).status_code == 410
    assert shop.new_client().get(second).status_code == 200


def test_claim_prevents_double_delivery(shop: Env, outbox: Outbox) -> None:
    admin = shop.login()
    book_for_product(admin, "Parallel", "prod_AbC123xyz")
    post_event(shop, payment_event())
    order_id = order_row(shop)["id"]
    with shop.db() as conn:
        now = now_iso()
        assert orders._claim(conn, order_id, now) is True
        # Ein zweiter Durchlauf (z. B. ein paralleler Thread) erhält sie nicht.
        assert orders._claim(conn, order_id, now) is False


# --------------------------------------------------------------------------- Einstellungen


def test_settings_show_mail_status_and_send_test(shop: Env, outbox: Outbox) -> None:
    admin = shop.login()
    page = admin.client.get("/admin/settings")
    assert "noreply@ebooks.example.com" in page.text
    assert "Test-E-Mail senden" in page.text
    sent = admin.post("/admin/settings/mail-test", {"email": "ich@example.com", "language": "en"})
    assert sent.status_code == 303
    assert outbox.sent[0].subject == "[Test] Your e-book: Sample Book"
    assert outbox.sent[0].to_email == "ich@example.com"

    outbox.fail = mail.MailError("Brevo antwortete mit 401: Key not found", permanent=True)
    failed = admin.post("/admin/settings/mail-test", {"email": "ich@example.com"})
    assert failed.status_code == 502
    assert "Key not found" in failed.text


def test_custom_subject_and_template(shop: Env, outbox: Outbox) -> None:
    admin = shop.login()
    saved = admin.post(
        "/admin/settings",
        {
            "max_pdf_mb": "300",
            "max_epub_mb": "100",
            "max_cover_mb": "10",
            "message_template": "Moin {name}!\n{link}",
            "message_template_en": "Hey {name}!\n{link}",
            "mail_subject": "Hier ist {titel}",
            "mail_subject_en": "Here is {titel}",
        },
    )
    assert saved.status_code == 303
    book_for_product(admin, "Eigenes", "prod_AbC123xyz")
    post_event(shop, payment_event(name="Erika"))
    deliver(shop)
    assert outbox.sent[0].subject == "Hier ist Eigenes"
    assert outbox.sent[0].text.startswith("Moin Erika!")


def test_manual_message_drops_name_placeholder(env: Env, admin) -> None:
    book_id = admin.published_book("Manuell")
    new = env.client.get(f"/admin/links/new?book_id={book_id}")
    token = re.search(r'name="form_token" value="([^"]+)"', new.text).group(1)
    created = admin.post(
        "/admin/links", {"book_id": book_id, "formats": ["pdf"], "form_token": token}
    )
    assert "Hallo," in created.text
    assert "{name}" not in created.text


def test_orders_page_shows_webhook_url(shop: Env) -> None:
    admin = shop.login()
    page = admin.client.get("/admin/orders")
    assert f"{BASE_URL}/webhooks/whop" in page.text
    assert "nicht vollständig eingerichtet" not in page.text


# --------------------------------------------------------------------------- Brevo-Client


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def test_brevo_request(make_env, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_env(**MAIL_ENV, MAIL_REPLY_TO="hilfe@example.com").settings
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeResponse(b'{"messageId": "<abc@smtp-relay>"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    email = mail.Email("k@example.com", "Käufer", "Betreff", "Text", "<p>Text</p>")
    assert mail.send(settings, email, tags=["whop-ebook"]) == "<abc@smtp-relay>"
    assert captured["url"] == "https://api.brevo.com/v3/smtp/email"
    assert captured["headers"]["api-key"] == "xkeysib-test"
    body = captured["body"]
    assert body["sender"] == {"email": "noreply@ebooks.example.com", "name": "Your E-Book"}
    assert body["to"] == [{"email": "k@example.com", "name": "Käufer"}]
    assert body["replyTo"] == {"email": "hilfe@example.com"}
    assert body["tags"] == ["whop-ebook"]
    assert body["textContent"] == "Text"


@pytest.mark.parametrize(
    ("code", "permanent"), [(400, True), (401, True), (429, False), (502, False)]
)
def test_brevo_errors(
    make_env, monkeypatch: pytest.MonkeyPatch, code: int, permanent: bool
) -> None:
    settings = make_env(**MAIL_ENV).settings

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, code, "Fehler", {}, io.BytesIO(b'{"message": "kaputt"}')
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(mail.MailError) as caught:
        mail.send(settings, mail.Email("k@example.com", "", "B", "T", "H"))
    assert caught.value.permanent is permanent
    assert "kaputt" in str(caught.value)


def test_invalid_sender_address_is_a_config_error(make_env) -> None:
    from ebookapp.config import ConfigError, load_settings

    with pytest.raises(ConfigError):
        load_settings({"SECRET_KEY": "x" * 40, "MAIL_FROM_EMAIL": "kein-at"})


def test_html_escapes_content() -> None:
    url = "https://ebooks.example.com/d/abc"
    text = mail.fill(
        "Hallo {name},\n\n<b>{titel}</b>\n\n{link}\n\nGruß",
        title="A & B",
        url=url,
        validity="",
        name="<script>",
    )
    html = mail.to_html(text, url, "de")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;A &amp; B&lt;/b&gt;" in html
    assert html.count(f'href="{url}"') == 2  # Button und Ersatzlink
