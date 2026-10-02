"""Prüfung hochgeladener Dateien (Format, MIME-Typ, Größe, Dateinamen, ZIP-Bombs)."""

from __future__ import annotations

import io
import struct
import zipfile
from pathlib import Path

import pytest

from ebookapp.errors import Invalid
from ebookapp.validation import (
    EpubLimits,
    download_filename,
    sanitize_filename,
    validate_epub,
    validate_pdf,
)
from tests.conftest import Env, make_epub, make_pdf, make_png

LIMITS = EpubLimits(max_uncompressed_bytes=50 * 1024 * 1024, max_entries=200, max_ratio=100)


def _write(tmp_path: Path, data: bytes, name: str = "datei") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _epub_raw(entries: list[tuple[str, bytes, int]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data, method in entries:
            archive.writestr(name, data, compress_type=method)
    return buffer.getvalue()


CONTAINER = b'<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>'


# --------------------------------------------------------------------------- PDF


def test_valid_pdf(tmp_path: Path) -> None:
    validate_pdf(_write(tmp_path, make_pdf()))


@pytest.mark.parametrize(
    "data",
    [
        b"Das ist nur Text und kein PDF." * 10,
        b"<html><script>alert(1)</script></html>" * 10 + b"%%EOF",
        b"MZ\x90\x00" + b"\x00" * 200 + b"%PDF-1.7 %%EOF",  # ausführbare Datei mit PDF-Spur
        b"%PDF-1.7\n" + b"x" * 500,  # ohne Endmarke
        b"%PDF-1.7",  # zu kurz
    ],
)
def test_invalid_pdf_is_rejected(tmp_path: Path, data: bytes) -> None:
    with pytest.raises(Invalid):
        validate_pdf(_write(tmp_path, data))


# --------------------------------------------------------------------------- EPUB


def test_valid_epub(tmp_path: Path) -> None:
    validate_epub(_write(tmp_path, make_epub()), LIMITS)


def test_epub_must_be_zip(tmp_path: Path) -> None:
    with pytest.raises(Invalid, match="kein ZIP"):
        validate_epub(_write(tmp_path, make_pdf()), LIMITS)


def test_plain_zip_is_not_an_epub(tmp_path: Path) -> None:
    data = _epub_raw([("hallo.txt", b"hallo", zipfile.ZIP_DEFLATED)])
    with pytest.raises(Invalid, match="mimetype"):
        validate_epub(_write(tmp_path, data), LIMITS)


def test_epub_mimetype_must_be_first_and_stored(tmp_path: Path) -> None:
    late = _epub_raw(
        [
            ("META-INF/container.xml", CONTAINER, zipfile.ZIP_DEFLATED),
            ("mimetype", b"application/epub+zip", zipfile.ZIP_STORED),
            ("content.opf", b"<package/>", zipfile.ZIP_DEFLATED),
        ]
    )
    with pytest.raises(Invalid, match="mimetype"):
        validate_epub(_write(tmp_path, late), LIMITS)


def test_epub_wrong_mimetype_content(tmp_path: Path) -> None:
    data = _epub_raw(
        [
            ("mimetype", b"application/zip", zipfile.ZIP_STORED),
            ("META-INF/container.xml", CONTAINER, zipfile.ZIP_DEFLATED),
            ("content.opf", b"<package/>", zipfile.ZIP_DEFLATED),
        ]
    )
    with pytest.raises(Invalid, match="mimetype"):
        validate_epub(_write(tmp_path, data), LIMITS)


def test_epub_requires_container_and_package(tmp_path: Path) -> None:
    no_container = _epub_raw([("mimetype", b"application/epub+zip", zipfile.ZIP_STORED)])
    with pytest.raises(Invalid, match=r"container\.xml"):
        validate_epub(_write(tmp_path, no_container), LIMITS)
    no_package = _epub_raw(
        [
            ("mimetype", b"application/epub+zip", zipfile.ZIP_STORED),
            ("META-INF/container.xml", CONTAINER, zipfile.ZIP_DEFLATED),
        ]
    )
    with pytest.raises(Invalid, match="Paketdatei"):
        validate_epub(_write(tmp_path, no_package, "b"), LIMITS)


def test_zip_bomb_ratio_is_rejected(tmp_path: Path) -> None:
    bomb = make_epub(extra={"OEBPS/bombe.bin": b"\x00" * (30 * 1024 * 1024)})
    assert len(bomb) < 200 * 1024  # stark komprimiert
    with pytest.raises(Invalid, match="ZIP-Bomb"):
        validate_epub(_write(tmp_path, bomb), LIMITS)


def test_uncompressed_size_limit(tmp_path: Path) -> None:
    import os

    big = make_epub(extra={"OEBPS/gross.bin": os.urandom(3 * 1024 * 1024)})
    small_limit = EpubLimits(max_uncompressed_bytes=1024 * 1024, max_entries=200, max_ratio=100)
    with pytest.raises(Invalid, match="zu groß"):
        validate_epub(_write(tmp_path, big), small_limit)


def test_forged_size_fields_are_caught_by_real_decompression(tmp_path: Path) -> None:
    """Die Größenangabe im Archiv wird gefälscht; gezählt wird, was wirklich entpackt wird."""
    payload = b"A" * (4 * 1024 * 1024)
    data = bytearray(make_epub(extra={"OEBPS/gross.txt": payload}))
    # Unkomprimierte Größe im zentralen Verzeichnis und im lokalen Header verkleinern.
    needle = struct.pack("<I", len(payload))
    assert data.count(needle) >= 2
    forged = bytes(data).replace(needle, struct.pack("<I", 1000))
    limit = EpubLimits(max_uncompressed_bytes=1024 * 1024, max_entries=200, max_ratio=10_000)
    with pytest.raises(Invalid):
        validate_epub(_write(tmp_path, forged), limit)


def test_too_many_entries(tmp_path: Path) -> None:
    extra = {f"OEBPS/seite{i}.xhtml": b"x" for i in range(300)}
    with pytest.raises(Invalid, match="zu viele"):
        validate_epub(_write(tmp_path, make_epub(extra=extra)), LIMITS)


@pytest.mark.parametrize("name", ["../../etc/passwd", "/absolut.txt", "OEBPS/../../x", "a\\b.txt"])
def test_unsafe_paths_are_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(Invalid, match="Pfade"):
        validate_epub(_write(tmp_path, make_epub(extra={name: b"x"})), LIMITS)


def test_encrypted_entries_are_rejected(tmp_path: Path) -> None:
    data = bytearray(make_epub())
    # Bit 0 der Allzweck-Flags im zentralen Verzeichnis des ersten Eintrags setzen.
    index = data.find(b"PK\x01\x02")
    data[index + 8] |= 0x01
    with pytest.raises(Invalid):
        validate_epub(_write(tmp_path, bytes(data)), LIMITS)


def test_truncated_epub_is_rejected(tmp_path: Path) -> None:
    data = make_epub(extra={"OEBPS/text.txt": b"hallo welt " * 5000})
    with pytest.raises(Invalid):
        validate_epub(_write(tmp_path, data[: len(data) // 2]), LIMITS)


# --------------------------------------------------------------------------- Dateinamen


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\x\\Mein Buch.pdf", "Mein Buch.pdf"),
        ("böses\x00\r\nname.pdf", "bösesname.pdf"),
        ("<script>.pdf", "_script_.pdf"),
        ("   ", "ersatz.pdf"),
        (None, "ersatz.pdf"),
        ("...", "ersatz.pdf"),
    ],
)
def test_sanitize_filename(given: str | None, expected: str) -> None:
    assert sanitize_filename(given, "ersatz.pdf") == expected


def test_sanitize_filename_limits_length() -> None:
    result = sanitize_filename("a" * 400 + ".pdf", "x")
    assert len(result) <= 150
    assert result.endswith(".pdf")


def test_download_filename_is_derived_from_title() -> None:
    assert download_filename("Mein Buch: Teil 1/2", ".pdf") == "Mein Buch_ Teil 1_2.pdf"
    assert download_filename("", ".epub") == "ebook.epub"
    assert "/" not in download_filename("../../x", ".pdf")


# --------------------------------------------------------------------------- über die Oberfläche


def test_upload_stores_files_outside_static_with_random_names(env: Env, admin) -> None:
    book_id = admin.create_book()
    edition_id = admin.draft_id(book_id)
    pdf = make_pdf("geheim")
    response = admin.post(
        f"/admin/books/{book_id}/editions/{edition_id}/files",
        files={"pdf": ("../../Mein Buch.pdf", pdf, "application/pdf")},
    )
    assert response.status_code == 303
    with env.db() as conn:
        row = conn.execute("SELECT * FROM edition_files").fetchone()
    assert row["original_filename"] == "Mein Buch.pdf"
    assert "Mein" not in row["storage_key"]
    stored = env.settings.books_dir / row["storage_key"]
    assert stored.read_bytes() == pdf
    assert row["size_bytes"] == len(pdf)

    import ebookapp

    static_dir = Path(ebookapp.__file__).parent / "static"
    assert static_dir not in stored.parents
    assert env.client.get(f"/static/{row['storage_key']}").status_code == 404
    assert env.client.get(f"/books/{row['storage_key']}").status_code == 404
    assert env.client.get(f"/data/books/{row['storage_key']}").status_code == 404


@pytest.mark.parametrize(
    ("field", "filename", "content_type", "data", "message"),
    [
        ("pdf", "buch.pdf", "application/pdf", b"kein pdf" * 50, "kein PDF"),
        ("pdf", "buch.exe", "application/pdf", make_pdf(), "Endung"),
        ("pdf", "buch.pdf", "text/html", make_pdf(), "Dateityp"),
        ("pdf", "buch.pdf", "application/pdf", make_epub(), "kein PDF"),
        ("epub", "buch.epub", "application/epub+zip", make_pdf(), "kein EPUB"),
        ("epub", "buch.epub", "image/png", make_epub(), "Dateityp"),
        ("epub", "buch.zip", "application/zip", make_epub(), "Endung"),
        ("pdf", "buch.pdf", "application/pdf", b"", "leer"),
    ],
)
def test_invalid_uploads_are_rejected_with_persistent_error(
    env: Env, admin, field: str, filename: str, content_type: str, data: bytes, message: str
) -> None:
    book_id = admin.create_book()
    edition_id = admin.draft_id(book_id)
    response = admin.post(
        f"/admin/books/{book_id}/editions/{edition_id}/files",
        files={field: (filename, data, content_type)},
    )
    assert response.status_code in (413, 422)
    assert message in response.text
    assert 'id="error-box"' in response.text  # dauerhafte Fehlermeldung
    assert 'id="error-text"' in response.text  # kopierbarer Fehlertext
    assert "Fehler-ID:" in response.text
    with env.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM edition_files").fetchone()[0] == 0
    assert not list(env.settings.books_dir.rglob("*.pdf"))
    assert not list(env.settings.tmp_dir.glob("*"))  # keine Reste im Zwischenspeicher


def test_file_size_limit_from_settings(env: Env, admin) -> None:
    saved = admin.post(
        "/admin/settings",
        {
            "max_pdf_mb": "1",
            "max_epub_mb": "1",
            "max_cover_mb": "1",
            "message_template": "Hallo {link}",
        },
    )
    assert saved.status_code == 303
    book_id = admin.create_book()
    edition_id = admin.draft_id(book_id)
    response = admin.upload(book_id, edition_id, make_pdf(size=1024 * 1024 + 5000))
    assert response.status_code == 413
    assert "größer als erlaubt (1 MB)" in response.text
    assert admin.upload(book_id, edition_id, make_pdf(size=500_000)).status_code == 303


def test_settings_cannot_exceed_installation_limit(make_env) -> None:
    env = make_env(MAX_UPLOAD_MB="20")
    admin = env.login()
    response = admin.post(
        "/admin/settings",
        {
            "max_pdf_mb": "21",
            "max_epub_mb": "5",
            "max_cover_mb": "5",
            "message_template": "Hallo {link}",
        },
    )
    assert response.status_code == 422
    assert "Höchstens 20 MB" in response.text


def test_request_body_limit(make_env) -> None:
    env = make_env(MAX_UPLOAD_MB="1")
    admin = env.login()
    book_id = admin.create_book()
    edition_id = admin.draft_id(book_id)
    response = admin.upload(book_id, edition_id, make_pdf(size=4 * 1024 * 1024))
    assert response.status_code == 413
    # Kleine Formulare ohne Datei dürfen nicht beliebig groß sein.
    huge = admin.post("/admin/books", {"title": "x", "description": "y" * (2 * 1024 * 1024)})
    assert huge.status_code == 413


def test_replacing_a_draft_file_removes_the_old_one(env: Env, admin) -> None:
    book_id = admin.create_book()
    edition_id = admin.draft_id(book_id)
    admin.upload(book_id, edition_id, make_pdf("alt"))
    admin.upload(book_id, edition_id, make_pdf("neu"))
    files = list(env.settings.books_dir.rglob("*.pdf"))
    assert len(files) == 1
    assert b"neu" in files[0].read_bytes()


def test_published_edition_is_immutable(env: Env, admin) -> None:
    book_id = admin.published_book()
    with env.db() as conn:
        edition_id = conn.execute("SELECT current_edition_id FROM books").fetchone()[0]
    response = admin.upload(book_id, edition_id, make_pdf("heimlich ersetzt"))
    assert response.status_code == 409
    assert "unveränderlich" in response.text


# --------------------------------------------------------------------------- Cover


def test_cover_is_reencoded(env: Env, admin) -> None:
    book_id = admin.create_book()
    png = make_png(3000, 2000)
    response = admin.post(
        f"/admin/books/{book_id}/cover", files={"cover": ("cover.png", png, "image/png")}
    )
    assert response.status_code == 303
    image = env.client.get(f"/admin/books/{book_id}/cover")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"
    assert image.content[:3] == b"\xff\xd8\xff"

    from PIL import Image

    with Image.open(io.BytesIO(image.content)) as decoded:
        assert max(decoded.size) <= 1600


@pytest.mark.parametrize(
    ("filename", "content_type", "data"),
    [
        ("cover.png", "image/png", b"kein bild"),
        ("cover.svg", "image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
        ("cover.png", "image/png", make_pdf()),
        ("cover.gif", "image/gif", b"GIF89a" + b"\x00" * 50),
        ("cover.png", "text/html", make_png()),
    ],
)
def test_invalid_cover_is_rejected(
    env: Env, admin, filename: str, content_type: str, data: bytes
) -> None:
    book_id = admin.create_book()
    response = admin.post(
        f"/admin/books/{book_id}/cover", files={"cover": (filename, data, content_type)}
    )
    assert response.status_code == 422
    with env.db() as conn:
        assert conn.execute("SELECT cover_key FROM books").fetchone()[0] is None
