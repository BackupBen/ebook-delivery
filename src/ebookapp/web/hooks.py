"""Eingehende Webhooks (Whop)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from ..db import open_db
from ..i18n import _
from ..security import rate_key
from ..services import orders
from .common import audit, client_ip, ctx

router = APIRouter(include_in_schema=False)
log = logging.getLogger("ebookapp.orders")

WEBHOOK_RATE_PER_MINUTE = 120


@router.post("/webhooks/whop")
async def whop_webhook(request: Request) -> Response:
    c = ctx(request)
    wait = c.limiter.hit(
        f"webhook:{rate_key(client_ip(request, c.settings))}", WEBHOOK_RATE_PER_MINUTE, 60
    )
    if wait:
        return JSONResponse(
            {"error": {"code": "rate_limited", "message": _("Zu viele Anfragen.")}},
            status_code=429,
            headers={"Retry-After": str(wait)},
        )
    # Die Signatur gilt für die unveränderten Rohdaten.
    body = await request.body()

    def handle() -> str:
        with open_db(c.settings.db_path) as conn:
            return orders.handle_whop_webhook(conn, c.settings, request.headers, body)

    try:
        result = await run_in_threadpool(handle)
    except orders.WebhookRejected as exc:
        log.warning("Whop-Webhook abgelehnt (%d): %s", exc.status_code, exc.message)
        if exc.status_code in (400, 401):
            with open_db(c.settings.db_path) as conn:
                audit(request, conn, "webhook_rejected", detail=exc.message)
        return JSONResponse(
            {"error": {"code": "webhook_rejected", "message": exc.message}},
            status_code=exc.status_code,
        )
    if result.startswith("pending"):
        c.mailer.wake()
    return JSONResponse({"status": "ok", "result": result.split(":", 1)[0]})
