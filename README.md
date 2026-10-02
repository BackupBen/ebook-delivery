# E-Book-Auslieferung

Eine kleine Webanwendung, die gekaufte E-Books (PDF und EPUB) über persönliche
Downloadlinks ausliefert. Verkauf und Zahlung finden woanders statt; diese App übernimmt
nur die Datei-Auslieferung.

- **Verwaltung** (`/admin`): Bücher, Ausgaben, Cover, Downloadlinks, API-Schlüssel, Backups.
- **Käuferseite** (`/d/<code>`): Titel, Cover, Download ohne Konto und ohne Tracking.
- **REST-API** (`/api/v1`): dieselben Funktionen für Automatisierungen.

Eine App, eine SQLite-Datenbank, ein Container. Kein Shop, keine Registrierung, keine
Zahlungsabwicklung.

## Inhalt

| Dokument | Thema |
|---|---|
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Deployment in Coolify oder mit Docker Compose, Updates |
| [docs/BACKUP-RESTORE.md](docs/BACKUP-RESTORE.md) | Backups, externes Ziel, Wiederherstellung |
| [docs/API.md](docs/API.md) | API mit curl-Beispielen |
| [docs/SICHERHEIT.md](docs/SICHERHEIT.md) | Sicherheitskonzept, Zählweise der Downloads, Grenzen |
| [docs/openapi.json](docs/openapi.json) | OpenAPI-Spezifikation |
| [.env.example](.env.example) | Alle Umgebungsvariablen |

## Wie es funktioniert

1. **Buch anlegen** und PDF und/oder EPUB in den Entwurf der ersten Ausgabe hochladen.
2. **Ausgabe veröffentlichen.** Veröffentlichte Ausgaben sind unveränderlich.
3. **Downloadlink erstellen.** Der Link gilt für genau ein Buch. Ohne weitere Angaben ist
   er dauerhaft gültig und unbegrenzt nutzbar; Ablaufdatum und Downloadlimit sind optional.
4. **Link versenden.** Die App zeigt den Link und eine Versandnachricht zum Kopieren. Sie
   verschickt selbst nichts.
5. **Bei Bedarf** einen Link deaktivieren (umkehrbar) oder widerrufen (endgültig).

Zwei Dinge sollte man wissen:

- **Ein Link wird nur beim Erstellen angezeigt.** Gespeichert wird nur eine Prüfsumme. Geht
  ein Link verloren, widerruft man ihn und erstellt einen neuen.
- **Neue Ausgaben stellen Links nie stillschweigend um.** Beim Veröffentlichen einer
  weiteren Ausgabe wählt man ausdrücklich, ob bestehende Links die neue Ausgabe erhalten
  oder an ihrer bisherigen bleiben.

Ein Downloadlink ist kein Kopierschutz: Wer den Link kennt, kann die Dateien laden, und
heruntergeladene Dateien lassen sich weitergeben.

## Schnellstart mit Docker

```sh
docker build -t ebook-delivery .

docker run -d --name ebooks -p 127.0.0.1:8000:8000 \
  -v ebook-data:/data -v ebook-backups:/backups \
  -e PUBLIC_BASE_URL=http://127.0.0.1:8000 \
  -e SECRET_KEY="$(openssl rand -base64 48)" \
  -e ADMIN_PASSWORD='ein-langes-passwort' \
  -e BACKUP_PASSWORD='ein-anderes-langes-passwort' \
  ebook-delivery
```

Danach `http://127.0.0.1:8000/admin` öffnen und mit `admin` anmelden. Für den Betrieb im
Internet gehört ein Reverse Proxy mit HTTPS davor, siehe
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Konfiguration

Alles wird über Umgebungsvariablen gesetzt. Im Image stehen keine Domains, Servernamen
oder Secrets. Die wichtigsten:

| Variable | Bedeutung |
|---|---|
| `PUBLIC_BASE_URL` | Öffentliche Adresse, z. B. `https://ebooks.example.com`. Erforderlich im Betrieb. |
| `SECRET_KEY` | Mindestens 32 zufällige Zeichen. Erforderlich. |
| `ADMIN_PASSWORD` | Legt beim ersten Start den Administrator an (mindestens 12 Zeichen). |
| `BACKUP_PASSWORD` | Aktiviert und verschlüsselt die Backups. |
| `BACKUP_OFFSITE_REPOSITORY` | Externes Backup-Ziel im restic-Format. Ohne Wert gibt es nur lokale Backups. |

Die vollständige Liste mit Standardwerten steht in [.env.example](.env.example).

## Daten

| Pfad im Container | Inhalt |
|---|---|
| `/data` | SQLite-Datenbank und Buchdateien. Muss ein persistentes Volume sein. |
| `/backups` | Lokale Backups. Muss ein persistentes Volume sein. |

Buchdateien liegen unter zufälligen Namen in `/data/books` und sind über keinen Webpfad
direkt erreichbar. Jede Auslieferung läuft über die App und wird gegen den Link geprüft.

## Kommandozeile

Im Container steht `ebookctl` zur Verfügung:

| Befehl | Zweck |
|---|---|
| `ebookctl set-password` | Administrator-Passwort setzen (beendet alle Sitzungen) |
| `ebookctl backup` | Backup sofort ausführen |
| `ebookctl snapshots [--source offsite]` | Vorhandene Sicherungsstände auflisten |
| `ebookctl backup-verify [--source offsite]` | Backup vollständig prüfen |
| `ebookctl restore [--source …] [--snapshot …] [--apply]` | Sicherungsstand prüfen und optional zurückspielen |
| `ebookctl gc` | Verwaiste Dateien entfernen |
| `ebookctl openapi` | OpenAPI-Spezifikation ausgeben |

## Entwicklung

```sh
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt && pip install --no-deps -e .

ruff check . && ruff format --check .   # Linter
pytest                                  # Tests (Backup-Tests benötigen restic)

# Lokaler Server
DATA_DIR=./local-data/data BACKUP_DIR=./local-data/backups \
  SECRET_KEY="$(openssl rand -base64 48)" ADMIN_PASSWORD='ein-langes-passwort' \
  PUBLIC_BASE_URL=http://127.0.0.1:8000 ebookctl serve --host 127.0.0.1

# End-to-End-Test gegen den laufenden Server (nur curl)
BASE_URL=http://127.0.0.1:8000 ADMIN_PASSWORD='ein-langes-passwort' scripts/e2e.sh
```

Nach Änderungen an der API: `scripts/export-openapi.sh` aktualisiert `docs/openapi.json`.
Abhängigkeiten sind mit Prüfsummen gepinnt (`requirements.txt`); neu erzeugen mit
`pip-compile --generate-hashes --strip-extras --no-header -o requirements.txt pyproject.toml`.

## Aufbau

```
src/ebookapp/
  config.py        Umgebungsvariablen
  db.py            SQLite, Schema-Migrationen
  security.py      Passwort-Hashing, Tokens, Prüfwerte
  validation.py    Prüfung hochgeladener Dateien
  storage.py       Dateiablage
  backup.py        Backups und Wiederherstellung (restic)
  services/        Geschäftslogik, gemeinsam für Oberfläche und API
  web/             Verwaltung (admin.py), Käuferseite (buyer.py), API (api.py)
  templates/       HTML-Vorlagen
  static/          CSS, JavaScript, lokal ausgelieferte API-Dokumentation
```

Oberfläche und API rufen dieselben Funktionen in `services/` auf. Validierung und
Geschäftsregeln gibt es deshalb nur einmal.
