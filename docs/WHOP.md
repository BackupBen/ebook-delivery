# Automatische Auslieferung für Whop

Nach einer Zahlung in Whop schickt die App dem Käufer selbst eine E-Mail mit seinem
persönlichen Downloadlink. Ein zusätzlicher Dienst (z. B. n8n) ist nicht nötig.

## Ablauf

1. Whop meldet eine erfolgreiche Zahlung (`payment.succeeded`) an
   `https://<PUBLIC_BASE_URL>/webhooks/whop`.
2. Die App prüft die Signatur. Unsignierte, veränderte oder mehr als fünf Minuten alte
   Meldungen werden abgelehnt.
3. Die App sucht das Buch, dessen **Whop-Produkt-ID** zur Zahlung passt, und erzeugt einen
   dauerhaften Downloadlink (PDF und EPUB, soweit vorhanden, ohne Downloadlimit).
4. Ein Hintergrund-Thread versendet die E-Mail über Brevo, in der **Sprache des Buchs**.
   Text ist die Versandnachricht aus den Einstellungen, Betreff ebenfalls dort.
5. Schlägt der Versand fehl, versucht die App es erneut: nach 1, 5, 15 und 60 Minuten,
   dann nach 3, 6 und 12 Stunden. Danach steht die Bestellung als „Versand fehlgeschlagen“ in
   der Verwaltung.

Jede Zahlung erzeugt genau eine Bestellung und einen Link, auch wenn Whop dieselbe Meldung
mehrfach zustellt.

## Einrichtung

### 1. Bücher zuordnen

Für jedes Produkt in Whop ein Buch anlegen (oder ein vorhandenes bearbeiten):

- **Whop-Produkt-ID** eintragen (`prod_…`, in Whop in der URL des Produkts).
- **Sprache** wählen. Sie bestimmt Downloadseite und E-Mail.
- Dateien hochladen und die Ausgabe **veröffentlichen**.

### 2. E-Mail-Versand (Brevo)

In Brevo unter *SMTP & API → API Keys* einen API-Schlüssel erstellen. Die Absender-Domain
muss in Brevo bestätigt sein. Dann in Coolify als Umgebungsvariablen eintragen:

| Variable | Beispiel | Pflicht |
|---|---|---|
| `BREVO_API_KEY` | `xkeysib-…` | ja |
| `MAIL_FROM_EMAIL` | `noreply@ebooks.example.com` | ja |
| `MAIL_FROM_NAME` | `Mein Verlag` | nein |
| `MAIL_REPLY_TO` | `hilfe@example.com` | nein, empfohlen bei einer noreply-Adresse |

Nach dem Neustart unter *Einstellungen → E-Mail-Versand* eine **Test-E-Mail** senden.

### 3. Webhook in Whop

In Whop unter *Developer → Webhooks* einen Webhook anlegen:

- URL: `https://<PUBLIC_BASE_URL>/webhooks/whop` (steht zum Kopieren unter *Bestellungen*)
- Ereignis: `payment.succeeded`

Whop zeigt danach ein Geheimnis `ws_…`. Es unverändert als `WHOP_WEBHOOK_SECRET` eintragen
und neu deployen. Bis dahin lehnt die App Meldungen mit 503 ab; Whop stellt sie bis zu drei
Tage lang erneut zu.

### 4. Umstellen von n8n

Erst wenn eine Testbestellung über die App angekommen ist, den alten Webhook (n8n) in Whop
löschen bzw. den n8n-Workflow deaktivieren. Laufen beide, erhält der Käufer zwei E-Mails.

## Verwaltung

Unter **Bestellungen** stehen alle Zahlungen mit Status:

| Status | Bedeutung | Aktion |
|---|---|---|
| Wird versendet | Link erzeugt, E-Mail in der Warteschlange | – |
| Versendet | E-Mail von Brevo angenommen | „Neuen Link senden“ widerruft den alten Link und verschickt einen neuen |
| Versand fehlgeschlagen | Alle Versuche erfolglos oder dauerhafter Fehler (z. B. ungültige Adresse) | Adresse korrigieren, „Jetzt erneut versuchen“ |
| Kein passendes Buch | Keine (aktive, veröffentlichte) Zuordnung zur Produkt-ID | Buch anlegen, „Erneut verarbeiten“ |
| Keine E-Mail-Adresse | Die Zahlung enthielt keine Adresse | Adresse eintragen |

Rückerstattungen widerrufen den Link nicht automatisch. Bei Bedarf den Link der Bestellung
in der Verwaltung widerrufen.

## Datenschutz

Gespeichert werden E-Mail-Adresse, Name, Zahlungs- und Produkt-ID, damit Bestellungen
nachvollziehbar sind und erneut versendet werden können. Der Link-Code wird nur
verschlüsselt und nur bis zum erfolgreichen Versand aufbewahrt. Der Brevo-Schlüssel und das
Whop-Geheimnis stehen nur in der Umgebung, nie in der Datenbank oder im Log.
