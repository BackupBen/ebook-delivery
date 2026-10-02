# Deployment

Die App läuft als ein einzelner Container hinter einem Reverse Proxy, der HTTPS übernimmt.
Diese Anleitung beschreibt Coolify (v4) und zusätzlich reines Docker Compose. Platzhalter
wie `ebooks.example.com` oder `KONTO/REPOSITORY` sind durch eigene Werte zu ersetzen.

## Voraussetzungen

| Was | Wozu |
|---|---|
| Domain mit A-Eintrag auf den Server | Coolify stellt das Zertifikat über Let's Encrypt aus. Der Eintrag muss **vor** dem ersten Deployment auflösen. |
| Container-Image | Baut der mitgelieferte GitHub-Workflow, siehe unten. |
| `SECRET_KEY` | Mindestens 32 zufällige Zeichen: `openssl rand -base64 48` |
| `ADMIN_PASSWORD` | Mindestens 12 Zeichen, nur für den ersten Start |
| `BACKUP_PASSWORD` | Verschlüsselt die Backups. Getrennt vom Server aufbewahren. |

## Image

Der Workflow `.github/workflows/ci.yml` führt bei jedem Push auf `main` Linter und Tests
aus, baut das Image, testet es als Container und veröffentlicht es als

```
ghcr.io/KONTO/REPOSITORY:latest
ghcr.io/KONTO/REPOSITORY:sha-<kurz>
ghcr.io/KONTO/REPOSITORY:<version>     # bei Tags wie v1.2.3
```

Der Name ergibt sich aus dem Repository; im Workflow ist nichts fest eingetragen. Für den
Betrieb empfiehlt sich ein fester Tag (`sha-…` oder eine Version) statt `latest`: Dann
ändert ein Redeploy nie unbemerkt die Version, und ein Zurückgehen ist ein Tag-Wechsel.

Ist das Repository privat, ist auch das Image privat. Der Server muss sich dann einmalig
an `ghcr.io` anmelden (`docker login ghcr.io` mit einem Token, der nur `read:packages`
besitzt), oder das Paket wird in den GitHub-Einstellungen auf öffentlich gestellt. Das
Image enthält keine Secrets und keine Buchdateien.

## Variante A: Coolify, Ressource „Docker Image“

1. **Neues Projekt** anlegen, darin **+ Add Resource → Docker Image**. Image:
   `ghcr.io/KONTO/REPOSITORY` mit dem gewünschten Tag.
2. **General**
   - Domains: `https://ebooks.example.com`
   - Ports Exposes: `8000`
   - Ports Mappings: **leer lassen**. Die App darf nur über den Proxy erreichbar sein.
   - Custom Docker Options: `--cap-drop=ALL --init`

     `--security-opt=no-new-privileges:true` ist sinnvoll, lässt sich hier aber nicht
     eintragen: Coolify (getestet mit 4.1.2) kürzt den Wert am ersten Bindestrich, und der
     Container startet nicht. Variante B setzt die Option über `docker-compose.yaml`.
3. **Persistent Storage**, zwei Einträge vom Typ *Volume Mount*:

   | Name | Destination Path |
   |---|---|
   | `ebook-data` | `/data` |
   | `ebook-backups` | `/backups` |

   Coolify stellt dem Namen die Kennung der Ressource voran. Keine *Directory Mounts*
   verwenden: Ein Verzeichnis des Hosts gehört dort `root`, die App läuft aber als
   Benutzer 10001 und könnte nicht schreiben.
4. **Environment Variables** (als „Runtime“, nicht „Build“):

   | Variable | Wert |
   |---|---|
   | `PUBLIC_BASE_URL` | `https://ebooks.example.com` |
   | `SECRET_KEY` | zufälliger Wert |
   | `ADMIN_PASSWORD` | Startpasswort |
   | `BACKUP_PASSWORD` | Backup-Passwort |

   Weitere Variablen nach Bedarf, siehe `.env.example`. Für die automatische Auslieferung
   nach Whop-Zahlungen zusätzlich `WHOP_WEBHOOK_SECRET`, `BREVO_API_KEY` und
   `MAIL_FROM_EMAIL`, siehe [WHOP.md](WHOP.md).
5. **Healthcheck**: nichts einstellen. Das Image bringt einen eigenen Health-Check mit
   (`ebookctl healthcheck`), der Vorrang hat. Coolify leitet erst dann Verkehr auf den
   Container, wenn er „healthy“ meldet.
6. **Resource Limits**: Memory `768m`, CPUs `1`. Die App selbst braucht wenig; Reserve
   benötigen die Passwortprüfung (ca. 64 MB je Anmeldung, höchstens zwei gleichzeitig) und
   restic während eines Backups.
7. **Deploy**.

## Variante B: Coolify, Ressource „Docker Compose“

Die Datei `docker-compose.yaml` aus dem Repository verwenden. Sie setzt zusätzlich ein
schreibgeschütztes Dateisystem (`read_only`), was Variante A über die Coolify-Oberfläche
nicht anbietet. Einzutragen sind dieselben Variablen wie oben sowie
`APP_IMAGE=ghcr.io/KONTO/REPOSITORY:TAG`. Die Domain wird in Coolify dem Dienst `app` mit
Port 8000 zugewiesen.

Ohne Coolify:

```sh
cp .env.example .env      # Werte eintragen, zusätzlich APP_IMAGE=…
docker compose up -d
```

In diesem Fall muss ein eigener Reverse Proxy mit HTTPS vor Port 8000 des Containers
stehen. Den Port nicht direkt ins Internet veröffentlichen.

## Nach dem ersten Deployment

1. `https://ebooks.example.com/healthz` liefert `{"status":"ok"}`.
2. Unter `/admin` mit `admin` und dem Startpasswort anmelden.
3. `ADMIN_PASSWORD` aus den Umgebungsvariablen **entfernen** und unter *Einstellungen* ein
   neues Passwort setzen. Die Variable wird nur ausgewertet, solange es noch keinen
   Administrator gibt; ein späterer Redeploy setzt das Passwort nie zurück.
4. Unter *Einstellungen & Backups* „Backup jetzt ausführen“ und danach „Lokales Backup
   prüfen“.
5. **Redeploy-Probe:** ein Testbuch anlegen, in Coolify *Redeploy* auslösen, prüfen, dass
   das Buch noch da ist. Fehlt es, sind die Volumes nicht eingebunden.
6. Externes Backup-Ziel einrichten, siehe [BACKUP-RESTORE.md](BACKUP-RESTORE.md). Bis
   dahin zeigt die Verwaltung eine Warnung.

## Betriebshinweise

- **Zugriffsprotokoll des Proxys.** Käuferlinks enthalten den geheimen Code im Pfad. Die
  App protokolliert ihn nie. Ein Reverse Proxy würde ihn aber in sein Zugriffsprotokoll
  schreiben, wenn dieses eingeschaltet ist. In einer Standardinstallation von Coolify ist
  das Traefik-Zugriffsprotokoll aus (keine Option `--accesslog` in der Proxy-Konfiguration);
  so sollte es bleiben.
- **Eine Instanz.** Die App ist für genau einen Container ausgelegt (SQLite, Rate Limits
  im Arbeitsspeicher). Nicht mehrfach parallel starten.
- **Vertrauenswürdige Proxys.** Die App wertet `X-Forwarded-For` nur aus, wenn die Anfrage
  aus einem privaten Netz kommt, also vom Proxy. Wer den Container ohne Proxy direkt
  erreichbar macht, sollte `TRUSTED_PROXIES=127.0.0.1/32` setzen.
- **Große Uploads.** Der Proxy darf Anfragen nicht früher begrenzen als die App. Traefik
  setzt von sich aus kein Größenlimit. Bei sehr langsamen Leitungen kann sein Lese-Timeout
  greifen (je nach Version 60 Sekunden ohne Daten).
- **Uhrzeit des Backups.** `BACKUP_HOUR` bezieht sich auf `APP_TIMEZONE`.

## Aktualisieren und Zurückgehen

1. Neuen Tag des Images eintragen (oder bei `latest`: *Redeploy*).
2. Coolify startet den neuen Container und schaltet um, sobald er „healthy“ ist.
3. Datenbank-Migrationen laufen beim Start automatisch.

Zurückgehen: den vorherigen Tag eintragen und neu deployen. Enthielt die neuere Version
eine Datenbank-Migration, vorher das Backup von vor dem Update zurückspielen
(`ebookctl restore`). Version 1 enthält nur das Ausgangsschema.

## Passwort vergessen

Im Terminal des Containers (Coolify: *Terminal*):

```sh
ebookctl set-password
```

Das setzt das Passwort des Administrators und beendet alle Sitzungen.
