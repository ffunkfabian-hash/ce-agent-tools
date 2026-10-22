import contextlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1] / "plugins/ce-shared/tools"

import sys
sys.path.insert(0, str(TOOLS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight = load("ce_blender_preflight")


class BlenderPreflightTests(unittest.TestCase):
    def setUp(self):
        lock_patch = patch.object(preflight.heavy_lock, "held", side_effect=lambda *args, **kwargs: contextlib.nullcontext())
        self.lock_mock = lock_patch.start()
        self.addCleanup(lock_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "profile.json"
        self.profile = {"format": 1, "generator": "example-generator", "unit_scale": 1.0,
                        "bones": [{"name": "root", "parent": None}], "materials": {"Mat_A": "M_A"},
                        "images": {"albedo": {"color_space": "sRGB", "required": True}}}

    def tearDown(self):
        self.temp.cleanup()

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return self.path

    def test_accepts_explicit_generator_profile(self):
        self.write(self.profile)
        self.assertEqual(preflight.load_profile(self.path), self.profile)

    def test_rejects_duplicate_keys_and_nonfinite_scale(self):
        self.path.write_text('{"format":1,"format":1}', encoding="utf-8")
        with self.assertRaises(preflight.ProfileError):
            preflight.load_profile(self.path)
        self.profile["unit_scale"] = float("inf")
        self.write(self.profile)
        with self.assertRaises(preflight.ProfileError):
            preflight.load_profile(self.path)

    def test_rejects_unknown_fields_and_dangling_parent(self):
        self.profile["surprise"] = True
        self.write(self.profile)
        with self.assertRaises(preflight.ProfileError):
            preflight.load_profile(self.path)
        del self.profile["surprise"]
        self.profile["bones"][0]["parent"] = "missing"
        self.write(self.profile)
        with self.assertRaisesRegex(preflight.ProfileError, "fehlt"):
            preflight.load_profile(self.path)

    def test_rejects_boolean_format_bone_cycles_and_ambiguous_image_names(self):
        self.profile["format"] = True
        self.write(self.profile)
        with self.assertRaisesRegex(preflight.ProfileError, "integer 1"):
            preflight.load_profile(self.path)
        self.profile["format"] = 1
        self.profile["bones"] = [{"name": "a", "parent": "a"}]
        self.write(self.profile)
        with self.assertRaisesRegex(preflight.ProfileError, "sich selbst"):
            preflight.load_profile(self.path)
        self.profile["bones"] = [{"name": "a", "parent": "b"}, {"name": "b", "parent": "a"}]
        self.write(self.profile)
        with self.assertRaisesRegex(preflight.ProfileError, "Zyklus"):
            preflight.load_profile(self.path)
        self.profile["bones"] = [{"name": "root", "parent": None}]
        self.profile["images"]["normal"] = {"name": "albedo", "color_space": "Non-Color", "required": True}
        self.write(self.profile)
        with self.assertRaisesRegex(preflight.ProfileError, "Doppelter Bildname"):
            preflight.load_profile(self.path)

    def test_generated_script_imports_fbx_and_writes_fresh_atomic_report(self):
        script = preflight.blender_script(self.profile, Path("asset.fbx"), Path("report.json"), "run-123")
        compile(script, "blender_preflight_generated.py", "exec")
        self.assertIn("bpy.ops.import_scene.fbx", script)
        self.assertIn("bpy.data.objects.remove(obj, do_unlink=True)", script)
        self.assertIn("if is_blend and abs(scene_scale", script)
        self.assertIn("unavailable_for_fbx", script)
        self.assertIn("actual_bones.setdefault", script)
        self.assertIn("bpy.data.images if image.name == expected_name", script)
        self.assertIn("os.replace(temporary_report", script)
        self.assertIn("run-123", script)

    def test_blender_command_propagates_python_errors(self):
        command = preflight.preflight_command("blender", Path("asset.fbx"), Path("runner.py"))
        self.assertIn("--python-exit-code", command)
        self.assertEqual(command[command.index("--python-exit-code") + 1], "1")

    def test_main_rejects_stale_report_without_overwriting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets = root / "assets"
            assets.mkdir()
            asset = assets / "model.blend"
            asset.write_bytes(b"placeholder")
            report = root / "reports" / "run.json"
            report.parent.mkdir()
            report.write_text("old report", encoding="utf-8")
            with patch.object(preflight.subprocess, "run") as run:
                code = preflight.main(["--profile", str(self.write(self.profile)), "--asset", str(asset), "--blender", "blender", "--out", str(report)])
            self.assertEqual(code, 2)
            self.assertEqual(report.read_text(encoding="utf-8"), "old report")
            run.assert_not_called()

    def test_main_rejects_report_with_wrong_run_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets = root / "assets"
            assets.mkdir()
            asset = assets / "model.blend"
            asset.write_bytes(b"placeholder")
            report = root / "reports" / "run.json"
            profile = self.write(self.profile)

            def write_stale_report(command, check=False):
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(json.dumps({"run_id": "old-run", "ok": True}), encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with patch.object(preflight.subprocess, "run", side_effect=write_stale_report):
                code = preflight.main(["--profile", str(profile), "--asset", str(asset), "--blender", "blender", "--out", str(report)])
            self.assertEqual(code, 1)

    def test_main_rejects_report_inside_repository(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as source_temp:
            repo = Path(temp)
            (repo / ".git").mkdir()
            asset = Path(source_temp) / "model.blend"
            asset.write_bytes(b"placeholder")
            profile = self.write(self.profile)
            with patch.object(preflight.subprocess, "run") as run:
                code = preflight.main(["--profile", str(profile), "--asset", str(asset), "--blender", "blender", "--out", str(repo / "report.json")])
            self.assertEqual(code, 2)
            run.assert_not_called()

    def test_main_returns_failure_when_blender_python_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets = root / "assets"
            assets.mkdir()
            asset = assets / "model.blend"
            asset.write_bytes(b"placeholder")
            report = root / "reports" / "run.json"
            profile = self.write(self.profile)
            with patch.object(preflight.subprocess, "run", return_value=SimpleNamespace(returncode=1)) as run:
                code = preflight.main(["--profile", str(profile), "--asset", str(asset), "--blender", "blender", "--out", str(report)])
            self.assertEqual(code, 1)
            self.assertIn("--python-exit-code", run.call_args.args[0])
            self.assertFalse(report.exists())


if __name__ == "__main__":
    unittest.main()
