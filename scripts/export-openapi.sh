#!/bin/sh
# Schreibt die OpenAPI-Spezifikation nach docs/openapi.json.
set -eu
cd "$(dirname "$0")/.."
python3 -m ebookapp.cli openapi > docs/openapi.json
echo "docs/openapi.json aktualisiert."
