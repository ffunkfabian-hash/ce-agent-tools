"""heavy_lock.py of ce-shared: one heavy run at a time on the machine. Tests taken over from Cube Escape
(Tools/tests/test_heavy_lock.py), the robot generator and Toolgen (tests/test_heavy_lock.py), minus what needs their
repositories (quiet.py, run_testbed.sh, Blender). Run: python3 -m unittest discover -s tests"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent / "plugins" / "ce-shared" / "tools"
SCRIPT = TOOLS / "heavy_lock.py"
sys.path.insert(0, str(TOOLS))
import heavy_lock as heavy  # noqa: E402

try:
    import fcntl
except ImportError:  # Windows (0.31): the lock's own LockFileEx layer
    fcntl = None
WINDOWS = os.name == "nt"


def try_lock(fd):
    """flock(LOCK_EX | LOCK_NB) independent of heavy_lock where there is flock, else heavy_lock.lock_fd."""
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        heavy.lock_fd(fd if isinstance(fd, int) else fd.fileno())


class LockCase(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name)
        self.lock = self.base / "sub" / "heavy-run.lock"
        env = {k: v for k, v in os.environ.items() if k not in ("CE_HEAVY_LOCK", "CE_HEAVY_HELD")}
        env["CE_HEAVY_LOCK_FILE"] = str(self.lock)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lines = []

    def free(self):
        """True when nobody holds the lock (takes and drops it at once)."""
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT | heavy.OPEN_FLAGS, 0o644)
        try:
            try_lock(fd)
            if WINDOWS:  # a closed handle frees its lock only "when resources allow"
                heavy.unlock_fd(fd)
            return True
        except BlockingIOError:
            return False
        finally:
            os.close(fd)

    def cli(self, *args, **kw):
        return subprocess.Popen([sys.executable, str(SCRIPT), *args], env=os.environ.copy(), **kw)


class Contract(LockCase):
    def test_holder_line_status_and_release(self):
        self.assertIsNone(heavy.holder())
        with heavy.held("full regression", "/x/checkout", out=self.lines.append) as lock:
            self.assertTrue(lock.held)
            self.assertEqual(lock["pid"], os.getpid())  # dict-like access (Cube Escape callers)
            info = json.loads(self.lock.read_text())
            self.assertEqual(sorted(info), ["checkout", "pid", "since", "what"])
            self.assertEqual((info["pid"], info["what"], info["checkout"]), (os.getpid(), "full regression", "/x/checkout"))
            self.assertIn("full regression (PID %d, /x/checkout" % os.getpid(), heavy.holder())
            self.assertEqual(os.environ.get("CE_HEAVY_HELD"), "1", "children of the holder do not queue again")
            self.assertEqual(lock.state(), "held, waited 0 s")
        self.assertNotIn("CE_HEAVY_HELD", os.environ)
        self.assertEqual(self.lock.read_text(), "", "no stale holder line")
        self.assertTrue(self.free())
        self.assertTrue(self.lock.exists(), "the lock file is never deleted")

    def test_skipped(self):
        self.assertIsNone(heavy.skipped({}))
        self.assertIn("CE_HEAVY_LOCK=0", heavy.skipped({"CE_HEAVY_LOCK": " 0 "}))
        self.assertIn("CE_HEAVY_HELD=1", heavy.skipped({"CE_HEAVY_HELD": "1"}))
        os.environ["CE_HEAVY_LOCK"] = "0"
        with heavy.held("x") as lock:
            self.assertFalse(lock.held)
            self.assertIsNone(heavy.holder())
            self.assertEqual(lock.state(), "skipped (CE_HEAVY_LOCK=0)")

    def test_a_second_run_waits_and_names_the_holder(self):
        hold = 6 if WINDOWS else 1.5  # Windows: the parent check below asks PowerShell, slow under load (0.31)
        holder = self.cli("run", "--what", "baseline.sh", "--checkout", "/x/first", "--", sys.executable, "-c",
                          f"import time; print('go', flush=True); time.sleep({hold})", stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "go")
            self.assertIn("baseline.sh", heavy.holder())
            pid = heavy.holder_pid()
            if WINDOWS and pid != holder.pid:  # a venv's python.exe redirects: the interpreter is its child (0.31)
                self.assertEqual(heavy.process_table().get(pid, (None,))[0], holder.pid)
            else:
                self.assertEqual(pid, holder.pid)
            start = time.monotonic()
            with mock.patch.dict(os.environ, CE_HEAVY_HELD=""):
                with heavy.held("second", out=self.lines.append, poll=0.05):
                    waited = time.monotonic() - start
            self.assertGreater(waited, 0.5, "it waited for the first run")
            self.assertIn("waiting", self.lines[0])
            self.assertIn("baseline.sh", self.lines[0])
            self.assertIn("/x/first", self.lines[0])
            self.assertIn("CE_HEAVY_LOCK=0", self.lines[0], "the way out is named")
            self.assertIn("free after", self.lines[-1])
        finally:
            holder.communicate(timeout=10)
        self.assertEqual(holder.returncode, 0)

    def test_waits_then_acquires_after_release(self):
        first = heavy.HeavyLock("first").acquire()

        def release():
            time.sleep(0.15)
            first.release()

        thread = threading.Thread(target=release)
        thread.start()
        with mock.patch.dict(os.environ, CE_HEAVY_HELD=""):
            waiter = heavy.HeavyLock("second", poll=0.01, out=self.lines.append).acquire()
        thread.join()
        try:
            self.assertTrue(waiter.held)
            self.assertTrue(any("first" in line for line in self.lines))
        finally:
            waiter.release()

    def test_opt_out_and_nested_runs_do_not_wait(self):
        with heavy.held("outer"):
            for env in ({"CE_HEAVY_LOCK": "0", "CE_HEAVY_HELD": ""}, {"CE_HEAVY_HELD": "1"}):
                with self.subTest(env=env):
                    code = subprocess.call([sys.executable, str(SCRIPT), "run", "--", sys.executable, "-c", "pass"],
                                           env=dict(os.environ, **env), stdout=subprocess.DEVNULL, timeout=5)
                    self.assertEqual(code, 0)

    def test_opt_out_says_so(self):
        with heavy.held("other", out=self.lines.append):
            proc = subprocess.run([sys.executable, str(SCRIPT), "run", "--", "true"], capture_output=True, text=True,
                                  env=dict(os.environ, CE_HEAVY_LOCK="0", CE_HEAVY_HELD=""), timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("skipped (CE_HEAVY_LOCK=0)", proc.stdout)

    def test_the_lock_is_not_inherited(self):
        lock = heavy.HeavyLock("x", out=self.lines.append).acquire()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], close_fds=False)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        lock.release()
        self.assertIsNone(heavy.holder())
        again = heavy.HeavyLock("y", out=self.lines.append, poll=0.05)
        start = time.monotonic()
        again.acquire()
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(self.lines, [], "never had to wait")
        again.release()

    def test_cli_passes_env_and_exit_code(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), "run", "--what", "probe", "--", sys.executable, "-c",
                               "import os, sys; print(os.environ.get('CE_HEAVY_HELD')); "
                               "print(open(os.environ['CE_HEAVY_LOCK_FILE']).read().strip()); sys.exit(3)"],
                              env=os.environ.copy(), capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 3)
        held, line = proc.stdout.splitlines()[:2]
        self.assertEqual(held, "1")
        self.assertEqual(json.loads(line)["what"], "probe")
        self.assertIsNone(heavy.holder())

    def test_status_cli(self):
        self.assertEqual(subprocess.run([sys.executable, str(SCRIPT), "status"], capture_output=True, text=True,
                                        env=os.environ.copy()).stdout.split(), ["shared", "free", "unity", "free",
                                                                                "blender", "free", "gpu", "free"])
        with heavy.held("probe", "/x"):
            out = subprocess.run([sys.executable, str(SCRIPT), "status"], capture_output=True, text=True,
                                 env=os.environ.copy()).stdout
        self.assertIn("probe (PID %d, /x" % os.getpid(), out)

    def test_exit_code_of_the_command_is_kept(self):
        code = subprocess.call([sys.executable, str(SCRIPT), "run", "--", sys.executable, "-c", "raise SystemExit(7)"],
                               env=os.environ.copy(), timeout=5)
        self.assertEqual(code, 7)

    def test_a_holder_that_has_not_written_its_line_yet_is_waited_for(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        with self.lock.open("a") as handle:
            try_lock(handle)
            self.assertIsNone(heavy.holder_pid(wait=0.1), "empty file: no PID after the wait")
            handle.write(json.dumps({"pid": 4242}))
            handle.flush()
            self.assertEqual(heavy.holder_pid(wait=0.1), 4242)

    def test_describe_tolerates_foreign_lines(self):
        self.assertEqual(heavy.describe("  "), "unknown")
        self.assertEqual(heavy.describe("legacy holder\n"), "legacy holder")


class ProcessGroups(LockCase):
    def test_background_children_never_keep_the_lock(self):
        # the 0.57 trap: caffeinate & inherited the copy locks and kept the copy busy after the run
        survivor = ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'], "
                    "close_fds=False, start_new_session=True)")
        code = subprocess.call([sys.executable, str(SCRIPT), "run", "--what", "probe", "--", sys.executable, "-c",
                                survivor], env=os.environ.copy(), timeout=10)
        self.assertEqual(code, 0)
        self.assertTrue(self.free(), "the background child did not inherit the lock")

    @unittest.skipIf(WINDOWS, "POSIX TERM; Windows: test_windows_*")
    def test_a_terminated_wrapper_ends_its_command_before_the_lock_is_free(self):
        marker = self.base / "child-ended"
        child = ("import pathlib, signal, sys, time\n"
                 f"signal.signal(signal.SIGTERM, lambda *a: (pathlib.Path({str(marker)!r}).write_text('x'), sys.exit(0)))\n"
                 "print('go', flush=True)\ntime.sleep(10)\n")
        wrapper = self.cli("run", "--", sys.executable, "-c", child, stdout=subprocess.PIPE, text=True)
        self.assertEqual(wrapper.stdout.readline().strip(), "go")
        wrapper.terminate()
        wrapper.wait(timeout=5)
        wrapper.stdout.close()
        self.assertTrue(marker.exists(), "the command got the TERM and ended first")
        self.assertTrue(self.free())

    def grandchild_run(self, grandchild: str, then: str):
        """A wrapped command that starts a grandchild in its process group, says 'go <pid>' and goes on with `then`."""
        command = ("import subprocess, sys\n"
                   f"subprocess.Popen([sys.executable, '-c', {grandchild!r}], stdout=subprocess.DEVNULL)\n"
                   "import os; print('go', os.getpid(), flush=True)\n" + then)
        wrapper = self.cli("run", "--what", "check_output.sh", "--", sys.executable, "-c", command,
                           stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: wrapper.poll() is None and wrapper.kill())
        self.addCleanup(wrapper.stdout.close)
        word, pid = wrapper.stdout.readline().split()
        self.assertEqual(word, "go")
        return wrapper, int(pid)

    def test_a_grandchild_keeps_the_lock_after_its_parent_died(self):
        marker = self.base / "grandchild-ended"
        grandchild = f"import pathlib, time; time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('x')"
        wrapper, command_pid = self.grandchild_run(grandchild, "import time; time.sleep(30)")
        os.kill(command_pid, getattr(signal, "SIGKILL", 9))  # the command dies, the grandchild goes on (Windows: TerminateProcess)
        time.sleep(0.4)
        self.assertIsNone(wrapper.poll(), "the wrapper still waits for the grandchild")
        self.assertFalse(marker.exists())
        self.assertFalse(self.free(), "the lock is held while the grandchild runs")
        self.assertNotEqual(wrapper.wait(timeout=10), 0, "the killed command stays a failure")
        self.assertTrue(marker.exists(), "the lock was held until the grandchild ended")
        self.assertTrue(self.free())

    @unittest.skipIf(WINDOWS, "POSIX TERM; Windows: test_windows_*")
    def test_a_terminated_wrapper_passes_the_term_to_its_grandchildren(self):
        marker = self.base / "grandchild-ended"
        grandchild = ("import pathlib, signal, sys, time\n"
                      f"def done(*a): time.sleep(0.5); pathlib.Path({str(marker)!r}).write_text('x'); sys.exit(0)\n"
                      "signal.signal(signal.SIGTERM, done)\n"
                      f"pathlib.Path({str(marker) + '.ready'!r}).write_text('x')\ntime.sleep(30)\n")
        wrapper, _ = self.grandchild_run(grandchild, "import time; time.sleep(30)")
        ready = str(marker) + ".ready"
        deadline = time.monotonic() + 10
        while not os.path.exists(ready):  # the grandchild has set its handler
            self.assertLess(time.monotonic(), deadline, "the grandchild did not start")
            time.sleep(0.02)
        wrapper.terminate()
        wrapper.wait(timeout=10)
        self.assertTrue(marker.exists(), "the grandchild got the TERM and ended before the lock was free")
        self.assertTrue(self.free())

    def test_groups_wait_for_a_grandchild_when_the_block_ends(self):
        marker = self.base / "grandchild-ended"
        grandchild = f"import pathlib, time; time.sleep(0.8); pathlib.Path({str(marker)!r}).write_text('x')"
        with heavy.held("full regression", out=self.lines.append), heavy.Groups(out=self.lines.append) as groups:
            child = groups.popen([sys.executable, "-c", "import subprocess, sys\n"
                                  f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])"])
            self.assertEqual(child.wait(timeout=10), 0)
            self.assertFalse(marker.exists())
        self.assertTrue(marker.exists(), "the block ended only after the grandchild")
        self.assertTrue(self.free())

    def test_groups_refuse_a_new_child_after_a_signal(self):
        with heavy.Groups() as groups:
            groups.signum = 15  # a TERM arrived earlier in the block
            with self.assertRaises(heavy.Interrupted):
                groups.popen([sys.executable, "-c", "pass"])
            groups.signum = None
        self.assertEqual(groups.pgids, [])

    def test_forwarding_survives_handlers_set_outside_python(self):
        """Inside Blender signal.signal() returns None for handlers set in C - restoring them must not fail."""
        real = heavy.signal.signal
        seen = []

        def fake(sig, handler):
            seen.append(sig)
            real(sig, handler)
            return None

        with mock.patch.object(heavy.signal, "signal", side_effect=fake):
            self.assertEqual(heavy.run_forwarding([sys.executable, "-c", "raise SystemExit(4)"]), 4)
        self.assertEqual(len(seen), 3, "installed, nothing put back (there is nothing to put back)")
        sig = heavy.signal
        for number in heavy.FORWARDED:
            real(number, sig.default_int_handler if number == sig.SIGINT else sig.SIG_DFL)

    def wait_for(self, path, seconds=10):
        deadline = time.monotonic() + seconds
        while not os.path.exists(path):
            self.assertLess(time.monotonic(), deadline, "%s did not appear" % path)
            time.sleep(0.02)

    @unittest.skipUnless(WINDOWS, "Windows job objects")
    def test_windows_a_killed_wrapper_takes_its_whole_job_along(self):
        """TerminateProcess of the wrapper (a tool's time limit) cannot be caught: kill on job close ends the command
        and its grandchild, so nothing of the heavy step runs on once the lock is free."""
        marker = self.base / "grandchild"
        grandchild = ("import os, pathlib, time\n"
                      f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n")
        wrapper, command_pid = self.grandchild_run(grandchild, "import time; time.sleep(30)")
        self.wait_for(marker)
        grandchild_pid = int(marker.read_text())
        self.assertFalse(self.free())
        wrapper.terminate()
        wrapper.wait(timeout=10)
        deadline = time.monotonic() + 5
        while (heavy.pid_alive(command_pid) or heavy.pid_alive(grandchild_pid)) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(heavy.pid_alive(command_pid), "the command died with the wrapper")
        self.assertFalse(heavy.pid_alive(grandchild_pid), "the grandchild died with the wrapper")
        self.assertTrue(self.free())

    @unittest.skipUnless(WINDOWS, "Windows console groups")
    def test_windows_ctrl_break_goes_on_to_the_group_and_the_lock_waits_for_it(self):
        marker = self.base / "grandchild-ended"
        grandchild = ("import pathlib, signal, sys, time\n"
                      f"def done(*a): time.sleep(0.5); pathlib.Path({str(marker)!r}).write_text('x'); sys.exit(0)\n"
                      "signal.signal(signal.SIGBREAK, done)\n"
                      f"pathlib.Path({str(marker) + '.ready'!r}).write_text('x')\n"
                      "for _ in range(300): time.sleep(0.1)\n")  # one long sleep() would only end for SIGINT
        command = ("import subprocess, sys\n"
                   f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
                   "print('go', flush=True)\nimport time; time.sleep(30)\n")
        wrapper = self.cli("run", "--", sys.executable, "-c", command, stdout=subprocess.PIPE, text=True,
                           creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        self.addCleanup(lambda: wrapper.poll() is None and wrapper.kill())
        self.addCleanup(wrapper.stdout.close)
        self.assertEqual(wrapper.stdout.readline().strip(), "go")
        self.wait_for(str(marker) + ".ready")
        try:
            os.kill(wrapper.pid, signal.CTRL_BREAK_EVENT)
        except OSError as error:  # no console to share (a detached test runner)
            self.skipTest("no console for Ctrl+Break: %s" % error)
        wrapper.wait(timeout=15)
        self.assertTrue(marker.exists(), "the grandchild got Ctrl+Break and ended before the lock was free")
        self.assertTrue(self.free())

    @unittest.skipUnless(WINDOWS, "Windows job objects")
    def test_windows_a_child_that_breaks_away_does_not_keep_the_lock(self):
        """CREATE_BREAKAWAY_FROM_JOB is Windows' start_new_session: such a child is not waited for."""
        survivor = ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(6)'], "
                    "creationflags=0x01000000)")
        start = time.monotonic()
        code = subprocess.call([sys.executable, str(SCRIPT), "run", "--what", "probe", "--", sys.executable, "-c",
                                survivor], env=os.environ.copy(), timeout=20)
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - start, 5, "the wrapper did not wait for the child that left")
        self.assertTrue(self.free())

    def test_cache_dir_per_system(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\x\AppData\Local"}):
            path = heavy.cache_dir("heavy-run.lock")
        if WINDOWS:
            self.assertEqual(path, os.path.join(r"C:\Users\x\AppData\Local", "CubeEscape", "heavy-run.lock"))
        else:  # macOS (and Linux) unchanged
            self.assertEqual(path, os.path.join(os.path.expanduser("~"), "Library", "Caches", "CubeEscape",
                                                "heavy-run.lock"))
        self.assertEqual(heavy.DEFAULT, heavy.cache_dir("heavy-run.lock"))

    def test_scripts_start_on_every_system(self):
        """Windows starts only programs (WinError 193 for verify.sh): scripts go through Git Bash or this Python."""
        scripts = {"run.sh": "echo plain\n", "zsh.sh": "#!/bin/zsh -f\necho z\n", "env.py": "#!/usr/bin/env python3\n",
                   "bash": "#!/usr/bin/env -S bash -eu\n", "strict.sh": "#!/bin/bash -e -o pipefail\nfalse\necho ran on\n",
                   "tool": "binary"}
        for name, text in scripts.items():
            (self.base / name).write_text(text)
        argv = lambda name: heavy.script_argv([str(self.base / name), "--x"])
        if not WINDOWS:
            for name in scripts:
                self.assertEqual(argv(name), [str(self.base / name), "--x"], "POSIX: unchanged")
            return
        sh, bash = heavy.posix_shell("sh"), heavy.posix_shell("bash")
        self.assertNotIn("system32", (sh + bash).lower(), "never WSL")
        path = lambda name: str(self.base / name)
        self.assertEqual(argv("run.sh"), [sh, path("run.sh"), "--x"])
        self.assertEqual(argv("zsh.sh"), [bash, path("zsh.sh"), "--x"], "no zsh on Windows; zsh -f is not bash -f")
        self.assertEqual(argv("bash"), [bash, "-eu", path("bash"), "--x"], "the shebang's options stay (-e)")
        strict = subprocess.run(heavy.script_argv([path("strict.sh")]), capture_output=True, text=True, timeout=30)
        self.assertNotEqual(strict.returncode, 0, "bash -e stops at `false` as on the Mac")
        self.assertNotIn("ran on", strict.stdout)
        self.assertEqual(argv("env.py"), [sys.executable, str(self.base / "env.py"), "--x"])
        self.assertEqual(argv("tool"), [str(self.base / "tool"), "--x"])
        self.assertEqual(heavy.script_argv(["git", "status"]), ["git", "status"])
        code = subprocess.call([sys.executable, str(SCRIPT), "run", "--", str(self.base / "run.sh")],
                               env=os.environ.copy(), timeout=30)
        self.assertEqual(code, 0, "heavy_lock run starts a .sh script")

    @unittest.skipUnless(WINDOWS, "Windows job objects")
    def test_windows_failed_job_setup_never_leaves_a_suspended_command(self):
        """Resume refused: the command is ended and the start fails. Assignment refused: the command still runs and
        is waited for on its own (no job)."""
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        started = []
        real_popen = heavy.subprocess.Popen
        def popen(*args, **kw):
            started.append(real_popen(*args, **kw))
            return started[-1]
        with mock.patch.object(heavy.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(heavy._ntdll, "NtResumeProcess", return_value=-1):
            with self.assertRaises(OSError):
                heavy.spawn_group(command)
        self.assertEqual(started[0].wait(timeout=10), started[0].returncode, "the suspended command was ended")
        with mock.patch.object(heavy._k32, "AssignProcessToJobObject", return_value=0):
            child, group = heavy.spawn_group([sys.executable, "-c", "import time; time.sleep(0.5)"])
        self.assertIsNone(group.handle, "no job")
        self.assertTrue(group.alive())
        self.assertEqual(child.wait(timeout=10), 0, "it ran (was resumed)")
        self.assertFalse(group.alive())
        heavy.close_group(group)

    def test_pid_alive_never_ends_the_process(self):
        """Windows' os.kill(pid, 0) is TerminateProcess - pid_alive must only look."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertTrue(heavy.pid_alive(child.pid))
        time.sleep(0.2)
        self.assertIsNone(child.poll(), "still running after the check")
        child.kill()
        child.wait()
        self.assertFalse(heavy.pid_alive(child.pid))

    def test_run_group_passes_env(self):
        out = self.base / "env.txt"
        code = heavy.run_group([sys.executable, "-c", f"import os; open({str(out)!r}, 'w').write(os.environ['PROBE'])"],
                               env=dict(os.environ, PROBE="42"))
        self.assertEqual(code, 0)
        self.assertEqual(out.read_text(), "42")


if __name__ == "__main__":
    unittest.main()


class Resources(LockCase):
    """0.5.0: one lock per resource (unity, blender, gpu); callers without a resource keep the shared file."""

    def test_lock_path_per_resource(self):
        self.assertEqual(heavy.lock_path(), str(self.lock))
        self.assertEqual(heavy.lock_path("unity"), str(self.lock.parent / "heavy-unity.lock"))
        self.assertEqual(heavy.lock_path("blender"), str(self.lock.parent / "heavy-blender.lock"))
        with mock.patch.dict(os.environ, {"CE_HEAVY_RESOURCE": "gpu"}):
            self.assertEqual(heavy.lock_path(), str(self.lock.parent / "heavy-gpu.lock"))
        with self.assertRaises(ValueError):
            heavy.lock_path("metal")

    def test_different_resources_run_side_by_side(self):
        with heavy.held("regression", "/a", out=self.lines.append, resource="unity") as unity:
            self.assertTrue(unity.held)
            self.assertEqual(unity["resource"], "unity")
            with heavy.held("bake", "/b", out=self.lines.append, resource="blender") as blender:
                self.assertTrue(blender.held)  # did not wait for unity
            self.assertEqual(self.lines, [])  # nothing waited
            self.assertIsNotNone(heavy.holder("unity"))
            self.assertIsNone(heavy.holder("blender"))
            self.assertIsNone(heavy.holder())  # the shared file stays free
            self.assertEqual(heavy.holder_pids(), [os.getpid()])

    def test_same_resource_queues(self):
        with heavy.held("regression", "/a", resource="unity"):
            with mock.patch.dict(os.environ, CE_HEAVY_HELD=""):  # another session, not a child of this holder
                proc = self.cli("run", "--resource", "unity", "--what", "second", "--", sys.executable, "-c", "pass",
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            time.sleep(1.0)
            self.assertIsNone(proc.poll())  # still waiting
        self.assertEqual(proc.wait(timeout=20), 0)
        self.assertIn("heavy lock (unity): waiting", proc.stdout.read())

    def test_legacy_and_resource_callers_exclude_each_other(self):
        for held_resource, next_resource in [("blender", None), (None, "unity"), ("unity", "gpu"), ("gpu", "blender")]:
            with self.subTest(held=held_resource, waiting=next_resource):
                with heavy.held("first", resource=held_resource):
                    env = dict(os.environ, CE_HEAVY_HELD="")
                    args = [sys.executable, str(SCRIPT), "run"]
                    if next_resource:
                        args += ["--resource", next_resource]
                    proc = subprocess.Popen(args + ["--", sys.executable, "-c", "pass"], env=env,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    try:
                        time.sleep(.2)
                        self.assertIsNone(proc.poll(), "migration/GPU barrier must block")
                    except BaseException:
                        proc.terminate(); proc.wait(timeout=5)
                        raise
                self.assertEqual(proc.wait(timeout=10), 0)
                output = proc.stdout.read()
                self.assertIn("first", output, "waiting on the gate must name the actual holder")
                proc.stdout.close()

    def test_legacy_parent_covers_resource_children(self):
        with heavy.held("legacy"):
            self.assertIsNotNone(heavy.skipped(resource="blender"))

    def test_nested_resources_remember_both_and_reject_gpu_upgrade(self):
        with heavy.held("unity", resource="unity"):
            with heavy.held("blender", resource="blender"):
                self.assertIsNotNone(heavy.skipped(resource="unity"))
            with self.assertRaisesRegex(ValueError, "gpu lock before"):
                heavy.HeavyLock("upgrade", resource="gpu").acquire()

    def test_child_on_another_resource_queues_for_it(self):
        with heavy.held("regression", "/a", resource="unity"):
            self.assertEqual(os.environ["CE_HEAVY_HELD"], "unity")
            self.assertIsNone(heavy.skipped(resource="blender"))  # queues for blender
            self.assertIsNotNone(heavy.skipped(resource="unity"))  # same resource: inside the parent
            with self.assertRaisesRegex(ValueError, "legacy shared lock"):
                heavy.skipped()  # a global step cannot upgrade a resource lock


class Nametag(LockCase):
    """0.24 "Nametag": status names who holds a lock (session, branch, client, running time, command) and who waits -
    read at status time, so holder lines of older tool versions are named too."""

    def repo(self, name=None, model=None, reserved_at=None):
        """A git checkout on branch claude/ref-0.14-eyebox, with a reservation when `name` is given."""
        path = self.base / "wt"
        path.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "-q", "-b", "claude/ref-0.14-eyebox", str(path)], check=True)
        if name:
            record = {"name": name, "branch": "claude/ref-0.14-eyebox", "reserved_at": reserved_at or time.time()}
            if model:
                record["model"] = model
            (path / ".git" / "ce-session.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_process_table_parses_ps_and_survives_failures(self):
        fake = mock.Mock(return_value=mock.Mock(stdout="  1     0 /sbin/launchd\n 42     1 node /usr/local/bin/claude\n"
                                                       "garbage\n 43    42\n"))
        self.assertEqual(heavy.process_table(fake), {1: (0, "/sbin/launchd"), 42: (1, "node /usr/local/bin/claude"),
                                                     43: (42, "")})
        self.assertEqual(heavy.process_table(mock.Mock(side_effect=OSError("no ps"))), {})

    def test_client_is_the_first_known_ancestor(self):
        table = {10: (1, "/Applications/Antigravity.app/Contents/MacOS/Antigravity"), 20: (10, "zsh -l"),
                 30: (20, "node /opt/homebrew/bin/codex exec"), 40: (30, "python3 heavy_lock.py run")}
        self.assertEqual(heavy.client_of(40, table), "Codex")
        self.assertEqual(heavy.client_of(20, table), "Antigravity")
        self.assertIsNone(heavy.client_of(99, table))
        self.assertIsNone(heavy.client_of(5, {5: (6, "a"), 6: (5, "b")}), "a cycle ends")

    def test_children_nearest_first(self):
        table = {7: (1, "heavy_lock run"), 8: (7, "sh -c blender"), 9: (8, "blender -b scene.blend"), 11: (9, "worker")}
        self.assertEqual(heavy.children_of(7, table), ["sh -c blender", "blender -b scene.blend"])
        self.assertEqual(heavy.children_of(11, table), [])

    def test_session_from_the_reservation_else_the_branch(self):
        self.assertEqual(heavy.session_of(None), {})
        self.assertEqual(heavy.session_of(self.repo()), {"branch": "claude/ref-0.14-eyebox"})
        found = heavy.session_of(self.repo("REF 0.14 „Eyebox“", "Opus 5.5 high"))
        self.assertEqual(found, {"branch": "claude/ref-0.14-eyebox", "name": "REF 0.14 „Eyebox“", "model": "Opus 5.5 high"})
        old = heavy.session_of(self.repo("Old", reserved_at=time.time() - 4 * 24 * 3600))
        self.assertTrue(old["stale"])
        self.assertEqual(heavy.session_of(str(self.base / "missing")), {})

    def test_status_names_session_branch_command_and_children(self):
        checkout = self.repo("REF 0.14 „Eyebox“", "Opus 5.5 high")
        me = os.getpid()
        table = {me: (1, "python3 heavy_lock.py run --resource gpu -- blender -b"), me + 1: (me, "blender -b ref.blend")}
        self.assertEqual(heavy.status_lines(table), ["shared   free", "unity    free", "blender  free", "gpu      free"])
        with heavy.held("Blender", checkout, resource="gpu"):
            lines = heavy.status_lines(table)
        text = "\n".join(lines)
        self.assertTrue(lines[0].startswith("shared   barrier, held exclusively by the gpu run"), lines[0])
        self.assertIn("gpu      Blender (PID %d, %s" % (me, checkout), text)
        self.assertIn("session: REF 0.14 „Eyebox“ (Opus 5.5 high) · branch claude/ref-0.14-eyebox; running 0 min", text)
        self.assertIn("command: python3 heavy_lock.py run --resource gpu -- blender -b", text)
        self.assertIn("runs:    blender -b ref.blend", text)

    def test_older_holder_lines_and_unknown_pids(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        path = Path(heavy.lock_path("unity"))
        with path.open("a+") as handle:
            try_lock(handle)
            handle.write(json.dumps({"pid": 999999, "what": "regression", "checkout": "/nowhere", "since": "2026-10-24 21:23:26"}))
            handle.flush()
            text = "\n".join(heavy.status_lines({1: (0, "init")}))
            self.assertIn("unity    regression (PID 999999, /nowhere, since 2026-10-24 21:23:26)", text)
            self.assertIn("no reserved session", text)
            self.assertIn("PID 999999 is not in the process table", text)
            handle.seek(0); handle.truncate(); handle.write("legacy holder"); handle.flush()
            self.assertEqual(heavy.details("unity", {}), [], "a foreign line names no details")
            self.assertIn("unity    legacy holder", heavy.status_lines({}))

    def test_waiting_runs_are_listed_under_their_resource(self):
        import ce_lock_status
        checkout = self.repo("DS Patch release", "Opus 5.5 high")
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        record = ce_lock_status.announce(heavy.lock_path("blender"), "blender", "build_all.py --release", checkout,
                                         heavy.lock_path())
        self.assertIsNotNone(record, "telemetry needs ps to read the start time of this process")
        self.addCleanup(ce_lock_status.forget, record)
        lines = heavy.status_lines({})
        index = lines.index("blender  free")
        self.assertTrue(lines[index + 1].strip().startswith("waiting: build_all.py --release – session: DS Patch release "
                                                            "(Opus 5.5 high) · branch claude/ref-0.14-eyebox, since "), lines)
        self.assertTrue(lines[index + 1].endswith(" UTC"))
        self.assertNotIn("waiting", "\n".join(lines[:index]), "only under its own resource")
