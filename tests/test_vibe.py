"""ce_vibe runs against a fake vibe binary in throwaway git repos; no Mistral call, no network."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "plugins/ce-shared/tools"
sys.path.insert(0, str(TOOLS))
import ce_vibe

FAKE_VIBE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, subprocess, sys
    from pathlib import Path
    args = sys.argv[1:]
    value = lambda flag: args[args.index(flag) + 1]
    work, out, mode = Path(value("--workdir")), Path(value("--output-dir")), os.environ["FAKE_VIBE_MODE"]
    env = {k: os.environ.get(k) for k in ("GH_TOKEN", "GH_CONFIG_DIR", "GIT_CONFIG_COUNT")}
    env.update({k: v for k, v in os.environ.items() if k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))})
    record = {"argv": args, "cwd": os.getcwd(), "prompt": Path(value("--prompt-file")).read_text(encoding="utf-8"),
              "env": env,
              "app_before": (work / "app.py").read_text(),
              "files": sorted(p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file() and ".git" not in p.parts)}
    if mode == "hang":
        sleeper = [sys.executable, "-c", "import time; time.sleep(30)"] if os.name == "nt" else ["sh", "-c", "sleep 30"]
        child = subprocess.Popen(sleeper)
        Path(os.environ["FAKE_VIBE_RECORD"]).write_text(json.dumps({"child": child.pid}))
        print("teilweise", flush=True)
        sys.stderr.write("arbeite\\n")
        sys.stderr.flush()
        import time
        time.sleep(30)
    app = work / "app.py"
    app.write_text(app.read_text().replace("a - b", "a + b"))
    (work / "new.txt").write_text("neu\\n")
    (work / "cache.tmp").write_text("ignoriert\\n")
    if mode == "binary":
        (work / "img.bin").write_bytes(bytes(range(256)) * 4)
        (work / "notes.md").rename(work / "docs.md")
    if mode == "branch":
        subprocess.run(["git", "-C", str(work), "branch", "vibe-branch"])
    if mode == "commit":
        subprocess.run(["git", "-C", str(work), "-c", "user.name=v", "-c", "user.email=v@v", "commit", "-qam", "x"])
    if mode == "push":
        record["push"] = subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:refs/heads/vibe"],
                                        capture_output=True).returncode
    Path(os.environ["FAKE_VIBE_RECORD"]).write_text(json.dumps(record))
    limit = mode == "limit"
    out.mkdir(parents=True, exist_ok=True)
    (out / "export.json").write_text(json.dumps({
        "outcome": "turn_limit" if limit else "finished", "stop_reason": "limit" if limit else None, "error": None,
        "usage": {"input_tokens": 100, "output_tokens": 10}, "cost_usd": 0.0123, "steps": 3, "vibe_version": "3.0.4",
        "config": {"active_model": "mistral-medium-3.5"}, "exit_code": 3 if limit else 0}))
    print("Fertig: app.py korrigiert, Tests grün.")
    sys.exit(3 if limit else 0)
''')


GUARD_HOOKS = textwrap.dedent('''\
    [[hooks]]
    name = "ce-shared-guard"
    type = "pre_tool"
    match = "re:bash|process_start|git_bash|powershell|write_file|edit|mcp_.*"
    command = "if [ -f '<guard>' ]; then exec python3 '<guard>'; fi"
    ''')


def sh(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


class VibeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        sh(self.repo, "init", "-q", "-b", "main")
        (self.repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
        (self.repo / "notes.md").write_text("alt\n")
        (self.repo / ".gitignore").write_text("*.tmp\n")
        sh(self.repo, "add", ".")
        sh(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        sh(self.repo, "remote", "add", "origin", str(self.origin))
        fake = self.tmp / "vibe"
        fake.write_text(FAKE_VIBE, encoding="utf-8")
        fake.chmod(0o755)
        self.record = self.tmp / "record.json"
        (self.tmp / "prompt.txt").write_text("Fix add in app.py\nmehr Details\n")
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(ce_vibe, "STATE", self.tmp / "state").start()
        mock.patch.object(ce_vibe, "repo_hold", return_value=None).start()
        self.private = mock.patch.object(ce_vibe, "is_private", return_value=False).start()
        self.vibe_home = self.tmp / "vibe-home"
        self.vibe_home.mkdir()
        guard = self.tmp / "ce tools/hooks/vibe_guard.py"
        guard.parent.mkdir(parents=True)
        guard.write_text("pass\n")
        self.guard = guard
        # as_posix: a TOML basic string takes no raw backslashes (C:\Users); the same text as str() on POSIX
        self.guard_hooks = GUARD_HOOKS.replace("<guard>", guard.as_posix())
        (self.vibe_home / "hooks.toml").write_text(self.guard_hooks)
        mock.patch.dict(os.environ, {"CE_VIBE_BIN": str(fake), "FAKE_VIBE_RECORD": str(self.record),
                                     "FAKE_VIBE_MODE": "edit", "GH_TOKEN": "secret",
                                     "VIBE_HOME": str(self.vibe_home)}).start()

    def call(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ce_vibe.main(["run", "--prompt-file", str(self.tmp / "prompt.txt"), "--repo", str(self.repo),
                                 "--json", *extra])
        summary = json.loads(out.getvalue()) if out.getvalue().strip() else None
        return code, summary, err.getvalue()

    def worktrees(self):
        listing = subprocess.run(["git", "-C", str(self.repo), "worktree", "list"], capture_output=True, text=True)
        return len(listing.stdout.splitlines())

    def test_finished_run_returns_patch_and_removes_worktree(self):
        code, summary, _ = self.call()
        self.assertEqual(code, 0)
        self.assertEqual(summary["outcome"], "finished")
        self.assertEqual(sorted(summary["files"]), ["app.py", "new.txt"])  # cache.tmp is gitignored
        self.assertEqual((summary["insertions"], summary["deletions"]), (2, 1))
        self.assertEqual(summary["cost_usd"], 0.0123)
        self.assertEqual(summary["model"], "mistral-medium-3.5")
        self.assertIn("Fertig", summary["answer"])
        self.assertIsNone(summary["worktree"])
        self.assertEqual(self.worktrees(), 1)
        self.assertEqual((self.repo / "app.py").read_text(), "def add(a, b):\n    return a - b\n")
        patch = Path(summary["patch"])
        subprocess.run(["git", "-C", str(self.repo), "apply", str(patch)], check=True)
        self.assertIn("a + b", (self.repo / "app.py").read_text())
        run_dir = Path(summary["run_dir"])
        for name in ("prompt.txt", "answer.txt", "vibe.log", "summary.json", "export/export.json"):
            self.assertTrue((run_dir / name).is_file(), name)
        self.assertTrue(run_dir.name.endswith("fix-add-in-app-py"))

    def test_command_runs_headless_auto_approve_with_limits_in_worktree(self):
        self.call("--max-turns", "7", "--max-price", "0.30", "--time-limit", "90", "--model", "devstral")
        record = json.loads(self.record.read_text())
        argv = record["argv"]
        for flag, value in (("--agent", "auto-approve"), ("--max-turns", "7"), ("--max-price", "0.30"),
                            ("--time-limit", "90"), ("--model", "devstral"), ("--output", "text")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--trust", argv)
        workdir = argv[argv.index("--workdir") + 1]
        self.assertEqual(record["cwd"], workdir)
        self.assertNotEqual(Path(workdir), self.repo)
        self.assertTrue(record["prompt"].startswith(ce_vibe.PREAMBLE))
        self.assertIn("Fix add in app.py", record["prompt"])

    def test_worker_cannot_push_and_has_no_gh_login(self):
        os.environ["FAKE_VIBE_MODE"] = "push"
        code, _, _ = self.call()
        record = json.loads(self.record.read_text())
        self.assertNotEqual(record["push"], 0)
        refs = subprocess.run(["git", "-C", str(self.origin), "for-each-ref"], capture_output=True, text=True).stdout
        self.assertEqual(refs, "")
        self.assertIsNone(record["env"]["GH_TOKEN"])
        self.assertNotEqual(record["env"]["GH_CONFIG_DIR"], None)
        # the push block sits behind whatever GIT_CONFIG_* entries the environment already has
        last = int(record["env"]["GIT_CONFIG_COUNT"]) - 1
        self.assertEqual(record["env"].get(f"GIT_CONFIG_KEY_{last}"), "remote.origin.pushurl")
        self.assertEqual(record["env"].get(f"GIT_CONFIG_VALUE_{last}"), "ce-vibe-no-push://blocked")
        self.assertEqual(code, 0)

    def test_worker_cannot_push_with_pre_existing_git_config(self):
        os.environ["FAKE_VIBE_MODE"] = "push"
        preset = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "diff.noprefix", "GIT_CONFIG_VALUE_0": "true",
                  "GIT_CONFIG_KEY_1": "color.diff", "GIT_CONFIG_VALUE_1": "always"}
        with mock.patch.dict(os.environ, preset):
            code, _, _ = self.call()
        record = json.loads(self.record.read_text())
        self.assertNotEqual(record["push"], 0)
        self.assertEqual(record["env"]["GIT_CONFIG_COUNT"], "3")
        # entries already set by the caller stay untouched, the push block is appended after them
        for key, value in preset.items():
            if key != "GIT_CONFIG_COUNT":
                self.assertEqual(record["env"].get(key), value)
        self.assertEqual(record["env"].get("GIT_CONFIG_KEY_2"), "remote.origin.pushurl")
        self.assertEqual(record["env"].get("GIT_CONFIG_VALUE_2"), "ce-vibe-no-push://blocked")
        self.assertEqual(code, 0)

    def test_uncommitted_changes_are_carried_but_not_in_patch(self):
        (self.repo / "notes.md").write_text("eigene Arbeit\n")
        (self.repo / "app.py").write_text("# Kopf\ndef add(a, b):\n    return a - b\n")
        code, summary, _ = self.call()
        self.assertEqual(code, 0)
        self.assertTrue(summary["carried_changes"])
        self.assertTrue(json.loads(self.record.read_text())["app_before"].startswith("# Kopf"))
        self.assertNotIn("notes.md", summary["files"])
        patch = Path(summary["patch"]).read_text()
        self.assertNotIn("Kopf", patch.split("@@")[0])
        subprocess.run(["git", "-C", str(self.repo), "apply", summary["patch"]], check=True)
        self.assertEqual((self.repo / "app.py").read_text(), "# Kopf\ndef add(a, b):\n    return a + b\n")
        self.assertEqual((self.repo / "notes.md").read_text(), "eigene Arbeit\n")

    def test_new_untracked_files_are_carried_but_not_in_patch(self):
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "helper.py").write_text("HELPER = 1\n")
        (self.repo / "local.tmp").write_text("ignoriert\n")
        code, summary, _ = self.call()
        self.assertEqual(code, 0)
        self.assertTrue(summary["carried_changes"])
        seen = json.loads(self.record.read_text())["files"]
        self.assertIn("pkg/helper.py", seen)
        self.assertNotIn("local.tmp", seen)
        self.assertEqual(sorted(summary["files"]), ["app.py", "new.txt"])

    def test_clean_base_leaves_own_changes_out(self):
        (self.repo / "app.py").write_text("# Kopf\ndef add(a, b):\n    return a - b\n")
        _, summary, _ = self.call("--clean-base")
        self.assertFalse(summary["carried_changes"])
        self.assertFalse(json.loads(self.record.read_text())["app_before"].startswith("# Kopf"))

    def test_turn_limit_keeps_patch_and_exits_3(self):
        os.environ["FAKE_VIBE_MODE"] = "limit"
        code, summary, _ = self.call()
        self.assertEqual(code, 3)
        self.assertEqual(summary["outcome"], "turn_limit")
        self.assertIn("app.py", summary["files"])

    def test_commit_by_vibe_is_warned_and_still_in_patch(self):
        os.environ["FAKE_VIBE_MODE"] = "commit"
        code, summary, _ = self.call()
        self.assertEqual(code, 0)
        self.assertTrue(any("committet" in w for w in summary["warnings"]))
        self.assertIn("app.py", summary["files"])
        branches = subprocess.run(["git", "-C", str(self.repo), "branch"], capture_output=True, text=True).stdout
        self.assertEqual(branches.split(), ["*", "main"])

    def test_hard_timeout_kills_process_group_and_cleans_up(self):
        os.environ["FAKE_VIBE_MODE"] = "hang"
        with mock.patch.dict(ce_vibe.DEFAULTS, {"kill_grace": 0}):
            code, summary, _ = self.call("--time-limit", "2")
        self.assertEqual(code, 1)
        self.assertIsNone(summary["exit_code"])
        self.assertTrue(any("abgebrochen" in w for w in summary["warnings"]))
        log = (Path(summary["run_dir"]) / "vibe.log").read_text()
        self.assertIn("arbeite", log)
        self.assertIn("harter Abbruch", log)
        self.assertNotIn("b'", log)
        self.assertEqual(summary["answer"], "teilweise")
        child = json.loads(self.record.read_text())["child"]
        for _ in range(50):  # the killed child is reaped by init shortly after
            if os.name == "nt":  # never os.kill(pid, 0) there: it is TerminateProcess (0.31)
                if not ce_vibe.heavy_lock.pid_alive(child):
                    break
            else:
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
            __import__("time").sleep(0.1)
        else:
            self.fail("child process of vibe survived the timeout")
        self.assertEqual(self.worktrees(), 1)

    def test_failure_after_worktree_creation_removes_it(self):
        with mock.patch.object(ce_vibe, "snapshot_tree", side_effect=ce_vibe.VibeError("kaputt")):
            code, summary, err = self.call()
        self.assertEqual((code, summary), (1, None))
        self.assertIn("kaputt", err)
        self.assertEqual(self.worktrees(), 1)
        self.assertFalse(self.record.exists())

    def test_binary_file_and_rename_apply_cleanly(self):
        os.environ["FAKE_VIBE_MODE"] = "binary"
        with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "diff.noprefix",
                                          "GIT_CONFIG_VALUE_0": "true", "GIT_CONFIG_KEY_1": "color.diff",
                                          "GIT_CONFIG_VALUE_1": "always"}):
            code, summary, _ = self.call()
        self.assertEqual(code, 0)
        self.assertEqual(sorted(summary["files"]), ["app.py", "docs.md", "img.bin", "new.txt", "notes.md"])
        subprocess.run(["git", "-C", str(self.repo), "apply", summary["patch"]], check=True)
        self.assertEqual((self.repo / "img.bin").read_bytes(), bytes(range(256)) * 4)
        self.assertEqual((self.repo / "docs.md").read_text(), "alt\n")
        self.assertFalse((self.repo / "notes.md").exists())

    def test_new_branch_is_warned(self):
        os.environ["FAKE_VIBE_MODE"] = "branch"
        _, summary, _ = self.call()
        self.assertTrue(any("Branches" in w for w in summary["warnings"]))

    def test_embedded_repo_is_skipped_with_warning(self):
        nested = self.repo / "vendor"
        nested.mkdir()
        sh(nested, "init", "-q")
        code, summary, _ = self.call()
        self.assertEqual(code, 0)
        self.assertTrue(any("vendor" in w for w in summary["warnings"]))

    def test_keep_leaves_worktree(self):
        _, summary, _ = self.call("--keep")
        self.assertTrue(Path(summary["worktree"]).is_dir())
        self.assertEqual(self.worktrees(), 2)

    def test_private_or_unknown_repo_is_refused_before_any_worktree(self):
        for visibility in (True, None):
            self.private.return_value = visibility
            code, summary, err = self.call()
            self.assertEqual(code, 1)
            self.assertIsNone(summary)
            self.assertIn("--allow-private", err)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.worktrees(), 1)
        self.private.return_value = True
        code, _, _ = self.call("--allow-private")
        self.assertEqual(code, 0)

    def test_run_without_the_guard_hook_is_refused_before_any_worktree(self):
        hooks = self.guard_hooks
        for text in ("", "[[hooks]]\nname = 'lint'\ntype = 'post_agent'\ncommand = 'make check'\n",
                     hooks.replace("pre_tool", "post_tool"), "not toml [", hooks.replace("vibe_guard.py", "moved/vibe_guard.py"),
                     hooks.replace("re:bash|process_start|git_bash|powershell|write_file|edit|mcp_.*", "write_file"),
                     hooks.replace("re:bash|process_start|git_bash|powershell|write_file|edit|mcp_.*", "bash")):
            (self.vibe_home / "hooks.toml").write_text(text)
            code, summary, err = self.call()
            self.assertEqual((code, summary), (1, None), text)
            self.assertIn("Vibe-Wächter fehlt", err)
            self.assertIn("--client-hooks=vibe", err)
        (self.vibe_home / "hooks.toml").unlink()
        self.assertEqual(self.call()[0], 1)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.worktrees(), 1)
        (self.vibe_home / "hooks.toml").write_text(hooks.replace("re:bash|process_start|git_bash|powershell|write_file|edit|mcp_.*",
                                                                 "*"))
        self.assertEqual(self.call()[0], 0)

    def test_a_project_hook_that_would_replace_the_guard_is_refused(self):
        (self.repo / ".vibe").mkdir()
        (self.repo / ".vibe" / "hooks.toml").write_text('[[hooks]]\nname = "ce-shared-guard"\ntype = "pre_tool"\ncommand = "true"\n')
        code, summary, err = self.call()
        self.assertEqual((code, summary), (1, None))
        self.assertIn("überschreibt den Vibe-Wächter", err)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.worktrees(), 1)
        (self.repo / ".vibe" / "hooks.toml").write_text('[[hooks]]\nname = "lint"\ntype = "post_agent"\ncommand = "true"\n')
        self.assertEqual(self.call()[0], 0)

    def test_status_names_the_guard_hook(self):
        out = io.StringIO()
        with redirect_stdout(out):
            ce_vibe.status()
        report = json.loads(out.getvalue())
        self.assertEqual((report["guard_hook"], report["hooks"]), ("ce-shared-guard", str(self.vibe_home / "hooks.toml")))

    def test_an_unquoted_windows_guard_path_counts_as_the_guard(self):
        # (0.31) POSIX shlex eats the backslashes of C:\...\vibe_guard.py; Windows also reads the cmd-style words
        line = r"py C:\ce\hooks\vibe_guard.py"
        self.assertIn(r"C:\ce\hooks\vibe_guard.py", ce_vibe.command_words(line, windows=True))
        self.assertNotIn(r"C:\ce\hooks\vibe_guard.py", ce_vibe.command_words(line, windows=False))
        guard = self.tmp / "g" / "vibe_guard.py"
        guard.parent.mkdir()
        guard.write_text("pass\n")
        (self.vibe_home / "hooks.toml").write_text(  # TOML literal string: the path as written, backslashes kept
            f"[[hooks]]\nname = 'win-guard'\ntype = 'pre_tool'\nmatch = '*'\ncommand = 'py {guard}'\n")
        self.assertEqual(ce_vibe.guard_hook(), "win-guard")

    def test_windows_ends_the_whole_job_at_once(self):
        # (0.31) signal_group's first call is Ctrl+Break, the second TerminateJobObject; a finished job is left alone
        group = object()
        with mock.patch.object(ce_vibe.heavy_lock, "group_alive", return_value=True), \
                mock.patch.object(ce_vibe.heavy_lock, "signal_group") as signal_group:
            ce_vibe.end_job(group)
        self.assertEqual([call.args[0] for call in signal_group.call_args_list], [group, group])
        with mock.patch.object(ce_vibe.heavy_lock, "group_alive", return_value=False), \
                mock.patch.object(ce_vibe.heavy_lock, "signal_group") as signal_group:
            ce_vibe.end_job(group)
        signal_group.assert_not_called()
        with mock.patch.object(ce_vibe.heavy_lock, "WINDOWS", True), \
                mock.patch.object(ce_vibe, "run_worker_windows", return_value=(0, "", "", False)) as windows:
            self.assertEqual(ce_vibe.run_worker(["vibe"], self.repo, {}, 5), (0, "", "", False))
        windows.assert_called_once_with(["vibe"], self.repo, {}, 5)

    def test_hold_repo_is_refused(self):
        with mock.patch.object(ce_vibe, "repo_hold", return_value="Lockstep läuft"):
            code, _, err = self.call("--allow-private")
        self.assertEqual(code, 1)
        self.assertIn("Lockstep läuft", err)
        self.assertFalse(self.record.exists())

    def test_missing_vibe_and_empty_prompt(self):
        with mock.patch.dict(os.environ, {"CE_VIBE_BIN": ""}), mock.patch.object(ce_vibe.shutil, "which", return_value=None):
            code, _, err = self.call()
        self.assertEqual((code, "vibe nicht gefunden" in err), (1, True))
        (self.tmp / "prompt.txt").write_text("  \n")
        code, _, err = self.call()
        self.assertEqual((code, "leer" in err), (1, True))

    def test_github_slug(self):
        for url, slug in (("git@github.com:o/r.git", "o/r"), ("https://github.com/o/r", "o/r"),
                          ("https://github.com/o/r.git/", "o/r")):
            sh(self.repo, "remote", "set-url", "origin", url)
            self.assertEqual(ce_vibe.github_slug(self.repo), slug)
        sh(self.repo, "remote", "set-url", "origin", str(self.origin))
        self.assertIsNone(ce_vibe.github_slug(self.repo))

    def test_read_export_tolerates_broken_files(self):
        # (a) missing file
        result = ce_vibe.read_export(self.tmp / "nonexistent.json")
        self.assertEqual(result, {})
        # (b) invalid JSON
        bad_json = self.tmp / "bad.json"
        bad_json.write_text("invalid json")
        result = ce_vibe.read_export(bad_json)
        self.assertEqual(result, {})
        # (c) JSON array
        array_json = self.tmp / "array.json"
        array_json.write_text("[1, 2, 3]")
        result = ce_vibe.read_export(array_json)
        self.assertEqual(result, {})
        # (d) valid object
        obj_json = self.tmp / "obj.json"
        obj_json.write_text('{"key": "value", "num": 42}')
        result = ce_vibe.read_export(obj_json)
        self.assertEqual(result, {"key": "value", "num": 42})


class CleanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.trash = self.tmp / "Trash"
        self.trash.mkdir()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(ce_vibe, "STATE", self.tmp / "state").start()
        mock.patch.dict(os.environ, {"CE_TRASH": str(self.trash)}).start()
        self.runs = self.tmp / "state" / "runs"
        old = __import__("time").time() - 30 * 86400
        for index in range(5):
            run = self.runs / f"2026100{index}-120000-auftrag"
            run.mkdir(parents=True)
            (run / "changes.patch").write_text("x")
            os.utime(run, (old, old))
        (self.runs / "20261000-120000-auftrag" / "worktree").mkdir()  # kept worktree: caller still works there
        fresh = self.runs / "20261030-120000-auftrag"
        fresh.mkdir()

    def clean(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = ce_vibe.main(["clean", *args])
        return code, out.getvalue()

    def test_dry_run_moves_nothing(self):
        code, out = self.clean("--keep", "2")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("würde verschieben"), 3)  # 6 runs, newest 2 kept, kept worktree stays
        self.assertNotIn("20261000", out)
        self.assertEqual(len(list(self.runs.iterdir())), 6)

    def test_apply_moves_old_runs_to_trash_and_keeps_newest_and_fresh(self):
        code, _ = self.clean("--keep", "2", "--apply")
        self.assertEqual(code, 0)
        left = sorted(path.name for path in self.runs.iterdir())
        # 20261000 keeps its worktree, 20261004 and 20261030 are the newest two
        self.assertEqual(left, ["20261000-120000-auftrag", "20261004-120000-auftrag", "20261030-120000-auftrag"])
        self.assertEqual(len(list(self.trash.iterdir())), 3)
        self.assertTrue((self.trash / "ce-vibe-20261001-120000-auftrag" / "changes.patch").is_file())

    def test_young_runs_stay(self):
        _, out = self.clean("--keep", "0", "--days", "60")
        self.assertIn("nichts aufzuräumen", out)

    def test_missing_trash_is_refused(self):
        self.trash.rmdir()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ce_vibe.main(["clean", "--keep", "2", "--apply"])
        self.assertEqual(code, 1)
        self.assertIn("Papierkorb", err.getvalue())
        self.assertEqual(len(list(self.runs.iterdir())), 6)


if __name__ == "__main__":
    unittest.main()
