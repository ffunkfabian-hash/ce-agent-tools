"""Risk-based model selection for the Codex judge."""
import json
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "plugins" / "ce-shared" / "tools"
sys.path.insert(0, str(TOOLS))
import ce_judge


class JudgeRoutingTests(unittest.TestCase):
    def make_repo(self, systems):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "Tools").mkdir()
        (root / "Tools" / "test_map.json").write_text(json.dumps({"systems": systems}), encoding="utf-8")
        return root

    def test_core_and_premerge_paths_come_from_test_map(self):
        root = self.make_repo([
            {"name": "player", "core": True, "paths": ["Assets/Scripts/Player/PlayerController.cs"]},
            {"name": "hud", "core": "merge", "paths": ["Assets/Scripts/Hud/*.cs"]},
        ])
        for path in ("Assets/Scripts/Player/PlayerController.cs", "Assets/Scripts/Hud/HudRules.cs"):
            with self.subTest(path=path):
                self.assertEqual(ce_judge.choose_model("+small change", [path], None, root), "sol")
                self.assertIn("Tools/test_map.json", ce_judge._route("+small change", [path], root)[1])
        self.assertEqual(ce_judge.choose_model("+small change", ["Assets/Scripts/Items/SmallFeature.cs"], None, root), "luna")
        self.assertEqual(ce_judge._route("+small change", ["Assets/Tests/EditMode/SmallFeatureTests.cs"], root)[0], "luna")

    def test_offline_core_fallback_covers_save_and_settings_gap(self):
        for path in ("Assets/Scripts/Core/SaveSystem.cs", "Assets/Scripts/Core/SettingsStore.cs"):
            with self.subTest(path=path):
                self.assertEqual(ce_judge.choose_model("+small change", [path], None), "sol")

    def test_risk_categories_route_to_sol(self):
        root = self.make_repo([])
        for path in (
            "schemas/level.v3.json",
            "Packages/manifest.json",
            "Packages/packages-lock.json",
            "ProjectSettings/ProjectSettings.asset",
            "plugins/ce-shared/tools/heavy_lock.py",
            "plugins/ce-shared/tools/ce_judge.py",
            "plugins/ce-shared/tools/ce_check.py",
            "plugins/ce-shared/tools/ce_sync.py",
            "plugins/ce-shared/hooks/guard.py",
            "Tools/testplan.py",
            ".agents/skills/ce-testing/scripts/verify.sh",
        ):
            with self.subTest(path=path):
                self.assertEqual(ce_judge.choose_model("+small change", [path], None, root), "sol")

    def test_small_routine_docs_and_feature_tests_stay_luna(self):
        root = self.make_repo([
            {"name": "core", "core": True, "paths": ["Assets/Scripts/Core/SaveSystem.cs"]},
        ])
        for path in ("docs/guide.md", "Assets/Tests/EditMode/WorkbenchPaletteTests.cs"):
            with self.subTest(path=path):
                self.assertEqual(ce_judge.choose_model("+typo fix", [path], None, root), "luna")

    def test_new_renamed_and_deleted_core_paths_route_high(self):
        cases = (
            ("Assets/Scripts/Core/NewLoader.cs", "new file mode 100644\n+class NewLoader {}"),
            ("Assets/Scripts/Core/OldLoader.cs", "deleted file mode 100644\n-class OldLoader {}"),
            ("Assets/Scripts/Core/RenamedLoader.cs", "similarity index 90%\nrename from Assets/Scripts/Core/OldLoader.cs\nrename to Assets/Scripts/Core/RenamedLoader.cs\n+change"),
        )
        root = self.make_repo([])
        for path, diff in cases:
            with self.subTest(path=path):
                self.assertEqual(ce_judge.choose_model(diff, [path], None, root), "sol")

    def test_malformed_metadata_does_not_make_script_changes_look_safe(self):
        root = self.make_repo([])
        (root / "Tools" / "test_map.json").write_text('{"systems": [null]}', encoding="utf-8")
        model, reason = ce_judge._route("+small change", ["Assets/Scripts/UnknownCore.cs"], root)
        self.assertEqual(model, "sol")
        self.assertIn("ungültig", reason)
        self.assertEqual(ce_judge.choose_model("+typo", ["docs/guide.md"], None, root), "luna")

    def test_explicit_override_wins_without_erasing_automatic_risk(self):
        self.assertEqual(ce_judge.choose_model("+small", ["Assets/Scripts/Core/SaveSystem.cs"], "luna"), "luna")
        model, reason = ce_judge._route("+small", ["Assets/Scripts/Core/SaveSystem.cs"])
        self.assertEqual(model, "sol")
        self.assertIn("Offline-Fallback", reason)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            ce_judge, "git", side_effect=lambda *args, **kwargs: "abc" if args[0] == "rev-parse" else ""
        ), mock.patch.object(
            ce_judge, "diff_of", return_value=("+small", ["Assets/Scripts/Core/SaveSystem.cs"])
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(ce_judge.main(["--repo", tmp, "--base", "main", "--model", "luna", "--dry-run"]), 0)
        self.assertIn("Richter luna", output.getvalue())
        self.assertIn("automatisch wäre sol", output.getvalue())

    def test_existing_diff_size_threshold_remains(self):
        self.assertEqual(ce_judge.choose_model("+x\n" * 299, ["src/a.py"], None), "luna")
        model, reason = ce_judge._route("+x\n" * 300, ["src/a.py"])
        self.assertEqual(model, "sol")
        self.assertIn("Schwelle 300", reason)


if __name__ == "__main__":
    unittest.main()
