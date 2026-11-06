"""0.36 Capstan: the hard core reaches a session through SessionStart, also without the global CLAUDE.md."""
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "plugins" / "ce-shared" / "hooks"
HOOK = HOOKS / "session.py"
COPY = HOOKS / "kern.md"
NEXOGEN = Path(os.environ.get("CE_HOME") or Path.home() / "Claude") / "cubeescape-nexogen"
SYNC = f"git -C {NEXOGEN} show origin/main:templates/kern.md > plugins/ce-shared/hooks/kern.md"

spec = importlib.util.spec_from_file_location("ce_session_kern", HOOK)
session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session)


def rules() -> list:
    return re.findall(r"^\d+\. .+$", COPY.read_text(encoding="utf-8"), re.MULTILINE)


class KernCopy(unittest.TestCase):
    def test_copy_has_the_ten_rules_of_the_core(self):
        found = rules()
        self.assertEqual([int(rule.split(".")[0]) for rule in found], list(range(1, 11)))
        self.assertIn("Kein Commit direkt auf `main`.", found[0])

    @unittest.skipUnless((NEXOGEN / ".git").exists(), "needs the Nexogen checkout: the Mac's ./ce check compares, CI has none")
    def test_copy_is_word_for_word_the_nexogen_source(self):
        source = subprocess.run(["git", "-C", str(NEXOGEN), "show", "origin/main:templates/kern.md"], capture_output=True, check=True).stdout
        self.assertEqual(COPY.read_bytes(), source, f"Kopie weicht von der Quelle ab, Abgleich: {SYNC}")


class KernAtSessionStart(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.repo = self.base / "repo"
        self.config = self.base / "config"
        self.repo.mkdir()
        self.config.mkdir()
        patch = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)})
        patch.start()
        self.addCleanup(patch.stop)

    def context(self) -> str:
        """The real hook as a process, as the client starts it."""
        env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(ROOT / "plugins" / "ce-shared")}
        done = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"hook_event_name": "SessionStart", "cwd": str(self.repo)}),
                              capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_without_global_rules_every_rule_comes_word_for_word_and_first(self):
        text = self.context()
        self.assertTrue(text.startswith(session.KERN_HEAD) or text.startswith("WARNUNG: ce-shared"), text[:80])
        for rule in rules():
            self.assertIn(rule, text)
        self.assertLess(text.index(rules()[-1]), text.index("Werkzeuge dieser Sitzung"))

    def test_block_stays_short(self):
        self.assertLess(len(session.kern_block(self.repo)), 1600)

    def test_loaded_global_rules_are_not_doubled(self):
        (self.config / "CLAUDE.md").write_text("# Globale Regeln\n" + "\n".join(rules()) + "\n", encoding="utf-8")
        text = self.context()
        self.assertNotIn(session.KERN_HEAD, text)
        self.assertNotIn(rules()[0], text)
        self.assertIn("Werkzeuge dieser Sitzung", text)

    def test_outdated_global_rules_do_not_count(self):
        changed = rules()
        changed[3] = "4. Merge nach `main` nur mit Ja des Nutzers."
        (self.config / "CLAUDE.md").write_text("\n".join(changed), encoding="utf-8")
        self.assertIn(rules()[3], self.context())

    def test_repo_file_and_its_import_count_as_loaded(self):
        (self.repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        (self.repo / "AGENTS.md").write_text("\n".join(rules()), encoding="utf-8")
        self.assertEqual(session.kern_block(self.repo), "")

    def test_a_file_the_session_does_not_load_does_not_count(self):
        (self.repo / "AGENTS.md").write_text("\n".join(rules()), encoding="utf-8")
        self.assertIn(rules()[0], session.kern_block(self.repo))

    def test_missing_copy_warns_and_keeps_the_context(self):
        with mock.patch.object(session, "KERN_FILE", self.base / "fehlt.md"), mock.patch.object(session, "version_hint", return_value=""):
            text = session.session_start({"cwd": str(self.repo)})["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(text.startswith(session.KERN_MISSING))
        self.assertIn("Werkzeuge dieser Sitzung", text)

    def test_a_failing_part_of_the_context_does_not_cost_the_core(self):
        """Judge 0.36: an unreadable docs/LESSONS.md left only a systemMessage, which the model does not see."""
        out = io.StringIO()
        stdin = io.StringIO(json.dumps({"hook_event_name": "SessionStart", "cwd": str(self.repo)}))
        with mock.patch.object(session, "lessons", side_effect=PermissionError("gesperrt")), mock.patch.object(session.sys, "stdin", stdin), \
             mock.patch.object(session, "version_hint", return_value=""), contextlib.redirect_stdout(out):
            self.assertEqual(session.main(), 0)
        result = json.loads(out.getvalue())
        self.assertIn("gesperrt", result["systemMessage"])
        self.assertIn(rules()[0], result["hookSpecificOutput"]["additionalContext"])

    def test_copy_without_rules_counts_as_missing(self):
        empty = self.base / "leer.md"
        empty.write_text("# nichts\n", encoding="utf-8")
        self.assertEqual(session.kern_block(self.repo, empty), session.KERN_MISSING)

    def test_broken_look_at_the_memory_shows_the_core(self):
        with mock.patch.object(session, "memory_text", side_effect=RuntimeError("kaputt")):
            self.assertIn(rules()[0], session.kern_block(self.repo))


if __name__ == "__main__":
    unittest.main()
