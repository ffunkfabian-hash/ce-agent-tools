"""Regression coverage for the audit of the workflow rollout."""
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "plugins/ce-shared/tools"
sys.path.insert(0, str(TOOLS))
import ce_common
import ce_judge
import ce_nightly
import ce_report
import ce_search
import ce_sync


class CrosscurrentTests(unittest.TestCase):
    def test_grouped_cli_routes_and_keeps_legacy_aliases(self):
        for args, expected in [(["codex", "judge", "--help"], "--base"),
                               (["session", "topics", "--help"], "--topics"),
                               (["codex", "quota", "--help"], None),
                               (["evidence", "diff", "--help"], "--out"),
                               (["search", "index", "--help"], "--repo"),
                               (["judge", "--help"], "--base"),
                               (["evidence", "clip", "--help"], "--fps")]:
            if expected is None:
                continue  # quota intentionally has no argparse help and reads real account state
            result = subprocess.run(["sh", str(TOOLS / "ce"), *args], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(expected, result.stdout)
        result = subprocess.run(["sh", str(TOOLS / "ce"), "evidence", "bad"], capture_output=True)
        self.assertEqual(result.returncode, 2)

    def test_reviewer_receives_diff_and_process_failure_cannot_approve(self):
        names = ["a.py"]
        def git(*args, **kwargs):
            return "abc" if args[0] == "rev-parse" else ""
        for returncode, output, expected in [(0, "Freigabe", 0), (1, "Freigabe", 1), (0, "Freigabe\nNachbessern", 1)]:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ce_judge, "git", side_effect=git), \
                 mock.patch.object(ce_judge, "diff_of", return_value=("+changed", names)), \
                 mock.patch.object(ce_judge.shutil, "which", return_value="codex"), \
                 mock.patch.object(ce_judge.subprocess, "run", return_value=subprocess.CompletedProcess([], returncode, output, "")) as run, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ce_judge.main(["--repo", tmp, "--base", "main"]), expected)
                self.assertEqual(run.call_args.args[0][-1], "-")
                self.assertIn("+changed", run.call_args.kwargs["input"])
                self.assertIn("Checkliste", run.call_args.kwargs["input"])

    def test_search_prunes_worktrees_library_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ["docs/a.md", "Library/no.md", ".claude/worktrees/other/no.md"]:
                path = root / rel; path.parent.mkdir(parents=True, exist_ok=True); path.write_text("words")
            (root / "docs/linked.md").symlink_to(root / "Library/no.md")
            self.assertEqual([p.relative_to(root).as_posix() for p in ce_search.source_files(root)], ["docs/a.md"])

    def test_sync_detects_deleted_schema_or_changed_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); config = {}
            ce_sync.sync(root, config, TOOLS)
            target = root / "scripts/ce_tools"
            next((target / "contracts").glob("*.json")).unlink()
            next((target / "agents").glob("*.md")).write_text("changed")
            findings = ce_sync.check(root, config, TOOLS)
            self.assertEqual(len(findings), 2, findings)

    def test_local_checkout_aliases_find_actual_mac_paths(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            home = Path(tmp)
            for rel in ["Claude/Cube Escape", "cubegen", "Claude/cubeescape-reference"]:
                (home / rel / ".git").mkdir(parents=True)
            roots = ce_common.standard_repos(["cubeescape", "cubeescape-cubegen", "cubeescape-motiongen"], home)
            self.assertEqual(len(roots), 3)

    def test_nightly_dry_run_never_indexes_and_all_skipped_is_not_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            (root / ".ce-repo.json").write_text('{"check": {"nightly": ["true"]}}')
            with mock.patch.object(ce_nightly, "LOG_DIR", Path(tmp) / "logs"), \
                 mock.patch.object(ce_nightly, "notify") as notify, mock.patch.object(ce_search, "build") as build, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ce_nightly.main(["--repo", str(root), "--dry-run"]), 0)
                build.assert_not_called(); notify.assert_not_called()
                (root / ".ce-repo.json").write_text('{"check": {}}')
                self.assertEqual(ce_nightly.main(["--repo", str(root)]), 2)

    def test_nightly_issue_deduplicates(self):
        with mock.patch.object(ce_nightly.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '[{"title":"Nachtlauf fehlgeschlagen"}]', '')) as run:
            ce_nightly.report_issue(Path('.'), ['error'], lambda text: None)
            self.assertEqual(run.call_count, 1)

    def test_weekly_report_filters_records_by_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); folder = root / "project"; folder.mkdir()
            path = folder / "session.jsonl"
            records = [{"type": "assistant", "timestamp": "2000-01-01T00:00:00Z"},
                       {"type": "assistant", "timestamp": dt.datetime.now(dt.timezone.utc).isoformat()}]
            path.write_text("\n".join(json.dumps(r) for r in records))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ce_report.main(["--since", "7d", "--root", str(root), "--json"]), 0)
            self.assertEqual(json.loads(out.getvalue())[0]["messages"], 1)

    def test_rerun_cannot_mask_failure_of_another_check_command(self):
        import ce_check
        with mock.patch.object(ce_check, "run_one", side_effect=[{"code": 1}, {"code": 0}]) as run:
            result = ce_check.run_mode(Path('.'), {"check": {"full": [["test"], ["docs"]], "rerun_failed": ["test", "--failed"]}}, "full")
            self.assertFalse(result["green"])
            self.assertEqual(run.call_count, 2)

    def test_static_config_version_is_supported(self):
        self.assertEqual(ce_common.read_version(Path('.'), {"version": "0.11.20"}), "0.11.20")

    def test_search_identity_survives_worktree_folder_names(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CE_SEARCH_EMBED": "0"}):
            root = Path(tmp) / "random-worktree"; root.mkdir()
            (root / ".ce-repo.json").write_text('{"name":"stable-repo"}')
            (root / "LESSONS.md").write_text("# Lehre\nStable identity unique token")
            index = Path(tmp) / "index.sqlite"
            ce_search.build([root], index, log=lambda line: None)
            hits = ce_search.search("identity", 8, "stable-repo", None, index)
            self.assertTrue(hits)
            self.assertEqual(hits[0]["repo"], "stable-repo")

    def test_contract_validator_checks_extra_properties_and_numeric_schema_order(self):
        import ce_contract
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        self.assertTrue(ce_contract.validate({"surprise": 1}, schema, schema))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ce_contract, "CONTRACTS", Path(tmp)):
            for version in (4, 10):
                (Path(tmp) / f"unity_contract.v{version}.json").write_text("{}")
            self.assertEqual(ce_contract.schema_for({"format": 10}, Path("unity_contract.json")), "unity_contract.v10")

    def test_scheduler_retains_issue_opt_in_and_refuses_duplicate_legacy_schedule(self):
        import plistlib
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ce_nightly, "HOME", Path(tmp)), \
             mock.patch.object(ce_nightly, "LOG_DIR", Path(tmp) / "logs"), \
             mock.patch.object(ce_nightly.shutil, "which", return_value=None), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ce_nightly.install(3, report_issues=True), 2)
            data = plistlib.loads(ce_nightly.plist_path().read_bytes())
            self.assertIn("--report-issues", data["ProgramArguments"])
            old = Path(tmp) / "Library/LaunchAgents/com.cubeescape.nightly.plist"
            old.write_text("legacy")
            self.assertEqual(ce_nightly.install(3), 2)
            self.assertEqual(old.read_text(), "legacy")

    def test_lint_distinguishes_policy_flags_and_repo_specific_headings(self):
        import ce_lint
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("# Agenten\n## Eigener Ablauf\n")
            self.assertEqual(ce_lint.check_limits(root, {"limits": {"no_media": True}}), [])
            self.assertTrue(ce_lint.check_sections(root))
            self.assertEqual(ce_lint.check_sections(root, {"docs": {"agent_sections": ["Eigener Ablauf"]}}), [])
            self.assertTrue(ce_lint.check_sections(root, {"docs": {"agent_sections": "invalid"}}))

    def test_reviewer_includes_tracked_working_tree_only_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def run(*args):
                return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True, text=True)
            run("init", "-q", "-b", "main")
            (root / "only_local.py").write_text("old = 1\n")
            run("add", "only_local.py"); run("commit", "-qm", "base")
            (root / "only_local.py").write_text("new = 2\n")
            diff, names = ce_judge.diff_of("main", None, root)
            self.assertEqual(names, ["only_local.py"])
            self.assertIn("+new = 2", diff)

    def test_scheduler_reports_launchd_activation_failure(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ce_nightly, "HOME", Path(tmp)), \
             mock.patch.object(ce_nightly, "LOG_DIR", Path(tmp) / "logs"), \
             mock.patch.object(ce_nightly.shutil, "which", return_value="launchctl"), \
             mock.patch.object(ce_nightly.subprocess, "run", side_effect=[
                 subprocess.CompletedProcess([], 113, "", "not loaded"),
                 subprocess.CompletedProcess([], 5, "", "bootstrap failed"),
                 subprocess.CompletedProcess([], 113, "", "not loaded")]), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ce_nightly.install(3), 1)
            self.assertIn("nicht aktiviert", out.getvalue())

    def test_registry_aliases_resolve_one_checkout(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            home = Path(tmp); root = home / 'Claude/cubeescape-nexogen'
            (root / '.git').mkdir(parents=True)
            (home / 'Claude/cubeescape-rules').symlink_to(root, target_is_directory=True)
            (root / 'repos.json').write_text(json.dumps({'schema_version': 1, 'repos': [
                {'github': 'owner/cubeescape-nexogen', 'paths': ['~/Claude/cubeescape-nexogen'], 'aliases': ['cubeescape-rules']}]}))
            self.assertEqual(ce_common.standard_repos(['cubeescape-nexogen', 'cubeescape-rules'], home), [root.resolve()])

    def test_nightly_install_preserves_explicit_selection(self):
        import plistlib
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ce_nightly, 'HOME', Path(tmp)), \
             mock.patch.object(ce_nightly, 'LOG_DIR', Path(tmp) / 'logs'), \
             mock.patch.object(ce_nightly.shutil, 'which', return_value=None), contextlib.redirect_stdout(io.StringIO()):
            ce_nightly.main(['install', '--repo', tmp, '--hour', '3'])
            plist = plistlib.loads(ce_nightly.plist_path().read_bytes())
            self.assertEqual(plist['ProgramArguments'][2:], ['--repo', str(Path(tmp).resolve())])

    def test_failed_uninstall_keeps_plist_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ce_nightly, 'HOME', Path(tmp)), \
             mock.patch.object(ce_nightly.shutil, 'which', return_value='launchctl'), \
             mock.patch.object(ce_nightly.subprocess, 'run', side_effect=[
                 subprocess.CompletedProcess([], 0, '', ''),
                 subprocess.CompletedProcess([], 5, '', 'denied'),
                 subprocess.CompletedProcess([], 0, '', '')]), contextlib.redirect_stdout(io.StringIO()):
            path = ce_nightly.plist_path(); path.parent.mkdir(parents=True); path.write_text('keep')
            self.assertEqual(ce_nightly.uninstall(), 1)
            self.assertEqual(path.read_text(), 'keep')

    def test_antigravity_transcript_is_parsed_by_ce_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'conv/.system_generated/logs/transcript.jsonl'
            path.parent.mkdir(parents=True)
            records = [
                {'type': 'USER_INPUT', 'created_at': '2026-10-21T18:00:00Z', 'content': 'hi'},
                {'type': 'PLANNER_RESPONSE', 'created_at': '2026-10-21T18:00:01Z', 'tool_calls': [
                    {'name': 'run_command', 'args': {'CommandLine': json.dumps('./check')}},
                    {'name': 'call_mcp_tool', 'args': {'ToolName': json.dumps('ce_search')}},
                    {'name': 'edit_file', 'args': {'Path': json.dumps('a.py')}},
                ]},
                {'type': 'GENERIC', 'created_at': '2026-10-21T18:00:02Z', 'content': 'ok'},
            ]
            path.write_text('\n'.join(json.dumps(r) for r in records))
            info = ce_report.analyse(path)
            self.assertEqual((info['messages'], info['checks'], info['searches'], info['tools']['Edit']), (2, 1, 1, 1))
