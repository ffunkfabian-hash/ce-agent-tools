import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "plugins" / "ce-shared" / "tools"
sys.path.insert(0, str(TOOLS))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, TOOLS / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pf = load("ce_postfach", "ce_postfach.py")
am = load("await_mail", "ce_await_mail.py")
SESSION = TOOLS / "ce_session_name.py"

# 0.43.1: the tests never ask GitHub which issue is the Postfach
os.environ["CE_POSTFACH_CACHE"] = str(Path(tempfile.mkdtemp()) / "postfach.json")
Path(os.environ["CE_POSTFACH_CACHE"]).write_text(json.dumps({"at": __import__('time').time(), "numbers": [16]}), encoding="utf-8")


class Fake:
    """Stands in for subprocess.run: records the gh call and answers like GitHub."""

    def __init__(self, returncode=0, stdout='{"id": 7, "html_url": "https://github.com/x/y/issues/16#issuecomment-7"}', stderr=""):
        self.calls = []
        self.result = subprocess.CompletedProcess([], returncode, stdout, stderr)

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return self.result


class Names(unittest.TestCase):
    def test_sender_from_branch(self):
        self.assertEqual(pf.sender_from_branch("claude/tg-0.23-livery", "TG", "Mac"), "TG 0.23 „Livery“ · Mac")
        self.assertEqual(pf.sender_from_branch("codex/ce-agent-tools-0.14-cross-talk", "CE-AGENT-TOOLS", "Codex-Mac"),
                         "CE-AGENT-TOOLS 0.14 „Cross Talk“ · Codex-Mac")
        self.assertEqual(pf.sender_from_branch("claude/formgen-patch-stuetzgriff", "DS", "Mac"), "DS Patch stuetzgriff · Mac")
        for branch in ("main", "release/0.20.0", "claude/beautiful-davinci-6v3i1z", ""):
            self.assertEqual(pf.sender_from_branch(branch, "TG", "Mac"), "", branch)

    def test_the_watcher_wakes_for_a_derived_name(self):
        name = pf.sender_from_branch("claude/tg-0.23-black-ice", "TG", "Mac")
        body = pf.format_message("Sightline · Cloud", "TG 0.23 „Black Ice“ · Mac", "Freigabe", "Opus 5.5, high")
        self.assertTrue(am.addressed(body, am.session_key(name)))
        patch = pf.sender_from_branch("claude/formgen-patch-stuetzgriff", "DS", "Mac")
        self.assertTrue(am.addressed(pf.format_message("Sightline", patch, "Rückfrage"), am.session_key(patch)))


class Spelling(unittest.TestCase):
    """Richter 0.14, Befund 2: the name derived from a branch slug must reach the watcher of the reserved spelling."""

    def test_hyphen_umlaut_and_case_are_the_same_session(self):
        for reserved, branch in (("Cross-Talk", "claude/tg-0.23-cross-talk"), ("Über", "claude/tg-0.23-ueber"), ("Black Ice", "claude/tg-0.23-black-ice")):
            derived = pf.sender_from_branch(branch, "TG", "Mac")
            key = am.session_key(f"TG 0.23 „{reserved}“ · Mac")
            self.assertTrue(am.addressed(pf.format_message("Sightline", derived, "x"), key), reserved)
            self.assertFalse(am.addressed(pf.format_message(derived, "alle", "x"), key), f"own broadcast woke {reserved}")
            found = pf.sessions([{"id": 1, "created_at": "a", "body": pf.announcement(f"TG 0.23 „{reserved}“ · Mac", branch, "b", "o/r")},
                                 {"id": 2, "created_at": "b", "body": pf.format_message(derived, "Sightline", "später")}])
            self.assertEqual((len(found), found[0]["last"]), (1, "später"), reserved)

    def test_unversioned_names_match_on_word_boundaries(self):
        laser, seeder = am.session_key("CE Patch laser · Mac"), am.session_key("CE Patch laser-seeder · Mac")
        self.assertFalse(am.addressed("Sightline → CE Patch laser-seeder: x", laser))
        self.assertTrue(am.addressed("Sightline → CE Patch laser · Mac: x", laser))
        self.assertTrue(am.addressed("Sightline → CE Patch laser: x", laser))
        noted = am.session_key("REF Aufräumen (Abnahme) · Mac")
        self.assertTrue(am.addressed("Sightline → REF Aufräumen (Abnahme): x", noted))
        self.assertTrue(am.addressed("Sightline → REF Aufräumen: x", noted))
        self.assertTrue(am.addressed("Sightline → REF Aufräumen (Abnahme): x", am.session_key("REF Aufräumen · Mac")))
        self.assertTrue(am.addressed("CE Patch laser-seeder · Mac → alle: x", laser))
        self.assertFalse(am.addressed("CE Patch laser-seeder · Mac → alle: x", seeder))


class Format(unittest.TestCase):
    def test_head_model_and_lines(self):
        body = pf.format_message("TG 0.23 „Livery“ · Mac", "Sightline · Cloud", "Frage am Bild", "Opus 5.5, high", ["a", "", "b"])
        self.assertEqual(body, "TG 0.23 „Livery“ · Mac → Sightline: Frage am Bild\nOpus 5.5, high\na\nb\n")
        self.assertIn("nicht angegeben", pf.format_message("A", "B", "x").splitlines()[1])

    def test_limits(self):
        with self.assertRaises(ValueError):
            pf.format_message("A", "B", "x", lines=[str(n) for n in range(11)])
        with self.assertRaises(ValueError):
            pf.format_message("A", "B", "zwei\nZeilen")
        with self.assertRaises(ValueError):
            pf.format_message("A", "B", " ")

    def test_announcement_names_branch_and_how_to_reach(self):
        body = pf.announcement("TG 0.23 „Livery“ · Mac", "claude/tg-0.23-livery", "4b0f599", "ffunkfabian-hash/cubeescape-toolgen",
                               "Gruppe 1", "Opus 5.5, high")
        self.assertTrue(body.startswith("TG 0.23 „Livery“ · Mac → Sightline: angemeldet\nOpus 5.5, high\n"))
        self.assertIn("Branch `claude/tg-0.23-livery` (Basis `4b0f599`)", body)
        self.assertIn('--name "TG 0.23 „Livery“"', body)
        self.assertTrue(am.addressed(body, am.session_key("Sightline · Cloud")))


class Broadcast(unittest.TestCase):
    def test_alle_reaches_everybody_but_the_sender(self):
        body = pf.format_message("TG 0.23 „Livery“ · Mac", "alle", "Blender-Sperre frei")
        self.assertTrue(am.addressed(body, am.session_key("CE 0.112 „Gridline“ · Mac")))
        self.assertTrue(am.addressed(body, am.session_key("Sightline · Cloud")))
        self.assertFalse(am.addressed(body, am.session_key("TG 0.23 „Livery“ · Mac")))
        self.assertFalse(am.addressed("Sightline → Nutzer: an alle ist nichts", am.session_key("CE 0.112 „Gridline“ · Mac")))

    def test_only_the_head_line_broadcasts(self):
        key = am.session_key("CE 0.112 „Gridline“ · Mac")
        quoted = "TG 0.23 „Livery“ · Mac → Sightline: Release\nm\nDer Wächter weckt bei `→ alle:` jede Sitzung\nalt: A → alle: y\n"
        self.assertFalse(am.addressed(quoted, key))
        self.assertFalse(am.addressed(" → alle: ohne Absender", key))


class Sending(unittest.TestCase):
    def repo(self, origin):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        path = Path(tmp.name)
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
        return path

    def test_posts_with_gh_to_the_postfach(self):
        fake, out = Fake(), io.StringIO()
        code = pf.send("A → B: x\nm\n", self.repo("https://github.com/ffunkfabian-hash/cubeescape-toolgen"), runner=fake, out=out)
        self.assertEqual(code, 0)
        command, kwargs = fake.calls[0]
        self.assertEqual(command[:5], ["gh", "api", "-X", "POST", "repos/ffunkfabian-hash/cubeescape-nexogen/issues/16/comments"])
        self.assertEqual(json.loads(kwargs["input"]), {"body": "A → B: x\nm\n"})
        self.assertIn("Kommentar 7", out.getvalue())

    def test_never_posts_without_github_origin_or_when_switched_off(self):
        fake, err = Fake(), io.StringIO()
        self.assertEqual(pf.send("A → B: x\n", self.repo("/tmp/origin.git"), runner=fake, err=err), 1)
        self.assertIn("Text zum Einfügen", err.getvalue())
        os.environ["CE_MAILBOX"] = "0"
        try:
            self.assertEqual(pf.send("A → B: x\n", self.repo("https://github.com/a/b"), runner=fake, err=io.StringIO()), 1)
        finally:
            del os.environ["CE_MAILBOX"]
        self.assertEqual(fake.calls, [])

    def test_only_github_urls_count_as_github(self):
        fake = Fake()
        for origin in ("/home/u/src/github.com/a/b.git", "file:///x/github.com/b.git"):
            self.assertEqual(pf.send("A → B: x\n", self.repo(origin), runner=fake, err=io.StringIO()), 1, origin)
        self.assertEqual(fake.calls, [])
        for origin in ("https://github.com/a/b", "https://user@github.com/a/b.git", "git@github.com:a/b.git", "ssh://git@github.com/a/b"):
            self.assertTrue(pf.GITHUB_ORIGIN.match(origin), origin)

    def test_timeout_warns_about_a_duplicate(self):
        def slow(*_args, **_kwargs):
            raise subprocess.TimeoutExpired("gh", 60)
        err = io.StringIO()
        self.assertEqual(pf.send("A → B: x\n", self.repo("https://github.com/a/b"), runner=slow, err=err), 1)
        self.assertIn("prüfen, ob der Kommentar schon steht", err.getvalue())

    def test_failed_post_prints_the_text(self):
        err = io.StringIO()
        code = pf.send("A → B: x\n", self.repo("git@github.com:a/b.git"), runner=Fake(1, "", "HTTP 403"), err=err)
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403", err.getvalue())
        self.assertIn("A → B: x", err.getvalue())

    def test_dry_run_only_prints(self):
        fake, out = Fake(), io.StringIO()
        self.assertEqual(pf.send("A → B: x\n", Path("."), dry_run=True, runner=fake, out=out), 0)
        self.assertEqual((out.getvalue(), fake.calls), ("A → B: x\n", []))


class Who(unittest.TestCase):
    def test_lists_announced_sessions_with_their_latest_message(self):
        comments = [
            {"id": 1, "created_at": "2026-10-22T15:00:00Z", "body": pf.announcement("TG 0.23 „Livery“ · Mac", "claude/tg-0.23-livery", "abc", "o/toolgen")},
            {"id": 2, "created_at": "2026-10-22T15:10:00Z", "body": "Sightline · Cloud → TG 0.23 „Livery“: Freigabe\nm\n"},
            {"id": 3, "created_at": "2026-10-22T15:20:00Z", "body": "TG 0.23 „Livery“ · Mac → Sightline: Frage am Bild\nm\n"},
            {"id": 4, "created_at": "2026-10-22T15:05:00Z", "body": pf.announcement("DS Patch stuetzgriff · Mac", "claude/formgen-patch-stuetzgriff", "def", "o/formgen")},
            {"id": 5, "created_at": "2026-10-22T14:00:00Z", "body": "freier Text ohne Kopf"},
        ]
        found = pf.sessions(comments)
        self.assertEqual([s["sender"] for s in found], ["TG 0.23 „Livery“ · Mac", "DS Patch stuetzgriff · Mac"])
        self.assertEqual(found[0]["last"], "Frage am Bild")
        self.assertIn("claude/tg-0.23-livery", found[0]["detail"])


class ReserveAnnounces(unittest.TestCase):
    """--reserve stays a reservation; the announcement never posts from a repository without a GitHub origin (like this test)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.bare, self.repo = base / "origin.git", base / "work"
        self.env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        self.env.pop("CE_MAILBOX", None)  # the local origin alone must keep it silent
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        (self.repo / ".ce-repo.json").write_text('{"name": "cubeescape-formgen", "version": "0.15"}')
        for command in (["add", "."], ["commit", "-q", "-m", "init"], ["remote", "add", "origin", str(self.bare)], ["push", "-q", "-u", "origin", "main"]):
            subprocess.run(["git", "-C", str(self.repo), *command], check=True, env=self.env)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SESSION), "--repo", str(self.repo), "--agent", "claude", *args],
                              capture_output=True, text=True, encoding="utf-8", env=self.env)

    def test_patch_reserves_and_offers_the_announcement_text(self):
        result = self.run_cli("--reserve", "--patch", "Stützgriff", "--about", "KG03/KG08 Stützhand")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Reserviert:   claude/formgen-patch-stuetzgriff", result.stdout)
        self.assertIn("Patch stuetzgriff · ", result.stdout)
        self.assertIn("→ Sightline: angemeldet", result.stderr)
        self.assertIn("Auftrag: KG03/KG08 Stützhand", result.stderr)

    def test_no_announce_stays_silent_and_a_taken_branch_announces_nothing(self):
        first = self.run_cli("--reserve", "--name", "Crosstalk", "--no-announce")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotIn("angemeldet", first.stdout + first.stderr)
        again = self.run_cli("--reserve", "--name", "Crosstalk", "--version", "0.16")
        self.assertEqual(again.returncode, 1)
        self.assertNotIn("angemeldet", again.stdout + again.stderr)

    def test_reserve_through_the_runpy_wrapper_keeps_exit_0(self):
        """Richter 0.14, Befund 1: Nexogen's session_name.py runs the tool with runpy from another folder."""
        wrapper = Path(self.tmp.name) / "wrapper.py"
        wrapper.write_text("import runpy, sys\nsys.argv[0] = sys.argv[1]\ndel sys.argv[1]\nrunpy.run_path(sys.argv[0], run_name='__main__')\n")
        result = subprocess.run([sys.executable, str(wrapper), str(SESSION), "--repo", str(self.repo), "--agent", "claude", "--name", "Probe", "--reserve"],
                                capture_output=True, text=True, encoding="utf-8", env=self.env, cwd=self.tmp.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("angemeldet", result.stderr)

    def test_a_broken_announcement_keeps_the_reservation(self):
        broken = Path(self.tmp.name) / "tools"
        broken.mkdir()
        (broken / "ce_session_name.py").write_text(SESSION.read_text(encoding="utf-8"), encoding="utf-8")
        (broken / "ce_postfach.py").write_text("raise RuntimeError('kaputt')\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(broken / "ce_session_name.py"), "--repo", str(self.repo), "--agent", "claude", "--name", "Probe", "--reserve"],
                                capture_output=True, text=True, encoding="utf-8", env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Anmeldung nicht gesendet", result.stderr)
        self.assertIn("Reservierung gilt", result.stderr)
        heads = subprocess.run(["git", "ls-remote", "--heads", str(self.bare)], capture_output=True, text=True, encoding="utf-8").stdout
        self.assertIn("claude/fg-0.16-probe", heads)

    def test_say_sends_under_the_reserved_spelling(self):
        self.run_cli("--reserve", "--name", "Cross-Talk", "--no-announce")
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "claude/fg-0.16-cross-talk"], check=True)
        out = subprocess.run([sys.executable, str(TOOLS / "ce_postfach.py"), "say", "--repo", str(self.repo), "--dry-run", "Thema"],
                             capture_output=True, text=True, encoding="utf-8", env=self.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith("FG 0.16 „Cross-Talk“ · "), out.stdout)

    def test_say_uses_the_reservation_on_a_designated_branch(self):
        """0.15.1: a cloud session works on claude/<designated>, not on the reserved branch – the sender name still comes from it."""
        self.run_cli("--reserve", "--patch", "probe-wakeline", "--no-announce")
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "-b", "claude/beautiful-davinci-6v3i1z"], check=True)
        out = subprocess.run([sys.executable, str(TOOLS / "ce_postfach.py"), "say", "--repo", str(self.repo), "--dry-run", "Thema"],
                             capture_output=True, text=True, encoding="utf-8", env=self.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith("FG Patch probe-wakeline · "), out.stdout)

    def test_patch_word(self):
        sn = load("session_name", "ce_session_name.py")
        self.assertEqual([sn.patch_word(n) for n in ("cube-escape", "cubeescape", "cubeescape-nexogen", "toolgen", "ce-agent-tools")],
                         ["cube-escape", "cube-escape", "nexogen", "toolgen", "ce-agent-tools"])


class SayDashlineTests(unittest.TestCase):
    """0.17.0 Dashline: lines starting with '-' (e.g. '---', '- Punkt', '-1 °C') are text lines, not options."""

    def run_say(self, *args, dry_run=True):
        cmd = [sys.executable, str(TOOLS / "ce_postfach.py"), "say", "--name", "Tester · Mac"]
        if dry_run:
            cmd.append("--dry-run")
        cmd.extend(args)
        env = dict(os.environ, CE_MAILBOX="0")
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)

    def test_triple_dash_as_line(self):
        res = self.run_say("Thema", "---")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("\n---\n", res.stdout)

    def test_dash_punkt_as_line(self):
        res = self.run_say("Thema", "- Punkt")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("\n- Punkt\n", res.stdout)
        res_dash_b = self.run_say("Thema", "-b")
        self.assertEqual(res_dash_b.returncode, 0, res_dash_b.stderr)
        self.assertIn("\n-b\n", res_dash_b.stdout)

    def test_mixed_options_and_dash_line(self):
        # Options allowed before and after lines; dry-run after lines as specified
        res = subprocess.run([sys.executable, str(TOOLS / "ce_postfach.py"), "say", "--name", "Tester · Mac",
                              "T", "a", "- b", "--to", "alle", "--dry-run"],
                             capture_output=True, text=True, encoding="utf-8", env=dict(os.environ, CE_MAILBOX="0"))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Tester · Mac → alle: T\n", res.stdout)
        self.assertIn("\na\n- b\n", res.stdout)

    def test_double_dash_before_lines(self):
        res = self.run_say("Thema", "--", "---")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("\n---\n", res.stdout)

    def test_unknown_option_remains_line_not_option(self):
        res = self.run_say("Thema", "--xyz")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("\n--xyz\n", res.stdout)

    def test_eleventh_line_rejected_as_before(self):
        eleven = [f"line {i}" for i in range(1, 12)]
        res = self.run_say("Thema", *eleven)
        self.assertEqual(res.returncode, 2)
        self.assertIn("höchstens 10 Zeilen", res.stderr)


if __name__ == "__main__":
    unittest.main()



class SyncCompleteness(unittest.TestCase):
    """Richter 0.14, Befund 8: every tool `ce` calls and every tool a synced tool imports travels with ce_sync."""

    def test_ce_entry_and_imports_are_synced(self):
        import re
        ce_sync = load("ce_sync", "ce_sync.py")
        synced = set(ce_sync.SYNC_FILES)
        called = set(re.findall(r"tools/(\w+\.py)", (TOOLS / "ce").read_text(encoding="utf-8")))
        self.assertEqual(called - synced, set(), "ce calls tools that ce_sync does not copy")
        local = {path.name for path in TOOLS.glob("*.py")}
        for name in sorted(synced & local):
            imports = re.findall(r"^\s*import (ce_\w+|heavy_lock)\b", (TOOLS / name).read_text(encoding="utf-8"), re.M)
            missing = {f"{module}.py" for module in imports} - synced
            self.assertEqual(missing, set(), f"{name} imports tools that ce_sync does not copy")


class Entry(unittest.TestCase):
    def test_reserve_accepts_name_with_and_without_flag(self):
        """`./ce session reserve X` and `./ce session reserve --name X` (Antigravity's spelling) give the same call."""
        fake_tools = Path(tempfile.mkdtemp()); self.addCleanup(lambda: __import__("shutil").rmtree(fake_tools))
        (fake_tools / "ce").write_text((TOOLS / "ce").read_text(encoding="utf-8"), encoding="utf-8")
        (fake_tools / "ce_check.py").write_text("")
        (fake_tools / "ce_session_name.py").write_text("import sys\nprint(sys.argv[1:])\n")
        for args in (["session", "reserve", "Crosstalk"], ["session", "reserve", "--name", "Crosstalk"], ["reserve", "--name", "Crosstalk"]):
            out = subprocess.run(["sh", str(fake_tools / "ce"), *args], capture_output=True, text=True, encoding="utf-8").stdout.strip()
            self.assertEqual(out, "['--name', 'Crosstalk', '--reserve']", args)


class MonthlyPostfach(unittest.TestCase):
    """0.43.1: the Postfach is the newest open issue with label `postfach`; reading covers it and the one before."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name) / "sub" / "postfach.json"
        self.now = 1_000_000.0

    def ask(self, *issues, returncode=0, runner=None):
        fake = runner or Fake(returncode=returncode, stdout=json.dumps(list(issues)), stderr="boom")
        numbers = am.mailboxes(runner=fake, cache=self.cache, now=lambda: self.now)
        return numbers, fake

    @staticmethod
    def issue(number, state="open", **extra):
        return dict(number=number, state=state, **extra)

    def test_label_found_gives_the_newest_open_issue_and_the_one_before(self):
        numbers, fake = self.ask(self.issue(30), self.issue(25, "closed"), self.issue(20, "closed"))
        self.assertEqual(numbers, [30, 25])
        self.assertIn("labels=postfach", fake.calls[0][0][2])

    def test_a_closed_newer_issue_and_a_pull_request_are_not_the_postfach(self):
        numbers, _ = self.ask(self.issue(31, "closed"), self.issue(30, pull_request={}), self.issue(25), self.issue(20, "closed"))
        self.assertEqual(numbers, [25, 20])

    def test_the_first_labelled_issue_reads_the_archive_16_before_it(self):
        self.assertEqual(self.ask(self.issue(30))[0], [30, 16])

    def test_no_issue_with_the_label_is_16(self):
        self.assertEqual(self.ask()[0], [16])

    def test_gh_failure_uses_the_cache_else_16(self):
        self.assertEqual(self.ask(returncode=1)[0], [16])
        self.cache.write_text(json.dumps({"at": self.now - 7200, "numbers": [30, 25]}), encoding="utf-8")
        self.assertEqual(self.ask(returncode=1)[0], [30, 25])
        self.cache.write_text(json.dumps({"at": self.now - 7200, "numbers": [30, 25]}), encoding="utf-8")
        self.assertEqual(am.mailboxes(runner=Fake(returncode=1), cache=self.cache, now=lambda: self.now), [30, 25])

    def test_gh_missing_uses_the_cache(self):
        def missing(*args, **kwargs):
            raise FileNotFoundError("gh")
        self.cache.parent.mkdir(parents=True); self.cache.write_text(json.dumps({"at": 0, "numbers": [30, 25]}), encoding="utf-8")
        self.assertEqual(self.ask(runner=missing)[0], [30, 25])

    def test_the_answer_is_kept_for_an_hour(self):
        _, first = self.ask(self.issue(30))
        self.now += 3000
        numbers, second = self.ask(self.issue(99))
        self.assertEqual((numbers, second.calls), ([30, 16], []))
        self.now += 700
        self.assertEqual(self.ask(self.issue(99))[0], [99, 16])

    def test_a_failed_ask_is_repeated_after_five_minutes_not_per_call(self):
        self.ask(returncode=1)
        self.now += 100
        self.assertEqual(self.ask(returncode=1)[1].calls, [])
        self.now += 250
        self.assertEqual(len(self.ask(returncode=1)[1].calls), 1)

    def test_offline_never_asks_gh(self):
        fake = Fake()
        self.assertEqual(am.mailboxes(runner=fake, cache=self.cache, now=lambda: self.now, offline=True), [16])
        self.cache.parent.mkdir(parents=True); self.cache.write_text(json.dumps({"at": 0, "numbers": [30, 25]}), encoding="utf-8")
        self.assertEqual(am.mailboxes(runner=fake, cache=self.cache, now=lambda: self.now, offline=True), [30, 25])
        self.assertEqual(fake.calls, [])

    def test_no_home_directory_means_no_cache_not_a_crash(self):
        def no_home():
            raise RuntimeError("Could not determine home directory")
        original = am.mailbox_cache
        am.mailbox_cache = no_home
        self.addCleanup(setattr, am, "mailbox_cache", original)
        self.assertEqual(am.mailboxes(runner=Fake(stdout="[]"), now=lambda: self.now), [16])
        self.assertEqual(am.mailboxes(runner=Fake(stdout=json.dumps([self.issue(30)])), now=lambda: self.now), [30, 16])

    def test_other_repos_keep_16(self):
        self.assertEqual(am.mailboxes("o/other", runner=Fake(), cache=self.cache), [16])

    def use_cache(self, numbers):
        """pf has its own copy of ce_await_mail: patch that one, and point it at a fresh cache"""
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text(json.dumps({"at": self.now, "numbers": numbers}), encoding="utf-8")
        before = os.environ["CE_POSTFACH_CACHE"]
        os.environ["CE_POSTFACH_CACHE"] = str(self.cache)
        self.addCleanup(os.environ.__setitem__, "CE_POSTFACH_CACHE", before)
        original = pf.am.time.time
        pf.am.time.time = lambda: self.now
        self.addCleanup(setattr, pf.am.time, "time", original)
        # 0.44.2: a real gh call would reach the live Postfach (on a signed-in Mac #83): fail loudly instead
        def no_real_gh(command, **kwargs):
            raise AssertionError(f"echter Aufruf im Test: {command}")
        original_run = pf.am.subprocess.run
        pf.am.subprocess.run = no_real_gh
        self.addCleanup(setattr, pf.am.subprocess, "run", original_run)

    def test_patched_clock_and_runner_are_seen_per_call(self):
        """0.44.2: `now=time.time` as default was bound once, use_cache() then patched nothing and the Mac asked GitHub"""
        self.use_cache([30, 25])
        self.assertEqual(am.mailboxes(), [30, 25])  # fresh by the patched clock: no gh call (no_real_gh would raise)
        self.now += 4000
        fake = Fake(stdout=json.dumps([self.issue(41), self.issue(30)]))
        pf.am.subprocess.run = fake
        self.assertEqual(am.mailboxes(cache=self.cache), [41, 30])
        self.assertEqual(len(fake.calls), 1)

    def test_say_writes_only_to_the_current_issue(self):
        self.use_cache([30, 25])
        fake = Fake()
        pf.post("A → B: x\n", runner=fake)
        self.assertEqual(fake.calls[0][0][4], "repos/ffunkfabian-hash/cubeescape-nexogen/issues/30/comments")

    def test_reading_covers_both_issues_without_double_messages_and_issue_overrides(self):
        pages = {30: [{"id": 5, "created_at": "b"}, {"id": 9, "created_at": "d"}], 25: [{"id": 3, "created_at": "a"}, {"id": 5, "created_at": "b"}]}
        asked = []
        original = pf.am.fetch_page
        pf.am.fetch_page = lambda repo, number, since: asked.append(number) or pages[number]
        self.addCleanup(setattr, pf.am, "fetch_page", original)
        self.use_cache([30, 25])
        self.assertEqual([c["id"] for c in pf.fetch("s")], [3, 5, 9])  # both issues, id order, id 5 once
        self.assertEqual(asked, [30, 25])
        asked.clear()
        self.assertEqual([c["id"] for c in pf.fetch("s", issue=25)], [3, 5])  # an explicit issue overrides
        self.assertEqual(asked, [25])
