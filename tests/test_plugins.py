import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "ce-shared"
sys.path.insert(0, str(PLUGIN / "tools"))
import ce_plugins  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


KEY = "ce-shared@ce-agent-tools"


class Machine(unittest.TestCase):
    """A ~/.claude/plugins folder as the Mac had it on 22.10.2026: user row current, project rows of worktrees pinned old."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.folder = base / "plugins"
        self.cache = {v: self.folder / "cache" / "ce-agent-tools" / "ce-shared" / v for v in ("0.11.0", "0.13.0", "0.15.1")}
        for path in self.cache.values():
            path.mkdir(parents=True)
        self.main = base / "Cube Escape"; self.main.mkdir()
        self.worktree = base / "Cube Escape" / ".claude" / "worktrees" / "wt"; self.worktree.mkdir(parents=True)
        self.gone = base / "Cube Escape" / ".claude" / "worktrees" / "gone"
        self.rows = [
            self.row("project", "0.15.1", self.gone),
            self.row("user", "0.15.1"),
            self.row("project", "0.13.0", self.main),
            self.row("project", "0.11.0", self.worktree),
        ]
        self.write({"version": 2, "plugins": {KEY: self.rows, "other@elsewhere": [self.row("user", "1.0.0")]}})
        self.env = mock.patch.dict(os.environ, {"CE_PLUGINS_DIR": str(self.folder)})
        self.env.start(); self.addCleanup(self.env.stop)

    def row(self, scope, version, project=None):
        row = {"scope": scope, "version": version, "installPath": str(self.cache.get(version, "/nowhere")),
               "gitCommitSha": "sha-" + version, "lastUpdated": "t-" + version}
        if project is not None:
            row["projectPath"] = str(project)
        return row

    def write(self, data):
        (self.folder / "installed_plugins.json").write_text(json.dumps(data), encoding="utf-8")

    def data(self):
        return json.loads((self.folder / "installed_plugins.json").read_text(encoding="utf-8"))

    def market(self, version):
        manifest = self.folder / "marketplaces" / "ce-agent-tools" / ".claude-plugin" / "marketplace.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"plugins": [{"name": "ce-shared", "version": version}]}), encoding="utf-8")

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ce_plugins.main(list(argv))
        return code, out.getvalue()


class CheckAndFix(Machine):
    def test_check_reports_stale_and_orphan_rows(self):
        code, out = self.run_main("check")
        self.assertEqual(code, 1)
        self.assertIn(f"-> {KEY} project 0.13.0  {self.main}", out)
        self.assertIn(f"-> {KEY} project 0.11.0  {self.worktree}", out)
        self.assertIn(f"-- {KEY} project 0.15.1  {self.gone}", out)
        self.assertIn("3 Einträge weichen ab", out)

    def test_fix_without_apply_writes_nothing(self):
        before = (self.folder / "installed_plugins.json").read_text(encoding="utf-8")
        code, out = self.run_main("fix")
        self.assertEqual(code, 0)
        self.assertIn("Probelauf", out)
        self.assertEqual((self.folder / "installed_plugins.json").read_text(encoding="utf-8"), before)
        self.assertEqual(list(self.folder.glob("installed_plugins.json.bak-*")), [])

    def test_fix_apply_pins_stale_rows_to_the_newest_install_and_drops_orphans(self):
        code, out = self.run_main("fix", "--apply")
        self.assertEqual(code, 0, out)
        rows = self.data()["plugins"][KEY]
        self.assertEqual({r["version"] for r in rows}, {"0.15.1"})
        self.assertEqual(sorted(r.get("projectPath", "") for r in rows), ["", str(self.main), str(self.worktree)])
        for r in rows:
            self.assertEqual((r["installPath"], r["gitCommitSha"]), (str(self.cache["0.15.1"]), "sha-0.15.1"))
        self.assertEqual(self.data()["plugins"]["other@elsewhere"][0]["version"], "1.0.0")  # foreign marketplaces untouched
        self.assertEqual(len(list(self.folder.glob("installed_plugins.json.bak-*"))), 1)
        self.assertEqual(self.run_main("check")[0], 0)

    def test_orphan_holding_the_newest_files_still_serves_as_template(self):
        self.rows[1]["version"], self.rows[1]["installPath"] = "0.13.0", str(self.cache["0.13.0"])  # user row old, orphan newest
        self.write({"version": 2, "plugins": {KEY: self.rows}})
        self.run_main("fix", "--apply")
        rows = self.data()["plugins"][KEY]
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["version"] for r in rows}, {"0.15.1"})

    def test_user_only_drops_every_project_row(self):
        self.run_main("fix", "--apply", "--user-only")
        rows = self.data()["plugins"][KEY]
        self.assertEqual([r["scope"] for r in rows], ["user"])

    def test_user_only_keeps_project_rows_without_a_current_user_row(self):
        del self.rows[1]
        self.write({"version": 2, "plugins": {KEY: self.rows}})
        self.run_main("fix", "--apply", "--user-only")
        rows = self.data()["plugins"][KEY]
        self.assertEqual(sorted(r["scope"] for r in rows), ["project", "project"])
        self.assertEqual({r["version"] for r in rows}, {"0.15.1"})

    def test_rows_without_existing_files_are_never_a_template(self):
        self.rows.append(self.row("project", "0.16.0", self.main))  # 0.16.0 not in the cache
        self.write({"version": 2, "plugins": {KEY: self.rows}})
        self.run_main("fix", "--apply")
        self.assertEqual({r["version"] for r in self.data()["plugins"][KEY]}, {"0.15.1"})

    def test_marketplace_ahead_names_the_update_commands(self):
        self.market("0.16.0")
        code, out = self.run_main("check")
        self.assertEqual(code, 1)
        self.assertIn("Marketplace nennt 0.16.0", out)
        self.assertIn("claude plugin update ce-shared@ce-agent-tools", out)

    def test_missing_or_foreign_file_is_quiet(self):
        (self.folder / "installed_plugins.json").unlink()
        self.assertEqual(self.run_main("check")[0], 0)
        self.write({"version": 1, "plugins": {KEY: {"version": "0.1"}}})
        code, out = self.run_main("fix", "--apply")
        self.assertEqual(code, 0)
        self.assertIn("unbekanntes Format", out)

    def test_backup_holds_the_original(self):
        before = (self.folder / "installed_plugins.json").read_text(encoding="utf-8")
        self.run_main("fix", "--apply")
        backup, = self.folder.glob("installed_plugins.json.bak-*")
        self.assertEqual(backup.read_text(encoding="utf-8"), before)

    def test_foreign_entries_in_the_list_do_not_crash(self):
        self.rows.append("not a row")
        self.write({"version": 2, "plugins": {KEY: self.rows}})
        self.assertEqual(self.run_main("check")[0], 1)
        self.run_main("fix", "--apply", "--user-only")
        self.assertIn("not a row", self.data()["plugins"][KEY])

    def test_newer_row_without_files_is_broken_not_newest(self):
        self.rows.append(self.row("project", "0.17.0", self.main))  # 0.17.0 not in the cache
        self.write({"version": 2, "plugins": {KEY: self.rows}})
        code, out = self.run_main("check")
        self.assertIn(f"!> {KEY} project 0.17.0", out)
        self.assertIn("Dateien von 0.17.0 fehlen -> 0.15.1", out)
        self.assertEqual(ce_plugins.newest_known(self.folder), "0.15.1")

    def test_second_plugin_of_the_marketplace_does_not_count_for_ce_shared(self):
        other = self.folder / "cache" / "ce-agent-tools" / "other" / "1.2.0"; other.mkdir(parents=True)
        self.write({"version": 2, "plugins": {KEY: self.rows, "other@ce-agent-tools": [
            {"scope": "user", "version": "1.2.0", "installPath": str(other)}]}})
        self.assertEqual(ce_plugins.newest_known(self.folder), "0.15.1")

    def test_version_order_is_numeric(self):
        self.assertEqual(ce_plugins.version_key("0.16.0-rc1"), ce_plugins.version_key("0.16.0"))
        self.assertGreater(ce_plugins.version_key("0.16.0"), ce_plugins.version_key("0.15.10"))
        self.assertGreater(ce_plugins.version_key("0.15.10"), ce_plugins.version_key("0.15.9"))


class SessionStartHint(Machine):
    def plugin_root(self, version):
        root = Path(self.tmp.name) / ("plugin-" + version)
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}), encoding="utf-8")
        (root / "tools").symlink_to(PLUGIN / "tools")
        return root

    def setUp(self):
        super().setUp()
        self.session = load("ce_session_hook", PLUGIN / "hooks" / "session.py")

    def test_old_loaded_version_warns(self):
        hint = self.session.version_hint(self.plugin_root("0.13.0"), self.folder)
        self.assertIn("ce-shared 0.13.0 geladen", hint)
        self.assertIn("0.15.1", hint)

    def test_newer_marketplace_warns_even_when_installs_agree(self):
        self.market("0.16.0")
        self.assertIn("gibt es 0.16.0", self.session.version_hint(self.plugin_root("0.15.1"), self.folder))

    def test_current_version_is_quiet(self):
        self.assertEqual(self.session.version_hint(self.plugin_root("0.15.1"), self.folder), "")

    def test_session_start_shows_the_warning_to_user_and_model(self):
        with mock.patch.object(self.session, "version_hint", return_value="ce-shared 0.13.0 geladen"), \
                mock.patch.object(self.session, "run", return_value=""):
            result = self.session.session_start({"cwd": self.tmp.name})
        self.assertEqual(result["systemMessage"], "ce-shared 0.13.0 geladen")
        self.assertTrue(result["hookSpecificOutput"]["additionalContext"].startswith("WARNUNG: ce-shared 0.13.0"))

    def test_session_start_survives_a_broken_check(self):
        with mock.patch.object(self.session, "version_hint", side_effect=RuntimeError("kaputt")), \
                mock.patch.object(self.session, "run", return_value=""):
            result = self.session.session_start({"cwd": self.tmp.name})
        self.assertNotIn("systemMessage", result)
        self.assertIn("Werkzeuge dieser Sitzung", result["hookSpecificOutput"]["additionalContext"])

    def test_cloud_without_plugins_folder_is_quiet(self):
        empty = Path(self.tmp.name) / "none"
        self.assertEqual(self.session.version_hint(self.plugin_root("0.13.0"), empty), "")


if __name__ == "__main__":
    unittest.main()
