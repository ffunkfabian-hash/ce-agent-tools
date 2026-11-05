"""Git pre-commit/pre-push net (plugins/ce-shared/githooks/ce_githooks.py) in real temporary repositories with local bare
remotes: what is blocked, what passes, what Git's protocol cannot show, and that existing hooks keep running."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "plugins" / "ce-shared" / "githooks" / "ce_githooks.py"
sys.path[:0] = [str(SCRIPT.parent), str(SCRIPT.parent.parent / "hooks")]
import ce_githooks  # noqa: E402
import guard  # noqa: E402

WINDOWS = os.name == "nt"
if WINDOWS:  # 0.31: Claude Code's Bash there is Git Bash; there is no zsh, and System32's bash is WSL
    sys.path.insert(0, str(ROOT / "plugins" / "ce-shared" / "tools"))
    import heavy_lock  # noqa: E402
    SHELLS = ((heavy_lock.posix_shell("bash") or "bash-not-found (Git for Windows missing)", "-c"),)
else:
    SHELLS = (("/bin/bash", "-c"), ("/bin/zsh", "-f", "-c"))


def posix(path) -> str:
    """A path as a shell line or a Git config value writes it (Windows: with /, a backslash would be an escape)."""
    return Path(path).as_posix() if WINDOWS else str(path)

RULES = guard.load_rules(SCRIPT.parent.parent / "rules")

MARKER_HOOK = '#!/bin/sh\necho "$0 $*" >> "{log}"\ncat >> "{log}"\nexit {code}\n'


class Repo:
    def __init__(self, base, name="work"):
        self.base = Path(base)
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.base),
                    "GIT_CONFIG_GLOBAL": str(self.base / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@x"}
        (self.base / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n")
        self.remote = self.base / "remote.git"
        self.git(self.base, "init", "-q", "--bare", str(self.remote))
        self.path = self.base / name
        self.git(self.base, "clone", "-q", str(self.remote), str(self.path))
        self.commit("start")
        self.git(self.path, "push", "-q", "origin", "main")

    def git(self, cwd, *args, check=True, env=None):
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", env=dict(self.env, **(env or {})))
        if check and result.returncode != 0:
            raise AssertionError(f"git {args}: {result.stderr}")
        return result

    def run(self, *args, **kw):
        return self.git(self.path, *args, **kw)

    def commit(self, message, check=True, env=None, cwd=None):
        cwd = cwd or self.path
        (Path(cwd) / "f.txt").write_text(message)
        self.git(cwd, "add", "f.txt")
        return self.git(cwd, "commit", "-q", "-m", message, check=check, env=env)

    def tool(self, *args, script=SCRIPT, check=True):
        result = subprocess.run([sys.executable, str(script), *args, "--repo", str(self.path)], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", env=self.env)
        if check and result.returncode != 0:
            raise AssertionError(result.stderr + result.stdout)
        return result

    def remote_sha(self, ref):
        result = self.git(self.base, "--git-dir", str(self.remote), "rev-parse", "-q", "--verify", ref, check=False)
        return result.stdout.strip() or None

    def head(self, cwd=None):
        return self.git(cwd or self.path, "rev-parse", "HEAD").stdout.strip()


class GitHookTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.repo = Repo(self.base)


# Assembled at run time: this file must not carry a secret-shaped string itself, the repository is public.
RANDOM = "q7Zt2LmX9vKc4RbN8wYd1HsP6gJf3TaE5uQi0Woz"
TOKENS = {"GitHub-Token": "gh" + "p_" + RANDOM[:36], "AWS-Zugangsschlüssel": "AK" + "IA" + "Q7ZT2LMX9VKC4RBN",
          "Anthropic-Schlüssel": "sk-" + "ant-api03-" + RANDOM, "OpenAI-Schlüssel": "sk-" + "proj-" + RANDOM + RANDOM,
          "Google-API-Schlüssel": "AI" + "za" + RANDOM[:35], "Slack-Token": "xo" + "xb-7184926035-" + RANDOM[:24],
          "Stripe-Live-Schlüssel": "sk" + "_live_" + RANDOM[:24], "npm-Token": "np" + "m_" + RANDOM[:36],
          "PyPI-Token": "py" + "pi-AgE" + RANDOM + RANDOM}
KEY_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"
KEY_BODY = "MIIEpAIBAAKCAQEA" + RANDOM + RANDOM


def diff(*added, path="src/a.py"):
    return ["commit:abc1234", f"diff --git a/{path} b/{path}", "index 1..2 100644", "--- a/x", f"+++ b/{path}", "@@ -0,0 +1 @@",
            *[f"+{line}" for line in added]]


class SecretPatternTests(unittest.TestCase):
    def test_every_format_is_found_also_inside_a_line(self):
        for name, token in TOKENS.items():
            for line in (token, f'TOKEN = "{token}"', f"url=https://x.test/?k={token}&a=1", f"export KEY={token}"):
                self.assertIn(name, ce_githooks.secrets_in_line(line), line)

    def test_placeholders_and_look_alikes_are_no_secrets(self):
        lines = ["gh" + "p_" + "x" * 36, "gh" + "p_" + "0123456789" * 3 + "012345"[:6], "gh" + "p_" + "YOUR" + "TOKEN" * 7, "AK" + "IA" + "IOSFODNN7EXAMPLE",
                 "sk-" + "ant-" + "your-key-here-your-key-here-your-key-here", "AI" + "za" + "SyEXAMPLE" + "a" * 26,
                 "sk-" + "task-with-a-very-long-name-that-goes-on-and-on-and-on", "disk-" + RANDOM, "risk-" + RANDOM + RANDOM,
                 "gh" + "p_short", "npm_install_" + RANDOM, "xo" + "xb-not-a-token", "password = hunter2", "api_key: abc123", RANDOM + RANDOM,
                 "sha256:" + "ab12" * 16, "commit 0123456789abcdef0123456789abcdef01234567"]
        for line in lines:
            self.assertEqual(ce_githooks.secrets_in_line(line), [], line)

    def test_only_added_lines_count_and_the_place_is_named(self):
        token = TOKENS["GitHub-Token"]
        hits, finished = ce_githooks.scan_diff(diff("ok", token, path="dir/b.env"))
        self.assertTrue(finished)
        self.assertEqual(hits, [("GitHub-Token", "dir/b.env", "abc1234")])
        self.assertEqual(ce_githooks.scan_diff(["commit:a", "diff --git a/x b/x", "--- a/x", "+++ b/x", "@@ -1 +0,0 @@", f"-{token}"])[0], [])

    def test_a_private_key_needs_a_body(self):
        self.assertEqual(ce_githooks.scan_diff(diff(KEY_HEADER, KEY_BODY))[0], [("privater Schlüssel", "src/a.py", "abc1234")])
        self.assertEqual(ce_githooks.scan_diff(diff(f'"key": "{KEY_HEADER}\\n{KEY_BODY}\\n"'))[0], [("privater Schlüssel", "src/a.py", "abc1234")])
        for lines in ((KEY_HEADER,), (KEY_HEADER, "..."), (f"Beginnt mit `{KEY_HEADER}`.", "Dann folgt der Inhalt."), (KEY_BODY,)):
            self.assertEqual(ce_githooks.scan_diff(diff(*lines))[0], [], lines)

    def test_a_diff_header_lookalike_in_added_text_and_overlong_lines(self):
        self.assertEqual(ce_githooks.scan_diff(diff("++ b/fake.txt", TOKENS["npm-Token"]))[0], [("npm-Token", "src/a.py", "abc1234")])
        self.assertEqual(ce_githooks.scan_diff(diff(TOKENS["npm-Token"] + " " * ce_githooks.LONGEST_LINE))[0], [])

    def test_the_time_limit_stops_the_scan(self):
        self.assertEqual(ce_githooks.scan_diff(diff("x"), deadline=0), ([], False))


@unittest.skipIf(WINDOWS, "the gh stub is a shell script; subprocess cannot start it there")
class PublicPushTests(GitHookTestCase):
    """Real pushes through the hook. A stub gh answers the visibility, insteadOf points a GitHub URL at the local bare remote."""

    def setUp(self):
        super().setUp()
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        self.answer = self.base / "visibility"
        self.answer.write_text("public")
        self.calls = self.base / "gh-calls"
        stub = bin_dir / "gh"
        stub.write_text(f'#!/bin/sh\necho "$*" >> "{posix(self.calls)}"\ncase "$*" in *pub*) echo public;; *) cat "{posix(self.answer)}";; esac\n')
        stub.chmod(0o755)
        self.repo.env["PATH"] = f"{bin_dir}{os.pathsep}{self.repo.env['PATH']}"
        self.repo.run("config", f"url.{posix(self.repo.remote)}.insteadOf", "https://github.com/o/r.git")
        self.repo.run("remote", "set-url", "origin", "https://github.com/o/r.git")
        self.repo.tool("install")
        self.repo.run("checkout", "-q", "-b", "claude/x")
        self.repo.commit("begin")
        self.assertEqual(self.push().returncode, 0)

    def push(self, *args):
        return self.repo.run("push", "-q", "origin", "claude/x", *args, check=False)

    def add(self, name, data, message="add"):
        path = self.repo.path / name
        path.write_bytes(data)
        self.repo.run("add", name)
        self.repo.run("commit", "-q", "-m", message)

    def test_a_big_binary_is_stopped_and_text_of_the_same_size_passes(self):
        self.add("huge.txt", b"line of text\n" * 500_000)
        self.assertEqual(self.push().returncode, 0)
        self.add("big.bin", b"\0" * (ce_githooks.BIG_BINARY + 1))
        result = self.push()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Binärdatei über 5 MB", result.stderr)
        self.assertIn("big.bin", result.stderr)
        self.assertIsNone(self.repo.remote_sha("refs/heads/does-not-exist"))

    def test_a_binary_at_the_limit_passes(self):
        self.add("ok.bin", b"\0" * ce_githooks.BIG_BINARY)
        self.assertEqual(self.push().returncode, 0)

    def test_a_binary_removed_again_is_still_in_the_pushed_history(self):
        self.add("big.bin", b"\0" * (ce_githooks.BIG_BINARY + 1))
        self.repo.run("rm", "-q", "big.bin")
        self.repo.run("commit", "-q", "-m", "wieder weg")
        self.assertIn("big.bin", self.push().stderr)

    def test_a_secret_is_stopped_by_type_and_place_and_never_printed(self):
        token = TOKENS["GitHub-Token"]
        self.add("conf.py", f'TOKEN = "{token}"\n'.encode())
        result = self.push()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GitHub-Token in `conf.py`", result.stderr)
        self.assertNotIn(token, result.stderr)
        self.assertNotIn(token[4:], result.stderr)

    def test_a_new_branch_is_scanned_and_an_old_secret_on_the_remote_stops_nothing(self):
        self.repo.run("checkout", "-q", "-b", "claude/y")
        self.add("conf.py", TOKENS["npm-Token"].encode())
        self.assertNotEqual(self.repo.run("push", "-q", "origin", "claude/y", check=False).returncode, 0)
        self.assertEqual(self.repo.run("push", "-q", "--no-verify", "origin", "claude/y").returncode, 0)
        self.add("other.txt", b"harmlos")
        self.assertEqual(self.repo.run("push", "-q", "origin", "claude/y").returncode, 0)

    def test_a_secret_added_inside_a_merge_commit_is_found(self):
        self.repo.run("checkout", "-q", "-b", "side")
        self.add("side.txt", b"side")
        self.repo.run("checkout", "-q", "claude/x")
        self.add("main.txt", b"main")
        self.repo.run("merge", "-q", "--no-commit", "--no-ff", "side")
        (self.repo.path / "conf.py").write_text(TOKENS["npm-Token"])
        self.repo.run("add", "conf.py")
        self.repo.run("commit", "-q", "-m", "merge")
        self.assertIn("npm-Token in `conf.py`", self.push().stderr)

    def test_a_clean_merge_of_a_branch_is_no_false_alarm(self):
        self.repo.run("checkout", "-q", "-b", "side")
        self.add("side.txt", b"side")
        self.repo.run("checkout", "-q", "claude/x")
        self.add("main.txt", b"main")
        self.repo.run("merge", "-q", "--no-edit", "side")
        self.assertEqual(self.push().returncode, 0)

    def test_a_git_that_cannot_show_the_diff_is_reported_not_passed_as_clean(self):
        with mock.patch.object(ce_githooks.subprocess, "Popen", return_value=subprocess.Popen(
                [sys.executable, "-c", "raise SystemExit(129)"], stdout=subprocess.PIPE, encoding="utf-8")):
            with self.assertRaisesRegex(RuntimeError, "Exit 129"):
                ce_githooks.diff_secrets(["HEAD"])

    def test_private_repositories_and_empty_pushes_are_not_scanned(self):
        self.calls.unlink()
        self.assertEqual(self.push().returncode, 0)  # nothing new: no question to GitHub
        self.assertFalse(self.calls.exists())
        self.answer.write_text("private")
        self.add("conf.py", TOKENS["npm-Token"].encode())
        self.add("big.bin", b"\0" * (ce_githooks.BIG_BINARY + 1))
        result = self.push()
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertIn("repos/o/r", self.calls.read_text())

    def test_no_answer_from_github_lets_the_push_through_with_a_message(self):
        self.answer.write_text("")
        self.add("conf.py", TOKENS["npm-Token"].encode())
        result = self.push()
        self.assertEqual(result.returncode, 0)
        self.assertIn("Sichtbarkeit von o/r nicht lesbar", result.stderr)

    def test_a_broken_scan_fails_open_and_the_ref_checks_still_block(self):
        with mock.patch.object(ce_githooks, "public_problems", side_effect=RuntimeError("kaputt")), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ce_githooks.check_hook("pre-push", io.StringIO("a 1 b 1\n")), 0)
        self.assertIn("kaputt", err.getvalue())
        self.add("conf.py", b"x")
        self.assertIn("Push auf `main`", self.repo.run("push", "-q", "origin", "HEAD:main", check=False).stderr)

    def test_the_wrapper_passes_the_remote_and_an_old_wrapper_still_means_origin(self):
        self.assertIn('check pre-push "$@"', (self.repo.path / ".git" / "ce-hooks" / "pre-push").read_text())
        self.repo.run("remote", "add", "mirror", "https://github.com/o/mirror.git")
        self.repo.run("config", f"url.{posix(self.repo.remote)}.insteadOf", "https://github.com/o/mirror.git", )
        self.add("x.txt", b"x")
        self.calls.unlink()
        self.repo.run("push", "-q", "mirror", "claude/x")
        self.assertIn("repos/o/mirror", self.calls.read_text())
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.repo.path)
        self.assertEqual(ce_githooks.remote_slugs("origin"), ["o/r"])

    def test_only_github_urls_have_a_slug(self):
        for url, slugs in (("https://github.com/o/r.git", ["o/r"]), ("git@github.com:o/r.git", ["o/r"]), ("ssh://git@github.com/o/r", ["o/r"]),
                           ("https://github.com/o/r/", ["o/r"]), ("https://gitlab.com/o/r.git", []), ("/tmp/remote.git", [])):
            self.assertEqual(ce_githooks.remote_slugs(url), slugs, url)

    def test_a_private_fetch_url_with_a_public_pushurl_is_checked_as_public(self):
        self.answer.write_text("private")
        self.repo.run("config", f"url.{posix(self.repo.remote)}.insteadOf", "https://github.com/o/pub.git", )
        self.repo.run("remote", "set-url", "--push", "origin", "https://github.com/o/pub.git")
        self.add("conf.py", TOKENS["npm-Token"].encode())
        result = self.push()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("öffentlichem Repo o/pub", result.stderr)

    def test_tracking_refs_of_a_private_fetch_url_do_not_hide_commits_from_a_public_pushurl(self):
        self.answer.write_text("private")
        self.add("conf.py", TOKENS["npm-Token"].encode())
        self.assertEqual(self.push().returncode, 0)  # now in origin/claude/x, the tracking ref of the private fetch URL
        other = self.base / "public.git"
        self.repo.git(self.base, "init", "-q", "--bare", str(other))
        self.repo.run("config", f"url.{posix(other)}.insteadOf", "https://github.com/o/pub.git", )
        self.repo.run("remote", "set-url", "--push", "origin", "https://github.com/o/pub.git")
        result = self.push()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("npm-Token in `conf.py`", result.stderr)

    def tracked_secret(self):
        """A secret in a commit origin already has; the ref line of a first push of it to another target."""
        self.answer.write_text("private")
        self.add("conf.py", TOKENS["npm-Token"].encode())
        self.assertEqual(self.push().returncode, 0)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.repo.path)
        return [f"refs/heads/claude/x {self.repo.head()} refs/heads/claude/x {'0' * 40}"]

    def problems(self, lines, url=""):
        """Called directly: these targets are GitHub URLs, so a real push would leave the machine."""
        answers = {"o/pub": "public"}
        with mock.patch.object(ce_githooks, "visibility", lambda slug: answers.get(slug, "private")):
            return " ".join(ce_githooks.public_problems(lines, "origin", url))

    def test_a_second_url_of_the_remote_is_a_target_the_tracking_refs_say_nothing_about(self):
        lines = self.tracked_secret()
        self.repo.run("remote", "set-url", "--add", "origin", "https://github.com/o/pub.git")
        self.assertIn("npm-Token in `conf.py`", self.problems(lines, "https://github.com/o/pub.git"))
        self.assertIn("npm-Token in `conf.py`", self.problems(lines))  # a wrapper from before 0.34 passes no URL

    def test_push_instead_of_is_a_target_the_tracking_refs_say_nothing_about(self):
        lines = self.tracked_secret()
        self.repo.run("config", "url.https://github.com/o/pub.git.pushInsteadOf", "https://github.com/o/r.git")
        self.assertIn("npm-Token in `conf.py`", self.problems(lines, "https://github.com/o/pub.git"))
        self.assertIn("npm-Token in `conf.py`", self.problems(lines))

    def test_with_one_target_the_tracking_refs_still_count(self):
        lines = self.tracked_secret()
        self.assertEqual(ce_githooks.ranges(lines, "origin")[0][1:], ["--not", "--remotes=origin"])
        self.assertEqual(ce_githooks.ranges(lines, "https://github.com/o/pub.git")[0][1:], ["--not"])

    def test_commits_a_private_remote_already_has_are_new_to_a_public_one(self):
        self.answer.write_text("private")
        self.add("conf.py", TOKENS["npm-Token"].encode())
        self.assertEqual(self.push().returncode, 0)
        other = self.base / "public.git"
        self.repo.git(self.base, "init", "-q", "--bare", str(other))
        self.repo.run("config", f"url.{posix(other)}.insteadOf", "https://github.com/o/pub.git", )
        self.repo.run("remote", "add", "public", "https://github.com/o/pub.git")
        result = self.repo.run("push", "-q", "public", "claude/x", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("npm-Token in `conf.py`", result.stderr)


class CommitTests(GitHookTestCase):
    def setUp(self):
        super().setUp()
        self.repo.tool("install")

    def test_commit_on_main_or_master_is_blocked_and_ce_allow_main_lets_it_through(self):
        result = self.repo.commit("on main", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Commit direkt auf `main`", result.stderr)
        self.assertEqual(self.repo.commit("allowed", env={"CE_ALLOW_MAIN": "1"}).returncode, 0)
        self.assertEqual(self.repo.commit("other value", check=False, env={"CE_ALLOW_MAIN": "yes"}).returncode, 1)
        self.repo.run("checkout", "-q", "-b", "master")
        self.assertNotEqual(self.repo.commit("on master", check=False).returncode, 0)

    def test_branches_and_detached_head_commit_normally(self):
        self.repo.run("checkout", "-q", "-b", "claude/x-patch-y")
        self.assertEqual(self.repo.commit("feature").returncode, 0)
        self.repo.run("checkout", "-q", "--detach")
        self.assertEqual(self.repo.commit("detached").returncode, 0)
        self.repo.run("checkout", "-q", "-b", "mainline")
        self.assertEqual(self.repo.commit("not main").returncode, 0)

    def test_no_verify_skips_the_hook_by_design(self):
        self.assertEqual(self.repo.run("commit", "-q", "--allow-empty", "--no-verify", "-m", "bypass").returncode, 0)

    def test_linked_worktrees_share_the_hooks(self):
        tree = self.base / "tree"
        self.repo.run("worktree", "add", "-q", "-b", "codex/x", str(tree))
        self.assertEqual(self.repo.commit("in worktree", cwd=tree).returncode, 0)
        self.repo.git(tree, "checkout", "-q", "--detach")
        self.repo.git(tree, "branch", "-q", "-f", "master", "HEAD")
        self.repo.git(tree, "checkout", "-q", "master")
        self.assertNotEqual(self.repo.commit("master in worktree", cwd=tree, check=False).returncode, 0)


class PushTests(GitHookTestCase):
    def setUp(self):
        super().setUp()
        self.repo.tool("install")
        self.repo.run("checkout", "-q", "-b", "feature")
        self.repo.commit("one")

    def push(self, *args, env=None):
        return self.repo.run("push", "-q", "origin", *args, check=False, env=env)

    def test_new_branches_and_fast_forwards_pass(self):
        self.assertEqual(self.push("feature").returncode, 0)
        self.repo.commit("two")
        self.assertEqual(self.push("feature").returncode, 0)
        self.assertEqual(self.repo.remote_sha("refs/heads/feature"), self.repo.head())

    def test_main_is_blocked_unless_ce_allow_main_and_its_deletion_always(self):
        self.repo.run("checkout", "-q", "main")
        self.repo.run("merge", "-q", "--ff-only", "feature")
        before = self.repo.remote_sha("refs/heads/main")
        result = self.push("main")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Push auf `main`", result.stderr)
        self.assertEqual(self.repo.remote_sha("refs/heads/main"), before)
        self.assertEqual(self.push("feature:master").returncode, 1, "master counts as main")
        self.assertEqual(self.push("main", env={"CE_ALLOW_MAIN": "1"}).returncode, 0)
        self.assertEqual(self.repo.remote_sha("refs/heads/main"), self.repo.head())
        result = self.push("--delete", "main", env={"CE_ALLOW_MAIN": "1"})
        self.assertIn("löschen ist gesperrt", result.stderr)

    def test_a_real_non_fast_forward_is_blocked_even_with_force_and_ce_allow_main(self):
        self.push("feature")
        self.repo.run("commit", "-q", "--amend", "-m", "rewritten")
        for env in (None, {"CE_ALLOW_MAIN": "1"}):
            result = self.push("--force", "feature", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Kein Fast-Forward", result.stderr)
        self.assertNotEqual(self.repo.remote_sha("refs/heads/feature"), self.repo.head())
        self.assertNotEqual(self.push("+feature").returncode, 0)
        self.assertNotEqual(self.push("--force-with-lease", "feature").returncode, 0)

    def test_force_on_a_fast_forward_is_invisible_to_git_hooks(self):
        self.push("feature")
        self.repo.commit("two")
        self.assertEqual(self.push("--force", "feature").returncode, 0, "documented limit: only the client guard "
                                                                          "sees the flag")

    def test_a_remote_commit_missing_locally_counts_as_non_fast_forward(self):
        self.push("feature")
        other = Repo.__new__(Repo)
        other.__dict__.update(self.repo.__dict__, path=self.base / "other")
        self.repo.git(self.base, "clone", "-q", "-b", "feature", str(self.repo.remote), str(other.path))
        other.commit("someone else")
        other.run("push", "-q", "origin", "feature", "--no-verify")
        self.repo.commit("mine")
        result = self.push("--force", "feature")
        self.assertIn("Kein Fast-Forward", result.stderr)
        self.assertEqual(self.repo.remote_sha("refs/heads/feature"), other.head())

    def test_deleting_branches_and_creating_tags_pass_overwriting_tags_does_not(self):
        self.push("feature")
        self.assertEqual(self.push("--delete", "feature").returncode, 0)
        self.assertIsNone(self.repo.remote_sha("refs/heads/feature"))
        self.repo.run("tag", "v1")
        self.assertEqual(self.push("v1").returncode, 0)
        self.repo.commit("two")
        self.repo.run("tag", "-f", "v1")
        result = self.push("--force", "v1")
        self.assertIn("Tags nie überschreiben", result.stderr)

    def test_one_bad_ref_rejects_the_whole_push(self):
        self.push("feature")
        self.repo.run("branch", "-q", "fresh")
        self.repo.run("commit", "-q", "--amend", "-m", "rewritten")
        result = self.push("--force", "fresh", "feature", "feature:main")
        self.assertIn("Kein Fast-Forward", result.stderr)
        self.assertIn("Push auf `main`", result.stderr)
        self.assertIsNone(self.repo.remote_sha("refs/heads/fresh"))

    def test_detached_head_pushes_by_target_ref(self):
        self.repo.run("checkout", "-q", "--detach")
        self.assertEqual(self.push("HEAD:refs/heads/from-detached").returncode, 0)
        self.assertNotEqual(self.push("HEAD:refs/heads/main").returncode, 0)

    def test_push_problems_fail_open_when_ancestry_is_undecidable(self):
        zero, a, b = "0" * 40, "a" * 40, "b" * 40
        lines = [f"refs/heads/x {a} refs/heads/x {b}"]
        with mock.patch.object(ce_githooks, "is_ancestor", return_value=None), redirect_stderr(io.StringIO()):
            self.assertEqual(ce_githooks.push_problems(lines, False), [])
        self.assertEqual(ce_githooks.push_problems([f"refs/heads/x {a} refs/heads/x {zero}", "garbage"], False), [])

    def test_an_internal_error_lets_git_continue(self):
        with mock.patch.object(ce_githooks, "push_problems", side_effect=RuntimeError("kaputt")), \
                redirect_stderr(io.StringIO()) as err:
            code = ce_githooks.check_hook("pre-push", io.StringIO("a 1 b 1\n"))
        self.assertEqual(code, 0)
        self.assertIn("Prüfung fehlgeschlagen", err.getvalue())


class ChainTests(GitHookTestCase):
    def hook(self, folder, name, code=0):
        log = self.base / f"{name}.log"
        path = Path(folder) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(MARKER_HOOK.format(log=log, code=code))
        path.chmod(0o755)
        return log

    def test_existing_hooks_keep_running_with_the_same_arguments_and_input(self):
        hooks = self.base / "work/.git/hooks"
        pre_commit, post_commit, pre_push = (self.hook(hooks, n) for n in ("pre-commit", "post-commit", "pre-push"))
        self.repo.tool("install")
        self.repo.run("checkout", "-q", "-b", "feature")
        self.repo.commit("one")
        self.assertTrue(pre_commit.exists() and post_commit.exists())
        self.repo.run("push", "-q", "origin", "feature")
        logged = pre_push.read_text()
        self.assertIn(f"origin {self.repo.remote}", logged)
        self.assertIn(f"refs/heads/feature {self.repo.head()} refs/heads/feature {'0' * 40}", logged)
        self.assertTrue((hooks / "pre-commit").is_file(), "the previous hook stays where it was")

    def test_a_failing_previous_hook_still_fails_and_non_executable_ones_are_skipped(self):
        hooks = self.base / "work/.git/hooks"
        self.hook(hooks, "pre-commit", code=3)
        self.repo.tool("install")
        self.repo.run("checkout", "-q", "-b", "feature")
        self.assertNotEqual(self.repo.commit("x", check=False).returncode, 0)
        if WINDOWS:  # the failing hook was checked above; there is no execute bit to take away
            self.skipTest("Windows: Git Bash takes every file with #! as executable")
        (hooks / "pre-commit").chmod(0o644)
        self.assertEqual(self.repo.commit("y").returncode, 0)

    def test_symlinked_previous_hooks_and_a_relative_local_hooks_path_are_kept_and_restored(self):
        scripts = self.base / "scripts"
        log = self.hook(scripts, "pre-commit")
        (self.repo.path / ".husky").mkdir()
        (self.repo.path / ".husky" / "pre-commit").symlink_to(scripts / "pre-commit")
        self.repo.run("config", "core.hooksPath", ".husky")
        self.repo.tool("install")
        self.repo.run("checkout", "-q", "-b", "feature")
        self.repo.commit("one")
        self.assertTrue(log.exists())
        self.repo.tool("uninstall")
        self.assertEqual(self.repo.run("config", "--get", "core.hooksPath").stdout.strip(), ".husky")
        self.assertFalse((self.base / "work/.git" / ce_githooks.FOLDER).exists())

    def test_a_global_hooks_path_is_chained_and_left_alone(self):
        global_hooks = self.base / "global hooks"
        log = self.hook(global_hooks, "commit-msg")
        with open(self.base / "gitconfig", "a") as config:
            config.write(f'[core]\n\thooksPath = "{posix(global_hooks)}"\n')
        self.repo.tool("install")
        self.assertTrue((self.base / "work/.git/ce-hooks/commit-msg").is_file())
        self.repo.run("checkout", "-q", "-b", "feature")
        self.repo.commit("one")
        self.assertTrue(log.exists(), "forwarded commit-msg ran")
        self.repo.run("checkout", "-q", "main")
        self.assertNotEqual(self.repo.commit("main", check=False).returncode, 0)
        self.repo.tool("uninstall")
        self.assertEqual(self.repo.run("config", "--local", "--get", "core.hooksPath", check=False).stdout, "")
        self.assertIn("global hooks", self.repo.run("config", "--get", "core.hooksPath").stdout)

    def test_only_hooks_present_at_install_get_forwarders(self):
        self.repo.tool("install")
        names = sorted(p.name for p in (self.base / "work/.git/ce-hooks").iterdir())
        self.assertEqual(names, ["pre-commit", "pre-push", "state.json"])
        self.hook(self.base / "work/.git/hooks", "post-merge")
        self.assertEqual(self.repo.tool("status", check=False).returncode, 1)
        self.repo.tool("install")
        self.assertTrue((self.base / "work/.git/ce-hooks/post-merge").is_file())
        self.assertEqual(self.repo.tool("status").returncode, 0)


class InstallTests(GitHookTestCase):
    def config(self):
        return self.repo.run("config", "--local", "--list").stdout

    def test_dry_run_changes_nothing_and_install_uninstall_round_trips(self):
        before = self.config()
        out = self.repo.tool("install", "--dry-run").stdout
        self.assertIn("core.hooksPath ->", out)
        self.assertEqual(self.config(), before)
        self.assertFalse((self.base / "work/.git/ce-hooks").exists())
        self.repo.tool("install")
        self.repo.tool("install")
        state = json.loads((self.base / "work/.git/ce-hooks/state.json").read_text())
        self.assertEqual(state, {"previous_local": None, "previous": str(self.base / "work/.git/hooks")})
        for name in ce_githooks.GUARDED:
            self.assertTrue(os.access(self.base / "work/.git/ce-hooks" / name, os.X_OK))
        self.assertEqual(self.repo.tool("status").returncode, 0)
        self.repo.tool("uninstall", "--dry-run")
        self.assertIn("ce-hooks", self.repo.run("config", "--get", "core.hooksPath").stdout)
        self.repo.tool("uninstall")
        self.assertEqual(self.config(), before)
        self.assertFalse((self.base / "work/.git/ce-hooks").exists())
        self.assertEqual(self.repo.commit("main again").returncode, 0)
        self.assertIn("nicht installiert", self.repo.tool("uninstall").stdout)

    def test_foreign_files_in_the_hook_folder_stop_the_install(self):
        folder = self.base / "work/.git/ce-hooks"
        folder.mkdir()
        (folder / "pre-push").write_text("#!/bin/sh\nexit 0\n")
        result = self.repo.tool("install", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("keine ce-shared-Wrapper", result.stderr)
        self.assertEqual(self.repo.run("config", "--get", "core.hooksPath", check=False).stdout, "")

    def test_paths_with_spaces_and_quotes_work(self):
        odd_base = self.base / ("it's a repo $HOME" if WINDOWS else "it's a \"repo\" $HOME")  # Windows: no " in names
        odd_base.mkdir()
        odd = Repo(odd_base, name="work tree")
        odd.tool("install")
        self.assertNotEqual(odd.commit("main", check=False).returncode, 0)
        odd.run("checkout", "-q", "-b", "feature")
        self.assertEqual(odd.commit("ok").returncode, 0)

    def test_a_missing_guard_script_forwards_to_previous_hooks_without_blocking(self):
        log = ChainTests.hook(self, self.base / "work/.git/hooks", "pre-commit")
        copy = self.base / "copy" / "ce_githooks.py"
        copy.parent.mkdir()
        shutil.copy2(SCRIPT, copy)
        self.repo.tool("install", script=copy)
        shutil.rmtree(copy.parent)
        result = self.repo.commit("main without guard")
        self.assertIn("Git-Wächter fehlt", result.stderr)
        self.assertTrue(log.exists())


class StartFailureTests(GitHookTestCase):
    """Gatehouse review (Sol high): a guard script that is there but does not start (syntax or import error) must not
    stop Git; the previous hooks still run with their input and their exit code; a deliberate refusal stays a refusal
    and never runs them."""

    def setUp(self):
        super().setUp()
        self.hooks = self.base / "work/.git/hooks"
        self.copy = self.base / "copy" / "ce_githooks.py"
        self.copy.parent.mkdir()
        shutil.copy2(SCRIPT, self.copy)

    def previous(self, name, code=0):
        return ChainTests.hook(self, self.hooks, name, code)

    def install_broken(self, text):
        shutil.copy2(SCRIPT, self.copy)
        self.repo.tool("install", script=self.copy)
        self.copy.write_text(text)

    def wrapper(self, name, *args, stdin=""):
        return subprocess.run(["sh", str(self.base / "work/.git/ce-hooks" / name), *args], cwd=self.repo.path,
                              input=stdin, capture_output=True, text=True, env=self.repo.env)

    def test_a_syntax_or_import_error_lets_the_commit_through_and_runs_the_previous_hook(self):
        for broken in ("def broken(:\n", "import ce_module_that_does_not_exist\n"):
            log = self.previous("pre-commit")
            self.install_broken(broken)
            result = self.repo.commit(f"main, guard broken: {broken[:10]}")
            self.assertIn("Git-Wächter startet nicht (Exit 1)", result.stderr)
            self.assertTrue(log.exists(), broken)
            log.unlink()

    def test_the_exit_code_of_the_previous_hook_survives_a_broken_guard(self):
        self.previous("pre-commit", code=3)
        self.install_broken("import ce_module_that_does_not_exist\n")
        self.assertNotEqual(self.repo.commit("x", check=False).returncode, 0)
        self.assertEqual(self.wrapper("pre-commit").returncode, 3)

    def test_pre_push_forwards_the_same_input_and_exit_code_when_the_guard_does_not_start(self):
        log = self.previous("pre-push", code=5)
        self.install_broken("def broken(:\n")
        lines = f"refs/heads/main {'a' * 40} refs/heads/main {'b' * 40}\nrefs/tags/v1 {'c' * 40} refs/tags/v1 {'0' * 40}\n"
        result = self.wrapper("pre-push", "origin", "url", stdin=lines)
        self.assertEqual(result.returncode, 5)
        self.assertIn("startet nicht", result.stderr)
        self.assertTrue(log.read_text().endswith(f"pre-push origin url\n{lines}"), log.read_text())
        (self.hooks / "pre-push").write_text(MARKER_HOOK.format(log=log, code=0))
        self.repo.run("checkout", "-q", "-b", "feature")
        self.repo.commit("one")
        self.assertEqual(self.repo.run("push", "-q", "origin", "main", check=False).returncode, 0, "unchecked")

    def test_a_deliberate_refusal_is_not_a_start_error_and_skips_the_previous_hooks(self):
        log, push_log = self.previous("pre-commit"), self.previous("pre-push")
        self.repo.tool("install", script=self.copy)
        result = self.repo.commit("on main", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Commit direkt auf `main`", result.stderr)
        self.assertNotIn("startet nicht", result.stderr)
        self.assertFalse(log.exists())
        self.assertEqual(self.wrapper("pre-commit").returncode, 1)
        script = subprocess.run([sys.executable, str(self.copy), "check", "pre-commit"], cwd=self.repo.path,
                                capture_output=True, text=True, env=self.repo.env)
        self.assertEqual(script.returncode, ce_githooks.REJECT)
        lines = f"refs/heads/x {'a' * 40} refs/heads/main {'b' * 40}\n"
        self.assertEqual(self.wrapper("pre-push", "origin", "url", stdin=lines).returncode, 1)
        self.assertFalse(push_log.exists())


class PrecheckBoundaryTests(GitHookTestCase):
    """The architecture boundary of Gatehouse: the client pre-check (guard.decide) does not simulate the shell; lines it
    cannot judge pass with a note, and the Git net decides when they run. The counter-examples of the last interpreter
    review (tools-judge-final.txt 7a-7e) and the measured client probe run here as real bash lines in repositories with
    the net installed - against main and against a working branch."""

    def setUp(self):
        super().setUp()
        self.repo.tool("install")
        self.repo.run("branch", "topic")
        (self.base / "second").mkdir()
        self.second = Repo(self.base / "second")
        self.second.run("switch", "-q", "-c", "work")
        self.second.tool("install")

    def sha(self, ref, repo=None):
        return (repo or self.repo).run("rev-parse", "-q", "--verify", ref, check=False).stdout.strip() or None

    def line(self, text, start="main", repo=None, env=None, shell=SHELLS[0]):
        """Pre-check verdict of the line in the start state, then the line run by bash: (verdict, completed run)."""
        repo = repo or self.repo
        repo.run("switch", "-q", start)
        result = guard.decide({"tool_name": "Bash", "tool_input": {"command": text}, "cwd": str(repo.path)}, RULES)
        message = result.get("systemMessage", "")
        verdict = ("block" if "hookSpecificOutput" in result else
                   "net" if "[commit-on-main]" in message and "Git-Netz (pre-commit)" in message else "pass")
        run = subprocess.run([*shell, text], cwd=repo.path, capture_output=True, text=True, timeout=60,
                             env=dict(repo.env, **(env or {})))
        return verdict, run

    def test_lines_that_aim_at_main_pass_the_pre_check_and_are_rejected_by_the_net(self):
        cases = [  # (start branch, line, the net rejected a commit attempt: True/False, None = depends on timing)
            ("topic", 'target=main; git switch -q "$target" && git commit --allow-empty -m x', True),  # 7a
            ("topic", "b=main; git switch -q $b; git commit --allow-empty -m x", True),
            ("topic", "f() { git switch -q main; }; f && git commit --allow-empty -m x", True),
            ("topic", "git switch -q main && git commit --allow-empty -m x | cat", True),
            ("topic", "bash -c 'git switch -q main; git commit --allow-empty -m x; exit 0'", True),
            ("topic", "git checkout -q main && git commit --allow-empty -m x", True),  # the former lexical block case
            ("main", "git switch -c fix missing-start; git commit --allow-empty -m x", True),  # 7d
            ("main", "git switch -b fix; git commit --allow-empty -m x", True),  # 7d
            ("main", "printf 'attempted\\n' > f.txt; git commit -m 'probe' .", True),  # shape of the measured probes
            ("main", "{ sleep 0.3; git switch -q topic; } | git commit --allow-empty -m x", None),  # 7c: either order
            ("main", "bash -c 'exit 0; git commit --allow-empty -m x'", False),  # 7e: no commit at all
        ]
        main = self.sha("main")
        for start, text, attempted in cases:
            verdict, run = self.line(text, start)
            case = (start, text, run.stderr)
            self.assertEqual(verdict, "net", case)
            self.assertEqual(self.sha("main"), main, case)
            if attempted is not None:
                self.assertEqual("ce-shared pre-commit: abgelehnt" in run.stderr, attempted, case)
            self.assertIsNone(self.sha("refs/heads/fix"), case)
            self.repo.run("checkout", "-q", "-f", "main")

    def test_the_same_lines_on_a_working_branch_commit_normally(self):
        cases = [  # (start branch, line, branch that gets the commit)
            ("main", 'target=topic; git switch -q "$target" && git commit --allow-empty -m x', "topic"),  # 7a
            ("topic", "save() { git commit --allow-empty -m x; }; save", "topic"),
            ("topic", "git commit --allow-empty -m x | cat", "topic"),
            ("topic", "git switch -c fix missing-start; git commit --allow-empty -m x", "topic"),  # 7d
            ("topic", "bash -c 'git commit --allow-empty -m x; exit 0'", "topic"),
        ]
        for start, text, branch in cases:
            before, main = self.sha(branch), self.sha("main")
            verdict, run = self.line(text, start)
            case = (start, text, run.stderr)
            self.assertEqual(verdict, "net", case)
            self.assertNotEqual(self.sha(branch), before, case)
            self.assertEqual(self.sha("main"), main, case)
        before = self.sha("topic")
        verdict, run = self.line("bash -c 'exit 0; git commit --allow-empty -m x'", "topic")  # 7e: not blocked
        self.assertEqual((verdict, run.returncode, self.sha("topic")), ("net", 0, before))

    def test_a_judged_leading_cd_lands_where_the_pre_check_looked(self):
        """Gangway: the one compound form the pre-check judges, `cd <path> && git commit`, run by bash and zsh (without
        startup files): a blocked line would have committed on main (the net rejects it here), a passed one commits on
        the working branch. Where the shells may land elsewhere (judge 7a/7b: `x/..` fails unless x is a folder, a
        missing logical target falls back to the physical path, a link undone by `..`) the line keeps the net's note."""
        (self.repo.path / "sub").mkdir()
        (self.second.path / "jump").symlink_to(self.repo.path / "sub")
        to_main, to_second = posix(os.path.relpath(self.repo.path, self.second.path)), posix(os.path.relpath(self.second.path, self.repo.path))
        cases = [  # (repo of the hook folder, line, verdict, the shell tried to commit on main)
            (self.second, f"cd {to_main} && git commit --allow-empty -m x", "block", True),
            (self.second, f"cd '{self.repo.path}' && git commit --allow-empty -m x", "block", True),
            (self.second, "cd jump && git commit --allow-empty -m x", "block", True),
            (self.repo, "cd sub && git commit --allow-empty -m x", "block", True),
            (self.repo, f"cd {to_second} && git commit --allow-empty -m x", "pass", False),
            (self.second, f"cd missing/../{to_main} && git commit --allow-empty -m x", "net", False),  # cd fails
            (self.second, f"cd f.txt/../{to_main} && git commit --allow-empty -m x", "net", False),  # cd fails
            (self.second, "cd jump/../sub && git commit --allow-empty -m x", "net", True),  # physically main/sub
            (self.second, "cd jump/.. && git commit --allow-empty -m x", "net", False),  # cd -L: work; cd -P: main
            (self.repo, "cd ../missing && git commit --allow-empty -m x", "net", False)]
        for shell in SHELLS:
            for repo, text, expected, on_main in cases:
                main, work = self.sha("main"), self.sha("work", self.second)
                verdict, run = self.line(text, "main" if repo is self.repo else "work", repo=repo, shell=shell)
                case = (shell[0], text, run.stderr)
                self.assertEqual(verdict, expected, case)
                self.assertEqual(self.sha("main"), main, case)
                self.assertEqual("ce-shared pre-commit: abgelehnt" in run.stderr, on_main, case)
                if expected == "pass":
                    self.assertNotEqual(self.sha("work", self.second), work, case)

    def test_a_function_with_a_prefix_git_dir_is_judged_where_it_lands(self):  # 7b
        main, work = self.sha("main"), self.sha("work", self.second)
        text = "save() {{ git commit --allow-empty -m x; }}; GIT_DIR='{}' save"
        verdict, run = self.line(text.format(self.repo.path / ".git"), "work", repo=self.second)
        self.assertEqual(verdict, "net")
        self.assertIn("ce-shared pre-commit: abgelehnt", run.stderr)
        self.assertEqual(self.sha("main"), main)
        verdict, run = self.line(text.format(self.second.path / ".git"), "main")
        self.assertEqual(verdict, "net")
        self.assertNotEqual(self.sha("work", self.second), work, run.stderr)
        self.assertEqual(self.sha("main"), main)

    def test_ce_allow_main_is_the_net_s_run_time_exception_not_the_pre_check_s(self):
        for text in ("export CE_ALLOW_MAIN=1; git commit --allow-empty -m allowed",
                     "CE_ALLOW_MAIN=1 git commit --allow-empty -m allowed"):  # boundary fix: prefixes go to the net
            main = self.sha("main")
            verdict, run = self.line(text)
            self.assertEqual(verdict, "net", text)
            self.assertNotEqual(self.sha("main"), main, (text, run.stderr))
        main = self.sha("main")
        with mock.patch.dict(os.environ, {"CE_ALLOW_MAIN": "1"}):  # even inherited by the client, no pre-check exception
            verdict, run = self.line("git commit --allow-empty -m x")
        self.assertEqual(verdict, "block", "a simple commit on main is denied before it runs")
        self.assertEqual(self.sha("main"), main, "run without the exception: the net rejects it as well")

    def test_a_git_dir_prefix_is_judged_by_the_net_where_it_lands(self):  # boundary fix: formerly read by the pre-check
        main, work = self.sha("main"), self.sha("work", self.second)
        verdict, run = self.line(f"GIT_DIR='{self.repo.path / '.git'}' git commit --allow-empty -m x", "work",
                                 repo=self.second)
        self.assertEqual(verdict, "net")
        self.assertIn("ce-shared pre-commit: abgelehnt", run.stderr)
        self.assertEqual(self.sha("main"), main)
        verdict, run = self.line(f"GIT_DIR='{self.second.path / '.git'}' git commit --allow-empty -m x", "main")
        self.assertEqual(verdict, "net")
        self.assertNotEqual(self.sha("work", self.second), work, run.stderr)


if __name__ == "__main__":
    unittest.main()
