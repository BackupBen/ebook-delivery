"""Gemeinsame Bausteine der Web-Schicht: Kontext, Datenbankzugriff, Client-Erkennung."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from starlette.requests import Request

from ..backup import BackupManager
from ..config import Settings
from ..db import connect
from ..errors import AppError, Forbidden
from ..ratelimit import RateLimiter
from ..schemas import validation_fields
from ..security import normalize_ip
from ..storage import Storage


@dataclass
class AppContext:
    settings: Settings
    storage: Storage
    limiter: RateLimiter
    backups: BackupManager


def ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Eine Datenbankverbindung pro Anfrage."""
    conn = connect(ctx(request).settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _is_trusted(settings: Settings, address: str) -> bool:
    ip = normalize_ip(address)
    if ip is None:
        return False
    return any(
        ip in network for network in settings.trusted_proxies if ip.version == network.version
    )


def _via_trusted_proxy(request: Request, settings: Settings) -> bool:
    return bool(request.client) and _is_trusted(settings, request.client.host)


def client_ip(request: Request, settings: Settings) -> str:
    """Ermittelt die Client-Adresse.

    X-Forwarded-For wird nur ausgewertet, wenn die Anfrage von einem vertrauenswürdigen
    Proxy kommt. Dann zählt der letzte Eintrag, der selbst kein vertrauenswürdiger Proxy
    ist; vom Client frei gesetzte Einträge weiter links werden ignoriert. Mehrere
    Header-Zeilen werden in ihrer Reihenfolge zusammengeführt.
    """
    peer = request.client.host if request.client else "unknown"
    if not _via_trusted_proxy(request, settings):
        return _canonical(peer)
    forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
    for candidate in reversed([part.strip() for part in forwarded.split(",") if part.strip()]):
        if not _is_trusted(settings, candidate):
            address = normalize_ip(candidate)
            return str(address) if address is not None else _canonical(peer)
    return _canonical(peer)


def _canonical(address: str) -> str:
    normalized = normalize_ip(address)
    return str(normalized) if normalized is not None else address


def request_scheme(request: Request, settings: Settings) -> str:
    if _via_trusted_proxy(request, settings):
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if proto in {"http", "https"}:
            return proto
    return request.url.scheme


def base_url(request: Request, settings: Settings) -> str:
    """Öffentliche Basis-URL. Maßgeblich ist PUBLIC_BASE_URL, sonst die Anfrage."""
    if settings.public_base_url:
        return settings.public_base_url
    host = request.headers.get("host", request.url.netloc)
    return f"{request_scheme(request, settings)}://{host}"


def check_same_origin(request: Request, settings: Settings) -> None:
    """CSRF-Schutz, erste Stufe: schreibende Anfragen müssen von der eigenen Seite kommen."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in {"same-origin", "none"}:
        raise Forbidden("Anfrage von einer fremden Seite abgelehnt.", code="csrf_origin")
    origin = request.headers.get("origin")
    if origin is not None and origin != base_url(request, settings):
        raise Forbidden("Anfrage von einer fremden Seite abgelehnt.", code="csrf_origin")


@dataclass
class ErrorInfo:
    """Fehler in einer Form, die Vorlagen dauerhaft und kopierbar anzeigen können."""

    message: str
    code: str
    fields: list[dict[str, str]]
    status_code: int

    @property
    def by_field(self) -> dict[str, str]:
        return {item["field"]: item["message"] for item in self.fields}


def error_info(exc: Exception) -> ErrorInfo:
    if isinstance(exc, ValidationError):
        fields = validation_fields(exc)
        return ErrorInfo(
            message="Bitte prüfe die markierten Angaben.",
            code="validation_error",
            fields=fields,
            status_code=422,
        )
    if isinstance(exc, AppError):
        return ErrorInfo(
            message=exc.message, code=exc.code, fields=exc.fields, status_code=exc.status_code
        )
    raise exc


def form_text(form: Any, name: str, default: str = "") -> str:
    value = form.get(name, default)
    return value if isinstance(value, str) else default
