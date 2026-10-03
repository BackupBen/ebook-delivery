"""Konsistente Backups von Datenbank und Buchdateien mit restic.

Ablauf eines Backups:
1. Die Datenbank wird über die SQLite-Backup-API in eine Staging-Datei kopiert. Das ist
   auch im laufenden Betrieb konsistent.
2. restic sichert diese Kopie zusammen mit dem Bücherverzeichnis. Buchdateien werden nach
   dem Schreiben nie verändert; während des Backups werden keine Dateien gelöscht.
3. Alte Snapshots werden nach der Aufbewahrungsregel entfernt.
4. Ist ein externes Ziel konfiguriert, läuft dasselbe dorthin.

Ein externes Ziel wird nur verwendet, wenn es ausdrücklich über Umgebungsvariablen
eingerichtet wurde.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .db import connect, now_iso, now_utc, parse_iso
from .errors import AppError, Conflict
from .i18n import _
from .storage import Storage

log = logging.getLogger("ebookapp.backup")

TAG = "ebookapp"
HOST = "ebookapp"
STAGING_DB = "db.sqlite3"
RESTIC_TIMEOUT = 6 * 3600

_CREDENTIALS = re.compile(r"(://)[^/@\s]+@")


def _sanitize(text: str) -> str:
    return _CREDENTIALS.sub(r"\1[redacted]@", text).strip()[-1200:]


class BackupError(AppError):
    status_code = 500
    code = "backup_failed"


class BackupManager:
    def __init__(self, settings: Settings, storage: Storage) -> None:
        self.settings = settings
        self.storage = storage
        self.restic = shutil.which("restic")
        self.local_repo = settings.backup_dir / "restic"
        self.cache_dir = settings.backup_dir / "cache"
        self.staging_dir = settings.data_dir / "backup-staging"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_housekeeping = 0.0

    # ------------------------------------------------------------------
    # Zustand
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.restic and self.settings.backup_password)

    @property
    def offsite_configured(self) -> bool:
        return bool(self.settings.backup_offsite_repository)

    @property
    def running(self) -> bool:
        return self._lock.locked() or self.storage.backup_running.is_set()

    def offsite_display(self) -> str | None:
        """Externes Ziel ohne Zugangsdaten, für die Anzeige."""
        repo = self.settings.backup_offsite_repository
        return _CREDENTIALS.sub(r"\1[redacted]@", repo) if repo else None

    def status(self, conn: sqlite3.Connection) -> dict[str, Any]:
        runs = [
            dict(row) for row in conn.execute("SELECT * FROM backup_runs ORDER BY id DESC LIMIT 12")
        ]

        def last_ok(target: str) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM backup_runs WHERE target = ? AND status = 'ok'"
                " ORDER BY id DESC LIMIT 1",
                (target,),
            ).fetchone()
            return dict(row) if row else None

        return {
            "enabled": self.enabled,
            "restic_available": bool(self.restic),
            "password_set": bool(self.settings.backup_password),
            "offsite_configured": self.offsite_configured,
            "offsite_repository": self.offsite_display(),
            "running": self.running,
            "hour": self.settings.backup_hour,
            "keep": {
                "daily": self.settings.backup_keep_daily,
                "weekly": self.settings.backup_keep_weekly,
                "monthly": self.settings.backup_keep_monthly,
            },
            "last_local": last_ok("local"),
            "last_offsite": last_ok("offsite"),
            "runs": runs,
        }

    # ------------------------------------------------------------------
    # restic
    # ------------------------------------------------------------------

    def _env(self, target: str) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self.settings.backup_dir),
            "RESTIC_CACHE_DIR": str(self.cache_dir),
            "TMPDIR": str(self.settings.tmp_dir),
            "RESTIC_PASSWORD": self.settings.backup_password or "",
        }
        if target == "offsite":
            for name in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE"):
                if os.environ.get(name):
                    env[name] = os.environ[name]
            env.update(self.settings.backup_offsite_env)
            env["RESTIC_REPOSITORY"] = self.settings.backup_offsite_repository or ""
        else:
            env["RESTIC_REPOSITORY"] = str(self.local_repo)
        return env

    def _restic(self, target: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        if not self.restic:
            raise BackupError(_("restic ist nicht installiert."), code="backup_unavailable")
        result = subprocess.run(  # noqa: S603 - feste Programmdatei, keine Shell
            [self.restic, *args],
            env=self._env(target),
            cwd=self.settings.data_dir,
            capture_output=True,
            text=True,
            timeout=RESTIC_TIMEOUT,
            check=False,
        )
        if check and result.returncode != 0:
            raise BackupError(
                _(
                    "restic %(command)s ist fehlgeschlagen: %(output)s",
                    command=args[0],
                    output=_sanitize(result.stderr or result.stdout),
                )
            )
        return result

    def _ensure_repo(self, target: str) -> None:
        if target == "local":
            self.local_repo.parent.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if self._restic(target, "cat", "config", check=False).returncode != 0:
            self._restic(target, "init")
            log.info("Backup-Repository (%s) wurde angelegt.", target)

    def _require_enabled(self) -> None:
        if not self.restic:
            raise BackupError(_("restic ist nicht installiert."), code="backup_unavailable")
        if not self.settings.backup_password:
            raise BackupError(
                _("Backups sind nicht eingerichtet: BACKUP_PASSWORD fehlt."),
                code="backup_unconfigured",
            )

    def _target_check(self, target: str) -> None:
        if target not in ("local", "offsite"):
            raise BackupError(_("Unbekanntes Backup-Ziel."), code="backup_target")
        if target == "offsite" and not self.offsite_configured:
            raise BackupError(
                _("Es ist kein externes Backup-Ziel eingerichtet."), code="backup_unconfigured"
            )

    # ------------------------------------------------------------------
    # Backup
    # ------------------------------------------------------------------

    def _snapshot_database(self) -> Path:
        """Konsistente Kopie der Datenbank über die SQLite-Backup-API."""
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        temp = self.staging_dir / f"{STAGING_DB}.tmp"
        final = self.staging_dir / STAGING_DB
        with contextlib.suppress(OSError):
            temp.unlink()
        source = sqlite3.connect(self.settings.db_path, timeout=60)
        target = sqlite3.connect(temp)
        try:
            source.backup(target)
            # Kurzlebige Daten gehören nicht ins Backup: Sitzungen, Client-Fingerabdrücke
            # und die für Wiederholungen verschlüsselt abgelegten Link-Codes.
            target.execute("PRAGMA secure_delete = ON")
            for table in ("sessions", "download_grants", "idempotency_keys"):
                target.execute(f"DELETE FROM {table}")  # noqa: S608 - feste Tabellennamen
            target.commit()
            target.execute("PRAGMA journal_mode = DELETE")
            target.execute("VACUUM")
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError(_("Die Datenbankkopie ist nicht konsistent."))
            target.commit()
        finally:
            target.close()
            source.close()
        os.replace(temp, final)
        return final

    def run(self, trigger: str = "manual") -> list[dict[str, Any]]:
        """Führt ein Backup aus (lokal und, falls eingerichtet, extern)."""
        self._require_enabled()
        if not self._lock.acquire(blocking=False):
            raise Conflict(_("Es läuft bereits ein Backup."), code="backup_running")
        results = []
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        # Dateisperre: schließt auch ein gleichzeitiges Backup aus einem zweiten Prozess
        # aus (Server und `ebookctl backup`).
        lock_file = open(self.settings.data_dir / "backup.lock", "w")  # noqa: SIM115
        try:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise Conflict(_("Es läuft bereits ein Backup."), code="backup_running") from exc
            self.storage.backup_running.set()
            try:
                self._snapshot_database()
                targets = ["local"] + (["offsite"] if self.offsite_configured else [])
                for target in targets:
                    results.append(self._run_target(target, trigger))
            finally:
                self.storage.backup_running.clear()
        finally:
            lock_file.close()
            self._lock.release()
        # Aufräumen, was während des Backups nicht gelöscht werden durfte.
        with contextlib.suppress(Exception):
            from .services.books import collect_orphans

            conn = connect(self.settings.db_path)
            try:
                collect_orphans(conn, self.storage)
            finally:
                conn.close()
        return results

    def _run_target(self, target: str, trigger: str) -> dict[str, Any]:
        conn = connect(self.settings.db_path)
        try:
            run_id = conn.execute(
                "INSERT INTO backup_runs (target, trigger, status, started_at)"
                " VALUES (?, ?, 'running', ?)",
                (target, trigger, now_iso()),
            ).lastrowid
            status, snapshot, detail = "ok", None, ""
            try:
                self._ensure_repo(target)
                result = self._restic(
                    target,
                    "backup",
                    "--json",
                    "--tag",
                    TAG,
                    "--host",
                    HOST,
                    str(self.staging_dir / STAGING_DB),
                    str(self.settings.books_dir),
                )
                summary = self._summary(result.stdout)
                snapshot = summary.get("snapshot_id")
                detail = _(
                    "%(files)s Dateien, %(bytes)s Bytes geprüft, %(added)s Bytes neu",
                    files=summary.get("total_files_processed", 0),
                    bytes=summary.get("total_bytes_processed", 0),
                    added=summary.get("data_added", 0),
                )
                forget = [
                    "forget",
                    "--prune",
                    "--tag",
                    TAG,
                    "--host",
                    HOST,
                    "--group-by",
                    "host,tags",
                    "--keep-daily",
                    str(self.settings.backup_keep_daily),
                ]
                if self.settings.backup_keep_weekly:
                    forget += ["--keep-weekly", str(self.settings.backup_keep_weekly)]
                if self.settings.backup_keep_monthly:
                    forget += ["--keep-monthly", str(self.settings.backup_keep_monthly)]
                self._restic(target, *forget)
            except (BackupError, subprocess.TimeoutExpired, OSError) as exc:
                status = "failed"
                detail = _sanitize(getattr(exc, "message", str(exc)))
                log.error("Backup (%s) fehlgeschlagen: %s", target, detail)
            conn.execute(
                "UPDATE backup_runs SET status = ?, snapshot_id = ?, detail = ?, finished_at = ?"
                " WHERE id = ?",
                (status, snapshot, detail, now_iso(), run_id),
            )
            if status == "ok":
                log.info("Backup (%s) abgeschlossen: Snapshot %s, %s", target, snapshot, detail)
            return {"target": target, "status": status, "snapshot_id": snapshot, "detail": detail}
        finally:
            conn.close()

    @staticmethod
    def _summary(stdout: str) -> dict[str, Any]:
        for line in reversed(stdout.splitlines()):
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("message_type") == "summary":
                return message
        return {}

    def snapshots(self, target: str = "local") -> list[dict[str, Any]]:
        self._require_enabled()
        self._target_check(target)
        result = self._restic(target, "snapshots", "--json", "--tag", TAG)
        return [
            {
                "id": item.get("short_id") or item.get("id", "")[:8],
                "time": item.get("time", ""),
                "paths": item.get("paths", []),
            }
            for item in json.loads(result.stdout or "[]")
        ]

    def verify(self, target: str = "local") -> str:
        """Prüft die Struktur des Repositories und liest alle Daten zur Kontrolle."""
        self._require_enabled()
        self._target_check(target)
        result = self._restic(target, "check", "--read-data")
        return _sanitize(result.stdout)

    # ------------------------------------------------------------------
    # Wiederherstellung
    # ------------------------------------------------------------------

    def restore(
        self, target: str = "local", snapshot: str = "latest", *, apply: bool = False
    ) -> dict[str, Any]:
        """Stellt einen Snapshot in ein Arbeitsverzeichnis wieder her und prüft ihn.

        Mit ``apply=True`` werden Datenbank und Bücher anschließend ersetzt. Der bisherige
        Stand bleibt unter ``pre-restore-<Zeit>`` im Datenverzeichnis erhalten.
        """
        self._require_enabled()
        self._target_check(target)
        if not re.fullmatch(r"latest|[0-9a-f]{8,64}", snapshot):
            raise BackupError(_("Ungültige Snapshot-Kennung."), code="backup_snapshot")
        stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
        work = self.settings.data_dir / "restore-tmp" / stamp
        work.mkdir(parents=True, exist_ok=False)
        try:
            args = ["restore", snapshot, "--target", str(work)]
            if snapshot == "latest":
                args += ["--tag", TAG, "--host", HOST]
            self._restic(target, *args)
            databases = sorted(work.rglob(f"backup-staging/{STAGING_DB}"))
            if not databases:
                raise BackupError(_("Der Snapshot enthält keine Datenbank."))
            restored_db = databases[0]
            restored_books = restored_db.parent.parent / "books"
            report = self._verify_restored(restored_db, restored_books)
            report.update({"target": target, "snapshot": snapshot, "applied": False})
            if not apply:
                return report
            if not report["ok"]:
                raise BackupError(
                    _(
                        "Die Wiederherstellung wurde nicht angewendet, weil die Prüfung "
                        "fehlgeschlagen ist: %(problems)s",
                        problems=report["problems"][:5],
                    )
                )
            report["previous_state"] = str(self._swap_in(restored_db, restored_books, stamp))
            report["applied"] = True
            return report
        finally:
            shutil.rmtree(work, ignore_errors=True)
            with contextlib.suppress(OSError):
                work.parent.rmdir()

    @staticmethod
    def _verify_restored(db_path: Path, books_dir: Path) -> dict[str, Any]:
        problems: list[str] = []
        files = 0
        total = 0
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                problems.append(_("Datenbank beschädigt: %(detail)s", detail=integrity))
            foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign:
                problems.append(_("%(count)s verletzte Fremdschlüssel", count=len(foreign)))
            rows = conn.execute(
                "SELECT DISTINCT storage_key, size_bytes, sha256 FROM edition_files"
            ).fetchall()
            covers = [
                row[0]
                for row in conn.execute("SELECT cover_key FROM books WHERE cover_key IS NOT NULL")
            ]
            counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
                for table in ("books", "editions", "edition_files", "links", "api_keys")
            }
        finally:
            conn.close()
        for row in rows:
            path = books_dir / row["storage_key"]
            if not path.is_file():
                problems.append(_("Datei fehlt: %(key)s", key=row["storage_key"]))
                continue
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            size = path.stat().st_size
            if size != row["size_bytes"] or digest.hexdigest() != row["sha256"]:
                problems.append(_("Prüfsumme stimmt nicht: %(key)s", key=row["storage_key"]))
            files += 1
            total += size
        for key in covers:
            if not (books_dir / key).is_file():
                problems.append(_("Cover fehlt: %(key)s", key=key))
        return {
            "ok": not problems,
            "problems": problems,
            "files_verified": files,
            "bytes_verified": total,
            "covers": len(covers),
            "rows": counts,
        }

    def _swap_in(self, restored_db: Path, restored_books: Path, stamp: str) -> Path:
        """Ersetzt den aktuellen Stand. Währenddessen antwortet die App mit 503.

        Schlägt ein Schritt fehl, wird der vorherige Stand zurückgelegt.
        """
        settings = self.settings
        previous = settings.data_dir / f"pre-restore-{stamp}"
        previous.mkdir()
        moved: list[tuple[Path, Path]] = []  # (ursprünglicher Ort, Ablage)
        placed: list[Path] = []  # neu abgelegte, wiederhergestellte Daten
        settings.maintenance_flag.write_text(f"restore {stamp}\n", encoding="utf-8")
        try:
            time.sleep(1.5)  # laufende kurze Anfragen beenden lassen
            with contextlib.suppress(sqlite3.Error):
                conn = sqlite3.connect(settings.db_path, timeout=30)
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    conn.close()
            try:
                for suffix in ("", "-wal", "-shm"):
                    source = Path(f"{settings.db_path}{suffix}")
                    if source.exists():
                        os.replace(source, previous / source.name)
                        moved.append((source, previous / source.name))
                if settings.books_dir.exists():
                    os.replace(settings.books_dir, previous / "books")
                    moved.append((settings.books_dir, previous / "books"))
                os.replace(restored_db, settings.db_path)
                placed.append(settings.db_path)
                if restored_books.exists():
                    os.replace(restored_books, settings.books_dir)
                else:
                    settings.books_dir.mkdir(parents=True, exist_ok=True)
                placed.append(settings.books_dir)
                conn = sqlite3.connect(settings.db_path)
                try:
                    conn.execute("PRAGMA journal_mode = WAL")
                finally:
                    conn.close()
            except BaseException:
                log.exception(
                    "Wiederherstellung fehlgeschlagen; der vorherige Stand wird zurückgelegt"
                )
                for path in placed:
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        for suffix in ("", "-wal", "-shm"):
                            with contextlib.suppress(OSError):
                                Path(f"{path}{suffix}").unlink()
                for original, stored in reversed(moved):
                    os.replace(stored, original)
                with contextlib.suppress(OSError):
                    previous.rmdir()
                raise
        finally:
            with contextlib.suppress(OSError):
                settings.maintenance_flag.unlink()
        log.warning("Wiederherstellung angewendet. Vorheriger Stand liegt unter %s", previous)
        return previous

    # ------------------------------------------------------------------
    # Zeitplan
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._mark_interrupted()
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _mark_interrupted(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            conn = connect(self.settings.db_path)
            try:
                conn.execute(
                    "UPDATE backup_runs SET status = 'failed', finished_at = ?,"
                    " detail = ? WHERE status = 'running'",
                    (now_iso(), _("Durch einen Neustart unterbrochen")),
                )
            finally:
                conn.close()

    def _loop(self) -> None:
        while not self._stop.wait(60):
            try:
                self.tick()
            except Exception:
                log.exception("Fehler im Zeitplan")

    def tick(self) -> None:
        if self.settings.maintenance_flag.exists():
            return
        if time.monotonic() - self._last_housekeeping > 3600:
            self._last_housekeeping = time.monotonic()
            from .services.misc import housekeeping

            conn = connect(self.settings.db_path)
            try:
                housekeeping(conn, self.settings)
            finally:
                conn.close()
            self.storage.clean_tmp()
        if self.enabled and self.due():
            self.run("schedule")

    def due(self, now: datetime | None = None) -> bool:
        """Täglich ab der eingestellten Stunde; nach einem Fehlschlag frühestens nach 60 min."""
        now = now or now_utc()
        local = now.astimezone(self.settings.timezone)
        if local.hour < self.settings.backup_hour:
            return False
        conn = connect(self.settings.db_path)
        try:
            last = conn.execute(
                "SELECT * FROM backup_runs WHERE target = 'local' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            last_ok = conn.execute(
                "SELECT * FROM backup_runs WHERE target = 'local' AND status = 'ok'"
                " ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if last_ok is not None:
            ok_local = parse_iso(last_ok["started_at"]).astimezone(self.settings.timezone)
            if ok_local.date() == local.date() and ok_local.hour >= self.settings.backup_hour:
                return False
        return last is None or now - parse_iso(last["started_at"]) >= timedelta(minutes=60)
