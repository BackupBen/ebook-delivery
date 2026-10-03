"""Zentrale ASGI-Middleware: Host-Prüfung, Größenlimit, Sicherheits-Header, Zugriffslog."""

from __future__ import annotations

import html
import json
import logging
import secrets
import time
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import i18n
from ..config import Settings
from ..i18n import _
from ..logging_setup import redact_path

log = logging.getLogger("ebookapp.access")
error_log = logging.getLogger("ebookapp.error")

SMALL_BODY_LIMIT = 1024 * 1024
MULTIPART_OVERHEAD = 1024 * 1024

CSP_DEFAULT = (
    "default-src 'none'; style-src 'self'; img-src 'self'; script-src 'self'; "
    "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
# Käuferseiten kommen ganz ohne Skripte und Formulare aus.
CSP_BUYER = (
    "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
# Ausgelieferte Buchdateien dürfen im Browser nichts ausführen.
CSP_FILE = "default-src 'none'; sandbox; frame-ancestors 'none'"


class BodyTooLarge(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=413, detail=_("Die Anfrage ist zu groß."))


class CoreMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = self.settings
        request_id = secrets.token_hex(8)
        scope.setdefault("state", {})["request_id"] = request_id
        path: str = scope["path"]
        method: str = scope["method"]
        headers = Headers(scope=scope)
        # Sprache der Verwaltung und der API-Meldungen für diese Anfrage.
        i18n.set_language(i18n.from_cookie_header(headers.get("cookie", "")))
        started = time.perf_counter()
        state: dict[str, Any] = {"status": 500, "started": False, "completed": False}
        is_api = path.startswith("/api/")
        is_buyer = path.startswith("/d/")
        secure = self._is_https(scope, headers)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["started"] = True
                response_headers = MutableHeaders(scope=message)
                response_headers.setdefault("X-Content-Type-Options", "nosniff")
                response_headers.setdefault("X-Frame-Options", "DENY")
                response_headers.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive")
                response_headers.setdefault(
                    "Referrer-Policy", "same-origin" if path.startswith("/admin") else "no-referrer"
                )
                response_headers.setdefault(
                    "Content-Security-Policy", CSP_BUYER if is_buyer else CSP_DEFAULT
                )
                response_headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                response_headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
                response_headers.setdefault(
                    "Permissions-Policy", "camera=(), geolocation=(), microphone=(), payment=()"
                )
                response_headers.setdefault("Cache-Control", "no-store")
                response_headers.setdefault("X-Request-ID", request_id)
                if secure:
                    response_headers.setdefault("Strict-Transport-Security", "max-age=31536000")
                if "server" in response_headers:
                    del response_headers["server"]
            elif message["type"] == "http.response.body" and not message.get("more_body"):
                state["completed"] = True
            await send(message)

        async def respond(status: int, code: str, message: str, extra: dict | None = None) -> None:
            if is_api:
                body = json.dumps(
                    {"error": {"code": code, "message": message, "request_id": request_id}},
                    ensure_ascii=False,
                ).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            else:
                body = _plain_page(status, message, request_id).encode("utf-8")
                content_type = "text/html; charset=utf-8"
            response_headers = [
                (b"content-type", content_type.encode("ascii")),
                (b"content-length", str(len(body)).encode("ascii")),
            ]
            for name, value in (extra or {}).items():
                response_headers.append((name.lower().encode("ascii"), value.encode("ascii")))
            await send_wrapper(
                {"type": "http.response.start", "status": status, "headers": response_headers}
            )
            await send_wrapper(
                {"type": "http.response.body", "body": b"" if method == "HEAD" else body}
            )

        try:
            # 1. Host-Header prüfen (nur wenn PUBLIC_BASE_URL gesetzt ist).
            if "*" not in settings.allowed_hosts:
                host = headers.get("host", "").lower()
                hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
                if hostname not in settings.allowed_hosts:
                    await respond(400, "invalid_host", _("Unbekannter Hostname."))
                    return

            # 2. Wartungsmodus während einer Wiederherstellung.
            if path != "/healthz" and settings.maintenance_flag.exists():
                await respond(
                    503,
                    "maintenance",
                    _("Wartungsarbeiten. Bitte in wenigen Minuten erneut versuchen."),
                    {"Retry-After": "60"},
                )
                return

            # 3. Größe des Anfragekörpers begrenzen, bevor er gelesen wird.
            content_type = headers.get("content-type", "")
            limit = (
                settings.max_upload_bytes * 2 + MULTIPART_OVERHEAD
                if content_type.startswith("multipart/form-data")
                else SMALL_BODY_LIMIT
            )
            declared = headers.get("content-length")
            if declared is not None:
                try:
                    too_large = int(declared) > limit
                except ValueError:
                    await respond(400, "bad_request", _("Ungültige Content-Length."))
                    return
                if too_large:
                    await respond(
                        413,
                        "payload_too_large",
                        _("Die Anfrage ist zu groß."),
                        {"Connection": "close"},
                    )
                    return

            received = 0

            async def receive_wrapper() -> Message:
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > limit:
                        raise BodyTooLarge()
                return message

            await self.app(scope, receive_wrapper, send_wrapper)
        except Exception as exc:
            if isinstance(exc, BodyTooLarge):
                if not state["started"]:
                    await respond(413, "payload_too_large", _("Die Anfrage ist zu groß."))
            else:
                # Der Traceback enthält keine URL; der Pfad wird bereinigt protokolliert.
                error_log.exception(
                    "Unerwarteter Fehler request_id=%s %s %s", request_id, method, redact_path(path)
                )
                if not state["started"]:
                    await respond(
                        500,
                        "internal_error",
                        _("Unerwarteter Serverfehler. Bitte die Fehler-ID angeben."),
                    )
                elif not state["completed"]:
                    # Die Antwort wurde mitten in der Übertragung abgebrochen.
                    raise
        finally:
            duration = (time.perf_counter() - started) * 1000
            level = logging.DEBUG if path == "/healthz" else logging.INFO
            log.log(
                level,
                "%s %s %s %d %.0fms",
                request_id,
                method,
                redact_path(path),
                state["status"],
                duration,
            )

    def _is_https(self, scope: Scope, headers: Headers) -> bool:
        if scope.get("scheme") == "https":
            return True
        if self.settings.public_base_url:
            return self.settings.public_base_url.startswith("https://")
        return headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"


def _plain_page(status: int, message: str, request_id: str) -> str:
    title = html.escape(_("Fehler %(code)s", code=status))
    error_id = html.escape(_("Fehler-ID"))
    return (
        f'<!doctype html><html lang="{i18n.current()}"><head><meta charset="utf-8">'
        '<meta name="robots" content="noindex, nofollow">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title>"
        '<link rel="stylesheet" href="/static/app.css"></head>'
        '<body><main class="narrow"><div class="card">'
        f"<h1>{title}</h1><p>{html.escape(message)}</p>"
        f'<p class="muted">{error_id}: <code>{html.escape(request_id)}</code></p>'
        "</div></main></body></html>"
    )
