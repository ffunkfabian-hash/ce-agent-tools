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
After structural validation, device and arm contracts also resolve named object/material links and reject parent cycles.
Exit code: 0 valid, 1 findings, 2 schema or file problem.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# plugin: plugins/ce-shared/contracts next to tools/; embedded copy: <tools_dir>/contracts (ce_sync copies the schemas)
CONTRACTS = next((p for p in (HERE.parent / "contracts", HERE / "contracts") if p.is_dir()), HERE.parent / "contracts")
TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool, "null": type(None)}


def read_json(path: Path, require_object: bool = True):
    """Reject ambiguous JSON and non-finite numbers before any schema is selected."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"doppelter JSON-Schlüssel: {key}")
            result[key] = value
        return result

    def number(token):
        value = float(token)
        if not math.isfinite(value):
            raise ValueError(f"nichtendliche JSON-Zahl: {token}")
        return value

    def constant(token):
        raise ValueError(f"ungültige JSON-Zahl: {token}")

    document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                          parse_float=number, parse_constant=constant)
    if require_object and not isinstance(document, dict):
        raise ValueError("Vertrag/Schema muss ein JSON-Objekt sein")
    return document


def load_schema(name: str) -> dict:
    path = CONTRACTS / (name if name.endswith(".json") else name + ".json")
    if not path.is_file():
        raise FileNotFoundError(f"kein Schema {name} unter {CONTRACTS}")
    return read_json(path)


def schema_for(document: dict, path: Path) -> str:
    """unity_contract*.json with format N -> unity_contract.vN (falls back to the highest schema <= N)."""
    stem = re.sub(r"_(fps|robot|fps_clips|game)$", "", path.stem)
    fmt = document.get("format")
    candidates = sorted(CONTRACTS.glob(f"{stem}.v*.json"), key=lambda p: int(p.stem.rsplit(".v", 1)[1]))
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
        return isinstance(value, (int, float)) and not isinstance(value, bool) and (not isinstance(value, float) or math.isfinite(value))
    if kind == "boolean":
        return isinstance(value, bool)
    return isinstance(value, TYPES[kind])


def validate(value, schema: dict, root: dict, where: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, float) and not math.isfinite(value):
        return [f"{where}: nichtendliche Zahl"]
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
    def matches(expected):
        return value == expected and (not isinstance(value, bool) and not isinstance(expected, bool) or type(value) is type(expected))

    if "const" in schema and not matches(schema["const"]):
        errors.append(f"{where}: {value!r}, erwartet {schema['const']!r}")
    if "enum" in schema and not any(matches(expected) for expected in schema["enum"]):
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
        if extra is False:
            for key in value.keys() - schema.get("properties", {}).keys():
                errors.append(f"{where}.{key}: additional property is not allowed")
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


def arm_references(document: dict) -> list[str]:
    """Check named links only after the structural arm schema has succeeded."""
    errors = []
    names = [node['name'] for node in document['nodes']]
    known = set(names)
    if len(known) != len(names):
        errors.append('$.nodes: doppelte Knotennamen')
    parents = {node['name']: node['parent'] for node in document['nodes']}
    for name in names:
        current, seen = name, set()
        while current in parents:
            if current in seen:
                errors.append(f'$.nodes.{name}: zyklische Elternkette')
                break
            seen.add(current)
            current = parents[current]
    links = [('$.root', document['root'])]
    for node in document['nodes']:
        if node['parent'] is not None:
            links.append(('$.nodes.' + node['name'] + '.parent', node['parent']))
    for side in ('R', 'L'):
        links.extend(('$.chains.' + side, name) for name in document['chains'][side])
        hand = document['fingers'][side]
        for finger in hand['fingers']:
            links.extend(('$.fingers.' + side, name) for name in finger)
        links.extend(('$.fingers.' + side + '.thumb', name) for name in hand['thumb'])
        links.append(('$.fingers.' + side + '.trigger_finger', hand['trigger_finger']))
    for renderer in document['renderers']:
        links.append(('$.renderers.' + renderer['name'] + '.node', renderer['node']))
        for material in renderer['materials']:
            if material not in document['materials']:
                errors.append(f"$.renderers.{renderer['name']}: unbekanntes Material {material}")
    for where, name in links:
        if name not in known:
            errors.append(f'{where}: unbekannter Knoten {name}')
    return errors


def graph_references(parents: dict, locations: dict) -> list[str]:
    """Resolve a named hierarchy and detect cycles without recursive traversal."""
    errors = []
    for name, parent in parents.items():
        if parent is not None and parent not in parents:
            errors.append(f'{locations[name]}.parent: unbekannter Knoten {parent}')
    completed = set()
    for name in parents:
        current, chain = name, set()
        while current in parents and current not in completed:
            if current in chain:
                errors.append(f'{locations[name]}.parent: zyklische Elternkette')
                break
            chain.add(current)
            current = parents[current]
        completed.update(chain)
    return errors


def device_references(document: dict) -> list[str]:
    """Resolve the v4/v5 device hierarchy without mixing object and material names.

    Toolgen exports empty objects in nodes, Socket_* objects in anchors and mesh
    objects in renderers. Root metadata may also describe an entry in nodes.
    Older contracts can describe a renderer in nodes as well, or omit its parent.
    """
    errors = []
    root = document['root']['name']
    parents = {root: None}
    locations = {root: '$.root'}
    declared = {}
    for collection in ('nodes', 'anchors', 'renderers'):
        names = set()
        for index, entry in enumerate(document.get(collection, [])):
            name = entry['name']
            where = f'$.{collection}[{index}]'
            if (name in names or (collection == 'anchors' and name in declared)
                    or (collection == 'renderers' and declared.get(name) == 'anchors')):
                errors.append(f'{where}.name: doppelter Objektname {name}')
            names.add(name)
            if 'parent' in entry:
                parent = entry['parent']
                if parent is not None and not isinstance(parent, str):
                    errors.append(f'{where}.parent: erwartet Knotenname oder null')
                    continue
                if name in declared and parents.get(name) != parent:
                    errors.append(f'{where}.parent: widersprüchliche Eltern für {name}')
                parents[name] = parent
            else:
                parents.setdefault(name, None)
            locations[name] = where
            declared[name] = collection
    # Contract 5 can carry a second device root under the left hand. Those
    # objects are declared only in assembly, while nodes describes the right
    # device in isolation (its root.parent is null instead of Prop_R).
    if document['format'] == 5 and 'assembly' in document:
        assembly, assembly_locations = {}, {}
        entries = document['assembly']
        if not isinstance(entries, list):
            errors.append('$.assembly: erwartet Knotenliste')
            entries = []
        for index, entry in enumerate(entries):
            where = f'$.assembly[{index}]'
            if (not isinstance(entry, dict) or not isinstance(entry.get('name'), str) or not entry['name']
                    or 'parent' not in entry or (entry['parent'] is not None and not isinstance(entry['parent'], str))):
                errors.append(f'{where}: erwartet name und parent (Knotenname oder null)')
                continue
            name, parent = entry['name'], entry['parent']
            if name in assembly:
                errors.append(f'{where}.name: doppelter Objektname {name}')
            assembly[name], assembly_locations[name] = parent, where
            if name in parents and name != root and parents[name] != parent:
                errors.append(f'{where}.parent: widersprüchliche Eltern für {name}')
        errors += graph_references(assembly, assembly_locations)
        for name, parent in assembly.items():
            if name not in parents:
                parents[name], locations[name] = parent, assembly_locations[name]
    errors += graph_references(parents, locations)
    for index, renderer in enumerate(document['renderers']):
        for material in renderer['materials']:
            if material not in document['materials']:
                errors.append(f'$.renderers[{index}].materials: unbekanntes Material {material}')
    for index, collider in enumerate(document['colliders']):
        part = collider.get('part')
        if part is not None and (not isinstance(part, str) or part not in parents):
            errors.append(f'$.colliders[{index}].part: unbekannter Knoten {part}')
    hold = document.get('hold', {})
    for field in ('hand_socket', 'support_socket', 'muzzle_socket', 'sight_socket', 'eye_socket', 'reticle_socket'):
        target = hold.get(field)
        if target is not None and (not isinstance(target, str) or target not in parents):
            errors.append(f'$.hold.{field}: unbekannter Knoten {target}')
    return errors


def check_file(path: Path, schema_name: str | None) -> tuple[str, list[str]]:
    document = read_json(path)
    name = schema_name or schema_for(document, path)
    schema = load_schema(name)
    errors = validate(document, schema, schema)
    if not errors and name.removesuffix('.json') == 'unity_contract_arms.v5':
        errors += arm_references(document)
    if not errors and name.removesuffix('.json') == 'unity_contract.v4':
        errors += device_references(document)
    return name, errors


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
