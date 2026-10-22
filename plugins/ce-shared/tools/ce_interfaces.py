#!/usr/bin/env python3
"""Read-only interface audit; native validators keep ownership of their contracts.

``robot-package ROOT`` validates the CE copy set before its first copy. The audit
requires explicit repository roots and never generates assets or infers versions.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
from pathlib import Path
import runpy
import struct
import subprocess
import sys

import ce_contract

ROBOT_MASKS = ("ColorLight", "MetalWear", "Aux", "Layers", "Lines", "Material", "Material2", "Badges", "Badges2", "Badges3")
ROBOT_FILES = ("CubeEscape_Robot.fbx", "robot_config.json", *(
    f"Textures/T_Robot_{name}.png" for name in
    ("BaseColor", "Normal", "Occlusion", "Emission", "EyeAtlas", "DisplayAtlas", "DisplayFlipbook")), *(
    f"Textures/Unity/T_Robot_U_{name}.png" for name in ROBOT_MASKS))
# CE RobotClips.Used: names are requirements, not a limit on additional clips.
ROBOT_CLIPS = ("Idle Walk Run RunFast Sprint CrawlIdle Crawl Jump DoubleJump Fall Land SlopeSlide Death TerminalTyping "
               "Listen HitHeavy Respawn Celebrate IdleScratch IdleFootTap IdleYawn PushStart Push MudWalk Jog SprintFull "
               "StrafeLeft StrafeRight RunBack CrawlFast CarryIdle CarryWalk Wade Knockback LandRoll Interact PickUp "
               "ToolUse Scan Hit Electrocuted GasCough PortalEnter Crank").split()


def _number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _vector(value, size):
    return isinstance(value, list) and len(value) == size and all(_number(x) for x in value)


def _colors(owner, fields, where, required=()):
    return [f'{where}.{field}: expected finite RGB' for field in fields
            if (field in owner or field in required) and not _vector(owner.get(field), 3)]


def _relative(value):
    return (isinstance(value, str) and bool(value) and "\\" not in value and
            not Path(value).is_absolute() and all(p not in ("", ".", "..") for p in value.split("/")))


def check_robot_config(document) -> list[str]:
    """Current CE consumer invariants, not a new producer format or image approval."""
    if not isinstance(document, dict):
        return ["robot_config: expected an object"]
    errors = []
    catalogs = {}
    for key in ("skins", "slots", "finishes", "badges", "linePatterns", "zones", "materials", "layers", "specials", "forms"):
        entries = document.get(key)
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            errors.append(f"{key}: expected an object list"); catalogs[key] = []; continue
        catalogs[key] = entries
        ids = [e.get("name" if key == "skins" else "id") for e in entries]
        if any(not isinstance(i, str) or not i for i in ids) or len(set(str(i) for i in ids)) != len(ids):
            errors.append(f"{key}: IDs must be nonempty unique strings")
    if not any(s.get("name") == "Classic" for s in catalogs["skins"]):
        errors.append("skins: Classic is required by CE RobotSurface")
    animations = document.get("displayAnimations", [])
    if not isinstance(animations, list) or any(not isinstance(a, dict) for a in animations):
        errors.append("displayAnimations: expected an object list"); animations = []
    for animation in animations:
        if not isinstance(animation.get("name"), str) or not animation["name"]:
            errors.append("displayAnimations: nonempty name required")
        if any(not _number(animation.get(k)) or animation[k] <= 0 for k in ("frames", "fps")):
            errors.append("displayAnimations: positive frames/fps required")
    for skin in catalogs["skins"]:
        for key in ("armor", "mech", "metal", "accent", "screen", "eye", "visor", "fabric"):
            if not _vector(skin.get(key), 3): errors.append(f"skins.{skin.get('name')}.{key}: expected finite RGB")
    slots = {}
    for slot in catalogs["slots"]:
        variants = slot.get("variants")
        if not isinstance(variants, list) or not variants or any(not isinstance(v, str) or not v for v in variants):
            errors.append(f"slots.{slot.get('id')}: expected nonempty variants"); continue
        if len(set(variants)) != len(variants): errors.append(f"slots.{slot.get('id')}: duplicate variant")
        if isinstance(slot.get("id"), str): slots[slot["id"]] = variants
    for key in ("badges", "linePatterns", "zones", "materials", "layers"):
        for entry in catalogs[key]:
            channel = entry.get("channel")
            if type(channel) is not int or not 0 <= channel <= 3:
                errors.append(f"{key}.{entry.get('id')}: channel must be 0..3")
            if not _relative(entry.get("texture")): errors.append(f"{key}.{entry.get('id')}: invalid mask filename")
    for key in ("finishes", "materials", "layers"):
        for entry in catalogs[key]:
            # Wear/Grime layers carry only a mask; CE reads surface values only for coloured layers.
            if key == "layers" and entry.get("color") is None: continue
            for field in ("roughness", "metalOffset"):
                if not _number(entry.get(field)): errors.append(f"{key}.{entry.get('id')}.{field}: expected finite number")
    for key in ('materials', 'layers', 'specials'):
        for entry in catalogs[key]:
            if key == 'layers' and entry.get('color') is None: continue
            errors += _colors(entry, ('color', 'color2'), f"{key}.{entry.get('id')}")
    cells = document.get("atlasCells")
    for key, grid_key in (("expressions", "eyeGrid"), ("displays", "displayGrid")):
        names, grid = document.get(key), document.get(grid_key)
        if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n for n in names):
            errors.append(f"{key}: expected nonempty names"); continue
        if len(set(names)) != len(names): errors.append(f"{key}: duplicate names")
        if not isinstance(grid, list) or len(grid) != 2 or any(type(v) is not int or v <= 0 for v in grid):
            errors.append(f"{grid_key}: expected two positive integers")
        elif len(names) > grid[0] * grid[1]: errors.append(f"{key}: does not fit {grid_key}")
        kind = "eyes" if key == "expressions" else "displays"
        atlas = cells.get(kind, {}) if isinstance(cells, dict) else {}
        if not isinstance(atlas, dict): atlas = {}
        for name in names:
            cell = atlas.get(name)
            if (not _vector(cell, 4) or min(cell[:2]) <= 0 or min(cell[2:]) < 0 or
                    cell[0] + cell[2] > 1.00001 or cell[1] + cell[3] > 1.00001):
                errors.append(f"atlasCells.{kind}.{name}: invalid atlas rectangle")
    for skin in catalogs["skins"]:
        errors += _colors(skin, ('armor2',), f"skins.{skin.get('name')}")
        variants = skin.get("variants", {})
        if not isinstance(variants, dict) or any(k not in slots or v not in slots[k] for k, v in variants.items()):
            errors.append(f"skins.{skin.get('name')}: unknown slot/variant")
        for field, catalog in (("expression", "expressions"), ("display", "displays")):
            allowed = document.get(catalog, [])
            if field == "display" and isinstance(allowed, list):
                allowed = allowed + [a.get("name") for a in animations]
            if skin.get(field) not in (None, "") and (not isinstance(allowed, list) or skin[field] not in allowed):
                errors.append(f"skins.{skin.get('name')}.{field}: unknown name")
        references = [("finish", "finishes"), ("form", "forms")]
        for field, catalog in references:
            value = skin.get(field)
            if value not in (None, "", "None") and value not in [e.get("id") for e in catalogs[catalog]]:
                errors.append(f"skins.{skin.get('name')}.{field}: unknown ID")
        for field, catalog, id_key in (("badges", "badges", "id"), ("layers", "layers", "id"), ("zones2", "zones", None)):
            entries = skin.get(field, [])
            if not isinstance(entries, list): errors.append(f"skins.{skin.get('name')}.{field}: expected list"); continue
            known = [e.get("id") for e in catalogs[catalog]]
            for entry in entries:
                value = entry.get(id_key) if isinstance(entry, dict) and id_key else entry
                if value not in known: errors.append(f"skins.{skin.get('name')}.{field}: unknown ID")
                if id_key and isinstance(entry, dict):
                    errors += _colors(entry, ('color',), f"skins.{skin.get('name')}.{field}", required=('color',))
                    if field == 'layers' and not _number(entry.get('amount')):
                        errors.append(f"skins.{skin.get('name')}.layers.amount: expected finite number")
        for field, catalog, id_key in (("lines", "linePatterns", "pattern"), ("material", "materials", "id"), ("special", "specials", "id")):
            entry = skin.get(field)
            if entry is None: continue
            if not isinstance(entry, dict): errors.append(f"skins.{skin.get('name')}.{field}: expected object"); continue
            if entry.get(id_key) not in ("None",) and entry.get(id_key) not in [e.get("id") for e in catalogs[catalog]]:
                errors.append(f"skins.{skin.get('name')}.{field}: unknown ID")
            errors += _colors(entry, ('color', 'color2'), f"skins.{skin.get('name')}.{field}",
                              required=('color',) if field in ('lines', 'material') else ())
            if field == 'lines' and not _number(entry.get('strength')):
                errors.append(f"skins.{skin.get('name')}.lines.strength: expected finite number")
    textures = document.get("unityTextures")
    if not isinstance(textures, list) or any(not isinstance(t, dict) for t in textures):
        errors.append("unityTextures: expected an object list")
    else:
        files = [t.get("file") for t in textures]
        if any(not _relative(f) for f in files) or len(set(str(f) for f in files)) != len(files):
            errors.append("unityTextures: invalid or duplicate filenames")
        for name in ROBOT_MASKS:
            if f"Unity/T_Robot_U_{name}.png" not in files: errors.append(f"unityTextures: missing CE role {name}")
        if any(not isinstance(t.get("channels"), str) or not t["channels"].strip() for t in textures):
            errors.append("unityTextures: channel descriptions required")
    clips = document.get("clips")
    if not isinstance(clips, dict):
        errors.append("clips: expected an object"); return errors
    for name in ROBOT_CLIPS:
        if name not in clips: errors.append(f"clips: missing CE clip {name}")
    for name, clip in clips.items():
        if not isinstance(clip, dict): errors.append(f"clips.{name}: expected an object"); continue
        frames = clip.get("frames")
        if not _number(frames) or frames <= 0 or not _number(clip.get("fps")) or clip["fps"] <= 0:
            errors.append(f"clips.{name}: frames/fps must be positive finite numbers"); continue
        if type(clip.get("loop")) is not bool: errors.append(f"clips.{name}.loop: expected boolean")
        if not _number(clip.get("naturalSpeed")) or clip["naturalSpeed"] < 0:
            errors.append(f"clips.{name}.naturalSpeed: expected nonnegative finite speed")
        for field in ("events", "footsteps"):
            entries = clip.get(field)
            if not isinstance(entries, list): errors.append(f"clips.{name}.{field}: expected a list"); continue
            for pair in entries:
                if (not isinstance(pair, list) or len(pair) != 2 or not _number(pair[0]) or
                        not 0 <= pair[0] <= frames or not isinstance(pair[1], str) or not pair[1]):
                    errors.append(f"clips.{name}.{field}: invalid frame/name pair")
    return errors


def check_robot_package(root) -> list[str]:
    """Validate the entire CE copy set before copying; no Blender or image decoding."""
    root = Path(root)
    errors = []
    for name in ROBOT_FILES:
        path = root / name
        if not path.is_file() or path.stat().st_size == 0:
            errors.append(f"missing/empty file: {name}"); continue
        if path.suffix == ".png":
            try:
                with path.open("rb") as stream: header = stream.read(24)
                if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
                    raise ValueError("invalid PNG header")
                width, height = struct.unpack(">II", header[16:24])
                cap = 1024 if "/Unity/" in name else 4096
                if not (0 < width <= cap and 0 < height <= cap): raise ValueError(f"dimensions exceed CE cap {cap}")
            except (OSError, ValueError) as error: errors.append(f"{name}: {error}")
    try: errors += check_robot_config(ce_contract.read_json(root / "robot_config.json"))
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error: errors.append(f"robot_config.json: {error}")
    return errors


def _result(name, depth, checked=0, findings=None, notes=None):
    findings = list(findings or [])
    if checked <= 0 and not findings: findings.append("no input was checked")
    return dict(connection=name, depth=depth, checked=checked, status="failed" if findings else "passed",
                findings=findings, notes=list(notes or []))


def _worker(kind, root, options):
    """Run repository-owned code only in a disposable interpreter, never a build."""
    errors, notes, checked = [], [], 0
    if kind == "cubegen":
        sys.path.insert(0, str(root))
        from cubegen.contract import read_sidecar, check_sidecar, check_export
        folder = Path(options["package"])
        paths = sorted(folder.rglob("*.cubegen.json"))
        for path in paths:
            document, findings = read_sidecar(str(path)); errors += [f"{path}: {e}" for e in findings]
            if document is not None and not findings:
                found, opened = check_sidecar(document, str(path), options["version"])
                errors += [f"{path}: {e}" for e in found]; notes += [f"{path}: {e}" for e in opened]
            checked += 1
            if options["editor"]:
                fbx = path.with_name(path.name.removesuffix(".cubegen.json") + ".fbx")
                if not fbx.is_file() or fbx.stat().st_size == 0: errors.append(f"editor package missing/empty FBX: {fbx}")
        if options["editor"] and paths and not errors:
            for model, report in check_export(str(folder), options["version"]).items():
                errors += [f"{model}: {e}" for e in report["errors"]]
                notes += [f"{model}: runtime budget: {e}" for e in report.get("runtime", [])]
    elif kind == "toolgen":
        required = ("scripts/import_concepts.py", "scripts/tool_assets/concept_spec.py")
        missing = [name for name in required if not (root / name).is_file()]
        if missing:
            return dict(checked=0, findings=[
                "Toolgen concept validation unavailable: missing " + ", ".join(missing) +
                "; use a Toolgen checkout with concept import support"], notes=[])
        module = runpy.run_path(str(root / "scripts/import_concepts.py"), run_name="ce_native")
        folder = root / "data/concepts"
        errors += module["check"](folder)
        loader = runpy.run_path(str(root / "scripts/tool_assets/concept_spec.py"), run_name="ce_native")
        record = module["read_provenance"](folder)
        for entry in record["imported"].values():
            loader["load"](entry["file"], folder); checked += 1
    elif kind == "motiongen":
        module = runpy.run_path(str(root / "tools/check_links.py"), run_name="ce_native")
        manifest = ce_contract.read_json(root / "data/clips.json", require_object=False)
        errors += module["validate_manifest"](manifest)[0]
        if not manifest: errors.append("empty measurement manifest")
        errors += module["check_anchors"](root)
        checked = 1
    else:
        raise ValueError(f"unknown native adapter {kind}")
    return dict(checked=checked, findings=errors, notes=notes)


def _native(kind, root, options):
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--worker", kind, str(root), json.dumps(options)]
    result = subprocess.run(command, cwd=root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                            capture_output=True, text=True, timeout=60)
    if result.returncode: raise ValueError(f"{kind} validator failed: {result.stderr[-2000:] or result.stdout[-2000:]}")
    report = json.loads(result.stdout)
    if not isinstance(report, dict) or type(report.get("checked")) is not int or not isinstance(report.get("findings"), list):
        raise ValueError(f"{kind}: invalid validator result")
    return report


def audit(args):
    results = []
    def run(name, depth, check):
        try:
            data = check(); results.append(_result(name, depth, **data))
        except (OSError, ValueError, KeyError, TypeError, RecursionError, subprocess.SubprocessError) as error:
            results.append(_result(name, depth, findings=[str(error)]))
    def formgen():
        schema = ce_contract.read_json(args.formgen / "claude/docs/geometry-spec.schema.json")
        if schema.get("$id") != "cubeescape-design/geometry-spec/1":
            raise ValueError("unknown Formgen geometry schema identity/version")
        paths = sorted((args.formgen / "claude/output/sheets").glob("*.geo.json"))
        notes = []
        if args.toolgen:
            imported = sorted((args.toolgen / "data/concepts").glob("*.geo.json"))
            if not paths and imported: notes.append("producer geo output absent; checking existing consumer snapshots")
            paths += imported
        findings = []
        for path in paths:
            findings += [f"{path}: {e}" for e in ce_contract.validate(ce_contract.read_json(path), schema, schema)]
        return dict(checked=len(paths), findings=findings, notes=notes)
    run("Formgen→Toolgen", "original geometry-spec/1 schema; no geometry/render approval", formgen)
    if args.toolgen:
        run("Formgen→Toolgen provenance", "native consumer hashes, format and sheet kind; no build",
            lambda: _native("toolgen", args.toolgen, {}))
    def cubegen():
        offered = ce_contract.read_json(args.cubegen / "cubegen/contract.json")
        copied = ce_contract.read_json(args.ce / "Assets/Resources/Cubegen/cubegen_contract.json")
        if offered.get("contract") != "cubegen-sidecar" or type(offered.get("version")) is not int or offered["version"] not in range(1, 5):
            raise ValueError("unknown Cubegen contract identity/version")
        report = _native("cubegen", args.cubegen, dict(package=str(args.cubegen_package or args.cubegen / "output"),
                                                     version=args.cubegen_version, editor=args.editor_package))
        if offered != copied: report["findings"].append("current native Cubegen contract differs from CE copy")
        return report
    run("Cubegen→CE", "native sidecar semantics and contract equality" +
        ("; editor FBX, map files and available GLB budget" if args.editor_package else "; sidecar-only, no package/geometry approval"), cubegen)
    run("Robogen→CE", "CE config invariants, complete copy set and PNG headers; no image/rig approval",
        lambda: dict(checked=1, findings=check_robot_package(args.robogen / "output")))
    run("Motiongen→CE/Toolgen", "native measurement manifest and local anchors; no model export",
        lambda: _native("motiongen", args.motiongen, {}))
    return dict(status="passed" if results and all(r["status"] == "passed" for r in results) else "failed", connections=results)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--worker":
        try:
            with contextlib.redirect_stdout(io.StringIO()): data = _worker(argv[1], Path(argv[2]), json.loads(argv[3]))
            print(json.dumps(data, allow_nan=False)); return 0
        except Exception as error:
            print(f"{type(error).__name__}: {error}", file=sys.stderr); return 1
    if argv and argv[0] == "audit":
        argv.pop(0)
    parser = argparse.ArgumentParser(description=__doc__)
    if argv and argv[0] == "robot-package":
        parser.add_argument("root", type=Path)
        args = parser.parse_args(argv[1:])
        report = _result("Robogen→CE", "config/copy-set/PNG-header preflight", 1, check_robot_package(args.root))
    else:
        for name in ("ce", "cubegen", "formgen", "robogen", "motiongen"):
            parser.add_argument("--" + name, type=lambda p: Path(p).expanduser().resolve(), required=True)
        parser.add_argument("--toolgen", type=lambda p: Path(p).expanduser().resolve())
        parser.add_argument("--cubegen-version", type=int, choices=range(1, 5), required=True)
        parser.add_argument("--cubegen-package", type=lambda p: Path(p).expanduser().resolve())
        parser.add_argument("--editor-package", action="store_true")
        report = audit(parser.parse_args(argv))
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
