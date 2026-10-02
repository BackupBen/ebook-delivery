"""Dateiablage außerhalb jedes Webverzeichnisses.

Dateien erhalten serverseitig erzeugte Zufallsnamen und werden nach dem Schreiben nie
verändert. Das macht Sicherungen einfach: bestehende Dateien ändern sich nicht.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .errors import TooLarge

CHUNK = 1024 * 1024


@dataclass(frozen=True)
class StoredTemp:
    path: Path
    size: int
    sha256: str


class FileFlag:
    """Markierung als Datei, damit sie auch zwischen Prozessen gilt.

    Ein Backup kann im Serverprozess oder über ``ebookctl backup`` in einem zweiten
    Prozess laufen. Beide müssen sehen, dass gerade gesichert wird. Eine liegen gebliebene
    Markierung (z. B. nach einem Absturz) verfällt nach ``max_age`` Sekunden.
    """

    def __init__(self, path: Path, max_age: float = 12 * 3600) -> None:
        self.path = path
        self.max_age = max_age

    def set(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    def clear(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink()

    def is_set(self) -> bool:
        try:
            return time.time() - self.path.stat().st_mtime < self.max_age
        except OSError:
            return False


class Storage:
    def __init__(self, books_dir: Path, tmp_dir: Path) -> None:
        self.books_dir = books_dir
        self.tmp_dir = tmp_dir
        # Während ein Backup läuft, werden Dateien nicht physisch gelöscht; die
        # Bereinigung (collect_orphans) holt das danach nach.
        self.backup_running = FileFlag(books_dir.parent / "backup-running")

    def ensure(self) -> None:
        for directory in (self.books_dir, self.tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                directory.chmod(0o700)

    def clean_tmp(self, older_than_seconds: float = 3600) -> None:
        cutoff = time.time() - older_than_seconds
        for entry in self.tmp_dir.glob("*"):
            with contextlib.suppress(OSError):
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()

    def write_temp(self, source: BinaryIO, max_bytes: int, too_large_message: str) -> StoredTemp:
        """Schreibt einen Upload in eine temporäre Datei und begrenzt dabei die Größe."""
        path = self.tmp_dir / f"upload-{secrets.token_hex(16)}.part"
        digest = hashlib.sha256()
        size = 0
        try:
            with open(path, "xb") as target:
                os.chmod(path, 0o600)
                while True:
                    chunk = source.read(CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise TooLarge(too_large_message)
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                path.unlink()
            raise
        return StoredTemp(path=path, size=size, sha256=digest.hexdigest())

    def commit(self, temp: Path, book_id: str, extension: str) -> str:
        """Verschiebt eine geprüfte temporäre Datei an ihren endgültigen Ort."""
        directory = self.books_dir / book_id
        directory.mkdir(parents=True, exist_ok=True)
        key = f"{book_id}/{secrets.token_hex(16)}{extension}"
        final = self.books_dir / key
        os.replace(temp, final)
        os.chmod(final, 0o600)
        self._fsync_dir(directory)
        return key

    def write_bytes(self, book_id: str, data: bytes, extension: str, prefix: str = "") -> str:
        """Speichert kleine, bereits geprüfte Daten (z. B. ein neu kodiertes Cover)."""
        temp = self.tmp_dir / f"upload-{secrets.token_hex(16)}.part"
        with open(temp, "xb") as target:
            os.chmod(temp, 0o600)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        directory = self.books_dir / book_id
        directory.mkdir(parents=True, exist_ok=True)
        key = f"{book_id}/{prefix}{secrets.token_hex(16)}{extension}"
        os.replace(temp, self.books_dir / key)
        self._fsync_dir(directory)
        return key

    def path(self, key: str) -> Path:
        """Löst einen Speicherschlüssel auf und verhindert Zugriffe außerhalb der Ablage."""
        root = self.books_dir.resolve()
        candidate = (root / key).resolve()
        if root not in candidate.parents:
            raise ValueError("Ungültiger Speicherschlüssel")
        return candidate

    def exists(self, key: str) -> bool:
        try:
            return self.path(key).is_file()
        except ValueError:
            return False

    def delete(self, key: str) -> None:
        if self.backup_running.is_set():
            return
        with contextlib.suppress(OSError, ValueError):
            self.path(key).unlink()

    def remove_book_dir(self, book_id: str) -> None:
        with contextlib.suppress(OSError, ValueError):
            self.path(f"{book_id}/x").parent.rmdir()

    def discard(self, temp: Path) -> None:
        with contextlib.suppress(OSError):
            temp.unlink()

    def all_keys(self) -> list[tuple[str, float]]:
        """Alle vorhandenen Dateien als (Schlüssel, Änderungszeit)."""
        result = []
        root = self.books_dir
        for path in root.rglob("*"):
            if path.is_file():
                with contextlib.suppress(OSError):
                    result.append((path.relative_to(root).as_posix(), path.stat().st_mtime))
        return result

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        with contextlib.suppress(OSError):
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
