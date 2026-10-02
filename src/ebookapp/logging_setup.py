"""Logging ohne Geheimnisse.

Käuferlinks und API-Schlüssel dürfen nie in Logs erscheinen. Die Anwendung protokolliert
Pfade deshalb selbst (bereits bereinigt) und filtert zusätzlich jede Logzeile.
"""

from __future__ import annotations

import logging
import re
import sys

# Link-Codes, Sitzungs- und CSRF-Tokens sind 43 Zeichen lange base64url-Werte.
_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")
_LINK = re.compile(r"(?i)/d/+[A-Za-z0-9_-]{16,}")
_API_KEY = re.compile(r"ebk_[0-9a-f]{12}_[A-Za-z0-9_-]{10,}")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_FILE_SEGMENTS = {"pdf", "epub", "cover"}


def redact(text: str) -> str:
    text = _API_KEY.sub("ebk_[redacted]", text)
    text = _LINK.sub("/d/[link]", text)
    text = _TOKEN.sub("[redacted]", text)
    return _BEARER.sub(r"\1[redacted]", text)


def redact_path(path: str) -> str:
    """Pfad für das Zugriffslog. Query-Strings werden nie protokolliert.

    Unterhalb von /d/ wird nichts aus der Anfrage übernommen außer dem Format: Auch
    ungewöhnlich geformte Pfade (doppelte Schrägstriche, Großschreibung, zusätzliche
    Segmente) können sonst einen gültigen Code enthalten.
    """
    parts = [part for part in path.split("/") if part]
    if parts and parts[0].lower() == "d":
        tail = parts[2].lower() if len(parts) > 2 else ""
        return f"/d/[link]/{tail}" if tail in _FILE_SEGMENTS else "/d/[link]"
    return redact(path[:300])


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))
    # uvicorn protokolliert Zugriffe mit vollständiger URL; das übernimmt die App selbst.
    access = logging.getLogger("uvicorn.access")
    access.handlers[:] = []
    access.propagate = False
    access.disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.propagate = True
