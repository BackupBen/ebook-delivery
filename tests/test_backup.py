"""Backups mit restic: Sicherung, Prüfung, tatsächliche Wiederherstellung, Zeitplan."""

from __future__ import annotations

import shutil
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ebookapp.backup import BackupError
from ebookapp.db import iso
from tests.conftest import Env, make_epub, make_pdf, make_png

pytestmark = pytest.mark.skipif(shutil.which("restic") is None, reason="restic nicht installiert")


@pytest.fixture
def backup_env(make_env) -> Env:
    return make_env(BACKUP_PASSWORD="ein-backup-passwort-fuer-tests")


def _populate(env: Env):
    admin = env.login()
    pdf, epub = make_pdf("Sicherung", 200_000), make_epub("Sicherung")
    book_id = admin.published_book("Gesichertes Buch", pdf, epub)
    admin.post(f"/admin/books/{book_id}/cover", files={"cover": ("c.png", make_png(), "image/png")})
    link_id, path = admin.create_link(book_id, label="vor dem Backup", max_downloads="9")
    env.new_client().get(f"{path}/pdf")
    return admin, book_id, link_id, path, pdf, epub


def test_backup_restore_roundtrip(backup_env: Env) -> None:
    env = backup_env
    backups = env.app.state.ctx.backups
    admin, book_id, link_id, path, pdf, epub = _populate(env)
    api_key = env.api_key(["books:read"])

    results = backups.run("manual")
    assert [item["target"] for item in results] == ["local"]
    assert results[0]["status"] == "ok", results
    assert results[0]["snapshot_id"]
    assert (env.settings.backup_dir / "restic" / "config").is_file()
    assert len(backups.snapshots()) == 1
    assert "no errors were found" in backups.verify()

    # Nach dem Backup: Daten gehen verloren bzw. werden verändert.
    late_book = admin.published_book("Nach dem Backup")
    admin.post(f"/admin/links/{link_id}/revoke", {"confirm": "1"})
    admin.post(f"/admin/books/{book_id}/delete", {"confirm": "1", "expected_link_count": "1"})
    assert env.new_client().get(path).status_code == 404
    assert not list((env.settings.books_dir / book_id).glob("*"))

    # Probelauf: prüft den Snapshot, verändert aber nichts.
    report = backups.restore("local", "latest", apply=False)
    assert report["ok"] is True
    assert report["applied"] is False
    assert report["files_verified"] == 2
    assert report["bytes_verified"] == len(pdf) + len(epub)
    assert report["rows"]["books"] == 1
    assert env.new_client().get(path).status_code == 404
    assert not (env.settings.data_dir / "restore-tmp").exists()

    # Tatsächliche Wiederherstellung
    report = backups.restore("local", "latest", apply=True)
    assert report["applied"] is True
    assert not env.settings.maintenance_flag.exists()

    buyer = env.new_client()
    page = buyer.get(path)
    assert page.status_code == 200
    assert "Gesichertes Buch" in page.text
    assert buyer.get(f"{path}/pdf").content == pdf
    assert buyer.get(f"{path}/epub").content == epub
    assert buyer.get(f"{path}/cover").status_code == 200

    with env.db() as conn:
        link = conn.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
        assert link["status"] == "active"
        assert link["label"] == "vor dem Backup"
        assert link["max_downloads"] == 9
        assert link["download_count"] >= 1
        assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    api = env.new_client()
    api.headers["Authorization"] = f"Bearer {api_key}"
    assert [b["id"] for b in api.get("/api/v1/books").json()["items"]] == [book_id]

    # Der Stand vor der Wiederherstellung bleibt zur Sicherheit erhalten.
    previous = list(env.settings.data_dir.glob("pre-restore-*"))
    assert len(previous) == 1
    assert (previous[0] / "app.sqlite3").is_file()
    assert (previous[0] / "books" / late_book).is_dir()

    # Anmeldung und weitere Arbeit funktionieren nach der Wiederherstellung.
    fresh = env.login(env.new_client())
    assert fresh.create_book("Nach der Wiederherstellung")


def test_restore_detects_damaged_snapshot(backup_env: Env) -> None:
    env = backup_env
    backups = env.app.state.ctx.backups
    _, book_id, *_ = _populate(env)
    # Eine Buchdatei fehlt bereits beim Backup: Der Snapshot ist unvollständig.
    victim = next((env.settings.books_dir / book_id).glob("*.pdf"))
    victim.unlink()
    assert backups.run("manual")[0]["status"] == "ok"

    report = backups.restore("local", "latest", apply=False)
    assert report["ok"] is False
    assert any("Datei fehlt" in problem for problem in report["problems"])
    with pytest.raises(BackupError, match="nicht angewendet"):
        backups.restore("local", "latest", apply=True)
    assert not list(env.settings.data_dir.glob("pre-restore-*"))  # nichts ersetzt
    assert not env.settings.maintenance_flag.exists()


def test_backup_is_consistent_while_writing(backup_env: Env) -> None:
    """Das Backup nutzt eine Datenbankkopie; Löschungen warten bis nach dem Backup."""
    env = backup_env
    context = env.app.state.ctx
    _, book_id, *_ = _populate(env)
    key = next((env.settings.books_dir / book_id).glob("*.pdf"))

    context.storage.backup_running.set()
    try:
        context.storage.delete(f"{book_id}/{key.name}")
        assert key.exists()  # während des Backups wird nichts gelöscht
    finally:
        context.storage.backup_running.clear()
    assert context.backups.run("manual")[0]["status"] == "ok"
    staged = env.settings.data_dir / "backup-staging" / "db.sqlite3"
    assert staged.is_file()
    assert not staged.with_name("db.sqlite3-wal").exists()


def test_retention_and_multiple_snapshots(make_env) -> None:
    env = make_env(
        BACKUP_PASSWORD="ein-backup-passwort-fuer-tests",
        BACKUP_KEEP_DAILY="2",
        BACKUP_KEEP_WEEKLY="0",
        BACKUP_KEEP_MONTHLY="0",
    )
    backups = env.app.state.ctx.backups
    _populate(env)
    for _ in range(4):
        assert backups.run("manual")[0]["status"] == "ok"
    # Mehrere Snapshots desselben Tages zählen als ein täglicher Stand. restic behält
    # zusätzlich den ältesten Snapshot, solange es weniger Tage als "keep-daily" gibt.
    assert len(backups.snapshots()) == 2
    with env.db() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM backup_runs WHERE status = 'ok'").fetchone()[0] == 4
        )


def test_backup_disabled_without_password(env: Env, admin) -> None:
    backups = env.app.state.ctx.backups
    assert backups.enabled is False
    with pytest.raises(BackupError, match="BACKUP_PASSWORD"):
        backups.run("manual")
    page = env.client.get("/admin/settings").text
    assert "Backups sind nicht aktiv" in page
    response = admin.post("/admin/settings/backup/run")
    assert response.status_code == 500
    assert "BACKUP_PASSWORD" in response.text


def test_gui_backup_and_missing_offsite_warning(backup_env: Env) -> None:
    env = backup_env
    admin, *_ = _populate(env)
    page = env.client.get("/admin/settings").text
    assert "Kein externes Backup-Ziel eingerichtet" in page
    assert "noch keines" in page
    assert admin.post("/admin/settings/backup/run").status_code == 303
    page = env.client.get("/admin/settings").text
    assert "Backup abgeschlossen." in page
    assert "erfolgreich" in page
    verify = admin.post("/admin/settings/backup/verify", {"target": "local"})
    assert verify.status_code == 200
    assert "no errors were found" in verify.text
    # Ein externes Ziel wird nie stillschweigend verwendet.
    offsite = admin.post("/admin/settings/backup/verify", {"target": "offsite"})
    assert offsite.status_code == 500
    assert "kein externes Backup-Ziel" in offsite.text


def test_offsite_target_is_used_only_when_configured(make_env, tmp_path) -> None:
    offsite_repo = tmp_path / "anderer-ort"
    env = make_env(
        BACKUP_PASSWORD="ein-backup-passwort-fuer-tests",
        BACKUP_OFFSITE_REPOSITORY=str(offsite_repo),
        BACKUP_OFFSITE_PASSWORD="eigenes-passwort-extern",
    )
    backups = env.app.state.ctx.backups
    _, _, _, path, pdf, _ = _populate(env)
    results = backups.run("manual")
    assert [(item["target"], item["status"]) for item in results] == [
        ("local", "ok"),
        ("offsite", "ok"),
    ]
    assert (offsite_repo / "config").is_file()
    assert len(backups.snapshots("offsite")) == 1

    # Totalverlust: Datenvolume UND lokales Backup sind weg. Wiederherstellung von extern.
    shutil.rmtree(env.settings.books_dir)
    shutil.rmtree(env.settings.backup_dir / "restic")
    env.settings.books_dir.mkdir()
    report = backups.restore("offsite", "latest", apply=True)
    assert report["applied"] is True
    assert env.new_client().get(f"{path}/pdf").content == pdf
    assert "anderer-ort" in env.login(env.new_client()).client.get("/admin/settings").text


def test_failed_backup_is_recorded(make_env, tmp_path) -> None:
    env = make_env(
        BACKUP_PASSWORD="ein-backup-passwort-fuer-tests",
        BACKUP_OFFSITE_REPOSITORY="rest:https://benutzer:geheimnis@127.0.0.1:1/repo",
    )
    backups = env.app.state.ctx.backups
    _populate(env)
    results = backups.run("manual")
    assert results[0]["status"] == "ok"
    assert results[1]["status"] == "failed"
    assert "geheimnis" not in results[1]["detail"]
    assert "geheimnis" not in (backups.offsite_display() or "")
    with env.db() as conn:
        row = conn.execute(
            "SELECT * FROM backup_runs WHERE target = 'offsite' ORDER BY id DESC"
        ).fetchone()
    assert row["status"] == "failed"
    assert "geheimnis" not in row["detail"]


def test_schedule(make_env) -> None:
    env = make_env(BACKUP_PASSWORD="ein-backup-passwort-fuer-tests", BACKUP_HOUR="3")
    backups = env.app.state.ctx.backups
    tz = ZoneInfo("Europe/Berlin")

    def at(day: int, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 10, day, hour, minute, tzinfo=tz)

    assert backups.due(at(5, 2, 59)) is False  # vor der eingestellten Stunde
    assert backups.due(at(5, 3, 0)) is True  # noch nie gesichert

    def record(status: str, when: datetime) -> None:
        with env.db() as conn:
            conn.execute(
                "INSERT INTO backup_runs (target, trigger, status, started_at) "
                "VALUES ('local', 'schedule', ?, ?)",
                (status, iso(when)),
            )

    record("ok", at(5, 3, 1))
    assert backups.due(at(5, 3, 2)) is False
    assert backups.due(at(5, 23, 0)) is False  # heute bereits erfolgreich
    assert backups.due(at(6, 2, 0)) is False
    assert backups.due(at(6, 3, 0)) is True  # nächster Tag

    record("failed", at(6, 3, 1))
    assert backups.due(at(6, 3, 30)) is False  # nach Fehlschlag eine Stunde warten
    assert backups.due(at(6, 4, 2)) is True

    # Ein manuelles Backup vor der Zeitplan-Stunde ersetzt den täglichen Lauf nicht.
    record("ok", at(7, 1, 0))
    assert backups.due(at(7, 3, 0)) is True


def test_interrupted_runs_are_marked_on_start(make_env) -> None:
    env = make_env(BACKUP_PASSWORD="ein-backup-passwort-fuer-tests")
    with env.db() as conn:
        conn.execute(
            "INSERT INTO backup_runs (target, trigger, status, started_at) "
            "VALUES ('local', 'schedule', 'running', '2026-01-01T00:00:00Z')"
        )
    backups = env.app.state.ctx.backups
    backups.start()
    backups.stop()
    with env.db() as conn:
        row = conn.execute("SELECT * FROM backup_runs").fetchone()
    assert row["status"] == "failed"
    assert "unterbrochen" in row["detail"]
