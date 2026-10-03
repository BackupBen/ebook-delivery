"""Eingabe- und Ausgabemodelle. GUI und API validieren mit denselben Modellen."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from .i18n import N_, _

Format = Literal["pdf", "epub"]
Scope = Literal["books:read", "books:write", "files:write", "links:manage"]
SCOPES: tuple[str, ...] = ("books:read", "books:write", "files:write", "links:manage")
SCOPE_LABELS = {
    "books:read": N_("Bücher lesen"),
    "books:write": N_("Bücher bearbeiten"),
    "files:write": N_("Dateien hochladen"),
    "links:manage": N_("Käuferlinks verwalten"),
}

WHOP_PATTERN = r"^prod_[A-Za-z0-9_-]{3,64}$"

Language = Literal["de", "en"]
LANGUAGES: tuple[str, ...] = ("de", "en")
LANGUAGE_LABELS = {"de": N_("Deutsch"), "en": N_("Englisch")}


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def _unique_formats(value: list[str]) -> list[str]:
    if len(set(value)) != len(value):
        raise ValueError("duplicate formats")
    return sorted(value, key=("pdf", "epub").index)


class BookCreate(_Input):
    title: str = Field(min_length=1, max_length=200, description="Titel des Buchs")
    description: str = Field(default="", max_length=5000, description="Optionale Beschreibung")
    whop_product_id: str | None = Field(
        default=None,
        pattern=WHOP_PATTERN,
        description="Optionale Whop-Produkt-ID (prod_…). Ordnet Zahlungen aus Whop diesem Buch zu.",
        examples=["prod_AbC123xyz"],
    )
    language: Language = Field(
        default="de",
        description="Sprache der Käuferseite und der Versandnachricht (de oder en)",
    )

    _blank = field_validator("whop_product_id", mode="before")(_blank_to_none)


class BookUpdate(_Input):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    whop_product_id: str | None = Field(default=None, pattern=WHOP_PATTERN)
    language: Language | None = Field(default=None)

    _blank = field_validator("whop_product_id", mode="before")(_blank_to_none)


class EditionCreate(_Input):
    note: str = Field(default="", max_length=500, description="Interne Notiz zur Ausgabe")
    copy_formats: list[Format] = Field(
        default_factory=list,
        description="Formate, deren Datei aus der aktuellen Ausgabe übernommen wird",
    )

    _formats = field_validator("copy_formats")(_unique_formats)


class EditionPublish(_Input):
    existing_links: Literal["migrate", "keep"] | None = Field(
        default=None,
        description=(
            "Pflicht, sobald das Buch bereits Käuferlinks hat: „migrate“ stellt alle nicht "
            "widerrufenen Links auf die neue Ausgabe um, „keep“ lässt sie an ihrer bisherigen "
            "Ausgabe. Es gibt absichtlich keinen Standardwert."
        ),
    )


class LinkCreate(_Input):
    book_id: str = Field(min_length=1, max_length=64)
    edition_id: str | None = Field(
        default=None,
        max_length=64,
        description="Veröffentlichte Ausgabe; Standard ist die aktuelle Ausgabe des Buchs",
    )
    label: str = Field(
        default="",
        max_length=200,
        description="Interne Bezeichnung zur Zuordnung. Bitte keine personenbezogenen Daten.",
    )
    formats: list[Format] = Field(
        default_factory=lambda: ["pdf", "epub"],
        min_length=1,
        description="Freigegebene Formate",
    )
    expires_at: AwareDatetime | None = Field(
        default=None, description="Ablaufzeitpunkt mit Zeitzone; ohne Angabe dauerhaft gültig"
    )
    max_downloads: int | None = Field(
        default=None, ge=1, le=1_000_000, description="Downloadlimit; ohne Angabe unbegrenzt"
    )

    _blank = field_validator("edition_id", "expires_at", "max_downloads", mode="before")(
        _blank_to_none
    )
    _formats = field_validator("formats")(_unique_formats)


class LinkUpdate(_Input):
    label: str | None = Field(default=None, max_length=200)
    formats: list[Format] | None = Field(default=None, min_length=1)
    expires_at: AwareDatetime | None = Field(
        default=None, description="null entfernt das Ablaufdatum"
    )
    max_downloads: int | None = Field(
        default=None, ge=1, le=1_000_000, description="null entfernt das Limit"
    )
    status: Literal["active", "disabled"] | None = Field(
        default=None, description="Aktivieren oder vorübergehend deaktivieren"
    )
    edition_id: str | None = Field(
        default=None, max_length=64, description="Link ausdrücklich an eine andere Ausgabe binden"
    )

    _blank = field_validator("expires_at", "max_downloads", mode="before")(_blank_to_none)

    @field_validator("formats")
    @classmethod
    def _formats(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _unique_formats(value)


class LinkLookup(_Input):
    code: str = Field(
        min_length=1,
        max_length=2048,
        description="Link-Code oder vollständiger Käuferlink",
    )


class LinkMigrate(_Input):
    confirm: Literal[True] = Field(
        description="Muss true sein: bestätigt die Umstellung aller nicht widerrufenen Links"
    )


class ApiKeyCreate(_Input):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[Scope] = Field(min_length=1)

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, value: list[str]) -> list[str]:
        return [scope for scope in SCOPES if scope in set(value)]


class SettingsUpdate(_Input):
    max_pdf_mb: int = Field(ge=1)
    max_epub_mb: int = Field(ge=1)
    max_cover_mb: int = Field(ge=1)
    message_template: str = Field(min_length=1, max_length=5000)
    message_template_en: str | None = Field(default=None, max_length=5000)

    mail_subject: str | None = Field(default=None, max_length=200)
    mail_subject_en: str | None = Field(default=None, max_length=200)

    _blank_en = field_validator(
        "message_template_en", "mail_subject", "mail_subject_en", mode="before"
    )(_blank_to_none)


class OrderEmail(_Input):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class PasswordChange(_Input):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)
    new_password_repeat: str = Field(min_length=1, max_length=256)


# ---------------------------------------------------------------------------
# Ausgabemodelle der API
# ---------------------------------------------------------------------------


class FileOut(BaseModel):
    id: str
    format: Format
    original_filename: str
    size_bytes: int
    sha256: str
    created_at: str


class EditionOut(BaseModel):
    id: str
    book_id: str
    number: int
    note: str
    status: Literal["draft", "published"]
    is_current: bool
    published_at: str | None
    created_at: str
    files: list[FileOut]
    links_count: int = Field(
        description="Nicht widerrufene Links, die an diese Ausgabe gebunden sind"
    )


class LinkCounts(BaseModel):
    total: int
    active: int = Field(description="Links, die derzeit Downloads erlauben")


class BookOut(BaseModel):
    id: str
    title: str
    description: str
    whop_product_id: str | None
    language: Language
    status: Literal["active", "archived"]
    has_cover: bool
    current_edition: EditionOut | None
    draft_edition: EditionOut | None
    editions_count: int
    links: LinkCounts
    downloads_total: int
    created_at: str
    updated_at: str


class BookList(BaseModel):
    items: list[BookOut]
    total: int
    limit: int
    offset: int


class EditionList(BaseModel):
    items: list[EditionOut]


LinkState = Literal["active", "disabled", "revoked", "expired", "exhausted"]


class LinkOut(BaseModel):
    """Ein Käuferlink ohne den geheimen Code."""

    id: str
    book_id: str
    book_title: str
    edition_id: str
    edition_number: int
    label: str
    formats: list[Format]
    expires_at: str | None
    max_downloads: int | None
    download_count: int
    status: Literal["active", "disabled", "revoked"]
    state: LinkState = Field(description="Tatsächlicher Zustand inklusive Ablauf und Limit")
    revoked_at: str | None
    last_download_at: str | None
    created_at: str
    updated_at: str


class LinkCreated(LinkOut):
    code: str = Field(description="Geheimer Code. Wird nur in dieser Antwort ausgegeben.")
    url: str = Field(description="Vollständiger Käuferlink. Wird nur in dieser Antwort ausgegeben.")


class LinkList(BaseModel):
    items: list[LinkOut]
    total: int
    limit: int
    offset: int


class PublishResult(BaseModel):
    edition: EditionOut
    links_migrated: int
    links_kept: int
    links_without_matching_format: int = Field(
        description=(
            "Links, die trotz „migrate“ an ihrer Ausgabe bleiben, weil die neue Ausgabe "
            "keines ihrer freigegebenen Formate enthält"
        )
    )


class MigrateResult(BaseModel):
    links_migrated: int
    links_without_matching_format: int


class DeletionPreview(BaseModel):
    book_id: str
    title: str
    links_total: int = Field(description="Alle Links des Buchs; diese Zahl bestätigt die Löschung")
    links_active: int
    links: list[LinkOut] = Field(description="Höchstens 500 Einträge; nur mit links:manage")
    links_truncated: bool
    editions: int
    files: int


class DeleteResult(BaseModel):
    deleted: bool
    links_invalidated: int
    files_deleted: int


class DayCount(BaseModel):
    date: str
    downloads: int


class FormatCount(BaseModel):
    format: Format
    downloads: int


class BookStat(BaseModel):
    book_id: str
    title: str
    downloads: int


class DownloadStats(BaseModel):
    """Zählwerte ohne Link-Codes und ohne personenbezogene Daten."""

    date_from: str
    date_to: str
    total: int
    by_format: list[FormatCount]
    by_day: list[DayCount] = Field(description="Gezählte Downloads pro Tag (UTC)")
    by_book: list[BookStat]


class ErrorBody(BaseModel):
    code: str
    message: str
    fields: list[dict[str, str]] | None = None
    details: dict[str, Any] | None = None


class ErrorOut(BaseModel):
    error: ErrorBody


# ---------------------------------------------------------------------------
# Deutsche Fehlermeldungen für Validierungsfehler
# ---------------------------------------------------------------------------

_MESSAGES = {
    "missing": N_("Pflichtangabe fehlt."),
    "string_too_short": N_("Die Angabe ist zu kurz (mindestens {min_length} Zeichen)."),
    "string_too_long": N_("Die Angabe ist zu lang (höchstens {max_length} Zeichen)."),
    "string_pattern_mismatch": N_("Die Angabe hat nicht das erwartete Format."),
    "string_type": N_("Es wird Text erwartet."),
    "int_type": N_("Es wird eine ganze Zahl erwartet."),
    "int_parsing": N_("Es wird eine ganze Zahl erwartet."),
    "int_from_float": N_("Es wird eine ganze Zahl erwartet."),
    "bool_type": N_("Es wird true oder false erwartet."),
    "bool_parsing": N_("Es wird true oder false erwartet."),
    "greater_than_equal": N_("Der Wert muss mindestens {ge} sein."),
    "less_than_equal": N_("Der Wert darf höchstens {le} sein."),
    "literal_error": N_("Erlaubt sind: {expected}."),
    "extra_forbidden": N_("Dieses Feld ist nicht bekannt."),
    "list_type": N_("Es wird eine Liste erwartet."),
    "too_short": N_("Es wird mindestens ein Eintrag benötigt."),
    "too_long": N_("Zu viele Einträge."),
    "datetime_parsing": N_(
        "Ungültiges Datum. Erwartet wird ISO 8601, z. B. 2030-01-31T23:59:00+01:00."
    ),
    "datetime_from_date_parsing": N_("Ungültiges Datum."),
    "datetime_type": N_("Ungültiges Datum."),
    "timezone_aware": N_("Das Datum benötigt eine Zeitzone, z. B. 2030-01-31T23:59:00+01:00."),
    "value_error": N_("Ungültiger Wert."),
    "json_invalid": N_("Der Anfragekörper ist kein gültiges JSON."),
    "model_attributes_type": N_("Es wird ein JSON-Objekt erwartet."),
    "dict_type": N_("Es wird ein JSON-Objekt erwartet."),
}

_WHOP_HINT = N_("Die Whop-Produkt-ID muss mit „prod_“ beginnen (Buchstaben, Ziffern, _ und -).")


def translate_errors(errors: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Wandelt Pydantic-Fehler in deutsche Feldfehler um."""
    result = []
    for error in errors:
        location = [str(part) for part in error.get("loc", ()) if part not in ("body", "query")]
        field = ".".join(location) or "body"
        template = _(_MESSAGES.get(error.get("type", ""), "Ungültiger Wert."))
        try:
            message = template.format(**(error.get("ctx") or {}))
        except (KeyError, IndexError):
            message = template
        if field == "whop_product_id" and error.get("type") == "string_pattern_mismatch":
            message = _(_WHOP_HINT)
        if field == "email" and error.get("type") == "string_pattern_mismatch":
            message = _("Bitte eine gültige E-Mail-Adresse angeben.")
        result.append({"field": field, "message": message})
    return result


def validation_fields(exc: ValidationError) -> list[dict[str, str]]:
    return translate_errors(exc.errors(include_url=False, include_input=False))
