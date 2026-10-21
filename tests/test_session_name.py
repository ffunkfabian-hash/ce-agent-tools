import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "plugins" / "ce-shared" / "tools" / "ce_session_name.py"

spec = importlib.util.spec_from_file_location("session_name", Path(__file__).resolve().parents[1] / "plugins" / "ce-shared" / "tools" / "ce_session_name.py")
sn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sn)


class Names(unittest.TestCase):
    def test_abbreviations(self):
        self.assertEqual(sn.abbreviation("cubeescape"), "CE")
        self.assertEqual(sn.abbreviation("cubeescape-reference"), "REF")
        self.assertEqual(sn.abbreviation("cubeescape-rules"), "RULES")
        self.assertEqual(sn.abbreviation("cubeescape-new"), "NEW")

    def test_place_from_the_machine_not_the_prompt(self):
        self.assertEqual(sn.detect_place("Darwin", {}, "claude"), "Mac")
        self.assertEqual(sn.detect_place("Linux", {"CLAUDE_CODE_CONTAINER_ID": "x"}, "auto"), "Cloud")
        self.assertEqual(sn.detect_place("Darwin", {"CODEX_HOME": "x"}, "auto"), "Codex-Mac")
        self.assertEqual(sn.detect_place("Linux", {}, "codex"), "Codex-Cloud")
        self.assertEqual(sn.detect_place("Darwin", {"ANTIGRAVITY_AGENT": "1"}, "auto"), "Gemini-Mac")
        self.assertEqual(sn.branch_name("gemini", "NX", "1.20", "Crosslink"), "gemini/nx-1.20-crosslink")

    def test_next_version_takes_the_highest_branch(self):
        branches = ["main", "claude/ce-0.96-pinpoint", "release/0.94.0", "codex/ce-0.95-tracefall", "claude/cube-escape-0-87-gunsmith"]
        self.assertEqual(sn.next_version("0.93.0", branches), "0.97")
        self.assertEqual(sn.next_version("0.96", []), "0.97")
        self.assertEqual(sn.next_version("0.10.1", ["claude/ref-0.11.1-gegenpruefung"]), "0.12")

    def test_next_version_ignores_other_major(self):
        self.assertEqual(sn.next_version("0.5.2", ["release/1.9.0"]), "0.6")

    def test_config_version_reads_number_or_file_pattern(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "CHANGELOG.md").write_text("# CHANGELOG\n\n## claude-0.14\n- x\n\n## claude-0.13\n", encoding="utf-8")
            (repo / "version.py").write_text('VERSION = "0.17.2"\n', encoding="utf-8")
            self.assertEqual(sn.config_version({"version": "0.11.19"}, repo), "0.11.19")
            self.assertEqual(sn.config_version({"version": {"file": "CHANGELOG.md", "pattern": r"(?m)^## claude-(\d+\.\d+(?:\.\d+)?)"}}, repo), "0.14")
            self.assertEqual(sn.config_version({"version": {"file": "version.py", "pattern": r'VERSION\s*=\s*"([^"]+)"'}}, repo), "0.17.2")
            self.assertEqual(sn.config_version({"version": {"file": "missing.txt", "pattern": "x"}}, repo), "0.0")
            self.assertEqual(sn.config_version({}, repo), "0.0")

    def test_formats(self):
        self.assertEqual(sn.session_name("CE", "Mac", "0.96", "Pinpoint"), "CE 0.96 „Pinpoint“ · Mac")
        self.assertEqual(sn.session_name("REF", "Mac", task="Aufräumen"), "REF Aufräumen · Mac")
        self.assertEqual(sn.branch_name("claude", "CE", "0.96", "Pinpoint"), "claude/ce-0.96-pinpoint")
        self.assertEqual(sn.branch_name("codex", "REF", task="Bild Prüfung"), "codex/ref-bild-pruefung")


if __name__ == "__main__":
    unittest.main()


class ReserveTests(unittest.TestCase):
    """1.15: the branch on origin is the reservation."""

    def setUp(self):
        import subprocess, tempfile
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.bare = base / "origin.git"; self.repo = base / "work"
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        (self.repo / ".ce-repo.json").write_text('{"name": "cubeescape-toolgen", "version": "0.19"}')
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
        import os
        self.env = dict(os.environ, **env)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "init"], check=True, env=self.env)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", str(self.bare)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "push", "-q", "-u", "origin", "main"], check=True, env=self.env)

    def run_cli(self, *args):
        import subprocess, sys
        return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo), *args], capture_output=True, text=True, env=self.env)

    def test_reserve_pushes_the_branch_and_the_number_is_then_taken(self):
        first = self.run_cli("--name", "Nightshift", "--agent", "claude", "--reserve")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("Reserviert:   claude/tg-0.20-nightshift", first.stdout)
        self.assertIn("claude/tg-0.20-nightshift", sn.remote_branches(self.repo))
        second = self.run_cli("--name", "Other", "--agent", "claude")
        self.assertIn("0.21", second.stdout, "the pushed branch takes 0.20")
        again = self.run_cli("--name", "Nightshift", "--version", "0.20", "--agent", "claude", "--reserve")
        self.assertEqual(again.returncode, 1)
        self.assertIn("schon", again.stderr)

    def test_topics_lists_open_agent_branches(self):
        self.run_cli("--name", "Nightshift", "--agent", "claude", "--reserve")
        out = self.run_cli("--task", "Prüfung", "--topics").stdout
        self.assertIn("offen:        claude/tg-0.20-nightshift", out)
