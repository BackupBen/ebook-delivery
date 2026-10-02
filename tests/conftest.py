"""Gemeinsame Testwerkzeuge."""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from starlette.testclient import TestClient

from ebookapp import security
from ebookapp.config import Settings, load_settings
from ebookapp.db import open_db
from ebookapp.schemas import ApiKeyCreate
from ebookapp.services import auth
from ebookapp.web.app import create_app

# Passwort-Hashing in Tests beschleunigen (Produktion nutzt N=2^16).
security.SCRYPT_N = 2**10

BASE_URL = "https://ebooks.example.com"
# Die Tests laufen wie in Produktion hinter einem Reverse Proxy: Die Gegenstelle ist eine
# vertrauenswürdige Proxy-Adresse, die Client-Adresse steht in X-Forwarded-For.
PROXY = ("10.0.0.2", 40000)
ADMIN_PASSWORD = "richtig-langes-testpasswort"
ALL_SCOPES = ["books:read", "books:write", "files:write", "links:manage"]


def make_pdf(text: str = "Hallo", size: int = 2000) -> bytes:
    body = f"%PDF-1.7\n% {text}\n".encode()
    filler = b"0" * max(0, size - len(body) - 8)
    return body + filler + b"\n%%EOF\n"


def make_epub(text: str = "Hallo", extra: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
            compress_type=zipfile.ZIP_DEFLATED,
        )
        archive.writestr("OEBPS/content.opf", "<package/>", compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr(
            "OEBPS/chapter1.xhtml", f"<html><body>{text}</body></html>", zipfile.ZIP_DEFLATED
        )
        for name, data in (extra or {}).items():
            archive.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def make_png(width: int = 60, height: int = 90) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (40, 120, 110)).save(buffer, format="PNG")
    return buffer.getvalue()


@dataclass
class Admin:
    """Angemeldete Browser-Sitzung."""

    client: TestClient
    csrf: str = ""

    def refresh_csrf(self) -> None:
        page = self.client.get("/admin/books")
        assert page.status_code == 200, page.status_code
        self.csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

    def post(self, url: str, data: dict[str, Any] | None = None, files: Any = None, **kw: Any):
        payload = {"csrf_token": self.csrf, **(data or {})}
        return self.client.post(url, data=payload, files=files, **kw)

    def create_book(self, title: str = "Testbuch", **extra: str) -> str:
        response = self.post("/admin/books", {"title": title, **extra})
        assert response.status_code == 303, response.text
        return response.headers["location"].rsplit("/", 1)[-1]

    def draft_id(self, book_id: str) -> str:
        page = self.client.get(f"/admin/books/{book_id}")
        return re.search(
            rf"/admin/books/{book_id}/editions/(ed_[0-9a-f]+)/files\"", page.text
        ).group(1)

    def upload(self, book_id: str, edition_id: str, pdf: bytes | None, epub: bytes | None = None):
        files = {}
        if pdf is not None:
            files["pdf"] = ("buch.pdf", pdf, "application/pdf")
        if epub is not None:
            files["epub"] = ("buch.epub", epub, "application/epub+zip")
        return self.post(f"/admin/books/{book_id}/editions/{edition_id}/files", files=files)

    def publish(self, book_id: str, edition_id: str, existing_links: str | None = None):
        data = {"existing_links": existing_links} if existing_links else {}
        return self.post(f"/admin/books/{book_id}/editions/{edition_id}/publish", data)

    def published_book(
        self, title: str = "Testbuch", pdf: bytes | None = None, epub: bytes | None = None
    ) -> str:
        book_id = self.create_book(title)
        edition_id = self.draft_id(book_id)
        response = self.upload(
            book_id, edition_id, pdf or make_pdf(title), epub if epub is not None else make_epub()
        )
        assert response.status_code == 303, response.text
        assert self.publish(book_id, edition_id).status_code == 303
        return book_id

    def create_link(self, book_id: str, **extra: Any) -> tuple[str, str]:
        """Erstellt einen Link über die Oberfläche. Liefert (Link-ID, Pfad des Käuferlinks)."""
        new = self.client.get(f"/admin/links/new?book_id={book_id}")
        form_token = re.search(r'name="form_token" value="([^"]+)"', new.text).group(1)
        data = {"book_id": book_id, "formats": ["pdf", "epub"], "form_token": form_token, **extra}
        response = self.post("/admin/links", data)
        assert response.status_code == 201, response.text
        url = re.search(r'id="link-url"[^>]*value="([^"]+)"', response.text).group(1)
        link_id = re.search(r"/admin/links/(lnk_[0-9a-f]+)", response.text).group(1)
        assert url.startswith(BASE_URL + "/d/")
        return link_id, url[len(BASE_URL) :]


@dataclass
class Env:
    app: Any
    settings: Settings
    client: TestClient
    tmp: Path
    extra: dict[str, Any] = field(default_factory=dict)

    def new_client(self, **kwargs: Any) -> TestClient:
        kwargs.setdefault("client", PROXY)
        return TestClient(self.app, base_url=BASE_URL, follow_redirects=False, **kwargs)

    def login(self, client: TestClient | None = None, password: str = ADMIN_PASSWORD) -> Admin:
        client = client or self.client
        page = client.get("/admin/login")
        token = re.search(r'name="login_token" value="([^"]+)"', page.text).group(1)
        response = client.post(
            "/admin/login", data={"username": "admin", "password": password, "login_token": token}
        )
        assert response.status_code == 303, response.text
        admin = Admin(client)
        admin.refresh_csrf()
        return admin

    def api_key(self, scopes: list[str] | None = None, name: str = "test") -> str:
        with open_db(self.settings.db_path) as conn:
            _, key = auth.create_api_key(conn, ApiKeyCreate(name=name, scopes=scopes or ALL_SCOPES))
        return key

    def api(self, scopes: list[str] | None = None) -> TestClient:
        client = self.new_client()
        client.headers["Authorization"] = f"Bearer {self.api_key(scopes)}"
        return client

    def db(self):
        return open_db(self.settings.db_path)


@pytest.fixture
def make_env(tmp_path: Path) -> Iterator[Callable[..., Env]]:
    def factory(**overrides: str) -> Env:
        values = {
            "DATA_DIR": str(tmp_path / "data"),
            "BACKUP_DIR": str(tmp_path / "backups"),
            "SECRET_KEY": "test-secret-key-" + "x" * 32,
            "ADMIN_PASSWORD": ADMIN_PASSWORD,
            "PUBLIC_BASE_URL": BASE_URL,
            "LOG_LEVEL": "INFO",
        }
        values.update(overrides)
        settings = load_settings(values)
        app = create_app(settings)
        client = TestClient(app, base_url=BASE_URL, follow_redirects=False, client=PROXY)
        return Env(app=app, settings=settings, client=client, tmp=tmp_path)

    yield factory


@pytest.fixture
def env(make_env: Callable[..., Env]) -> Env:
    return make_env()


@pytest.fixture
def admin(env: Env) -> Admin:
    return env.login()
