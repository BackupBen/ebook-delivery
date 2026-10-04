"""Kommandozeile: Server starten, Passwort setzen, Backups und Wiederherstellung."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.request

from .config import ConfigError, load_settings
from .db import migrate, open_db
from .security import MIN_PASSWORD_LENGTH


def _context():  # type: ignore[no-untyped-def]
    from .backup import BackupManager
    from .storage import Storage

    settings = load_settings()
    storage = Storage(settings.books_dir, settings.tmp_dir)
    storage.ensure()
    migrate(settings.db_path)
    return settings, storage, BackupManager(settings, storage)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .web.app import create_app

    settings = load_settings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=args.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        server_header=False,
        proxy_headers=False,
        timeout_keep_alive=30,
    )
    return 0


def cmd_healthcheck(args: argparse.Namespace) -> int:
    settings = load_settings()
    url = f"http://127.0.0.1:{settings.port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


def cmd_set_password(args: argparse.Namespace) -> int:
    from .services import auth

    settings, _, _ = _context()
    username = args.username or settings.admin_username
    if sys.stdin.isatty():
        password = getpass.getpass("Neues Passwort: ")
        if password != getpass.getpass("Wiederholen: "):
            print("Die Passwörter stimmen nicht überein.", file=sys.stderr)
            return 1
    else:
        password = sys.stdin.readline().rstrip("\n")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"Das Passwort braucht mindestens {MIN_PASSWORD_LENGTH} Zeichen.", file=sys.stderr)
        return 1
    with open_db(settings.db_path) as conn:
        auth.set_password(conn, username, password)
    print(f"Passwort für „{username}“ gesetzt. Alle Sitzungen wurden beendet.")
    return 0


def cmd_disable_2fa(args: argparse.Namespace) -> int:
    """Notausgang, wenn Authenticator-App und Notfall-Codes verloren sind."""
    from .services import mfa, security_log

    settings, _, _ = _context()
    username = args.username or settings.admin_username
    with open_db(settings.db_path) as conn:
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            print(f"Benutzer „{username}“ nicht gefunden.", file=sys.stderr)
            return 1
        mfa.disable(conn, row["id"])
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        security_log.record(
            conn, settings, "mfa_disabled", username=username, detail="ebookctl disable-2fa"
        )
    print(
        f"Zwei-Faktor-Anmeldung für „{username}“ ausgeschaltet. Alle Sitzungen wurden beendet. "
        "Bitte nach der Anmeldung neu einrichten."
    )
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    _, _, backups = _context()
    results = backups.run("cli")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item["status"] == "ok" for item in results) else 1


def cmd_snapshots(args: argparse.Namespace) -> int:
    _, _, backups = _context()
    print(json.dumps(backups.snapshots(args.source), ensure_ascii=False, indent=2))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    _, _, backups = _context()
    print(backups.verify(args.source))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    _, _, backups = _context()
    report = backups.restore(args.source, args.snapshot, apply=args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        return 1
    if not args.apply:
        print(
            "\nNur geprüft, nichts verändert. Mit --apply wird der Stand ersetzt.",
            file=sys.stderr,
        )
    else:
        print(
            "\nWiederhergestellt. Bitte beachten: Der Stand entspricht dem Zeitpunkt des "
            "Backups. Danach erstellte Bücher und Links fehlen, und danach widerrufene "
            "Links oder API-Schlüssel sind wieder aktiv. Bitte prüfen und bei Bedarf erneut "
            "widerrufen. Alle Sitzungen wurden beendet.",
            file=sys.stderr,
        )
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    from .services.books import collect_orphans

    settings, storage, _ = _context()
    with open_db(settings.db_path) as conn:
        removed = collect_orphans(conn, storage)
    print(f"{len(removed)} verwaiste Dateien entfernt.")
    return 0


def cmd_openapi(args: argparse.Namespace) -> int:
    """Gibt die OpenAPI-Spezifikation aus. Benötigt weder Konfiguration noch Datenbank."""
    from .i18n import translate_openapi, untranslated_openapi

    spec = translate_openapi(untranslated_openapi())
    print(json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ebookctl", description="E-Book-Auslieferung")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Webserver starten")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - im Container gewollt
    serve.set_defaults(func=cmd_serve)

    sub.add_parser("healthcheck", help="Prüft den laufenden Server").set_defaults(
        func=cmd_healthcheck
    )

    password = sub.add_parser("set-password", help="Administrator-Passwort setzen")
    password.add_argument("--username")
    password.set_defaults(func=cmd_set_password)

    disable_2fa = sub.add_parser("disable-2fa", help="Zwei-Faktor-Anmeldung ausschalten (Notfall)")
    disable_2fa.add_argument("--username")
    disable_2fa.set_defaults(func=cmd_disable_2fa)

    sub.add_parser("backup", help="Backup jetzt ausführen").set_defaults(func=cmd_backup)

    for name, func, text in (
        ("snapshots", cmd_snapshots, "Vorhandene Snapshots auflisten"),
        ("backup-verify", cmd_verify, "Backup-Repository vollständig prüfen"),
    ):
        command = sub.add_parser(name, help=text)
        command.add_argument("--source", choices=("local", "offsite"), default="local")
        command.set_defaults(func=func)

    restore = sub.add_parser("restore", help="Snapshot prüfen und optional zurückspielen")
    restore.add_argument("--source", choices=("local", "offsite"), default="local")
    restore.add_argument("--snapshot", default="latest")
    restore.add_argument(
        "--apply", action="store_true", help="Aktuellen Stand durch den Snapshot ersetzen"
    )
    restore.set_defaults(func=cmd_restore)

    sub.add_parser("gc", help="Verwaiste Dateien entfernen").set_defaults(func=cmd_gc)
    sub.add_parser("openapi", help="OpenAPI-Spezifikation ausgeben").set_defaults(func=cmd_openapi)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        from .errors import AppError

        if isinstance(exc, AppError):
            print(f"Fehler: {exc.message}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
