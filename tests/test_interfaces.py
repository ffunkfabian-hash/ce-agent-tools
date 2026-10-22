"""Read-only interface adapters: consumer requirements and honest validation depth."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zlib

TOOLS = Path(__file__).resolve().parents[1] / "plugins/ce-shared/tools"
sys.path.insert(0, str(TOOLS))
import ce_interfaces as interfaces


def config():
    skin = {k: [.2, .3, .4] for k in ("armor", "mech", "metal", "accent", "screen", "eye", "visor", "fabric")}
    skin.update(name="Classic", variants={"AccHead": "None"}, expression=None, display=None)
    return dict(skins=[skin], slots=[dict(id="AccHead", variants=["None", "Antenna"])],
                finishes=[dict(id="Standard", roughness=1.9, metalOffset=-.25)], badges=[], linePatterns=[], zones=[],
                materials=[], layers=[dict(id="Wear", texture="T_Robot_WearMask.png", channel=0)], specials=[], forms=[],
                expressions=["Normal"], displays=["Normal"], eyeGrid=[1, 1], displayGrid=[1, 1],
                atlasCells=dict(eyes=dict(Normal=[1., 1., 0., 0.]), displays=dict(Normal=[1., 1., 0., 0.])),
                unityTextures=[dict(file=f"Unity/T_Robot_U_{n}.png", channels="RGBA data") for n in interfaces.ROBOT_MASKS],
                clips={n: dict(fps=30, frames=20, loop=True, naturalSpeed=0., footsteps=[[0, "Foot_L"]], events=[])
                       for n in interfaces.ROBOT_CLIPS})


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def png(width=1, height=1):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    # Header-only oversized fixtures are sufficient for the explicitly named header check.
    pixels = zlib.compress(b"\0\xff\xff\xff\xff")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


def package(root):
    for name in interfaces.ROBOT_FILES:
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png() if name.endswith(".png") else b"fixture FBX; not rig validation")
    write_json(root / "robot_config.json", config())


class RobotInterfaceTests(unittest.TestCase):
    def test_realistic_additive_config_and_optional_mask_only_layers(self):
        document = config(); document["future"] = {"extra": True}
        document["clips"]["FutureClip"] = copy.deepcopy(document["clips"]["Idle"])
        self.assertEqual(interfaces.check_robot_config(document), [])

    def test_missing_classic_channel_finite_number_and_required_clip_are_rejected(self):
        for change in (lambda d: d["skins"][0].update(name="Other"),
                       lambda d: d["layers"][0].update(channel=4),
                       lambda d: d["layers"][0].update(channel=True),
                       lambda d: d["skins"][0].update(armor=[float("nan"), 0., 0.]),
                       lambda d: d["clips"].pop("Idle")):
            document = config(); change(document)
            with self.subTest(change=change): self.assertTrue(interfaces.check_robot_config(document))

    def test_bad_reference_atlas_and_clip_frames_are_rejected(self):
        for change in (lambda d: d["slots"].append(copy.deepcopy(d["slots"][0])),
                       lambda d: d["skins"][0].update(variants={"Unknown": "None"}),
                       lambda d: d["skins"][0].update(expression="Unknown"),
                       lambda d: d["skins"][0].update(zones2=["Unknown"]),
                       lambda d: d["skins"][0].update(badges=[dict(id="Unknown")]),
                       lambda d: d["atlasCells"]["eyes"].update(Normal=[.8, 1., .5, 0.]),
                       lambda d: d["clips"]["Idle"].update(fps=0),
                       lambda d: d["clips"]["Idle"].update(events=[[21, "Impact"]]),
                       lambda d: d["unityTextures"].pop()):
            document = config(); change(document)
            with self.subTest(change=change): self.assertTrue(interfaces.check_robot_config(document))

    def test_malformed_optional_sections_return_findings(self):
        for key in ("displayAnimations", "atlasCells", "clips", "skins", "unityTextures"):
            document = config(); document[key] = None
            with self.subTest(key=key): self.assertTrue(interfaces.check_robot_config(document))

    def test_colored_layers_and_skin_overrides_require_finite_rgb(self):
        document = config()
        layer = dict(id='Rust', texture='Rust.png', channel=0, color=[.1, .2, .3], roughness=1., metalOffset=0.)
        document['layers'].append(layer)
        document['skins'][0]['layers'] = [dict(id='Rust', amount=.5, color=[.4, .5, .6])]
        self.assertEqual(interfaces.check_robot_config(document), [])
        for target in [layer, document['skins'][0]['layers'][0]]:
            original = target['color']
            for bad in ['invalid RGB', [0, 1], [0, float('inf'), 0], [True, 0, 0]]:
                target['color'] = bad
                self.assertTrue(interfaces.check_robot_config(document))
            target['color'] = original

    def test_relative_package_path_is_resolved_before_native_working_directory_changes(self):
        args = [v for name in ('ce', 'cubegen', 'formgen', 'robogen', 'motiongen') for v in ('--' + name, '/tmp/' + name)]
        with mock.patch.object(interfaces, 'audit', return_value={'status': 'passed'}) as audit, \
             mock.patch.object(os, 'getcwd', return_value='/isolated-caller'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(interfaces.main(args + ['--cubegen-version', '4', '--cubegen-package', 'export']), 0)
        self.assertEqual(audit.call_args.args[0].cubegen_package, Path('/isolated-caller/export'))

    def test_complete_package_checked_before_any_copy_and_repeat_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); package(root)
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
            self.assertEqual(interfaces.check_robot_package(root), [])
            self.assertEqual(interfaces.check_robot_package(root), [])
            self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})
            (root / interfaces.ROBOT_FILES[-1]).unlink()
            findings = interfaces.check_robot_package(root)
            self.assertTrue(any(interfaces.ROBOT_FILES[-1] in e for e in findings))

    def test_png_type_and_both_mask_dimensions_are_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); package(root)
            path = root / "Textures/Unity/T_Robot_U_Aux.png"
            for bad in (b"not PNG", png(1, 2048), png(2048, 1)):
                path.write_bytes(bad)
                self.assertTrue(any("Aux.png" in e for e in interfaces.check_robot_package(root)))

    def test_duplicate_json_and_nonfinite_numbers_fail_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); package(root)
            for bad in ('{"clips":{},"clips":{}}', '{"number":NaN}', '{"number":1e999}'):
                (root / "robot_config.json").write_text(bad)
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(interfaces.main(["robot-package", str(root)]), 1)
                self.assertEqual(json.loads(output.getvalue())["status"], "failed")


class InterfaceAuditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.roots = {n: self.base / n for n in ("ce", "cubegen", "formgen", "robogen", "motiongen", "toolgen")}
        for root in self.roots.values(): root.mkdir()
        package(self.roots["robogen"] / "output")
        schema = {"$id": "cubeescape-design/geometry-spec/1", "type": "object", "required": ["format", "format_version"],
                  "properties": {"format": {"const": "cubeescape-design/geometry-spec"}, "format_version": {"const": 1}}}
        write_json(self.roots["formgen"] / "claude/docs/geometry-spec.schema.json", schema)
        write_json(self.roots["formgen"] / "claude/output/sheets/KG02.geo.json",
                   dict(format="cubeescape-design/geometry-spec", format_version=1))
        contract = dict(contract="cubegen-sidecar", version=4)
        write_json(self.roots["cubegen"] / "cubegen/contract.json", contract)
        write_json(self.roots["ce"] / "Assets/Resources/Cubegen/cubegen_contract.json", contract)
        self.args = [v for n, p in self.roots.items() if n != "toolgen" for v in ("--" + n, str(p))]
        self.args += ["--cubegen-version", "4"]

    def report(self, extra=()):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            status = interfaces.main(self.args + list(extra))
        return status, json.loads(output.getvalue())

    def test_missing_roots_and_all_skipped_never_pass(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            interfaces.main([])
        self.assertNotEqual(raised.exception.code, 0)
        with mock.patch.object(interfaces, "_native", return_value=dict(checked=0, findings=[])):
            status, report = self.report()
        self.assertEqual(status, 1)
        self.assertTrue(any("no input" in str(r["findings"]) for r in report["connections"]))

    def test_schema_only_depth_is_explicit_and_native_failure_cannot_approve(self):
        with mock.patch.object(interfaces, "_native", return_value=dict(checked=1, findings=[])):
            status, report = self.report()
        self.assertEqual(status, 0)
        self.assertIn("no geometry/render approval", report["connections"][0]["depth"])
        with mock.patch.object(interfaces, "_native", side_effect=ValueError("native crash")):
            self.assertEqual(self.report()[0], 1)

    def test_explicit_audit_subcommand_matches_the_shared_cli(self):
        with mock.patch.object(interfaces, "_native", return_value=dict(checked=1, findings=[])), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(interfaces.main(['audit', *self.args]), 0)

    def test_unknown_formgen_format_and_changed_native_contract_fail(self):
        with mock.patch.object(interfaces, "_native", return_value=dict(checked=1, findings=[])):
            write_json(self.roots["formgen"] / "claude/output/sheets/KG02.geo.json", dict(format="wrong", format_version=2))
            self.assertEqual(self.report()[0], 1)
            write_json(self.roots["ce"] / "Assets/Resources/Cubegen/cubegen_contract.json", dict(contract="changed", version=4))
            self.assertEqual(self.report()[0], 1)

    def test_imported_geometry_is_checked_without_generating_missing_producer_output(self):
        (self.roots["formgen"] / "claude/output/sheets/KG02.geo.json").unlink()
        write_json(self.roots["toolgen"] / "data/concepts/KG02.geo.json",
                   dict(format="cubeescape-design/geometry-spec", format_version=1))
        with mock.patch.object(interfaces, "_native", return_value=dict(checked=1, findings=[])):
            status, report = self.report(["--toolgen", str(self.roots["toolgen"])])
        self.assertEqual(status, 0)
        self.assertEqual(report["connections"][0]["checked"], 1)
        self.assertIn("consumer snapshots", str(report["connections"][0]["notes"]))
        self.assertFalse((self.roots["formgen"] / "claude/output/sheets/KG02.geo.json").exists())

    def test_missing_toolgen_concept_validators_report_unavailable_and_never_pass(self):
        root = self.roots["toolgen"]
        importer = root / "scripts/import_concepts.py"
        loader = root / "scripts/tool_assets/concept_spec.py"
        native = interfaces._native
        for available_importer, available_loader in ((False, False), (True, False), (False, True)):
            for path, available in ((importer, available_importer), (loader, available_loader)):
                path.unlink(missing_ok=True)
                if available:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("raise AssertionError('must not run incomplete validators')\n")
            with self.subTest(importer=available_importer, loader=available_loader), mock.patch.object(
                    interfaces, "_native", side_effect=lambda kind, path, options:
                    native(kind, path, options) if kind == "toolgen" else dict(checked=1, findings=[])):
                status, report = self.report(["--toolgen", str(root)])
                self.assertEqual(status, 1)
                result = next(r for r in report["connections"] if r["connection"].endswith("provenance"))
                self.assertEqual(result["checked"], 0)
                self.assertEqual(result["status"], "failed")
                self.assertIn("concept validation unavailable", str(result["findings"]))
                missing = loader if available_importer else importer
                self.assertIn(str(missing.relative_to(root)), str(result["findings"]))
                self.assertNotIn("FileNotFoundError", str(result["findings"]))

    def test_native_toolgen_consumer_checks_stay_read_only(self):
        root = self.roots["toolgen"]
        folder = root / "data/concepts"; folder.mkdir(parents=True)
        (root / "scripts/tool_assets").mkdir(parents=True)
        (root / "scripts/import_concepts.py").write_text(
            "import json\ndef check(folder): return []\n"
            "def read_provenance(folder): return json.load(open(folder / 'provenance.json'))\n")
        (root / "scripts/tool_assets/concept_spec.py").write_text(
            "import json\ndef load(name, where):\n"
            " d=json.load(open(where/name))\n"
            " if d.get('format_version') != 1: raise ValueError('unknown format')\n"
            " return d\n")
        write_json(folder / "provenance.json", dict(imported=dict(KG02=dict(file="KG02.geo.json"))))
        write_json(folder / "KG02.geo.json", dict(format_version=1))
        self.assertEqual(interfaces._native("toolgen", root, {})["checked"], 1)
        self.assertEqual(list(root.rglob("__pycache__")), [])
        write_json(folder / "KG02.geo.json", dict(format_version=2))
        with self.assertRaises(ValueError): interfaces._native("toolgen", root, {})

    def test_native_cubegen_isolated_and_editor_requires_fbx(self):
        root = self.roots["cubegen"]
        (root / "cubegen/__init__.py").write_text("")
        (root / "cubegen/contract.py").write_text(
            "import json\ndef read_sidecar(path): return json.load(open(path)), []\n"
            "def check_sidecar(doc, path, version): return ([] if version == 4 else ['wrong version']), []\n"
            "def check_export(path, version): return {'Test': {'errors': [], 'runtime': []}}\n")
        write_json(root / "output/Test.cubegen.json", dict(model="Test"))
        options = dict(package=str(root / "output"), version=4, editor=False)
        self.assertEqual(interfaces._native("cubegen", root, options)["findings"], [])
        options["editor"] = True
        self.assertTrue(interfaces._native("cubegen", root, options)["findings"])
        (root / "output/Test.fbx").write_text("fixture")
        self.assertEqual(interfaces._native("cubegen", root, options)["findings"], [])
        options["version"] = 1
        self.assertTrue(interfaces._native("cubegen", root, options)["findings"])
        self.assertEqual(list(root.rglob("__pycache__")), [])

    def test_native_motiongen_uses_measurements_not_a_model_export(self):
        root = self.roots["motiongen"]; (root / "tools").mkdir()
        (root / "tools/check_links.py").write_text(
            "def validate_manifest(value): return ([], set()) if isinstance(value, list) else (['not list'], set())\n"
            "def check_anchors(root): return []\n")
        write_json(root / "data/clips.json", [dict(measurement="ADS", clip="https://example.test", source="reference", timestamps="1s")])
        self.assertEqual(interfaces._native("motiongen", root, {})["findings"], [])
        write_json(root / "data/clips.json", [])
        self.assertTrue(interfaces._native("motiongen", root, {})["findings"])
        self.assertEqual(list(root.rglob("__pycache__")), [])


if __name__ == "__main__":
    unittest.main()
