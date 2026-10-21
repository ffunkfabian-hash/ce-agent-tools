#!/usr/bin/env python3
"""ce_contract.py validate <file.json> ... [--schema NAME] | list - contracts between the repos as JSON Schema.

0.5.0: the shape of a contract (what the producer writes and the consumer reads) lives once, in plugins/ce-shared/contracts/
<name>.v<format>.json, instead of in prose ("Mindestversion des Abnehmers ..."). A generator validates its export before it
counts as done, the consumer validates on import. Additive only: a new format adds keys, never removes or retypes one.

    python3 ce_contract.py validate output/pistol/unity_contract.json              # schema from "format" and the file name
    python3 ce_contract.py validate --schema unity_contract.v4 a.json b.json
    python3 ce_contract.py list

A small JSON-Schema subset is implemented here (no dependency): type, enum, const, required, properties, additionalProperties,
items, minItems, maxItems, minLength, minProperties, minimum, maximum, exclusiveMinimum, pattern, $ref (#/$defs/...). Unknown
keywords are ignored, so the schemas stay valid for full validators (jsonschema, Unity's) too.
Exit code: 0 valid, 1 findings, 2 schema or file problem.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# plugin: plugins/ce-shared/contracts next to tools/; embedded copy: <tools_dir>/contracts (ce_sync copies the schemas)
CONTRACTS = next((p for p in (HERE.parent / "contracts", HERE / "contracts") if p.is_dir()), HERE.parent / "contracts")
TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool, "null": type(None)}


def load_schema(name: str) -> dict:
    path = CONTRACTS / (name if name.endswith(".json") else name + ".json")
    if not path.is_file():
        raise FileNotFoundError(f"kein Schema {name} unter {CONTRACTS}")
    return json.loads(path.read_text(encoding="utf-8"))


def schema_for(document: dict, path: Path) -> str:
    """unity_contract*.json with format N -> unity_contract.vN (falls back to the highest schema <= N)."""
    stem = re.sub(r"_(fps|robot|fps_clips|game)$", "", path.stem)
    fmt = document.get("format")
    candidates = sorted(CONTRACTS.glob(f"{stem}.v*.json"))
    if not candidates:
        raise FileNotFoundError(f"kein Schema für {stem} unter {CONTRACTS}")
    if isinstance(fmt, int):
        usable = [c for c in candidates if int(c.stem.rsplit(".v", 1)[1]) <= fmt]
        if usable:
            return usable[-1].stem
    return candidates[-1].stem


def typed(value, kind) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    return isinstance(value, TYPES[kind])


def validate(value, schema: dict, root: dict, where: str = "$") -> list[str]:
    errors: list[str] = []
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            target = target[part]
        return validate(value, target, root, where)
    kinds = schema.get("type")
    if kinds:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not any(typed(value, k) for k in kinds):
            return [f"{where}: Typ {type(value).__name__}, erwartet {'/'.join(kinds)}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: {value!r}, erwartet {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} nicht in {schema['enum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{where}: leer")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{where}: {value!r} passt nicht zu /{schema['pattern']}/")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: {value} < {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{where}: {value} > {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{where}: {value} <= {schema['exclusiveMinimum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{where}: Pflichtfeld {key!r} fehlt")
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            errors.append(f"{where}: weniger als {schema['minProperties']} Einträge")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errors += validate(value[key], sub, root, f"{where}.{key}")
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            for key, item in value.items():
                if key not in schema.get("properties", {}):
                    errors += validate(item, extra, root, f"{where}.{key}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{where}: weniger als {schema['minItems']} Elemente")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{where}: mehr als {schema['maxItems']} Elemente")
        if "items" in schema:
            for index, item in enumerate(value):
                errors += validate(item, schema["items"], root, f"{where}[{index}]")
    return errors


def check_file(path: Path, schema_name: str | None) -> tuple[str, list[str]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    name = schema_name or schema_for(document, path)
    schema = load_schema(name)
    return name, validate(document, schema, schema)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    val = sub.add_parser("validate")
    val.add_argument("files", nargs="+", type=Path)
    val.add_argument("--schema", help="schema name (default: from the file name and its 'format')")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    if args.command == "list":
        for path in sorted(CONTRACTS.glob("*.json")):
            title = json.loads(path.read_text(encoding="utf-8")).get("title", "")
            print(f"{path.stem:28} {title}")
        return 0
    status = 0
    for path in args.files:
        try:
            name, errors = check_file(path, args.schema)
        except (OSError, ValueError, KeyError) as error:
            print(f"{path}: {error}")
            status = max(status, 2)
            continue
        for error in errors:
            print(f"{path}: {error}")
        print(f"{path}: {'gültig' if not errors else str(len(errors)) + ' Befunde'} ({name})")
        status = max(status, 1 if errors else 0)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
