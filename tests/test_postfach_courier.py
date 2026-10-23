"""0.21 „Courier“: Postfach reliability – each class from the evidence table in docs/versions/0.21.md has its counter-test here."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time as _time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "ce-shared"
TOOLS = PLUGIN / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(PLUGIN / "hooks"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


am = load("ce_await_mail", TOOLS / "ce_await_mail.py")
pf = load("ce_postfach", TOOLS / "ce_postfach.py")
watch_hook = load("postfach_watch", PLUGIN / "hooks" / "postfach_watch.py")
inbox = load("postfach_inbox", PLUGIN / "hooks" / "postfach_inbox.py")
SESSION = TOOLS / "ce_session_name.py"
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
ENV.pop("CE_MAILBOX", None)
NAME = "CE-AGENT-TOOLS 0.21 „Courier“ · Mac"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=ENV).stdout.strip()


def comment(number, body, minutes_ago=1):
    created = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() - minutes_ago * 60))
    return {"id": number, "created_at": created, "body": body, "pr": 16}


class Origin(unittest.TestCase):
    """A bare origin with main and a clone `work`, like a worktree that a session reserves in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.bare, self.repo = base / "origin.git", base / "work"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        (self.repo / ".ce-repo.json").write_text('{"name": "ce-agent-tools", "version": "0.20"}')
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "remote", "add", "origin", str(self.bare))
        git(self.repo, "push", "-q", "-u", "origin", "main")

    def advance_origin(self) -> str:
        """Another session merges into main: origin moves on, `work` does not know yet."""
        other = Path(self.tmp.name) / "other"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(other)], check=True, env=ENV)
        git(other, "commit", "-q", "--allow-empty", "-m", "merged elsewhere")
        git(other, "push", "-q", "origin", "main")
        return git(other, "rev-parse", "HEAD")

    def reserve(self, *args):
        return subprocess.run([sys.executable, str(SESSION), "--repo", str(self.repo), "--agent", "claude", "--reserve", *args],
                              capture_output=True, text=True, env=ENV)

    def say(self, *args):
        return subprocess.run([sys.executable, str(TOOLS / "ce_postfach.py"), "say", "--repo", str(self.repo), "--dry-run", *args],
                              capture_output=True, text=True, env=ENV)

    def remote_sha(self, branch):
        return git(self.bare, "rev-parse", f"refs/heads/{branch}")


class ModelLine(Origin):
    """Klasse 1: 6368742151, 6369788806, 6370037962 – „Modell und Aufwand: nicht angegeben“, then a second model line by hand."""

    def test_say_takes_the_model_of_the_reservation(self):
        self.assertEqual(self.reserve("--name", "Courier", "--model", "Opus 5.5, high", "--no-announce").returncode, 0)
        out = self.say("PRs offen")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines()[1], "Opus 5.5, high")

    def test_a_handwritten_model_line_is_the_model_line(self):
        body = pf.format_message("X", "Sightline", "Thema", "", ["Modell: Opus 5.5, high", "Inhalt"])
        self.assertEqual(body.splitlines()[1:], ["Modell: Opus 5.5, high", "Inhalt"])
        self.assertNotIn("nicht angegeben", body)

    def test_reserve_without_model_says_so(self):
        result = self.reserve("--patch", "kg11-augenabstand", "--no-announce")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--model fehlt", result.stderr)


class FreshBase(Origin):
    """Klasse 2: 6369967340 (Loupe on 9c0dab7a, main 53a80410), 6368620553 (Prismwake on b5c74fd, main b5a3a3c)."""

    def test_a_worktree_behind_main_reserves_on_the_fresh_tip(self):
        tip = self.advance_origin()
        result = self.reserve("--name", "Courier")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_sha("claude/ce-agent-tools-0.21-courier"), tip)
        self.assertIn(f"Basis `{tip[:8]}`", result.stderr)  # the announcement names the real base
        self.assertIn("vor der Arbeit wechseln", result.stderr)

    def test_own_commits_stay_and_are_named(self):
        self.advance_origin()
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "own work")
        head = git(self.repo, "rev-parse", "HEAD")
        result = self.reserve("--name", "Courier", "--no-announce")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_sha("claude/ce-agent-tools-0.21-courier"), head)
        self.assertIn("1 eigene Commits und liegt 1 hinter origin/main", result.stderr)

    def test_without_origin_main_it_keeps_head_and_says_so(self):
        git(self.repo, "remote", "set-url", "origin", str(Path(self.tmp.name) / "missing.git"))
        git(self.repo, "remote", "set-url", "--push", "origin", str(self.bare))
        result = self.reserve("--name", "Courier", "--no-announce")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nicht geholt", result.stderr)

    def test_a_new_name_in_a_reused_worktree_drops_the_old_pending_message(self):
        """Courier found Lodestar's ce-watch.json in the same worktree."""
        self.reserve("--name", "Lodestar", "--version", "0.20", "--no-announce")
        folder = Path(git(self.repo, "rev-parse", "--absolute-git-dir"))
        (folder / "ce-watch.json").write_text(json.dumps({"after": 5, "pending": 9, "pending_ids": [9, 10], "pending_since": "x",
                                                          "deliveries": 1, "hinted": 10, "reminded_at": 1, "last_failure": 1}))
        (folder / "ce-inbox.json").write_text(json.dumps({"name": "CE-AGENT-TOOLS 0.20 „Lodestar“ · Mac", "messages": [{"id": 9}]}))
        self.reserve("--name", "Courier", "--no-announce")
        self.assertEqual(json.loads((folder / "ce-watch.json").read_text()), {"after": 5})
        self.assertFalse((folder / "ce-inbox.json").exists())


class Recipients(Origin):
    """Klasse 3: 6368327242 (PR #19 only to „Plugin-Analyse“), 6344404291 (`→ A, B:` reached neither)."""

    def test_a_list_of_recipients_reaches_each(self):
        body = "Sightline → Formgen-Patch „Stützgriff“, TG 0.24 „Salvo“: Entscheidungen\nx"
        self.assertTrue(am.addressed(body, am.session_key("TG 0.24 „Salvo“ · Mac")))
        self.assertTrue(am.addressed(body, am.session_key("Formgen-Patch „Stützgriff“")))
        self.assertFalse(am.addressed(body, am.session_key("TG 0.23 „Livery“ · Mac")))

    def test_a_pr_message_also_goes_to_the_coordinator(self):
        out = self.say("--name", "CE-AGENT-TOOLS 0.18 „Sweepline“ · Mac", "--to", "Plugin-Analyse Nacharbeiten", "Übergabepunkt 0.18",
                       "0.18 ist als PR #19 offen")
        self.assertEqual(out.returncode, 0, out.stderr)
        head = out.stdout.splitlines()[0]
        self.assertEqual(head, "CE-AGENT-TOOLS 0.18 „Sweepline“ · Mac → Plugin-Analyse Nacharbeiten, Sightline: Übergabepunkt 0.18")
        self.assertTrue(am.addressed(out.stdout, "sightline"))
        self.assertTrue(am.addressed(out.stdout, am.session_key("Plugin-Analyse Nacharbeiten")))

    def test_no_copy_without_pr_with_no_cc_or_from_the_coordinator(self):
        for args in (["--to", "TG 0.24 „Salvo“", "Frage", "Griff links?"],
                     ["--to", "TG 0.24 „Salvo“", "--no-cc", "PR #8", "offen"],
                     ["--name", "Sightline · Cloud", "--to", "TG 0.24 „Salvo“", "PR #8 gemergt"]):
            out = self.say(*(args if "--name" in args else ["--name", "X 0.1 „Y“ · Mac", *args]))
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertNotIn(", Sightline", out.stdout.splitlines()[0], args)

    def test_several_to_options(self):
        out = self.say("--name", "X 0.1 „Y“ · Mac", "--to", "A 0.1 „B“", "--to", "C 0.2 „D“", "Thema")
        self.assertTrue(out.stdout.startswith("X 0.1 „Y“ · Mac → A 0.1 „B“, C 0.2 „D“: Thema"), out.stdout)

    def test_the_coordinator_watcher_can_hear_every_pr_message(self):
        sweepline = "CE-AGENT-TOOLS 0.18 „Sweepline“ · Mac → Plugin-Analyse Nacharbeiten: Übergabepunkt 0.18\nOpus\nPR #19 offen"
        self.assertFalse(am.wanted(sweepline, "sightline"))
        self.assertTrue(am.wanted(sweepline, "sightline", also_pr=True))
        self.assertFalse(am.wanted("Sightline → TG 0.24 „Salvo“: PR #8 gemergt", "sightline", also_pr=True), "own message")
        self.assertFalse(am.wanted("TG 0.24 „Salvo“ · Mac → Sightline-Probe X: Frage\nnichts", "sightline", also_pr=True))


RELEASE = """**Sightline → CE-AGENT-TOOLS 0.21 „Courier“: Freigabe Commit/PR**
Opus 5.5, Cloud
Freigabe per Post (ce-freigabe 1)
Ziel: CE-AGENT-TOOLS 0.21 „Courier“
Schritte: commit, push, pr
Zitat des Nutzers: „Per Post“
Zeit (UTC): 2026-10-23T17:37Z
"""


class Release(Origin):
    """Ziel 3 (Sightline 6370961598) and Klasse 4 (6367029652 acted on by Sweepline, refused by Lodestar 6370584837)."""

    def test_the_fixed_form_from_sightline_is_valid_for_reversible_steps(self):
        check = am.release_check(RELEASE)
        self.assertTrue(check["valid"], check)
        self.assertEqual(check["steps"], ["commit", "push", "pr"])
        self.assertIn("erlaubt nur commit, push, pr", am.release_note(RELEASE))

    def test_the_same_form_from_another_sender_or_for_other_steps_is_refused(self):
        cases = {
            "Absender": RELEASE.replace("**Sightline →", "**TG 0.24 „Salvo“ · Mac →"),
            "merge": RELEASE.replace("commit, push, pr", "commit, merge"),
            "force-push": RELEASE.replace("commit, push, pr", "force-push"),
            "Zitat": RELEASE.replace("Zitat des Nutzers: „Per Post“\n", ""),
            "Ziel": RELEASE.replace("Ziel: CE-AGENT-TOOLS 0.21 „Courier“", "Ziel: TG 0.25 „Ocular“"),
            "UTC": RELEASE.replace("2026-10-23T17:37Z", "gestern"),
        }
        for word, body in cases.items():
            check = am.release_check(body)
            self.assertFalse(check["valid"], word)
            self.assertIn("UNGÜLTIG", am.release_note(body), word)

    def test_a_release_counts_only_for_the_session_it_names(self):
        """Richter 0.21, Nr 7: an extra `→ X:` line, `Ziel: alle` or the coordinator's --also-pr must not make it valid elsewhere."""
        courier, salvo = am.session_key(NAME), am.session_key("TG 0.24 „Salvo“ · Mac")
        self.assertTrue(am.release_check(RELEASE, courier)["valid"])
        relayed = RELEASE + "Weitergabe → TG 0.24 „Salvo“: zur Kenntnis\n"
        self.assertTrue(am.addressed(relayed, salvo))
        self.assertIn("nicht für diese Sitzung", am.release_note(relayed, salvo))
        everybody = RELEASE.replace("→ CE-AGENT-TOOLS 0.21 „Courier“:", "→ alle:").replace("Ziel: CE-AGENT-TOOLS 0.21 „Courier“", "Ziel: alle")
        self.assertFalse(am.release_check(everybody, salvo)["valid"])
        self.assertFalse(am.release_check(RELEASE, "sightline")["valid"])

    def test_doubled_fields_or_marks_are_refused(self):
        self.assertIn("doppelt", am.release_check(RELEASE + "Schritte: merge\n")["reason"])
        self.assertIn("doppelt", am.release_check(RELEASE + "Freigabe per Post (ce-freigabe 1)\n")["reason"])

    def test_the_time_must_be_a_real_utc_time(self):
        for bad in ("2026-99-99T88:77Z", "2026-10-23T17:37", "2026-10-23T17:37+02:00"):
            self.assertFalse(am.release_check(RELEASE.replace("2026-10-23T17:37Z", bad))["valid"], bad)
        self.assertTrue(am.release_check(RELEASE.replace("2026-10-23T17:37Z", "2026-10-23T17:37:05Z"))["valid"])

    def test_an_informal_release_gets_a_visible_hint_and_ordinary_mail_none(self):
        informal = "**Sightline → CE-AGENT-TOOLS 0.18 „Sweepline“: Freigabe Commit/PR (Nutzer mobil, Variante A)**\nOpus\ncommitten, pushen, PR öffnen"
        self.assertFalse(am.release_check(informal)["formal"])
        self.assertIn("Leitfaden 1", am.release_note(informal))
        self.assertIsNone(am.release_check("Sightline → TG 0.24 „Salvo“: Frage am Bild\nKG03: Griff links?"))

    def test_say_writes_the_form_only_for_sightline(self):
        ok = self.say("--name", "Sightline · Cloud", "--to", "CE-AGENT-TOOLS 0.21 „Courier“", "--freigabe", "commit, push, PR öffnen",
                      "--zitat", "Per Post", "--zeit", "2026-10-23T17:37Z", "Freigabe Commit/PR")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertTrue(am.release_check(ok.stdout)["valid"], ok.stdout)
        other = self.say("--name", "TG 0.24 „Salvo“ · Mac", "--freigabe", "commit", "--zitat", "ja", "Freigabe")
        self.assertEqual(other.returncode, 2)
        merge = self.say("--name", "Sightline · Cloud", "--to", "TG 0.24 „Salvo“", "--freigabe", "commit,merge", "--zitat", "ja", "F")
        self.assertEqual(merge.returncode, 2)
        self.assertIn("merge", merge.stderr)
        forged = self.say("--name", "TG 0.24 „Salvo“ · Mac", "Thema", "Freigabe per Post (ce-freigabe 1)")
        self.assertEqual(forged.returncode, 2)

    def test_a_session_writing_about_a_release_is_told_the_rule(self):
        out = self.say("--name", "X 0.1 „Y“ · Mac", "Commit/PR warten auf Freigabe des Nutzers")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("nur Sightline im festen Format", out.stderr)


class Unreachable(unittest.TestCase):
    """Klasse 5: 6370037962 – `./ce session who` exit 3 in a cloud session that sees only one repository."""

    def test_a_private_postfach_names_the_way_out(self):
        text = am.explain(RuntimeError("gh: Not Found (HTTP 404)"))
        self.assertIn("Repositories", text)
        self.assertIn("cubeescape-nexogen", text)
        self.assertIn("gh fehlt", am.explain(FileNotFoundError(2, "No such file or directory: 'gh'")))

    def test_who_explains_instead_of_a_bare_error(self):
        original = pf.fetch
        pf.fetch = lambda since: (_ for _ in ()).throw(RuntimeError("gh: Not Found (HTTP 404)"))
        self.addCleanup(setattr, pf, "fetch", original)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(pf.main(["who"]), 3)
        self.assertIn("Repositories", err.getvalue())


class Follow(unittest.TestCase):
    """Klasse 6: the watcher ended after every message and was gone after a container restart."""

    def test_follow_prints_each_message_remembers_the_last_and_survives_failures(self):
        pages = iter([[comment(1, "Sightline → TG 0.24 „Salvo“: eins")], RuntimeError("offline"), RuntimeError("offline"),
                      RuntimeError("offline"), RuntimeError("offline"), RuntimeError("offline"),
                      [comment(1, "Sightline → TG 0.24 „Salvo“: eins"), comment(2, "Sightline → TG 0.24 „Salvo“: zwei")]])

        def fetch(_since):
            page = next(pages, [])
            if isinstance(page, Exception):
                raise page
            return page

        remembered, out, err = [], io.StringIO(), io.StringIO()
        clock = iter(range(0, 10_000_000, 60))
        am.follow(fetch, "0.24 salvo", 0, "2000-01-01T00:00:00Z", 60, lambda last, since: remembered.append(last),
                  sleep=lambda _: None, clock=lambda: next(clock), out=out, err=err, rounds=3)
        self.assertEqual(remembered, [1, 2])
        self.assertIn("Kommentar 1", out.getvalue())
        self.assertIn("Kommentar 2", out.getvalue())
        self.assertEqual(err.getvalue().count("versuche es weiter"), 1)

    def test_a_restart_goes_on_from_the_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({"after": 7, "since": "2026-10-23T17:00:00Z"}))
            env = dict(ENV, PATH="/nonexistent")  # no gh: the first read fails, the start line shows where it resumes
            process = subprocess.Popen([sys.executable, str(TOOLS / "ce_await_mail.py"), "--name", "Sightline", "--follow",
                                        "--state", str(state), "--interval", "30"], cwd=tmp, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                line = process.stderr.readline()
            finally:
                process.kill(); process.wait()
                process.stdout.close(); process.stderr.close()
            self.assertIn("ab Kommentar 7", line)

    def test_default_state_is_per_name(self):
        self.assertNotEqual(am.default_state("sightline"), am.default_state("0.24 salvo"))


class Worktree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        subprocess.run(["git", "init", "-q", "-b", "claude/ce-agent-tools-0.21-courier", str(self.repo)], check=True)
        self.git = self.repo / ".git"
        (self.git / am.SESSION_FILE).write_text(json.dumps({"name": NAME, "branch": "b", "reserved_at": _time.time() - 600}))
        for key in ("CE_WATCH", "CE_INBOX", "CE_INBOX_EVERY"):
            os.environ.pop(key, None)
        self.spawned = []
        self.now = [1_000_000.0]

    def text(self, **kwargs):
        kwargs.setdefault("now", lambda: self.now[0])
        kwargs.setdefault("spawn", self.spawned.append)
        kwargs.setdefault("which", lambda _: "/usr/bin/gh")
        return inbox.pending_text(str(self.repo / "sub") if (self.repo / "sub").is_dir() else str(self.repo), **kwargs)

    def put(self, *messages):
        (self.git / inbox.INBOX_FILE).write_text(json.dumps({"name": NAME, "checked_at": self.now[0], "messages": list(messages)}))


class DuringWork(Worktree):
    """Klasse 7: 6370961598 came 17:37 UTC during Courier's running step and was not delivered (no Stop, so no watcher)."""

    def test_nothing_without_reservation_and_no_network_on_most_calls(self):
        (self.git / am.SESSION_FILE).unlink()
        self.assertEqual(self.text(), "")
        self.assertEqual(self.spawned, [])

    def test_an_old_check_starts_one_background_read_per_interval(self):
        self.assertEqual(self.text(), "")
        self.assertEqual(self.spawned, [self.git])
        self.now[0] += 60
        self.assertEqual(self.text(), "")
        self.assertEqual(len(self.spawned), 1, "second call within the interval")
        self.now[0] += inbox.EVERY_S
        self.text()
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual(self.text(which=lambda _: None), "")

    def test_a_new_message_comes_once_as_context_and_counts_as_pending(self):
        self.put(comment(6370961598, "Sightline → CE-AGENT-TOOLS 0.21 „Courier“: Auftragsergänzung\nOpus\nZiel 3"))
        (self.repo / "sub").mkdir()  # the hook runs in a subfolder of the worktree
        text = self.text()
        self.assertIn("Kommentar 6370961598", text)
        self.assertIn("Ziel 3", text)
        state = am.load_state(self.git)
        self.assertEqual((state["hinted"], state["pending"], state["deliveries"]), (6370961598, 6370961598, 1))
        self.assertEqual(self.text(), "", "the same message twice")

    def test_release_messages_carry_the_check_line(self):
        self.put(comment(9, RELEASE))
        self.assertIn("Freigabe per Post gültig", self.text())

    def test_mail_of_the_previous_session_in_a_reused_worktree_stays_out(self):
        """Richter 0.21, Nr 1: Lodestar's fetched mail must not reach Courier in the same worktree."""
        (self.git / inbox.INBOX_FILE).write_text(json.dumps({"name": "CE-AGENT-TOOLS 0.20 „Lodestar“ · Mac", "checked_at": self.now[0],
                                                             "messages": [comment(9, "Sightline → CE-AGENT-TOOLS 0.20 „Lodestar“: x")]}))
        self.assertEqual(self.text(), "")
        self.put(comment(9, "Sightline → CE-AGENT-TOOLS 0.20 „Lodestar“: x"))  # even under the right name: not addressed here
        self.assertEqual(self.text(), "")

    def test_a_reservation_during_the_read_discards_its_result(self):
        def read(since):
            (self.git / am.SESSION_FILE).write_text(json.dumps({"name": "TG 0.25 „Ocular“ · Mac", "reserved_at": _time.time()}))
            return [comment(9, "Sightline → CE-AGENT-TOOLS 0.21 „Courier“: x")]
        inbox.fetch(self.git, read=read, now=lambda: self.now[0])
        self.assertFalse((self.git / inbox.INBOX_FILE).exists())

    def test_a_long_message_is_cut_visibly_with_the_link(self):
        long = comment(9, "Sightline → CE-AGENT-TOOLS 0.21 „Courier“: lang\n" + "x" * 5000 + "\nLETZTE ANWEISUNG")
        long["html_url"] = "https://github.com/o/r/issues/16#issuecomment-9"
        inbox.fetch(self.git, read=lambda since: [long], now=lambda: self.now[0])
        text = self.text()
        self.assertNotIn("LETZTE ANWEISUNG", text)
        self.assertIn("gekürzt auf 4000 Zeichen – Volltext: https://github.com/o/r/issues/16#issuecomment-9", text)

    def test_a_later_hint_does_not_skip_an_unconfirmed_earlier_one(self):
        """Richter 0.21 Runde 2, Nr 1: hint 10 must not overwrite the unconfirmed pending 9."""
        self.put(comment(9, "Sightline → CE-AGENT-TOOLS 0.21 „Courier“: neun"))
        self.assertIn("Kommentar 9", self.text())
        self.put(comment(9, "Sightline → CE-AGENT-TOOLS 0.21 „Courier“: neun"), comment(10, "Sightline → CE-AGENT-TOOLS 0.21 „Courier“: zehn"))
        self.assertIn("Kommentar 10", self.text())
        transcript = self.repo / "t.jsonl"
        transcript.write_text("... Kommentar 10 ...")
        self.assertEqual(am.settle(self.git, str(transcript)), 0, "9 unconfirmed: after must not jump to 10")
        transcript.write_text("... Kommentar 9 ...")
        self.assertEqual(am.settle(self.git, str(transcript)), 9)
        self.assertEqual(am.load_state(self.git)["pending"], 10)
        transcript.write_text("... Kommentar 9 ... Kommentar 10 ...")
        self.assertEqual(am.settle(self.git, str(transcript)), 10)

    def test_after_the_last_redelivery_only_the_lowest_counts_as_handled(self):
        """Richter 0.21 Runde 3, Nr 4: deliveries of 9 must not use up the retry of 10."""
        am.save_state(self.git, pending=9, pending_ids=[9, 10], deliveries=2, hinted=10)
        self.assertEqual(am.settle(self.git, ""), 9)
        state = am.load_state(self.git)
        self.assertEqual((state["pending"], state["pending_ids"], state["deliveries"]), (10, [10], 1))
        self.assertEqual(am.settle(self.git, ""), 9, "10 gets its own second delivery first")
        am.save_state(self.git, deliveries=2)
        self.assertEqual(am.settle(self.git, ""), 10)

    def test_a_cut_release_is_checked_on_the_full_text(self):
        """Richter 0.21 Runde 2, Nr 7: a second `Schritte: merge` behind the cut must not leave a valid `Schritte: commit`."""
        body = RELEASE.replace("commit, push, pr", "commit") + "x" * 5000 + "\nSchritte: merge\n"
        self.assertFalse(am.release_check(body)["valid"])
        inbox.fetch(self.git, read=lambda since: [comment(9, body)], now=lambda: self.now[0])
        text = self.text()
        self.assertIn("UNGÜLTIG", text)
        self.assertNotIn("gültig (ce-freigabe", text)

    def test_switched_off(self):
        self.put(comment(9, "Sightline → CE-AGENT-TOOLS 0.21 „Courier“: x"))
        os.environ["CE_INBOX"] = "0"
        self.addCleanup(os.environ.pop, "CE_INBOX", None)
        self.assertEqual(self.text(), "")

    def test_hook_output_is_additional_context(self):
        self.put(comment(9, "Sightline → CE-AGENT-TOOLS 0.21 „Courier“: x"))
        result = inbox.hint({"cwd": str(self.repo)}, now=lambda: self.now[0], spawn=self.spawned.append, which=lambda _: None)
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("Kommentar 9", result["hookSpecificOutput"]["additionalContext"])

    def test_fetch_keeps_only_new_addressed_messages(self):
        am.save_state(self.git, after=5)
        comments = [comment(5, "Sightline → CE-AGENT-TOOLS 0.21 „Courier“: alt"), comment(6, "Sightline → TG 0.25 „Ocular“: fremd"),
                    comment(7, "Sightline → CE-AGENT-TOOLS 0.21 „Courier“: neu"), comment(8, f"{NAME} → Sightline: eigene")]
        inbox.fetch(self.git, read=lambda since: comments, now=lambda: self.now[0])
        saved = json.loads((self.git / inbox.INBOX_FILE).read_text())
        self.assertEqual([m["id"] for m in saved["messages"]], [7])
        self.assertFalse((self.git / inbox.LOCK_FILE).exists())

    def test_fetch_records_a_failure_and_one_fetch_at_a_time(self):
        inbox.fetch(self.git, read=lambda since: (_ for _ in ()).throw(RuntimeError("offline")), now=lambda: self.now[0])
        self.assertIn("offline", json.loads((self.git / inbox.INBOX_FILE).read_text())["error"])
        (self.git / inbox.LOCK_FILE).write_text("1")
        calls = []
        inbox.fetch(self.git, read=lambda since: calls.append(since) or [], now=lambda: _time.time())
        self.assertEqual(calls, [], "a running fetch holds the lock")

    def test_the_stop_watcher_skips_what_the_hint_delivered_and_brings_it_again_without_proof(self):
        self.put(comment(9, "Sightline → CE-AGENT-TOOLS 0.21 „Courier“: x"))
        self.text()
        self.assertTrue(watch_hook.hinted(self.git, 9))
        transcript = self.repo / "t.jsonl"
        transcript.write_text("nothing about it")
        self.assertEqual(am.settle(self.git, str(transcript)), 0)  # no proof: pending stays, the hint mark goes
        self.assertFalse(watch_hook.hinted(self.git, 9))
        transcript.write_text("... Kommentar 9 ...")
        self.assertEqual(am.settle(self.git, str(transcript)), 9)

    def test_a_running_stop_watcher_ends_when_the_worktree_is_reserved_anew(self):
        """Richter 0.21 Runde 4, Nr 1: Lodestar's watcher must not deliver Lodestar's mail into Courier's session or state."""
        lodestar = "CE-AGENT-TOOLS 0.20 „Lodestar“ · Mac"
        (self.git / am.SESSION_FILE).write_text(json.dumps({"name": lodestar, "branch": "b", "reserved_at": _time.time() - 600}))
        calls = []

        def fetch(since):
            calls.append(since)
            if len(calls) == 1:  # Courier reserves the worktree while the watcher runs
                (self.git / am.SESSION_FILE).write_text(json.dumps({"name": NAME, "branch": "b", "reserved_at": _time.time()}))
                return []
            return [comment(9, "Sightline → CE-AGENT-TOOLS 0.20 „Lodestar“: x")]

        clock = iter(range(0, 10 ** 7, 60))
        err = io.StringIO()
        code = watch_hook.watch({"cwd": str(self.repo)}, fetch=fetch, sleep=lambda _: None, clock=lambda: next(clock),
                                parent=lambda: 42, err=err)
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertNotIn("pending", am.load_state(self.git))
        self.assertFalse((self.git / am.PID_FILE).exists())

    def test_codex_gets_the_hint_with_its_guard_answer(self):
        codex = load("codex_guard", PLUGIN / "hooks" / "codex_guard.py")
        self.put(comment(9, "Sightline → CE-AGENT-TOOLS 0.21 „Courier“: x"))
        schemas = load("test_client_hooks", ROOT / "tests" / "test_client_hooks.py")  # Codex 0.162.1 wire schemas
        out = io.StringIO()
        payload = schemas.codex_input("Bash", {"command": "ls"}, cwd=str(self.repo))
        self.assertEqual(schemas.validate(payload, schemas.INPUT_SCHEMA), [])
        os.environ["CE_INBOX_EVERY"] = "100000000"  # no background read from the test
        self.addCleanup(os.environ.pop, "CE_INBOX_EVERY", None)
        (self.git / inbox.INBOX_FILE).write_text(json.dumps({"name": NAME, "checked_at": _time.time(),
                                                             "messages": [comment(9, "Sightline → CE-AGENT-TOOLS 0.21 „Courier“: x")]}))
        codex.main(io.StringIO(json.dumps(payload)), out)
        answer = json.loads(out.getvalue())
        self.assertEqual(schemas.validate(answer, schemas.OUTPUT_SCHEMA), [])
        self.assertIn("Kommentar 9", answer["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("permissionDecision", answer["hookSpecificOutput"])
        out = io.StringIO()
        codex.main(io.StringIO(json.dumps(payload)), out)
        self.assertEqual(out.getvalue(), "", "nothing new: no output")


class Registration(unittest.TestCase):
    def test_hooks_json_registers_the_inbox_after_every_tool(self):
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        entry = hooks["PostToolUse"][0]["hooks"][0]
        self.assertIn("postfach_inbox.py\" hint", entry["command"])
        self.assertLessEqual(entry["timeout"], 5)

    def test_broken_tools_fail_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp)
            (plugin / "hooks").mkdir()
            (plugin / "tools").mkdir()
            (plugin / "hooks" / "postfach_inbox.py").write_text((PLUGIN / "hooks" / "postfach_inbox.py").read_text(encoding="utf-8"))
            (plugin / "tools" / "ce_await_mail.py").write_text("raise RuntimeError('kaputt')\n")
            result = subprocess.run([sys.executable, str(plugin / "hooks" / "postfach_inbox.py"), "hint"], input="{}",
                                    capture_output=True, text=True, env=dict(os.environ, CLAUDE_PLUGIN_ROOT=str(plugin)))
            self.assertEqual((result.returncode, result.stdout), (0, ""))
            # cwd outside any git repo: without a cwd in the input the hook uses the process folder, and the test run's folder is
            # this (reserved) worktree – its real Postfach once leaked into the expected output (0.21, failure during the probe)
            bad = subprocess.run([sys.executable, str(PLUGIN / "hooks" / "postfach_inbox.py"), "hint"], input="kein json",
                                 capture_output=True, text=True, cwd=tmp)
            self.assertEqual((bad.returncode, bad.stdout), (0, ""))

    def test_reserve_help_passes_through(self):
        out = subprocess.run(["sh", str(TOOLS / "ce"), "session", "reserve", "--help"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--model", out.stdout)


if __name__ == "__main__":
    unittest.main()
