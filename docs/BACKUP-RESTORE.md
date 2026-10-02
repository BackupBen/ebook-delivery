# Backups und Wiederherstellung

## Kurzfassung

- Die App sichert täglich Datenbank und Buchdateien mit [restic](https://restic.net):
  verschlüsselt, dedupliziert, mit Aufbewahrungsregel.
- Das **lokale** Backup liegt im Volume `/backups` auf demselben Server. Es schützt vor
  Bedienfehlern und einem beschädigten Datenvolume, **nicht** vor dem Verlust des Servers.
- Ein **externes** Ziel wird nur verwendet, wenn es ausdrücklich eingerichtet wurde. Ohne
  externes Ziel zeigt die Verwaltung dauerhaft eine Warnung.
- Wiederhergestellt wird mit `ebookctl restore` im Container.

## Was gesichert wird

| Inhalt | Im Backup |
|---|---|
| Bücher, Ausgaben, Buchdateien, Cover | ja |
| Downloadlinks (als Prüfsumme), Zähler, Statistik | ja |
| Administrator (Passwort als Hash), API-Schlüssel (als Hash), Einstellungen | ja |
| Angemeldete Sitzungen | nein, nach einer Wiederherstellung neu anmelden |
| Kurzlebige Prüfwerte der Downloadzählung und gespeicherte Wiederholungen (Idempotency-Key) | nein |
| Umgebungsvariablen (`SECRET_KEY`, Passwörter, Zugangsdaten) | **nein**, separat aufbewahren |

`SECRET_KEY` muss nach einer Wiederherstellung nicht derselbe sein: Links, API-Schlüssel
und Passwörter hängen nicht davon ab.

## Ablauf eines Backups

1. Die Datenbank wird über die SQLite-Backup-API in eine eigene Datei kopiert. Das ergibt
   auch im laufenden Betrieb einen in sich stimmigen Stand. Kurzlebige Daten werden aus der
   Kopie entfernt.
2. restic sichert diese Kopie zusammen mit dem Bücherverzeichnis. Buchdateien werden nach
   dem Hochladen nie verändert; während eines Backups löscht die App keine Dateien, sondern
   holt das danach nach. Dadurch passen Datenbankstand und Dateien zusammen.
3. Alte Stände werden nach der Aufbewahrungsregel entfernt.
4. Ist ein externes Ziel eingerichtet, läuft dasselbe dorthin.

Es kann immer nur ein Backup gleichzeitig laufen, auch wenn eines über die Oberfläche und
eines über die Kommandozeile gestartet wird.

## Einrichtung

| Variable | Bedeutung | Standard |
|---|---|---|
| `BACKUP_PASSWORD` | Verschlüsselt das Backup. Ohne Wert sind Backups aus. | leer |
| `BACKUP_HOUR` | Stunde (in `APP_TIMEZONE`), ab der täglich gesichert wird | `3` |
| `BACKUP_KEEP_DAILY` / `_WEEKLY` / `_MONTHLY` | Aufbewahrung | `7` / `4` / `6` |
| `BACKUP_OFFSITE_REPOSITORY` | Externes Ziel im restic-Format | leer |
| `BACKUP_OFFSITE_PASSWORD` | Eigenes Passwort für das externe Ziel | wie `BACKUP_PASSWORD` |

> **Das Backup-Passwort ist nicht wiederherstellbar.** Ohne `BACKUP_PASSWORD` lässt sich
> kein Backup lesen. Es gehört in einen Passwortmanager außerhalb des Servers.

Schlägt ein Lauf fehl, versucht die App es nach einer Stunde erneut. Jeder Lauf steht mit
Ergebnis unter *Einstellungen & Backups*.

### Externes Ziel

Geeignet ist jeder Speicher, den restic ohne zusätzliche Programme erreicht, zum Beispiel
ein S3-kompatibler Objektspeicher. Der Speicher muss außerhalb des Servers liegen; ein
Dienst auf demselben Server zählt nicht.

```sh
# S3-kompatibler Speicher
BACKUP_OFFSITE_REPOSITORY=s3:https://s3.example.com/bucket/ebooks
AWS_ACCESS_KEY_ID=…
AWS_SECRET_ACCESS_KEY=…

# Backblaze B2
BACKUP_OFFSITE_REPOSITORY=b2:bucket:ebooks
B2_ACCOUNT_ID=…
B2_ACCOUNT_KEY=…

# restic REST-Server
BACKUP_OFFSITE_REPOSITORY=rest:https://benutzer:passwort@backup.example.com/ebooks
```

Empfehlungen für den Speicher: eigener Bucket nur für diese Backups; Zugangsdaten, die nur
diesen Bucket lesen und schreiben dürfen; nach Möglichkeit Versionierung oder eine
Löschsperre, damit ein kompromittierter Server alte Stände nicht entfernen kann.

SFTP-Ziele werden vom Image nicht unterstützt (kein SSH-Client enthalten).

Nach dem Eintragen der Variablen neu deployen und unter *Einstellungen & Backups* „Backup
jetzt ausführen“ sowie „Externes Backup prüfen“ auslösen.

## Wiederherstellung

Alle Befehle laufen im Container (Coolify: *Terminal* der Ressource; sonst
`docker exec -it <container> …`).

### Stände ansehen

```sh
ebookctl snapshots                    # lokal
ebookctl snapshots --source offsite   # extern
```

### Prüfen, ohne etwas zu verändern

```sh
ebookctl restore                                   # neuester lokaler Stand
ebookctl restore --source offsite --snapshot 1a2b3c4d
```

Der Stand wird in ein Arbeitsverzeichnis zurückgeholt und geprüft: Integrität der
Datenbank, und für jede Buchdatei Vorhandensein, Größe und SHA-256. Die Ausgabe nennt
`ok`, die Zahl der geprüften Dateien und alle Probleme. Der laufende Betrieb bleibt
unberührt.

### Zurückspielen

```sh
ebookctl restore --apply
```

1. Derselbe Stand wird zurückgeholt und geprüft. Bei Problemen bricht der Befehl ab, ohne
   etwas zu ändern.
2. Die App antwortet für wenige Sekunden mit „Wartungsarbeiten“ (HTTP 503).
3. Der bisherige Stand wird nach `/data/pre-restore-<Zeit>/` verschoben, der
   wiederhergestellte an seine Stelle gesetzt. Schlägt ein Schritt fehl, wird der bisherige
   Stand zurückgelegt.
4. Die App arbeitet ohne Neustart mit dem wiederhergestellten Stand weiter.

Danach:

- **Anmelden.** Alle Sitzungen sind beendet.
- **Prüfen, was nach dem Backup geschah.** Der Stand entspricht dem Zeitpunkt des Backups:
  Später erstellte Bücher und Links fehlen. Später widerrufene Links und API-Schlüssel
  sind **wieder aktiv** und müssen erneut widerrufen werden.
- **Aufräumen.** `/data/pre-restore-<Zeit>/` löschen, sobald feststeht, dass alles stimmt.
  Das Verzeichnis belegt so viel Platz wie der alte Bestand.

Während der Wiederherstellung wird kurzzeitig etwa der doppelte Platz der Buchdateien auf
dem Datenvolume benötigt.

### Der Server ist verloren

1. Die App auf einem neuen Server deployen ([DEPLOYMENT.md](DEPLOYMENT.md)), mit neuen,
   leeren Volumes und denselben Backup-Variablen (`BACKUP_PASSWORD`,
   `BACKUP_OFFSITE_REPOSITORY` und Zugangsdaten). `ADMIN_PASSWORD` wird nicht benötigt.
2. Im Container: `ebookctl restore --source offsite --apply`
3. Mit dem bisherigen Administrator-Passwort anmelden. Bestehende Käuferlinks
   funktionieren wieder, sobald die Domain auf den neuen Server zeigt.

Ohne externes Backup gibt es in diesem Fall nichts wiederherzustellen.

### Backup prüfen

```sh
ebookctl backup-verify                    # liest alle Daten des lokalen Backups
ebookctl backup-verify --source offsite
```

Empfehlung: einmal im Monat `ebookctl restore` (ohne `--apply`) gegen das externe Ziel
ausführen. Nur ein Backup, das sich zurückholen lässt, ist eines.

## Durchgeführter Wiederherstellungstest

Getestet am 2. Oktober 2026 mit dem gebauten Image in einem Container ohne Root-Rechte, mit
schreibgeschütztem Dateisystem und benannten Volumes (über `docker-compose.yaml`). Der
Ausgangsbestand: ein Buch mit zwei Ausgaben, vier Dateieinträgen, drei Links, ein
API-Schlüssel.

| Schritt | Ergebnis |
|---|---|
| `ebookctl backup` | lokal erfolgreich, 4 Dateien, 3,5 MB |
| Redeploy: Container entfernt, neues Image, gleiche Volumes | Käuferseite 200, Datei 200, SHA-256 der Datei identisch; Buch, Ausgaben, Links und Sicherungsstand vorhanden; bisheriges Passwort gilt weiter, geändertes `ADMIN_PASSWORD` wird ignoriert |
| **Szenario A:** Buch gelöscht | Käuferlink liefert 404 |
| `ebookctl restore` | `ok`, 3 Dateien geprüft, nichts verändert (Link weiterhin 404) |
| `ebookctl restore --apply` | Käuferseite 200, Datei 200, SHA-256 identisch |
| **Szenario B:** Datenvolume gelöscht, neuer Container mit leerem Volume | Käuferlink liefert 404 |
| `ebookctl restore --apply` | Käuferseite 200, Datei 200, SHA-256 identisch; Anmeldung mit dem Passwort aus dem Backup; API-Schlüssel aus dem Backup gilt |
| `ebookctl backup-verify` | „no errors were found“ |
| Protokoll des Containers | keine Link-Codes, keine API-Schlüssel, keine Fehler |

Zusätzlich decken automatische Tests ab: Wiederherstellung von einem zweiten Ziel nach
Verlust von Datenvolume **und** lokalem Backup, Erkennen eines unvollständigen Stands,
Zurücklegen des bisherigen Stands bei einem Fehler, Ausschluss kurzlebiger Daten.

**Nicht getestet:** ein echtes externes Ziel. Im Test stand ein zweites Verzeichnis für das
externe Ziel; die Übertragung zu einem Speicheranbieter wurde nicht ausgeführt, weil kein
Ziel bestätigt ist. Nach dem Einrichten sollte Szenario „Der Server ist verloren“ einmal
mit `ebookctl restore --source offsite` (ohne `--apply`) geprüft werden.
