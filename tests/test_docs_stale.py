"""0.32 (Panorama 2c): ./ce docs warns about patterns that go stale - warning only, never a failing exit code."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "plugins" / "ce-shared" / "tools"
sys.path.insert(0, str(TOOLS))
import ce_docs


class StaleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / ".ce-repo.json").write_text(json.dumps({"name": "demo", "docs": {}}), encoding="utf-8")

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def warnings(self, config=None):
        return ce_docs.stale(self.root, config or {})

    def messages(self, text, name="AGENTS.md", config=None):
        self.write(name, text)
        return [w["warning"] for w in self.warnings(config)]

    def test_each_pattern_hits(self):
        cases = {
            "Regeln liegen in `cubeescape-rules`.": "cubeescape-nexogen",
            "Die Sperre `heavy-run.lock` schützt alles.": "heavy-run.lock",
            "Werkzeuge sind eingebettet in jedem Repo.": "Lader",
            "Wir nutzen Agent Tools 0.12 hier.": "Agent Tools 0.12",
            "Stand: ce-agent-tools 0.5.0": "ce-agent-tools 0.5.0",
            "Plugin ce-shared 0.15.1 genügt": "ce-shared 0.15.1",
            "Agent-Tools 0.29": "Agent-Tools 0.29",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                found = self.messages(text)
                self.assertEqual(len(found), 1, found)
                self.assertIn(expected, found[0])

    def test_current_history_and_since_mentions_do_not_hit(self):
        for text in ("Agent Tools 0.30 ist der Stand.", "Agent Tools 0.31.2", "Ab Agent Tools 0.26 gilt das.", "seit Agent Tools 0.13",
                     "Nur Agent Tools 1.0 und Agent Tools 0.100", "früher `cubeescape-rules`", "Ehemals heavy-run.lock",
                     "die alte gemeinsame Datei heavy-run.lock", "abgelöst: eingebettet in jedem Repo",
                     "Agent Tools 0.12 <!-- ce-docs: ok -->", "ce-docs: ok Agent Tools 0.12", "Agent Tools Version 0.12 (kein Muster)",
                     "eingebettet im Repo", "cubeescape-nexogen und rules"):
            with self.subTest(text=text):
                self.assertEqual(self.messages(text), [])

    def test_code_fences_and_history_files_are_not_scanned(self):
        self.write("README.md", "```\ncubeescape-rules\nheavy-run.lock\n```\n")
        self.write("CHANGELOG.md", "Agent Tools 0.12 mit `cubeescape-rules`\n")
        self.write("docs/versions/0.5.md", "heavy-run.lock\n")
        self.write("docs/pruefung-2026-11.md", "cubeescape-rules\n")
        self.write("docs/aktuell.md", "cubeescape-rules\n")
        self.assertEqual([(w["path"], w["line"]) for w in self.warnings()], [("docs/aktuell.md", 1)])

    def test_configuration_threshold_and_ignore(self):
        self.write("AGENTS.md", "Agent Tools 0.12 und Agent Tools 0.35\n")
        self.assertEqual(len(self.warnings()), 1)
        config = {"docs": {"stale": {"agent_tools_below": 40}}}
        self.assertEqual(len(self.warnings(config)), 2)
        self.assertEqual(self.warnings({"docs": {"stale": {"ignore": ["AGENTS.md"]}}}), [])
        self.assertEqual(len(self.warnings({"docs": {"stale": "kaputt"}})), 1)     # a wrong type is ignored, not fatal

    def test_line_numbers_and_several_findings_in_one_line(self):
        self.write("AGENTS.md", "a\n\nAgent Tools 0.12, cubeescape-rules, heavy-run.lock\n")
        found = self.warnings()
        self.assertEqual({w["line"] for w in found}, {3})
        self.assertEqual(len(found), 3)

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def test_default_branch_must_match_origin_head(self):
        self.git("init", "-q", "-b", "main")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
        self.write("AGENTS.md", "Standardbranch `claude/basic-first-light`, nicht `main`.\n")
        self.assertEqual(self.warnings(), [], "origin/HEAD unknown: no claim, no warning")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        found = self.warnings()
        self.assertEqual(len(found), 1)
        self.assertIn("origin/HEAD) ist `main`", found[0]["warning"])
        self.write("AGENTS.md", "Standardbranch `main`.\n")
        self.assertEqual(self.warnings(), [])

    def test_main_prints_warnings_but_keeps_exit_0_and_json(self):
        self.write("AGENTS.md", "Siehe cubeescape-rules.\n")
        report = self.root / "out.json"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = ce_docs.main(["--repo", str(self.root), "--json", str(report)])
        self.assertEqual(code, 0)
        self.assertIn("Warnung: AGENTS.md:1:", output.getvalue())
        self.assertIn("1 Warnungen (nur Hinweis)", output.getvalue())
        self.assertEqual(len(json.loads(report.read_text(encoding="utf-8"))["warnings"]), 1)
        self.write("AGENTS.md", "Siehe [weg](fehlt.md) und cubeescape-rules.\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ce_docs.main(["--repo", str(self.root)]), 1)         # real findings still fail

    def test_a_broken_scan_never_breaks_the_check(self):
        self.write("AGENTS.md", "ok\n")
        output = io.StringIO()
        original = ce_docs.stale
        ce_docs.stale = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaputt"))
        try:
            with contextlib.redirect_stdout(output):
                code = ce_docs.main(["--repo", str(self.root)])
        finally:
            ce_docs.stale = original
        self.assertEqual(code, 0)
        self.assertIn("Veralterungsprüfung übersprungen", output.getvalue())


if __name__ == "__main__":
    unittest.main()
