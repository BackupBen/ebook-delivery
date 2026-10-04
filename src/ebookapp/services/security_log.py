"""Sicherheitsprotokoll und Benachrichtigungen per E-Mail.

Protokolliert werden Anmeldungen, Fehlversuche und sicherheitsrelevante Änderungen. Bei
eingeschalteter Benachrichtigung verschickt die App eine E-Mail über Brevo; der Versand
läuft in einem eigenen Thread und verzögert keine Anfrage.
"""

from __future__ import annotations

import html
import logging
import sqlite3
import threading
from datetime import timedelta
from typing import Any

from .. import i18n
from ..config import Settings
from ..db import connect, iso, now_iso, now_utc, parse_iso
from ..i18n import N_, _
from . import mail, misc

log = logging.getLogger("ebookapp.security")

EVENTS = {
    "login_success": N_("Anmeldung erfolgreich"),
    "login_failed": N_("Anmeldung fehlgeschlagen"),
    "login_blocked": N_("Anmeldung gesperrt (zu viele Versuche)"),
    "mfa_failed": N_("Falscher Bestätigungscode"),
    "recovery_code_used": N_("Notfall-Code verwendet"),
    "mfa_enabled": N_("Zwei-Faktor-Anmeldung eingeschaltet"),
    "mfa_disabled": N_("Zwei-Faktor-Anmeldung ausgeschaltet"),
    "recovery_codes_renewed": N_("Neue Notfall-Codes erstellt"),
    "password_changed": N_("Passwort geändert"),
    "logout": N_("Abgemeldet"),
    "api_key_created": N_("API-Schlüssel erstellt"),
    "api_key_revoked": N_("API-Schlüssel widerrufen"),
    "api_key_rejected": N_("Ungültiger API-Schlüssel verwendet"),
    "book_deleted": N_("Buch gelöscht"),
    "webhook_rejected": N_("Whop-Webhook abgelehnt"),
    "alerts_changed": N_("Benachrichtigungen geändert"),
}

# Ereignisse, die auf einen Angriff oder eine ungewollte Änderung hindeuten können.
WARNINGS = frozenset(
    {
        "login_failed",
        "login_blocked",
        "mfa_failed",
        "recovery_code_used",
        "mfa_disabled",
        "api_key_rejected",
        "webhook_rejected",
    }
)

# Benachrichtigung „bei jeder Anmeldung“
LOGIN_ALERTS = frozenset({"login_success"})
# Benachrichtigung „bei Warnzeichen“: Das Passwort war richtig, aber der Code falsch; zu
# viele Fehlversuche; sicherheitsrelevante Änderungen.
WARNING_ALERTS = frozenset(
    {
        "login_blocked",
        "mfa_failed",
        "recovery_code_used",
        "mfa_disabled",
        "password_changed",
        "recovery_codes_renewed",
        "api_key_created",
        "alerts_changed",
    }
)
# Höchstens eine Warn-Benachrichtigung je Ereignisart in diesem Abstand.
WARNING_ALERT_INTERVAL_MINUTES = 30
RETENTION_DAYS = 365
MAX_ROWS = 50_000


def record(
    conn: sqlite3.Connection,
    settings: Settings,
    event: str,
    *,
    username: str = "",
    ip: str = "",
    user_agent: str = "",
    detail: str = "",
) -> None:
    if event not in EVENTS:
        raise ValueError(f"Unbekanntes Ereignis: {event}")
    conn.execute(
        "INSERT INTO security_events (created_at, event, username, ip, user_agent, detail)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (now_iso(), event, username[:100], ip[:64], user_agent[:300], detail[:500]),
    )
    try:
        _maybe_alert(conn, settings, event, username=username, ip=ip, detail=detail)
    except Exception:  # Benachrichtigungen dürfen nie eine Anmeldung verhindern.
        log.exception("Benachrichtigung konnte nicht vorbereitet werden")


def label(event: str) -> str:
    return _(EVENTS.get(event, event))


def list_events(
    conn: sqlite3.Connection,
    *,
    event: str | None = None,
    warnings_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if event in EVENTS:
        where.append("event = ?")
        params.append(event)
    if warnings_only:
        where.append(f"event IN ({', '.join('?' for _w in WARNINGS)})")
        params.extend(sorted(WARNINGS))
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM security_events {clause}", params).fetchone()[0]  # noqa: S608
    rows = conn.execute(
        f"SELECT * FROM security_events {clause} ORDER BY id DESC LIMIT ? OFFSET ?",  # noqa: S608
        [*params, limit, offset],
    ).fetchall()
    items = [
        {
            **dict(row),
            "label": label(row["event"]),
            "warning": row["event"] in WARNINGS,
        }
        for row in rows
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def recent_warnings(conn: sqlite3.Connection, hours: int = 24) -> int:
    since = iso(now_utc() - timedelta(hours=hours))
    placeholders = ", ".join("?" for _w in WARNINGS)
    return conn.execute(
        f"SELECT COUNT(*) FROM security_events WHERE created_at >= ?"  # noqa: S608
        f" AND event IN ({placeholders})",
        [since, *sorted(WARNINGS)],
    ).fetchone()[0]


def purge(conn: sqlite3.Connection) -> None:
    cutoff = iso(now_utc() - timedelta(days=RETENTION_DAYS))
    conn.execute("DELETE FROM security_events WHERE created_at < ?", (cutoff,))
    conn.execute(
        "DELETE FROM security_events WHERE id <= (SELECT id FROM security_events"
        " ORDER BY id DESC LIMIT 1 OFFSET ?)",
        (MAX_ROWS,),
    )


# ---------------------------------------------------------------------------
# Benachrichtigungen
# ---------------------------------------------------------------------------


def alert_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "email": misc._get(conn, "alert_email") or "",
        "on_login": misc._get(conn, "alert_on_login") == "1",
        "on_warning": misc._get(conn, "alert_on_warning") == "1",
        "language": i18n.normalize(misc._get(conn, "alert_language")),
    }


def save_alert_settings(
    conn: sqlite3.Connection, *, email: str, on_login: bool, on_warning: bool, language: str
) -> None:
    misc._set(conn, "alert_email", email.strip().lower())
    misc._set(conn, "alert_on_login", "1" if on_login else "0")
    misc._set(conn, "alert_on_warning", "1" if on_warning else "0")
    misc._set(conn, "alert_language", i18n.normalize(language))


def _maybe_alert(
    conn: sqlite3.Connection,
    settings: Settings,
    event: str,
    *,
    username: str,
    ip: str,
    detail: str,
) -> None:
    config = alert_settings(conn)
    if not config["email"] or not settings.mail_configured:
        return
    wanted = (config["on_login"] and event in LOGIN_ALERTS) or (
        config["on_warning"] and event in WARNING_ALERTS
    )
    if not wanted:
        return
    if event in WARNING_ALERTS:
        key = f"alert_last_{event}"
        last = misc._get(conn, key)
        if last and parse_iso(last) > now_utc() - timedelta(minutes=WARNING_ALERT_INTERVAL_MINUTES):
            return
        misc._set(conn, key, now_iso())
    with i18n.language(config["language"]):
        email = _alert_email(settings, config["email"], event, username, ip, detail)
    threading.Thread(
        target=_send_alert, args=(settings, email), name="security-alert", daemon=True
    ).start()


def _alert_email(
    settings: Settings, to: str, event: str, username: str, ip: str, detail: str
) -> mail.Email:
    base = settings.public_base_url or ""
    title = label(event)
    when = (
        now_utc()
        .astimezone(settings.timezone)
        .strftime("%d.%m.%Y %H:%M %Z" if i18n.current() == "de" else "%Y-%m-%d %H:%M %Z")
    )
    lines = [
        _("Sicherheitshinweis von %(site)s", site=base or _("E-Book-Auslieferung")),
        "",
        f"{_('Ereignis')}: {title}",
        f"{_('Zeit')}: {when}",
        f"{_('Benutzer')}: {username or '–'}",
        f"{_('Adresse')}: {ip or '–'}",
    ]
    if detail:
        lines.append(f"{_('Details')}: {detail}")
    lines += [
        "",
        _(
            "Warst du das nicht? Ändere sofort dein Passwort, prüfe die Zwei-Faktor-Anmeldung"
            " und sieh dir das Sicherheitsprotokoll an:"
        ),
        f"{base}/admin/security",
    ]
    text = "\n".join(lines) + "\n"
    body = "".join(
        f'<p style="margin:0 0 6px;">{html.escape(line)}</p>' if line else "<br>" for line in lines
    )
    return mail.Email(
        to_email=to,
        to_name="",
        subject=f"[{_('Sicherheit')}] {title}",
        text=text,
        html=(
            '<!doctype html><html><body style="font-family:system-ui,Arial,sans-serif;'
            f'color:#1c1b19;font-size:15px;">{body}</body></html>'
        ),
    )


def _send_alert(settings: Settings, email: mail.Email) -> None:
    try:
        mail.send(settings, email, tags=["security"])
    except mail.MailError as exc:
        log.warning("Sicherheits-Benachrichtigung nicht versendet: %s", exc)


def send_test_alert(conn: sqlite3.Connection, settings: Settings) -> None:
    """Versendet sofort eine Test-Benachrichtigung (wirft MailError)."""
    config = alert_settings(conn)
    with i18n.language(config["language"]):
        email = _alert_email(
            settings, config["email"], "alerts_changed", "", "", _("Test-Benachrichtigung")
        )
    mail.send(settings, email, tags=["security"])


def record_from_thread(settings: Settings, event: str, **values: Any) -> None:
    """Für Aufrufer ohne eigene Datenbankverbindung."""
    conn = connect(settings.db_path)
    try:
        record(conn, settings, event, **values)
    finally:
        conn.close()
