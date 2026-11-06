"""0.36 Capstan: ce_nightly.run_one on a checkout that is not clean main. Real git repos, the real ce_check run."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "ce-shared" / "tools"))
import ce_nightly  # noqa: E402

# the shape of Cube Escape's nightly.sh: origin/main in a worktree of its own, whatever the launching checkout looks like
OWN = '#!/bin/sh\ntouch "$(git rev-parse --git-common-dir)/ran"\nref=origin/main\ngit fetch -q origin main\ngit worktree add --detach "$TMP_TREE" "$ref" >/dev/null 2>&1\n'
# the shape of Toolgen's and Robogen's: it tests the checkout it lies in
IN_PLACE = '#!/bin/sh\ntouch "$(git rev-parse --git-common-dir)/ran"\ncd "$(dirname "$0")/.."\nsh scripts/check_output.sh\n'
ONLY_COMMENT = "#!/bin/sh\n# works on origin/main with git worktree add\n" + 'touch "$(git rev-parse --git-common-dir)/ran"\n'
SCRIPT = "scripts/nightly.sh"
FLAT = ["sh", SCRIPT, "run"]  # Cube Escape's form of check.nightly: one argv, not a list of argvs


def git(root, *argv):
    return subprocess.run(["git", *argv], cwd=root, capture_output=True, text=True, check=True).stdout


@unittest.skipIf(os.name == "nt", "the fixture scripts are POSIX shell; the Windows run has no nightly.sh of this shape")
class RunOneCheckout(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.lines = []

    def repo(self, script=OWN, nightly=None):
        origin = self.base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        root = self.base / "repo"
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        git(root, "config", "user.name", "t")
        git(root, "config", "user.email", "t@example.invalid")
        (root / "scripts").mkdir()
        if script is not None:
            (root / SCRIPT).write_text(script, encoding="utf-8")
        config = {"name": "t", "check": {"quick": ["true"], "nightly": FLAT if nightly is None else nightly}}
        (root / ".ce-repo.json").write_text(json.dumps(config), encoding="utf-8")
        (root / "a.txt").write_text("a\n", encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "-qm", "start")
        git(root, "remote", "add", "origin", str(origin))
        git(root, "push", "-q", "origin", "main")
        return root

    def run_one(self, root):
        with mock.patch.dict("os.environ", {"TMP_TREE": str(self.base / "tree")}):
            state, _ = ce_nightly.run_one(root, self.lines.append, False)
        return state

    def ran(self, root):
        return (root / ".git" / "ran").exists()

    def assert_stopped(self, root):
        self.assertEqual(self.run_one(root), "red (checkout)")
        self.assertFalse(self.ran(root), "fremder Arbeitsstand wurde ausgeführt")
        self.assertFalse(any(line.startswith("WARNUNG") for line in self.lines))

    def assert_warned(self, root, text):
        self.assertEqual(self.run_one(root), "green (Warnung)")
        self.assertTrue(self.ran(root))
        warning = [line for line in self.lines if line.startswith("WARNUNG")]
        self.assertEqual(len(warning), 1, self.lines)
        self.assertIn(text, warning[0])
        self.assertIn(SCRIPT, warning[0])

    def test_clean_main_runs_without_warning(self):
        root = self.repo()
        self.assertEqual(self.run_one(root), "green")
        self.assertTrue(self.ran(root))
        self.assertFalse(any(line.startswith("WARNUNG") for line in self.lines))

    def test_dirty_with_own_script_warns_and_runs(self):
        root = self.repo()
        (root / "a.txt").write_text("fremd\n", encoding="utf-8")
        (root / "neu.txt").write_text("fremd\n", encoding="utf-8")
        self.assert_warned(root, "2 geänderte Dateien")

    def test_wrong_branch_with_own_script_warns_and_runs(self):
        root = self.repo()
        git(root, "checkout", "-qb", "fremd")
        (root / "a.txt").write_text("fremd\n", encoding="utf-8")
        git(root, "commit", "-qam", "fremd")
        self.assert_warned(root, "Branch fremd")

    def test_detached_with_own_script_warns_and_runs(self):
        root = self.repo()
        git(root, "checkout", "-q", "--detach")
        self.assert_warned(root, "ohne Branch")

    def test_main_behind_origin_with_a_newer_script_warns_and_runs(self):
        """Cube Escape on 06.11.: main seven commits behind origin/main, one untracked file. Behind is not foreign work."""
        root = self.repo()
        git(root, "checkout", "-qb", "neuer")
        (root / SCRIPT).write_text(OWN + "# neuer\n", encoding="utf-8")
        git(root, "commit", "-qam", "neuer")
        git(root, "push", "-q", "origin", "neuer:main")
        git(root, "checkout", "-q", "main")
        git(root, "fetch", "-q", "origin")
        (root / "neu.txt").write_text("fremd\n", encoding="utf-8")
        self.assert_warned(root, "1 geänderte Dateien")

    def test_nested_form_of_check_nightly_counts_too(self):
        root = self.repo(nightly=[["sh", SCRIPT]])
        (root / "neu.txt").write_text("fremd\n", encoding="utf-8")
        self.assert_warned(root, "1 geänderte Dateien")

    def test_failing_run_stays_red_and_keeps_the_warning(self):
        root = self.repo(script=OWN + "exit 3\n")
        (root / "neu.txt").write_text("fremd\n", encoding="utf-8")
        state = self.run_one(root)
        self.assertTrue(state.startswith("red (exit") and state.endswith(ce_nightly.WARNED), state)

    def test_dirty_with_in_place_script_stops(self):
        root = self.repo(script=IN_PLACE)
        (root / "a.txt").write_text("fremd\n", encoding="utf-8")
        self.assert_stopped(root)

    def test_wrong_branch_with_in_place_script_stops(self):
        root = self.repo(script=IN_PLACE)
        git(root, "checkout", "-qb", "fremd")
        self.assert_stopped(root)

    def test_origin_main_only_in_a_comment_stops(self):
        root = self.repo(script=ONLY_COMMENT)
        (root / "a.txt").write_text("fremd\n", encoding="utf-8")
        self.assert_stopped(root)

    def test_direct_commands_as_in_formgen_stop(self):
        root = self.repo(nightly=[["sh", "-c", "touch .git/ran"], ["true"]])
        (root / "a.txt").write_text("fremd\n", encoding="utf-8")
        self.assert_stopped(root)

    def test_more_commands_beside_the_script_stop(self):
        root = self.repo(nightly=[["sh", SCRIPT], ["sh", "-c", "touch .git/ran"]])
        (root / "a.txt").write_text("fremd\n", encoding="utf-8")
        self.assert_stopped(root)

    def test_local_commit_on_main_changing_the_script_is_foreign_work(self):
        root = self.repo()
        (root / SCRIPT).write_text(OWN + "# fremd\n", encoding="utf-8")
        git(root, "commit", "-qam", "lokal, nie gepusht")
        (root / "neu.txt").write_text("fremd\n", encoding="utf-8")
        self.assert_stopped(root)

    def test_changed_script_is_foreign_work(self):
        root = self.repo()
        (root / SCRIPT).write_text(OWN + "# fremd\n", encoding="utf-8")
        self.assert_stopped(root)

    def test_script_changed_on_a_branch_is_foreign_work(self):
        root = self.repo()
        git(root, "checkout", "-qb", "fremd")
        (root / SCRIPT).write_text(OWN + "# fremd\n", encoding="utf-8")
        git(root, "commit", "-qam", "fremd")
        self.assert_stopped(root)

    def test_changed_config_is_foreign_work(self):
        root = self.repo()
        config = json.loads((root / ".ce-repo.json").read_text(encoding="utf-8"))
        config["check"]["nightly"] = ["sh", "-c", "touch .git/ran"]
        (root / ".ce-repo.json").write_text(json.dumps(config), encoding="utf-8")
        self.assert_stopped(root)

    def test_script_only_on_a_branch_stops(self):
        root = self.repo(script=None)
        git(root, "checkout", "-qb", "fremd")
        (root / SCRIPT).write_text(OWN, encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "-qm", "fremd")
        self.assert_stopped(root)

    def test_loader_entry_changed_is_foreign_work(self):
        root = self.repo()
        (root / "ce").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "-qm", "ce")
        git(root, "push", "-q", "origin", "main")
        self.assertEqual(ce_nightly.own_main_script(root, ["ce"]), SCRIPT)
        (root / "ce").write_text("#!/usr/bin/env python3\nprint('fremd')\n", encoding="utf-8")
        self.assertIsNone(ce_nightly.own_main_script(root, ["ce"]))

    def test_without_origin_main_it_stops(self):
        root = self.repo()
        git(root, "remote", "remove", "origin")
        (root / "a.txt").write_text("fremd\n", encoding="utf-8")
        self.assert_stopped(root)


class ReportOfTheNight(unittest.TestCase):
    def night(self, state):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ce_nightly, "LOG_DIR", Path(tmp) / "logs"), \
             mock.patch.object(ce_nightly, "has_nightly", return_value=True), mock.patch.object(ce_nightly, "tidy"), \
             mock.patch.object(ce_nightly, "run_one", return_value=(state, 60.0)), \
             mock.patch.object(ce_nightly, "notify") as notify, mock.patch("ce_search.build", side_effect=RuntimeError("aus")), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = ce_nightly.run(mock.Mock(repo=[str(Path(tmp) / "Spiel")], dry_run=False, report_issues=False))
        return code, notify.call_args.args[1], out.getvalue()

    def test_warning_is_in_report_and_notification_and_stays_green(self):
        code, text, report = self.night("green (Warnung)")
        self.assertEqual(code, 0)
        self.assertEqual(text, "alle grün; Checkout-Warnung: Spiel")
        self.assertIn("green (Warnung)", report)

    def test_red_with_warning_stays_red(self):
        code, text, _ = self.night("red (exit 3) (Warnung)")
        self.assertEqual(code, 1)
        self.assertEqual(text, "rot: Spiel; Checkout-Warnung: Spiel")

    def test_green_without_warning_is_unchanged(self):
        self.assertEqual(self.night("green")[:2], (0, "alle grün"))


if __name__ == "__main__":
    unittest.main()
