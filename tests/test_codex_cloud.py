import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import os
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "codex_cloud", Path(__file__).resolve().parents[1] / "plugins" / "ce-shared" / "tools" / "ce_codex_cloud.py")
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)


class SubmitGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        prompt = Path(self.temp.name) / "auftrag.txt"
        prompt.write_text("Nur README.md lesen, nichts aendern.")
        self.args = SimpleNamespace(prompt_file=prompt, branch="claude/test",
            model="gpt-6-luna", effort="low", reason="Lesen", env="env_test",
            repo=Path(self.temp.name), dry_run=False, push_approved=True)

    @patch.object(cloud, "run")
    def test_no_approval_never_contacts_cloud(self, run):
        self.args.push_approved = False
        with self.assertRaisesRegex(ValueError, "Nutzerfreigabe"):
            cloud.submit(self.args)
        run.assert_not_called()

    @patch.object(cloud, "run")
    def test_main_rejected(self, run):
        self.args.branch = "main"
        with self.assertRaises(ValueError):
            cloud.submit(self.args)
        run.assert_not_called()

    @patch.object(cloud, "run")
    def test_dry_run_does_not_execute(self, run):
        self.args.dry_run = True
        with patch("builtins.print"):
            cloud.submit(self.args)
        run.assert_not_called()

    @patch.object(cloud, "run")
    def test_dirty_checkout_rejected(self, run):
        run.side_effect = [SimpleNamespace(stdout=""), SimpleNamespace(stdout="abc"),
                           SimpleNamespace(stdout=" M file")]
        with self.assertRaisesRegex(ValueError, "sauber"):
            cloud.submit(self.args)
        self.assertEqual(run.call_count, 3)

    @patch.object(cloud, "run")
    def test_unpushed_commit_rejected(self, run):
        run.side_effect = [SimpleNamespace(stdout=""), SimpleNamespace(stdout="abc"),
                           SimpleNamespace(stdout=""), SimpleNamespace(stdout="def refs/heads/claude/test")]
        with self.assertRaisesRegex(ValueError, "Remote-Branch"):
            cloud.submit(self.args)
        self.assertEqual(run.call_count, 4)

    @patch.object(cloud, "run")
    def test_submit_uses_one_attempt_and_literal_prompt(self, run):
        self.args.prompt_file.write_text("README.md: `danger` $(no) ; untouched")
        run.side_effect = [SimpleNamespace(stdout=""), SimpleNamespace(stdout="abc"),
                           SimpleNamespace(stdout=""), SimpleNamespace(stdout="abc refs/heads/claude/test"),
                           SimpleNamespace(stdout=""), SimpleNamespace(stdout="")]
        cloud.submit(self.args)
        command = run.call_args_list[-1].args[0]
        self.assertEqual(command[:3], ["codex", "cloud", "exec"])
        self.assertEqual(command[command.index("--attempts") + 1], "1")
        self.assertIn("`danger` $(no) ; untouched", command[-1])
        self.assertTrue(command[-1].startswith("gpt-6-luna, `low`, Lesen."))
        self.assertFalse(any("push" in c.args[0] for c in run.call_args_list))


class DoctorChecks(unittest.TestCase):
    @patch.object(cloud, "run")
    def test_empty_environments_fail_even_with_successful_login(self, run):
        run.side_effect = [SimpleNamespace(), SimpleNamespace(),
            subprocess.CalledProcessError(1, "codex", stderr=
                "Error: no cloud environments are available for this workspace")]
        with self.assertRaisesRegex(ValueError, "keine Umgebung für dieses Konto sichtbar"):
            cloud.doctor()
        self.assertEqual(run.call_count, 3)

    @patch.object(cloud, "run")
    def test_visible_environment_with_no_tasks_is_healthy(self, run):
        run.side_effect = [SimpleNamespace(), SimpleNamespace(),
            subprocess.CalledProcessError(1, "codex", stderr=
                "Error: environment 'ce-doctor-environment-probe' not found; "
                "run `codex cloud` to list available environments"),
            SimpleNamespace(stdout='{"tasks": []}')]
        with patch("builtins.print"):
            cloud.doctor()
        self.assertIn("--env", run.call_args_list[2].args[0])
        self.assertEqual(run.call_count, 4)

    @patch.object(cloud, "run")
    def test_network_or_auth_failure_is_not_reported_as_empty(self, run):
        run.side_effect = subprocess.CalledProcessError(1, "codex", stderr="HTTP 401")
        with self.assertRaisesRegex(ValueError, "HTTP 401"):
            cloud.check_environments()

    @patch.object(cloud, "run")
    def test_probe_label_coincidentally_exists(self, run):
        cloud.check_environments()
        run.assert_called_once()

    @patch.object(cloud.shutil, "which", return_value=None)
    @patch.object(cloud, "run")
    def test_cli_dry_run_without_installation_or_login(self, run, which):
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "prompt.txt"
            prompt.write_text("README.md lesen")
            argv = ["codex_cloud.py", "submit", "--env", "env_test",
                    "--repo", tmp, "--branch", "claude/test", "--prompt-file", str(prompt),
                    "--model", "gpt-6-luna", "--effort", "low", "--reason", "Lesen", "--dry-run"]
            with patch.object(cloud.sys, "argv", argv), patch("builtins.print"):
                self.assertEqual(cloud.main(), 0)
        run.assert_not_called()
        which.assert_not_called()
