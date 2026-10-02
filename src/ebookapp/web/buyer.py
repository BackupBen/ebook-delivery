"""Öffentliche Seiten: Käuferlink, Dateiauslieferung, Health-Check.

Es gibt keine öffentlichen Listen. Jede Datei-Anfrage wird serverseitig gegen den Link
geprüft; die Dateien selbst liegen außerhalb jedes Webverzeichnisses.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from ..db import open_db, parse_iso
from ..security import client_fingerprint, rate_key
from ..services import links
from ..validation import MEDIA_TYPES, download_filename
from .admin import render
from .common import client_ip, ctx
from .middleware import CSP_FILE

router = APIRouter(include_in_schema=False)

STATE_PAGES: dict[str, tuple[int, str, str]] = {
    "not_found": (
        404,
        "Link nicht gefunden",
        "Diesen Downloadlink gibt es nicht. Bitte prüfe, ob der Link vollständig kopiert wurde.",
    ),
    "disabled": (
        403,
        "Link derzeit deaktiviert",
        "Dieser Downloadlink ist im Moment deaktiviert. Bitte wende dich an den Verkäufer.",
    ),
    "revoked": (
        410,
        "Link widerrufen",
        "Dieser Downloadlink wurde widerrufen und kann nicht mehr verwendet werden. Bitte "
        "wende dich an den Verkäufer.",
    ),
    "expired": (
        410,
        "Link abgelaufen",
        "Die Gültigkeit dieses Downloadlinks ist abgelaufen. Bitte wende dich an den "
        "Verkäufer, wenn du einen neuen Link benötigst.",
    ),
    "exhausted": (
        410,
        "Downloadlimit erreicht",
        "Für diesen Link wurde die maximale Anzahl an Downloads erreicht. Ein bereits "
        "begonnener Download kann noch kurze Zeit fortgesetzt werden. Bitte wende dich an "
        "den Verkäufer, wenn du einen neuen Link benötigst.",
    ),
    "format_unavailable": (
        404,
        "Format nicht verfügbar",
        "Dieses Format ist über diesen Link nicht verfügbar.",
    ),
    "rate_limited": (
        429,
        "Zu viele Anfragen",
        "Von deinem Anschluss kamen zu viele Anfragen. Bitte warte einige Minuten und "
        "versuche es dann erneut.",
    ),
    "file_missing": (
        503,
        "Datei vorübergehend nicht verfügbar",
        "Die Datei kann gerade nicht bereitgestellt werden. Bitte versuche es später "
        "erneut oder wende dich an den Verkäufer.",
    ),
}


def state_page(request: Request, state: str, retry_after: int | None = None) -> Response:
    status, title, message = STATE_PAGES[state]
    response = render(
        request,
        "buyer/state.html",
        {"title": title, "message": message, "state": state},
        status_code=status,
    )
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
    return response


def _throttle(request: Request) -> Response | None:
    """Rate Limits gegen das Durchprobieren von Codes und gegen Anfragefluten."""
    c = ctx(request)
    settings = c.settings
    key = rate_key(client_ip(request, settings))
    wait = c.limiter.retry_after(
        f"bad-link:{key}", settings.invalid_link_max, settings.invalid_link_window_minutes * 60
    )
    if not wait:
        wait = c.limiter.hit(f"download:{key}", settings.download_rate_per_minute, 60)
    return state_page(request, "rate_limited", wait) if wait else None


def _unknown(request: Request) -> Response:
    c = ctx(request)
    key = rate_key(client_ip(request, c.settings))
    c.limiter.record(f"bad-link:{key}", c.settings.invalid_link_window_minutes * 60)
    return state_page(request, "not_found")


_RANGE = re.compile(r"^bytes=(\d+)-\d*$")


def _range_unsatisfiable(header: str | None, size: int) -> bool:
    """Erkennt eine einzelne Bereichsangabe, die hinter dem Dateiende beginnt."""
    match = _RANGE.match((header or "").strip())
    return bool(match) and int(match.group(1)) >= size


@router.get("/")
def home(request: Request) -> Response:
    return render(request, "buyer/home.html", {})


@router.get("/robots.txt")
def robots() -> Response:
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@router.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@router.get("/healthz")
def healthz(request: Request) -> Response:
    c = ctx(request)
    try:
        with open_db(c.settings.db_path) as conn:
            conn.execute("SELECT 1").fetchone()
    except sqlite3.Error:
        return JSONResponse({"status": "error"}, status_code=503)
    status = "maintenance" if c.settings.maintenance_flag.exists() else "ok"
    return JSONResponse({"status": status})


@router.get("/d/{code}")
def buyer_page(request: Request, code: str) -> Response:
    limited = _throttle(request)
    if limited is not None:
        return limited
    c = ctx(request)
    with open_db(c.settings.db_path) as conn:
        view = links.resolve(conn, code)
    if view is None:
        return _unknown(request)
    if view.state != "active":
        return state_page(request, view.state)
    files = [
        {
            "format": fmt,
            "label": fmt.upper(),
            "size_bytes": view.files[fmt]["size_bytes"],
            "url": f"/d/{code}/{fmt}",
            "hint": (
                "für Computer, Tablets und zum Drucken"
                if fmt == "pdf"
                else "für E-Book-Reader und Lese-Apps"
            ),
        }
        for fmt in ("pdf", "epub")
        if fmt in view.files
    ]
    context: dict[str, Any] = {
        "title": view.title,
        "description": view.description,
        "files": files,
        "cover_url": f"/d/{code}/cover" if view.cover_key else None,
        "expires_at": view.expires_at,
        "expires_local": (
            parse_iso(view.expires_at).astimezone(c.settings.timezone) if view.expires_at else None
        ),
        "remaining": (
            max(0, view.max_downloads - view.download_count)
            if view.max_downloads is not None
            else None
        ),
    }
    return render(request, "buyer/download.html", context)


@router.get("/d/{code}/cover")
def buyer_cover(request: Request, code: str) -> Response:
    limited = _throttle(request)
    if limited is not None:
        return limited
    c = ctx(request)
    with open_db(c.settings.db_path) as conn:
        view = links.resolve(conn, code)
    if view is None:
        return _unknown(request)
    if view.state != "active" or not view.cover_key or not c.storage.exists(view.cover_key):
        return Response(status_code=404)
    return FileResponse(c.storage.path(view.cover_key), media_type=view.cover_mime)


@router.api_route("/d/{code}/{fmt}", methods=["GET", "HEAD"])
def buyer_file(request: Request, code: str, fmt: str) -> Response:
    limited = _throttle(request)
    if limited is not None:
        return limited
    c = ctx(request)
    settings = c.settings
    if fmt not in MEDIA_TYPES:
        return state_page(request, "format_unavailable")
    fingerprint = client_fingerprint(settings.secret_key, client_ip(request, settings))
    # Die Verbindung wird vor der Auslieferung geschlossen; lange Downloads halten keine
    # Datenbankverbindung offen.
    with open_db(settings.db_path) as conn:
        view = links.resolve(conn, code)
        if view is None:
            return _unknown(request)
        candidate = view.files.get(fmt)
        if candidate is not None and view.state in ("active", "exhausted"):
            # Erst prüfen, ob überhaupt geliefert werden kann. Sonst würde ein Download
            # gezählt, den der Käufer nie erhält.
            if not c.storage.exists(candidate["storage_key"]):
                return state_page(request, "file_missing")
            if _range_unsatisfiable(request.headers.get("range"), candidate["size_bytes"]):
                return Response(
                    status_code=416,
                    headers={"Content-Range": f"bytes */{candidate['size_bytes']}"},
                )
        try:
            file_row, _counted = links.authorize_download(
                conn,
                view,
                fmt,
                fingerprint,
                head=request.method == "HEAD",
                window_minutes=settings.download_window_minutes,
                window_max_hours=settings.download_window_max_hours,
            )
        except links.DownloadDenied as denied:
            return state_page(request, denied.state)
    return FileResponse(
        c.storage.path(file_row["storage_key"]),
        media_type=MEDIA_TYPES[fmt],
        filename=download_filename(view.title, f".{fmt}"),
        content_disposition_type="attachment",
        headers={"Content-Security-Policy": CSP_FILE, "Cache-Control": "private, no-store"},
    )
