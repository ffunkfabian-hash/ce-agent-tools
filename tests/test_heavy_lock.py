"""heavy_lock.py of ce-shared: one heavy run at a time on the machine. Tests taken over from Cube Escape
(Tools/tests/test_heavy_lock.py), the robot generator and Toolgen (tests/test_heavy_lock.py), minus what needs their
repositories (quiet.py, run_testbed.sh, Blender). Run: python3 -m unittest discover -s tests"""
import fcntl
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
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
        holder = self.cli("run", "--what", "baseline.sh", "--checkout", "/x/first", "--", sys.executable, "-c",
                          "import time; print('go', flush=True); time.sleep(1.5)", stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "go")
            self.assertIn("baseline.sh", heavy.holder())
            self.assertEqual(heavy.holder_pid(), holder.pid)
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
                                        env=os.environ.copy()).stdout.strip(), "free")
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
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
        os.kill(command_pid, signal.SIGKILL)  # the command dies, the grandchild goes on
        time.sleep(0.4)
        self.assertIsNone(wrapper.poll(), "the wrapper still waits for the grandchild")
        self.assertFalse(marker.exists())
        self.assertFalse(self.free(), "the lock is held while the grandchild runs")
        self.assertNotEqual(wrapper.wait(timeout=10), 0, "the killed command stays a failure")
        self.assertTrue(marker.exists(), "the lock was held until the grandchild ended")
        self.assertTrue(self.free())

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
        for number, handler in ((sig.SIGTERM, sig.SIG_DFL), (sig.SIGHUP, sig.SIG_DFL),
                                (sig.SIGINT, sig.default_int_handler)):
            real(number, handler)

    def test_run_group_passes_env(self):
        out = self.base / "env.txt"
        code = heavy.run_group([sys.executable, "-c", f"import os; open({str(out)!r}, 'w').write(os.environ['PROBE'])"],
                               env=dict(os.environ, PROBE="42"))
        self.assertEqual(code, 0)
        self.assertEqual(out.read_text(), "42")


if __name__ == "__main__":
    unittest.main()
