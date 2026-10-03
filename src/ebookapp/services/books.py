"""Bücher, Ausgaben und Dateien."""

from __future__ import annotations

import sqlite3
from typing import Any, BinaryIO

from ..db import now_iso, transaction
from ..errors import Conflict, Invalid, NotFound, field_error
from ..i18n import _
from ..schemas import BookCreate, BookUpdate, EditionCreate, EditionPublish
from ..security import new_id
from ..storage import Storage
from ..validation import (
    EXTENSIONS,
    FORMATS,
    EpubLimits,
    check_declared,
    process_cover,
    sanitize_filename,
    validate_epub,
    validate_pdf,
)
from . import links as link_service

# ---------------------------------------------------------------------------
# Ausgabe
# ---------------------------------------------------------------------------


def _file_out(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "format": row["format"],
        "original_filename": row["original_filename"],
        "size_bytes": row["size_bytes"],
        "sha256": row["sha256"],
        "created_at": row["created_at"],
    }


def _edition_out(conn: sqlite3.Connection, row: sqlite3.Row, current_id: str | None) -> dict:
    files = conn.execute(
        "SELECT * FROM edition_files WHERE edition_id = ? ORDER BY format DESC", (row["id"],)
    ).fetchall()
    links_count = conn.execute(
        "SELECT COUNT(*) FROM links WHERE edition_id = ? AND status != 'revoked'", (row["id"],)
    ).fetchone()[0]
    return {
        "id": row["id"],
        "book_id": row["book_id"],
        "number": row["number"],
        "note": row["note"],
        "status": row["status"],
        "is_current": row["id"] == current_id,
        "published_at": row["published_at"],
        "created_at": row["created_at"],
        "files": [_file_out(item) for item in files],
        "links_count": links_count,
    }


def book_out(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    current = None
    if row["current_edition_id"]:
        current_row = conn.execute(
            "SELECT * FROM editions WHERE id = ?", (row["current_edition_id"],)
        ).fetchone()
        if current_row:
            current = _edition_out(conn, current_row, row["current_edition_id"])
    draft_row = conn.execute(
        "SELECT * FROM editions WHERE book_id = ? AND status = 'draft' ORDER BY number DESC",
        (row["id"],),
    ).fetchone()
    now = now_iso()
    counts = conn.execute(
        f"""
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN {link_service.STATE_SQL} = 'active' THEN 1 ELSE 0 END), 0)
                   AS active,
               COALESCE(SUM(l.download_count), 0) AS downloads
        FROM links l WHERE l.book_id = :book
        """,  # noqa: S608 - STATE_SQL ist eine feste Konstante
        {"book": row["id"], "now": now},
    ).fetchone()
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "whop_product_id": row["whop_product_id"],
        "language": row["language"],
        "status": row["status"],
        "has_cover": bool(row["cover_key"]),
        "current_edition": current,
        "draft_edition": (
            _edition_out(conn, draft_row, row["current_edition_id"]) if draft_row else None
        ),
        "editions_count": conn.execute(
            "SELECT COUNT(*) FROM editions WHERE book_id = ?", (row["id"],)
        ).fetchone()[0],
        "links": {"total": counts["total"], "active": counts["active"]},
        "downloads_total": counts["downloads"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------------------
# Bücher
# ---------------------------------------------------------------------------


def get_book_row(conn: sqlite3.Connection, book_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if row is None:
        raise NotFound(_("Dieses Buch gibt es nicht."), code="book_not_found")
    return row


def get_book(conn: sqlite3.Connection, book_id: str) -> dict[str, Any]:
    return book_out(conn, get_book_row(conn, book_id))


def create_book(conn: sqlite3.Connection, data: BookCreate) -> dict[str, Any]:
    book_id = new_id("bk")
    now = now_iso()
    with transaction(conn):
        conn.execute(
            "INSERT INTO books"
            " (id, title, description, whop_product_id, language, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (book_id, data.title, data.description, data.whop_product_id, data.language, now, now),
        )
        conn.execute(
            "INSERT INTO editions (id, book_id, number, status, created_at)"
            " VALUES (?, ?, 1, 'draft', ?)",
            (new_id("ed"), book_id, now),
        )
    return get_book(conn, book_id)


def list_books(
    conn: sqlite3.Connection,
    *,
    q: str | None = None,
    status: str = "active",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    where = []
    params: list[Any] = []
    if status in ("active", "archived"):
        where.append("status = ?")
        params.append(status)
    if q and q.strip():
        like = f"%{_escape_like(q.strip())}%"
        where.append(
            "(title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\'"
            " OR whop_product_id LIKE ? ESCAPE '\\' OR id = ?)"
        )
        params.extend([like, like, like, q.strip()])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM books {clause}", params).fetchone()[0]  # noqa: S608
    rows = conn.execute(
        f"SELECT * FROM books {clause} ORDER BY title COLLATE NOCASE, created_at"  # noqa: S608
        " LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return {
        "items": [book_out(conn, row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def update_book(conn: sqlite3.Connection, book_id: str, data: BookUpdate) -> dict[str, Any]:
    changes = {name: getattr(data, name) for name in data.model_fields_set}
    if "title" in changes and changes["title"] is None:
        raise field_error("title", _("Der Titel darf nicht leer sein."))
    if "description" in changes and changes["description"] is None:
        changes["description"] = ""
    if "language" in changes and changes["language"] is None:
        del changes["language"]
    with transaction(conn):
        get_book_row(conn, book_id)
        if changes:
            assignments = ", ".join(f"{column} = ?" for column in changes)
            conn.execute(
                f"UPDATE books SET {assignments}, updated_at = ? WHERE id = ?",  # noqa: S608
                [*changes.values(), now_iso(), book_id],
            )
    return get_book(conn, book_id)


def set_archived(conn: sqlite3.Connection, book_id: str, archived: bool) -> dict[str, Any]:
    with transaction(conn):
        get_book_row(conn, book_id)
        conn.execute(
            "UPDATE books SET status = ?, updated_at = ? WHERE id = ?",
            ("archived" if archived else "active", now_iso(), book_id),
        )
    return get_book(conn, book_id)


PREVIEW_LINK_LIMIT = 500


def deletion_preview(
    conn: sqlite3.Connection, book_id: str, *, include_links: bool = True
) -> dict[str, Any]:
    row = get_book_row(conn, book_id)
    listing = link_service.list_links(conn, book_id=book_id, limit=PREVIEW_LINK_LIMIT)
    active = conn.execute(
        f"SELECT COUNT(*) FROM links l WHERE l.book_id = :book"  # noqa: S608
        f" AND {link_service.STATE_SQL} = 'active'",
        {"book": book_id, "now": now_iso()},
    ).fetchone()[0]
    files = conn.execute(
        "SELECT COUNT(DISTINCT f.storage_key) FROM edition_files f"
        " JOIN editions e ON e.id = f.edition_id WHERE e.book_id = ?",
        (book_id,),
    ).fetchone()[0]
    return {
        "book_id": book_id,
        "title": row["title"],
        "links_total": listing["total"],
        "links_active": active,
        "links": listing["items"] if include_links else [],
        "links_truncated": include_links and listing["total"] > len(listing["items"]),
        "editions": conn.execute(
            "SELECT COUNT(*) FROM editions WHERE book_id = ?", (book_id,)
        ).fetchone()[0],
        "files": files + (1 if row["cover_key"] else 0),
    }


def delete_book(
    conn: sqlite3.Connection, storage: Storage, book_id: str, expected_link_count: int
) -> dict[str, Any]:
    """Löscht ein Buch samt Dateien und Links.

    ``expected_link_count`` muss der Anzahl der betroffenen Links entsprechen. So ist
    sichergestellt, dass der Aufrufer weiß, wie viele Links ungültig werden.
    """
    with transaction(conn):
        row = get_book_row(conn, book_id)
        total = conn.execute("SELECT COUNT(*) FROM links WHERE book_id = ?", (book_id,)).fetchone()[
            0
        ]
        if expected_link_count != total:
            raise Conflict(
                _(
                    "Die Löschung wurde nicht bestätigt: Es würden %(total)s Käuferlinks ungültig.",
                    total=total,
                ),
                code="confirmation_mismatch",
                details={"links_total": total},
            )
        keys = [
            item["storage_key"]
            for item in conn.execute(
                "SELECT DISTINCT f.storage_key FROM edition_files f"
                " JOIN editions e ON e.id = f.edition_id WHERE e.book_id = ?",
                (book_id,),
            )
        ]
        if row["cover_key"]:
            keys.append(row["cover_key"])
        conn.execute("UPDATE books SET current_edition_id = NULL WHERE id = ?", (book_id,))
        conn.execute("DELETE FROM links WHERE book_id = ?", (book_id,))
        conn.execute("DELETE FROM editions WHERE book_id = ?", (book_id,))
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    for key in keys:
        storage.delete(key)
    storage.remove_book_dir(book_id)
    return {"deleted": True, "links_invalidated": total, "files_deleted": len(keys)}


def set_cover(
    conn: sqlite3.Connection,
    storage: Storage,
    book_id: str,
    source: BinaryIO,
    filename: str | None,
    content_type: str | None,
    max_bytes: int,
) -> dict[str, Any]:
    get_book_row(conn, book_id)
    check_declared("cover", filename, content_type, "cover")
    data, mime, extension = process_cover(source, max_bytes)
    key = storage.write_bytes(book_id, data, extension, prefix="cover-")
    try:
        with transaction(conn):
            row = get_book_row(conn, book_id)
            conn.execute(
                "UPDATE books SET cover_key = ?, cover_mime = ?, updated_at = ? WHERE id = ?",
                (key, mime, now_iso(), book_id),
            )
    except BaseException:
        storage.delete(key)
        raise
    if row["cover_key"]:
        storage.delete(row["cover_key"])
    return get_book(conn, book_id)


def remove_cover(conn: sqlite3.Connection, storage: Storage, book_id: str) -> dict[str, Any]:
    with transaction(conn):
        row = get_book_row(conn, book_id)
        conn.execute(
            "UPDATE books SET cover_key = NULL, cover_mime = NULL, updated_at = ? WHERE id = ?",
            (now_iso(), book_id),
        )
    if row["cover_key"]:
        storage.delete(row["cover_key"])
    return get_book(conn, book_id)


# ---------------------------------------------------------------------------
# Ausgaben
# ---------------------------------------------------------------------------


def _edition_row(conn: sqlite3.Connection, book_id: str, edition_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM editions WHERE id = ? AND book_id = ?", (edition_id, book_id)
    ).fetchone()
    if row is None:
        raise NotFound(_("Diese Ausgabe gibt es nicht."), code="edition_not_found")
    return row


def list_editions(conn: sqlite3.Connection, book_id: str) -> list[dict[str, Any]]:
    book = get_book_row(conn, book_id)
    rows = conn.execute(
        "SELECT * FROM editions WHERE book_id = ? ORDER BY number DESC", (book_id,)
    ).fetchall()
    return [_edition_out(conn, row, book["current_edition_id"]) for row in rows]


def get_edition(conn: sqlite3.Connection, book_id: str, edition_id: str) -> dict[str, Any]:
    book = get_book_row(conn, book_id)
    return _edition_out(conn, _edition_row(conn, book_id, edition_id), book["current_edition_id"])


def create_edition(conn: sqlite3.Connection, book_id: str, data: EditionCreate) -> dict[str, Any]:
    edition_id = new_id("ed")
    now = now_iso()
    with transaction(conn):
        book = get_book_row(conn, book_id)
        if conn.execute(
            "SELECT 1 FROM editions WHERE book_id = ? AND status = 'draft'", (book_id,)
        ).fetchone():
            raise Conflict(
                _(
                    "Es gibt bereits einen Entwurf für eine neue Ausgabe. Veröffentliche oder "
                    "lösche ihn zuerst."
                ),
                code="draft_exists",
            )
        number = conn.execute(
            "SELECT COALESCE(MAX(number), 0) + 1 FROM editions WHERE book_id = ?", (book_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO editions (id, book_id, number, note, status, created_at)"
            " VALUES (?, ?, ?, ?, 'draft', ?)",
            (edition_id, book_id, number, data.note, now),
        )
        for fmt in data.copy_formats:
            source = None
            if book["current_edition_id"]:
                source = conn.execute(
                    "SELECT * FROM edition_files WHERE edition_id = ? AND format = ?",
                    (book["current_edition_id"], fmt),
                ).fetchone()
            if source is None:
                raise field_error(
                    "copy_formats",
                    _(
                        "Die aktuelle Ausgabe enthält keine %(format)s-Datei zum Übernehmen.",
                        format=fmt.upper(),
                    ),
                )
            conn.execute(
                "INSERT INTO edition_files (id, edition_id, format, storage_key,"
                " original_filename, size_bytes, sha256, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("fil"),
                    edition_id,
                    fmt,
                    source["storage_key"],
                    source["original_filename"],
                    source["size_bytes"],
                    source["sha256"],
                    now,
                ),
            )
    return get_edition(conn, book_id, edition_id)


def _require_draft(row: sqlite3.Row) -> None:
    if row["status"] != "draft":
        raise Conflict(
            _(
                "Veröffentlichte Ausgaben sind unveränderlich. Lege für geänderte Dateien eine "
                "neue Ausgabe an."
            ),
            code="edition_published",
        )


def _release_key(conn: sqlite3.Connection, storage: Storage, key: str) -> None:
    """Löscht eine Datei physisch, sobald keine Ausgabe mehr auf sie verweist."""
    if not conn.execute("SELECT 1 FROM edition_files WHERE storage_key = ?", (key,)).fetchone():
        storage.delete(key)


def upload_files(
    conn: sqlite3.Connection,
    storage: Storage,
    *,
    book_id: str,
    edition_id: str,
    uploads: dict[str, tuple[BinaryIO, str | None, str | None]],
    max_bytes: dict[str, int],
    epub_limits: EpubLimits,
) -> dict[str, Any]:
    """Prüft hochgeladene Dateien und hinterlegt sie im Entwurf einer Ausgabe.

    ``uploads`` ordnet einem Format (Datenstrom, Dateiname, angegebener MIME-Typ) zu.
    Entweder werden alle Dateien übernommen oder keine: Ist eine ungültig, bleibt der
    Entwurf unverändert.
    """
    for fmt in uploads:
        if fmt not in FORMATS:
            raise field_error("format", _("Erlaubt sind die Formate pdf und epub."))
    get_book_row(conn, book_id)
    _require_draft(_edition_row(conn, book_id, edition_id))
    for fmt, (_source, filename, content_type) in uploads.items():
        check_declared(fmt, filename, content_type, fmt)

    staged: list[tuple[str, Any, str | None]] = []
    keys: dict[str, str] = {}
    old_keys: list[str] = []
    try:
        for fmt, (source, filename, _ctype) in uploads.items():
            limit_mb = max_bytes[fmt] // (1024 * 1024)
            temp = storage.write_temp(
                source,
                max_bytes[fmt],
                _(
                    "Die %(format)s-Datei ist größer als erlaubt (%(limit)s MB).",
                    format=fmt.upper(),
                    limit=limit_mb,
                ),
            )
            staged.append((fmt, temp, filename))
            if temp.size == 0:
                raise field_error(fmt, _("Die Datei ist leer."))
            if fmt == "pdf":
                validate_pdf(temp.path, "pdf")
            else:
                validate_epub(temp.path, epub_limits, "epub")

        for fmt, temp, _name in staged:
            keys[fmt] = storage.commit(temp.path, book_id, EXTENSIONS[fmt][0])
        with transaction(conn):
            _require_draft(_edition_row(conn, book_id, edition_id))
            for fmt, temp, filename in staged:
                old = conn.execute(
                    "SELECT * FROM edition_files WHERE edition_id = ? AND format = ?",
                    (edition_id, fmt),
                ).fetchone()
                if old:
                    old_keys.append(old["storage_key"])
                    conn.execute("DELETE FROM edition_files WHERE id = ?", (old["id"],))
                conn.execute(
                    "INSERT INTO edition_files (id, edition_id, format, storage_key,"
                    " original_filename, size_bytes, sha256, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id("fil"),
                        edition_id,
                        fmt,
                        keys[fmt],
                        sanitize_filename(filename, f"datei{EXTENSIONS[fmt][0]}"),
                        temp.size,
                        temp.sha256,
                        now_iso(),
                    ),
                )
            conn.execute("UPDATE books SET updated_at = ? WHERE id = ?", (now_iso(), book_id))
    except BaseException:
        for _fmt, temp, _name in staged:
            storage.discard(temp.path)
        for key in keys.values():
            storage.delete(key)
        raise
    for key in old_keys:
        _release_key(conn, storage, key)
    return get_edition(conn, book_id, edition_id)


def delete_file(
    conn: sqlite3.Connection, storage: Storage, book_id: str, edition_id: str, fmt: str
) -> dict[str, Any]:
    with transaction(conn):
        get_book_row(conn, book_id)
        _require_draft(_edition_row(conn, book_id, edition_id))
        row = conn.execute(
            "SELECT * FROM edition_files WHERE edition_id = ? AND format = ?", (edition_id, fmt)
        ).fetchone()
        if row is None:
            raise NotFound(_("Diese Datei gibt es nicht."), code="file_not_found")
        conn.execute("DELETE FROM edition_files WHERE id = ?", (row["id"],))
    _release_key(conn, storage, row["storage_key"])
    return get_edition(conn, book_id, edition_id)


def delete_edition(
    conn: sqlite3.Connection, storage: Storage, book_id: str, edition_id: str
) -> None:
    with transaction(conn):
        book = get_book_row(conn, book_id)
        row = _edition_row(conn, book_id, edition_id)
        if book["current_edition_id"] == edition_id:
            raise Conflict(
                _(
                    "Die aktuelle Ausgabe kann nicht gelöscht werden. Veröffentliche zuerst "
                    "eine andere Ausgabe oder lösche das ganze Buch."
                ),
                code="edition_is_current",
            )
        bound = conn.execute(
            "SELECT COUNT(*) FROM links WHERE edition_id = ?", (edition_id,)
        ).fetchone()[0]
        if bound:
            raise Conflict(
                _(
                    "An diese Ausgabe sind noch %(count)s Käuferlinks gebunden (auch "
                    "widerrufene zählen). Stelle sie zuerst auf eine andere Ausgabe um oder "
                    "lösche sie.",
                    count=bound,
                ),
                code="edition_has_links",
                details={"links": bound},
            )
        keys = [
            item["storage_key"]
            for item in conn.execute(
                "SELECT storage_key FROM edition_files WHERE edition_id = ?", (edition_id,)
            )
        ]
        conn.execute("DELETE FROM editions WHERE id = ?", (row["id"],))
    for key in keys:
        _release_key(conn, storage, key)


def publish_edition(
    conn: sqlite3.Connection, book_id: str, edition_id: str, data: EditionPublish
) -> dict[str, Any]:
    """Veröffentlicht einen Entwurf als neue aktuelle Ausgabe.

    Bestehende Links werden nur umgestellt, wenn ``existing_links == "migrate"``
    ausdrücklich angegeben ist. Hat das Buch Links, ist die Angabe Pflicht.
    """
    now = now_iso()
    with transaction(conn):
        get_book_row(conn, book_id)
        row = _edition_row(conn, book_id, edition_id)
        if row["status"] != "draft":
            raise Conflict(_("Diese Ausgabe ist bereits veröffentlicht."), code="edition_published")
        if not conn.execute(
            "SELECT 1 FROM edition_files WHERE edition_id = ?", (edition_id,)
        ).fetchone():
            raise Invalid(
                _("Die Ausgabe enthält noch keine Datei. Lade zuerst ein PDF oder EPUB hoch."),
                code="edition_empty",
            )
        existing = conn.execute(
            "SELECT COUNT(*) FROM links WHERE book_id = ? AND status != 'revoked'", (book_id,)
        ).fetchone()[0]
        if existing and data.existing_links is None:
            message = _(
                "Das Buch hat %(count)s bestehende Käuferlinks. Wähle ausdrücklich, ob sie "
                "die neue Ausgabe erhalten (migrate) oder an ihrer bisherigen Ausgabe bleiben "
                "(keep).",
                count=existing,
            )
            raise Invalid(
                message,
                code="existing_links_required",
                fields=[{"field": "existing_links", "message": message}],
                details={"existing_links": existing},
            )
        conn.execute(
            "UPDATE editions SET status = 'published', published_at = ? WHERE id = ?",
            (now, edition_id),
        )
        conn.execute(
            "UPDATE books SET current_edition_id = ?, updated_at = ? WHERE id = ?",
            (edition_id, now, book_id),
        )
        migrated = 0
        unmatched = 0
        if data.existing_links == "migrate":
            migrated, unmatched = _migrate(conn, book_id, edition_id, now)
    return {
        "edition": get_edition(conn, book_id, edition_id),
        "links_migrated": migrated,
        "links_kept": existing - migrated,
        "links_without_matching_format": unmatched,
    }


def _migrate(conn: sqlite3.Connection, book_id: str, edition_id: str, now: str) -> tuple[int, int]:
    """Stellt Links um. Liefert (umgestellt, übersprungen).

    Ein Link wird nur umgestellt, wenn die Zielausgabe mindestens eines seiner
    freigegebenen Formate enthält. Sonst bliebe er zwar aktiv, böte aber keine Datei mehr
    an. Solche Links bleiben an ihrer bisherigen Ausgabe und werden gemeldet.
    """
    available = {
        row["format"]
        for row in conn.execute(
            "SELECT format FROM edition_files WHERE edition_id = ?", (edition_id,)
        )
    }
    candidates = "book_id = ? AND status != 'revoked' AND edition_id != ?"
    matching = "((allow_pdf = 1 AND ?) OR (allow_epub = 1 AND ?))"
    cursor = conn.execute(
        f"UPDATE links SET edition_id = ?, updated_at = ? WHERE {candidates} AND {matching}",  # noqa: S608
        (edition_id, now, book_id, edition_id, int("pdf" in available), int("epub" in available)),
    )
    skipped = conn.execute(
        f"SELECT COUNT(*) FROM links WHERE {candidates}",  # noqa: S608
        (book_id, edition_id),
    ).fetchone()[0]
    return cursor.rowcount, skipped


def migrate_links(conn: sqlite3.Connection, book_id: str, edition_id: str) -> dict[str, int]:
    """Stellt alle nicht widerrufenen Links eines Buchs ausdrücklich auf eine Ausgabe um."""
    with transaction(conn):
        get_book_row(conn, book_id)
        row = _edition_row(conn, book_id, edition_id)
        if row["status"] != "published":
            raise Conflict(
                _("Links können nur an veröffentlichte Ausgaben gebunden werden."),
                code="edition_not_published",
            )
        migrated, skipped = _migrate(conn, book_id, edition_id, now_iso())
    return {"links_migrated": migrated, "links_without_matching_format": skipped}


def referenced_keys(conn: sqlite3.Connection) -> set[str]:
    keys = {row[0] for row in conn.execute("SELECT storage_key FROM edition_files")}
    keys.update(
        row[0] for row in conn.execute("SELECT cover_key FROM books WHERE cover_key IS NOT NULL")
    )
    return keys


def collect_orphans(
    conn: sqlite3.Connection, storage: Storage, min_age_seconds: float = 3600
) -> list[str]:
    """Entfernt Dateien, auf die kein Datenbankeintrag mehr verweist.

    Junge Dateien bleiben unangetastet, damit laufende Uploads nicht gestört werden.
    """
    import time

    if storage.backup_running.is_set():
        return []
    referenced = referenced_keys(conn)
    cutoff = time.time() - min_age_seconds
    removed = []
    for key, mtime in storage.all_keys():
        if key not in referenced and mtime < cutoff:
            storage.delete(key)
            removed.append(key)
    return removed
