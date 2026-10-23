import ast
import contextlib
import math
import importlib.util
import io
import json
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1] / "plugins/ce-shared/tools"

import sys
sys.path.insert(0, str(TOOLS))
spec = importlib.util.spec_from_file_location("ce_turnaround", TOOLS / "ce_turnaround.py")
turnaround = importlib.util.module_from_spec(spec)
spec.loader.exec_module(turnaround)


class TurnaroundTests(unittest.TestCase):
    def setUp(self):
        lock_patch = patch.object(turnaround.heavy_lock, "held", side_effect=lambda *args, **kwargs: contextlib.nullcontext())
        self.lock_mock = lock_patch.start()
        self.addCleanup(lock_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_dir = self.root / "source"
        self.source_dir.mkdir()
        self.source = self.source_dir / "input.fbx"
        self.source.write_bytes(b"placeholder")
        self.output = self.root / "preview-output"
        self.profile_path = self.root / "profile.json"
        self.profile_path.write_text(json.dumps(turnaround.example_profile()), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def args(self, *extra):
        return ["--input", str(self.source), "--profile", str(self.profile_path), "--out", str(self.output), *extra]

    def test_rejects_boolean_profile_format(self):
        profile = turnaround.example_profile()
        profile["format"] = True
        with self.assertRaisesRegex(ValueError, "format"):
            turnaround.validate_profile(profile)

    def test_generated_script_uses_profile_bounds_limits_and_color_and_parses(self):
        profile = turnaround.example_profile()
        script = turnaround.make_script(self.source, self.output, 8, 512, profile)
        compile(script, "turnaround_generated.py", "exec")
        self.assertIn("bound_box", script)
        self.assertIn("camera_data.clip_end = radius * 10", script)
        self.assertIn('margin = profile["camera"]["bounds_margin"]', script)
        self.assertIn('scene.view_settings.view_transform = color["view_transform"]', script)
        self.assertIn('"turn_" + format(index + 1, "03d")', script)
        self.assertIn("frame_count = 8", script)
        self.assertIn("bpy.data.objects.remove(obj, do_unlink=True)", script)

    def test_light_irradiance_and_camera_clipping_are_scale_independent(self):
        script = turnaround.make_script(self.source, self.output, 1, 128, turnaround.example_profile())
        assignments = {ast.unparse(node.targets[0]): node.value for node in ast.parse(script).body if isinstance(node, ast.Assign)}
        def expression(name, radius):
            return eval(compile(ast.Expression(assignments[name]), "generated", "eval"), {"radius": radius})
        irradiances = []
        for radius in (0.001, 1.0, 1000.0):
            energy = expression("light_data.energy", radius)
            # Light distance is sqrt(2²+3²+4²) radii; relative area is constant.
            irradiances.append(energy / (29 * radius ** 2))
            self.assertLess(expression("camera_data.clip_start", radius), radius * 5)
            self.assertGreater(expression("camera_data.clip_end", radius), radius * 5)
        self.assertAlmostEqual(irradiances[0], irradiances[1])
        self.assertAlmostEqual(irradiances[1], irradiances[2])
        self.assertEqual(expression("light_data.energy", 1), 1400)
        class BoundsVector:
            def __init__(self, values):
                self.length = math.sqrt(sum(value ** 2 for value in values))
        radius = eval(compile(ast.Expression(assignments["radius"]), "bounds", "eval"),
                      {"Vector": BoundsVector, "minimum": [0, 0, 0], "maximum": [0.002] * 3})
        self.assertAlmostEqual(radius, math.sqrt(3) * 0.001)

    def test_main_returns_blender_failure_without_running_media_on_script_exception(self):
        with patch.object(turnaround.subprocess, "run", return_value=SimpleNamespace(returncode=1)) as run:
            code = turnaround.main(self.args("--frames", "4"))
        self.assertEqual(code, 1)
        self.assertEqual(run.call_count, 1)
        self.assertIn("--python-exit-code", run.call_args.args[0])

    def test_blend_command_disables_autoexec_and_uses_argument_list(self):
        blend = self.source_dir / "odd ' name.blend"
        command = turnaround.blender_command("blender", blend, self.output / "runner.py")
        self.assertIn("--disable-autoexec", command)
        self.assertEqual(command[command.index("--python-exit-code") + 1], "1")
        self.assertIn(str(blend.resolve()), command)
        self.assertEqual(command[-2], "--python")
        self.assertEqual(command[-1], str((self.output / "runner.py").resolve()))

    def test_media_uses_shared_ce_media_and_requires_explicit_clip_choice(self):
        media = TOOLS / "ce_media.py"
        sheet = turnaround.media_command(media, self.output, 12, False, 24)
        clip = turnaround.media_command(media, self.output, 12, True, 24)
        self.assertEqual(sheet[1:3], [str(media), "sheet"])
        self.assertEqual(clip[1:3], [str(media), "clip"])
        self.assertEqual(clip[clip.index("--fps") + 1], "24")

    def test_clip_flag_requires_orbit_frames(self):
        with patch.object(turnaround.subprocess, "run") as run:
            code = turnaround.main(self.args("--clip"))
        self.assertEqual(code, 2)
        run.assert_not_called()

    def test_example_profile_is_json_on_stdout(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = turnaround.main(["--example-profile"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), turnaround.example_profile())

    def test_rejects_outside_asset_tree_and_production_repo(self):
        with patch.object(turnaround.subprocess, "run") as run:
            code = turnaround.main(self.args("--out", str(self.source_dir / "previews")))
        self.assertEqual(code, 2)
        run.assert_not_called()

        repo = self.root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        with tempfile.TemporaryDirectory() as source_temp:
            source = Path(source_temp) / "other-source.fbx"
            source.write_bytes(b"placeholder")
            out = repo / "preview"
            with patch.object(turnaround.subprocess, "run") as run:
                code = turnaround.main(["--input", str(source), "--profile", str(self.profile_path), "--out", str(out)])
            self.assertEqual(code, 2)
            run.assert_not_called()

    def test_rejects_nonempty_output_without_overwriting(self):
        self.output.mkdir()
        existing = self.output / "important.txt"
        existing.write_text("keep", encoding="utf-8")
        with patch.object(turnaround.subprocess, "run") as run:
            code = turnaround.main(self.args())
        self.assertEqual(code, 2)
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
        run.assert_not_called()

    def test_profile_limits_reject_invalid_frame_count_before_blender(self):
        with patch.object(turnaround.subprocess, "run") as run:
            code = turnaround.main(self.args("--frames", "121"))
        self.assertEqual(code, 2)
        run.assert_not_called()

    def test_profile_schema_rejects_unknown_fields_and_nonfinite_camera_values(self):
        profile = turnaround.example_profile()
        profile["unexpected"] = True
        with self.assertRaises(ValueError):
            turnaround.validate_profile(profile)
        profile = turnaround.example_profile()
        profile["camera"]["bounds_margin"] = float("inf")
        with self.assertRaises(ValueError):
            turnaround.validate_profile(profile)


if __name__ == "__main__":
    unittest.main()
