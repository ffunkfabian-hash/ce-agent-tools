#!/usr/bin/env python3
"""Headless still/orbit preview adapter for an explicitly supplied .blend or FBX."""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

try:
    import heavy_lock
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import heavy_lock


def example_profile() -> dict:
    """Return a neutral example, not a claimed production-generator contract."""
    return {
        "format": 1,
        "generator": "example-generator",
        "camera": {"bounds_margin": 1.4, "elevation_degrees": 24.0},
        "limits": {"min_resolution": 128, "max_resolution": 2048, "max_frames": 120},
        "color_management": {"view_transform": "Standard", "look": "Medium High Contrast", "exposure": 0.0, "gamma": 1.0},
    }


def validate_profile(profile: dict) -> dict:
    expected = {"format", "generator", "camera", "limits", "color_management"}
    if not isinstance(profile, dict) or set(profile) != expected:
        raise ValueError("Profil braucht genau format, generator, camera, limits und color_management")
    if type(profile["format"]) is not int or profile["format"] != 1 or not isinstance(profile["generator"], str) or not profile["generator"].strip():
        raise ValueError("Profilformat muss 1 und generator ein nichtleerer Name sein")
    camera = profile["camera"]
    limits = profile["limits"]
    color = profile["color_management"]
    if not isinstance(camera, dict) or set(camera) != {"bounds_margin", "elevation_degrees"}:
        raise ValueError("camera braucht bounds_margin und elevation_degrees")
    margin, elevation = camera["bounds_margin"], camera["elevation_degrees"]
    if not _finite_number(margin) or margin < 1 or not _finite_number(elevation) or not 0 <= elevation < 89:
        raise ValueError("camera.bounds_margin muss >= 1 und elevation_degrees zwischen 0 und 89 sein")
    if not isinstance(limits, dict) or set(limits) != {"min_resolution", "max_resolution", "max_frames"}:
        raise ValueError("limits braucht min_resolution, max_resolution und max_frames")
    low, high, frames = (limits[k] for k in ("min_resolution", "max_resolution", "max_frames"))
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (low, high, frames)) or not 16 <= low <= high <= 8192 or not 1 <= frames <= 360:
        raise ValueError("limits außerhalb der zulässigen Grenzen (Auflösung 16–8192, Frames 1–360)")
    if not isinstance(color, dict) or set(color) != {"view_transform", "look", "exposure", "gamma"}:
        raise ValueError("color_management braucht view_transform, look, exposure und gamma")
    if not isinstance(color["view_transform"], str) or not color["view_transform"] or not isinstance(color["look"], str):
        raise ValueError("view_transform und look müssen Text sein")
    if not _finite_number(color["exposure"]) or not _finite_number(color["gamma"]) or color["gamma"] <= 0:
        raise ValueError("exposure muss endlich und gamma endlich und positiv sein")
    return profile


def _finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def load_profile(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"doppelter JSON-Schlüssel: {key}")
            result[key] = value
        return result

    profile = json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda token: (_ for _ in ()).throw(ValueError(f"ungültige Zahl: {token}")), object_pairs_hook=unique)
    return validate_profile(profile)


def make_script(source: Path, out_dir: Path, frames: int, resolution: int, profile: dict) -> str:
    """Build and return standalone Blender Python using safely repr-quoted values."""
    profile = validate_profile(profile)
    return f'''import bpy
import json
import math
import os
from mathutils import Vector

source = {str(source.resolve())!r}
output_dir = {str(out_dir.resolve())!r}
frame_count = {frames!r}
resolution = {resolution!r}
profile = {profile!r}
if source.lower().endswith(".fbx"):
    for obj in list(bpy.context.scene.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.ops.import_scene.fbx(filepath=source)
scene = bpy.context.scene
meshes = [obj for obj in scene.objects if obj.type == "MESH"]
if not meshes:
    raise RuntimeError("Keine Meshes zum Anzeigen")
points = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
minimum = [min(point[axis] for point in points) for axis in range(3)]
maximum = [max(point[axis] for point in points) for axis in range(3)]
center = Vector(tuple((minimum[i] + maximum[i]) * 0.5 for i in range(3)))
radius = max(Vector(tuple(maximum[i] - minimum[i] for i in range(3))).length * 0.5, 1e-6)
margin = profile["camera"]["bounds_margin"]
elevation = math.radians(profile["camera"]["elevation_degrees"])
camera_data = bpy.data.cameras.new("CE_TurnaroundCamera")
camera = bpy.data.objects.new("CE_TurnaroundCamera", camera_data)
scene.collection.objects.link(camera)
scene.camera = camera
camera_data.type = "ORTHO"
camera_data.ortho_scale = radius * 2.0 * margin
camera_data.clip_start = radius * 0.001
camera_data.clip_end = radius * 10
light_data = bpy.data.lights.new("CE_TurnaroundLight", "AREA")
light = bpy.data.objects.new("CE_TurnaroundLight", light_data)
scene.collection.objects.link(light)
# Preserve irradiance as the bounds-relative distance and emitter area scale.
# Reference: 1400 W at a bounds radius of one Blender unit.
light_data.energy = 1400 * radius ** 2
light_data.shape = "DISK"
light_data.size = radius * 2
scene.render.resolution_x = resolution
scene.render.resolution_y = resolution
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = "PNG"
color = profile["color_management"]
scene.view_settings.view_transform = color["view_transform"]
scene.view_settings.look = color["look"]
scene.view_settings.exposure = color["exposure"]
scene.view_settings.gamma = color["gamma"]
if scene.render.engine == "CYCLES":
    scene.cycles.seed = 0
    scene.cycles.use_animated_seed = False
for index in range(frame_count):
    angle = 2 * math.pi * index / frame_count if frame_count > 1 else 0
    direction = Vector((math.sin(angle) * math.cos(elevation), -math.cos(angle) * math.cos(elevation), math.sin(elevation))).normalized()
    camera.location = center + direction * radius * 5
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    light.location = center + Vector((radius * 2, -radius * 3, radius * 4))
    light.rotation_euler = (center - light.location).to_track_quat("-Z", "Y").to_euler()
    scene.render.filepath = os.path.join(output_dir, "turn_" + format(index + 1, "03d") + ".png")
    bpy.ops.render.render(write_still=True)
'''


def _outside_production_paths(output: Path, source: Path) -> bool:
    """Reject output inside the asset tree or any repository/project checkout."""
    if output == source.parent or source.parent in output.parents or output in source.parents:
        return False
    for parent in (output, *output.parents):
        if (parent / ".git").exists() or (parent / ".ce-repo.json").exists() or (parent / "ProjectSettings").is_dir():
            return False
    return True


def media_command(media: Path, output: Path, frames: int, clip: bool, fps: int) -> list[str]:
    if clip:
        return [sys.executable, str(media), "clip", str(output), "--out", str(output / "orbit.mp4"), "--fps", str(fps), "--height", "480"]
    if frames > 1:
        return [sys.executable, str(media), "sheet", str(output), "--out", str(output / "contact.png"), "--label"]
    return [sys.executable, str(media), "still", str(output / "turn_001.png"), "--out", str(output / "still.png")]


def blender_command(blender: str, source: Path, script: Path) -> list[str]:
    command = [blender, "--background", "--disable-autoexec", "--python-exit-code", "1"]
    command += [str(source.resolve())] if source.suffix.lower() == ".blend" else ["--factory-startup"]
    command += ["--python", str(script.resolve())]
    return command


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Vorhandene .blend- oder .fbx-Datei")
    parser.add_argument("--profile", type=Path, help="Explizites generatorbezogenes Vorschauprofil")
    parser.add_argument("--example-profile", action="store_true", help="Beispielprofil als JSON ausgeben und beenden")
    parser.add_argument("--blender", default="blender")
    parser.add_argument("--out", type=Path, help="Neuer oder leerer Ausgabeordner außerhalb von Repo und Assetpfad")
    parser.add_argument("--frames", type=int, default=1, help="1 für Standbild, mehr für Orbitvorschau")
    parser.add_argument("--resolution", type=int, default=640)
    parser.add_argument("--clip", action="store_true", help="Orbitbilder zusätzlich als MP4 exportieren (nur zusammen mit --frames > 1)")
    parser.add_argument("--fps", type=int, default=30, help="Bildrate für ausdrücklich angeforderte MP4")
    args = parser.parse_args(argv)
    if args.example_profile:
        print(json.dumps(example_profile(), ensure_ascii=False, indent=2))
        return 0
    if not args.input or not args.profile or not args.out:
        parser.error("--input, --profile und --out sind erforderlich (oder --example-profile)")
    source = args.input.resolve()
    output = args.out.resolve()
    try:
        profile = load_profile(args.profile)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"ce_turnaround: Profilfehler: {exc}", file=sys.stderr)
        return 2
    if not source.is_file() or source.suffix.lower() not in {".blend", ".fbx"}:
        print("ce_turnaround: --input muss eine vorhandene .blend- oder .fbx-Datei sein", file=sys.stderr)
        return 2
    if not _outside_production_paths(output, source):
        print("ce_turnaround: Ausgabe muss außerhalb des Assetpfads und jedes Produktions-Repos liegen", file=sys.stderr)
        return 2
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        print("ce_turnaround: Ausgabeordner existiert und ist nicht leer", file=sys.stderr)
        return 2
    limits = profile["limits"]
    if not limits["min_resolution"] <= args.resolution <= limits["max_resolution"] or not 1 <= args.frames <= limits["max_frames"]:
        print("ce_turnaround: Auflösung oder Framezahl liegt außerhalb der Profilgrenzen", file=sys.stderr)
        return 2
    if args.clip and args.frames <= 1:
        print("ce_turnaround: --clip erfordert --frames > 1", file=sys.stderr)
        return 2
    if args.fps < 1 or args.fps > 60:
        print("ce_turnaround: --fps muss zwischen 1 und 60 liegen", file=sys.stderr)
        return 2
    media = Path(__file__).with_name("ce_media.py")
    if not media.is_file():
        print(f"ce_turnaround: ce_media.py fehlt: {media}", file=sys.stderr)
        return 2
    output.mkdir(parents=True, exist_ok=True)
    script = output / "turnaround_runner.py"
    script.write_text(make_script(source, output, args.frames, args.resolution, profile), encoding="utf-8")
    command = blender_command(args.blender, source, script)
    try:
        with heavy_lock.held(f"{profile['generator']} turnaround", resource="blender"):
            result = subprocess.run(heavy_lock.script_argv(command), check=False)  # (0.31) Windows: a script Blender
    except OSError as exc:
        print(f"ce_turnaround: Blender nicht startbar: {exc}", file=sys.stderr)
        return 2
    if result.returncode:
        return 1
    if subprocess.run(media_command(media.resolve(), output, args.frames, args.clip, args.fps), check=False).returncode:
        return 1
    print(f"{output}: {args.frames} Bild(er) für {profile['generator']}")
    return 0


if __name__ == "__main__":
    try:  # (0.31) Windows: a cp1252 console or pipe cannot take the tool's „“ → ·
        import ce_common as _console
        _console.utf8_console()
    except ImportError:
        pass
    raise SystemExit(main())
