"""Tests of the shared tools (ce_check, ce_lint, ce_release, ce_worktrees) and the pre-commit doc hook.
Run: python3 -m unittest discover -s tests"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "ce-shared"
sys.path.insert(0, str(PLUGIN / "tools"))
sys.path.insert(0, str(PLUGIN / "hooks"))
import ce_check  # noqa: E402
import ce_common  # noqa: E402
import ce_lint  # noqa: E402
import ce_release  # noqa: E402
import ce_worktrees  # noqa: E402
import precommit  # noqa: E402

GOOD_AGENTS = "# X\n\n" + "".join(f"## {name}\n\ntext\n\n" for name in ce_lint.REQUIRED_SECTIONS)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def write(root, name, text):
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def run_main(module, *argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = module.main(list(argv))
    return code, out.getvalue()


class RepoCase(unittest.TestCase):
    """A scratch repo with .ce-repo.json, version file, CHANGELOG, AGENTS.md."""

    config = {"name": "demo", "version": {"file": "version.py", "pattern": "VERSION\\s*=\\s*\"([^\"]+)\""},
              "check": {"quick": [sys.executable, "-c", "print('quick ok')"]},
              "docs": {"versions": "docs/versions", "changelog": "CHANGELOG.md"},
              "limits": {"AGENTS.md": 150, "CLAUDE.md": 20},
              "release": {"branch_prefix": "release/", "tag_prefix": "v"}}

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve() / "demo"
        self.root.mkdir()
        self.set_config(self.config)
        write(self.root, "version.py", 'VERSION = "1.2.0"\n')
        write(self.root, "CHANGELOG.md", "# Changelog\n\n## 1.2.0\n- thing\n")
        write(self.root, "AGENTS.md", GOOD_AGENTS)
        write(self.root, "CLAUDE.md", "# Claude\n@AGENTS.md\n")
        write(self.root, "docs/versions/1.2.0.md", "report\n")

    def set_config(self, config):
        write(self.root, ".ce-repo.json", json.dumps(config))

    def init_git(self):
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")


class CommonTests(RepoCase):
    def test_find_root_walks_up_and_reads_version(self):
        sub = self.root / "a" / "b"
        sub.mkdir(parents=True)
        root, config = ce_common.open_repo(str(sub))
        self.assertEqual(root, self.root)
        self.assertEqual(ce_common.read_version(root, config), "1.2.0")

    def test_missing_config_and_bad_pattern(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(ce_common.RepoError):
                ce_common.find_root(empty)
        config = dict(self.config, version={"file": "version.py", "pattern": "NOPE=(.*)"})
        with self.assertRaises(ce_common.RepoError):
            ce_common.read_version(self.root, config)


class CheckTests(RepoCase):
    def test_quick_green_result_first(self):
        code, out = run_main(ce_check, "quick", "--repo", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("demo quick: GREEN", out.splitlines()[0])

    def test_red_shows_tail_of_failed_command_only(self):
        self.set_config(dict(self.config, check={"quick": [
            [sys.executable, "-c", "print('fine')"],
            [sys.executable, "-c", "import sys; print('boom detail'); sys.exit(3)"]]}))
        code, out = run_main(ce_check, "quick", "--repo", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("RED (1 of 2", out.splitlines()[0])
        self.assertIn("boom detail", out)
        self.assertNotIn("fine\n", out.split("--- ", 1)[1])

    def test_full_repeats_only_failed_once_and_counts_green_if_that_passes(self):
        marker = self.root / "attempt"
        script = ("import pathlib, sys\n"
                  f"m = pathlib.Path({str(marker)!r})\n"
                  "if '--failed' in sys.argv:\n    m.write_text('rerun'); sys.exit(0)\n"
                  "sys.exit(1)\n")
        write(self.root, "tests.py", script)
        self.set_config(dict(self.config, check={"full": [sys.executable, "tests.py"],
                                                 "rerun_failed": [sys.executable, "tests.py", "--failed"]}))
        code, out = run_main(ce_check, "full", "--repo", str(self.root))
        self.assertEqual(code, 0)
        self.assertEqual(marker.read_text(), "rerun")
        self.assertIn("after repeating only what failed", out.splitlines()[0])

    def test_full_stays_red_when_the_repeat_fails_too_and_quick_never_repeats(self):
        fail = [sys.executable, "-c", "raise SystemExit(1)"]
        self.set_config(dict(self.config, check={"full": fail, "quick": fail, "rerun_failed": fail}))
        code, out = run_main(ce_check, "full", "--repo", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("RED", out.splitlines()[0])
        self.assertIn("2 command(s)", out.splitlines()[0], "one run plus one repeat")
        code, out = run_main(ce_check, "quick", "--repo", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("1 command(s)", out.splitlines()[0], "no repeat in quick")

    def test_configuration_problem_is_exit_2(self):
        self.set_config(dict(self.config, check={}))
        code, out = run_main(ce_check, "full", "--repo", str(self.root))
        self.assertEqual(code, 2)
        self.assertIn("check.full missing", out)

    def test_unknown_command_is_red_not_a_crash(self):
        self.set_config(dict(self.config, check={"quick": ["/nonexistent/tool"]}))
        code, out = run_main(ce_check, "quick", "--repo", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("cannot start", out)


class LintTests(RepoCase):
    def lint(self, *extra):
        return run_main(ce_lint, "--repo", str(self.root), *extra)

    def test_clean_repo_is_ok(self):
        code, out = self.lint()
        self.assertEqual((code, out.strip()), (0, "demo lint: ok"))

    def test_limit_exceeded_is_an_error(self):
        write(self.root, "CLAUDE.md", "line\n" * 25)
        code, out = self.lint()
        self.assertEqual(code, 1)
        self.assertIn("CLAUDE.md: 25 lines, limit 20", out)
        self.assertTrue(out.splitlines()[0].startswith("demo lint: 1 error"))

    def test_missing_and_misordered_sections(self):
        write(self.root, "AGENTS.md", "## Projekt\n\n## Regeln\n\n## Befehle\n\n## Definition of Done\n")
        code, out = self.lint()
        self.assertEqual(code, 1)
        self.assertIn("'## Wahrheit und Aktualität' missing", out)
        self.assertIn("not in the standard order", out)

    def test_section_headings_may_continue_after_the_keyword(self):
        write(self.root, "AGENTS.md", GOOD_AGENTS.replace("## Regeln", "## Regeln und Verbote"))
        self.assertEqual(self.lint()[0], 0)

    def test_note_files_in_main_folder(self):
        write(self.root, "VERSION_0.5_NOTES.md", "x")
        write(self.root, "docs/FOO_PROGRESS.md", "fine here, only the main folder counts")
        code, out = self.lint()
        self.assertEqual(code, 1)
        self.assertIn("VERSION_0.5_NOTES.md", out)
        self.assertNotIn("FOO_PROGRESS", out)

    def test_version_report_and_changelog_warn_only(self):
        (self.root / "docs/versions/1.2.0.md").unlink()
        write(self.root, "CHANGELOG.md", "# Changelog\n")
        code, out = self.lint()
        self.assertEqual(code, 0)
        self.assertIn("no report for version 1.2.0", out)
        self.assertIn("no entry mentions version 1.2.0", out)
        self.assertEqual(self.lint("--strict")[0], 1)


class ReleaseTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.init_git()

    def release(self, *extra):
        with mock.patch.object(ce_release.ce_common, "git_out", wraps=ce_common.git_out):
            return run_main(ce_release, "--repo", str(self.root), "--remote", "nowhere", *extra)

    def refs(self):
        return git(self.root, "for-each-ref", "--format=%(refname)")

    def test_dry_run_is_default_changes_nothing_and_lists_commands(self):
        before = self.refs()
        code, out = self.release()
        self.assertEqual(code, 0)
        self.assertIn("release 1.2.0 (dry run): ready", out.splitlines()[0])
        self.assertIn("would run: git tag -a v1.2.0", out)
        self.assertIn("would run: git branch release/1.2.0", out)
        self.assertIn("would run: git push nowhere release/1.2.0 v1.2.0", out)
        self.assertEqual(self.refs(), before, "nothing was created")

    def test_blocked_by_dirty_tree_missing_changelog_and_existing_tag(self):
        write(self.root, "CHANGELOG.md", "# Changelog\n")
        write(self.root, "new.txt", "x")
        git(self.root, "tag", "v1.2.0")
        code, out = self.release("--execute")
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED", out.splitlines()[0])
        self.assertIn("no entry for 1.2.0", out)
        self.assertIn("working tree not clean", out)
        self.assertIn("tag v1.2.0 exists already", out)
        self.assertNotIn("release/1.2.0", self.refs(), "blocked: nothing executed")

    def test_execute_without_push_creates_tag_and_branch(self):
        code, out = self.release("--execute", "--no-push")
        self.assertEqual(code, 0, out)
        self.assertIn("skipped (--no-push)", out)
        refs = self.refs()
        self.assertIn("refs/tags/v1.2.0", refs)
        self.assertIn("refs/heads/release/1.2.0", refs)

    def test_execute_pushes_to_remote_and_never_forces(self):
        remote = self.root.parent / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        git(self.root, "remote", "add", "origin", str(remote))
        code, out = run_main(ce_release, "--repo", str(self.root), "--execute")
        self.assertEqual(code, 0, out)
        self.assertNotIn("--force", out)
        listing = subprocess.run(["git", "-C", str(remote), "for-each-ref", "--format=%(refname)"],
                                 capture_output=True, text=True).stdout
        self.assertIn("refs/tags/v1.2.0", listing)
        self.assertIn("refs/heads/release/1.2.0", listing)
        # a second run is blocked: tag and branch exist
        self.assertEqual(run_main(ce_release, "--repo", str(self.root))[0], 1)

    def test_existing_remote_ref_blocks(self):
        remote = self.root.parent / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        git(self.root, "remote", "add", "origin", str(remote))
        git(self.root, "push", "-q", "origin", "HEAD:refs/heads/release/1.2.0")
        code, out = run_main(ce_release, "--repo", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("refs/heads/release/1.2.0 exists on origin", out)


class WorktreeTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.init_git()
        self.projects = self.root.parent / "projects"
        self.projects.mkdir()
        self.tree = self.root.parent / "demo-wt"
        git(self.root, "worktree", "add", "-q", "-b", "feature", str(self.tree))

    def states(self, cwds=None, active=60):
        collected = ce_worktrees.collect([self.root], self.projects, "main", active, cwds or {})
        return {Path(s["path"]).name: s for s in collected[0][1]}

    def session(self, path, minutes_old):
        folder = self.projects / ce_worktrees.project_dir_name(str(path))
        folder.mkdir()
        file = folder / "abcdef12-0000.jsonl"
        file.write_text("{}\n")
        old = time.time() - minutes_old * 60
        os.utime(file, (old, old))

    def test_parse_porcelain(self):
        parsed = ce_worktrees.parse_worktrees("worktree /a\nHEAD abc\nbranch refs/heads/main\n\n"
                                              "worktree /b\nHEAD def\ndetached\nprunable gone\n")
        self.assertEqual([(w["path"], w["branch"], w["prunable"]) for w in parsed],
                         [("/a", "main", False), ("/b", None, True)])

    def test_project_dir_name_matches_claude_code_scheme(self):
        self.assertEqual(ce_worktrees.project_dir_name("/Users/x/Claude/Cube Escape/.claude/wt"),
                         "-Users-x-Claude-Cube-Escape--claude-wt")

    def test_clean_idle_merged_worktree_is_not_protected(self):
        states = self.states()
        self.assertEqual(set(states), {"demo", "demo-wt"})
        state = states["demo-wt"]
        self.assertEqual((state["branch"], state["dirty"], state["ahead"], state["behind"]), ("feature", 0, 0, 0))
        self.assertFalse(state["protected"])
        self.assertNotIn(ce_worktrees.DO_NOT_DELETE, ce_worktrees.format_state(state))

    def test_dirty_unmerged_active_session_and_process_protect(self):
        write(self.tree, "x.txt", "x")
        self.assertIn("1 offene", self.states()["demo-wt"]["reasons"][0])
        git(self.tree, "add", "-A")
        git(self.tree, "commit", "-q", "-m", "work")
        reasons = " ".join(self.states()["demo-wt"]["reasons"])
        self.assertIn("1 Commit(s) nicht in main", reasons)
        self.assertEqual(self.states()["demo-wt"]["ahead"], 1)
        # clean up the situation: merge back, then only a session/process remains as reason
        git(self.root, "merge", "-q", "feature")
        self.assertFalse(self.states()["demo-wt"]["protected"])
        self.session(self.tree, minutes_old=5)
        busy = self.states()["demo-wt"]
        self.assertTrue(busy["protected"])
        self.assertIn("Sitzung aktiv", busy["reasons"][0])
        self.assertIn(ce_worktrees.DO_NOT_DELETE, ce_worktrees.format_state(busy))
        self.assertTrue(self.states(cwds={str(self.tree): ["claude"]}, active=0)["demo-wt"]["protected"])

    def test_old_session_is_shown_but_does_not_protect(self):
        self.session(self.tree, minutes_old=60 * 30)
        state = self.states()["demo-wt"]
        self.assertFalse(state["protected"])
        self.assertEqual(state["session"]["id"], "abcdef12-0000")
        self.assertIn("Sitzung abcdef12", ce_worktrees.format_state(state))

    def test_main_summary_line_and_missing_repo(self):
        code, out = run_main(ce_worktrees, "--repos", str(self.root), str(self.root.parent / "nope"),
                             "--projects", str(self.projects))
        self.assertEqual(code, 0)
        self.assertIn("2 Worktree(s) in 2 Repo(s)", out.splitlines()[0])
        self.assertIn("nope: (kein Repo", out)


class PrecommitHookTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.init_git()

    def payload(self, command="git commit -m x"):
        return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.root)}

    def test_command_detection(self):
        for command in ("git commit -m x", "cd a && git commit -am 'x'", "git -C /r commit --amend"):
            self.assertTrue(precommit.is_commit(command), command)
        for command in ("git status", "echo git commit", "git log --grep commit", "git commit-tree abc"):
            self.assertFalse(precommit.is_commit(command), command)

    def test_warns_when_committed_docs_break_the_lint(self):
        write(self.root, "AGENTS.md", "## Projekt\n")
        git(self.root, "add", "AGENTS.md")
        result = precommit.decide(self.payload())
        self.assertIn("vor-commit-doku", result["systemMessage"])
        self.assertIn("missing", result["systemMessage"])
        self.assertNotIn("hookSpecificOutput", result, "warns, never blocks")

    def test_silent_for_code_only_commit_and_for_clean_docs(self):
        write(self.root, "code.py", "x = 1\n")
        git(self.root, "add", "code.py")
        self.assertEqual(precommit.decide(self.payload()), {})
        write(self.root, "AGENTS.md", GOOD_AGENTS + "\nmore\n")
        git(self.root, "add", "AGENTS.md")
        self.assertEqual(precommit.decide(self.payload()), {})

    def test_commit_all_considers_unstaged_tracked_docs(self):
        write(self.root, "AGENTS.md", "## Projekt\n")
        self.assertEqual(precommit.decide(self.payload("git commit -m x")), {})
        self.assertIn("vor-commit-doku", precommit.decide(self.payload("git commit -am x"))["systemMessage"])

    def test_runs_the_repo_own_check_docs_and_ignores_other_tools(self):
        write(self.root, "Tools/check_docs.py", "import sys; print('docs broken: x.md'); sys.exit(1)\n")
        write(self.root, "README.md", "r\n")
        git(self.root, "add", "-A")
        message = precommit.decide(self.payload())["systemMessage"]
        self.assertIn("Tools/check_docs.py", message)
        self.assertIn("docs broken: x.md", message)
        self.assertEqual(precommit.decide({"tool_name": "Edit", "tool_input": {}}), {})

    def test_hook_script_always_exits_0_with_json(self):
        done = subprocess.run([sys.executable, str(PLUGIN / "hooks" / "precommit.py")], input="not json",
                              capture_output=True, text=True)
        self.assertEqual((done.returncode, json.loads(done.stdout)), (0, {}))

    def test_hooks_json_registers_it(self):
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]["PreToolUse"]
        self.assertTrue(any("precommit.py" in h["command"] for entry in hooks for h in entry["hooks"]))


if __name__ == "__main__":
    unittest.main()
