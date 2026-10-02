"""Einstellungen, Idempotenz, Statistik und Aufräumarbeiten."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..config import MB, Settings
from ..db import iso, now_iso, now_utc, transaction
from ..errors import Conflict, Invalid, field_error
from ..schemas import SettingsUpdate

DEFAULT_MESSAGE_TEMPLATE = """Hallo,

vielen Dank für deinen Kauf! Hier ist dein persönlicher Downloadlink für „{titel}“:

{link}

{gueltigkeit}
Bitte bewahre den Link gut auf und gib ihn nicht weiter.

Viele Grüße"""

# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    pdf_mb: int
    epub_mb: int
    cover_mb: int
    hard_mb: int

    def bytes_for(self, kind: str) -> int:
        return {"pdf": self.pdf_mb, "epub": self.epub_mb, "cover": self.cover_mb}[kind] * MB


def _get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_limits(conn: sqlite3.Connection, settings: Settings) -> Limits:
    hard = settings.max_upload_bytes // MB

    def value(key: str, default: int) -> int:
        raw = _get(conn, key)
        try:
            number = int(raw) if raw is not None else default
        except ValueError:
            number = default
        return max(1, min(number, hard))

    return Limits(
        pdf_mb=value("max_pdf_mb", settings.default_max_pdf_mb),
        epub_mb=value("max_epub_mb", settings.default_max_epub_mb),
        cover_mb=value("max_cover_mb", settings.default_max_cover_mb),
        hard_mb=hard,
    )


def get_message_template(conn: sqlite3.Connection) -> str:
    return _get(conn, "message_template") or DEFAULT_MESSAGE_TEMPLATE


def update_settings(conn: sqlite3.Connection, settings: Settings, data: SettingsUpdate) -> None:
    hard = settings.max_upload_bytes // MB
    for name in ("max_pdf_mb", "max_epub_mb", "max_cover_mb"):
        if getattr(data, name) > hard:
            raise field_error(
                name,
                f"Höchstens {hard} MB möglich (Obergrenze MAX_UPLOAD_MB der Installation).",
            )
    if "{link}" not in data.message_template:
        raise field_error("message_template", "Die Vorlage muss den Platzhalter {link} enthalten.")
    with transaction(conn):
        _set(conn, "max_pdf_mb", str(data.max_pdf_mb))
        _set(conn, "max_epub_mb", str(data.max_epub_mb))
        _set(conn, "max_cover_mb", str(data.max_cover_mb))
        _set(conn, "message_template", data.message_template)


def render_message(template: str, *, title: str, url: str, validity: str) -> str:
    """Füllt die Versandnachricht. Bewusst ohne str.format, damit Vorlagen nichts auslösen."""
    text = template.replace("{titel}", title).replace("{link}", url)
    text = text.replace("{gueltigkeit}", validity)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


# ---------------------------------------------------------------------------
# Idempotenz
# ---------------------------------------------------------------------------

IDEMPOTENCY_KEY_RE = re.compile(r"^[\x21-\x7e]{16,200}$")
IDEMPOTENCY_TTL_HOURS = 24
IN_PROGRESS_TIMEOUT_MINUTES = 15


@dataclass(frozen=True)
class Replay:
    status_code: int
    body: Any
    wrapped_secret: str | None


def request_hash(*parts: Any) -> str:
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _key_hash(scope: str, key: str) -> str:
    return hashlib.sha256(f"{scope}\0{key}".encode()).hexdigest()


def idempotency_begin(
    conn: sqlite3.Connection, scope: str, key: str, fingerprint: str
) -> Replay | None:
    """Reserviert einen Idempotency-Key oder liefert die gespeicherte Antwort.

    Der Schlüssel wird nur gehasht gespeichert. Gleicher Schlüssel mit anderem Inhalt
    ist ein Fehler; eine noch laufende Anfrage mit demselben Schlüssel ebenfalls.
    """
    if not IDEMPOTENCY_KEY_RE.match(key):
        raise Invalid(
            "Der Idempotency-Key muss aus 16 bis 200 druckbaren ASCII-Zeichen bestehen "
            "(empfohlen: eine zufällige UUID).",
            code="idempotency_key_invalid",
        )
    hashed = _key_hash(scope, key)
    now = now_utc()
    with transaction(conn):
        conn.execute(
            "DELETE FROM idempotency_keys WHERE created_at < ?",
            (iso(now - timedelta(hours=IDEMPOTENCY_TTL_HOURS)),),
        )
        row = conn.execute(
            "SELECT * FROM idempotency_keys WHERE scope = ? AND key_hash = ?", (scope, hashed)
        ).fetchone()
        if row is not None:
            if row["request_hash"] != fingerprint:
                raise Invalid(
                    "Dieser Idempotency-Key wurde bereits für eine andere Anfrage verwendet.",
                    code="idempotency_key_reused",
                )
            if row["state"] == "done":
                return Replay(
                    status_code=row["status_code"],
                    body=json.loads(row["response_json"]),
                    wrapped_secret=row["wrapped_secret"],
                )
            stale = iso(now - timedelta(minutes=IN_PROGRESS_TIMEOUT_MINUTES))
            if row["created_at"] >= stale:
                raise Conflict(
                    "Eine Anfrage mit diesem Idempotency-Key wird gerade verarbeitet.",
                    code="idempotency_in_progress",
                    headers={"Retry-After": "5"},
                )
            conn.execute(
                "DELETE FROM idempotency_keys WHERE scope = ? AND key_hash = ?", (scope, hashed)
            )
        conn.execute(
            "INSERT INTO idempotency_keys (scope, key_hash, request_hash, state, created_at)"
            " VALUES (?, ?, ?, 'in_progress', ?)",
            (scope, hashed, fingerprint, iso(now)),
        )
    return None


def idempotency_finish(
    conn: sqlite3.Connection,
    scope: str,
    key: str,
    status_code: int,
    body: Any,
    wrapped_secret: str | None = None,
) -> None:
    conn.execute(
        "UPDATE idempotency_keys SET state = 'done', status_code = ?, response_json = ?,"
        " wrapped_secret = ? WHERE scope = ? AND key_hash = ?",
        (
            status_code,
            json.dumps(body, ensure_ascii=False),
            wrapped_secret,
            scope,
            _key_hash(scope, key),
        ),
    )


def idempotency_abort(conn: sqlite3.Connection, scope: str, key: str) -> None:
    """Gibt den Schlüssel nach einem Fehler wieder frei, damit ein neuer Versuch möglich ist."""
    conn.execute(
        "DELETE FROM idempotency_keys WHERE scope = ? AND key_hash = ? AND state = 'in_progress'",
        (scope, _key_hash(scope, key)),
    )


# ---------------------------------------------------------------------------
# Statistik
# ---------------------------------------------------------------------------


def download_stats(
    conn: sqlite3.Connection,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    book_id: str | None = None,
    link_id: str | None = None,
) -> dict[str, Any]:
    """Zählwerte der gezählten Downloads. Enthält keine Codes und keine Client-Daten."""
    today = now_utc().date()
    date_to = date_to or today
    date_from = date_from or (date_to - timedelta(days=29))
    if date_from > date_to:
        raise field_error("date_from", "Das Startdatum liegt nach dem Enddatum.")
    if (date_to - date_from).days > 731:
        raise field_error("date_from", "Der Zeitraum darf höchstens zwei Jahre umfassen.")

    where = ["e.created_at >= :start", "e.created_at < :end"]
    params: dict[str, Any] = {
        "start": f"{date_from.isoformat()}T00:00:00Z",
        "end": f"{(date_to + timedelta(days=1)).isoformat()}T00:00:00Z",
    }
    if book_id:
        where.append("e.book_id = :book_id")
        params["book_id"] = book_id
    if link_id:
        where.append("e.link_id = :link_id")
        params["link_id"] = link_id
    clause = " AND ".join(where)

    by_day = conn.execute(
        f"SELECT substr(e.created_at, 1, 10) AS day, COUNT(*) AS n FROM download_events e"  # noqa: S608
        f" WHERE {clause} GROUP BY day ORDER BY day",
        params,
    ).fetchall()
    by_format = conn.execute(
        f"SELECT e.format, COUNT(*) AS n FROM download_events e WHERE {clause}"  # noqa: S608
        " GROUP BY e.format ORDER BY e.format DESC",
        params,
    ).fetchall()
    by_book = conn.execute(
        f"SELECT e.book_id, b.title, COUNT(*) AS n FROM download_events e"  # noqa: S608
        f" JOIN books b ON b.id = e.book_id WHERE {clause}"
        " GROUP BY e.book_id ORDER BY n DESC, b.title LIMIT 200",
        params,
    ).fetchall()
    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "total": sum(row["n"] for row in by_day),
        "by_format": [{"format": row["format"], "downloads": row["n"]} for row in by_format],
        "by_day": [{"date": row["day"], "downloads": row["n"]} for row in by_day],
        "by_book": [
            {"book_id": row["book_id"], "title": row["title"], "downloads": row["n"]}
            for row in by_book
        ],
    }


def housekeeping(conn: sqlite3.Connection, settings: Settings) -> None:
    """Entfernt abgelaufene Sitzungen, Idempotenz-Einträge und Download-Fingerabdrücke."""
    now = now_utc()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
    conn.execute(
        "DELETE FROM idempotency_keys WHERE created_at < ?",
        (iso(now - timedelta(hours=IDEMPOTENCY_TTL_HOURS)),),
    )
    conn.execute(
        "DELETE FROM download_grants WHERE last_seen_at < ? OR first_seen_at < ?",
        (
            iso(now - timedelta(minutes=settings.download_window_minutes)),
            iso(now - timedelta(hours=settings.download_window_max_hours)),
        ),
    )
