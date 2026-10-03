"""Findet alle zur Übersetzung markierten Texte (``_()``/``N_()``) im Quelltext.

Aufruf zum Prüfen von Hand:  python -m tests.i18n_extract [fragment.json ...]
gibt die Texte aus, für die es (auch in den angegebenen Fragmenten) keine Übersetzung gibt.
"""

from __future__ import annotations

import ast
import json
import sys
from collections.abc import Iterator
from pathlib import Path

from jinja2 import Environment, nodes

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "ebookapp"
MARKERS = {"_", "N_", "gettext", "mark"}


def python_strings() -> Iterator[tuple[str, str]]:
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            first = node.args[0]
            if name in MARKERS and isinstance(first, ast.Constant) and isinstance(first.value, str):
                yield first.value, f"{path.relative_to(ROOT)}:{node.lineno}"


def template_strings() -> Iterator[tuple[str, str]]:
    env = Environment(autoescape=True)
    for path in sorted((PACKAGE / "templates").rglob("*.html")):
        tree = env.parse(path.read_text(encoding="utf-8"))
        for call in tree.find_all(nodes.Call):
            if (
                isinstance(call.node, nodes.Name)
                and call.node.name in ("_", "N_")
                and call.args
                and isinstance(call.args[0], nodes.Const)
                and isinstance(call.args[0].value, str)
            ):
                yield call.args[0].value, f"{path.relative_to(ROOT)}:{call.lineno}"


def openapi_strings() -> Iterator[tuple[str, str]]:
    from ebookapp.i18n import OPENAPI_KEYS, untranslated_openapi

    def walk(value: object, where: str) -> Iterator[tuple[str, str]]:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in OPENAPI_KEYS and isinstance(item, str):
                    yield item, f"openapi:{where}/{key}"
                else:
                    yield from walk(item, f"{where}/{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                yield from walk(item, f"{where}/{index}")

    spec = untranslated_openapi()
    yield from walk(spec, "")
    yield spec["info"]["title"], "openapi:/info/title"
    for tag in spec.get("tags", []):
        yield tag["name"], "openapi:/tags/name"


def all_strings() -> dict[str, str]:
    found: dict[str, str] = {}
    for text, where in [*python_strings(), *template_strings(), *openapi_strings()]:
        found.setdefault(text, where)
    return found


def missing(extra: dict[str, str] | None = None) -> dict[str, str]:
    from ebookapp.i18n import OPENAPI_UNTRANSLATED
    from ebookapp.translations_en import EN

    known = {**EN, **(extra or {})}
    return {
        text: where
        for text, where in all_strings().items()
        if text not in known and text not in OPENAPI_UNTRANSLATED
    }


if __name__ == "__main__":
    fragments: dict[str, str] = {}
    for name in sys.argv[1:]:
        fragments.update(json.loads(Path(name).read_text(encoding="utf-8")))
    for text, where in sorted(missing(fragments).items(), key=lambda item: item[1]):
        print(f"{where}\t{json.dumps(text, ensure_ascii=False)}")
