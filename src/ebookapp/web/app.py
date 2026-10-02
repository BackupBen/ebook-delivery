"""Zusammenbau der Anwendung."""

from __future__ import annotations

import contextlib
import logging
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import RedirectResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from ..backup import BackupManager
from ..config import Settings, load_settings
from ..db import migrate, open_db
from ..errors import AppError
from ..logging_setup import configure_logging
from ..ratelimit import RateLimiter
from ..services import auth, misc
from ..storage import Storage
from . import admin, buyer, buyer_texts
from .api import build_api
from .common import AppContext, ErrorInfo
from .middleware import CoreMiddleware

log = logging.getLogger("ebookapp")


class CachedStaticFiles(StaticFiles):
    """Statische Dateien der Oberfläche (CSS, JS). Buchdateien liegen hier nie."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response


def prepare(context: AppContext) -> None:
    """Verzeichnisse, Schema und Administrator vorbereiten."""
    settings = context.settings
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        settings.backup_dir.mkdir(parents=True, exist_ok=True)
    context.storage.ensure()
    context.storage.clean_tmp(older_than_seconds=0)
    # Hochgeladene Dateien werden auf dem Datenvolume zwischengespeichert, nicht im
    # (schreibgeschützten) Container-Dateisystem.
    tempfile.tempdir = str(settings.tmp_dir)
    migrate(settings.db_path)
    with open_db(settings.db_path) as conn:
        auth.ensure_admin(conn, settings)
        misc.housekeeping(conn, settings)
    if not settings.public_base_url:
        log.warning(
            "PUBLIC_BASE_URL ist nicht gesetzt. Links werden aus der Anfrage abgeleitet und "
            "der Host-Header wird nicht geprüft. Für den Produktivbetrieb bitte setzen."
        )
    if not context.backups.enabled:
        log.warning("Backups sind nicht aktiv (BACKUP_PASSWORD fehlt oder restic nicht gefunden).")
    elif not context.backups.offsite_configured:
        log.warning("Es ist kein externes Backup-Ziel eingerichtet (BACKUP_OFFSITE_REPOSITORY).")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    storage = Storage(settings.books_dir, settings.tmp_dir)
    context = AppContext(
        settings=settings,
        storage=storage,
        limiter=RateLimiter(),
        backups=BackupManager(settings, storage),
    )
    prepare(context)
    admin.install_filters(settings.timezone)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        context.backups.start()
        try:
            yield
        finally:
            context.backups.stop()

    # Keine automatischen Weiterleitungen bei abschließendem Schrägstrich: Sie würden den
    # Link-Code in einer Location-Kopfzeile mit dem internen Schema (http) wiederholen.
    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        redirect_slashes=False,
    )
    app.state.ctx = context
    app.add_middleware(CoreMiddleware, settings=settings)

    app.mount(
        "/static",
        CachedStaticFiles(directory=str(resources.files("ebookapp") / "static")),
        name="static",
    )
    app.mount("/api/v1", build_api(context))
    app.include_router(buyer.router)
    app.include_router(admin.router)

    @app.exception_handler(admin.LoginRequired)
    async def login_required(request: Request, exc: admin.LoginRequired) -> Response:
        return RedirectResponse("/admin/login", status_code=303)

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError) -> Response:
        info = ErrorInfo(
            message=exc.message, code=exc.code, fields=exc.fields, status_code=exc.status_code
        )
        return _error_page(request, info, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        messages = {
            404: "Diese Seite gibt es nicht.",
            405: "Diese Aktion ist hier nicht möglich.",
            413: "Die hochgeladenen Daten sind zu groß.",
        }
        info = ErrorInfo(
            message=messages.get(exc.status_code, "Die Anfrage konnte nicht verarbeitet werden."),
            code=f"http_{exc.status_code}",
            fields=[],
            status_code=exc.status_code,
        )
        return _error_page(request, info, getattr(exc, "headers", None))

    return app


def _error_page(request: Request, info: ErrorInfo, headers: dict[str, str] | None) -> Response:
    in_admin = request.url.path.startswith("/admin") and getattr(request.state, "session", None)
    template = "admin/error.html" if in_admin else "buyer/state.html"
    context = {
        "nav": "",
        "title": f"Fehler {info.status_code}",
        "message": info.message,
        "state": "error",
        "lang": buyer_texts.DEFAULT_LANGUAGE,
        "t": buyer_texts.TEXTS[buyer_texts.DEFAULT_LANGUAGE],
    }
    response = admin.render(
        request,
        template,
        context,
        status_code=info.status_code,
        error=info if in_admin else None,
    )
    for name, value in (headers or {}).items():
        response.headers[name] = value
    return response
