#!/usr/bin/env bash
# End-to-End-Test gegen eine laufende Instanz, nur mit curl.
#
# Prüft: Anmeldung, Upload, Veröffentlichung, Linkerstellung, Käuferdownload (auch HEAD,
# parallele Range-Anfragen und Wiederaufnahme), Downloadlimit, API-Ablauf und Widerruf.
# Legt ein Testbuch an und löscht es am Ende wieder.
#
# Aufruf:  BASE_URL=http://127.0.0.1:8000 ADMIN_PASSWORD=... scripts/e2e.sh
#
# Gedacht für eine lokale Instanz oder einen Testcontainer, der direkt (ohne Reverse Proxy)
# erreichbar ist: Verschiedene Käufer werden über den Header X-Forwarded-For simuliert, und
# den wertet die App nur aus, wenn die Anfrage von einer vertrauenswürdigen Adresse kommt.
set -euo pipefail

BASE_URL="${BASE_URL:?BASE_URL fehlt}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:?ADMIN_PASSWORD fehlt}"
KEEP="${KEEP:-0}"   # KEEP=1 lässt Buch und Link bestehen (für den Redeploy-Test)

DEV_A="X-Forwarded-For: 198.51.100.10"   # Käufer an Anschluss A
DEV_B="X-Forwarded-For: 203.0.113.20"    # Käufer an Anschluss B
export DEV_A DEV_B
WORK="$(mktemp -d)"
JAR="$WORK/cookies.txt"
trap 'rm -rf "$WORK"' EXIT
pass=0

ok() { pass=$((pass + 1)); printf '  ok  %s\n' "$1"; }
fail() { printf 'FEHLER  %s\n' "$1" >&2; exit 1; }
expect() { [ "$2" = "$3" ] && ok "$1" || fail "$1: erwartet $3, erhalten $2"; }
field() { grep -o "name=\"$1\" value=\"[^\"]*\"" | head -1 | sed 's/.*value="\([^"]*\)"/\1/'; }
status() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
admin() { curl -sS -b "$JAR" -c "$JAR" -H "Origin: $BASE_URL" "$@"; }
sha() { sha256sum "$1" | cut -d' ' -f1; }

echo "== Testdateien erzeugen"
python3 - "$WORK" <<'PY'
import os, sys, zipfile
work = sys.argv[1]
with open(f"{work}/buch.pdf", "wb") as f:
    f.write(b"%PDF-1.7\n" + os.urandom(3 * 1024 * 1024) + b"\n%%EOF\n")
with open(f"{work}/buch2.pdf", "wb") as f:
    f.write(b"%PDF-1.7\n% zweite Ausgabe\n" + os.urandom(200_000) + b"\n%%EOF\n")
with zipfile.ZipFile(f"{work}/buch.epub", "w") as z:
    z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
    z.writestr("META-INF/container.xml",
               '<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
    z.writestr("content.opf", "<package/>")
    z.writestr("kapitel.xhtml", "<html><body>" + "Text " * 5000 + "</body></html>",
               compress_type=zipfile.ZIP_DEFLATED)
open(f"{work}/fake.pdf", "wb").write(b"<html>kein pdf</html>" * 100)
PY

echo "== Ohne Anmeldung"
expect "Health-Check" "$(status "$BASE_URL/healthz")" 200
expect "Verwaltung leitet zur Anmeldung" "$(status "$BASE_URL/admin/books")" 303
expect "API ohne Schlüssel" "$(status "$BASE_URL/api/v1/books")" 401
expect "Unbekannter Link" "$(status "$BASE_URL/d/$(printf 'a%.0s' {1..43})")" 404

echo "== Anmeldung"
token="$(admin "$BASE_URL/admin/login" | field login_token)"
expect "Falsches Passwort" "$(admin -o /dev/null -w '%{http_code}' "$BASE_URL/admin/login" \
  --data-urlencode "username=$ADMIN_USERNAME" --data-urlencode "password=falsch-falsch-falsch" \
  --data-urlencode "login_token=$token")" 401
token="$(admin "$BASE_URL/admin/login" | field login_token)"
expect "Anmeldung" "$(admin -o /dev/null -w '%{http_code}' "$BASE_URL/admin/login" \
  --data-urlencode "username=$ADMIN_USERNAME" --data-urlencode "password=$ADMIN_PASSWORD" \
  --data-urlencode "login_token=$token")" 303
csrf="$(admin "$BASE_URL/admin/books" | field csrf_token)"
[ -n "$csrf" ] && ok "CSRF-Token erhalten" || fail "kein CSRF-Token"
expect "POST ohne CSRF-Token" "$(admin -o /dev/null -w '%{http_code}' "$BASE_URL/admin/books" \
  --data-urlencode "title=ohne token")" 403

echo "== Buch anlegen, Dateien hochladen, veröffentlichen"
location="$(admin -o /dev/null -w '%{redirect_url}' "$BASE_URL/admin/books" \
  --data-urlencode "csrf_token=$csrf" --data-urlencode "title=E2E-Testbuch $(date +%s)")"
book_id="${location##*/}"
[ "${book_id#bk_}" != "$book_id" ] && ok "Buch angelegt ($book_id)" || fail "Buch nicht angelegt"
edition_id="$(admin "$BASE_URL/admin/books/$book_id" | grep -o 'editions/ed_[0-9a-f]*/files"' | head -1 | cut -d/ -f2)"
expect "Ungültiges PDF wird abgewiesen" "$(admin -o /dev/null -w '%{http_code}' \
  "$BASE_URL/admin/books/$book_id/editions/$edition_id/files" \
  -F "csrf_token=$csrf" -F "pdf=@$WORK/fake.pdf;type=application/pdf")" 422
expect "Upload PDF und EPUB" "$(admin -o /dev/null -w '%{http_code}' \
  "$BASE_URL/admin/books/$book_id/editions/$edition_id/files" \
  -F "csrf_token=$csrf" -F "pdf=@$WORK/buch.pdf;type=application/pdf" \
  -F "epub=@$WORK/buch.epub;type=application/epub+zip")" 303
expect "Veröffentlichen" "$(admin -o /dev/null -w '%{http_code}' \
  "$BASE_URL/admin/books/$book_id/editions/$edition_id/publish" \
  --data-urlencode "csrf_token=$csrf")" 303

echo "== Käuferlink mit Limit 2 erstellen"
form_token="$(admin "$BASE_URL/admin/links/new?book_id=$book_id" | field form_token)"
created="$(admin "$BASE_URL/admin/links" --data-urlencode "csrf_token=$csrf" \
  --data-urlencode "form_token=$form_token" --data-urlencode "book_id=$book_id" \
  --data-urlencode "formats=pdf" --data-urlencode "formats=epub" \
  --data-urlencode "label=E2E" --data-urlencode "max_downloads=2")"
link_url="$(printf '%s' "$created" | grep -o 'id="link-url"[^>]*value="[^"]*"' | sed 's/.*value="\([^"]*\)"/\1/')"
link_id="$(printf '%s' "$created" | grep -o 'lnk_[0-9a-f]*' | head -1)"
[ -n "$link_url" ] && ok "Link erstellt ($link_id)" || fail "kein Link in der Antwort"
# Bei abweichender PUBLIC_BASE_URL den Pfad gegen BASE_URL verwenden.
link="$BASE_URL/d/${link_url##*/d/}"
count() { admin "$BASE_URL/admin/links/$link_id" | grep -o '<dt>Downloads</dt><dd>[0-9]*' | grep -o '[0-9]*$'; }

echo "== Käuferseite (ohne Cookies)"
page="$(curl -sS -D "$WORK/headers.txt" "$link")"
printf '%s' "$page" | grep -q "E2E-Testbuch" && ok "Seite zeigt den Titel" || fail "Titel fehlt"
grep -qi '^referrer-policy: no-referrer' "$WORK/headers.txt" && ok "Referrer-Policy: no-referrer" || fail "Referrer-Policy"
grep -qi '^x-robots-tag: noindex' "$WORK/headers.txt" && ok "X-Robots-Tag: noindex" || fail "X-Robots-Tag"
printf '%s' "$page" | grep -qiE '<script|https?://' && fail "Seite enthält Skripte oder fremde URLs" || ok "keine Skripte, keine fremden URLs"

echo "== HEAD zählt nicht"
for _ in 1 2 3; do expect "HEAD" "$(status -I -H "$DEV_A" "$link/pdf")" 200; done
expect "Zähler nach HEAD" "$(count)" 0

echo "== Parallele Range-Anfragen eines Käufers zählen einmal"
size="$(stat -c %s "$WORK/buch.pdf")"
chunk=$(( size / 8 + 1 ))
seq 0 7 | xargs -P 8 -I{} sh -c '
  start=$(( {} * '"$chunk"' )); end=$(( start + '"$chunk"' - 1 ))
  curl -sS -H "$DEV_A" -r "$start-$end" -o "'"$WORK"'/part{}" -w "%{http_code}\n" "'"$link"'/pdf"
' > "$WORK/codes.txt"
expect "8 Teilantworten mit 206" "$(sort -u "$WORK/codes.txt" | tr '\n' ' ')" "206 "
cat "$WORK"/part{0..7} > "$WORK/assembled.pdf"
expect "Zusammengesetzte Datei ist identisch" "$(sha "$WORK/assembled.pdf")" "$(sha "$WORK/buch.pdf")"
expect "Zähler nach parallelen Segmenten" "$(count)" 1

echo "== Abbruch und Wiederaufnahme zählen nicht erneut"
curl -sS -H "$DEV_A" -r 0-999999 -o "$WORK/resume.pdf" "$link/pdf"
curl -sS -H "$DEV_A" -C - -o "$WORK/resume.pdf" "$link/pdf"
expect "Fortgesetzter Download ist identisch" "$(sha "$WORK/resume.pdf")" "$(sha "$WORK/buch.pdf")"
curl -sS -H "$DEV_A" -o "$WORK/full.epub" "$link/epub"
expect "EPUB identisch" "$(sha "$WORK/full.epub")" "$(sha "$WORK/buch.epub")"
expect "Zähler (PDF + EPUB)" "$(count)" 2

echo "== Limit erreicht"
expect "Anderer Anschluss wird abgewiesen" "$(status -H "$DEV_B" "$link/pdf")" 410
expect "HEAD von anderem Anschluss" "$(status -I -H "$DEV_B" "$link/pdf")" 410
expect "Erster Käufer darf fortsetzen" "$(status -H "$DEV_A" -r 100-200 "$link/pdf")" 206
expect "Zähler unverändert" "$(count)" 2

echo "== API: Schlüssel, Buch, Upload, Ausgabe, Link, Widerruf"
key="$(admin "$BASE_URL/admin/settings/api-keys" --data-urlencode "csrf_token=$csrf" \
  --data-urlencode "name=e2e" --data-urlencode "scopes=books:read" \
  --data-urlencode "scopes=books:write" --data-urlencode "scopes=files:write" \
  --data-urlencode "scopes=links:manage" | grep -o 'id="api-key"[^>]*value="[^"]*"' | sed 's/.*value="\([^"]*\)"/\1/')"
[ -n "$key" ] && ok "API-Schlüssel erstellt" || fail "kein API-Schlüssel"
api() { curl -sS -H "Authorization: Bearer $key" "$@"; }
json() { python3 -c "import sys, json; print(json.load(sys.stdin)$1)"; }
expect "Schlüssel in der URL wird abgewiesen" "$(status "$BASE_URL/api/v1/books?api_key=$key")" 400

draft="$(api -X POST "$BASE_URL/api/v1/books/$book_id/editions" -H 'Content-Type: application/json' \
  -d '{"note": "zweite Ausgabe", "copy_formats": ["epub"]}' | json "['id']")"
api -o /dev/null -X POST "$BASE_URL/api/v1/books/$book_id/editions/$draft/files" \
  -F "pdf=@$WORK/buch2.pdf;type=application/pdf"
expect "Veröffentlichen ohne Wahl wird abgelehnt" "$(api -o /dev/null -w '%{http_code}' -X POST \
  "$BASE_URL/api/v1/books/$book_id/editions/$draft/publish")" 422
expect "Veröffentlichen mit keep" "$(api -o /dev/null -w '%{http_code}' -X POST \
  "$BASE_URL/api/v1/books/$book_id/editions/$draft/publish" \
  -H 'Content-Type: application/json' -d '{"existing_links": "keep"}')" 200
curl -sS -H "$DEV_A" -o "$WORK/old.pdf" "$link/pdf"
expect "Alter Link liefert weiter die alte Ausgabe" "$(sha "$WORK/old.pdf")" "$(sha "$WORK/buch.pdf")"

idem="e2e-$(date +%s%N)"
first="$(api -X POST "$BASE_URL/api/v1/links" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $idem" -d "{\"book_id\": \"$book_id\", \"label\": \"E2E API\"}")"
second="$(api -X POST "$BASE_URL/api/v1/links" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $idem" -d "{\"book_id\": \"$book_id\", \"label\": \"E2E API\"}")"
expect "Idempotente Wiederholung liefert denselben Link" \
  "$(printf '%s' "$second" | json "['id']")" "$(printf '%s' "$first" | json "['id']")"
api_link_id="$(printf '%s' "$first" | json "['id']")"
api_code="$(printf '%s' "$first" | json "['code']")"
curl -sS -o "$WORK/new.pdf" "$BASE_URL/d/$api_code/pdf"
expect "Neuer Link liefert die neue Ausgabe" "$(sha "$WORK/new.pdf")" "$(sha "$WORK/buch2.pdf")"
api "$BASE_URL/api/v1/links" | grep -q "$api_code" && fail "Liste enthält den Code" || ok "Liste enthält keinen Code"
expect "Widerruf per API" "$(api -X POST "$BASE_URL/api/v1/links/$api_link_id/revoke" | json "['state']")" revoked
expect "Widerrufener Link (Seite)" "$(status "$BASE_URL/d/$api_code")" 410
expect "Widerrufener Link (Datei)" "$(status "$BASE_URL/d/$api_code/pdf")" 410

echo "== Widerruf über die Oberfläche"
expect "Widerruf" "$(admin -o /dev/null -w '%{http_code}' "$BASE_URL/admin/links/$link_id/revoke" \
  --data-urlencode "csrf_token=$csrf" --data-urlencode "confirm=1")" 303
expect "Seite nach Widerruf" "$(status -H "$DEV_A" "$link")" 410
expect "Fortsetzung nach Widerruf" "$(status -H "$DEV_A" -r 100-200 "$link/pdf")" 410

if [ "$KEEP" = "1" ]; then
  keep_link="$(api -X POST "$BASE_URL/api/v1/links" -H 'Content-Type: application/json' \
    -d "{\"book_id\": \"$book_id\", \"label\": \"Redeploy-Test\"}")"
  printf '%s\n' "$book_id" > "${KEEP_FILE:-/tmp/e2e-keep.txt}"
  printf '%s' "$keep_link" | json "['code']" >> "${KEEP_FILE:-/tmp/e2e-keep.txt}"
  sha "$WORK/buch2.pdf" >> "${KEEP_FILE:-/tmp/e2e-keep.txt}"
  printf '%s\n' "$key" >> "${KEEP_FILE:-/tmp/e2e-keep.txt}"
  ok "Buch und Link bleiben für den Redeploy-Test bestehen"
else
  echo "== Aufräumen"
  total="$(api "$BASE_URL/api/v1/books/$book_id/deletion-preview" | json "['links_total']")"
  expect "Löschen mit falscher Bestätigung" "$(api -o /dev/null -w '%{http_code}' -X DELETE \
    "$BASE_URL/api/v1/books/$book_id?expected_link_count=99")" 409
  expect "Testbuch gelöscht" "$(api -o /dev/null -w '%{http_code}' -X DELETE \
    "$BASE_URL/api/v1/books/$book_id?expected_link_count=$total")" 200
  key_id="$(admin "$BASE_URL/admin/settings" | grep -o 'api-keys/key_[0-9a-f]*/revoke' | head -1 | cut -d/ -f2)"
  admin -o /dev/null "$BASE_URL/admin/settings/api-keys/$key_id/revoke" \
    --data-urlencode "csrf_token=$csrf" --data-urlencode "confirm=1"
  expect "Widerrufener API-Schlüssel" "$(status -H "Authorization: Bearer $key" "$BASE_URL/api/v1/books")" 401
fi

printf '\nAlle %d Prüfungen bestanden.\n' "$pass"
