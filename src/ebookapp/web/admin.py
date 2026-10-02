"""Geschützter Verwaltungsbereich (serverseitig gerenderte Seiten)."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.responses import FileResponse, RedirectResponse, Response
from starlette.templating import Jinja2Templates

from .. import __version__
from ..db import open_db, parse_iso
from ..errors import AppError, Forbidden, NotFound, field_error
from ..schemas import (
    LANGUAGE_LABELS,
    SCOPE_LABELS,
    SCOPES,
    ApiKeyCreate,
    BookCreate,
    BookUpdate,
    EditionCreate,
    EditionPublish,
    LinkCreate,
    LinkUpdate,
    OrderEmail,
    PasswordChange,
    SettingsUpdate,
)
from ..security import constant_time_equal, new_token, rate_key, unwrap_code, wrap_code
from ..services import auth, books, links, mail, misc, orders
from ..validation import MEDIA_TYPES, EpubLimits, download_filename
from .common import (
    AppContext,
    ErrorInfo,
    base_url,
    check_same_origin,
    client_ip,
    ctx,
    error_info,
    form_text,
    get_conn,
)
from .middleware import CSP_FILE

router = APIRouter(include_in_schema=False)

templates = Jinja2Templates(directory=str(resources.files("ebookapp") / "templates"))

PAGE_SIZE = 50

STATE_LABELS = {
    "active": "aktiv",
    "disabled": "deaktiviert",
    "revoked": "widerrufen",
    "expired": "abgelaufen",
    "exhausted": "Limit erreicht",
}


# Höchstens zwei gleichzeitige Passwortprüfungen (siehe login_submit).
_PASSWORD_SLOTS = threading.BoundedSemaphore(2)


class LoginRequired(Exception):
    """Es gibt keine gültige Sitzung."""


# ---------------------------------------------------------------------------
# Vorlagen-Helfer
# ---------------------------------------------------------------------------


def _filesize(value: int | None, language: str = "de") -> str:
    if value is None:
        return "–"
    size = float(value)
    for unit in ("Bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "Bytes":
                return f"{int(size)} Bytes" if language == "de" else f"{int(size)} bytes"
            text = f"{size:.1f} {unit}"
            return text if language == "en" else text.replace(".", ",")
        size /= 1024
    return f"{value} Bytes"


def install_filters(settings_timezone: Any) -> None:
    def _dt(value: str | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
        if not value:
            return "–"
        try:
            return parse_iso(value).astimezone(settings_timezone).strftime(fmt)
        except ValueError:
            return value

    def _dt_local(value: str | None) -> str:
        """Wert für <input type=datetime-local>."""
        return _dt(value, "%Y-%m-%dT%H:%M") if value else ""

    templates.env.filters["dt"] = _dt
    templates.env.filters["dt_local"] = _dt_local
    templates.env.filters["filesize"] = _filesize
    templates.env.globals["STATE_LABELS"] = STATE_LABELS
    templates.env.globals["SCOPE_LABELS"] = SCOPE_LABELS
    templates.env.globals["LANGUAGE_LABELS"] = LANGUAGE_LABELS
    templates.env.globals["app_version"] = __version__


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    error: ErrorInfo | None = None,
) -> Response:
    c = ctx(request)
    session = getattr(request.state, "session", None)
    flash: list[dict[str, str]] = []
    if session and name.startswith("admin/"):
        with open_db(c.settings.db_path) as conn:
            flash = auth.pop_flash(conn, session["token_hash"])
    data = {
        "session": session,
        "csrf_token": session["csrf_token"] if session else "",
        "flash": flash,
        "error": error,
        "request_id": getattr(request.state, "request_id", ""),
        "now_text": datetime.now(c.settings.timezone).strftime("%d.%m.%Y %H:%M:%S %Z"),
        "timezone_name": c.settings.timezone_name,
    }
    data.update(context or {})
    if error is not None and status_code == 200:
        status_code = error.status_code
    return templates.TemplateResponse(request, name, data, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def flash(conn: sqlite3.Connection, session: dict[str, Any], kind: str, message: str) -> None:
    auth.add_flash(conn, session["token_hash"], kind, message)


# ---------------------------------------------------------------------------
# Sitzung und CSRF
# ---------------------------------------------------------------------------


def require_admin(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    c = ctx(request)
    token = request.cookies.get(c.settings.session_cookie_name, "")
    session = auth.get_session(conn, c.settings, token) if token else None
    if session is None:
        raise LoginRequired()
    request.state.session = session
    return session


@dataclass
class AdminPost:
    session: dict[str, Any]
    form: FormData


async def admin_post(
    request: Request, session: dict[str, Any] = Depends(require_admin)
) -> AsyncIterator[AdminPost]:
    """Schreibende Anfrage: gültige Sitzung, eigene Herkunft und CSRF-Token."""
    c = ctx(request)
    check_same_origin(request, c.settings)
    async with request.form(max_files=4, max_fields=80) as form:
        token = form_text(form, "csrf_token")
        if not token or not constant_time_equal(token, session["csrf_token"]):
            raise Forbidden(
                "Das Formular ist abgelaufen oder ungültig. Bitte die Seite neu laden.",
                code="csrf_token",
            )
        yield AdminPost(session=session, form=form)


def _upload(form: FormData, name: str) -> UploadFile | None:
    value = form.get(name)
    if isinstance(value, UploadFile) and (value.filename or (value.size or 0) > 0):
        return value
    return None


def _epub_limits(c: AppContext) -> EpubLimits:
    return EpubLimits(
        max_uncompressed_bytes=c.settings.epub_max_uncompressed_bytes,
        max_entries=c.settings.epub_max_entries,
        max_ratio=c.settings.epub_max_ratio,
    )


def _page(request: Request) -> tuple[int, int]:
    try:
        page = min(100_000, max(1, int(request.query_params.get("page", "1"))))
    except ValueError:
        page = 1
    return page, (page - 1) * PAGE_SIZE


# ---------------------------------------------------------------------------
# Anmeldung
# ---------------------------------------------------------------------------


def _login_page(
    request: Request, *, error: str | None = None, status_code: int = 200, username: str = ""
) -> Response:
    c = ctx(request)
    token = new_token()
    with open_db(c.settings.db_path) as conn:
        has_admin = conn.execute("SELECT 1 FROM users").fetchone() is not None
    response = render(
        request,
        "admin/login.html",
        {"login_token": token, "login_error": error, "username": username, "has_admin": has_admin},
        status_code=status_code,
    )
    response.set_cookie(
        c.settings.login_csrf_cookie_name,
        token,
        max_age=3600,
        httponly=True,
        secure=c.settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return response


@router.get("/admin/login")
def login_form(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
    c = ctx(request)
    token = request.cookies.get(c.settings.session_cookie_name, "")
    if token and auth.get_session(conn, c.settings, token):
        return redirect("/admin/books")
    return _login_page(request)


@router.post("/admin/login")
async def login_submit(request: Request) -> Response:
    c = ctx(request)
    settings = c.settings
    ip = client_ip(request, settings)
    window = settings.login_window_minutes * 60
    try:
        check_same_origin(request, settings)
    except Forbidden:
        return _login_page(request, error="Anfrage abgelehnt.", status_code=403)

    async with request.form(max_files=0, max_fields=10) as form:
        username = form_text(form, "username").strip()[:100]
        password = form_text(form, "password")[:256]
        token = form_text(form, "login_token")

    cookie = request.cookies.get(settings.login_csrf_cookie_name, "")
    if not token or not cookie or not constant_time_equal(token, cookie):
        return _login_page(
            request,
            error="Das Anmeldeformular ist abgelaufen. Bitte erneut versuchen.",
            status_code=403,
            username=username,
        )

    # Jeder Versuch wird VOR der Passwortprüfung gezählt. So können gleichzeitige Anfragen
    # das Limit nicht unterlaufen. Der Zähler gilt je Anschluss und Benutzername: Ein
    # Angreifer kann damit nur sich selbst aussperren, nicht den Administrator.
    address = rate_key(ip)
    ip_key = f"login-ip:{address}"
    user_key = f"login-user:{address}:{username.lower()}"
    wait = c.limiter.hit(ip_key, settings.login_max_failures * 4, window) or c.limiter.hit(
        user_key, settings.login_max_failures, window
    )
    if wait:
        response = _login_page(
            request,
            error=(
                "Zu viele Anmeldeversuche. Bitte in "
                f"{max(1, (wait + 59) // 60)} Minuten erneut versuchen."
            ),
            status_code=429,
            username=username,
        )
        response.headers["Retry-After"] = str(wait)
        return response

    # Die Passwortprüfung ist absichtlich teuer (scrypt, ca. 64 MB). Mehr als zwei
    # gleichzeitige Prüfungen werden abgewiesen, damit eine Anfrageflut weder den
    # Arbeitsspeicher noch die übrigen Anfragen blockiert.
    def attempt() -> str | bool | None:
        if not _PASSWORD_SLOTS.acquire(blocking=False):
            return False
        try:
            with open_db(settings.db_path) as conn:
                user = auth.authenticate(conn, username, password)
                if user is None:
                    return None
                return auth.create_session(conn, settings, user["id"])
        finally:
            _PASSWORD_SLOTS.release()

    session_token = await run_in_threadpool(attempt)
    if session_token is False:
        response = _login_page(
            request,
            error=(
                "Die Anmeldung ist gerade ausgelastet. Bitte in wenigen Sekunden erneut versuchen."
            ),
            status_code=429,
            username=username,
        )
        response.headers["Retry-After"] = "3"
        return response
    if session_token is None:
        auth.log.warning("Fehlgeschlagene Anmeldung von %s", ip)
        return _login_page(
            request,
            error="Benutzername oder Passwort ist falsch.",
            status_code=401,
            username=username,
        )

    c.limiter.reset(user_key)
    response = redirect("/admin/books")
    response.set_cookie(
        settings.session_cookie_name,
        session_token,
        max_age=settings.session_max_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(
        settings.login_csrf_cookie_name,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@router.post("/admin/logout")
def logout(
    request: Request,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)
    auth.destroy_session(conn, post.session["token"])
    response = redirect("/admin/login")
    response.delete_cookie(
        c.settings.session_cookie_name,
        path="/",
        secure=c.settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/admin")
@router.get("/admin/")
def admin_root(session: dict = Depends(require_admin)) -> Response:
    return redirect("/admin/books")


# ---------------------------------------------------------------------------
# Bücher
# ---------------------------------------------------------------------------


@router.get("/admin/books")
def books_list(
    request: Request,
    session: dict = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    q = request.query_params.get("q", "").strip()[:200]
    status = request.query_params.get("status", "active")
    if status not in ("active", "archived", "all"):
        status = "active"
    page, offset = _page(request)
    result = books.list_books(conn, q=q, status=status, limit=PAGE_SIZE, offset=offset)
    return render(
        request,
        "admin/books_list.html",
        {
            "nav": "books",
            "result": result,
            "q": q,
            "status": status,
            "page": page,
            "page_size": PAGE_SIZE,
        },
    )


@router.get("/admin/books/new")
def book_new(request: Request, session: dict = Depends(require_admin)) -> Response:
    return render(request, "admin/book_new.html", {"nav": "books", "values": {}})


@router.post("/admin/books")
def book_create(
    request: Request,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    values = {
        "title": form_text(post.form, "title"),
        "description": form_text(post.form, "description"),
        "whop_product_id": form_text(post.form, "whop_product_id"),
        "language": form_text(post.form, "language") or "de",
    }
    try:
        book = books.create_book(conn, BookCreate.model_validate(values))
    except (AppError, ValidationError) as exc:
        return render(
            request,
            "admin/book_new.html",
            {"nav": "books", "values": values},
            error=error_info(exc),
        )
    flash(conn, post.session, "success", "Buch angelegt. Lade jetzt die Dateien hoch.")
    return redirect(f"/admin/books/{book['id']}")


def _book_page(
    request: Request,
    conn: sqlite3.Connection,
    book_id: str,
    *,
    error: ErrorInfo | None = None,
    values: dict[str, Any] | None = None,
) -> Response:
    c = ctx(request)
    book = books.get_book(conn, book_id)
    return render(
        request,
        "admin/book_detail.html",
        {
            "nav": "books",
            "book": book,
            "editions": books.list_editions(conn, book_id),
            "links": links.list_links(conn, book_id=book_id, limit=20),
            "limits": misc.get_limits(conn, c.settings),
            "values": values or {},
        },
        error=error,
    )


def _book_action(
    request: Request,
    conn: sqlite3.Connection,
    post: AdminPost,
    book_id: str,
    action: Any,
    success: str | None,
) -> Response:
    """Führt eine Aktion aus und zeigt Fehler dauerhaft auf der Buchseite an."""
    try:
        message = action()
    except (AppError, ValidationError) as exc:
        if isinstance(exc, NotFound) and exc.code == "book_not_found":
            raise
        return _book_page(request, conn, book_id, error=error_info(exc))
    text = message if isinstance(message, str) else success
    if text:
        flash(conn, post.session, "success", text)
    return redirect(f"/admin/books/{book_id}")


@router.get("/admin/books/{book_id}")
def book_detail(
    request: Request,
    book_id: str,
    session: dict = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    return _book_page(request, conn, book_id)


@router.post("/admin/books/{book_id}/edit")
def book_edit(
    request: Request,
    book_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    values = {
        "title": form_text(post.form, "title"),
        "description": form_text(post.form, "description"),
        "whop_product_id": form_text(post.form, "whop_product_id") or None,
        "language": form_text(post.form, "language") or None,
    }
    try:
        books.update_book(conn, book_id, BookUpdate.model_validate(values))
    except (AppError, ValidationError) as exc:
        if isinstance(exc, NotFound):
            raise
        return _book_page(request, conn, book_id, error=error_info(exc), values=values)
    flash(conn, post.session, "success", "Änderungen gespeichert.")
    return redirect(f"/admin/books/{book_id}")


@router.post("/admin/books/{book_id}/cover")
def book_cover_upload(
    request: Request,
    book_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)

    def action() -> None:
        upload = _upload(post.form, "cover")
        if upload is None:
            raise field_error("cover", "Bitte ein Bild auswählen.")
        books.set_cover(
            conn,
            c.storage,
            book_id,
            upload.file,
            upload.filename,
            upload.content_type,
            misc.get_limits(conn, c.settings).bytes_for("cover"),
        )

    return _book_action(request, conn, post, book_id, action, "Cover gespeichert.")


@router.post("/admin/books/{book_id}/cover/delete")
def book_cover_delete(
    request: Request,
    book_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)
    return _book_action(
        request,
        conn,
        post,
        book_id,
        lambda: books.remove_cover(conn, c.storage, book_id) and None,
        "Cover entfernt.",
    )


@router.get("/admin/books/{book_id}/cover")
def book_cover(
    request: Request,
    book_id: str,
    session: dict = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)
    row = books.get_book_row(conn, book_id)
    if not row["cover_key"] or not c.storage.exists(row["cover_key"]):
        raise NotFound("Kein Cover vorhanden.")
    return FileResponse(c.storage.path(row["cover_key"]), media_type=row["cover_mime"])


@router.post("/admin/books/{book_id}/archive")
def book_archive(
    request: Request,
    book_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    archived = form_text(post.form, "archived") == "1"
    return _book_action(
        request,
        conn,
        post,
        book_id,
        lambda: books.set_archived(conn, book_id, archived) and None,
        "Buch archiviert. Bestehende Links funktionieren weiter."
        if archived
        else "Buch wieder aktiv.",
    )


@router.get("/admin/books/{book_id}/delete")
def book_delete_confirm(
    request: Request,
    book_id: str,
    session: dict = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    return render(
        request,
        "admin/book_delete.html",
        {"nav": "books", "preview": books.deletion_preview(conn, book_id)},
    )


@router.post("/admin/books/{book_id}/delete")
def book_delete(
    request: Request,
    book_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)
    preview = books.deletion_preview(conn, book_id)
    try:
        if form_text(post.form, "confirm") != "1":
            raise field_error("confirm", "Bitte bestätige die Löschung mit dem Häkchen.")
        try:
            expected = int(form_text(post.form, "expected_link_count", "-1"))
        except ValueError:
            expected = -1
        result = books.delete_book(conn, c.storage, book_id, expected)
    except AppError as exc:
        if isinstance(exc, NotFound):
            raise
        return render(
            request,
            "admin/book_delete.html",
            {"nav": "books", "preview": preview},
            error=error_info(exc),
        )
    flash(
        conn,
        post.session,
        "success",
        f"„{preview['title']}“ wurde gelöscht. {result['links_invalidated']} Links sind "
        f"damit ungültig, {result['files_deleted']} Dateien wurden entfernt.",
    )
    return redirect("/admin/books")


# ---------------------------------------------------------------------------
# Ausgaben und Dateien
# ---------------------------------------------------------------------------


@router.post("/admin/books/{book_id}/editions")
def edition_create(
    request: Request,
    book_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    def action() -> str:
        data = EditionCreate.model_validate(
            {
                "note": form_text(post.form, "note"),
                "copy_formats": [
                    v for v in post.form.getlist("copy_formats") if isinstance(v, str)
                ],
            }
        )
        edition = books.create_edition(conn, book_id, data)
        return f"Entwurf für Ausgabe {edition['number']} angelegt."

    return _book_action(request, conn, post, book_id, action, None)


@router.post("/admin/books/{book_id}/editions/{edition_id}/files")
def edition_upload(
    request: Request,
    book_id: str,
    edition_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)

    def action() -> str:
        limits = misc.get_limits(conn, c.settings)
        uploads = {fmt: upload for fmt in ("pdf", "epub") if (upload := _upload(post.form, fmt))}
        if not uploads:
            raise field_error("pdf", "Bitte mindestens eine Datei auswählen.")
        books.upload_files(
            conn,
            c.storage,
            book_id=book_id,
            edition_id=edition_id,
            uploads={
                fmt: (item.file, item.filename, item.content_type) for fmt, item in uploads.items()
            },
            max_bytes={fmt: limits.bytes_for(fmt) for fmt in uploads},
            epub_limits=_epub_limits(c),
        )
        uploaded = [fmt.upper() for fmt in uploads]
        return f"{' und '.join(uploaded)} gespeichert."

    return _book_action(request, conn, post, book_id, action, None)


@router.post("/admin/books/{book_id}/editions/{edition_id}/files/{fmt}/delete")
def edition_file_delete(
    request: Request,
    book_id: str,
    edition_id: str,
    fmt: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)
    return _book_action(
        request,
        conn,
        post,
        book_id,
        lambda: books.delete_file(conn, c.storage, book_id, edition_id, fmt) and None,
        f"{fmt.upper()}-Datei aus dem Entwurf entfernt.",
    )


@router.get("/admin/books/{book_id}/editions/{edition_id}/files/{fmt}")
def edition_file_download(
    request: Request,
    book_id: str,
    edition_id: str,
    fmt: str,
    session: dict = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    """Kontroll-Download für den Administrator (zählt nicht als Käuferdownload)."""
    c = ctx(request)
    book = books.get_book_row(conn, book_id)
    books.get_edition(conn, book_id, edition_id)
    row = conn.execute(
        "SELECT * FROM edition_files WHERE edition_id = ? AND format = ?", (edition_id, fmt)
    ).fetchone()
    if row is None or not c.storage.exists(row["storage_key"]):
        raise NotFound("Diese Datei gibt es nicht.")
    return FileResponse(
        c.storage.path(row["storage_key"]),
        media_type=MEDIA_TYPES[fmt],
        filename=download_filename(book["title"], f".{fmt}"),
        headers={"Content-Security-Policy": CSP_FILE},
    )


@router.post("/admin/books/{book_id}/editions/{edition_id}/publish")
def edition_publish(
    request: Request,
    book_id: str,
    edition_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    def action() -> str:
        data = EditionPublish.model_validate(
            {"existing_links": form_text(post.form, "existing_links") or None}
        )
        result = books.publish_edition(conn, book_id, edition_id, data)
        text = f"Ausgabe {result['edition']['number']} ist veröffentlicht."
        if result["links_migrated"]:
            text += f" {result['links_migrated']} Links wurden auf die neue Ausgabe umgestellt."
        if result["links_kept"]:
            text += f" {result['links_kept']} Links bleiben an ihrer bisherigen Ausgabe."
        if result["links_without_matching_format"]:
            text += (
                f" Davon wurden {result['links_without_matching_format']} nicht umgestellt, "
                "weil die neue Ausgabe keines ihrer freigegebenen Formate enthält."
            )
        return text

    return _book_action(request, conn, post, book_id, action, None)


@router.post("/admin/books/{book_id}/editions/{edition_id}/delete")
def edition_delete(
    request: Request,
    book_id: str,
    edition_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)

    def action() -> None:
        if form_text(post.form, "confirm") != "1":
            raise field_error("confirm", "Bitte bestätige die Löschung mit dem Häkchen.")
        books.delete_edition(conn, c.storage, book_id, edition_id)

    return _book_action(request, conn, post, book_id, action, "Ausgabe gelöscht.")


@router.post("/admin/books/{book_id}/editions/{edition_id}/migrate-links")
def edition_migrate_links(
    request: Request,
    book_id: str,
    edition_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    def action() -> str:
        if form_text(post.form, "confirm") != "1":
            raise field_error("confirm", "Bitte bestätige die Umstellung mit dem Häkchen.")
        result = books.migrate_links(conn, book_id, edition_id)
        text = f"{result['links_migrated']} Links wurden auf diese Ausgabe umgestellt."
        if result["links_without_matching_format"]:
            text += (
                f" {result['links_without_matching_format']} Links bleiben an ihrer Ausgabe, "
                "weil diese Ausgabe keines ihrer freigegebenen Formate enthält."
            )
        return text

    return _book_action(request, conn, post, book_id, action, None)


# ---------------------------------------------------------------------------
# Käuferlinks
# ---------------------------------------------------------------------------


def _local_datetime(c: AppContext, value: str) -> datetime | None:
    """Wandelt die Eingabe eines datetime-local-Felds in einen Zeitpunkt mit Zeitzone."""
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=c.settings.timezone)
    except ValueError as exc:
        raise field_error("expires_at", "Ungültiges Ablaufdatum.") from exc


def _validity_text(c: AppContext, link: dict[str, Any], language: str = "de") -> str:
    return misc.validity_text(link, language, c.settings.timezone)


def _links_page(
    request: Request, conn: sqlite3.Connection, *, error: ErrorInfo | None = None
) -> Response:
    q = request.query_params.get("q", "").strip()[:200]
    state = request.query_params.get("state", "")
    book_id = request.query_params.get("book_id", "")
    page, offset = _page(request)
    result = links.list_links(
        conn, q=q, state=state or None, book_id=book_id or None, limit=PAGE_SIZE, offset=offset
    )
    return render(
        request,
        "admin/links_list.html",
        {
            "nav": "links",
            "result": result,
            "q": q,
            "state": state,
            "book_id": book_id,
            "page": page,
            "page_size": PAGE_SIZE,
            "all_books": books.list_books(conn, status="all", limit=500)["items"],
        },
        error=error,
    )


@router.get("/admin/links")
def links_list(
    request: Request,
    session: dict = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    return _links_page(request, conn)


def _link_form(
    request: Request,
    conn: sqlite3.Connection,
    values: dict[str, Any],
    *,
    error: ErrorInfo | None = None,
) -> Response:
    available = [
        book
        for book in books.list_books(conn, status="active", limit=500)["items"]
        if book["current_edition"]
    ]
    return render(
        request,
        "admin/link_new.html",
        {"nav": "links", "books": available, "values": values, "form_token": new_token()},
        error=error,
    )


@router.get("/admin/links/new")
def link_new(
    request: Request,
    session: dict = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    values = {
        "book_id": request.query_params.get("book_id", ""),
        "formats": ["pdf", "epub"],
    }
    return _link_form(request, conn, values)


@router.post("/admin/links")
def link_create(
    request: Request,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)
    form = post.form
    values = {
        "book_id": form_text(form, "book_id"),
        "label": form_text(form, "label"),
        "formats": [v for v in form.getlist("formats") if isinstance(v, str)],
        "expires_at": form_text(form, "expires_at"),
        "max_downloads": form_text(form, "max_downloads"),
    }
    # Das versteckte Formular-Token verhindert doppelte Links beim erneuten Absenden.
    form_token = form_text(form, "form_token")
    scope = f"gui:{post.session['user_id']}"
    reserved = False
    try:
        data = LinkCreate.model_validate(
            {**values, "expires_at": _local_datetime(c, values["expires_at"])}
        )
        replay = misc.idempotency_begin(
            conn, scope, form_token, misc.request_hash("gui-link", values)
        )
        if replay is not None:
            link = replay.body
            code = unwrap_code(
                c.settings.secret_key, scope, form_token, link["id"], replay.wrapped_secret or ""
            )
        else:
            reserved = True
            link, code = links.create_link(conn, data)
            misc.idempotency_finish(
                conn,
                scope,
                form_token,
                201,
                link,
                wrap_code(c.settings.secret_key, scope, form_token, link["id"], code),
            )
    except (AppError, ValidationError) as exc:
        if reserved:
            misc.idempotency_abort(conn, scope, form_token)
        return _link_form(request, conn, values, error=error_info(exc))

    url = links.build_url(base_url(request, c.settings), code)
    language = books.get_book_row(conn, link["book_id"])["language"]
    message = misc.render_message(
        misc.get_message_template(conn, language),
        title=link["book_title"],
        url=url,
        validity=_validity_text(c, link, language),
    )
    return render(
        request,
        "admin/link_created.html",
        {
            "nav": "links",
            "link": link,
            "url": url,
            "message": message,
            "language_label": LANGUAGE_LABELS[language],
        },
        status_code=201,
    )


@router.post("/admin/links/lookup")
def link_lookup(
    request: Request,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    try:
        link = links.lookup(conn, form_text(post.form, "code"))
    except AppError as exc:
        return _links_page(request, conn, error=error_info(exc))
    return redirect(f"/admin/links/{link['id']}")


def _link_page(
    request: Request, conn: sqlite3.Connection, link_id: str, *, error: ErrorInfo | None = None
) -> Response:
    link = links.get_link(conn, link_id)
    return render(
        request,
        "admin/link_detail.html",
        {
            "nav": "links",
            "link": link,
            "book": books.get_book(conn, link["book_id"]),
            "editions": [
                e for e in books.list_editions(conn, link["book_id"]) if e["status"] == "published"
            ],
            "stats": misc.download_stats(conn, link_id=link_id),
        },
        error=error,
    )


@router.get("/admin/links/{link_id}")
def link_detail(
    request: Request,
    link_id: str,
    session: dict = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    return _link_page(request, conn, link_id)


def _link_action(
    request: Request,
    conn: sqlite3.Connection,
    post: AdminPost,
    link_id: str,
    action: Any,
    success: str,
    target: str | None = None,
) -> Response:
    try:
        action()
    except (AppError, ValidationError) as exc:
        if isinstance(exc, NotFound):
            raise
        return _link_page(request, conn, link_id, error=error_info(exc))
    flash(conn, post.session, "success", success)
    return redirect(target or f"/admin/links/{link_id}")


@router.post("/admin/links/{link_id}/edit")
def link_edit(
    request: Request,
    link_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)
    form = post.form

    def action() -> None:
        current = links.get_link(conn, link_id)
        payload: dict[str, Any] = {
            "label": form_text(form, "label"),
            "formats": [v for v in form.getlist("formats") if isinstance(v, str)],
            "max_downloads": form_text(form, "max_downloads") or None,
            "edition_id": form_text(form, "edition_id") or current["edition_id"],
        }
        # Das Ablaufdatum nur ändern, wenn es im Formular tatsächlich geändert wurde.
        # Sonst würde ein bereits abgelaufener Link beim Speichern abgewiesen.
        expires = _local_datetime(c, form_text(form, "expires_at"))
        current_expiry = parse_iso(current["expires_at"]) if current["expires_at"] else None
        if (
            expires is None
            or current_expiry is None
            or (expires.replace(second=0) != current_expiry.replace(second=0))
        ):
            payload["expires_at"] = expires
        links.update_link(conn, link_id, LinkUpdate.model_validate(payload))

    return _link_action(request, conn, post, link_id, action, "Änderungen gespeichert.")


@router.post("/admin/links/{link_id}/status")
def link_status(
    request: Request,
    link_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    target = "disabled" if form_text(post.form, "status") == "disabled" else "active"
    return _link_action(
        request,
        conn,
        post,
        link_id,
        lambda: links.update_link(conn, link_id, LinkUpdate(status=target)),
        "Link deaktiviert." if target == "disabled" else "Link wieder aktiv.",
    )


@router.post("/admin/links/{link_id}/revoke")
def link_revoke(
    request: Request,
    link_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    def action() -> None:
        if form_text(post.form, "confirm") != "1":
            raise field_error("confirm", "Bitte bestätige den Widerruf mit dem Häkchen.")
        links.revoke_link(conn, link_id)

    return _link_action(
        request,
        conn,
        post,
        link_id,
        action,
        "Link widerrufen. Er kann nicht mehr verwendet werden.",
    )


@router.post("/admin/links/{link_id}/delete")
def link_delete(
    request: Request,
    link_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    def action() -> None:
        if form_text(post.form, "confirm") != "1":
            raise field_error("confirm", "Bitte bestätige die Löschung mit dem Häkchen.")
        links.delete_link(conn, link_id)

    return _link_action(
        request, conn, post, link_id, action, "Link endgültig gelöscht.", target="/admin/links"
    )


# ---------------------------------------------------------------------------
# Einstellungen, API-Schlüssel, Backups
# ---------------------------------------------------------------------------


def _settings_page(
    request: Request,
    conn: sqlite3.Connection,
    *,
    error: ErrorInfo | None = None,
    values: dict[str, Any] | None = None,
    backup_report: str | None = None,
) -> Response:
    c = ctx(request)
    limits = misc.get_limits(conn, c.settings)
    return render(
        request,
        "admin/settings.html",
        {
            "nav": "settings",
            "limits": limits,
            "message_template": misc.get_message_template(conn),
            "message_template_en": misc.get_message_template(conn, "en"),
            "mail_subject": misc.get_mail_subject(conn, "de"),
            "mail_subject_en": misc.get_mail_subject(conn, "en"),
            "mail_configured": c.settings.mail_configured,
            "mail_from": c.settings.mail_from_email,
            "mail_from_name": c.settings.mail_from_name,
            "whop_configured": bool(c.settings.whop_webhook_secret),
            "api_keys": auth.list_api_keys(conn),
            "scopes": SCOPES,
            "backup": c.backups.status(conn),
            "backup_report": backup_report,
            "values": values or {},
            "public_base_url": c.settings.public_base_url,
            "download_window": c.settings.download_window_minutes,
            "download_window_max": c.settings.download_window_max_hours,
        },
        error=error,
    )


@router.get("/admin/settings")
def settings_page(
    request: Request,
    session: dict = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    return _settings_page(request, conn)


@router.post("/admin/settings")
def settings_save(
    request: Request,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)
    values = {
        name: form_text(post.form, name)
        for name in (
            "max_pdf_mb",
            "max_epub_mb",
            "max_cover_mb",
            "message_template",
            "message_template_en",
            "mail_subject",
            "mail_subject_en",
        )
    }
    try:
        misc.update_settings(conn, c.settings, SettingsUpdate.model_validate(values))
    except (AppError, ValidationError) as exc:
        return _settings_page(request, conn, error=error_info(exc), values=values)
    flash(conn, post.session, "success", "Einstellungen gespeichert.")
    return redirect("/admin/settings")


@router.post("/admin/settings/password")
def settings_password(
    request: Request,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    try:
        data = PasswordChange.model_validate(
            {
                name: form_text(post.form, name)
                for name in ("current_password", "new_password", "new_password_repeat")
            }
        )
        auth.change_password(conn, post.session["user_id"], data, post.session["token"])
    except (AppError, ValidationError) as exc:
        return _settings_page(request, conn, error=error_info(exc))
    flash(conn, post.session, "success", "Passwort geändert. Andere Sitzungen wurden beendet.")
    return redirect("/admin/settings")


@router.post("/admin/settings/api-keys")
def api_key_create(
    request: Request,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    values = {
        "name": form_text(post.form, "name"),
        "scopes": [v for v in post.form.getlist("scopes") if isinstance(v, str)],
    }
    try:
        key, secret = auth.create_api_key(conn, ApiKeyCreate.model_validate(values))
    except (AppError, ValidationError) as exc:
        return _settings_page(request, conn, error=error_info(exc), values=values)
    return render(
        request,
        "admin/apikey_created.html",
        {"nav": "settings", "key": key, "secret": secret},
        status_code=201,
    )


@router.post("/admin/settings/api-keys/{key_id}/revoke")
def api_key_revoke(
    request: Request,
    key_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    try:
        if form_text(post.form, "confirm") != "1":
            raise field_error("confirm", "Bitte bestätige den Widerruf mit dem Häkchen.")
        auth.revoke_api_key(conn, key_id)
    except AppError as exc:
        return _settings_page(request, conn, error=error_info(exc))
    flash(conn, post.session, "success", "API-Schlüssel widerrufen.")
    return redirect("/admin/settings")


@router.post("/admin/settings/backup/run")
def backup_run(
    request: Request,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)
    try:
        results = c.backups.run("manual")
    except AppError as exc:
        return _settings_page(request, conn, error=error_info(exc))
    failed = [item for item in results if item["status"] != "ok"]
    if failed:
        text = "; ".join(f"{item['target']}: {item['detail']}" for item in failed)
        return _settings_page(
            request,
            conn,
            error=ErrorInfo(
                message=f"Backup fehlgeschlagen. {text}",
                code="backup_failed",
                fields=[],
                status_code=500,
            ),
        )
    flash(conn, post.session, "success", "Backup abgeschlossen.")
    return redirect("/admin/settings")


@router.post("/admin/settings/backup/verify")
def backup_verify(
    request: Request,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)
    target = "offsite" if form_text(post.form, "target") == "offsite" else "local"
    try:
        report = c.backups.verify(target)
    except AppError as exc:
        return _settings_page(request, conn, error=error_info(exc))
    return _settings_page(request, conn, backup_report=report)


# ---------------------------------------------------------------------------
# Bestellungen (Whop)
# ---------------------------------------------------------------------------


@router.get("/admin/orders")
def orders_list(
    request: Request,
    session: dict = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)
    q = request.query_params.get("q", "").strip()[:254]
    status = request.query_params.get("status", "")
    page, offset = _page(request)
    return render(
        request,
        "admin/orders_list.html",
        {
            "nav": "orders",
            "result": orders.list_orders(
                conn, q=q, status=status or None, limit=PAGE_SIZE, offset=offset
            ),
            "counts": orders.counts(conn),
            "q": q,
            "status": status,
            "page": page,
            "page_size": PAGE_SIZE,
            "ORDER_STATUS_LABELS": orders.STATUS_LABELS,
            "webhook_url": f"{base_url(request, c.settings)}/webhooks/whop",
            "whop_configured": bool(c.settings.whop_webhook_secret),
            "mail_configured": c.settings.mail_configured,
        },
    )


def _order_page(
    request: Request, conn: sqlite3.Connection, order_id: str, *, error: ErrorInfo | None = None
) -> Response:
    c = ctx(request)
    return render(
        request,
        "admin/order_detail.html",
        {
            "nav": "orders",
            "order": orders.get_order(conn, order_id),
            "mail_configured": c.settings.mail_configured,
        },
        error=error,
    )


@router.get("/admin/orders/{order_id}")
def order_detail(
    request: Request,
    order_id: str,
    session: dict = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    return _order_page(request, conn, order_id)


def _order_action(
    request: Request,
    conn: sqlite3.Connection,
    post: AdminPost,
    order_id: str,
    action: Any,
) -> Response:
    c = ctx(request)
    try:
        result = action()
    except (AppError, ValidationError) as exc:
        if isinstance(exc, NotFound):
            raise
        return _order_page(request, conn, order_id, error=error_info(exc))
    if result == "pending":
        c.mailer.wake()
        message = (
            "Die E-Mail wird versendet."
            if c.settings.mail_configured
            else "Der Link ist bereit. Die E-Mail wird versendet, sobald der Versand "
            "eingerichtet ist."
        )
    else:
        message = f"Status: {orders.STATUS_LABELS.get(result, result)}."
    flash(conn, post.session, "success", message)
    return redirect(f"/admin/orders/{order_id}")


@router.post("/admin/orders/{order_id}/retry")
def order_retry(
    request: Request,
    order_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)
    return _order_action(
        request, conn, post, order_id, lambda: orders.retry(conn, c.settings, order_id)
    )


@router.post("/admin/orders/{order_id}/resend")
def order_resend(
    request: Request,
    order_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)
    return _order_action(
        request,
        conn,
        post,
        order_id,
        lambda: orders.resend_new_link(conn, c.settings, order_id),
    )


@router.post("/admin/orders/{order_id}/email")
def order_email(
    request: Request,
    order_id: str,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)

    def action() -> str:
        address = OrderEmail.model_validate({"email": form_text(post.form, "email")}).email
        orders.update_email(conn, order_id, address)
        order = orders.get_order(conn, order_id)
        if order["status"] == "sent":
            return "sent"
        return orders.retry(conn, c.settings, order_id)

    return _order_action(request, conn, post, order_id, action)


@router.post("/admin/settings/mail-test")
def settings_mail_test(
    request: Request,
    post: AdminPost = Depends(admin_post),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    c = ctx(request)
    language = "en" if form_text(post.form, "language") == "en" else "de"
    try:
        address = OrderEmail.model_validate({"email": form_text(post.form, "email")}).email
        url = f"{base_url(request, c.settings)}/d/BEISPIEL"
        text = mail.fill(
            misc.get_message_template(conn, language),
            title="Beispielbuch" if language == "de" else "Sample Book",
            url=url,
            validity=misc.validity_text(
                {"expires_at": None, "max_downloads": None}, language, c.settings.timezone
            ),
            name="",
        )
        subject = mail.subject_line(
            misc.get_mail_subject(conn, language),
            title="Beispielbuch" if language == "de" else "Sample Book",
            name="",
        )
        mail.send(
            c.settings,
            mail.Email(
                to_email=address,
                to_name="",
                subject=f"[Test] {subject}",
                text=text,
                html=mail.to_html(text, url, language),
            ),
            tags=["test"],
        )
    except mail.MailError as exc:
        return _settings_page(
            request,
            conn,
            error=ErrorInfo(
                message=f"Test-E-Mail nicht versendet. {exc}",
                code="mail_failed",
                fields=[],
                status_code=502,
            ),
        )
    except (AppError, ValidationError) as exc:
        return _settings_page(request, conn, error=error_info(exc))
    flash(conn, post.session, "success", f"Test-E-Mail an {address} versendet.")
    return redirect("/admin/settings#versand")
