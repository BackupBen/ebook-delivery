"""Zwei-Faktor-Anmeldung mit zeitbasierten Einmalcodes (TOTP, RFC 6238) und Notfall-Codes.

Kompatibel mit gängigen Authenticator-Apps (Google Authenticator, Microsoft Authenticator,
1Password, Bitwarden, Aegis, …): SHA-1, 6 Ziffern, 30 Sekunden.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
import struct
import time
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlsplit

from ..config import Settings
from ..db import iso, now_iso, now_utc, parse_iso, transaction
from ..errors import Conflict, field_error
from ..i18n import _

STEP_SECONDS = 30
DIGITS = 6
# Toleranz für abweichende Uhren: ein Zeitschritt davor und danach.
WINDOW = 1
RECOVERY_CODE_COUNT = 10
RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # ohne 0/O, 1/I
CHALLENGE_MINUTES = 5
CHALLENGE_MAX_ATTEMPTS = 5


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------


def _code(secret: bytes, counter: int) -> str:
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_SECONDS)


def code_at(secret: bytes, step: int) -> str:
    return _code(secret, step)


def b32(secret: bytes) -> str:
    return base64.b32encode(secret).decode("ascii").rstrip("=")


def _matching_step(secret: bytes, code: str, after_step: int, now: float | None) -> int | None:
    """Liefert den Zeitschritt, zu dem der Code passt, oder None. Jeder Schritt gilt nur
    einmal (``after_step``), damit ein abgefangener Code nicht wiederverwendet werden kann."""
    step = current_step(now)
    for candidate in range(step - WINDOW, step + WINDOW + 1):
        if candidate > after_step and hmac.compare_digest(_code(secret, candidate), code):
            return candidate
    return None


def _normalize(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


# Verschlüsselung des Geheimnisses mit dem SECRET_KEY (Schlüsselstrom aus HMAC-SHA256).
# Ein Datenbank-Backup allein genügt damit nicht, um Codes zu erzeugen.
def _pad(settings: Settings, user_id: int, purpose: str) -> bytes:
    message = f"totp-v1\0{purpose}\0{user_id}".encode()
    return hmac.new(settings.secret_key, message, hashlib.sha256).digest()


def _wrap(settings: Settings, user_id: int, purpose: str, secret: bytes) -> str:
    pad = _pad(settings, user_id, purpose)
    mac = hmac.new(pad, secret, hashlib.sha256).digest()[:8]
    return base64.b64encode(bytes(a ^ b for a, b in zip(secret, pad, strict=False)) + mac).decode()


def _unwrap(settings: Settings, user_id: int, purpose: str, wrapped: str) -> bytes | None:
    raw = base64.b64decode(wrapped)
    body, mac = raw[:-8], raw[-8:]
    pad = _pad(settings, user_id, purpose)
    secret = bytes(a ^ b for a, b in zip(body, pad, strict=False))
    expected = hmac.new(pad, secret, hashlib.sha256).digest()[:8]
    # Passt die Prüfsumme nicht, wurde der SECRET_KEY geändert.
    return secret if hmac.compare_digest(mac, expected) else None


# ---------------------------------------------------------------------------
# Einrichtung
# ---------------------------------------------------------------------------


def _user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise Conflict(_("Diesen Benutzer gibt es nicht."), code="user_not_found")
    return row


def is_enabled(conn: sqlite3.Connection, user_id: int) -> bool:
    row = conn.execute("SELECT totp_secret FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row["totp_secret"])


def status(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    row = _user(conn, user_id)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM recovery_codes WHERE user_id = ? AND used_at IS NULL", (user_id,)
    ).fetchone()[0]
    return {
        "enabled": bool(row["totp_secret"]),
        "enabled_at": row["totp_enabled_at"],
        "recovery_remaining": remaining,
    }


def begin_setup(conn: sqlite3.Connection, settings: Settings, user_id: int) -> str:
    """Erzeugt ein neues Geheimnis für die Einrichtung und liefert es in Base32."""
    secret = secrets.token_bytes(20)
    with transaction(conn):
        _user(conn, user_id)
        conn.execute(
            "UPDATE users SET totp_pending = ? WHERE id = ?",
            (_wrap(settings, user_id, "pending", secret), user_id),
        )
    return b32(secret)


def pending_secret(conn: sqlite3.Connection, settings: Settings, user_id: int) -> str | None:
    row = _user(conn, user_id)
    if not row["totp_pending"]:
        return None
    secret = _unwrap(settings, user_id, "pending", row["totp_pending"])
    return b32(secret) if secret else None


def provisioning_uri(settings: Settings, username: str, secret_b32: str) -> str:
    host = urlsplit(settings.public_base_url or "").hostname or "localhost"
    issuer = f"E-Book Delivery ({host})"
    label = quote(f"{issuer}:{username}")
    return (
        f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )


def qr_svg(uri: str) -> str:
    import segno

    return segno.make(uri, error="m").svg_inline(
        scale=5, border=2, dark="#000", light="#fff", omitsize=True
    )


def enable(
    conn: sqlite3.Connection, settings: Settings, user_id: int, code: str, now: float | None = None
) -> list[str]:
    """Schließt die Einrichtung ab, wenn der Code passt, und liefert die Notfall-Codes."""
    row = _user(conn, user_id)
    secret = (
        _unwrap(settings, user_id, "pending", row["totp_pending"]) if row["totp_pending"] else None
    )
    if secret is None:
        raise Conflict(
            _("Die Einrichtung ist abgelaufen. Bitte neu beginnen."), code="mfa_setup_missing"
        )
    step = _matching_step(secret, _normalize(code), 0, now)
    if step is None:
        raise field_error(
            "code", _("Der Code passt nicht. Prüfe die Uhrzeit des Geräts und versuche es erneut.")
        )
    with transaction(conn):
        conn.execute(
            "UPDATE users SET totp_secret = ?, totp_pending = NULL, totp_enabled_at = ?,"
            " totp_last_step = ? WHERE id = ?",
            (_wrap(settings, user_id, "active", secret), now_iso(), step, user_id),
        )
    return new_recovery_codes(conn, user_id)


def disable(conn: sqlite3.Connection, user_id: int) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE users SET totp_secret = NULL, totp_pending = NULL, totp_enabled_at = NULL,"
            " totp_last_step = 0 WHERE id = ?",
            (user_id,),
        )
        conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM login_challenges WHERE user_id = ?", (user_id,))


def new_recovery_codes(conn: sqlite3.Connection, user_id: int) -> list[str]:
    codes = [
        "".join(secrets.choice(RECOVERY_ALPHABET) for _i in range(10))
        for _n in range(RECOVERY_CODE_COUNT)
    ]
    with transaction(conn):
        conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO recovery_codes (user_id, code_hash) VALUES (?, ?)",
            [(user_id, hashlib.sha256(code.encode()).hexdigest()) for code in codes],
        )
    return [f"{code[:5]}-{code[5:]}" for code in codes]


def verify(
    conn: sqlite3.Connection, settings: Settings, user_id: int, code: str, now: float | None = None
) -> str | None:
    """Prüft einen Code. Liefert "totp", "recovery" oder None."""
    row = _user(conn, user_id)
    if not row["totp_secret"]:
        return None
    normalized = _normalize(code)
    if len(normalized) == DIGITS and normalized.isdigit():
        secret = _unwrap(settings, user_id, "active", row["totp_secret"])
        if secret is None:
            return None
        step = _matching_step(secret, normalized, row["totp_last_step"], now)
        if step is None:
            return None
        with transaction(conn):
            # Nur übernehmen, wenn kein paralleler Versuch denselben Schritt verbraucht hat.
            cursor = conn.execute(
                "UPDATE users SET totp_last_step = ? WHERE id = ? AND totp_last_step < ?",
                (step, user_id, step),
            )
        return "totp" if cursor.rowcount == 1 else None
    if len(normalized) == 10:
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        with transaction(conn):
            cursor = conn.execute(
                "UPDATE recovery_codes SET used_at = ? WHERE user_id = ? AND code_hash = ?"
                " AND used_at IS NULL",
                (now_iso(), user_id, digest),
            )
        return "recovery" if cursor.rowcount == 1 else None
    return None


# ---------------------------------------------------------------------------
# Zweiter Schritt der Anmeldung
# ---------------------------------------------------------------------------


def create_challenge(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = now_utc()
    with transaction(conn):
        conn.execute("DELETE FROM login_challenges WHERE expires_at < ?", (iso(now),))
        conn.execute(
            "INSERT INTO login_challenges (token_hash, user_id, created_at, expires_at)"
            " VALUES (?, ?, ?, ?)",
            (
                hashlib.sha256(token.encode()).hexdigest(),
                user_id,
                iso(now),
                iso(now + timedelta(minutes=CHALLENGE_MINUTES)),
            ),
        )
    return token


def get_challenge(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT c.*, u.username FROM login_challenges c JOIN users u ON u.id = c.user_id"
        " WHERE c.token_hash = ?",
        (hashlib.sha256(token.encode()).hexdigest(),),
    ).fetchone()
    if row is None:
        return None
    if parse_iso(row["expires_at"]) < now_utc() or row["attempts"] >= CHALLENGE_MAX_ATTEMPTS:
        drop_challenge(conn, token)
        return None
    return row


def count_failure(conn: sqlite3.Connection, token: str) -> int:
    """Zählt einen Fehlversuch. Liefert die verbleibenden Versuche."""
    digest = hashlib.sha256(token.encode()).hexdigest()
    with transaction(conn):
        conn.execute(
            "UPDATE login_challenges SET attempts = attempts + 1 WHERE token_hash = ?", (digest,)
        )
        row = conn.execute(
            "SELECT attempts FROM login_challenges WHERE token_hash = ?", (digest,)
        ).fetchone()
    left = CHALLENGE_MAX_ATTEMPTS - (row["attempts"] if row else CHALLENGE_MAX_ATTEMPTS)
    if left <= 0:
        drop_challenge(conn, token)
    return max(0, left)


def drop_challenge(conn: sqlite3.Connection, token: str) -> None:
    conn.execute(
        "DELETE FROM login_challenges WHERE token_hash = ?",
        (hashlib.sha256(token.encode()).hexdigest(),),
    )
