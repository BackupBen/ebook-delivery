"""Serverseitige Prüfung hochgeladener Dateien.

PDF und EPUB werden nie ausgeführt oder entpackt abgelegt. EPUB-Archive werden nur
lesend und mit harten Obergrenzen geprüft (Schutz vor ZIP-Bombs).
"""

from __future__ import annotations

import io
import itertools
import re
import struct
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from PIL import Image, UnidentifiedImageError

from .errors import Invalid, field_error
from .i18n import _

FORMATS = ("pdf", "epub")

EXTENSIONS = {"pdf": (".pdf",), "epub": (".epub",)}
MEDIA_TYPES = {"pdf": "application/pdf", "epub": "application/epub+zip"}

# Vom Client angegebene MIME-Typen, die wir akzeptieren. Browser senden für EPUB häufig
# application/octet-stream. Maßgeblich ist immer der tatsächlich geprüfte Inhalt.
DECLARED_TYPES = {
    "pdf": {"application/pdf", "application/x-pdf", "application/octet-stream"},
    "epub": {
        "application/epub+zip",
        "application/zip",
        "application/x-zip-compressed",
        "application/octet-stream",
    },
    "cover": {"image/jpeg", "image/png", "image/webp", "application/octet-stream"},
}
COVER_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
COVER_FORMATS = ["JPEG", "PNG", "WEBP"]
COVER_MAX_PIXELS = 40_000_000
COVER_MAX_EDGE = 1600
# Pillow verweigert damit übergroße Bilder, bevor sie dekodiert werden.
Image.MAX_IMAGE_PIXELS = COVER_MAX_PIXELS

PDF_HEADER = re.compile(rb"^%PDF-[12]\.\d")
# Ohne verschachtelbare Wiederholungen: lineare Laufzeit auch bei präparierten Eingaben.
ROOTFILE = re.compile(
    rb"<(?:[A-Za-z0-9_.-]{1,40}:)?rootfile\s[^<>]{0,2000}?\bfull-path\s{0,10}=\s{0,10}"
    rb"[\"']([^\"'<>]{1,1000})[\"']"
)
CONTAINER_MAX_BYTES = 64 * 1024

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE = re.compile(r"[^\w .()\[\]+,&'-]", re.UNICODE)


@dataclass(frozen=True)
class EpubLimits:
    max_uncompressed_bytes: int
    max_entries: int
    max_ratio: int


def sanitize_filename(name: str | None, fallback: str) -> str:
    """Entfernt Pfadanteile, Steuerzeichen und ungewöhnliche Zeichen aus einem Dateinamen."""
    text = unicodedata.normalize("NFKC", name or "")
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = _CONTROL.sub("", text)
    text = _UNSAFE.sub("_", text).strip(" .")
    if len(text) > 150:
        stem, dot, ext = text.rpartition(".")
        text = f"{stem[: 150 - len(ext) - 1]}.{ext}" if dot and len(ext) <= 10 else text[:150]
    return text or fallback


def download_filename(title: str, extension: str) -> str:
    """Dateiname für den Käufer, abgeleitet vom Buchtitel."""
    # Schrägstriche gehören bei einem Titel zum Text und sind kein Pfad.
    flat = title.replace("/", "_").replace("\\", "_")
    base = sanitize_filename(flat, "ebook").replace(".", " ").strip() or "ebook"
    return f"{base[:120].strip()}{extension}"


def check_declared(kind: str, filename: str | None, content_type: str | None, field: str) -> None:
    """Prüft Dateiendung und vom Client angegebenen MIME-Typ."""
    extensions = COVER_EXTENSIONS if kind == "cover" else EXTENSIONS[kind]
    lower = (filename or "").lower()
    if not lower.endswith(extensions):
        raise field_error(
            field,
            _(
                "Die Datei muss die Endung %(extensions)s haben.",
                extensions=_(" oder ").join(extensions),
            ),
        )
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared and declared not in DECLARED_TYPES[kind]:
        raise field_error(
            field,
            _(
                "Der angegebene Dateityp „%(type)s“ passt nicht zu %(kind)s.",
                type=declared,
                kind=kind.upper(),
            ),
        )


def validate_pdf(path: Path, field: str = "pdf") -> None:
    size = path.stat().st_size
    if size < 64:
        raise field_error(field, _("Die Datei ist zu klein, um ein gültiges PDF zu sein."))
    with open(path, "rb") as handle:
        head = handle.read(16)
        handle.seek(max(0, size - 2048))
        tail = handle.read()
    if not PDF_HEADER.match(head):
        raise field_error(field, _("Die Datei ist kein PDF (Kennung %PDF- fehlt am Dateianfang)."))
    if b"%%EOF" not in tail:
        raise field_error(
            field, _("Das PDF ist unvollständig oder beschädigt (Endmarke %%EOF fehlt).")
        )


def validate_epub(path: Path, limits: EpubLimits, field: str = "epub") -> None:
    def fail(message: str) -> Invalid:
        return field_error(field, message)

    with open(path, "rb") as handle:
        if handle.read(4) != b"PK\x03\x04":
            raise fail(_("Die Datei ist kein EPUB (kein ZIP-Archiv)."))
        # Das Ende des Archivs nennt Anzahl und Größe der Verzeichniseinträge. Beides wird
        # geprüft, bevor das Verzeichnis überhaupt eingelesen wird.
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - 65_557))
        tail = handle.read()
    index = tail.rfind(b"PK\x05\x06")
    if index < 0 or len(tail) - index < 22:
        raise fail(_("Die Datei ist kein gültiges EPUB (ZIP-Archiv beschädigt)."))
    total_entries, directory_size = struct.unpack("<HI", tail[index + 10 : index + 16])
    if total_entries > limits.max_entries or total_entries == 0xFFFF:
        raise fail(
            _(
                "Das EPUB enthält zu viele Einträge (erlaubt %(max)s).",
                max=limits.max_entries,
            )
        )
    if directory_size > max(1024 * 1024, limits.max_entries * 512):
        raise fail(_("Das EPUB enthält ein ungewöhnlich großes Inhaltsverzeichnis."))
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError) as exc:
        raise fail(_("Die Datei ist kein gültiges EPUB (ZIP-Archiv beschädigt).")) from exc

    with archive:
        infos = archive.infolist()
        if not infos:
            raise fail(_("Das EPUB ist leer."))
        if len(infos) > limits.max_entries:
            raise fail(
                _(
                    "Das EPUB enthält zu viele Einträge (%(count)s, erlaubt %(max)s).",
                    count=len(infos),
                    max=limits.max_entries,
                )
            )

        names: set[str] = set()
        declared_total = 0
        compressed_total = 0
        for info in infos:
            name = info.filename
            parts = name.split("/")
            if (
                not name
                or len(name) > 1024
                or name.startswith("/")
                or "\\" in name
                or "\x00" in name
                or ".." in parts
                or (len(name) > 1 and name[1] == ":")
            ):
                raise fail(_("Das EPUB enthält unzulässige Pfade."))
            if name in names:
                raise fail(_("Das EPUB enthält doppelte Einträge."))
            names.add(name)
            if info.flag_bits & 0x1:
                raise fail(_("Das EPUB enthält verschlüsselte ZIP-Einträge."))
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise fail(_("Das EPUB verwendet ein nicht unterstütztes Kompressionsverfahren."))
            declared_total += info.file_size
            compressed_total += info.compress_size
            if declared_total > limits.max_uncompressed_bytes:
                raise fail(_("Das EPUB ist entpackt zu groß."))
            if info.file_size > 1024 * 1024 and info.file_size > limits.max_ratio * max(
                info.compress_size, 1
            ):
                raise fail(
                    _("Das EPUB hat ein auffälliges Kompressionsverhältnis (ZIP-Bomb-Schutz).")
                )

        if declared_total > limits.max_ratio * max(compressed_total, 1) and declared_total > (
            1024 * 1024
        ):
            raise fail(_("Das EPUB hat ein auffälliges Kompressionsverhältnis (ZIP-Bomb-Schutz)."))

        # Überlappende Einträge sind das Kennzeichen nicht-rekursiver ZIP-Bombs.
        ordered = sorted(infos, key=lambda item: item.header_offset)
        for previous, current in itertools.pairwise(ordered):
            minimum_end = previous.header_offset + 30 + previous.compress_size
            if current.header_offset < minimum_end:
                raise fail(_("Das EPUB enthält überlappende Einträge (ZIP-Bomb-Schutz)."))

        first = infos[0]
        if first.filename != "mimetype" or first.compress_type != zipfile.ZIP_STORED:
            raise fail(_("Die Datei ist kein EPUB (Eintrag „mimetype“ fehlt am Archivanfang)."))
        if first.file_size > 64:
            raise fail(_("Die Datei ist kein EPUB (Eintrag „mimetype“ ist ungültig)."))

        # Tatsächlich entpacken und mitzählen: Die Größenangaben im Archiv können gefälscht
        # sein. Gelesen wird in kleinen Blöcken und nie über die Obergrenze hinaus.
        actual_total = 0
        mimetype = b""
        container = b""
        try:
            for info in infos:
                if info.is_dir():
                    continue
                with archive.open(info) as member:
                    keep = info.filename in ("mimetype", "META-INF/container.xml")
                    buffer = io.BytesIO()
                    while True:
                        chunk = member.read(256 * 1024)
                        if not chunk:
                            break
                        actual_total += len(chunk)
                        if actual_total > limits.max_uncompressed_bytes:
                            raise fail(_("Das EPUB ist entpackt zu groß (ZIP-Bomb-Schutz)."))
                        if keep:
                            buffer.write(chunk)
                            if buffer.tell() > CONTAINER_MAX_BYTES:
                                raise fail(_("Das EPUB enthält ungewöhnlich große Steuerdateien."))
                    if info.filename == "mimetype":
                        mimetype = buffer.getvalue()
                    elif info.filename == "META-INF/container.xml":
                        container = buffer.getvalue()
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, RuntimeError) as exc:
            raise fail(_("Das EPUB ist beschädigt (Einträge lassen sich nicht lesen).")) from exc

        if mimetype.strip() != b"application/epub+zip":
            raise fail(_("Die Datei ist kein EPUB (falscher Inhalt im Eintrag „mimetype“)."))
        if not container:
            raise fail(_("Das EPUB ist unvollständig (META-INF/container.xml fehlt)."))
        match = ROOTFILE.search(container)
        if not match:
            raise fail(_("Das EPUB ist unvollständig (kein Verweis auf die Paketdatei)."))
        try:
            rootfile = match.group(1).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise fail(
                _("Das EPUB ist unvollständig (ungültiger Verweis auf die Paketdatei).")
            ) from exc
        if rootfile not in names:
            raise fail(_("Das EPUB ist unvollständig (Paketdatei fehlt)."))


def process_cover(source: BinaryIO, max_bytes: int, field: str = "cover") -> tuple[bytes, str, str]:
    """Prüft ein Cover und kodiert es neu. Liefert (Bytes, MIME-Typ, Dateiendung).

    Durch das Neukodieren werden Metadaten entfernt und Dateien, die nur vorgeben, ein
    Bild zu sein, zuverlässig abgewiesen.
    """
    data = source.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise field_error(field, _("Das Cover ist zu groß."))
    if not data:
        raise field_error(field, _("Das Cover ist leer."))
    try:
        with Image.open(io.BytesIO(data), formats=COVER_FORMATS) as probe:
            image_format = probe.format
            if image_format not in COVER_FORMATS:
                raise field_error(field, _("Das Cover muss ein JPEG-, PNG- oder WebP-Bild sein."))
            if probe.width * probe.height > COVER_MAX_PIXELS:
                raise field_error(field, _("Das Cover hat zu viele Bildpunkte."))
            probe.verify()
        with Image.open(io.BytesIO(data), formats=COVER_FORMATS) as image:
            image.load()
            if getattr(image, "n_frames", 1) > 1:
                image.seek(0)
            has_alpha = image.mode in ("RGBA", "LA") or "transparency" in image.info
            converted = image.convert("RGBA" if has_alpha else "RGB")
            converted.thumbnail((COVER_MAX_EDGE, COVER_MAX_EDGE), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            if has_alpha:
                converted.save(output, format="PNG", optimize=True)
                return output.getvalue(), "image/png", ".png"
            converted.save(output, format="JPEG", quality=88, optimize=True)
            return output.getvalue(), "image/jpeg", ".jpg"
    except Invalid:
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise field_error(field, _("Das Cover ist kein gültiges Bild.")) from exc
