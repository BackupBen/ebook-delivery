"""Konfiguration ausschließlich über Umgebungsvariablen.

Im Code und im Image sind keine Servernamen, Domains oder Secrets hinterlegt.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MB = 1024 * 1024

DEFAULT_TRUSTED_PROXIES = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"


class ConfigError(RuntimeError):
    """Ungültige oder fehlende Konfiguration."""


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "ja"}


def _int(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} muss eine ganze Zahl sein.") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} muss zwischen {minimum} und {maximum} liegen.")
    return value


def _networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    result = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(ipaddress.ip_network(part, strict=False))
        except ValueError as exc:
            raise ConfigError(f"TRUSTED_PROXIES enthält einen ungültigen Eintrag: {part}") from exc
    return tuple(result)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    backup_dir: Path
    secret_key: bytes
    public_base_url: str | None
    allowed_hosts: frozenset[str]
    cookie_secure: bool
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    admin_username: str
    admin_password: str | None
    timezone: ZoneInfo
    timezone_name: str
    port: int
    log_level: str

    # Uploads
    max_upload_bytes: int
    default_max_pdf_mb: int
    default_max_epub_mb: int
    default_max_cover_mb: int
    epub_max_uncompressed_bytes: int
    epub_max_entries: int
    epub_max_ratio: int

    # Sitzungen
    session_idle_minutes: int
    session_max_hours: int

    # Downloads
    download_window_minutes: int
    download_window_max_hours: int

    # Rate Limits
    login_max_failures: int
    login_window_minutes: int
    api_rate_per_minute: int
    download_rate_per_minute: int
    invalid_link_max: int
    invalid_link_window_minutes: int

    # Backups
    backup_password: str | None
    backup_hour: int
    backup_keep_daily: int
    backup_keep_weekly: int
    backup_keep_monthly: int
    backup_offsite_repository: str | None
    backup_offsite_env: dict[str, str] = field(default_factory=dict, repr=False)

    # Whop und E-Mail-Versand (Brevo)
    whop_webhook_secret: str | None = field(default=None, repr=False)
    brevo_api_key: str | None = field(default=None, repr=False)
    brevo_api_url: str = "https://api.brevo.com/v3/smtp/email"
    mail_from_email: str | None = None
    mail_from_name: str = ""
    mail_reply_to: str | None = None

    @property
    def mail_configured(self) -> bool:
        return bool(self.brevo_api_key and self.mail_from_email)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.sqlite3"

    @property
    def books_dir(self) -> Path:
        return self.data_dir / "books"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def maintenance_flag(self) -> Path:
        return self.data_dir / "MAINTENANCE"

    @property
    def public_origin(self) -> str | None:
        if not self.public_base_url:
            return None
        parts = urlsplit(self.public_base_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def session_cookie_name(self) -> str:
        # Das Präfix __Host- erzwingt Secure, Path=/ und verhindert Domain-Cookies.
        return "__Host-ebook_session" if self.cookie_secure else "ebook_session"

    @property
    def login_csrf_cookie_name(self) -> str:
        return "__Host-ebook_login" if self.cookie_secure else "ebook_login"


# Zugangsdaten, die restic für externe Ziele versteht. Sie werden nur an den
# restic-Prozess für das externe Ziel weitergereicht.
OFFSITE_ENV_PREFIXES = ("AWS_", "B2_", "RESTIC_REST_", "AZURE_", "GOOGLE_", "OS_", "ST_")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    data_dir = Path(env.get("DATA_DIR", "/data")).resolve()
    backup_dir = Path(env.get("BACKUP_DIR", "/backups")).resolve()

    secret = env.get("SECRET_KEY", "")
    if len(secret) < 32:
        raise ConfigError(
            "SECRET_KEY fehlt oder ist zu kurz. Es werden mindestens 32 zufällige Zeichen "
            "benötigt (z. B. erzeugt mit: openssl rand -base64 48)."
        )

    public_base_url = env.get("PUBLIC_BASE_URL", "").strip().rstrip("/") or None
    hosts: set[str] = {"localhost", "127.0.0.1", "[::1]"}
    if public_base_url:
        parts = urlsplit(public_base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ConfigError("PUBLIC_BASE_URL muss mit http:// oder https:// beginnen.")
        if parts.path not in {"", "/"} or parts.query or parts.fragment:
            raise ConfigError("PUBLIC_BASE_URL darf keinen Pfad enthalten.")
        hosts.add(parts.hostname.lower())
    for extra in env.get("ALLOWED_HOSTS", "").split(","):
        if extra.strip():
            hosts.add(extra.strip().lower())
    # Ohne PUBLIC_BASE_URL kann der Host nicht geprüft werden (nur für lokale Tests gedacht).
    allowed_hosts = frozenset(hosts) if public_base_url else frozenset({"*"})

    cookie_default = not (public_base_url or "").startswith("http://")
    cookie_secure = _bool(env.get("COOKIE_SECURE"), cookie_default)

    tz_name = env.get("APP_TIMEZONE", "Europe/Berlin").strip() or "Europe/Berlin"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"APP_TIMEZONE ist unbekannt: {tz_name}") from exc

    max_upload_mb = _int(env, "MAX_UPLOAD_MB", 512, 1, 8192)

    offsite_repo = env.get("BACKUP_OFFSITE_REPOSITORY", "").strip() or None
    offsite_env = {
        key: value for key, value in env.items() if key.startswith(OFFSITE_ENV_PREFIXES) and value
    }
    offsite_password = env.get("BACKUP_OFFSITE_PASSWORD", "").strip()
    if offsite_password:
        offsite_env["RESTIC_PASSWORD"] = offsite_password

    return Settings(
        data_dir=data_dir,
        backup_dir=backup_dir,
        secret_key=secret.encode("utf-8"),
        public_base_url=public_base_url,
        allowed_hosts=allowed_hosts,
        cookie_secure=cookie_secure,
        trusted_proxies=_networks(env.get("TRUSTED_PROXIES", DEFAULT_TRUSTED_PROXIES)),
        admin_username=(env.get("ADMIN_USERNAME", "admin").strip() or "admin"),
        admin_password=env.get("ADMIN_PASSWORD") or None,
        timezone=tz,
        timezone_name=tz_name,
        port=_int(env, "PORT", 8000, 1, 65535),
        log_level=(env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"),
        max_upload_bytes=max_upload_mb * MB,
        default_max_pdf_mb=min(_int(env, "MAX_PDF_MB", 300, 1, 8192), max_upload_mb),
        default_max_epub_mb=min(_int(env, "MAX_EPUB_MB", 100, 1, 8192), max_upload_mb),
        default_max_cover_mb=min(_int(env, "MAX_COVER_MB", 10, 1, 100), max_upload_mb),
        epub_max_uncompressed_bytes=_int(env, "EPUB_MAX_UNCOMPRESSED_MB", 1024, 1, 16384) * MB,
        epub_max_entries=_int(env, "EPUB_MAX_ENTRIES", 10000, 1, 200000),
        epub_max_ratio=_int(env, "EPUB_MAX_RATIO", 200, 2, 10000),
        session_idle_minutes=_int(env, "SESSION_IDLE_MINUTES", 120, 5, 1440),
        session_max_hours=_int(env, "SESSION_MAX_HOURS", 24, 1, 720),
        download_window_minutes=_int(env, "DOWNLOAD_WINDOW_MINUTES", 60, 1, 1440),
        download_window_max_hours=_int(env, "DOWNLOAD_WINDOW_MAX_HOURS", 24, 1, 168),
        login_max_failures=_int(env, "LOGIN_MAX_FAILURES", 5, 1, 100),
        login_window_minutes=_int(env, "LOGIN_WINDOW_MINUTES", 15, 1, 1440),
        api_rate_per_minute=_int(env, "API_RATE_PER_MINUTE", 300, 1, 100000),
        download_rate_per_minute=_int(env, "DOWNLOAD_RATE_PER_MINUTE", 240, 1, 100000),
        invalid_link_max=_int(env, "INVALID_LINK_MAX", 20, 1, 10000),
        invalid_link_window_minutes=_int(env, "INVALID_LINK_WINDOW_MINUTES", 10, 1, 1440),
        backup_password=env.get("BACKUP_PASSWORD") or None,
        backup_hour=_int(env, "BACKUP_HOUR", 3, 0, 23),
        backup_keep_daily=_int(env, "BACKUP_KEEP_DAILY", 7, 1, 365),
        backup_keep_weekly=_int(env, "BACKUP_KEEP_WEEKLY", 4, 0, 520),
        backup_keep_monthly=_int(env, "BACKUP_KEEP_MONTHLY", 6, 0, 240),
        backup_offsite_repository=offsite_repo,
        backup_offsite_env=offsite_env,
        whop_webhook_secret=env.get("WHOP_WEBHOOK_SECRET", "").strip() or None,
        brevo_api_key=env.get("BREVO_API_KEY", "").strip() or None,
        brevo_api_url=(
            env.get("BREVO_API_URL", "").strip() or "https://api.brevo.com/v3/smtp/email"
        ),
        mail_from_email=_email(env, "MAIL_FROM_EMAIL"),
        mail_from_name=env.get("MAIL_FROM_NAME", "").strip()[:100],
        mail_reply_to=_email(env, "MAIL_REPLY_TO"),
    )


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _email(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    if not value:
        return None
    if not _EMAIL_RE.match(value) or len(value) > 254:
        raise ConfigError(f"{name} ist keine gültige E-Mail-Adresse.")
    return value
