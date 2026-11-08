"""Risk-based model selection for the Codex judge."""
import json
import contextlib
import io
from pathlib import Path
import subprocess
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
            "plugins/ce-shared/tools/ce_api.py",
            "plugins/ce-shared/tools/ce_api_budget.py",
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


class BaseCorePathsRoutingTests(unittest.TestCase):
    """0.43.2: core_paths of .ce-repo.json at the base count as core paths (incident 0.44: ce_merge_check.py got luna)."""

    CORE = ["plugins/ce-shared/tools/ce_merge_check.py", "plugins/ce-shared/rules/*.md", "src/core/**", "docs/guarded/"]

    def git(self, *args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                              cwd=self.root, check=True, capture_output=True, text=True).stdout

    def make_repo(self, config):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.git("init", "-q", "-b", "main")
        if config is not None:
            text = config if isinstance(config, str) else json.dumps(config)
            (self.root / ".ce-repo.json").write_text(text, encoding="utf-8")
        (self.root / "README.md").write_text("x\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        return self.root

    def route(self, path, diff="+small change", base="main"):
        return ce_judge._route(diff, [path], self.root, base)

    def test_listed_core_paths_route_to_sol(self):
        self.make_repo({"core_paths": self.CORE})
        for path in ("plugins/ce-shared/tools/ce_merge_check.py", "plugins/ce-shared/rules/commit-on-main.md",
                     "src/core/deep/inner/loader.py", "docs/guarded/save.md"):
            with self.subTest(path=path):
                model, reason = self.route(path)
                self.assertEqual(model, "sol")
                self.assertIn("core_paths der Basis main", reason)
                self.assertIn(path, reason)
                self.assertEqual(ce_judge.choose_model("+small", [path], None, self.root, "main"), "sol")

    def test_paths_outside_core_paths_stay_luna(self):
        self.make_repo({"core_paths": self.CORE})
        for path in ("plugins/ce-shared/tools/ce_session_name.py",   # same folder, not listed
                     "plugins/ce-shared/rules/sub/extra.md",          # `*` stays inside the folder
                     "plugins/ce-shared/tools/ce_merge_check.py.bak",  # exact name, not a prefix
                     "src/corelib/a.py", "docs/guarded.md", "README.md"):
            with self.subTest(path=path):
                model, reason = self.route(path)
                self.assertEqual(model, "luna")
                self.assertIn("kein erkannter Kern-/Vertragspfad", reason)

    def test_base_decides_not_the_pull_request(self):
        self.make_repo({"core_paths": self.CORE})
        self.git("checkout", "-q", "-b", "feature")
        (self.root / ".ce-repo.json").write_text(json.dumps({"core_paths": ["other/*"]}), encoding="utf-8")
        self.git("commit", "-q", "-am", "drop core path")
        self.assertEqual(self.route("plugins/ce-shared/tools/ce_merge_check.py")[0], "sol")
        self.assertEqual(self.route("other/x.py")[0], "luna")  # added only by the PR: not a core path of the base

    def test_rename_out_of_core_path_counts(self):
        self.make_repo({"core_paths": self.CORE})
        diff = ("diff --git a/src/core/a.py b/src/a.py\nsimilarity index 100%\n"
                "rename from src/core/a.py\nrename to src/a.py\n")
        self.assertEqual(self.route("src/a.py", diff)[0], "sol")

    def test_git_quoted_paths_are_decoded(self):
        self.make_repo({"core_paths": self.CORE})
        diff = ('diff --git "a/src/core/\\303\\244.py" b/src/a.py\nsimilarity index 100%\n'
                'rename from "src/core/\\303\\244.py"\nrename to src/a.py\n')
        self.assertEqual(self.route("src/a.py", diff)[0], "sol")
        self.assertEqual(self.route('"src/core/\\303\\244.py"')[0], "sol")   # --name-only quotes the same way
        self.assertEqual(self.route('"src/\\303\\244.py"')[0], "luna")
        # real git: a new non-ASCII file under a core path, end to end through diff_of
        self.git("checkout", "-q", "-b", "feature")
        (self.root / "src" / "core").mkdir(parents=True)
        (self.root / "src" / "core" / "\u00e4.py").write_text("x = 1\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "umlaut")
        (self.root / "src" / "core" / "[x].py").write_text("y = 2\n", encoding="utf-8")  # glob characters stay literal
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "brackets")
        diff, names = ce_judge.diff_of("main", None, self.root)
        self.assertEqual(sorted(names), ["src/core/[x].py", "src/core/\u00e4.py"])  # unquoted, so the pathspec finds them
        self.assertIn("+x = 1", diff)
        self.assertIn("+y = 2", diff)
        model, reason = ce_judge._route(diff, names, self.root, "main")
        self.assertEqual(model, "sol")
        self.assertIn("core_paths", reason)

    def test_no_config_or_no_core_paths_keeps_old_routing(self):
        for config in (None, {"name": "x"}, {"core_paths": []}):
            with self.subTest(config=config):
                self.make_repo(config)
                self.assertEqual(self.route("plugins/ce-shared/tools/ce_merge_check.py")[0], "luna")
                self.assertEqual(self.route("Assets/Scripts/Core/SaveSystem.cs")[0], "sol")  # Unity rules stay
        self.assertEqual(self.route("src/core/a.py", base="no-such-ref")[0], "luna")
        self.assertEqual(ce_judge._route("+small", ["src/core/a.py"], self.root)[0], "luna")  # no base given

    def test_unreadable_core_paths_route_up(self):
        for config in ("{not json", json.dumps({"core_paths": "src/core/**"}), json.dumps({"core_paths": None}),
                       json.dumps(["src/core/**"])):
            with self.subTest(config=config):
                self.make_repo(config)
                model, reason = self.route("README.md")
                self.assertEqual(model, "sol")
                self.assertIn("nicht auswertbar", reason)

    def test_merge_check_not_loadable_routes_up(self):
        self.make_repo({"core_paths": self.CORE})
        with mock.patch.dict(sys.modules, {"ce_merge_check": None}):
            model, reason = self.route("README.md")
        self.assertEqual(model, "sol")
        self.assertIn("nicht auswertbar", reason)

    def test_main_names_core_path_for_the_incident(self):
        self.make_repo({"core_paths": self.CORE})
        self.git("checkout", "-q", "-b", "feature")
        target = self.root / "plugins" / "ce-shared" / "tools" / "ce_merge_check.py"
        target.parent.mkdir(parents=True)
        target.write_text("x = 1\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "touch core")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(ce_judge.main(["--repo", str(self.root), "--base", "main", "--codex", "--dry-run"]), 0)
        self.assertIn("Richter sol", output.getvalue())
        self.assertIn("core_paths der Basis main: plugins/ce-shared/tools/ce_merge_check.py", output.getvalue())


if __name__ == "__main__":
    unittest.main()
