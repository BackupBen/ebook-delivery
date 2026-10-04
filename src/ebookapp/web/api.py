"""Versionierte REST-API unter /api/v1.

Die API ruft dieselben Service-Funktionen auf wie die Verwaltungsoberfläche. Validierung
und Geschäftsregeln sind deshalb identisch.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any, BinaryIO, Literal

from fastapi import Depends, FastAPI, File, Header, Query, Request, Security, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import __version__, i18n
from ..db import open_db
from ..errors import AppError, Forbidden, RateLimited, Unauthorized, field_error
from ..i18n import _
from ..schemas import (
    BookCreate,
    BookList,
    BookOut,
    BookUpdate,
    DeleteResult,
    DeletionPreview,
    DownloadStats,
    EditionCreate,
    EditionList,
    EditionOut,
    EditionPublish,
    ErrorOut,
    LinkCreate,
    LinkCreated,
    LinkList,
    LinkLookup,
    LinkMigrate,
    LinkOut,
    LinkState,
    LinkUpdate,
    MigrateResult,
    PublishResult,
    translate_errors,
)
from ..security import rate_key, unwrap_code, wrap_code
from ..services import auth, books, links, misc
from ..validation import EpubLimits
from . import admin
from .common import AppContext, audit, base_url, client_ip, ctx, get_conn

DESCRIPTION = """
REST-API zur Verwaltung von Büchern, Ausgaben und Käuferlinks.

## Anmeldung

Jede Anfrage benötigt einen API-Schlüssel im Header `Authorization: Bearer <API_KEY>`.
Schlüssel werden im Verwaltungsbereich unter **Einstellungen → API-Schlüssel** erstellt und
dort nur einmal vollständig angezeigt. Schlüssel in der URL werden abgewiesen.

| Berechtigung | Erlaubt |
|---|---|
| `books:read` | Bücher, Ausgaben und Statistik lesen |
| `books:write` | Bücher anlegen, bearbeiten, archivieren, löschen; Ausgaben veröffentlichen |
| `files:write` | PDF, EPUB und Cover hochladen oder entfernen |
| `links:manage` | Käuferlinks erstellen, auflisten, prüfen, ändern, widerrufen |

## Idempotenz

`POST /books`, die Upload-Endpunkte und `POST /links` akzeptieren den Header
`Idempotency-Key` (16 bis 200 Zeichen, am besten eine zufällige UUID). Eine Wiederholung
mit demselben Schlüssel und demselben Inhalt liefert innerhalb von 24 Stunden die
ursprüngliche Antwort mit dem Header `Idempotent-Replayed: true` und erzeugt kein Duplikat.
Derselbe Schlüssel mit anderem Inhalt wird mit 422 abgewiesen.

## Käuferlinks

Der geheime Code und die vollständige URL eines Links stehen **nur** in der Antwort auf
`POST /links`. Gespeichert wird ausschließlich ein Hash; Listen und Detailabfragen enthalten
den Code nie.

## Ausgaben

Ein neues Buch erhält automatisch einen Entwurf für Ausgabe 1. Dateien werden in einen
Entwurf hochgeladen; veröffentlichte Ausgaben sind unveränderlich. Beim Veröffentlichen
einer weiteren Ausgabe muss `existing_links` ausdrücklich `migrate` oder `keep` sein, sobald
das Buch Käuferlinks hat.

## Fehler

Fehler haben immer die Form `{"error": {"code": "...", "message": "...", "fields": [...]}}`.
"""

TAGS = [
    {"name": "Bücher", "description": "Bücher und ihre Metadaten"},
    {"name": "Ausgaben", "description": "Ausgaben eines Buchs und deren Dateien"},
    {"name": "Käuferlinks", "description": "Persönliche Downloadlinks"},
    {"name": "Statistik", "description": "Zählwerte ohne Codes und ohne Client-Daten"},
]

ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorOut, "description": "API-Schlüssel fehlt oder ist ungültig"},
    403: {"model": ErrorOut, "description": "Berechtigung fehlt"},
    404: {"model": ErrorOut, "description": "Nicht gefunden"},
    409: {"model": ErrorOut, "description": "Konflikt mit dem aktuellen Zustand"},
    422: {"model": ErrorOut, "description": "Ungültige Eingabe"},
    429: {"model": ErrorOut, "description": "Zu viele Anfragen"},
}

URL_CREDENTIAL_PARAMS = ("api_key", "apikey", "access_token", "token", "key", "authorization")

bearer = HTTPBearer(
    auto_error=False,
    bearerFormat="ebk_…",
    description="API-Schlüssel aus dem Verwaltungsbereich",
)

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description="Optional. Wiederholungen mit demselben Schlüssel erzeugen kein Duplikat.",
    ),
]


def _check_key(context: AppContext, request: Request) -> dict[str, Any]:
    """Prüft den API-Schlüssel einer Anfrage. Läuft, bevor der Anfragekörper gelesen wird."""
    settings = context.settings
    if any(name in request.query_params for name in URL_CREDENTIAL_PARAMS):
        raise AppError(
            _(
                "Zugangsdaten dürfen nicht in der URL stehen. Verwende den Header "
                "Authorization: Bearer <API_KEY>."
            ),
            code="credentials_in_url",
        )
    address = rate_key(client_ip(request, settings))
    fail_key = f"api-fail:{address}"
    wait = context.limiter.retry_after(fail_key, 20, 600)
    if wait:
        raise RateLimited(
            _("Zu viele fehlgeschlagene Anmeldungen. Bitte später erneut versuchen."),
            headers={"Retry-After": str(wait)},
        )
    header = request.headers.get("authorization", "")
    scheme, _sep, token = header.partition(" ")
    key = None
    if scheme.lower() == "bearer" and token.strip():
        with open_db(settings.db_path) as conn:
            key = auth.authenticate_api_key(conn, token.strip())
    if key is None:
        if header:
            context.limiter.record(fail_key, 600)
            with open_db(settings.db_path) as conn:
                audit(request, conn, "api_key_rejected", detail=request.url.path[:200])
        raise Unauthorized(
            _("API-Schlüssel fehlt oder ist ungültig."), headers={"WWW-Authenticate": "Bearer"}
        )
    wait = context.limiter.hit(f"api:{key['id']}", settings.api_rate_per_minute, 60)
    if wait:
        raise RateLimited(
            _("Rate Limit erreicht. Bitte kurz warten."), headers={"Retry-After": str(wait)}
        )
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data") and "files:write" not in key["scopes"]:
        raise Forbidden(
            _("Diesem API-Schlüssel fehlt die Berechtigung „%(scope)s“.", scope="files:write"),
            code="insufficient_scope",
            details={"required_scope": "files:write"},
        )
    return key


class ApiAuthGate:
    """Authentifiziert API-Anfragen, bevor ihr Körper gelesen wird.

    FastAPI liest ``multipart/form-data`` vollständig ein, bevor Abhängigkeiten laufen.
    Ohne diese Schranke könnte jeder ohne Schlüssel große Uploads auf dem Datenvolume
    zwischenspeichern lassen. Abgewiesene Anfragen werden beantwortet, ohne dass ein
    einziges Byte des Körpers angenommen wird.
    """

    OPEN_PATHS = ("/docs", "/openapi.json")

    def __init__(self, app: ASGIApp, context: AppContext) -> None:
        self.app = app
        self.context = context

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].endswith(self.OPEN_PATHS):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            key = await run_in_threadpool(_check_key, self.context, request)
        except AppError as exc:
            headers = dict(exc.headers)
            if scope["method"] not in ("GET", "HEAD"):
                headers["Connection"] = "close"
            response = JSONResponse(exc.to_dict(), status_code=exc.status_code, headers=headers)
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["api_key"] = key
        await self.app(scope, receive, send)


def authenticate(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer),
) -> dict[str, Any]:
    """Liefert den von der Schranke geprüften Schlüssel.

    Die ``Security``-Abhängigkeit trägt das Bearer-Schema in die OpenAPI-Beschreibung ein.
    """
    key = getattr(request.state, "api_key", None)
    if key is None:  # pragma: no cover - die Schranke läuft immer vorher
        raise Unauthorized(
            _("API-Schlüssel fehlt oder ist ungültig."), headers={"WWW-Authenticate": "Bearer"}
        )
    return key


def require(scope: str) -> Callable[..., dict[str, Any]]:
    def dependency(key: dict[str, Any] = Depends(authenticate)) -> dict[str, Any]:
        if scope not in key["scopes"]:
            raise Forbidden(
                _("Diesem API-Schlüssel fehlt die Berechtigung „%(scope)s“.", scope=scope),
                code="insufficient_scope",
                details={"required_scope": scope},
            )
        return key

    return dependency


def _hash_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    stream.seek(0)
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def _idempotent(
    request: Request,
    conn: sqlite3.Connection,
    key: dict[str, Any],
    idempotency_key: str | None,
    fingerprint: Callable[[], str],
    action: Callable[[], Any],
    *,
    status_code: int = 200,
) -> Response:
    """Führt ``action`` aus und speichert die Antwort für Wiederholungen."""
    if idempotency_key is None:
        return JSONResponse(action(), status_code=status_code)
    scope = f"api:{key['id']}"
    replay = misc.idempotency_begin(conn, scope, idempotency_key, fingerprint())
    if replay is not None:
        return JSONResponse(
            replay.body, status_code=replay.status_code, headers={"Idempotent-Replayed": "true"}
        )
    try:
        body = action()
    except BaseException:
        misc.idempotency_abort(conn, scope, idempotency_key)
        raise
    misc.idempotency_finish(conn, scope, idempotency_key, status_code, body)
    return JSONResponse(body, status_code=status_code)


def _epub_limits(c: AppContext) -> EpubLimits:
    return EpubLimits(
        max_uncompressed_bytes=c.settings.epub_max_uncompressed_bytes,
        max_entries=c.settings.epub_max_entries,
        max_ratio=c.settings.epub_max_ratio,
    )


def build_api(context: AppContext) -> FastAPI:
    api = FastAPI(
        title="E-Book-Auslieferung API",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        servers=[{"url": "/api/v1", "description": "Diese Installation"}],
        responses=ERRORS,
        redirect_slashes=False,
    )
    api.state.ctx = context
    if context is not None:
        api.add_middleware(ApiAuthGate, context=context)

    # ------------------------------------------------------------------
    # Fehlerbehandlung
    # ------------------------------------------------------------------

    @api.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError) -> Response:
        return JSONResponse(exc.to_dict(), status_code=exc.status_code, headers=exc.headers)

    @api.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        fields = translate_errors(list(exc.errors()))
        return JSONResponse(
            {
                "error": {
                    "code": "validation_error",
                    "message": _("Die Anfrage enthält ungültige Angaben."),
                    "fields": fields,
                }
            },
            status_code=422,
        )

    @api.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        codes = {404: "not_found", 405: "method_not_allowed", 413: "payload_too_large"}
        messages = {
            404: _("Diesen Endpunkt gibt es nicht."),
            405: _("Diese Methode ist hier nicht erlaubt."),
            413: _("Die Anfrage ist zu groß."),
        }
        return JSONResponse(
            {
                "error": {
                    "code": codes.get(exc.status_code, "error"),
                    "message": messages.get(exc.status_code, str(exc.detail)),
                }
            },
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @api.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> Response:
        # Protokolliert wird der Fehler zentral in der Middleware (mit bereinigtem Pfad).
        return JSONResponse(
            {
                "error": {
                    "code": "internal_error",
                    "message": _("Unerwarteter Serverfehler. Bitte die Fehler-ID angeben."),
                    "request_id": getattr(request.state, "request_id", ""),
                }
            },
            status_code=500,
        )

    @api.exception_handler(admin.LoginRequired)
    async def login_required(request: Request, exc: admin.LoginRequired) -> Response:
        return RedirectResponse("/admin/login", status_code=303)

    # ------------------------------------------------------------------
    # Dokumentation
    # ------------------------------------------------------------------

    @api.get("/openapi.json", include_in_schema=False)
    def openapi_json(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        """Die Spezifikation ist nur für angemeldete Administratoren und API-Schlüssel sichtbar."""
        c = ctx(request)
        token = request.cookies.get(c.settings.session_cookie_name, "")
        allowed = bool(token and auth.get_session(conn, c.settings, token))
        if not allowed:
            header = request.headers.get("authorization", "")
            if header.lower().startswith("bearer "):
                allowed = auth.authenticate_api_key(conn, header[7:].strip()) is not None
        if not allowed:
            raise Unauthorized(_("Anmeldung erforderlich."), headers={"WWW-Authenticate": "Bearer"})
        return JSONResponse(i18n.translate_openapi(api.openapi()))

    @api.get("/docs", include_in_schema=False)
    def docs(request: Request, session: dict = Depends(admin.require_admin)) -> Response:
        response = admin.render(request, "admin/api_docs.html", {"nav": "api"})
        # Swagger UI setzt Inline-Styles und Daten-URLs für Symbole ein.
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "script-src 'self'; connect-src 'self'; font-src 'self' data:; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    # ------------------------------------------------------------------
    # Bücher
    # ------------------------------------------------------------------

    @api.get("/books", response_model=BookList, tags=["Bücher"], summary="Bücher auflisten")
    def list_books(
        q: Annotated[
            str | None, Query(max_length=200, description="Suche in Titel, Beschreibung, Whop-ID")
        ] = None,
        status: Annotated[Literal["active", "archived", "all"], Query()] = "active",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
        key: dict = Depends(require("books:read")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Berechtigung: `books:read`"""
        return books.list_books(conn, q=q, status=status, limit=limit, offset=offset)

    @api.post(
        "/books", response_model=BookOut, status_code=201, tags=["Bücher"], summary="Buch anlegen"
    )
    def create_book(
        request: Request,
        data: BookCreate,
        idempotency_key: IdempotencyKey = None,
        key: dict = Depends(require("books:write")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Legt ein Buch mit einem Entwurf für Ausgabe 1 an. Berechtigung: `books:write`"""
        return _idempotent(
            request,
            conn,
            key,
            idempotency_key,
            lambda: misc.request_hash("POST /books", data.model_dump(mode="json")),
            lambda: books.create_book(conn, data),
            status_code=201,
        )

    @api.get("/books/{book_id}", response_model=BookOut, tags=["Bücher"], summary="Buch abrufen")
    def get_book(
        book_id: str,
        key: dict = Depends(require("books:read")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Berechtigung: `books:read`"""
        return books.get_book(conn, book_id)

    @api.patch(
        "/books/{book_id}", response_model=BookOut, tags=["Bücher"], summary="Buch bearbeiten"
    )
    def update_book(
        book_id: str,
        data: BookUpdate,
        key: dict = Depends(require("books:write")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Ändert nur die übergebenen Felder. Berechtigung: `books:write`"""
        return books.update_book(conn, book_id, data)

    @api.post(
        "/books/{book_id}/archive",
        response_model=BookOut,
        tags=["Bücher"],
        summary="Buch archivieren",
    )
    def archive_book(
        book_id: str,
        key: dict = Depends(require("books:write")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Archivierte Bücher erhalten keine neuen Links; bestehende Links funktionieren weiter.

        Berechtigung: `books:write`
        """
        return books.set_archived(conn, book_id, True)

    @api.post(
        "/books/{book_id}/unarchive",
        response_model=BookOut,
        tags=["Bücher"],
        summary="Archivierung aufheben",
    )
    def unarchive_book(
        book_id: str,
        key: dict = Depends(require("books:write")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Berechtigung: `books:write`"""
        return books.set_archived(conn, book_id, False)

    @api.get(
        "/books/{book_id}/deletion-preview",
        response_model=DeletionPreview,
        tags=["Bücher"],
        summary="Folgen einer Löschung anzeigen",
    )
    def deletion_preview(
        book_id: str,
        key: dict = Depends(require("books:write")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Zeigt, wie viele Links durch die Löschung ungültig würden. Berechtigung:
        `books:write`. Die einzelnen Links (`links`) sind nur enthalten, wenn der Schlüssel
        zusätzlich `links:manage` besitzt; die Zählwerte sind immer enthalten.
        """
        return books.deletion_preview(conn, book_id, include_links="links:manage" in key["scopes"])

    @api.delete(
        "/books/{book_id}", response_model=DeleteResult, tags=["Bücher"], summary="Buch löschen"
    )
    def delete_book(
        request: Request,
        book_id: str,
        expected_link_count: Annotated[
            int,
            Query(
                ge=0,
                description=(
                    "Bestätigung: Anzahl der Links, die ungültig werden (`links_total` aus "
                    "`deletion-preview`). Stimmt die Zahl nicht, wird nichts gelöscht."
                ),
            ),
        ],
        key: dict = Depends(require("books:write")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Löscht Buch, Ausgaben, Dateien und alle Links endgültig. Berechtigung: `books:write`"""
        title = books.get_book_row(conn, book_id)["title"]
        result = books.delete_book(conn, ctx(request).storage, book_id, expected_link_count)
        audit(
            request,
            conn,
            "book_deleted",
            username=f"API: {key['name']}",
            detail=f"{title} ({book_id})",
        )
        return result

    @api.put(
        "/books/{book_id}/cover",
        response_model=BookOut,
        tags=["Bücher"],
        summary="Cover hochladen",
    )
    def upload_cover(
        request: Request,
        book_id: str,
        file: Annotated[UploadFile, File(description="JPEG, PNG oder WebP")],
        key: dict = Depends(require("files:write")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Das Bild wird geprüft, verkleinert und neu kodiert. Berechtigung: `files:write`"""
        c = ctx(request)
        return books.set_cover(
            conn,
            c.storage,
            book_id,
            file.file,
            file.filename,
            file.content_type,
            misc.get_limits(conn, c.settings).bytes_for("cover"),
        )

    @api.delete(
        "/books/{book_id}/cover", response_model=BookOut, tags=["Bücher"], summary="Cover entfernen"
    )
    def delete_cover(
        request: Request,
        book_id: str,
        key: dict = Depends(require("files:write")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Berechtigung: `files:write`"""
        return books.remove_cover(conn, ctx(request).storage, book_id)

    # ------------------------------------------------------------------
    # Ausgaben
    # ------------------------------------------------------------------

    @api.get(
        "/books/{book_id}/editions",
        response_model=EditionList,
        tags=["Ausgaben"],
        summary="Ausgaben auflisten",
    )
    def list_editions(
        book_id: str,
        key: dict = Depends(require("books:read")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Berechtigung: `books:read`"""
        return {"items": books.list_editions(conn, book_id)}

    @api.post(
        "/books/{book_id}/editions",
        response_model=EditionOut,
        status_code=201,
        tags=["Ausgaben"],
        summary="Neue Ausgabe als Entwurf anlegen",
    )
    def create_edition(
        request: Request,
        book_id: str,
        data: EditionCreate,
        idempotency_key: IdempotencyKey = None,
        key: dict = Depends(require("books:write")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Pro Buch gibt es höchstens einen Entwurf. Berechtigung: `books:write`"""
        return _idempotent(
            request,
            conn,
            key,
            idempotency_key,
            lambda: misc.request_hash("POST editions", book_id, data.model_dump(mode="json")),
            lambda: books.create_edition(conn, book_id, data),
            status_code=201,
        )

    @api.get(
        "/books/{book_id}/editions/{edition_id}",
        response_model=EditionOut,
        tags=["Ausgaben"],
        summary="Ausgabe abrufen",
    )
    def get_edition(
        book_id: str,
        edition_id: str,
        key: dict = Depends(require("books:read")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Berechtigung: `books:read`"""
        return books.get_edition(conn, book_id, edition_id)

    @api.delete(
        "/books/{book_id}/editions/{edition_id}",
        status_code=204,
        tags=["Ausgaben"],
        summary="Ausgabe löschen",
    )
    def delete_edition(
        request: Request,
        book_id: str,
        edition_id: str,
        key: dict = Depends(require("books:write")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        """Nur Entwürfe und ältere Ausgaben ohne gebundene Links. Berechtigung: `books:write`"""
        books.delete_edition(conn, ctx(request).storage, book_id, edition_id)
        return Response(status_code=204)

    @api.post(
        "/books/{book_id}/editions/{edition_id}/files",
        response_model=EditionOut,
        tags=["Ausgaben"],
        summary="PDF und/oder EPUB hochladen",
    )
    def upload_files(
        request: Request,
        book_id: str,
        edition_id: str,
        pdf: Annotated[UploadFile | None, File(description="PDF-Datei")] = None,
        epub: Annotated[UploadFile | None, File(description="EPUB-Datei")] = None,
        idempotency_key: IdempotencyKey = None,
        key: dict = Depends(require("files:write")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Lädt Dateien als `multipart/form-data` in einen Entwurf. Vorhandene Dateien
        desselben Formats werden ersetzt. Geprüft werden Dateiendung, angegebener MIME-Typ,
        tatsächlicher Inhalt und Größe. Berechtigung: `files:write`
        """
        c = ctx(request)
        uploads = {fmt: item for fmt, item in (("pdf", pdf), ("epub", epub)) if item is not None}
        if not uploads:
            raise field_error(
                "pdf", _("Es wurde keine Datei übergeben (Felder pdf und/oder epub).")
            )

        def action() -> Any:
            limits = misc.get_limits(conn, c.settings)
            return books.upload_files(
                conn,
                c.storage,
                book_id=book_id,
                edition_id=edition_id,
                uploads={
                    fmt: (item.file, item.filename, item.content_type)
                    for fmt, item in uploads.items()
                },
                max_bytes={fmt: limits.bytes_for(fmt) for fmt in uploads},
                epub_limits=_epub_limits(c),
            )

        return _idempotent(
            request,
            conn,
            key,
            idempotency_key,
            lambda: misc.request_hash(
                "POST files",
                book_id,
                edition_id,
                {fmt: _hash_stream(item.file) for fmt, item in uploads.items()},
            ),
            action,
        )

    @api.delete(
        "/books/{book_id}/editions/{edition_id}/files/{fmt}",
        response_model=EditionOut,
        tags=["Ausgaben"],
        summary="Datei aus einem Entwurf entfernen",
    )
    def delete_file(
        request: Request,
        book_id: str,
        edition_id: str,
        fmt: Literal["pdf", "epub"],
        key: dict = Depends(require("files:write")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Berechtigung: `files:write`"""
        return books.delete_file(conn, ctx(request).storage, book_id, edition_id, fmt)

    @api.post(
        "/books/{book_id}/editions/{edition_id}/publish",
        response_model=PublishResult,
        tags=["Ausgaben"],
        summary="Ausgabe veröffentlichen",
    )
    def publish_edition(
        book_id: str,
        edition_id: str,
        data: EditionPublish | None = None,
        key: dict = Depends(require("books:write")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Macht den Entwurf zur aktuellen Ausgabe. Hat das Buch bereits Käuferlinks, muss
        `existing_links` ausdrücklich `migrate` oder `keep` sein; sonst antwortet die API
        mit 422 (`existing_links_required`). Links, deren freigegebene Formate in der neuen
        Ausgabe fehlen, werden nicht umgestellt und in `links_without_matching_format`
        gezählt. Berechtigung: `books:write`, für `migrate` zusätzlich `links:manage`
        """
        data = data or EditionPublish()
        if data.existing_links == "migrate" and "links:manage" not in key["scopes"]:
            raise Forbidden(
                _(
                    "Das Umstellen bestehender Links erfordert zusätzlich die Berechtigung "
                    "„links:manage“."
                ),
                code="insufficient_scope",
                details={"required_scope": "links:manage"},
            )
        return books.publish_edition(conn, book_id, edition_id, data)

    @api.post(
        "/books/{book_id}/editions/{edition_id}/migrate-links",
        response_model=MigrateResult,
        tags=["Ausgaben"],
        summary="Bestehende Links auf diese Ausgabe umstellen",
    )
    def migrate_links(
        book_id: str,
        edition_id: str,
        data: LinkMigrate,
        key: dict = Depends(require("links:manage")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Stellt alle nicht widerrufenen Links des Buchs ausdrücklich auf diese
        veröffentlichte Ausgabe um. Berechtigung: `links:manage`
        """
        return books.migrate_links(conn, book_id, edition_id)

    # ------------------------------------------------------------------
    # Käuferlinks
    # ------------------------------------------------------------------

    @api.get("/links", response_model=LinkList, tags=["Käuferlinks"], summary="Links auflisten")
    def list_links(
        book_id: Annotated[str | None, Query(max_length=64)] = None,
        state: Annotated[LinkState | None, Query()] = None,
        q: Annotated[
            str | None, Query(max_length=200, description="Suche in Bezeichnung und Buchtitel")
        ] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
        key: dict = Depends(require("links:manage")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Die Liste enthält keine Codes. Berechtigung: `links:manage`"""
        return links.list_links(conn, book_id=book_id, state=state, q=q, limit=limit, offset=offset)

    @api.post(
        "/links",
        response_model=LinkCreated,
        status_code=201,
        tags=["Käuferlinks"],
        summary="Link erstellen",
    )
    def create_link(
        request: Request,
        data: LinkCreate,
        idempotency_key: IdempotencyKey = None,
        key: dict = Depends(require("links:manage")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Erstellt einen Link für genau ein Buch. Ohne `expires_at` und `max_downloads` ist
        er dauerhaft und unbegrenzt gültig. `code` und `url` stehen nur in dieser Antwort.
        Berechtigung: `links:manage`
        """
        c = ctx(request)
        public = base_url(request, c.settings)

        if idempotency_key is None:
            link, code = links.create_link(conn, data)
            return JSONResponse(
                {**link, "code": code, "url": links.build_url(public, code)}, status_code=201
            )

        scope = f"api:{key['id']}"
        fingerprint = misc.request_hash("POST /links", data.model_dump(mode="json"))
        replay = misc.idempotency_begin(conn, scope, idempotency_key, fingerprint)
        if replay is not None:
            # Der Code ist nicht gespeichert. Er wird aus einem Wert zurückgewonnen, der
            # nur zusammen mit dem Idempotency-Key des Clients lesbar ist.
            code = unwrap_code(
                c.settings.secret_key,
                scope,
                idempotency_key,
                replay.body["id"],
                replay.wrapped_secret or "",
            )
            return JSONResponse(
                {**replay.body, "code": code, "url": links.build_url(public, code)},
                status_code=replay.status_code,
                headers={"Idempotent-Replayed": "true"},
            )
        try:
            link, code = links.create_link(conn, data)
        except BaseException:
            misc.idempotency_abort(conn, scope, idempotency_key)
            raise
        misc.idempotency_finish(
            conn,
            scope,
            idempotency_key,
            201,
            link,
            wrap_code(c.settings.secret_key, scope, idempotency_key, link["id"], code),
        )
        return JSONResponse(
            {**link, "code": code, "url": links.build_url(public, code)}, status_code=201
        )

    @api.post(
        "/links/lookup",
        response_model=LinkOut,
        tags=["Käuferlinks"],
        summary="Link anhand seines Codes prüfen",
    )
    def lookup_link(
        data: LinkLookup,
        key: dict = Depends(require("links:manage")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Findet den Link zu einem Code oder einer vollständigen URL und liefert seinen
        Zustand. Der Code steht im Anfragekörper, nie in der URL. Berechtigung: `links:manage`
        """
        return links.lookup(conn, data.code)

    @api.get(
        "/links/{link_id}",
        response_model=LinkOut,
        tags=["Käuferlinks"],
        summary="Link abrufen und Zustand prüfen",
    )
    def get_link(
        link_id: str,
        key: dict = Depends(require("links:manage")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """`state` berücksichtigt Ablaufdatum und Downloadlimit. Berechtigung: `links:manage`"""
        return links.get_link(conn, link_id)

    @api.patch(
        "/links/{link_id}", response_model=LinkOut, tags=["Käuferlinks"], summary="Link ändern"
    )
    def update_link(
        link_id: str,
        data: LinkUpdate,
        key: dict = Depends(require("links:manage")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Ändert nur die übergebenen Felder. `status: disabled` deaktiviert vorübergehend,
        `status: active` aktiviert wieder. Berechtigung: `links:manage`
        """
        return links.update_link(conn, link_id, data)

    @api.post(
        "/links/{link_id}/revoke",
        response_model=LinkOut,
        tags=["Käuferlinks"],
        summary="Link widerrufen",
    )
    def revoke_link(
        link_id: str,
        key: dict = Depends(require("links:manage")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Endgültig; ein erneuter Aufruf ändert nichts. Berechtigung: `links:manage`"""
        return links.revoke_link(conn, link_id)

    @api.delete(
        "/links/{link_id}",
        status_code=204,
        tags=["Käuferlinks"],
        summary="Widerrufenen Link löschen",
    )
    def delete_link(
        link_id: str,
        key: dict = Depends(require("links:manage")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        """Nur widerrufene Links können gelöscht werden. Berechtigung: `links:manage`"""
        links.delete_link(conn, link_id)
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # Statistik
    # ------------------------------------------------------------------

    @api.get(
        "/stats/downloads",
        response_model=DownloadStats,
        tags=["Statistik"],
        summary="Downloadstatistik",
    )
    def stats(
        date_from: Annotated[
            date | None, Query(description="Standard: 30 Tage vor date_to")
        ] = None,
        date_to: Annotated[date | None, Query(description="Standard: heute (UTC)")] = None,
        book_id: Annotated[str | None, Query(max_length=64)] = None,
        key: dict = Depends(require("books:read")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Gezählte Downloads nach Tag, Format und Buch. Berechtigung: `books:read`"""
        return misc.download_stats(conn, date_from=date_from, date_to=date_to, book_id=book_id)

    @api.get(
        "/links/{link_id}/stats",
        response_model=DownloadStats,
        tags=["Statistik"],
        summary="Downloadstatistik eines Links",
    )
    def link_stats(
        link_id: str,
        date_from: Annotated[date | None, Query()] = None,
        date_to: Annotated[date | None, Query()] = None,
        key: dict = Depends(require("links:manage")),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Berechtigung: `links:manage`"""
        links.get_link(conn, link_id)
        return misc.download_stats(conn, date_from=date_from, date_to=date_to, link_id=link_id)

    return api
