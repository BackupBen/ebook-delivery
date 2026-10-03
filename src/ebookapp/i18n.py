"""Sprache der Verwaltung, der API-Meldungen und der API-Dokumentation.

Die Texte stehen im Quelltext auf Deutsch und werden mit ``_()`` markiert. Englisch ist
die Standardsprache; die Übersetzungen stehen in ``translations_en.py``. Gewählt wird die
Sprache je Browser über ein Cookie (Umschalter in der Kopfzeile der Verwaltung).

Die Käuferseiten sind davon unabhängig: Sie folgen der Sprache des Buchs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .translations_en import EN

LANGUAGES: tuple[str, ...] = ("en", "de")
DEFAULT_LANGUAGE = "en"
COOKIE_NAME = "ebook_lang"
LANGUAGE_NAMES = {"en": "English", "de": "Deutsch"}

_current: ContextVar[str] = ContextVar("ui_language", default=DEFAULT_LANGUAGE)


def normalize(value: str | None) -> str:
    return value if value in LANGUAGES else DEFAULT_LANGUAGE


def current() -> str:
    return _current.get()


def set_language(language: str | None) -> None:
    _current.set(normalize(language))


@contextmanager
def language(value: str) -> Iterator[None]:
    token = _current.set(normalize(value))
    try:
        yield
    finally:
        _current.reset(token)


def from_cookie_header(cookie_header: str) -> str:
    """Liest die Sprache aus der Cookie-Kopfzeile, ohne weitere Cookies zu zerlegen."""
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            return normalize(value.strip())
    return DEFAULT_LANGUAGE


def gettext(message: str, **values: Any) -> str:
    """Übersetzt einen deutschen Quelltext. Platzhalter im Stil ``%(name)s``."""
    text = message if _current.get() == "de" else EN.get(message, message)
    return text % values if values else text


def mark(message: str) -> str:
    """Markiert einen Text zur Übersetzung, ohne ihn zu übersetzen (z. B. in Konstanten)."""
    return message


_ = gettext
N_ = mark


# ---------------------------------------------------------------------------
# OpenAPI-Spezifikation
# ---------------------------------------------------------------------------

# Felder der Spezifikation, deren Texte übersetzt werden.
OPENAPI_KEYS = ("summary", "description")
# Von FastAPI erzeugte, bereits englische Texte.
OPENAPI_UNTRANSLATED = frozenset({"Successful Response", "Validation Error"})


def untranslated_openapi() -> dict[str, Any]:
    from .web.api import build_api

    return build_api(None).openapi()  # type: ignore[arg-type]


def translate_openapi(spec: dict[str, Any]) -> dict[str, Any]:
    """Liefert eine übersetzte Kopie (die zwischengespeicherte Spezifikation bleibt)."""

    def walk(value: Any, parent: str | None = None) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key in OPENAPI_KEYS and isinstance(item, str):
                    result[key] = gettext(item)
                elif key == "tags" and isinstance(item, list):
                    result[key] = [
                        gettext(tag) if isinstance(tag, str) else walk(tag, "tag") for tag in item
                    ]
                elif key == "name" and parent == "tag" and isinstance(item, str):
                    result[key] = gettext(item)
                else:
                    result[key] = walk(item, "tag" if key == "tags" else None)
            return result
        if isinstance(value, list):
            return [walk(item, parent) for item in value]
        return value

    translated = walk(spec)
    info = translated.get("info")
    if isinstance(info, dict) and isinstance(info.get("title"), str):
        info["title"] = gettext(info["title"])
    return translated
