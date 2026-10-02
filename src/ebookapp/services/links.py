"""Käuferlinks und die Autorisierung von Downloads."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from ..db import iso, now_iso, now_utc, transaction
from ..errors import Conflict, NotFound, field_error
from ..schemas import LinkCreate, LinkUpdate
from ..security import CODE_RE, new_id, new_link_code, sha256_hex

# Tatsächlicher Zustand eines Links als SQL-Ausdruck (benötigt den Parameter :now).
STATE_SQL = """(CASE
    WHEN l.status = 'revoked' THEN 'revoked'
    WHEN l.status = 'disabled' THEN 'disabled'
    WHEN l.expires_at IS NOT NULL AND l.expires_at <= :now THEN 'expired'
    WHEN l.max_downloads IS NOT NULL AND l.download_count >= l.max_downloads THEN 'exhausted'
    ELSE 'active' END)"""

SELECT = f"""
    SELECT l.*, b.title AS book_title, e.number AS edition_number, {STATE_SQL} AS state
    FROM links l
    JOIN books b ON b.id = l.book_id
    JOIN editions e ON e.id = l.edition_id
"""  # noqa: S608 - STATE_SQL ist eine feste Konstante

STATES = ("active", "disabled", "revoked", "expired", "exhausted")


def _formats(row: sqlite3.Row) -> list[str]:
    return [
        fmt for fmt, allowed in (("pdf", row["allow_pdf"]), ("epub", row["allow_epub"])) if allowed
    ]


def link_out(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "book_id": row["book_id"],
        "book_title": row["book_title"],
        "edition_id": row["edition_id"],
        "edition_number": row["edition_number"],
        "label": row["label"],
        "formats": _formats(row),
        "expires_at": row["expires_at"],
        "max_downloads": row["max_downloads"],
        "download_count": row["download_count"],
        "status": row["status"],
        "state": row["state"],
        "revoked_at": row["revoked_at"],
        "last_download_at": row["last_download_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row(conn: sqlite3.Connection, link_id: str) -> sqlite3.Row:
    row = conn.execute(f"{SELECT} WHERE l.id = :id", {"id": link_id, "now": now_iso()}).fetchone()
    if row is None:
        raise NotFound("Diesen Link gibt es nicht.", code="link_not_found")
    return row


def get_link(conn: sqlite3.Connection, link_id: str) -> dict[str, Any]:
    return link_out(_row(conn, link_id))


def build_url(base_url: str, code: str) -> str:
    return f"{base_url.rstrip('/')}/d/{code}"


def _published_edition(conn: sqlite3.Connection, book_id: str, edition_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM editions WHERE id = ? AND book_id = ?", (edition_id, book_id)
    ).fetchone()
    if row is None:
        raise field_error("edition_id", "Diese Ausgabe gehört nicht zu dem Buch.")
    if row["status"] != "published":
        raise field_error(
            "edition_id", "Links können nur an veröffentlichte Ausgaben gebunden werden."
        )
    return row


def _check_future(expires_at: Any) -> str | None:
    if expires_at is None:
        return None
    if expires_at <= now_utc():
        raise field_error("expires_at", "Das Ablaufdatum muss in der Zukunft liegen.")
    return iso(expires_at)


def create_link(conn: sqlite3.Connection, data: LinkCreate) -> tuple[dict[str, Any], str]:
    """Erstellt einen Link. Der geheime Code wird nur hier einmalig zurückgegeben."""
    code = new_link_code()
    link_id = new_id("lnk")
    now = now_iso()
    expires_at = _check_future(data.expires_at)
    with transaction(conn):
        book = conn.execute("SELECT * FROM books WHERE id = ?", (data.book_id,)).fetchone()
        if book is None:
            raise field_error("book_id", "Dieses Buch gibt es nicht.")
        if book["status"] == "archived":
            raise field_error(
                "book_id", "Für archivierte Bücher können keine neuen Links erstellt werden."
            )
        edition_id = data.edition_id or book["current_edition_id"]
        if not edition_id:
            raise field_error(
                "book_id",
                "Das Buch hat noch keine veröffentlichte Ausgabe. Lade Dateien hoch und "
                "veröffentliche die Ausgabe zuerst.",
            )
        _published_edition(conn, book["id"], edition_id)
        available = {
            item["format"]
            for item in conn.execute(
                "SELECT format FROM edition_files WHERE edition_id = ?", (edition_id,)
            )
        }
        if not available.intersection(data.formats):
            raise field_error(
                "formats", "Die Ausgabe enthält keine Datei in den ausgewählten Formaten."
            )
        conn.execute(
            "INSERT INTO links (id, book_id, edition_id, code_hash, label, allow_pdf, allow_epub,"
            " expires_at, max_downloads, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                link_id,
                book["id"],
                edition_id,
                sha256_hex(code),
                data.label,
                int("pdf" in data.formats),
                int("epub" in data.formats),
                expires_at,
                data.max_downloads,
                now,
                now,
            ),
        )
    return get_link(conn, link_id), code


def list_links(
    conn: sqlite3.Connection,
    *,
    book_id: str | None = None,
    state: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    where = []
    params: dict[str, Any] = {"now": now_iso()}
    if book_id:
        where.append("l.book_id = :book_id")
        params["book_id"] = book_id
    if state in STATES:
        where.append(f"{STATE_SQL} = :state")
        params["state"] = state
    if q and q.strip():
        text = q.strip()
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append(
            "(l.label LIKE :like ESCAPE '\\' OR b.title LIKE :like ESCAPE '\\' OR l.id = :exact)"
        )
        params["like"] = f"%{escaped}%"
        params["exact"] = text
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(
        f"SELECT COUNT(*) FROM links l JOIN books b ON b.id = l.book_id {clause}",  # noqa: S608
        params,
    ).fetchone()[0]
    rows = conn.execute(
        f"{SELECT} {clause} ORDER BY l.created_at DESC, l.id LIMIT :limit OFFSET :offset",
        {**params, "limit": limit, "offset": offset},
    ).fetchall()
    return {
        "items": [link_out(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def update_link(conn: sqlite3.Connection, link_id: str, data: LinkUpdate) -> dict[str, Any]:
    fields = data.model_fields_set
    with transaction(conn):
        row = _row(conn, link_id)
        if row["status"] == "revoked":
            raise Conflict(
                "Widerrufene Links können nicht mehr geändert werden.", code="link_revoked"
            )
        changes: dict[str, Any] = {}
        if "label" in fields:
            changes["label"] = data.label or ""
        if "formats" in fields and data.formats is not None:
            changes["allow_pdf"] = int("pdf" in data.formats)
            changes["allow_epub"] = int("epub" in data.formats)
        if "expires_at" in fields:
            changes["expires_at"] = _check_future(data.expires_at)
        if "max_downloads" in fields:
            changes["max_downloads"] = data.max_downloads
        if "status" in fields and data.status is not None:
            changes["status"] = data.status
        if "edition_id" in fields and data.edition_id is not None:
            _published_edition(conn, row["book_id"], data.edition_id)
            changes["edition_id"] = data.edition_id
        if {"allow_pdf", "allow_epub", "edition_id"} & changes.keys():
            available = {
                item["format"]
                for item in conn.execute(
                    "SELECT format FROM edition_files WHERE edition_id = ?",
                    (changes.get("edition_id", row["edition_id"]),),
                )
            }
            allowed = {
                fmt for fmt in ("pdf", "epub") if changes.get(f"allow_{fmt}", row[f"allow_{fmt}"])
            }
            if not allowed & available:
                raise field_error(
                    "formats",
                    "Die Ausgabe enthält keine Datei in den freigegebenen Formaten. Der Link "
                    "würde nichts mehr anbieten.",
                )
        if changes:
            assignments = ", ".join(f"{column} = ?" for column in changes)
            conn.execute(
                f"UPDATE links SET {assignments}, updated_at = ? WHERE id = ?",  # noqa: S608
                [*changes.values(), now_iso(), link_id],
            )
    return get_link(conn, link_id)


def revoke_link(conn: sqlite3.Connection, link_id: str) -> dict[str, Any]:
    """Widerruft einen Link endgültig. Ein erneuter Aufruf ändert nichts."""
    now = now_iso()
    with transaction(conn):
        row = _row(conn, link_id)
        if row["status"] != "revoked":
            conn.execute(
                "UPDATE links SET status = 'revoked', revoked_at = ?, updated_at = ? WHERE id = ?",
                (now, now, link_id),
            )
            conn.execute("DELETE FROM download_grants WHERE link_id = ?", (link_id,))
    return get_link(conn, link_id)


def delete_link(conn: sqlite3.Connection, link_id: str) -> None:
    with transaction(conn):
        row = _row(conn, link_id)
        if row["status"] != "revoked":
            raise Conflict(
                "Nur widerrufene Links können endgültig gelöscht werden.", code="link_not_revoked"
            )
        conn.execute("DELETE FROM links WHERE id = ?", (link_id,))


def extract_code(text: str) -> str | None:
    """Liest den Code aus einem eingefügten Link oder Code."""
    text = text.strip()
    if CODE_RE.match(text):
        return text
    try:
        path = urlsplit(text).path
    except ValueError:
        return None
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "d" and CODE_RE.match(parts[1]):
        return parts[1]
    return None


def lookup(conn: sqlite3.Connection, text: str) -> dict[str, Any]:
    """Findet den Link zu einem Code, z. B. wenn ein Käufer seinen Link zurückschickt."""
    code = extract_code(text)
    row = None
    if code:
        row = conn.execute(
            f"{SELECT} WHERE l.code_hash = :hash", {"hash": sha256_hex(code), "now": now_iso()}
        ).fetchone()
    if row is None:
        raise NotFound("Zu diesem Code gibt es keinen Link.", code="link_not_found")
    return link_out(row)


# ---------------------------------------------------------------------------
# Käuferseite und Downloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuyerView:
    link_id: str
    state: str
    title: str
    description: str
    book_id: str
    edition_id: str
    cover_key: str | None
    cover_mime: str | None
    expires_at: str | None
    max_downloads: int | None
    download_count: int
    files: dict[str, sqlite3.Row]


class DownloadDenied(Exception):
    def __init__(self, state: str) -> None:
        super().__init__(state)
        self.state = state


def resolve(conn: sqlite3.Connection, code: str) -> BuyerView | None:
    """Löst einen Code auf. Unbekannte Codes liefern None."""
    if not CODE_RE.match(code):
        return None
    row = conn.execute(
        f"""
        SELECT l.*, {STATE_SQL} AS state, b.title, b.description, b.cover_key, b.cover_mime
        FROM links l JOIN books b ON b.id = l.book_id
        WHERE l.code_hash = :hash
        """,  # noqa: S608 - STATE_SQL ist eine feste Konstante
        {"hash": sha256_hex(code), "now": now_iso()},
    ).fetchone()
    if row is None:
        return None
    allowed = set(_formats(row))
    files = {
        item["format"]: item
        for item in conn.execute(
            "SELECT * FROM edition_files WHERE edition_id = ?", (row["edition_id"],)
        )
        if item["format"] in allowed
    }
    return BuyerView(
        link_id=row["id"],
        state=row["state"],
        title=row["title"],
        description=row["description"],
        book_id=row["book_id"],
        edition_id=row["edition_id"],
        cover_key=row["cover_key"],
        cover_mime=row["cover_mime"],
        expires_at=row["expires_at"],
        max_downloads=row["max_downloads"],
        download_count=row["download_count"],
        files=files,
    )


def authorize_download(
    conn: sqlite3.Connection,
    view: BuyerView,
    fmt: str,
    client_hash: str,
    *,
    head: bool,
    window_minutes: int,
    window_max_hours: int,
) -> tuple[sqlite3.Row, bool]:
    """Autorisiert eine Datei-Anfrage und zählt sie höchstens einmal.

    Zählweise: Ein Download wird gezählt, sobald ein Client eine Datei zu laden beginnt.
    Weitere Anfragen desselben Clients für dieselbe Datei (Range-Anfragen, parallele
    Segmente, Wiederaufnahmen) zählen nicht erneut, solange zwischen zwei Anfragen
    höchstens ``window_minutes`` liegen und der Beginn höchstens ``window_max_hours``
    zurückliegt. HEAD-Anfragen zählen nie.

    Liefert (Dateieintrag, wurde_gezählt). Löst DownloadDenied aus, wenn der Link die
    Anfrage nicht erlaubt.
    """
    if view.state not in ("active", "exhausted"):
        raise DownloadDenied(view.state)
    file_row = view.files.get(fmt)
    if file_row is None:
        raise DownloadDenied("format_unavailable")

    now = now_utc()
    idle_cutoff = iso(now - timedelta(minutes=window_minutes))
    start_cutoff = iso(now - timedelta(hours=window_max_hours))

    def active_grant() -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM download_grants WHERE link_id = ? AND file_id = ? AND client_hash = ?"
            " AND last_seen_at >= ? AND first_seen_at >= ?",
            (view.link_id, file_row["id"], client_hash, idle_cutoff, start_cutoff),
        ).fetchone()

    if head:
        # HEAD verändert nichts. Ist das Limit erreicht, bleibt HEAD nur für Clients mit
        # laufendem Download erlaubt.
        if view.state == "exhausted" and active_grant() is None:
            raise DownloadDenied("exhausted")
        return file_row, False

    stamp = iso(now)
    with transaction(conn):
        link = conn.execute("SELECT * FROM links WHERE id = ?", (view.link_id,)).fetchone()
        if link is None or link["status"] != "active":
            raise DownloadDenied("revoked" if link is None else link["status"])
        if link["expires_at"] is not None and link["expires_at"] <= stamp:
            raise DownloadDenied("expired")
        if active_grant() is not None:
            conn.execute(
                "UPDATE download_grants SET last_seen_at = ?"
                " WHERE link_id = ? AND file_id = ? AND client_hash = ?",
                (stamp, view.link_id, file_row["id"], client_hash),
            )
            return file_row, False
        if link["max_downloads"] is not None and link["download_count"] >= link["max_downloads"]:
            raise DownloadDenied("exhausted")
        conn.execute(
            "UPDATE links SET download_count = download_count + 1, last_download_at = ?"
            " WHERE id = ?",
            (stamp, view.link_id),
        )
        conn.execute(
            "INSERT INTO download_grants (link_id, file_id, client_hash, first_seen_at,"
            " last_seen_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (link_id, file_id, client_hash)"
            " DO UPDATE SET first_seen_at = excluded.first_seen_at,"
            " last_seen_at = excluded.last_seen_at",
            (view.link_id, file_row["id"], client_hash, stamp, stamp),
        )
        conn.execute(
            "INSERT INTO download_events (link_id, book_id, edition_id, format, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (view.link_id, view.book_id, view.edition_id, fmt, stamp),
        )
        # Abgelaufene Fingerabdrücke entfernen: Es bleiben keine Client-Daten zurück.
        conn.execute("DELETE FROM download_grants WHERE first_seen_at < ?", (start_cutoff,))
        return file_row, True
