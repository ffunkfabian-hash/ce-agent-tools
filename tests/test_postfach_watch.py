import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time as _time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "ce-shared"
sys.path.insert(0, str(PLUGIN / "tools"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = load("postfach_watch", PLUGIN / "hooks" / "postfach_watch.py")
am = hook.am
NAME = "TG 0.24 „Salvo“ · Mac"


def comment(number, body, created=None):
    # a fixed time turned these tests red 30 min after it (the watcher reads from the reservation or the look-back): relative to now
    created = created or _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() - 60))
    return {"id": number, "created_at": created, "body": body, "pr": 16}


class Worktree(unittest.TestCase):
    """A git worktree on a reserved branch, as ce_session_name.py --reserve leaves it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        subprocess.run(["git", "init", "-q", "-b", "claude/tg-0.24-salvo", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "--allow-empty", "-m", "init"], check=True, env=env)
        self.git = self.repo / ".git"
        self.reserve()
        os.environ.pop("CE_WATCH", None)

    def reserve(self, name=NAME, branch="claude/tg-0.24-salvo"):
        (self.git / am.SESSION_FILE).write_text(json.dumps({"name": name, "branch": branch}), encoding="utf-8")

    def fake_watcher(self) -> int:
        """A live process whose command line names the watcher (alive() ignores foreign pids)."""
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "ce_await_mail"])
        self.addCleanup(lambda: (process.kill(), process.wait()))
        return process.pid

    def payload(self, **extra):
        return dict({"cwd": str(self.repo), "hook_event_name": "Stop", "stop_hook_active": False}, **extra)


class Remind(Worktree):
    def test_reserved_session_without_watcher_is_held_once(self):
        result = hook.remind(self.payload(), sleep=lambda _: None, which=lambda _: "/usr/bin/gh")
        self.assertEqual(result["decision"], "block")
        self.assertIn('./ce session mail --name "TG 0.24 „Salvo“"', result["reason"])
        self.assertEqual(hook.remind(self.payload(stop_hook_active=True), sleep=lambda _: None, which=lambda _: "/usr/bin/gh"), {})

    def test_reminds_at_most_every_two_hours_and_never_without_gh_or_after_failure(self):
        """Richter 0.15, 7a: a session that cannot listen must not be woken again and again."""
        clock = [1_000_000.0]
        remind = lambda **kw: hook.remind(self.payload(), sleep=lambda _: None, now=lambda: clock[0], **kw)
        self.assertEqual(remind(which=lambda _: "/usr/bin/gh")["decision"], "block")
        clock[0] += 60
        self.assertEqual(remind(which=lambda _: "/usr/bin/gh"), {}, "second stop a minute later")
        clock[0] += 2 * 3600
        self.assertEqual(remind(which=lambda _: None), {}, "no gh: no reminder")
        am.save_state(self.git, last_failure=clock[0] - 60)
        self.assertEqual(remind(which=lambda _: "/usr/bin/gh"), {}, "GitHub just failed")
        am.save_state(self.git, last_failure=0)
        self.assertEqual(remind(which=lambda _: "/usr/bin/gh")["decision"], "block")

    def test_reservation_belongs_to_the_worktree_not_the_branch(self):
        """0.15.1, live probe 22.10.: --reserve does not check the branch out, a cloud session stays on its designated branch."""
        self.reserve(branch="claude/cube-escape-patch-probe-wakeline")
        self.assertEqual(hook.remind(self.payload(), sleep=lambda _: None, which=lambda _: "/usr/bin/gh")["decision"], "block")
        folder, session = am.reservation(str(self.repo))
        self.assertEqual((folder, session["name"]), (self.git, NAME))

    def test_old_reservation_without_timestamp_expires_by_file_time(self):
        old = __import__("time").time() - (am.RESERVATION_DAYS + 1) * 86400
        os.utime(self.git / am.SESSION_FILE, (old, old))
        self.assertEqual(am.reservation(str(self.repo)), (None, {}))

    def test_silent_with_watcher_without_reservation_or_switched_off(self):
        (self.git / am.PID_FILE).write_text(str(self.fake_watcher()))
        self.assertEqual(hook.remind(self.payload(), sleep=lambda _: None, which=lambda _: "/usr/bin/gh"), {})
        (self.git / am.PID_FILE).unlink()
        (self.git / am.SESSION_FILE).unlink()
        self.assertEqual(hook.remind(self.payload(), sleep=lambda _: None, which=lambda _: "/usr/bin/gh"), {})
        self.reserve()
        os.environ["CE_WATCH"] = "0"
        try:
            self.assertEqual(hook.remind(self.payload(), sleep=lambda _: None, which=lambda _: "/usr/bin/gh"), {})
        finally:
            del os.environ["CE_WATCH"]

    def test_a_dead_pid_does_not_count(self):
        (self.git / am.PID_FILE).write_text("999999999")
        self.assertEqual(hook.remind(self.payload(), sleep=lambda _: None, which=lambda _: "/usr/bin/gh")["decision"], "block")


class Watch(Worktree):
    def run_watch(self, pages, parent=lambda: 42, transcript=""):
        calls, err = [], io.StringIO()

        def fetch(_since):
            calls.append(_since)
            return pages[min(len(calls), len(pages)) - 1]

        clock = iter(range(0, 10 ** 7, 60))
        code = hook.watch(self.payload(transcript_path=transcript), fetch=fetch, sleep=lambda _: None, clock=lambda: next(clock),
                          parent=parent, err=err)
        return code, err.getvalue(), calls

    def test_a_message_wakes_with_exit_2_and_is_remembered(self):
        pages = [[], [comment(7, "Sightline · Cloud → TG 0.24 „Salvo“: Freigabe\nm\n")]]
        code, text, _ = self.run_watch(pages)
        self.assertEqual(code, 2)
        self.assertIn("Kommentar 7", text)
        self.assertIn("Freigabe", text)
        self.assertEqual(am.load_state(self.git)["pending"], 7)
        self.assertNotIn("after", am.load_state(self.git))
        self.assertFalse((self.git / am.PID_FILE).exists())
        transcript = self.repo / "t.jsonl"
        transcript.write_text('{"text": "Postfach … Kommentar 7 …"}\n')
        code, _, _ = self.run_watch([[comment(7, "Sightline → TG 0.24 „Salvo“: Freigabe")]] * 3, transcript=str(transcript))
        self.assertNotEqual(code, 2, "a delivered comment (seen in the transcript) must not wake again")
        self.assertEqual(am.load_state(self.git)["after"], 7)

    def test_without_proof_of_delivery_a_message_comes_once_more_then_counts(self):
        """Richter 0.15, 7b: a wake that went nowhere must not lose the message, but must not loop either."""
        page = [[comment(7, "Sightline → TG 0.24 „Salvo“: Freigabe")]]
        self.assertEqual(self.run_watch(page)[0], 2)
        self.assertEqual(self.run_watch(page * 3)[0], 2, "redelivered once without proof")
        self.assertNotEqual(self.run_watch(page * 3)[0], 2, "then handled")
        self.assertEqual(am.load_state(self.git)["after"], 7)

    def test_second_delivery_is_marked_as_such(self):
        """Richter 0.15, 7g: the session must not act twice on the same message."""
        page = [[comment(7, "Sightline → TG 0.24 „Salvo“: Freigabe")]]
        self.assertNotIn("Erneute Zustellung", self.run_watch(page)[1])
        code, text, _ = self.run_watch(page * 3)
        self.assertEqual(code, 2)
        self.assertIn("Erneute Zustellung von Kommentar 7", text)

    def test_unwritable_state_never_wakes(self):
        """Richter 0.15, 7e: without a record every Stop would wake with the same message – stay quiet instead."""
        (self.git / am.STATE_FILE).mkdir()
        page = [[comment(7, "Sightline → TG 0.24 „Salvo“: Freigabe")]] * 3
        for _ in range(3):
            self.assertEqual(self.run_watch(page)[0], 0)

    def test_state_writes_are_atomic(self):
        self.assertTrue(am.save_state(self.git, after=5))
        self.assertEqual(list(self.git.glob(am.STATE_FILE + ".*.tmp")), [])
        self.assertEqual(am.load_state(self.git)["after"], 5)

    def test_message_found_after_the_session_died_stays_unmarked(self):
        """Richter 0.15, 7b: parent gone between the last sleep and the find – nothing marked, nobody woken."""
        parents = iter([100, 100, 1, 1])
        code, text, _ = self.run_watch([[], [comment(8, "Sightline → TG 0.24 „Salvo“: x")]], parent=lambda: next(parents, 1))
        self.assertEqual((code, text), (0, ""))
        self.assertEqual(am.load_state(self.git), {})

    def test_reads_from_the_reservation_not_only_thirty_minutes(self):
        """Richter 0.15, 7c: the first answer after a long first turn still arrives."""
        import time as _time
        reserved = _time.time() - 3 * 3600
        (self.git / am.SESSION_FILE).write_text(json.dumps({"name": NAME, "branch": "claude/tg-0.24-salvo", "reserved_at": reserved}))
        old = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() - 2 * 3600))
        code, _, calls = self.run_watch([[comment(9, "Sightline → TG 0.24 „Salvo“: Antwort", created=old)]])
        self.assertEqual(code, 2)
        self.assertLessEqual(calls[0], old)

    def test_stale_reservation_is_ignored(self):
        import time as _time
        (self.git / am.SESSION_FILE).write_text(json.dumps({"name": NAME, "branch": "claude/tg-0.24-salvo",
                                                            "reserved_at": _time.time() - 8 * 86400}))
        self.assertEqual(self.run_watch([[comment(7, "Sightline → TG 0.24 „Salvo“: x")]])[2], [])

    def test_broadcast_wakes_and_own_messages_do_not(self):
        self.assertEqual(self.run_watch([[comment(8, "Sightline → alle: Blender frei")]])[0], 2)
        am.save_state(self.git, after=8, pending=None, deliveries=None)
        own = [[comment(9, "TG 0.24 „Salvo“ · Mac → Sightline: fertig")]] * 2
        self.assertEqual(self.run_watch(own, parent=iter([1, 1, 2, 2]).__next__)[0], 0)

    def test_no_second_watcher_and_none_without_reservation(self):
        (self.git / am.PID_FILE).write_text(str(self.fake_watcher()))
        code, _, calls = self.run_watch([[comment(7, "Sightline → TG 0.24 „Salvo“: x")]])
        self.assertEqual((code, calls), (0, []))
        (self.git / am.PID_FILE).unlink()
        (self.git / am.SESSION_FILE).unlink()
        code, _, calls = self.run_watch([[comment(7, "Sightline → TG 0.24 „Salvo“: x")]])
        self.assertEqual((code, calls), (0, []))

    def test_ends_quietly_when_the_session_process_is_gone(self):
        parents = iter([100, 100, 1, 1, 1])
        code, text, calls = self.run_watch([[]], parent=lambda: next(parents))
        self.assertEqual((code, text), (0, ""))
        self.assertFalse((self.git / am.PID_FILE).exists())


class ManualWatcher(Worktree):
    def run_cli(self, *args):
        env = dict(os.environ, PATH=os.environ["PATH"])
        return subprocess.run([sys.executable, str(PLUGIN / "tools" / "ce_await_mail.py"), *args],
                              cwd=self.repo, capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)

    def test_second_watcher_for_the_same_session_exits_4(self):
        (self.git / am.PID_FILE).write_text(str(self.fake_watcher()))
        result = self.run_cli("--timeout", "0.01")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("läuft schon", result.stderr)

    def test_without_name_and_without_reservation_it_explains(self):
        (self.git / am.SESSION_FILE).unlink()
        result = self.run_cli("--timeout", "0.01")
        self.assertEqual(result.returncode, 5, "not 2 - a rule restarts on 2")
        self.assertIn("--name fehlt", result.stderr)


class Slot(Worktree):
    def test_register_is_atomic_and_takes_over_stale_files(self):
        (self.git / am.PID_FILE).write_text(str(self.fake_watcher()))
        self.assertFalse(am.register(self.git), "no claim while a live watcher holds the slot")
        (self.git / am.PID_FILE).write_text("999999999")
        self.assertTrue(am.register(self.git), "dead pid is taken over")
        am.unregister(self.git)
        self.assertFalse((self.git / am.PID_FILE).exists())

    def test_foreign_pid_does_not_count(self):
        # 0.31: Windows has no sleep program outside Git Bash - a Python sleeper without the watcher's name instead
        sleeper = subprocess.Popen(["sleep", "30"] if os.name != "nt" else [sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        (self.git / am.PID_FILE).write_text(str(sleeper.pid))
        self.assertEqual(am.watcher_pid(self.git), 0)
        self.assertIsNone(sleeper.poll(), "the check must not end the foreign process (os.kill(pid, 0) on Windows would)")


class WindowsBranches(Worktree):
    """0.31: the Windows paths of the watcher, mocked so they also run on macOS/Linux, plus real checks on Windows."""

    class FakeLock:
        def __init__(self, alive=True, started="filetime 1"):
            self.alive, self.started, self.asked = alive, started, []

        def pid_alive(self, pid):
            self.asked.append(pid)
            return self.alive

        def process_started(self, pid):
            return self.started

    def windows(self, lock, command):
        from unittest import mock
        patches = [mock.patch.object(am, "WINDOWS", True), mock.patch.object(am, "_heavy_lock", return_value=lock),
                   mock.patch.object(am, "windows_command", return_value=command),
                   mock.patch.object(am.os, "kill", side_effect=AssertionError("os.kill on Windows is TerminateProcess"))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_alive_on_windows_never_signals_and_checks_the_command_line(self):
        from unittest import mock
        self.windows(self.FakeLock(alive=True), "C:\\Python\\python.exe ce_await_mail.py --name x")
        self.assertTrue(am.alive(4242))
        with mock.patch.object(am, "windows_command", return_value="C:\\Windows\\notepad.exe"):
            self.assertFalse(am.alive(4242), "a reused pid of another program does not count")
        with mock.patch.object(am, "windows_command", return_value=None):
            self.assertTrue(am.alive(4242), "an unreadable command line counts as alive, like a failed ps")
        lock = self.FakeLock(alive=False)
        with mock.patch.object(am, "_heavy_lock", return_value=lock):
            self.assertFalse(am.alive(4242))
        self.assertEqual(lock.asked, [4242])

    def test_alive_on_windows_without_heavy_lock_stays_quiet(self):
        self.windows(None, "")
        self.assertTrue(am.alive(4242), "no platform layer: no second watcher rather than two")

    def test_windows_parent_reports_1_when_the_session_is_gone_or_its_pid_reused(self):
        state = {}
        self.assertEqual(hook.windows_parent(state, self.FakeLock(alive=True)), os.getppid())
        self.assertEqual(hook.windows_parent(state, self.FakeLock(alive=False)), 1)
        self.assertEqual(hook.windows_parent(state, self.FakeLock(alive=True, started="filetime 2")), 1, "pid reused")
        self.assertEqual(hook.windows_parent(state, self.FakeLock(alive=True)), os.getppid())

    def test_the_inbox_read_starts_detached_and_retries_without_breakaway(self):
        from unittest import mock
        inbox = load("postfach_inbox", PLUGIN / "hooks" / "postfach_inbox.py")
        calls = []

        def popen(argv, **kwargs):
            calls.append(kwargs["creationflags"])
            if len(calls) == 1:
                raise PermissionError("job without breakaway")

        with mock.patch.multiple(subprocess, DETACHED_PROCESS=0x8, CREATE_NEW_PROCESS_GROUP=0x200, create=True):
            inbox.start_fetch_windows(["x"], popen=popen)
        self.assertEqual(calls, [0x8 | 0x200 | 0x01000000, 0x8 | 0x200])

    @unittest.skipUnless(os.name == "nt", "real Windows processes")
    def test_on_windows_the_check_leaves_the_watcher_running(self):
        import heavy_lock
        pid = self.fake_watcher()
        (self.git / am.PID_FILE).write_text(str(pid))
        self.assertEqual(am.watcher_pid(self.git), pid)
        self.assertTrue(heavy_lock.pid_alive(pid), "alive() must not end the process it looks at")
        self.assertIn("ce_await_mail", am.windows_command(pid))
        self.assertEqual(am.windows_command(999999999), "")
        self.assertEqual(hook.windows_parent({}), os.getppid())

    def test_hooks_speak_utf8_whatever_the_console_encoding(self):
        """0.31: Windows' cp1252 pipe would turn „Salvo“ into mojibake and → into an error; the hooks switch to UTF-8."""
        env = {key: value for key, value in os.environ.items() if key not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        for name in ("postfach_watch", "postfach_inbox"):
            script = (f"import importlib.util, sys; sys.path.insert(0, {str(PLUGIN / 'hooks')!r}); "
                      f"spec = importlib.util.spec_from_file_location('h', {str(PLUGIN / 'hooks' / (name + '.py'))!r}); "
                      "h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h); h.utf8_io(); "
                      "line = sys.stdin.readline().strip(); print(line + ' → ok'); print('„Fehler“ →', file=sys.stderr)")
            done = subprocess.run([sys.executable, "-c", script], input="TG 0.24 „Salvo“ · Ö".encode("utf-8"),
                                  capture_output=True, env=env, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.decode("utf-8").strip(), "TG 0.24 „Salvo“ · Ö → ok", name)
            self.assertEqual(done.stderr.decode("utf-8").strip(), "„Fehler“ →", name)


class Registration(unittest.TestCase):
    def test_hooks_json_registers_async_watcher_and_reminder(self):
        stop = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["Stop"]
        entries = [h for group in stop for h in group["hooks"]]
        watch = [h for h in entries if "postfach_watch.py\" watch" in h["command"]]
        remind = [h for h in entries if "postfach_watch.py\" remind" in h["command"]]
        self.assertEqual(len(watch), 1)
        self.assertTrue(watch[0].get("async") and watch[0].get("asyncRewake"))
        self.assertEqual(watch[0].get("timeout"), hook.MAX_HOURS * 3600, "Richter 0.15, 7d: the client default would end it early")
        self.assertEqual(len(remind), 1)
        self.assertFalse(remind[0].get("async"))

    def test_broken_tools_fail_open(self):
        with tempfile.TemporaryDirectory() as folder:
            plugin = Path(folder) / "ce-shared"
            (plugin / "hooks").mkdir(parents=True)
            (plugin / "tools").mkdir()
            (plugin / "hooks" / "postfach_watch.py").write_text((PLUGIN / "hooks" / "postfach_watch.py").read_text(encoding="utf-8"), encoding="utf-8")
            (plugin / "tools" / "ce_await_mail.py").write_text("raise RuntimeError('kaputt')\n")
            for mode, expected in (("remind", "{}"), ("watch", "")):
                result = subprocess.run([sys.executable, str(plugin / "hooks" / "postfach_watch.py"), mode], input="{}",
                                        capture_output=True, text=True, encoding="utf-8", env=dict(os.environ, CLAUDE_PLUGIN_ROOT=str(plugin)))
                self.assertEqual((result.returncode, result.stdout.strip()), (0, expected), mode)


if __name__ == "__main__":
    unittest.main()
