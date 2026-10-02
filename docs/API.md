# API

Versionierte REST-API unter `/api/v1`. Sie nutzt dieselbe Geschäftslogik, Validierung und
Zugriffskontrolle wie die Verwaltungsoberfläche.

- Interaktive Dokumentation: `/api/v1/docs` (nach Anmeldung in der Verwaltung)
- OpenAPI-Spezifikation: `/api/v1/openapi.json` (mit Anmeldung oder API-Schlüssel) und
  [openapi.json](openapi.json) in diesem Repository

## Anmeldung

API-Schlüssel werden unter *Einstellungen & Backups → API-Schlüssel* erstellt und nur
einmal vollständig angezeigt. Jede Anfrage sendet den Schlüssel im Header:

```
Authorization: Bearer ebk_…
```

Schlüssel in der URL (`?api_key=…`) werden mit 400 abgewiesen.

| Berechtigung | Erlaubt |
|---|---|
| `books:read` | Bücher, Ausgaben und Statistik lesen |
| `books:write` | Bücher anlegen, bearbeiten, archivieren, löschen; Ausgaben anlegen und veröffentlichen |
| `files:write` | PDF, EPUB und Cover hochladen oder entfernen |
| `links:manage` | Käuferlinks erstellen, auflisten, prüfen, ändern, widerrufen, umstellen |

Zwei Aktionen berühren Links, obwohl sie zu Büchern gehören:

- Veröffentlichen mit `existing_links: "migrate"` verlangt zusätzlich `links:manage`.
- `deletion-preview` liefert die Liste der betroffenen Links nur mit `links:manage`; die
  Anzahl ist immer enthalten.

## Beispiele

```sh
export BASE=https://ebooks.example.com/api/v1
export API_KEY=ebk_…
```

### 1. Buch erstellen

```sh
curl -sS -X POST "$BASE/books" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{
        "title": "Mein E-Book",
        "description": "Kurze Beschreibung für die Downloadseite.",
        "whop_product_id": "prod_AbC123xyz"
      }'
```

Die Antwort enthält `id` (z. B. `bk_…`) und unter `draft_edition.id` den Entwurf der
ersten Ausgabe (z. B. `ed_…`).

```sh
export BOOK=bk_…
export EDITION=ed_…
```

### 2. PDF und EPUB hochladen

```sh
curl -sS -X POST "$BASE/books/$BOOK/editions/$EDITION/files" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Idempotency-Key: $(uuidgen)" \
  -F "pdf=@mein-ebook.pdf;type=application/pdf" \
  -F "epub=@mein-ebook.epub;type=application/epub+zip"
```

Beide Felder sind optional, mindestens eines muss vorhanden sein. Ist eine der Dateien
ungültig, wird keine übernommen. Ein Cover lädt man getrennt hoch:

```sh
curl -sS -X PUT "$BASE/books/$BOOK/cover" \
  -H "Authorization: Bearer $API_KEY" \
  -F "file=@cover.jpg;type=image/jpeg"
```

### 3. Ausgabe veröffentlichen

Erste Ausgabe (es gibt noch keine Links):

```sh
curl -sS -X POST "$BASE/books/$BOOK/editions/$EDITION/publish" \
  -H "Authorization: Bearer $API_KEY"
```

Weitere Ausgabe: zuerst einen Entwurf anlegen (hier wird das unveränderte EPUB
übernommen), die neue Datei hochladen und dann **ausdrücklich** entscheiden, was mit den
bestehenden Links geschieht.

```sh
curl -sS -X POST "$BASE/books/$BOOK/editions" \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"note": "Tippfehler korrigiert", "copy_formats": ["epub"]}'

# … neue PDF-Datei in den Entwurf hochladen (wie in Schritt 2) …

curl -sS -X POST "$BASE/books/$BOOK/editions/$NEW_EDITION/publish" \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"existing_links": "keep"}'       # oder "migrate"
```

- `keep`: Bestehende Links liefern weiter ihre bisherige Ausgabe.
- `migrate`: Alle nicht widerrufenen Links liefern ab sofort die neue Ausgabe. Links, deren
  freigegebene Formate in der neuen Ausgabe fehlen, bleiben an ihrer Ausgabe und werden in
  `links_without_matching_format` gezählt.
- Ohne Angabe antwortet die API mit 422 (`existing_links_required`), sobald das Buch Links
  hat. Es gibt absichtlich keinen Standardwert.

### 4. Dauerhaften Käuferlink erzeugen

```sh
curl -sS -X POST "$BASE/links" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d "{\"book_id\": \"$BOOK\", \"label\": \"Bestellung 1042\"}"
```

Ohne `expires_at` und `max_downloads` ist der Link dauerhaft gültig und unbegrenzt
nutzbar. Die Antwort enthält `url` und `code`. **Nur diese Antwort** enthält sie; später
sind sie nicht mehr abrufbar.

Mit Einschränkungen:

```sh
-d "{\"book_id\": \"$BOOK\", \"formats\": [\"pdf\"], \"max_downloads\": 5,
     \"expires_at\": \"2030-12-31T23:59:00+01:00\"}"
```

### 5. Link widerrufen

```sh
export LINK=lnk_…

curl -sS -X POST "$BASE/links/$LINK/revoke" \
  -H "Authorization: Bearer $API_KEY"
```

Der Widerruf ist endgültig; ein erneuter Aufruf ändert nichts. Vorübergehend deaktivieren
und wieder aktivieren:

```sh
curl -sS -X PATCH "$BASE/links/$LINK" \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"status": "disabled"}'           # zurück mit {"status": "active"}
```

### Weitere Aufrufe

```sh
# Bücher suchen
curl -sS "$BASE/books?q=ebook&status=all" -H "Authorization: Bearer $API_KEY"

# Links eines Buchs nach Zustand
curl -sS "$BASE/links?book_id=$BOOK&state=active" -H "Authorization: Bearer $API_KEY"

# Zustand eines Links prüfen (state: active, disabled, revoked, expired, exhausted)
curl -sS "$BASE/links/$LINK" -H "Authorization: Bearer $API_KEY"

# Link zu einem Code finden, den ein Käufer zurückschickt (Code im Body, nie in der URL)
curl -sS -X POST "$BASE/links/lookup" \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"code": "https://ebooks.example.com/d/…"}'

# Downloadstatistik (Zählwerte, ohne Codes und ohne Client-Daten)
curl -sS "$BASE/stats/downloads?date_from=2030-01-01&date_to=2030-01-31" \
  -H "Authorization: Bearer $API_KEY"

# Buch löschen: erst nachsehen, wie viele Links ungültig werden, dann mit der Zahl bestätigen
curl -sS "$BASE/books/$BOOK/deletion-preview" -H "Authorization: Bearer $API_KEY"
curl -sS -X DELETE "$BASE/books/$BOOK?expected_link_count=3" \
  -H "Authorization: Bearer $API_KEY"
```

## Idempotenz

`POST /books`, `POST /books/{id}/editions`, der Datei-Upload und `POST /links` akzeptieren
den Header `Idempotency-Key` (16 bis 200 druckbare ASCII-Zeichen, empfohlen: eine zufällige
UUID).

| Fall | Ergebnis |
|---|---|
| Wiederholung mit gleichem Schlüssel und gleichem Inhalt (innerhalb von 24 Stunden) | Ursprüngliche Antwort, Header `Idempotent-Replayed: true`, kein Duplikat |
| Gleicher Schlüssel, anderer Inhalt | 422 `idempotency_key_reused` |
| Gleicher Schlüssel, erste Anfrage läuft noch | 409 `idempotency_in_progress` |
| Erste Anfrage ist fehlgeschlagen | Der Schlüssel ist wieder frei |

Schlüssel gelten je API-Schlüssel. Bei `POST /links` liefert die Wiederholung denselben
Link einschließlich `code` und `url`. Der Code liegt dafür nicht im Klartext vor, siehe
[SICHERHEIT.md](SICHERHEIT.md).

## Fehler

Fehler haben immer dieselbe Form:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Die Anfrage enthält ungültige Angaben.",
    "fields": [{"field": "title", "message": "Die Angabe ist zu kurz (mindestens 1 Zeichen)."}]
  }
}
```

| Status | Bedeutung |
|---|---|
| 400 | Zugangsdaten in der URL |
| 401 | Schlüssel fehlt, ist ungültig oder widerrufen |
| 403 | Berechtigung fehlt (`details.required_scope` nennt sie) |
| 404 | Buch, Ausgabe oder Link gibt es nicht |
| 409 | Konflikt mit dem Zustand, z. B. Ausgabe bereits veröffentlicht, Bestätigung passt nicht |
| 413 | Datei oder Anfrage zu groß |
| 422 | Ungültige Eingabe, ungültige Datei, fehlende Entscheidung (`existing_links_required`) |
| 429 | Rate Limit; `Retry-After` nennt die Wartezeit in Sekunden |
| 500 | Unerwarteter Fehler; `error.request_id` für Rückfragen angeben |

## Limits

- 300 Anfragen pro Minute und Schlüssel (`API_RATE_PER_MINUTE`).
- Nach 20 fehlgeschlagenen Anmeldungen in 10 Minuten wird die Adresse vorübergehend gesperrt.
- Dateigrößen wie in der Verwaltung eingestellt (*Einstellungen*), höchstens `MAX_UPLOAD_MB`.
- Anfragen ohne gültigen Schlüssel werden abgewiesen, bevor ihr Inhalt gelesen wird.
