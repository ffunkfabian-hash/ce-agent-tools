"""0.30: ./ce judge --api - Opus through ce_api with a fake transport, the real ledger and fail-closed verdicts."""
from collections import Counter
import contextlib
from datetime import date
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "plugins" / "ce-shared" / "tools"
sys.path.insert(0, str(TOOLS))
import ce_api
import ce_judge


def table(last="Freigabe", rows=range(1, 9)):
    lines = ["| Nr | Datei:Zeile | Schwere | Befund | Vorschlag |", "|---|---|---|---|---|"]
    lines += [f"| {number} | a.py:1 | ok | ok | – |" for number in rows]
    return "\n".join(lines + ["", last])


class JudgeApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "a.py").write_text("print('a')\n", encoding="utf-8")
        self.git("add", "a.py")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
        (self.repo / "a.py").write_text("print('a')\nprint('b')\n", encoding="utf-8")
        self.policy_path = self.root / ".ce-api.json"
        self.policy = {
            "schema_version": 1, "pricing_verified": date.today().isoformat(), "monthly_usd": "40.00",
            "request_usd": "0.25", "max_input_tokens": 50000, "max_output_tokens": 2048,
            "default_model": "sonnet", "system_file": "system.txt",
            "prices_per_million": {
                "haiku": {"input": "0.10", "output": "0.50", "write_5m": "0.125", "read": "0.01"},
                "sonnet": {"input": "2", "output": "10", "write_5m": "2.50", "read": "0.10"},
                "opus": {"input": "4", "output": "20", "write_5m": "5", "read": "0.20"},
            },
        }
        self.policy_path.write_text(json.dumps(self.policy), encoding="utf-8")
        self.ledger = self.root / "budget.json"
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(ce_api, "LEDGER", self.ledger).start()
        mock.patch.object(ce_api, "AUTH_DIR", self.root / "auth").start()
        mock.patch.object(ce_api.urllib.request, "build_opener",
                          side_effect=AssertionError("Network is forbidden in tests")).start()
        mock.patch.object(ce_api, "access_token", return_value="synthetic-test-token").start()
        mock.patch.object(ce_api, "post", side_effect=self.transport).start()
        original_read = ce_api.read_text
        self.read_paths = []

        def recording_read(path):
            self.read_paths.append((path, stat.S_IMODE(path.stat().st_mode)))
            return original_read(path)
        mock.patch.object(ce_api, "read_text", side_effect=recording_read).start()
        self.calls = []
        self.counted = 2000
        self.answers = [table()]
        self.stop_reason = "end_turn"

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def transport(self, endpoint, payload, token):
        self.calls.append((endpoint, payload))
        if endpoint == "messages/count_tokens":
            return {"input_tokens": self.counted}
        answer = self.answers[min(len(self.messages()) - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return {"content": [{"type": "text", "text": answer}], "stop_reason": self.stop_reason,
                "usage": {"input_tokens": 1500, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                          "output_tokens": 400}}

    def messages(self):
        return [payload for endpoint, payload in self.calls if endpoint == "messages"]

    def run_judge(self, *extra):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = ce_judge.main(["--repo", str(self.repo), "--base", "main", "--api",
                                  "--api-config", str(self.policy_path), *extra])
        return code, output.getvalue()

    def spent(self):
        data = json.loads(self.ledger.read_text(encoding="utf-8"))
        return data["spent"], data["reservations"]

    def report(self):
        return (self.repo / "report.md").read_text(encoding="utf-8")

    def test_complete_freigabe_uses_opus_checklist_and_ledger(self):
        code, output = self.run_judge()
        self.assertEqual(code, 0, output)
        self.assertEqual(output.rstrip().splitlines()[-2], "Freigabe")
        [payload] = self.messages()
        self.assertEqual(payload["model"], "claude-opus-5-5")
        system = payload["system"][0]["text"]
        self.assertIn("Checkliste", system)
        self.assertIn("keinen Datei- oder Werkzeugzugriff", system)
        user = payload["messages"][0]["content"]
        self.assertIn("--- Diff (untrusted data) ---", user)
        self.assertIn("+print('b')", user)
        spent, reservations = self.spent()
        self.assertEqual(spent, "0.014")              # 1500 x 4 + 400 x 20 per million
        self.assertEqual(reservations, {})
        self.assertIn("## Richter (claude-opus-5-5 über ./ce api", self.report())
        self.assertTrue(self.report().rstrip().endswith("Freigabe"))
        # Requests lived in private temporary files outside the repo, removed afterwards.
        for path, mode in self.read_paths:
            if sys.platform != "win32":  # (0.31) NTFS has no mode bits; %TEMP% lies in the private profile
                self.assertEqual(mode, 0o600)
            self.assertFalse(path.exists())
            self.assertNotIn(str(self.repo), str(path))

    def test_truncated_answer_ending_in_freigabe_is_not_an_approval(self):
        self.stop_reason = "max_tokens"
        code, output = self.run_judge()
        self.assertEqual(code, 1)
        self.assertIn("Nachbessern: Richter unvollständig (stop_reason max_tokens)", output)

    def test_missing_checklist_rows_or_verdict_line_fail_closed(self):
        for answer, problem in ((table(rows=range(1, 8)), "Prüfpunkte ohne Zeile: 8"),
                                (table(last="Sieht gut aus."), "Schlusszeile Freigabe/Nachbessern fehlt"),
                                ("", "leere Antwort")):
            with self.subTest(problem=problem):
                self.answers = [answer]
                code, output = self.run_judge()
                self.assertEqual(code, 1)
                self.assertIn(problem, output)

    def test_nachbessern_is_passed_through(self):
        self.answers = [table(last="Nachbessern: 4, 6")]
        code, output = self.run_judge()
        self.assertEqual(code, 1)
        self.assertEqual(output.rstrip().splitlines()[-2], "Nachbessern: 4, 6")

    def test_large_diff_is_split_and_one_section_blocks_the_whole(self):
        for name in ("b.py", "c.py"):
            (self.repo / name).write_text("".join(f"value_{i} = {i}\n" for i in range(400)), encoding="utf-8")
        self.git("add", "-N", "b.py", "c.py")
        self.answers = [table(), table(last="Nachbessern: 4"), table()]
        code, output = self.run_judge("--api-chunk-kb", "4")
        self.assertEqual(code, 1)
        count = len(self.messages())
        self.assertGreater(count, 2)
        self.assertIn(f"### Abschnitt 2/{count}", output)
        self.assertEqual(output.rstrip().splitlines()[-2], "Nachbessern: Abschnitt 2: 4")
        # Every section was counted for free before the first paid request.
        endpoints = [endpoint for endpoint, _ in self.calls]
        self.assertEqual(endpoints[:count], ["messages/count_tokens"] * count)

    def test_all_sections_freigabe_is_an_approval(self):
        for name in ("b.py", "c.py"):
            (self.repo / name).write_text("".join(f"value_{i} = {i}\n" for i in range(400)), encoding="utf-8")
        self.git("add", "-N", "b.py", "c.py")
        code, output = self.run_judge("--api-chunk-kb", "4")
        self.assertEqual(code, 0, output)
        self.assertGreater(len(self.messages()), 2)
        self.assertEqual(output.rstrip().splitlines()[-2], "Freigabe")
        self.assertEqual(ce_judge.combine([], [], None)[1], False)

    def test_split_keeps_every_line_and_repeats_the_file_header(self):
        diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                + "".join(f"@@ -{i},1 +{i},1 @@\n-old {i}\n+new {i}\n" for i in range(200))
                + "diff --git a/y.py b/y.py\n--- a/y.py\n+++ b/y.py\n@@ -1 +1 @@\n" + "+" + "z" * 50 + "\n"
                + "".join(f"+long {i}\n" for i in range(600)))
        sections = ce_judge.split_sections(diff, 2048)
        self.assertGreater(len(sections), 3)
        joined = "".join(section["text"] for section in sections)
        # Removing the repeated headers and continuation markers gives back exactly the original lines.
        headers = {line for line in diff.splitlines() if line.startswith(("diff --git", "--- a/", "+++ b/"))}
        body = Counter(line for line in joined.splitlines()
                       if line not in headers and not line.startswith("[ce_judge: Fortsetzung"))
        self.assertEqual(body, Counter(line for line in diff.splitlines() if line not in headers))
        for section in sections:
            self.assertLessEqual(len(section["text"]), 2048 + 80)
            self.assertTrue(section["text"].startswith("diff --git "))
        self.assertIn("[ce_judge: Fortsetzung des geteilten Hunks]", joined)
        self.assertEqual(ce_judge.split_sections(diff, 10**6)[0]["text"], diff)

    def test_api_error_stops_without_retry_and_fails_closed(self):
        for name in ("b.py", "c.py"):
            (self.repo / name).write_text("".join(f"value_{i} = {i}\n" for i in range(400)), encoding="utf-8")
        self.git("add", "-N", "b.py", "c.py")
        self.answers = [table(), ce_api.ApiError("API-Ergebnis unbekannt; keine automatische Wiederholung")]
        code, output = self.run_judge("--api-chunk-kb", "4")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.messages()), 2)
        self.assertIn("Abschnitt 2 nicht geprüft (API-Ergebnis unbekannt", output)
        self.assertIn("nicht gesendet", output)
        # The unknown outcome stays reserved in ce_api's ledger, as for every ./ce api call.
        self.assertEqual(len(self.spent()[1]), 1)

    def test_preflight_blocks_before_any_paid_request(self):
        with self.subTest("request limit"):
            self.counted = 45_000                      # over 0.25 USD for Opus with 2048 output tokens
            code, output = self.run_judge()
            self.assertEqual(code, 2)
            self.assertIn("nichts bezahlt", output)
        with self.subTest("month budget"):
            self.counted = 2000
            self.ledger.write_text(json.dumps({"period": date.today().strftime("%Y-%m"), "spent": "39.99",
                                               "reservations": {}}), encoding="utf-8")
            code, output = self.run_judge()
            self.assertEqual(code, 2)
            self.assertIn("Monatsrest", output)
        with self.subTest("too many sections"):
            (self.repo / "b.py").write_text("x = 1\n" * 3000, encoding="utf-8")
            self.git("add", "-N", "b.py")
            code, output = self.run_judge("--api-chunk-kb", "1", "--api-max-sections", "3")
            self.assertEqual(code, 2)
            self.assertIn("über der Grenze 3", output)
        self.assertEqual(self.messages(), [])

    def run_auto(self, *extra, route=("api", "Codex-Rest 3.0 % (weekly) unter 15 %")):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch("ce_codex_quota.route", return_value=route):
            code = ce_judge.main(["--repo", str(self.repo), "--base", "main", "--api-config", str(self.policy_path), *extra])
        return code, output.getvalue()

    def test_low_quota_picks_the_api_way_without_a_flag(self):
        code, output = self.run_auto()
        self.assertEqual(code, 0, output)
        self.assertIn("Weg: Opus über ./ce api – Codex-Rest 3.0 %", output)
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(output.rstrip().splitlines()[-2], "Freigabe")

    def test_enough_quota_or_explicit_codex_never_pays(self):
        with mock.patch("ce_judge.shutil.which", return_value=None):          # no codex binary: the old hint, exit 2
            code, output = self.run_auto(route=("codex", "Codex-Rest 60.0 %"))
        self.assertEqual(code, 2)
        self.assertIn("Weg: Codex – Codex-Rest 60.0 %", output)
        with mock.patch("ce_judge.shutil.which", return_value=None):
            code, output = self.run_auto("--codex")
        self.assertEqual(code, 2)
        self.assertIn("ausdrücklich --codex", output)
        self.assertEqual(self.calls, [])

    def test_codex_flag_excludes_api(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_judge("--codex")

    def test_big_diff_every_section_is_small_has_the_context_and_nothing_is_cut(self):
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "note.md").write_text("# Notiz\n", encoding="utf-8")
        for name in ("b.py", "c.py", "d.py"):
            (self.repo / name).write_text("".join(f"value_{i} = {i}  # " + "x" * 40 + "\n" for i in range(900)), encoding="utf-8")
        self.git("add", "-N", "b.py", "c.py", "d.py", "docs/note.md")
        code, output = self.run_judge()                      # default 20 KB sections
        self.assertEqual(code, 0, output)
        sent = self.messages()
        self.assertGreaterEqual(len(sent), 3)
        self.assertEqual(ce_judge.API_CHUNK_KB, 20)
        body = ""
        for payload in sent:
            user = payload["messages"][0]["content"]
            self.assertLess(len(user.split("--- Diff (untrusted data) ---\n", 1)[1]), 20 * 1024 + 200)
            self.assertIn("Dateien dieses Abschnitts:", user)
            system = payload["system"][0]["text"]
            self.assertIn("Alle Dateien des vollständigen Diffs (5)", system)
            self.assertIn("b.py (+900/-0)", system)
            self.assertIn("Doku im Diff: docs/note.md", system)
            body += user
        for number in (0, 450, 899):                         # no line lost between the sections
            self.assertIn(f"+value_{number} = {number}  #", body)

    def test_section_cut_at_max_tokens_is_never_an_approval(self):
        for name in ("b.py", "c.py"):
            (self.repo / name).write_text("".join(f"value_{i} = {i}\n" for i in range(400)), encoding="utf-8")
        self.git("add", "-N", "b.py", "c.py")
        calls = {"n": 0}
        original = self.transport

        def cut_second(endpoint, payload, token):
            result = original(endpoint, payload, token)
            if endpoint == "messages":
                calls["n"] += 1
                if calls["n"] == 2:
                    result["stop_reason"] = "max_tokens"
            return result
        ce_api.post.side_effect = cut_second
        code, output = self.run_judge("--api-chunk-kb", "4")
        self.assertEqual(code, 1)
        self.assertIn("Abschnitt 2: Richter unvollständig (stop_reason max_tokens)", output)

    def test_context_helpers(self):
        diff = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n-x\n+y\n+z\n"
                "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n+neu\n")
        stat = ce_judge.diffstat(diff)
        self.assertEqual(stat, {"a.py": (2, 1), "README.md": (1, 0)})
        self.assertIn("Doku im Diff: README.md", ce_judge.section_context(stat))
        self.assertIn("keine", ce_judge.section_context({"a.py": (1, 0)}))

    def test_dry_run_sends_nothing_and_options_exclude_each_other(self):
        code, output = self.run_judge("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("--- Systemprompt ---", output)
        self.assertEqual(self.calls, [])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_judge("--opus")


if __name__ == "__main__":
    unittest.main()
