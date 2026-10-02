"""Texte der Käuferseiten auf Deutsch und Englisch.

Die Sprache richtet sich nach dem Buch des Links. Seiten ohne Buchbezug (Startseite,
unbekannter Link, Rate Limit) folgen der Sprache des Browsers.
"""

from __future__ import annotations

from datetime import datetime

DEFAULT_LANGUAGE = "de"

STATE_PAGES: dict[str, dict[str, tuple[int, str, str]]] = {
    "de": {
        "not_found": (
            404,
            "Link nicht gefunden",
            "Diesen Downloadlink gibt es nicht. Bitte prüfe, ob der Link vollständig kopiert "
            "wurde.",
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
    },
    "en": {
        "not_found": (
            404,
            "Link not found",
            "This download link does not exist. Please check that the link was copied completely.",
        ),
        "disabled": (
            403,
            "Link currently disabled",
            "This download link is disabled at the moment. Please contact the seller.",
        ),
        "revoked": (
            410,
            "Link revoked",
            "This download link has been revoked and can no longer be used. Please contact "
            "the seller.",
        ),
        "expired": (
            410,
            "Link expired",
            "This download link has expired. Please contact the seller if you need a new link.",
        ),
        "exhausted": (
            410,
            "Download limit reached",
            "This link has reached its maximum number of downloads. A download that has "
            "already started can be resumed for a short time. Please contact the seller if "
            "you need a new link.",
        ),
        "format_unavailable": (
            404,
            "Format not available",
            "This format is not available through this link.",
        ),
        "rate_limited": (
            429,
            "Too many requests",
            "Too many requests came from your connection. Please wait a few minutes and try again.",
        ),
        "file_missing": (
            503,
            "File temporarily unavailable",
            "The file cannot be provided right now. Please try again later or contact the seller.",
        ),
    },
}

TEXTS: dict[str, dict[str, str]] = {
    "de": {
        "site_title": "E-Book-Download",
        "home_intro": "Hier werden gekaufte E-Books über persönliche Downloadlinks bereitgestellt.",
        "home_hint": "Wenn du ein E-Book gekauft hast, öffne bitte den Link, den du vom "
        "Verkäufer erhalten hast. Eine Übersicht oder Suche gibt es auf dieser Seite nicht.",
        "download_suffix": "Download",
        "cover_alt": "Cover von {title}",
        "your_ebook": "Dein E-Book",
        "downloads_heading": "Herunterladen",
        "download_button": "{format} herunterladen",
        "hint_pdf": "für Computer, Tablets und zum Drucken",
        "hint_epub": "für E-Book-Reader und Lese-Apps",
        "no_files": "Für diesen Link stehen im Moment keine Dateien bereit. Bitte wende dich "
        "an den Verkäufer.",
        "valid_until": "Dieser Link ist bis zum {date} gültig.",
        "remaining": "Verbleibende Downloads: {count}. Ein unterbrochener Download kann "
        "fortgesetzt werden, ohne erneut zu zählen.",
        "personal": "Dieser Link ist für dich persönlich bestimmt. Jeder, der ihn kennt, kann "
        "die Dateien herunterladen. Bewahre ihn daher gut auf und gib ihn nicht weiter.",
        "save_files": "Speichere die Dateien nach dem Herunterladen auf deinem Gerät.",
        "error_id": "Fehler-ID für Rückfragen:",
    },
    "en": {
        "site_title": "E-book download",
        "home_intro": "This site provides purchased e-books through personal download links.",
        "home_hint": "If you bought an e-book, please open the link you received from the "
        "seller. There is no catalogue or search on this site.",
        "download_suffix": "Download",
        "cover_alt": "Cover of {title}",
        "your_ebook": "Your e-book",
        "downloads_heading": "Download",
        "download_button": "Download {format}",
        "hint_pdf": "for computers, tablets and printing",
        "hint_epub": "for e-readers and reading apps",
        "no_files": "No files are available for this link at the moment. Please contact the "
        "seller.",
        "valid_until": "This link is valid until {date}.",
        "remaining": "Downloads remaining: {count}. An interrupted download can be resumed "
        "without counting again.",
        "personal": "This link is meant for you personally. Anyone who has it can download "
        "the files, so please keep it safe and do not share it.",
        "save_files": "Please save the files on your device after downloading.",
        "error_id": "Error ID for enquiries:",
    },
}

_MONTHS_EN = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def normalize(language: str | None) -> str:
    return language if language in TEXTS else DEFAULT_LANGUAGE


def format_datetime(value: datetime, language: str) -> str:
    """Datum mit Uhrzeit, unabhängig von der Locale des Containers."""
    if language == "en":
        return f"{_MONTHS_EN[value.month - 1]} {value.day}, {value.year}, {value:%H:%M}"
    return value.strftime("%d.%m.%Y, %H:%M Uhr")


def from_accept_language(header: str | None) -> str:
    """Wählt Deutsch oder Englisch nach dem Accept-Language-Header, sonst Deutsch."""
    choices: list[tuple[float, int, str]] = []
    for position, part in enumerate((header or "").split(",")):
        tag, _, params = part.strip().partition(";")
        primary = tag.strip().lower().split("-", 1)[0]
        if primary not in TEXTS:
            continue
        quality = 1.0
        for param in params.split(";"):
            name, _, raw = param.strip().partition("=")
            if name.strip() == "q":
                try:
                    quality = float(raw)
                except ValueError:
                    quality = 0.0
        if quality > 0:
            choices.append((-quality, position, primary))
    return min(choices)[2] if choices else DEFAULT_LANGUAGE
