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
import ce_sync  # noqa: E402
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
        ce_sync.sync(self.root, self.config, ce_sync.source_dir())

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


class SyncTests(RepoCase):
    def tools(self):
        return self.root / "scripts" / "ce_tools"

    def test_copies_have_header_checksum_and_are_up_to_date(self):
        copy = (self.tools() / "ce_lint.py").read_text()
        lines = copy.splitlines()
        self.assertTrue(lines[0].startswith("#!"))
        self.assertRegex(lines[1], r"^# GENERATED from ce-agent-tools 0\.5\.0 - do not edit$")
        self.assertRegex(lines[2], r"^# ce-sha256: [0-9a-f]{64}$")
        self.assertEqual(ce_sync.check(self.root, self.config, ce_sync.source_dir()), [])
        code, out = run_main(ce_sync, "--repo", str(self.root), "--check")
        self.assertEqual(code, 0, out)
        self.assertEqual(run_main(ce_sync, "--repo", str(self.root))[1].strip().split(": ")[1], "up to date")

    def test_hand_edit_is_an_error_and_sync_repairs_it(self):
        path = self.tools() / "ce_check.py"
        path.write_text(path.read_text() + "\n# tweak\n")
        findings = ce_sync.check(self.root, self.config, ce_sync.source_dir())
        self.assertEqual([f[0] for f in findings], ["error"])
        self.assertEqual(run_main(ce_sync, "--repo", str(self.root), "--check")[0], 1)
        code, out = run_main(ce_lint, "--repo", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("edited by hand", out)
        run_main(ce_sync, "--repo", str(self.root))
        self.assertEqual(run_main(ce_sync, "--repo", str(self.root), "--check")[0], 0)

    def test_older_copy_is_a_warning_and_missing_dir_an_error(self):
        source = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(source))
        for name in ce_sync.SYNC_FILES:
            (source / name).write_text((ce_sync.source_dir() / name).read_text() + "# newer\n")
        findings = ce_sync.check(self.root, self.config, source)
        self.assertEqual({f[0] for f in findings}, {"warning"})
        self.assertEqual(len(findings), len(ce_sync.SYNC_FILES))
        import shutil
        shutil.rmtree(self.tools())
        self.assertEqual(ce_sync.check(self.root, self.config, None)[0][0], "error")

    def test_custom_tools_dir(self):
        config = dict(self.config, tools_dir="tools/shared")
        ce_sync.sync(self.root, config, ce_sync.source_dir())
        self.assertTrue((self.root / "tools" / "shared" / "heavy_lock.py").is_file())

    def test_copies_run_without_the_plugin(self):
        env = {k: v for k, v in os.environ.items() if k != "CE_TOOLS"}
        env["HOME"] = str(self.root.parent)  # no default plugin path either
        done = subprocess.run([sys.executable, "-I", str(self.tools() / "ce_lint.py"), "--repo", str(self.root)],
                              capture_output=True, text=True, env=env, cwd=self.root.parent)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lock = subprocess.run([sys.executable, str(self.tools() / "heavy_lock.py"), "status"], capture_output=True,
                              text=True, env=dict(env, CE_HEAVY_LOCK_FILE=str(self.root / "l")))
        self.assertEqual(lock.stdout.split()[:2], ["shared", "free"])
        done = subprocess.run([sys.executable, str(self.tools() / "ce_sync.py"), "--repo", str(self.root), "--check"],
                              capture_output=True, text=True, env=env, cwd=self.root.parent)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


class CheckTests(RepoCase):
    def setUp(self):
        super().setUp()
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        patcher = mock.patch.dict(os.environ, {"CE_CHECK_CACHE": cache.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_quick_is_targeted_and_expands_files(self):
        self.init_git()
        write(self.root, "new.py", "x\n")
        (self.root / "version.py").write_text('VERSION = "1.2.0"  # touched\n')
        probe = "import sys; print('FILES', *sys.argv[1:])"
        self.set_config(dict(self.config, check={"quick": [sys.executable, "-c", "print('ALL')"],
                                                 "targeted": [sys.executable, "-c", probe, "{files}"]}))
        out = run_main(ce_check, "quick", "--repo", str(self.root), "--verbose")[1]
        self.assertIn("FILES .ce-repo.json new.py version.py", out)
        out = run_main(ce_check, "quick", "--repo", str(self.root), "--no-targeted", "--verbose")[1]
        self.assertIn("ALL", out)
        self.assertNotIn("FILES", out)

    def test_second_full_without_change_gets_a_hint_but_runs(self):
        self.init_git()
        self.set_config(dict(self.config, check={"full": [sys.executable, "-c", "print('full')"]}))
        git(self.root, "commit", "-qam", "cfg")
        first = run_main(ce_check, "full", "--repo", str(self.root))
        self.assertEqual(first[0], 0)
        self.assertNotIn("hint", first[1])
        second = run_main(ce_check, "full", "--repo", str(self.root))
        self.assertEqual(second[0], 0, "never blocks")
        self.assertIn("voller Lauf nur einmal vor Abschluss", second[1])
        write(self.root, "change.txt", "x")
        self.assertNotIn("hint", run_main(ce_check, "full", "--repo", str(self.root))[1])

    def test_nightly_runs_its_own_commands_or_reports_the_missing_key(self):
        """0.5.0: ./check nightly bundles the image stages; a repo without check.nightly is a configuration problem."""
        code, out = run_main(ce_check, "nightly", "--repo", str(self.root))
        self.assertEqual(code, 2, out)
        self.assertIn("check.nightly missing", out)
        config = json.loads((self.root / ".ce-repo.json").read_text())
        config["check"]["nightly"] = [[sys.executable, "-c", "print('night')"]]
        (self.root / ".ce-repo.json").write_text(json.dumps(config))
        code, out = run_main(ce_check, "nightly", "--repo", str(self.root), "--verbose")
        self.assertEqual(code, 0, out)
        self.assertIn("night", out)

    def test_old_run_gives_no_hint(self):
        self.init_git()
        self.set_config(dict(self.config, check={"full": [sys.executable, "-c", "pass"]}))
        run_main(ce_check, "full", "--repo", str(self.root))
        file = ce_check.state_file(self.root)
        state = json.loads(file.read_text())
        state["time"] -= 31 * 60
        file.write_text(json.dumps(state))
        self.assertNotIn("hint", run_main(ce_check, "full", "--repo", str(self.root))[1])

    def test_nightly_runs_its_own_commands_or_reports_the_missing_key(self):
        """0.5.0: ./check nightly bundles the image stages; a repo without check.nightly is a configuration problem."""
        code, out = run_main(ce_check, "nightly", "--repo", str(self.root))
        self.assertEqual(code, 2, out)
        self.assertIn("check.nightly missing", out)
        config = json.loads((self.root / ".ce-repo.json").read_text())
        config["check"]["nightly"] = [[sys.executable, "-c", "print('night')"]]
        (self.root / ".ce-repo.json").write_text(json.dumps(config))
        code, out = run_main(ce_check, "nightly", "--repo", str(self.root), "--verbose")
        self.assertEqual(code, 0, out)
        self.assertIn("night", out)

    def test_full_repeats_last_failures_first_and_skips_full_if_still_red(self):
        self.init_git()
        flag = self.root / "fixed"
        calls = self.root / "calls"
        write(self.root, "t.py", "import sys, pathlib\n"
              f"pathlib.Path({str(calls)!r}).open('a').write(' '.join(sys.argv[1:]) + ';')\n"
              f"sys.exit(0 if pathlib.Path({str(flag)!r}).exists() else 1)\n")
        self.set_config(dict(self.config, check={"full": [sys.executable, "t.py", "full"],
                                                 "rerun_failed": [sys.executable, "t.py", "failed"]}))
        self.assertEqual(run_main(ce_check, "full", "--repo", str(self.root))[0], 1)
        calls.write_text("")
        code, out = run_main(ce_check, "full", "--repo", str(self.root))
        self.assertEqual(code, 1)
        self.assertEqual(calls.read_text(), "failed;", "only the repeat ran")
        self.assertIn("full run skipped", out)
        flag.write_text("x")
        calls.write_text("")
        self.assertEqual(run_main(ce_check, "full", "--repo", str(self.root))[0], 0)
        self.assertEqual(calls.read_text(), "failed;full;", "repeat first, then the full run")
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


    def test_minor_report_counts_and_missing_folder_is_silent(self):
        write(self.root, "VERSION_X.txt", "")  # irrelevant file, keeps the folder logic honest
        (self.root / "VERSION_X.txt").unlink()
        write(self.root, "version.py", 'VERSION = "1.2.1"\n')
        write(self.root, "CHANGELOG.md", "## 1.2.1\n")
        (self.root / "docs/versions/1.2.0.md").rename(self.root / "docs/versions/1.2.md")
        self.assertEqual(self.lint(), (0, "demo lint: ok\n"))
        (self.root / "docs/versions/1.2.md").unlink()
        self.assertIn("no report for version 1.2.1", self.lint()[1])
        (self.root / "docs/versions").rmdir()
        self.assertEqual(self.lint(), (0, "demo lint: ok\n"))


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

    def bare_remote(self):
        remote = self.root.parent / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        git(self.root, "remote", "add", "origin", str(remote))
        return remote

    def remote_refs(self, remote):
        return subprocess.run(["git", "-C", str(remote), "for-each-ref", "--format=%(objectname) %(refname)"],
                              capture_output=True, text=True).stdout

    def test_execute_on_throwaway_repo_tag_branch_and_push_point_at_head(self):
        remote = self.bare_remote()
        head = git(self.root, "rev-parse", "HEAD")
        code, out = run_main(ce_release, "--repo", str(self.root), "--execute")
        self.assertEqual(code, 0, out)
        self.assertIn("EXECUTE", out.splitlines()[0])
        self.assertEqual(git(self.root, "cat-file", "-t", "v1.2.0"), "tag", "annotated tag")
        self.assertEqual(git(self.root, "rev-parse", "v1.2.0^{commit}"), head)
        self.assertEqual(git(self.root, "rev-parse", "release/1.2.0"), head)
        self.assertEqual(git(self.root, "rev-parse", "--abbrev-ref", "HEAD"), "main", "the checkout is not switched")
        refs = self.remote_refs(remote)
        self.assertIn(f"{head} refs/heads/release/1.2.0", refs)
        self.assertIn("refs/tags/v1.2.0", refs)
        self.assertNotIn("refs/heads/main", refs, "only branch and tag are pushed")

    def test_execute_refuses_dirty_tree_and_existing_tag_without_touching_the_remote(self):
        remote = self.bare_remote()
        write(self.root, "dirty.txt", "x")
        code, out = run_main(ce_release, "--repo", str(self.root), "--execute")
        self.assertEqual(code, 1)
        self.assertIn("working tree not clean", out)
        (self.root / "dirty.txt").unlink()
        git(self.root, "tag", "v1.2.0")
        code, out = run_main(ce_release, "--repo", str(self.root), "--execute")
        self.assertEqual(code, 1)
        self.assertIn("tag v1.2.0 exists already", out)
        self.assertEqual(self.remote_refs(remote), "")
        self.assertNotIn("release/1.2.0", self.refs())

    def test_failing_push_is_reported_as_failure(self):
        git(self.root, "remote", "add", "origin", str(self.root.parent / "does-not-exist.git"))
        code, out = run_main(ce_release, "--repo", str(self.root), "--execute")
        self.assertEqual(code, 1)
        self.assertIn("FAIL git push", out)

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
                             "--projects", str(self.projects), "--list")
        self.assertEqual(code, 0)
        self.assertIn("2 Worktree(s) in 2 Repo(s)", out.splitlines()[0])
        self.assertIn("nope: (kein Repo", out)

    def test_table_has_title_and_hint_and_json_is_valid(self):
        self.session(self.tree, minutes_old=5)
        folder = self.projects / ce_worktrees.project_dir_name(str(self.tree))
        (folder / "abcdef12-0000.jsonl").write_text(
            '{"type":"custom-title","customTitle":"Alt"}\n{"type":"user"}\n{"type":"custom-title","customTitle":"Neu"}\n')
        code, out = run_main(ce_worktrees, "--repos", str(self.root), "--projects", str(self.projects))
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[1].startswith("Repo "))
        row = next(line for line in lines if "demo-wt" in line)
        self.assertIn("Neu (", row)
        self.assertIn("feature", row)
        self.assertIn("ja", row)
        self.assertIn("NICHT LÖSCHEN: Sitzung aktiv", row)
        self.assertTrue(any("(Hauptordner)" in line for line in lines))
        data = json.loads(run_main(ce_worktrees, "--repos", str(self.root), "--projects", str(self.projects),
                                   "--json")[1])
        self.assertEqual(len(data[0]["worktrees"]), 2)
        titles = [w["session"]["title"] for w in data[0]["worktrees"] if w["session"]]
        self.assertEqual(titles, ["Neu"])


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

    def test_runs_the_repo_own_check_skills(self):
        write(self.root, ".agents/skills/check_skills.py", "import sys; print('skill broken: ce-x'); sys.exit(1)\n")
        write(self.root, "README.md", "r\n")
        git(self.root, "add", "-A")
        message = precommit.decide(self.payload())["systemMessage"]
        self.assertIn(".agents/skills/check_skills.py", message)
        self.assertIn("skill broken: ce-x", message)
        self.assertNotIn("hookSpecificOutput", precommit.decide(self.payload()))

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
