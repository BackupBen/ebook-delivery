"""E-Mail-Versand über die Transaktions-API von Brevo.

Es gibt bewusst keine weitere Abhängigkeit: Ein einzelner HTTPS-Aufruf mit der
Standardbibliothek genügt. Der API-Schlüssel steht nur in der Umgebung, nie in der
Datenbank oder im Log.
"""

from __future__ import annotations

import html
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..config import Settings

log = logging.getLogger("ebookapp.mail")

TIMEOUT_SECONDS = 15

DEFAULT_SUBJECTS = {
    "de": "Dein E-Book: {titel}",
    "en": "Your e-book: {titel}",
}

BUTTON_LABELS = {"de": "E-Book herunterladen", "en": "Download your e-book"}
FALLBACK_HINTS = {
    "de": "Falls der Button nicht funktioniert, kopiere diesen Link in deinen Browser:",
    "en": "If the button does not work, copy this link into your browser:",
}


@dataclass(frozen=True)
class Email:
    to_email: str
    to_name: str
    subject: str
    text: str
    html: str


class MailError(Exception):
    """Versand fehlgeschlagen. ``permanent``: eine Wiederholung ändert nichts."""

    def __init__(self, message: str, *, permanent: bool) -> None:
        super().__init__(message)
        self.permanent = permanent


def fill(template: str, *, title: str, url: str, validity: str, name: str) -> str:
    """Füllt Platzhalter. Bewusst ohne str.format, damit Vorlagen nichts auslösen."""
    text = template
    if not name:
        # „Hallo {name},“ wird ohne Namen zu „Hallo,“.
        text = re.sub(r"[ \t]*\{name\}", "", text)
    text = text.replace("{name}", name).replace("{titel}", title).replace("{link}", url)
    text = text.replace("{gueltigkeit}", validity)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def subject_line(template: str, *, title: str, name: str) -> str:
    line = fill(template, title=title, url="", validity="", name=name)
    # Kopfzeilen dürfen keine Zeilenumbrüche enthalten.
    return " ".join(line.split())[:200]


def to_html(text: str, url: str, language: str) -> str:
    """Einfaches HTML aus dem Text: Absätze, Zeilenumbrüche und ein Button für den Link."""
    blocks = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in paragraph.splitlines()]
        if url and url in lines:
            before = [line for line in lines[: lines.index(url)] if line]
            after = [line for line in lines[lines.index(url) + 1 :] if line]
            if before:
                blocks.append(_paragraph(before))
            blocks.append(_button(url, language))
            if after:
                blocks.append(_paragraph(after))
            continue
        blocks.append(_paragraph(lines))
    escaped_url = html.escape(url, quote=True)
    if url:
        blocks.append(
            '<p style="color:#5c5a55;font-size:13px;">'
            f"{html.escape(FALLBACK_HINTS.get(language, FALLBACK_HINTS['de']))}<br>"
            f'<a href="{escaped_url}" style="color:#1f5f56;word-break:break-all;">'
            f"{escaped_url}</a></p>"
        )
    body = "\n".join(blocks)
    return (
        f'<!doctype html><html lang="{html.escape(language)}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        '<body style="margin:0;padding:24px;background:#f6f5f1;'
        "font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;"
        'color:#1c1b19;font-size:16px;line-height:1.5;">'
        '<div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:10px;'
        f'padding:28px;">{body}</div></body></html>'
    )


def _paragraph(lines: list[str]) -> str:
    return f'<p style="margin:0 0 16px;">{"<br>".join(html.escape(line) for line in lines)}</p>'


def _button(url: str, language: str) -> str:
    label = html.escape(BUTTON_LABELS.get(language, BUTTON_LABELS["de"]))
    return (
        '<p style="margin:24px 0;">'
        f'<a href="{html.escape(url, quote=True)}" style="display:inline-block;'
        "padding:12px 22px;background:#1f5f56;color:#ffffff;text-decoration:none;"
        f'border-radius:8px;font-weight:600;">{label}</a></p>'
    )


def send(settings: Settings, email: Email, *, tags: list[str] | None = None) -> str:
    """Versendet eine E-Mail und liefert die Message-ID von Brevo."""
    if not settings.mail_configured:
        raise MailError(
            "E-Mail-Versand ist nicht eingerichtet (BREVO_API_KEY und MAIL_FROM_EMAIL).",
            permanent=True,
        )
    sender: dict[str, str] = {"email": settings.mail_from_email or ""}
    if settings.mail_from_name:
        sender["name"] = settings.mail_from_name
    recipient: dict[str, str] = {"email": email.to_email}
    if email.to_name:
        recipient["name"] = email.to_name[:100]
    payload: dict[str, object] = {
        "sender": sender,
        "to": [recipient],
        "subject": email.subject,
        "htmlContent": email.html,
        "textContent": email.text,
    }
    if settings.mail_reply_to:
        payload["replyTo"] = {"email": settings.mail_reply_to}
    if tags:
        payload["tags"] = tags
    request = urllib.request.Request(  # noqa: S310 - feste HTTPS-Adresse aus der Konfiguration
        settings.brevo_api_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "api-key": settings.brevo_api_key or "",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            body = response.read(65536)
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        # 429 und 5xx sind vorübergehend; andere 4xx (z. B. falscher Schlüssel,
        # ungültige Adresse, Absender nicht bestätigt) ändern sich durch Warten nicht.
        permanent = 400 <= exc.code < 500 and exc.code != 429
        raise MailError(f"Brevo antwortete mit {exc.code}: {detail}", permanent=permanent) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MailError(f"Brevo nicht erreichbar: {exc}", permanent=False) from exc
    try:
        message_id = str(json.loads(body or b"{}").get("messageId") or "")
    except (ValueError, AttributeError):
        message_id = ""
    return message_id[:200]


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        data = json.loads(exc.read(65536) or b"{}")
    except (ValueError, OSError):
        return exc.reason or "unbekannter Fehler"
    if isinstance(data, dict):
        return str(data.get("message") or data.get("code") or exc.reason)[:300]
    return str(exc.reason)[:300]
