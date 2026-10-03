"""Administrator, Sitzungen und API-Schlüssel."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta
from typing import Any

from ..config import Settings
from ..db import iso, now_iso, now_utc, parse_iso, transaction
from ..errors import NotFound, field_error
from ..i18n import _
from ..schemas import SCOPES, ApiKeyCreate, PasswordChange
from ..security import (
    API_KEY_RE,
    DUMMY_HASH,
    MIN_PASSWORD_LENGTH,
    hash_password,
    needs_rehash,
    new_api_key,
    new_id,
    new_token,
    sha256_hex,
    verify_password,
)

log = logging.getLogger("ebookapp.auth")


# ---------------------------------------------------------------------------
# Administrator
# ---------------------------------------------------------------------------


def ensure_admin(conn: sqlite3.Connection, settings: Settings) -> str:
    """Legt beim ersten Start den Administrator an.

    ``ADMIN_PASSWORD`` wird nur verwendet, solange es noch keinen Administrator gibt.
    Spätere Passwortänderungen in der Oberfläche werden bei Redeploys nicht überschrieben.
    """
    if conn.execute("SELECT 1 FROM users").fetchone():
        return "exists"
    password = settings.admin_password
    if not password:
        log.warning(
            "Es gibt noch keinen Administrator. ADMIN_PASSWORD setzen und neu starten oder "
            "im Container `ebookctl set-password` ausführen."
        )
        return "missing"
    if len(password) < MIN_PASSWORD_LENGTH:
        log.error(
            "ADMIN_PASSWORD ist zu kurz (mindestens %d Zeichen). Es wurde kein Administrator "
            "angelegt.",
            MIN_PASSWORD_LENGTH,
        )
        return "too_short"
    set_password(conn, settings.admin_username, password)
    log.info("Administrator „%s“ wurde angelegt.", settings.admin_username)
    return "created"


def set_password(conn: sqlite3.Connection, username: str, password: str) -> None:
    """Setzt das Passwort (legt den Benutzer bei Bedarf an) und beendet alle Sitzungen."""
    now = now_iso()
    with transaction(conn):
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (username) DO UPDATE SET password_hash = excluded.password_hash,"
            " updated_at = excluded.updated_at",
            (username, hash_password(password), now, now),
        )
        conn.execute(
            "DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = ?)",
            (username,),
        )


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if row is None:
        verify_password(password, DUMMY_HASH)
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    if needs_rehash(row["password_hash"]):
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (hash_password(password), now_iso(), row["id"]),
        )
    return row


def change_password(
    conn: sqlite3.Connection, user_id: int, data: PasswordChange, keep_token: str
) -> None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or not verify_password(data.current_password, row["password_hash"]):
        raise field_error("current_password", _("Das aktuelle Passwort ist nicht korrekt."))
    if data.new_password != data.new_password_repeat:
        raise field_error(
            "new_password_repeat", _("Die beiden neuen Passwörter stimmen nicht überein.")
        )
    with transaction(conn):
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (hash_password(data.new_password), now_iso(), user_id),
        )
        # Alle anderen Sitzungen beenden.
        conn.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
            (user_id, sha256_hex(keep_token)),
        )


# ---------------------------------------------------------------------------
# Sitzungen
# ---------------------------------------------------------------------------


def create_session(conn: sqlite3.Connection, settings: Settings, user_id: int) -> str:
    token = new_token()
    now = now_utc()
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, last_seen_at,"
        " expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            sha256_hex(token),
            user_id,
            new_token(),
            iso(now),
            iso(now),
            iso(now + timedelta(hours=settings.session_max_hours)),
        ),
    )
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (iso(now),))
    return token


def get_session(conn: sqlite3.Connection, settings: Settings, token: str) -> dict[str, Any] | None:
    if not token or len(token) > 128:
        return None
    row = conn.execute(
        "SELECT s.*, u.username FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token_hash = ?",
        (sha256_hex(token),),
    ).fetchone()
    if row is None:
        return None
    now = now_utc()
    idle_limit = parse_iso(row["last_seen_at"]) + timedelta(minutes=settings.session_idle_minutes)
    if parse_iso(row["expires_at"]) <= now or idle_limit <= now:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
        return None
    if now - parse_iso(row["last_seen_at"]) > timedelta(seconds=60):
        conn.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
            (iso(now), row["token_hash"]),
        )
    return {
        "token": token,
        "token_hash": row["token_hash"],
        "user_id": row["user_id"],
        "username": row["username"],
        "csrf_token": row["csrf_token"],
    }


def destroy_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (sha256_hex(token),))


def add_flash(conn: sqlite3.Connection, token_hash: str, kind: str, message: str) -> None:
    row = conn.execute("SELECT flash FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()
    if row is None:
        return
    items = json.loads(row["flash"]) if row["flash"] else []
    items.append({"kind": kind, "message": message})
    conn.execute(
        "UPDATE sessions SET flash = ? WHERE token_hash = ?",
        (json.dumps(items[-10:], ensure_ascii=False), token_hash),
    )


def pop_flash(conn: sqlite3.Connection, token_hash: str) -> list[dict[str, str]]:
    row = conn.execute("SELECT flash FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()
    if row is None or not row["flash"]:
        return []
    conn.execute("UPDATE sessions SET flash = NULL WHERE token_hash = ?", (token_hash,))
    return json.loads(row["flash"])


# ---------------------------------------------------------------------------
# API-Schlüssel
# ---------------------------------------------------------------------------


def _key_out(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "key_prefix": row["key_prefix"],
        "scopes": [scope for scope in SCOPES if scope in row["scopes"].split(",")],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "revoked_at": row["revoked_at"],
    }


def create_api_key(conn: sqlite3.Connection, data: ApiKeyCreate) -> tuple[dict[str, Any], str]:
    """Erstellt einen Schlüssel. Der vollständige Wert wird nur hier einmalig geliefert."""
    key, prefix = new_api_key()
    key_id = new_id("key")
    conn.execute(
        "INSERT INTO api_keys (id, name, key_prefix, key_hash, scopes, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (key_id, data.name, prefix, sha256_hex(key), ",".join(data.scopes), now_iso()),
    )
    row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    return _key_out(row), key


def list_api_keys(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
    return [_key_out(row) for row in rows]


def revoke_api_key(conn: sqlite3.Connection, key_id: str) -> None:
    cursor = conn.execute(
        "UPDATE api_keys SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?",
        (now_iso(), key_id),
    )
    if cursor.rowcount == 0:
        raise NotFound(_("Diesen API-Schlüssel gibt es nicht."), code="api_key_not_found")


def authenticate_api_key(conn: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    if not API_KEY_RE.match(token):
        return None
    row = conn.execute(
        "SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL", (sha256_hex(token),)
    ).fetchone()
    if row is None:
        return None
    now = now_utc()
    last = row["last_used_at"]
    if last is None or now - parse_iso(last) > timedelta(seconds=60):
        conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (iso(now), row["id"]))
    return _key_out(row)
