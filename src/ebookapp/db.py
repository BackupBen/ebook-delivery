"""SQLite-Zugriff und Schema-Migrationen."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

MIGRATIONS: list[str] = [
    # Version 1
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf_token TEXT NOT NULL,
        flash TEXT,
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );

    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE books (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        whop_product_id TEXT,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
        cover_key TEXT,
        cover_mime TEXT,
        current_edition_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX books_status ON books(status, title);

    CREATE TABLE editions (
        id TEXT PRIMARY KEY,
        book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        number INTEGER NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published')),
        published_at TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (book_id, number)
    );

    CREATE TABLE edition_files (
        id TEXT PRIMARY KEY,
        edition_id TEXT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
        format TEXT NOT NULL CHECK (format IN ('pdf', 'epub')),
        storage_key TEXT NOT NULL,
        original_filename TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (edition_id, format)
    );
    CREATE INDEX edition_files_key ON edition_files(storage_key);

    CREATE TABLE links (
        id TEXT PRIMARY KEY,
        book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        edition_id TEXT NOT NULL REFERENCES editions(id),
        code_hash TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL DEFAULT '',
        allow_pdf INTEGER NOT NULL DEFAULT 1,
        allow_epub INTEGER NOT NULL DEFAULT 1,
        expires_at TEXT,
        max_downloads INTEGER,
        download_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'disabled', 'revoked')),
        revoked_at TEXT,
        last_download_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX links_book ON links(book_id, created_at);
    CREATE INDEX links_edition ON links(edition_id);

    -- Kurzlebige Zuordnung "dieser Client lädt diese Datei gerade". Enthält nur einen
    -- verschlüsselten Fingerabdruck und wird nach Ablauf des Zeitfensters gelöscht.
    CREATE TABLE download_grants (
        link_id TEXT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
        file_id TEXT NOT NULL,
        client_hash TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        PRIMARY KEY (link_id, file_id, client_hash)
    );

    CREATE TABLE download_events (
        id INTEGER PRIMARY KEY,
        link_id TEXT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
        book_id TEXT NOT NULL,
        edition_id TEXT NOT NULL,
        format TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX download_events_book ON download_events(book_id, created_at);
    CREATE INDEX download_events_link ON download_events(link_id, created_at);

    CREATE TABLE api_keys (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        key_prefix TEXT NOT NULL,
        key_hash TEXT NOT NULL UNIQUE,
        scopes TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_used_at TEXT,
        revoked_at TEXT
    );

    CREATE TABLE idempotency_keys (
        scope TEXT NOT NULL,
        key_hash TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('in_progress', 'done')),
        status_code INTEGER,
        response_json TEXT,
        wrapped_secret TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (scope, key_hash)
    );

    CREATE TABLE backup_runs (
        id INTEGER PRIMARY KEY,
        target TEXT NOT NULL CHECK (target IN ('local', 'offsite')),
        trigger TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'failed')),
        snapshot_id TEXT,
        detail TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL,
        finished_at TEXT
    );
    CREATE INDEX backup_runs_started ON backup_runs(started_at);
    """,
    # Version 2: Sprache der Käuferseite und der Versandnachricht je Buch
    """
    ALTER TABLE books ADD COLUMN language TEXT NOT NULL DEFAULT 'de'
        CHECK (language IN ('de', 'en'));
    """,
    # Version 3: Bestellungen aus Whop und E-Mail-Versand
    """
    -- Bereits verarbeitete Webhook-Zustellungen (webhook-id). Whop stellt mindestens
    -- einmal zu; Wiederholungen werden hieran erkannt.
    CREATE TABLE webhook_events (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        type TEXT NOT NULL,
        received_at TEXT NOT NULL
    );
    CREATE INDEX webhook_events_received ON webhook_events(received_at);

    CREATE TABLE orders (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL DEFAULT 'whop',
        payment_id TEXT NOT NULL,
        product_id TEXT NOT NULL DEFAULT '',
        product_title TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL DEFAULT '',
        book_id TEXT REFERENCES books(id) ON DELETE SET NULL,
        link_id TEXT REFERENCES links(id) ON DELETE SET NULL,
        status TEXT NOT NULL
            CHECK (status IN ('pending', 'sent', 'failed', 'unmatched', 'no_email')),
        detail TEXT NOT NULL DEFAULT '',
        -- Verschlüsselter Link-Code, nur bis die E-Mail versendet ist.
        wrapped_code TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        sent_at TEXT,
        message_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (source, payment_id)
    );
    CREATE INDEX orders_created ON orders(created_at);
    CREATE INDEX orders_due ON orders(status, next_attempt_at);
    """,
]


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(value: datetime) -> str:
    """UTC-Zeitstempel in einem lexikografisch sortierbaren Format."""
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(now_utc())


def iso_in(**delta: float) -> str:
    return iso(now_utc() + timedelta(**delta))


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def connect(path: Path) -> sqlite3.Connection:
    # isolation_level=None: Transaktionen werden ausdrücklich mit BEGIN gesteuert.
    # check_same_thread=False: FastAPI führt Abhängigkeiten und Handler ggf. in
    # verschiedenen Threads desselben Requests aus; die Verbindung wird nie parallel genutzt.
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextmanager
def open_db(path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Schreibtransaktion. BEGIN IMMEDIATE verhindert Lese-/Schreib-Wettläufe."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(path: Path) -> int:
    """Wendet ausstehende Migrationen an und liefert die Schema-Version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for index in range(version, len(MIGRATIONS)):
            statements = MIGRATIONS[index]
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _split(statements):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {index + 1}")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return len(MIGRATIONS)
    finally:
        conn.close()


def _split(script: str) -> list[str]:
    """Zerlegt ein Skript in einzelne Anweisungen (ohne Trigger-Unterstützung)."""
    lines = [line for line in script.splitlines() if not line.strip().startswith("--")]
    return [part.strip() for part in "\n".join(lines).split(";") if part.strip()]
