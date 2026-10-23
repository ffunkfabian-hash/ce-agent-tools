import importlib.util
import fcntl
import os
import time
from types import ModuleType, SimpleNamespace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import venv
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "integrations/blender/ce_blender_tools"
TOOLS = ROOT / "plugins/ce-shared/tools"
spec = importlib.util.spec_from_file_location("blender_addon_jobs", PACKAGE / "jobs.py")
jobs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jobs)


class BlenderAddonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="odd ' $ addon-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / "profile.json"
        self.profile.write_text(json.dumps({"format": 1, "generator": "test", "unit_scale": 1,
                                           "bones": [], "materials": {}, "images": {}}), encoding="utf-8")

    def prepare(self, **overrides):
        options = dict(tools_root=TOOLS, python=sys.executable, blender="blender", cache=self.root / "results",
                       kind="profile_preflight", profile=self.profile)
        options.update(overrides)
        return jobs.prepare_request(**options)

    def test_current_scene_requires_saved_clean_file_and_never_saves(self):
        self.assertEqual(jobs.current_asset("/saved.blend", True, False), "/saved.blend")
        for args in [("", False, False), ("/saved.blend", True, True)]:
            with self.assertRaisesRegex(ValueError, "speichern"):
                jobs.current_asset(*args)

    def test_argv_preserves_special_characters_and_each_job_is_fresh(self):
        command, first = self.prepare()
        _, second = self.prepare()
        self.assertIsInstance(command, list)
        self.assertEqual(command[-1], str(first / "request.json"))
        self.assertNotEqual(first, second)
        self.assertEqual(json.loads((first / "request.json").read_text())["profile"], str(self.profile.resolve()))
        self.assertIn("' $", command[-1])

    def test_selected_virtual_environment_keeps_its_packages_for_worker_and_supervisor(self):
        environment = self.root / "selected environment"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
        python = environment / "bin" / "python"
        purelib = subprocess.run([str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                 capture_output=True, text=True, check=True).stdout.strip()
        (Path(purelib) / "ce_selected_environment_marker.py").write_text("VALUE = 'selected'\n")
        asset = self.root / "asset.blend"
        asset.touch()
        for kind in ("profile_preflight", "preflight"):
            with self.subTest(kind=kind):
                command, directory = self.prepare(python=python, kind=kind, asset=asset)
                probe = subprocess.run([command[0], "-c", "import ce_selected_environment_marker as m; print(m.VALUE)"],
                                       capture_output=True, text=True, timeout=15)
                self.assertEqual(probe.returncode, 0, probe.stderr)
                self.assertEqual(probe.stdout.strip(), "selected")
                if kind == "preflight":
                    self.assertEqual(command[command.index("--") + 1], command[0])
                else:
                    job = jobs.Job(command, directory)
                    job.process.wait(timeout=15)
                    self.assertTrue(job.poll()["ok"], (directory / "process.log").read_text())

    def test_profile_check_uses_existing_core_in_real_external_python(self):
        command, directory = self.prepare()
        job = jobs.Job(command, directory)
        job.process.wait(timeout=15)
        result = job.poll()
        self.assertTrue(result["ok"], (directory / "process.log").read_text())
        self.assertEqual(result["summary"], "Profil gültig: test")
        self.assertFalse((directory / "preflight.json").exists())

    def test_invalid_core_profile_becomes_visible_result(self):
        self.profile.write_text('{"format": 1}', encoding="utf-8")
        command, directory = self.prepare()
        job = jobs.Job(command, directory)
        job.process.wait(timeout=15)
        self.assertFalse(job.poll()["ok"])
        self.assertIn("fehlende Schlüssel", job.poll()["summary"])
        self.assertIn("ProfileError", (directory / "process.log").read_text())

    def test_turnaround_worker_preserves_explicit_media_options(self):
        spec = importlib.util.spec_from_file_location("blender_addon_worker", PACKAGE / "worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        turnaround = Mock()
        turnaround.main.return_value = 0
        request = dict(tools_root=str(TOOLS), kind="turnaround", profile=str(self.profile), blender="blender path",
                       asset="odd ' $.blend", frames=8, resolution=256, clip=True, fps=12)
        with patch.dict(sys.modules, {"ce_blender_preflight": Mock(), "ce_turnaround": turnaround}):
            result = worker.run(request, self.root)
        self.assertTrue(result["ok"])
        argv = turnaround.main.call_args.args[0]
        self.assertEqual(argv[argv.index("--input") + 1], "odd ' $.blend")
        self.assertEqual(argv[argv.index("--frames") + 1], "8")
        self.assertEqual(argv[argv.index("--fps") + 1], "12")
        self.assertIn("--clip", argv)

    def test_turnaround_profile_uses_turnaround_schema(self):
        self.profile.write_text('{"format": 1}', encoding="utf-8")
        command, directory = self.prepare(kind="profile_turnaround")
        job = jobs.Job(command, directory)
        job.process.wait(timeout=15)
        result = job.poll()
        self.assertFalse(result["ok"])
        self.assertIn("camera", result["summary"])

    def test_process_is_nonblocking_no_shell_and_in_own_group(self):
        command, directory = self.prepare()
        process = Mock()
        process.poll.return_value = None
        with patch.object(jobs.subprocess, "Popen", return_value=process) as popen:
            job = jobs.Job(command, directory)
        self.addCleanup(job.log.close)
        self.assertIsNone(job.poll())
        self.assertEqual(popen.call_args.args[0], command)
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        if jobs.os.name == "posix":
            self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_missing_and_malformed_result_does_not_look_successful(self):
        command, directory = self.prepare()
        process = Mock()
        process.poll.return_value = 0
        with patch.object(jobs.subprocess, "Popen", return_value=process):
            job = jobs.Job(command, directory)
        self.assertFalse(job.poll()["ok"])
        (directory / "result.json").write_text('{"ok": true}', encoding="utf-8")
        self.assertFalse(job.poll()["ok"])

    def test_only_asset_jobs_are_wrapped_in_existing_lock_supervisor(self):
        profile_command, _ = self.prepare()
        self.assertNotIn(str(TOOLS / "heavy_lock.py"), profile_command)
        asset = self.root / "asset.blend"
        asset.touch()
        for kind in ("preflight", "turnaround"):
            command, _ = self.prepare(kind=kind, asset=asset)
            self.assertEqual(command[1:6], [str(TOOLS / "heavy_lock.py"), "run", "--resource", "blender", "--what"])
            self.assertIn(str(PACKAGE / "worker.py"), command)

    def test_cancel_signals_only_supervisor_and_keeps_job_log_until_reaped(self):
        command, directory = self.prepare()
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch.object(jobs.subprocess, "Popen", return_value=process):
            job = jobs.Job(command, directory)
        self.addCleanup(job.log.close)
        with patch.object(jobs.os, "killpg", create=True) as killpg:
            job.cancel()
            job.cancel()
        process.terminate.assert_called_once_with()
        killpg.assert_not_called()
        self.assertTrue(job.cancelling)
        self.assertFalse(job.log.closed)
        self.assertIsNone(job.poll())
        process.poll.return_value = -15
        self.assertFalse(job.poll()["ok"])
        self.assertTrue(job.log.closed)

    def test_real_cancel_holds_isolated_lock_until_delayed_child_cleanup(self):
        asset_dir = self.root / "assets"
        asset_dir.mkdir()
        asset = asset_dir / "asset.blend"
        asset.touch()
        ready, terminated, ended = [self.root / name for name in ("ready", "terminated", "ended")]
        renderer = self.root / "fake-blender"
        renderer.write_text("#!" + sys.executable + "\n" + f'''import signal, time
from pathlib import Path
stop = False
def cleanup_signal(signum, frame):
    global stop
    Path({str(terminated)!r}).touch()
    stop = True
signal.signal(signal.SIGTERM, cleanup_signal)
Path({str(ready)!r}).touch()
while not stop:
    time.sleep(0.01)
time.sleep(0.7)
Path({str(ended)!r}).touch()
''', encoding="utf-8")
        renderer.chmod(0o700)
        command, directory = self.prepare(kind="preflight", asset=asset, blender=renderer)
        env = {key: value for key, value in os.environ.items() if key not in ("CE_HEAVY_HELD", "CE_HEAVY_LOCK", "CE_HEAVY_RESOURCE")}
        env["CE_HEAVY_LOCK_FILE"] = str(self.root / "heavy-run.lock")
        with patch.dict(os.environ, env, clear=True):
            job = jobs.Job(command, directory)
        def wait_until(predicate, seconds=10):
            deadline = time.monotonic() + seconds
            while not predicate() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(predicate(), (directory / "process.log").read_text())
        def lock_free():
            with (self.root / "heavy-blender.lock").open("a+") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except BlockingIOError:
                    return False
        try:
            wait_until(ready.exists)
            self.assertFalse(lock_free())
            start = time.monotonic()
            job.cancel()
            self.assertLess(time.monotonic() - start, 0.2, "cancel must not block the UI")
            wait_until(terminated.exists)
            self.assertFalse(ended.exists())
            self.assertIsNone(job.poll(), "supervisor must still hold its child-group barrier")
            self.assertFalse(job.log.closed)
            self.assertFalse(lock_free(), "renderer cleanup must finish before releasing the lock")
            wait_until(ended.exists)
            job.process.wait(timeout=10)
            self.assertTrue(lock_free())
            self.assertFalse(job.poll()["ok"])
            self.assertTrue(job.log.closed)
        finally:
            job.cancel()
            job.process.wait(timeout=15)
            job.poll()

    def test_real_ignored_term_escalates_only_worker_group_and_reaps_supervisor(self):
        assets = self.root / "stubborn-assets"
        assets.mkdir()
        asset = assets / "asset.blend"
        asset.touch()
        ready = self.root / "stubborn-ready"
        renderer = self.root / "stubborn-blender"
        renderer.write_text("#!" + sys.executable + "\n" + f'''import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(ready)!r}).write_text(str(os.getpid()))
while True:
    time.sleep(0.01)
''', encoding="utf-8")
        renderer.chmod(0o700)
        command, directory = self.prepare(kind="preflight", asset=asset, blender=renderer)
        env = {key: value for key, value in os.environ.items() if key not in ("CE_HEAVY_HELD", "CE_HEAVY_LOCK", "CE_HEAVY_RESOURCE")}
        env["CE_HEAVY_LOCK_FILE"] = str(self.root / "stubborn-run.lock")
        with patch.dict(os.environ, env, clear=True):
            job = jobs.Job(command, directory, cancel_grace=0.2)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), (directory / "process.log").read_text())
            identity = json.loads((directory / "worker-identity.json").read_text())
            self.assertEqual(identity["supervisor_pid"], job.process.pid)
            self.assertNotEqual(identity["pgid"], job.process.pid)
            job.cancel()
            self.assertIsNone(job.poll())
            self.assertFalse(job.escalated)
            result = None
            while result is None and time.monotonic() < deadline:
                result = job.poll()
                time.sleep(0.01)
            self.assertIsNotNone(result, "own worker group must be stopped within its cancellation grace")
            self.assertTrue(job.escalated)
            self.assertFalse(result["ok"])
            self.assertNotEqual(job.process.returncode, -9, "the supervisor must never receive KILL")
            self.assertTrue(job.log.closed)
            with (self.root / "stubborn-blender.lock").open("a+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            job.cancel()
            # Continue the same nonblocking cleanup path even on assertion failures.
            deadline = time.monotonic() + 10
            while job.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            job.process.wait(timeout=3)

    def test_escalation_rejects_unattested_group_identity(self):
        asset = self.root / "asset.blend"
        asset.touch()
        command, directory = self.prepare(kind="preflight", asset=asset)
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch.object(jobs.subprocess, "Popen", return_value=process):
            job = jobs.Job(command, directory, cancel_grace=0)
        self.addCleanup(job.log.close)
        identity = {"job_id": job.request["job_id"], "supervisor_pid": 99999, "pid": 54321, "pgid": 54321}
        path = directory / "worker-identity.json"
        path.write_text(json.dumps(identity))
        with patch.object(jobs.os, "killpg") as killpg:
            job.cancel()
            job.poll()
            killpg.assert_not_called()
            identity["supervisor_pid"] = process.pid
            identity["job_id"] = "wrong-run"
            path.write_text(json.dumps(identity))
            job.poll()
            killpg.assert_not_called()
            identity["job_id"] = job.request["job_id"]
            path.write_text(json.dumps(identity))
            job.poll()
            killpg.assert_called_once_with(54321, jobs.signal.SIGKILL)

    def test_unregister_keeps_cleanup_timer_and_blocks_new_jobs_until_reaped(self):
        # Import only the UI wrapper against a minimal Blender API; no renderer/API duplication.
        bpy = ModuleType("bpy")
        props = ModuleType("bpy.props")
        for key in ("BoolProperty", "EnumProperty", "IntProperty", "PointerProperty", "StringProperty"):
            setattr(props, key, lambda **kw: None)
        bpy.types = SimpleNamespace(PropertyGroup=object, Operator=object, Panel=object,
                                    WindowManager=type("WindowManager", (), {"ce_blender_tools": None}))
        bpy.app = SimpleNamespace(timers=Mock(), binary_path="/test/blender")
        bpy.path = SimpleNamespace(abspath=lambda value: value)
        bpy.app.timers.is_registered.return_value = True
        bpy.utils = Mock()
        bpy.context = SimpleNamespace(window_manager=SimpleNamespace(windows=[]))
        package = ModuleType("ce_addon_test")
        package.__path__ = [str(PACKAGE)]
        spec = importlib.util.spec_from_file_location("ce_addon_test.addon", PACKAGE / "addon.py")
        addon = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"bpy": bpy, "bpy.props": props, "ce_addon_test": package, "ce_addon_test.jobs": jobs}):
            spec.loader.exec_module(addon)
            settings = SimpleNamespace(python=sys.executable, tools_root=str(TOOLS), cache=str(self.root),
                preflight_profile=str(self.profile), frames=1, resolution=256, clip=False, fps=12)
            context = SimpleNamespace(window_manager=SimpleNamespace(ce_blender_tools=settings))
            operator = addon.CE_OT_run_tool()
            operator.kind = "profile_preflight"
            with patch.object(addon, "prepare_request", return_value=(["python"], self.root)), patch.object(addon, "Job", return_value=Mock()):
                self.assertEqual(operator.execute(context), {"FINISHED"})
            bpy.app.timers.register.assert_called_once_with(addon._poll, first_interval=0.25, persistent=True)
            job = Mock(cancelling=True)
            job.poll.return_value = None
            addon._job = job
            addon.unregister()
            self.assertIs(addon._job, job)
            job.cancel.assert_called_once_with()
            bpy.app.timers.unregister.assert_not_called()
            self.assertFalse(addon.CE_OT_run_tool.poll(None))
            self.assertEqual(addon._poll(), 0.25)
            job.poll.return_value = {"ok": False, "summary": "Abgebrochen", "issues": []}
            self.assertIsNone(addon._poll())
            self.assertIsNone(addon._job)
            self.assertTrue(addon.CE_OT_run_tool.poll(None))

    def test_preflight_worker_delegates_asset_validation_and_preserves_issues(self):
        spec = importlib.util.spec_from_file_location("blender_addon_worker", PACKAGE / "worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        def preflight_main(argv):
            self.assertEqual(argv[:2], ["--profile", str(self.profile)])
            Path(argv[-1]).write_text('{"ok": false, "issues": ["bone/parent: Root"]}', encoding="utf-8")
            return 1
        preflight = Mock(main=preflight_main)
        request = dict(tools_root=str(TOOLS), kind="preflight", profile=str(self.profile), blender="safe binary", asset="odd ' $.blend")
        with patch.dict(sys.modules, {"ce_blender_preflight": preflight, "ce_turnaround": Mock()}):
            result = worker.run(request, self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"], ["bone/parent: Root"])


if __name__ == "__main__":
    unittest.main()
