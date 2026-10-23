"""Own-scene Blender smoke. Run only in a fresh headless Blender under its resource lock.

blender --background --factory-startup --python-exit-code 1 --python smoke.py -- \
    --repo /path/to/ce-agent-tools --out /external/new/smoke --python /path/to/python3
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--repo", required=True, type=Path)
parser.add_argument("--out", required=True, type=Path)
parser.add_argument("--python", required=True)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
repo = args.repo.resolve()
output = args.out.resolve()
if output.exists() and any(output.iterdir()):
    raise RuntimeError("Neuer oder leerer externer Smoke-Ordner erforderlich")
output.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(repo / "integrations" / "blender"))
sys.path.insert(0, str(repo / "plugins" / "ce-shared" / "tools"))
import ce_blender_tools
from ce_blender_tools import addon
import ce_blender_preflight as preflight
import ce_turnaround as turnaround

# Real RNA registration and an actual asynchronous profile check through the UI operator.
ce_blender_tools.register()
assert hasattr(bpy.types.WindowManager, "ce_blender_tools")
assert bpy.types.Panel.bl_rna_get_subclass_py("CE_PT_tools") is addon.CE_PT_tools
settings = bpy.context.window_manager.ce_blender_tools
settings.tools_root = str(repo / "plugins" / "ce-shared" / "tools")
settings.python = args.python
settings.cache = str(output / "jobs")
profile = {"format": 1, "generator": "addon-smoke", "unit_scale": 1,
           "bones": [], "materials": {"Body": "Body"}, "images": {}}
profile_path = output / "preflight-profile.json"
profile_path.write_text(json.dumps(profile), encoding="utf-8")
settings.preflight_profile = str(profile_path)
result = bpy.ops.ce_tools.run(kind="profile_preflight")
assert result == {"FINISHED"}, result
assert addon._job is not None
addon._job.process.wait(timeout=20)
addon._poll()
assert addon._last_result["ok"], addon._last_result
# A file load must not drop the completion callback while the job remains active.
# Background mode does not pump timers here, so the pending Job deterministically
# survives the load even if its external profile process has already exited.
load_fixture = output / "timer-load.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(load_fixture))
assert bpy.ops.ce_tools.run(kind="profile_preflight") == {"FINISHED"}
loading_job = addon._job
assert loading_job is not None
bpy.ops.wm.open_mainfile(filepath=str(load_fixture), load_ui=False)
assert addon._job is loading_job
assert bpy.app.timers.is_registered(addon._poll), "file load lost the pending-job timer"
assert not addon.CE_OT_run_tool.poll(bpy.context)
loading_job.process.wait(timeout=20)
assert addon._poll() is None
assert addon._job is None and addon._last_result["ok"]
assert addon.CE_OT_run_tool.poll(bpy.context)
# Property registration alone does not imply actual layout drawing; GUI probe remains separate.
ce_blender_tools.unregister()
assert not hasattr(bpy.types.WindowManager, "ce_blender_tools")
ce_blender_tools.register()
ce_blender_tools.unregister()

assets = output / "assets"
assets.mkdir()
render_profile = turnaround.example_profile()
render_profile["color_management"]["look"] = "None"
report = {"registration": "passed", "profile_operator": "passed", "timer_after_blend_load": "passed", "renders": {}, "preflight": {}}
means = []
for label, scale in (("small", 0.01), ("large", 10.0)):
    # This process was launched with factory startup; no user's scene is opened.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 16
    scene.cycles.use_denoising = False
    world = bpy.data.worlds.new("FixtureWorld")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.05, 0.05, 0.05, 1)
    world.node_tree.nodes["Background"].inputs[1].default_value = 0.2
    scene.world = world
    bpy.ops.mesh.primitive_cube_add(size=2 * scale)
    cube = bpy.context.object
    cube.name = "FixtureCube"
    material = bpy.data.materials.new("Body")
    material.use_nodes = True
    material.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.3, 0.3, 0.3, 1)
    material.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = 0.8
    cube.data.materials.append(material)
    source = assets / (label + ".blend")
    bpy.ops.wm.save_as_mainfile(filepath=str(source))
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    preview = output / ("preview-" + label)
    preview.mkdir()
    exec(compile(turnaround.make_script(source, preview, 1, 128, render_profile), "generated_turnaround", "exec"), {})
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    image_path = preview / "turn_001.png"
    image = bpy.data.images.load(str(image_path), check_existing=False)
    pixels = list(image.pixels)
    mean = sum(pixels[i] * 0.2126 + pixels[i+1] * 0.7152 + pixels[i+2] * 0.0722 for i in range(0, len(pixels), 4)) / (len(pixels) / 4)
    assert mean > 0.02, (label, mean)
    means.append(mean)
    report["renders"][label] = {"frame": str(image_path), "mean_linear_luminance": mean, "source_sha256": before}
    bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False)
    check_path = output / (label + "-preflight.json")
    exec(compile(preflight.blender_script(profile, source, check_path, "smoke-" + label), "generated_preflight", "exec"), {})
    check = json.loads(check_path.read_text(encoding="utf-8"))
    assert check["ok"], check
    report["preflight"][label] = str(check_path)
assert abs(means[0] - means[1]) / max(means) < 0.03, means
report["relative_luminance_difference"] = abs(means[0] - means[1]) / max(means)
(output / "smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
