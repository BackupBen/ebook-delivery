"""Bestellungen aus Whop: Webhook prüfen, Link erzeugen, E-Mail versenden.

Ablauf: Whop meldet ``payment.succeeded``. Die App sucht das Buch mit der Whop-Produkt-ID,
erzeugt einen dauerhaften Downloadlink und stellt die E-Mail in eine Warteschlange. Ein
Hintergrund-Thread versendet sie über Brevo und wiederholt fehlgeschlagene Versuche.

Der Link-Code wird – verschlüsselt mit dem SECRET_KEY – nur so lange gespeichert, bis die
E-Mail versendet ist. Danach existiert er wie bei jedem anderen Link nur noch beim Käufer.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..config import Settings
from ..db import connect, iso, now_iso, now_utc, transaction
from ..errors import AppError, Conflict, NotFound
from ..schemas import LinkCreate
from ..security import new_id, unwrap_code, wrap_code
from . import links as link_service
from . import mail, misc

log = logging.getLogger("ebookapp.orders")

SOURCE_WHOP = "whop"
SIGNATURE_TOLERANCE_SECONDS = 300
# Wartezeit nach dem 1., 2., … fehlgeschlagenen Versuch. Danach gilt der Versand als
# fehlgeschlagen und wird in der Verwaltung angezeigt.
RETRY_MINUTES = (1, 5, 15, 60, 180, 360, 720)
CLAIM_MINUTES = 10
WRAP_SCOPE = "order-mail"

STATUS_LABELS = {
    "pending": "Wird versendet",
    "sent": "Versendet",
    "failed": "Versand fehlgeschlagen",
    "unmatched": "Kein passendes Buch",
    "no_email": "Keine E-Mail-Adresse",
}


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


class WebhookRejected(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def verify_whop_signature(
    secret: str, headers: Mapping[str, str], body: bytes, now: float | None = None
) -> str:
    """Prüft die Signatur nach „Standard Webhooks“ und liefert die webhook-id.

    Whop signiert ``{webhook-id}.{webhook-timestamp}.{Rohdaten}`` mit HMAC-SHA256. Der
    Schlüssel sind die Bytes des Geheimnisses genau so, wie Whop es ausgibt (``ws_…``).
    """
    webhook_id = headers.get("webhook-id", "")
    timestamp = headers.get("webhook-timestamp", "")
    signatures = headers.get("webhook-signature", "")
    if not webhook_id or not timestamp or not signatures or len(webhook_id) > 200:
        raise WebhookRejected(400, "Signatur-Kopfzeilen fehlen.")
    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise WebhookRejected(400, "Ungültiger Zeitstempel.") from exc
    current = time.time() if now is None else now
    if abs(current - sent_at) > SIGNATURE_TOLERANCE_SECONDS:
        raise WebhookRejected(401, "Zeitstempel außerhalb des erlaubten Fensters.")
    signed = f"{webhook_id}.{timestamp}.".encode() + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    for candidate in signatures.split():
        version, _, value = candidate.partition(",")
        if version == "v1" and hmac.compare_digest(value.encode(), expected.encode()):
            return webhook_id
    raise WebhookRejected(401, "Signatur ungültig.")


@dataclass(frozen=True)
class Payment:
    payment_id: str
    product_id: str
    product_title: str
    email: str
    name: str


def _text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def parse_payment(data: Any) -> Payment:
    """Liest die benötigten Felder. Unterstützt das ältere (``product.id``, ``user.email``)
    und das aktuelle Format (``product_id``, ``customer_email``) der Whop-API."""
    data = data if isinstance(data, dict) else {}
    product = data.get("product") if isinstance(data.get("product"), dict) else {}
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    return Payment(
        payment_id=_text(data.get("id"), 100),
        product_id=_text(data.get("product_id"), 100) or _text(product.get("id"), 100),
        product_title=_text(product.get("title"), 200),
        email=(_text(user.get("email"), 254) or _text(data.get("customer_email"), 254)).lower(),
        name=_text(user.get("name"), 100),
    )


def handle_whop_webhook(
    conn: sqlite3.Connection, settings: Settings, headers: Mapping[str, str], body: bytes
) -> str:
    """Verarbeitet eine Zustellung von Whop und liefert ein kurzes Ergebnis fürs Log."""
    if not settings.whop_webhook_secret:
        # 503: Whop wiederholt die Zustellung, bis das Geheimnis eingetragen ist.
        raise WebhookRejected(503, "WHOP_WEBHOOK_SECRET ist nicht gesetzt.")
    webhook_id = verify_whop_signature(settings.whop_webhook_secret, headers, body)
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise WebhookRejected(400, "Kein gültiges JSON.") from exc
    if not isinstance(payload, dict):
        raise WebhookRejected(400, "Unerwartetes Format.")
    event_type = _text(payload.get("type"), 100)

    seen = conn.execute(
        "SELECT 1 FROM webhook_events WHERE id = ? AND source = ?", (webhook_id, SOURCE_WHOP)
    ).fetchone()
    if seen:
        return "duplicate"

    result = "ignored"
    if event_type == "payment.succeeded":
        payment = parse_payment(payload.get("data"))
        if payment.payment_id:
            result = record_payment(conn, settings, payment)
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO webhook_events (id, source, type, received_at)"
            " VALUES (?, ?, ?, ?)",
            (webhook_id, SOURCE_WHOP, event_type or "?", now_iso()),
        )
    log.info(
        "Whop-Webhook %s: %s (%s)",
        event_type or "?",
        result,
        payment.payment_id if event_type == "payment.succeeded" else "-",
    )
    return result


# ---------------------------------------------------------------------------
# Bestellungen
# ---------------------------------------------------------------------------


def record_payment(conn: sqlite3.Connection, settings: Settings, payment: Payment) -> str:
    """Legt die Bestellung an (einmal je Zahlung) und erzeugt den Link."""
    now = now_iso()
    with transaction(conn):
        existing = conn.execute(
            "SELECT id, status FROM orders WHERE source = ? AND payment_id = ?",
            (SOURCE_WHOP, payment.payment_id),
        ).fetchone()
        if existing:
            return f"duplicate:{existing['status']}"
        order_id = new_id("ord")
        conn.execute(
            "INSERT INTO orders (id, source, payment_id, product_id, product_title, email, name,"
            " status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                order_id,
                SOURCE_WHOP,
                payment.payment_id,
                payment.product_id,
                payment.product_title,
                payment.email,
                payment.name,
                now,
                now,
            ),
        )
    return attach_link(conn, settings, order_id)


def _matching_book(conn: sqlite3.Connection, product_id: str) -> tuple[sqlite3.Row | None, str]:
    if not product_id:
        return None, "Die Zahlung enthält keine Produkt-ID."
    rows = conn.execute(
        "SELECT * FROM books WHERE whop_product_id = ? ORDER BY status = 'active' DESC, created_at",
        (product_id,),
    ).fetchall()
    if not rows:
        return None, f"Kein Buch mit der Whop-Produkt-ID {product_id}."
    book = rows[0]
    if book["status"] != "active":
        return None, f"Das Buch „{book['title']}“ ist archiviert."
    if not book["current_edition_id"]:
        return None, f"Das Buch „{book['title']}“ hat noch keine veröffentlichte Ausgabe."
    return book, ""


def attach_link(conn: sqlite3.Connection, settings: Settings, order_id: str) -> str:
    """Erzeugt den Link einer Bestellung und plant den Versand."""
    order = _order_row(conn, order_id)
    if not order["email"]:
        _update(conn, order_id, status="no_email", detail="Die Zahlung enthält keine E-Mail.")
        return "no_email"
    book, problem = _matching_book(conn, order["product_id"])
    if book is None:
        _update(conn, order_id, status="unmatched", detail=problem, book_id=None)
        return "unmatched"
    try:
        link, code = link_service.create_link(
            conn,
            LinkCreate(book_id=book["id"], label=f"Whop {order['payment_id']}"[:200]),
        )
    except AppError as exc:
        _update(conn, order_id, status="unmatched", detail=exc.message, book_id=book["id"])
        return "unmatched"
    _update(
        conn,
        order_id,
        status="pending",
        detail="",
        book_id=book["id"],
        link_id=link["id"],
        wrapped_code=wrap_code(settings.secret_key, WRAP_SCOPE, order_id, link["id"], code),
        attempts=0,
        next_attempt_at=now_iso(),
    )
    return "pending"


_UPDATABLE = {
    "status",
    "detail",
    "book_id",
    "link_id",
    "wrapped_code",
    "attempts",
    "next_attempt_at",
    "sent_at",
    "message_id",
    "email",
}


def _update(conn: sqlite3.Connection, order_id: str, **changes: Any) -> None:
    unknown = set(changes) - _UPDATABLE
    if unknown:
        raise ValueError(f"Unbekannte Spalten: {unknown}")
    assignments = ", ".join(f"{column} = ?" for column in changes)
    with transaction(conn):
        conn.execute(
            f"UPDATE orders SET {assignments}, updated_at = ? WHERE id = ?",  # noqa: S608
            [*changes.values(), now_iso(), order_id],
        )


def _order_row(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise NotFound("Diese Bestellung gibt es nicht.", code="order_not_found")
    return row


def order_out(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    book = None
    if row["book_id"]:
        book = conn.execute(
            "SELECT id, title, language FROM books WHERE id = ?", (row["book_id"],)
        ).fetchone()
    link = None
    if row["link_id"]:
        with contextlib.suppress(NotFound):
            link = link_service.get_link(conn, row["link_id"])
    return {
        "id": row["id"],
        "source": row["source"],
        "payment_id": row["payment_id"],
        "product_id": row["product_id"],
        "product_title": row["product_title"],
        "email": row["email"],
        "name": row["name"],
        "status": row["status"],
        "status_label": STATUS_LABELS.get(row["status"], row["status"]),
        "detail": row["detail"],
        "attempts": row["attempts"],
        "next_attempt_at": row["next_attempt_at"],
        "sent_at": row["sent_at"],
        "book": dict(book) if book else None,
        "link": link,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_order(conn: sqlite3.Connection, order_id: str) -> dict[str, Any]:
    return order_out(conn, _order_row(conn, order_id))


def list_orders(
    conn: sqlite3.Connection,
    *,
    q: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if status in STATUS_LABELS:
        where.append("status = ?")
        params.append(status)
    if q and q.strip():
        term = q.strip().lower()
        where.append("(email = ? OR payment_id = ? OR product_id = ? OR id = ?)")
        params.extend([term, q.strip(), q.strip(), q.strip()])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM orders {clause}", params).fetchone()[0]  # noqa: S608
    rows = conn.execute(
        f"SELECT * FROM orders {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",  # noqa: S608
        [*params, limit, offset],
    ).fetchall()
    return {
        "items": [order_out(conn, row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM orders GROUP BY status").fetchall()
    return {row["status"]: row["n"] for row in rows}


def retry(conn: sqlite3.Connection, settings: Settings, order_id: str) -> str:
    """Versucht eine Bestellung erneut: ohne Link wird er jetzt erzeugt, sonst neu versendet."""
    order = _order_row(conn, order_id)
    if order["status"] == "sent":
        raise Conflict(
            "Die E-Mail wurde bereits versendet. Verwende „Neuen Link senden“.",
            code="order_already_sent",
        )
    if order["status"] in ("unmatched", "no_email") or not order["link_id"]:
        return attach_link(conn, settings, order_id)
    if not order["wrapped_code"]:
        return resend_new_link(conn, settings, order_id)
    _update(conn, order_id, status="pending", detail="", attempts=0, next_attempt_at=now_iso())
    return "pending"


def resend_new_link(conn: sqlite3.Connection, settings: Settings, order_id: str) -> str:
    """Widerruft den bisherigen Link und versendet einen neuen."""
    order = _order_row(conn, order_id)
    if order["link_id"]:
        with contextlib.suppress(NotFound):
            link_service.revoke_link(conn, order["link_id"])
    _update(conn, order_id, link_id=None, wrapped_code=None)
    return attach_link(conn, settings, order_id)


def update_email(conn: sqlite3.Connection, order_id: str, email: str) -> None:
    _order_row(conn, order_id)
    _update(conn, order_id, email=email.strip().lower())


# ---------------------------------------------------------------------------
# Versand
# ---------------------------------------------------------------------------


def build_email(
    conn: sqlite3.Connection, settings: Settings, order: sqlite3.Row, url: str
) -> mail.Email:
    book = conn.execute("SELECT * FROM books WHERE id = ?", (order["book_id"],)).fetchone()
    link = link_service.get_link(conn, order["link_id"])
    language = book["language"] if book else "de"
    title = book["title"] if book else order["product_title"]
    validity = misc.validity_text(link, language, settings.timezone)
    text = mail.fill(
        misc.get_message_template(conn, language),
        title=title,
        url=url,
        validity=validity,
        name=order["name"],
    )
    subject = mail.subject_line(
        misc.get_mail_subject(conn, language), title=title, name=order["name"]
    )
    return mail.Email(
        to_email=order["email"],
        to_name=order["name"],
        subject=subject,
        text=text,
        html=mail.to_html(text, url, language),
    )


def public_base(settings: Settings) -> str:
    return settings.public_base_url or f"http://127.0.0.1:{settings.port}"


def _claim(conn: sqlite3.Connection, order_id: str, now: str) -> bool:
    """Reserviert eine fällige Bestellung, damit sie nie doppelt versendet wird."""
    until = iso(now_utc() + timedelta(minutes=CLAIM_MINUTES))
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE orders SET next_attempt_at = ? WHERE id = ? AND status = 'pending'"
            " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
            (until, order_id, now),
        )
    return cursor.rowcount == 1


def deliver_due(conn: sqlite3.Connection, settings: Settings, limit: int = 20) -> int:
    """Versendet fällige E-Mails. Liefert die Zahl der erfolgreich versendeten."""
    if not settings.mail_configured:
        return 0
    now = now_iso()
    due = conn.execute(
        "SELECT id FROM orders WHERE status = 'pending'"
        " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
        " ORDER BY created_at LIMIT ?",
        (now, limit),
    ).fetchall()
    sent = 0
    for item in due:
        if _claim(conn, item["id"], now) and _deliver_one(conn, settings, item["id"]):
            sent += 1
    return sent


def _deliver_one(conn: sqlite3.Connection, settings: Settings, order_id: str) -> bool:
    order = _order_row(conn, order_id)
    if not order["link_id"] or not order["wrapped_code"]:
        # Der Link wurde z. B. nach einem Absturz nicht vollständig angelegt.
        attach_link(conn, settings, order_id)
        order = _order_row(conn, order_id)
        if order["status"] != "pending" or not order["wrapped_code"]:
            return False
    try:
        link = link_service.get_link(conn, order["link_id"])
    except NotFound:
        _update(conn, order_id, status="failed", detail="Der Link wurde gelöscht.")
        return False
    if link["status"] == "revoked":
        _update(conn, order_id, status="failed", detail="Der Link wurde widerrufen.")
        return False
    code = unwrap_code(
        settings.secret_key, WRAP_SCOPE, order_id, order["link_id"], order["wrapped_code"]
    )
    url = link_service.build_url(public_base(settings), code)
    attempts = order["attempts"] + 1
    try:
        message_id = mail.send(
            settings, build_email(conn, settings, order, url), tags=["whop-ebook"]
        )
    except mail.MailError as exc:
        if exc.permanent or attempts > len(RETRY_MINUTES):
            _update(
                conn,
                order_id,
                status="failed",
                detail=str(exc)[:500],
                attempts=attempts,
                next_attempt_at=None,
            )
            log.warning("E-Mail zu Bestellung %s fehlgeschlagen: %s", order_id, exc)
        else:
            wait = RETRY_MINUTES[attempts - 1]
            _update(
                conn,
                order_id,
                detail=str(exc)[:500],
                attempts=attempts,
                next_attempt_at=iso(now_utc() + timedelta(minutes=wait)),
            )
            log.info("E-Mail zu Bestellung %s: neuer Versuch in %d min", order_id, wait)
        return False
    _update(
        conn,
        order_id,
        status="sent",
        detail="",
        attempts=attempts,
        sent_at=now_iso(),
        message_id=message_id,
        wrapped_code=None,
        next_attempt_at=None,
    )
    log.info("E-Mail zu Bestellung %s versendet", order_id)
    return True


class OrderMailer:
    """Hintergrund-Thread für den Versand. ``wake()`` startet einen Durchlauf sofort."""

    INTERVAL_SECONDS = 30

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="mailer", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=TIMEOUT_JOIN)
            self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.INTERVAL_SECONDS)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.tick()
            except Exception:
                log.exception("Fehler beim E-Mail-Versand")

    def tick(self) -> int:
        if self.settings.maintenance_flag.exists():
            return 0
        with self._lock:
            conn = connect(self.settings.db_path)
            try:
                return deliver_due(conn, self.settings)
            finally:
                conn.close()


TIMEOUT_JOIN = mail.TIMEOUT_SECONDS + 5
