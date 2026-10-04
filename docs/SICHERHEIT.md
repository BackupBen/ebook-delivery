# Sicherheit

Dieses Dokument beschreibt, wie die App Dateien und Zugänge schützt, wie Downloads gezählt
werden und wo die Grenzen liegen.

## Wovor die App schützt und wovor nicht

| Schützt | Schützt nicht |
|---|---|
| Abruf von Dateien ohne gültigen Link | Weitergabe eines Links durch den Käufer |
| Erraten von Links (256 Bit Zufall, Rate Limit) | Weitergabe heruntergeladener Dateien |
| Auslesen von Links aus Datenbank, Backups und Protokollen | Mitlesen am Gerät des Käufers |
| Unbefugten Zugriff auf Verwaltung und API | Verlust des Servers ohne externes Backup |

Ein Downloadlink ist ein Zugangsschlüssel, kein Kopierschutz. Die App behauptet nichts
anderes, auch nicht auf der Käuferseite.

## Dateien

- Buchdateien liegen in `/data/books` unter serverseitig erzeugten Zufallsnamen. Kein
  Webpfad zeigt auf dieses Verzeichnis; statisch ausgeliefert werden nur CSS und
  JavaScript der Oberfläche.
- Jede Auslieferung läuft durch die App und wird gegen den Link geprüft: Zustand, Ablauf,
  Limit, freigegebenes Format, gebundene Ausgabe.
- Dateien werden mit `Content-Disposition: attachment`, festem Inhaltstyp,
  `X-Content-Type-Options: nosniff` und `Content-Security-Policy: sandbox` gesendet. Der
  Browser führt sie nicht aus.
- Es gibt keine öffentlichen Listen, keine Suche und keine Import-Funktion für URLs.

## Uploads

Geprüft wird serverseitig, unabhängig von Oberfläche oder API:

| Prüfung | PDF | EPUB | Cover |
|---|---|---|---|
| Dateiendung und angegebener MIME-Typ | ja | ja | ja |
| Tatsächlicher Inhalt | Kennung am Dateianfang, Endmarke | ZIP-Struktur, `mimetype` als erster Eintrag, `container.xml`, Paketdatei | Dekodierung als JPEG, PNG oder WebP |
| Größe | einstellbar | einstellbar | einstellbar, Bildpunkte begrenzt |
| Sonstiges | | siehe unten | wird verkleinert und neu kodiert, Metadaten entfallen |

EPUB-Archive werden nie entpackt abgelegt. Gegen ZIP-Bombs prüft die App, bevor das
Archivverzeichnis gelesen wird, Anzahl und Größe der Einträge; danach Kompressionsverhältnis,
überlappende Einträge, unzulässige Pfade und verschlüsselte Einträge. Zusätzlich wird
jeder Eintrag in kleinen Blöcken tatsächlich dekomprimiert und mitgezählt, weil die
Größenangaben im Archiv gefälscht sein können; bei Erreichen der Obergrenze bricht die
Prüfung ab.

Originale Dateinamen werden bereinigt und nur als Angabe gespeichert. Der Dateiname beim
Download ergibt sich aus dem Buchtitel.

Hochgeladen werden mehrere Dateien eines Formulars ganz oder gar nicht. Anfragen an die API
ohne gültigen Schlüssel werden abgewiesen, bevor ihr Inhalt gelesen wird.

## Geheimnisse

| Geheimnis | Gespeichert als | Sichtbar |
|---|---|---|
| Code eines Käuferlinks (256 Bit) | SHA-256 | einmal, beim Erstellen |
| API-Schlüssel | SHA-256 | einmal, beim Erstellen |
| Administrator-Passwort | scrypt (N=2^16, r=8, p=2) mit Salt | nie |
| Sitzungs-Token | SHA-256 | nur im Cookie |
| Whop-Webhook-Geheimnis, Brevo-API-Schlüssel | nur in der Umgebung | nie |

**Protokolle.** Die App schreibt ihr Zugriffsprotokoll selbst: Methode, bereinigter Pfad,
Status, Dauer, Fehler-ID. Unterhalb von `/d/` wird aus der Anfrage nur das Format
übernommen; Query-Strings werden nie protokolliert. Zusätzlich schwärzt ein Filter in jeder
Logzeile alles, was wie ein Link-Code, Token oder API-Schlüssel aussieht. Adressen von
Käufern werden nicht protokolliert; die Adresse erscheint nur bei fehlgeschlagenen
Anmeldungen an der Verwaltung. Fehlerseiten zeigen eine Fehler-ID, keine internen Details.

**Reverse Proxy.** Der Code steht im Pfad des Links. Ein eingeschaltetes Zugriffsprotokoll
des Proxys würde ihn deshalb aufzeichnen. Siehe [DEPLOYMENT.md](DEPLOYMENT.md).

**Wiederholte API-Anfragen.** `POST /links` mit `Idempotency-Key` liefert bei einer
Wiederholung denselben Link samt Code. Dafür wird der Code 24 Stunden lang in
verschlüsselter Form aufbewahrt. Der Schlüsselstrom hängt vom `SECRET_KEY` des Servers
**und** vom Idempotency-Key des Clients ab; der Idempotency-Key selbst wird nur als
Prüfsumme gespeichert. Aus der Datenbank allein ist der Code nicht zu gewinnen. Wer
allerdings Datenbank und `SECRET_KEY` besitzt und den Idempotency-Key errät, könnte Codes
der letzten 24 Stunden zurückrechnen. Deshalb verlangt die API mindestens 16 Zeichen und
empfiehlt eine zufällige UUID. Diese Einträge gelangen nicht ins Backup. Wer das nicht
möchte, lässt den Header bei `POST /links` weg.

## Verwaltung

- Kein Registrierungsformular. Der Administrator entsteht beim ersten Start aus
  `ADMIN_PASSWORD` oder über `ebookctl set-password`.
- Sitzungen liegen serverseitig. Das Cookie ist `HttpOnly`, `Secure`, `SameSite=Lax` und
  trägt das Präfix `__Host-`. Sitzungen enden nach 120 Minuten ohne Aktivität und spätestens
  nach 24 Stunden. Eine Passwortänderung beendet alle anderen Sitzungen.
- **CSRF:** Jede schreibende Anfrage braucht ein an die Sitzung gebundenes Token. Zusätzlich
  werden `Origin` und `Sec-Fetch-Site` geprüft. Keine Aktion wird per GET ausgelöst.
- **Anmeldung:** Je Anschluss und Benutzername sind 5 Versuche in 15 Minuten möglich. Jeder
  Versuch wird vor der Passwortprüfung gezählt, damit gleichzeitige Anfragen das Limit
  nicht unterlaufen. Die Sperre gilt nur für den Anschluss, von dem die Versuche kommen:
  Ein Angreifer kann den Administrator nicht aussperren. Höchstens zwei Passwortprüfungen
  laufen gleichzeitig.
- Sicherheits-Header auf allen Seiten: strikte Content-Security-Policy ohne Inline-Skripte,
  `X-Frame-Options: DENY`, `nosniff`, HSTS, `X-Robots-Tag: noindex`.

## Zwei-Faktor-Anmeldung

- Unter **Sicherheit** einschaltbar: zeitbasierte Einmalcodes (TOTP, RFC 6238; SHA-1,
  6 Ziffern, 30 Sekunden) aus jeder gängigen Authenticator-App. Das Geheimnis wird mit dem
  `SECRET_KEY` verschlüsselt gespeichert.
- Jeder Code gilt nur einmal (auch nicht innerhalb seines 30-Sekunden-Fensters erneut).
- Nach richtigem Passwort hat der zweite Schritt fünf Versuche und fünf Minuten. Unabhängig
  vom Anschluss sind je Benutzer höchstens zehn falsche Codes je Zeitfenster erlaubt: Auch wer
  das Passwort kennt, kann die Codes nicht durchprobieren.
- Zehn Notfall-Codes (je 50 Bit, nur als Prüfsumme gespeichert, jeweils einmal gültig).
- Ausschalten und neue Notfall-Codes erfordern Passwort **und** Code.
- Notausgang mit Zugriff auf den Server: `ebookctl disable-2fa` (beendet alle Sitzungen).

## Sicherheitsprotokoll und Benachrichtigungen

- Protokolliert werden Anmeldungen und Fehlversuche, Sperren, falsche Codes, Notfall-Codes,
  Änderungen an Passwort, Zwei-Faktor-Anmeldung und Benachrichtigungen, erstellte und
  widerrufene API-Schlüssel, gelöschte Bücher sowie abgelehnte API-Schlüssel und
  Whop-Webhooks; jeweils mit Zeit, Benutzer, Adresse und Browser. Käufer werden nicht
  protokolliert.
- Warnereignisse werden je Anschluss gedrosselt (höchstens 20 je 10 Minuten und Art), damit
  eine Anfrageflut das Protokoll nicht füllt. Einträge werden nach einem Jahr gelöscht,
  höchstens 50 000 bleiben erhalten.
- Optional E-Mail über Brevo bei jeder Anmeldung und/oder bei Warnzeichen (gleiche Warnung
  höchstens alle 30 Minuten). Der Versand verzögert keine Anmeldung.

## Käuferseite

- Keine Skripte, keine Formulare, keine Cookies, keine fremden Quellen.
- `Referrer-Policy: no-referrer`, damit der Link nicht über den Referrer weitergegeben wird.
- `noindex` als Header und im HTML, dazu `robots.txt` mit `Disallow: /`.
- Verständliche Seiten für abgelaufene, widerrufene, deaktivierte und ausgeschöpfte Links.
  Unbekannte Links erhalten 404.
- Kein automatisches Weiterleiten bei abweichender Schreibweise des Pfads; eine
  Weiterleitung würde den Code in einer weiteren Kopfzeile wiederholen.

## Zählweise der Downloads

Ein **Download** ist ein begonnener Abruf einer Datei von einem Internetanschluss aus.

1. **Gezählt wird beim Beginn**, nicht beim Abschluss. Ob eine Übertragung vollständig
   ankommt, kann der Server nicht sicher feststellen.
2. **Derselbe Anschluss, dieselbe Datei: einmal.** Weitere Anfragen zählen nicht erneut,
   solange zwischen zwei Anfragen höchstens 60 Minuten liegen (`DOWNLOAD_WINDOW_MINUTES`)
   und der erste Abruf höchstens 24 Stunden zurückliegt (`DOWNLOAD_WINDOW_MAX_HOURS`). Das
   umfasst Range-Anfragen, parallele Segmente eines Download-Managers, Wiederaufnahmen nach
   einem Abbruch und einen erneuten vollständigen Abruf.
3. **HEAD zählt nie.**
4. **Nicht lieferbare Anfragen zählen nicht:** Bereich hinter dem Dateiende (416), fehlende
   Datei, nicht freigegebenes Format.
5. **Das Limit gilt je Link über alle Formate.** PDF und EPUB zu laden, verbraucht zwei.
6. **Ist das Limit erreicht,** werden neue Abrufe mit 410 abgewiesen. Ein Anschluss mit
   laufendem Zeitfenster kann seinen Download weiter fortsetzen.
7. **Ablauf, Deaktivierung und Widerruf wirken sofort,** auch auf laufende Zeitfenster.

Gleichzeitige Anfragen werden in einer Datenbanktransaktion entschieden. Das Limit kann
dadurch auch bei vielen parallelen Anfragen nicht überschritten werden.

**Was als derselbe Anschluss gilt.** Die Adresse des Clients, gekürzt auf das Netz (/24 bei
IPv4, /56 bei IPv6). So zählen übliche Adresswechsel während eines Downloads nicht doppelt.
Der Browser-Typ (User-Agent) fließt bewusst nicht ein: Apps reichen Downloads häufig an
einen Download-Manager mit anderer Kennung weiter. Gespeichert wird von der Adresse nur ein
nicht umkehrbarer Prüfwert, der nach Ablauf des Zeitfensters gelöscht wird und nicht ins
Backup gelangt.

**Folgen dieser Zählweise.**

- Abgebrochene Downloads zählen, lassen sich aber im Zeitfenster kostenlos wiederholen.
- Zwei Personen am selben Anschluss, die dieselbe Datei innerhalb des Zeitfensters laden,
  zählen als ein Download.
- Wechselt ein Käufer mitten im Download das Netz (WLAN zu Mobilfunk), zählt die
  Fortsetzung als neuer Download. Bei knappen Limits ist ein kleiner Puffer sinnvoll.
- Dauert eine einzelne Übertragung länger als das Zeitfenster und bricht dann ab, zählt
  die Fortsetzung neu.
- Nach einer Änderung von `SECRET_KEY` beginnen laufende Zeitfenster neu.

## Ausgaben

Jeder Link ist fest an eine Ausgabe gebunden. Beim Veröffentlichen einer weiteren Ausgabe
muss ausdrücklich gewählt werden, ob bestehende Links umgestellt werden; es gibt keinen
Standardwert. Ein Link wird nie auf eine Ausgabe umgestellt, die keines seiner freigegebenen
Formate enthält. Veröffentlichte Ausgaben und ihre Dateien sind unveränderlich.

**Bestellungen aus Whop.** Für den E-Mail-Versand muss die App den Code eines Links noch
kennen, nachdem er erzeugt wurde. Er wird deshalb mit dem `SECRET_KEY` verschlüsselt bei der
Bestellung abgelegt und **nach dem erfolgreichen Versand gelöscht**. Nur bei Bestellungen,
deren E-Mail noch aussteht oder fehlgeschlagen ist, liegt er verschlüsselt in der Datenbank
(und damit im Backup). E-Mail-Adresse und Name des Käufers werden zur Bestellung
gespeichert; sie erscheinen nicht im Log und nicht in der Bezeichnung des Links.

## Whop-Webhook

- Jede Meldung muss nach „Standard Webhooks“ mit HMAC-SHA256 signiert sein
  (`webhook-id`, `webhook-timestamp`, `webhook-signature`). Geprüft wird über die
  unveränderten Rohdaten mit konstantem Zeitvergleich.
- Meldungen, die älter oder neuer als fünf Minuten sind, werden abgelehnt (Schutz vor
  Wiedereinspielen).
- Bereits verarbeitete `webhook-id`s und Zahlungs-IDs werden erkannt: Jede Zahlung erzeugt
  höchstens einen Link und eine E-Mail.
- Ohne `WHOP_WEBHOOK_SECRET` nimmt der Endpunkt nichts an.
- E-Mails gehen nur an die Adresse aus der signierten Zahlung; Betreff und Text stammen aus
  den Vorlagen der Verwaltung. Inhalte werden für die HTML-Fassung maskiert.

## API

- Anmeldung ausschließlich über `Authorization: Bearer`. Schlüssel in der URL werden
  abgewiesen, auch zusätzlich zu einem gültigen Header.
- Vier Berechtigungen; jeder Endpunkt prüft die seine.
- Rate Limit je Schlüssel, Sperre nach wiederholt falschen Schlüsseln je Anschluss.
- Die Sitzung der Verwaltung gilt nicht für die API; die API setzt keine Cookies.
- Interaktive Dokumentation und Spezifikation sind nur angemeldet erreichbar. Die
  Dokumentation lädt nichts von fremden Servern.

## Lieferkette

- Abhängigkeiten sind mit Prüfsummen gepinnt; Dependabot schlägt Updates wöchentlich vor.
- Die CI prüft vor jeder Veröffentlichung die Python-Abhängigkeiten (pip-audit) und das
  fertige Image (Trivy, kritische und hohe Schwachstellen mit verfügbarem Fix). Schlägt
  die Prüfung fehl, wird kein Image veröffentlicht.
- Der Prüf-Job hat nur Leserechte und keine Secrets; die Trivy-Action ist auf einen Commit
  gepinnt (nach dem Lieferketten-Angriff auf Trivy im März 2026 wurden Versions-Tags
  umgebogen).

## Container

- Läuft als Benutzer 10001 ohne Root-Rechte und ohne Linux-Capabilities.
- Mit `docker-compose.yaml` zusätzlich schreibgeschütztes Dateisystem; beschreibbar sind nur
  die beiden Volumes und ein kleines `/tmp`.
- Speicher-, CPU- und Prozesslimits.
- Abhängigkeiten sind auf Versionen mit Prüfsummen festgelegt.

## Bekannte Grenzen

- **Rate Limits liegen im Arbeitsspeicher.** Nach einem Neustart beginnen sie von vorn. Die
  App ist für genau eine Instanz ausgelegt.
- **Vertrauen in den Proxy.** Die Adresse des Clients stammt aus `X-Forwarded-For`, wenn
  die Anfrage aus einem privaten Netz kommt. Der Container darf deshalb nur über den
  Reverse Proxy erreichbar sein, nicht über einen direkt veröffentlichten Port.
- **Langsame Uploads.** Die App selbst begrenzt nicht, wie lange ein angemeldeter Client
  für einen Upload braucht. Das übernimmt der Reverse Proxy.
- **Wiederherstellung dreht die Zeit zurück.** Nach dem Zurückspielen eines Backups sind
  danach widerrufene Links und API-Schlüssel wieder aktiv.
- **Rückerstattungen.** Eine Erstattung in Whop widerruft den Link nicht automatisch.
- **PDF-Prüfung.** Geprüft werden Kennung und Endmarke, nicht der innere Aufbau. Die App
  wertet PDFs nicht aus und liefert sie nur als Download.

## Geprüft durch Tests

Die automatischen Tests (`pytest`) decken die Punkte dieses Dokuments ab, darunter:
Anmeldung und CSRF, Rate Limits auch unter gleichzeitigen Anfragen, Upload-Prüfung mit
präparierten Dateien, Zählweise mit HEAD, Range, parallelen Anfragen und Wiederaufnahme,
Versionierung, Berechtigungen aller API-Endpunkte, Idempotenz, Protokolle ohne Codes und
Schlüssel, Backup und Wiederherstellung. `scripts/e2e.sh` prüft denselben Ablauf mit curl
gegen einen laufenden Server.
