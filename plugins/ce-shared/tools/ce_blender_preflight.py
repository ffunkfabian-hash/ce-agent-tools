#!/usr/bin/env python3
"""Validate an explicit generator profile and optionally inspect one Blender asset."""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import uuid
from pathlib import Path

try:
    import heavy_lock
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import heavy_lock


class ProfileError(ValueError):
    pass


def _keys(value, required, optional=(), where="Profil"):
    if not isinstance(value, dict):
        raise ProfileError(f"{where}: Objekt erwartet")
    extra = set(value) - set(required) - set(optional)
    missing = set(required) - set(value)
    if extra or missing:
        raise ProfileError(f"{where}: unbekannte Schlüssel {sorted(extra)}; fehlende Schlüssel {sorted(missing)}")


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ProfileError(f"doppelter JSON-Schlüssel: {key}")
        out[key] = value
    return out


def load_profile(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda token: (_ for _ in ()).throw(ProfileError(f"nicht endliche Zahl: {token}")), object_pairs_hook=_unique)
    except (OSError, json.JSONDecodeError, ProfileError) as exc:
        raise ProfileError(str(exc)) from exc
    _keys(data, {"format", "generator", "unit_scale", "bones", "materials", "images"}, {"require_unit_object_scale"})
    if type(data["format"]) is not int or data["format"] != 1 or not isinstance(data["generator"], str) or not data["generator"].strip():
        raise ProfileError("format muss integer 1 und generator ein nichtleerer Name sein")
    scale = data["unit_scale"]
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
        raise ProfileError("unit_scale muss endlich und positiv sein")
    if "require_unit_object_scale" in data and not isinstance(data["require_unit_object_scale"], bool):
        raise ProfileError("require_unit_object_scale muss boolesch sein")
    if not isinstance(data["bones"], list) or not isinstance(data["materials"], dict) or not isinstance(data["images"], dict):
        raise ProfileError("bones muss Liste, materials und images müssen Objekte sein")
    names = set()
    parents = {}
    for index, bone in enumerate(data["bones"]):
        _keys(bone, {"name", "parent"}, where=f"bones[{index}]")
        if not isinstance(bone["name"], str) or not bone["name"] or bone["name"] in names:
            raise ProfileError(f"bones[{index}].name muss eindeutig und nichtleer sein")
        if bone["parent"] is not None and not isinstance(bone["parent"], str):
            raise ProfileError(f"bones[{index}].parent muss Text oder null sein")
        names.add(bone["name"])
        parents[bone["name"]] = bone["parent"]
    for name, parent in parents.items():
        if parent == name:
            raise ProfileError(f"Bone {name}: Parent darf nicht auf sich selbst zeigen")
        if parent is not None and parent not in names:
            raise ProfileError(f"Bone {name}: Parent {parent} fehlt in bones")
    for name in parents:
        seen = set()
        current = name
        while current is not None:
            if current in seen:
                raise ProfileError(f"Bone-Hierarchie enthält Zyklus bei {current}")
            seen.add(current)
            current = parents[current]
    for name, target in data["materials"].items():
        if not isinstance(name, str) or not name or not isinstance(target, str) or not target:
            raise ProfileError("materials ordnet nichtleere Namen auf nichtleere Zielnamen ab")
    image_names = set()
    for role, spec in data["images"].items():
        _keys(spec, {"color_space", "required"}, {"name"}, f"images.{role}")
        image_name = spec.get("name", role)
        if not isinstance(role, str) or not role or not isinstance(image_name, str) or not image_name:
            raise ProfileError(f"images.{role}: name muss nichtleerer Text sein")
        if image_name in image_names:
            raise ProfileError(f"Doppelter Bildname im Profil: {image_name}")
        image_names.add(image_name)
        if not isinstance(spec["color_space"], str) or not spec["color_space"] or not isinstance(spec["required"], bool):
            raise ProfileError(f"images.{role}: color_space muss Text, required boolesch sein")
    return data


def blender_script(profile: dict, input_path: Path, report_path: Path, run_id: str | None = None) -> str:
    """Return inspection code with all external strings encoded as a Python literal."""
    run_id = run_id or uuid.uuid4().hex
    payload = json.dumps({"profile": profile, "input": str(input_path.resolve()), "report": str(report_path.resolve()), "run_id": run_id})
    return f'''import bpy
import json
import os

cfg = json.loads({payload!r})
profile = cfg["profile"]
scene = bpy.context.scene
is_blend = cfg["input"].lower().endswith(".blend")
if not is_blend:
    for obj in list(scene.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.ops.import_scene.fbx(filepath=cfg["input"])
objects = list(scene.objects)
armatures = [obj for obj in objects if obj.type == "ARMATURE"]
actual_bones = {{}}
issues = []
for armature in armatures:
    for bone in armature.data.bones:
        actual_bones.setdefault(bone.name, []).append(bone.parent.name if bone.parent else None)
for bone in profile["bones"]:
    parents = actual_bones.get(bone["name"], [])
    if len(parents) > 1:
        issues.append("ambiguous bone name: " + bone["name"])
    elif not parents or parents[0] != bone["parent"]:
        issues.append("bone/parent: " + bone["name"])
assigned_materials = {{slot.material.name for obj in objects for slot in obj.material_slots if slot.material}}
for expected, target in profile["materials"].items():
    if expected not in assigned_materials and target not in assigned_materials:
        issues.append("assigned material missing: " + expected + " -> " + target)
image_report = {{}}
for role, spec in profile["images"].items():
    expected_name = spec.get("name", role)
    matches = [(image.name, image.colorspace_settings.name, image.library.filepath if image.library else None)
               for image in bpy.data.images if image.name == expected_name]
    if len(matches) > 1:
        issues.append("ambiguous image name: " + expected_name)
    if spec["required"] and not matches:
        issues.append("image missing: " + expected_name)
    if len(matches) == 1:
        name, color_space, library = matches[0]
        image_report[role] = {{"name": name, "color_space": color_space, "library": library}}
        if color_space != spec["color_space"]:
            issues.append("image color space: " + role)
scene_scale = scene.unit_settings.scale_length if is_blend else None
if is_blend and abs(scene_scale - profile["unit_scale"]) > 1e-6:
    issues.append("scene unit scale mismatch")
object_scales = {{obj.name: list(obj.scale) for obj in objects if obj.type in {{"ARMATURE", "MESH"}}}}
if profile.get("require_unit_object_scale", False):
    for name, scale in object_scales.items():
        if any(abs(component - 1.0) > 1e-5 for component in scale):
            issues.append("object scale mismatch: " + name)
report = {{"run_id": cfg["run_id"], "ok": not issues, "issues": issues,
          "unit_scale": scene_scale, "unit_scale_status": "checked" if is_blend else "unavailable_for_fbx",
          "object_scales": object_scales, "bones": actual_bones,
          "assigned_materials": sorted(assigned_materials), "images": image_report}}
temporary_report = cfg["report"] + "." + cfg["run_id"] + ".tmp"
with open(temporary_report, "w", encoding="utf-8") as stream:
    json.dump(report, stream, indent=2)
os.replace(temporary_report, cfg["report"])
'''

def _safe_report_path(output_dir: Path, source: Path) -> bool:
    output_dir = output_dir.resolve()
    asset_dir = source.resolve().parent
    if output_dir == asset_dir or asset_dir in output_dir.parents:
        return False
    for parent in (output_dir, *output_dir.parents):
        if (parent / ".git").exists() or (parent / ".ce-repo.json").exists() or (parent / "ProjectSettings").is_dir():
            return False
    return True


def preflight_command(blender: str, asset: Path, script: Path) -> list[str]:
    command = [blender, "--background", "--disable-autoexec", "--python-exit-code", "1"]
    command += [str(asset.resolve())] if asset.suffix.lower() == ".blend" else ["--factory-startup"]
    command += ["--python", str(script.resolve())]
    return command


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--asset", type=Path, help="Explizite .blend- oder .fbx-Eingabe")
    parser.add_argument("--blender", default="blender")
    parser.add_argument("--out", type=Path, help="JSON-Prüfbericht für Assetprüfung")
    args = parser.parse_args(argv)
    try:
        profile = load_profile(args.profile)
    except ProfileError as exc:
        print(f"ce_blender_preflight: Profilfehler: {exc}", file=sys.stderr)
        return 2
    if not args.asset:
        print(json.dumps({"ok": True, "generator": profile["generator"], "profile": str(args.profile)}, ensure_ascii=False))
        return 0
    if args.asset.suffix.lower() not in {".blend", ".fbx"} or not args.asset.is_file():
        print("ce_blender_preflight: --asset muss eine vorhandene .blend- oder .fbx-Datei sein", file=sys.stderr)
        return 2
    if not args.out:
        print("ce_blender_preflight: Assetprüfung erfordert --out", file=sys.stderr)
        return 2
    args.out = args.out.resolve()
    source = args.asset.resolve()
    if not _safe_report_path(args.out.parent, source):
        print("ce_blender_preflight: Bericht muss außerhalb von Assetpfad und Produktions-Repos liegen", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    script_path = args.out.with_suffix(".preflight.py")
    if args.out.exists() or script_path.exists():
        print("ce_blender_preflight: Bericht oder Skript existiert bereits; neuer Laufpfad erforderlich", file=sys.stderr)
        return 2
    run_id = uuid.uuid4().hex
    try:
        with script_path.open("x", encoding="utf-8") as stream:
            stream.write(blender_script(profile, args.asset, args.out, run_id))
    except OSError as exc:
        print(f"ce_blender_preflight: Skript kann nicht exklusiv angelegt werden: {exc}", file=sys.stderr)
        return 2
    command = preflight_command(args.blender, source, script_path)
    try:
        with heavy_lock.held(f"{profile['generator']} preflight", resource="blender"):
            result = subprocess.run(command, check=False)
    except OSError as exc:
        print(f"ce_blender_preflight: Blender nicht startbar: {exc}", file=sys.stderr)
        return 2
    if result.returncode:
        return 1
    try:
        report = json.loads(args.out.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ce_blender_preflight: Bericht fehlt oder ist ungültig: {exc}", file=sys.stderr)
        return 1
    if report.get("run_id") != run_id:
        print("ce_blender_preflight: Bericht gehört nicht zu diesem Lauf", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
