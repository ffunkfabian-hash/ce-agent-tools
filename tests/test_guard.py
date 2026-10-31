"""Tests of the ce-shared guard: every shared rule with matching and non-matching commands, parity with the hookify
rules it replaces, and the subagent profiles. Run: python3 -m unittest discover -s tests"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "ce-shared"
sys.path.insert(0, str(PLUGIN / "hooks"))
import guard  # noqa: E402

RULES = guard.load_rules(PLUGIN / "rules")
CE = "/Users/ffunk/Claude/Cube Escape"
WINDOWS = os.name == "nt"
WINDOWS_SYSTEM_PATH = os.pathsep.join((os.path.join(os.environ.get("SystemRoot") or "C:/Windows", "System32"),
                                       os.environ.get("SystemRoot") or "C:/Windows"))
_OUTSIDE = tempfile.TemporaryDirectory(prefix="ce-guard-")  # removed at exit
OUTSIDE_GIT = _OUTSIDE.name  # no repository: the branch of a plain git commit is unknown


def bash(command, **extra):
    return dict({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command},
                 "cwd": OUTSIDE_GIT}, **extra)


def edit(path, text="x", **extra):
    return dict({"hook_event_name": "PreToolUse", "tool_name": "Edit",
                 "tool_input": {"file_path": path, "old_string": "a", "new_string": text}}, **extra)


def names(payload):
    tool, data = payload["tool_name"], payload["tool_input"]
    return {rule["name"] for rule in guard.matching_rules(RULES, tool, data, payload.get("cwd"))}


def denied(result):
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def verdict(command, cwd):
    """commit-on-main's answer: "block", "net" (passes with a note: the Git net decides at run time) or "pass"."""
    result = guard.decide(bash(command, cwd=str(cwd)), RULES)
    if denied(result):
        return "block" if "[commit-on-main]" in result["systemMessage"] else "other block"
    message = result.get("systemMessage", "")
    if "[commit-on-main]" in message:
        return "net" if "Git-Netz (pre-commit)" in message else "other"
    return "pass"


class SharedRuleTests(unittest.TestCase):
    CASES = {
        "git-kein-force-push": (
            ["git push --force origin main", "git push -f", "git -C /x push origin +main",
             "git push --force-with-lease", "git push --mirror backup"],
            ["git push origin release/0.56.0", "git push origin main && [ -f x ] && echo ok",
             "git log --oneline -3; grep -f patterns.txt file", "git status"]),
        "git-stash-geteilt": (
            ["git stash", "git stash pop", "git stash -u", "git -C /x stash clear", "git stash && make"],
            ['git stash push -u -m "wip-123"', "git stash list --format='%H %gs'", "git stash apply abc123"]),
        "teleport-nur-frisch": (
            ["claude --teleport abc", "claude -p x --teleport"],
            ["claude -p 'hello'", "echo teleport"]),
        "unity-nie-auf-checkout": (
            [f'Unity -batchmode -projectPath "{CE}" -quit',
             f'Unity -projectPath "{CE}/.claude/worktrees/x" -batchmode',
             f'unity mcp --project-path "{CE}"', f'unity open "{CE}"',
             "Unity -projectPath /Users/ffunk/.codex/worktrees/abc/CubeEscape",
             # Windows (0.31): drive, backslashes, the ~/Claude junction, Git Bash's /g/…, an escaped blank
             r'Unity.exe -batchmode -projectPath "G:\Claude\Cube Escape" -quit',
             r'"C:\Program Files\Unity\Hub\Editor\6000.6.2f1\Editor\Unity.exe" -projectPath "C:\Users\ffunk\Claude\Cube Escape"',
             'Unity.exe -projectPath "G:/Claude/Cube Escape/.claude/worktrees/x"', "unity open '/g/Claude/Cube Escape'",
             'unity mcp --project-path "~/Claude/Cube Escape"', r"Unity -projectPath ~/Claude/Cube\ Escape -batchmode",
             r'Unity.exe -projectPath "$HOME/Claude/Cube Escape"', r"Unity.exe -projectPath C:\Users\ffunk\.codex\worktrees\a\x"],
            ['Unity -batchmode -projectPath "/Users/ffunk/Library/Caches/CubeEscape/verify/x-mcp"',
             "unity command eval --project-path /Users/ffunk/Library/Caches/CubeEscape/verify/x-mcp",
             "unity editors --installed",
             r'Unity.exe -batchmode -projectPath "C:\Users\ffunk\AppData\Local\CubeEscape\verify\Cube_Escape-94ad3a2d"',
             r"unity command eval --project-path C:\Users\ffunk\AppData\Local\CubeEscape\verify\x-mcp",
             r'Unity.exe -projectPath "G:\Claude\cubeescape-cubegen\unity"', 'git -C "G:/Claude/Cube Escape" status']),
        "unity-cli-ziel-nennen": (
            ["unity mcp", "unity command eval 'return 1;'", "unity list", "unity pipeline install",
             "cd /x && unity command quit"],
            ["unity command eval 'return 1;' --project-path /tmp/copy-mcp", "unity editors --installed",
             "unity pipeline list", "unity status", "unity_mcp.sh start", "unity --version"]),
        "heavy-resource": (
            ["python3 Tools/heavy_lock.py run --what x -- ./verify.sh run", "python3 scripts/heavy_lock.py run -- bash check_output.sh"],
            ["python3 Tools/heavy_lock.py run --resource unity -- ./verify.sh run", "python3 Tools/heavy_lock.py status"]),
        "commit-on-main": (  # outside a repository only --allow-empty-message decides; branches: CommitOnMainTests
            ["git commit --allow-empty-message -m ''", "git -C /x commit -q --allow-empty-mess -m ''",
             "git add . && git commit --allow-empty-message -m ''", "git commit -am x --allow-empty-message"],
            ["git checkout claude/ce-0.106-fastlane && git commit -m x", "git commit -m 'auf branch'", "git checkout main",
             "git checkout main && git checkout -b fix && git commit -m x", "echo 'git checkout main && git commit'",
             "git commit -m --allow-empty-message", "git commit -m 'kein --allow-empty-message'",
             "echo 'git commit --allow-empty-message'", "git commit --allow-empty -m x"]),
    }
    # Gatehouse (bounded pre-check): the old lexical hits are compound lines. They are no longer guessed but passed
    # with a note to the Git net, which rejects them at run time (test_githooks.PrecheckBoundaryTests).
    DELEGATED = ["git checkout main && git commit -m x", "git switch main && git add . && git commit -am y"]
    WARN_CASES = {
        "heavy-unlocked": (
            ["bash .agents/skills/ce-testing/scripts/verify.sh run --full", "python3 toolgen/generate_tools.py --tier full",
             "bash scripts/check_output.sh output"],
            ["python3 Tools/heavy_lock.py run --resource unity -- bash verify.sh run --full",
             "CE_HEAVY_LOCK=0 python3 toolgen/generate_tools.py --tier standard", "python3 toolgen/generate_tools.py --tier quick"]),
        "output-cap": (
            ["cat logs/toolgen_report.json", "cat output/x/qa_surface.json; echo", "python3 -m json.tool data/tools.json"],
            ["cat logs/toolgen_report.json | head -20", "jq '.tools[0]' logs/toolgen_report.json", "cat README.md",
             "tail -40 logs/run.log", "cat x.json | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))'"]),
        "judge-template": (
            ["codex exec -m gpt-6-luna 'Prüfe den Diff' < diff.txt"],
            ["codex exec \"$(cat ~/.claude/templates/pruefung.md)\" < diff.txt", "codex exec 'Checkliste: …'", "codex --version"]),
    }

    def test_every_rule_blocks_what_it_should_and_nothing_else(self):
        self.assertEqual({rule["name"] for rule in RULES},
                         set(self.CASES) | set(self.WARN_CASES) | {"actions-sparsam", "merge-nur-sightline"}, "a new rule needs cases here")
        for name, (hits, misses) in self.CASES.items():
            for command in hits:
                self.assertIn(name, names(bash(command)), command)
                self.assertTrue(denied(guard.decide(bash(command), RULES)), command)
            for command in misses:
                self.assertNotIn(name, names(bash(command)), command)
        for name, (hits, misses) in self.WARN_CASES.items():
            for command in hits:
                self.assertIn(name, names(bash(command)), command)
                self.assertFalse(denied(guard.decide(bash(command), RULES)), command)
            for command in misses:
                self.assertNotIn(name, names(bash(command)), command)

    def test_former_lexical_commit_hits_are_delegated_to_the_git_net_with_a_note(self):
        for command in self.DELEGATED:
            self.assertEqual(verdict(command, OUTSIDE_GIT), "net", command)

    def test_workflow_edits_warn_but_do_not_block(self):
        result = guard.decide(edit("/r/.github/workflows/tests.yml"), RULES)
        self.assertIn("actions-sparsam", result.get("systemMessage", ""))
        self.assertFalse(denied(result))
        self.assertEqual(guard.decide(edit("/r/docs/workflows.md"), RULES), {})

    def test_multiedit_and_write_are_file_events(self):
        payload = {"tool_name": "Write", "tool_input": {"file_path": "/r/.github/workflows/a.yaml", "content": "x"}}
        self.assertEqual(names(payload), {"actions-sparsam"})

    def test_windows_paths_of_a_workflow_warn_too(self):
        """Claude Code on Windows names files with backslashes (0.31)."""
        for path in (r"G:\Claude\x\.github\workflows\check.yml", "C:/r/.github/workflows/a.yaml"):
            self.assertEqual(names(edit(path)), {"actions-sparsam"}, path)
        for path in (r"G:\Claude\x\.github\workflows\README.md", r"G:\Claude\x\docs\workflows\a.yml",
                     r"G:\r\.github\workflows\sub\a.yml"):
            self.assertEqual(names(edit(path)), set(), path)

    def test_the_old_force_push_false_positive_is_gone(self):
        # The hookify rule matched "-f" anywhere after the word push - even a test of a file named after the rule.
        command = 'for n in git-kein-force-push other; do [ -f ".claude/hookify.$n.local.md" ] && cat "$n"; done'
        self.assertNotIn("git-kein-force-push", names(bash(command)))


class CommitOnMainTests(unittest.TestCase):
    """commit-on-main judges the branch a single simple `git commit` really lands on, in real repositories (Gatehouse:
    a plain `git commit` in a checkout already on main used to pass). Compound lines are left to the Git net, except
    `cd <path> && git … commit` (Gangway, Mistral Vibe's commit form)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin"}
        for name, branch in (("on main", "main"), ("feature", "work"), ("old", "master")):
            subprocess.run(["git", "init", "-q", "-b", branch, str(self.base / name)], check=True, env=env)
        detached = self.base / "detached"
        subprocess.run(["git", "init", "-q", str(detached)], check=True, env=env)
        subprocess.run(["git", "-C", str(detached), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q",
                        "--allow-empty", "-m", "x"], check=True, env=env)
        subprocess.run(["git", "-C", str(detached), "checkout", "-q", "--detach"], check=True, env=env)

    def check(self, expected, cases):
        for command, cwd in cases:
            self.assertEqual(verdict(command, self.base / cwd), expected, (command, cwd))

    def test_the_real_branch_decides_a_simple_commit(self):
        main, feature = self.base / "on main", self.base / "feature"
        self.check("block", [("git commit -m x", "on main"), ("git commit -am 'fix'", "old"),
                             ('git -c user.name=x commit -m "y"', "on main"),
                             (f'git -C "{main}" commit -m x', "feature"), ("/usr/bin/git commit -m x", "on main"),
                             ("git commit -m x  # note", "on main"), ("git \\\n  commit -m x", "on main"),
                             ("git commit -m x\n", "on main")])
        self.check("pass", [("git commit -m x", "feature"), ("git commit -m x", "detached"), ("git commit -m x", "."),
                            (f'git -C "{feature}" commit -m x', "on main"), ("echo 'git commit -m x'", "on main"),
                            ('echo "run: git commit" # git commit', "on main"), ("git log --grep commit", "on main"),
                            ("git commit-tree HEAD^{tree} -m x", "on main"), ("git checkout main", "feature")])

    def test_quotes_and_backslashes_inside_words_are_read_not_skipped(self):
        """Root counter-check (bounded-parent-boundary-findings.json): the raw text never mentions `git commit`, the
        shell words do. The raw mention only decides about notes; literal calls are judged from the words."""
        commands = ["'gi't commit -m x", "git 'c'ommit -m x", "g\\it commit -m x", '"git" commit -m x',
                    "gi\"t\" co'mm'it -am x", "\\git commit -m x"]
        self.check("block", [(command, "on main") for command in commands] + [(commands[0], "old")])
        self.check("pass", [(command, "feature") for command in commands] + [("'gi't log -m x", "on main"),
                                                                              ("echo 'gi't commit", "on main")])

    def test_prefix_assignments_are_left_to_the_git_net(self):
        """Gatehouse boundary fix: NAME=value prefixes are no longer read before the line runs (a quoted name such as
        'PATH'=x is no assignment at all in bash). Formerly blocked prefix cases now pass with a note; CE_ALLOW_MAIN as
        a prefix reaches the Git net, which honours it (test_githooks.PrecheckBoundaryTests)."""
        main = self.base / "on main"
        self.check("net", [("GIT_AUTHOR_NAME=x git commit --amend --no-edit", "on main"),
                           ("CE_ALLOW_MAIN=1 git commit -m x", "on main"), ("'PATH'=/missing git commit -m x", "on main"),
                           ("'GIT_DIR'=/missing git commit -m x", "on main"), (f"GIT_DIR='{main}/.git' git commit", "feature"),
                           ("X=1 git commit -m x", "feature")])
        self.check("pass", [("X=1 make test", "on main"), ("FOO=bar ls", "on main"), ("'PATH'=/x ls", "on main")])

    def test_lines_without_a_commit_get_no_note(self):
        self.check("pass", [("make test && ls -la", "on main"), ("echo a; echo b | cat > out.txt", "on main"),
                            ("for f in *.md; do wc -l \"$f\"; done", "on main"), ("cd sub && git status", "on main"),
                            ("ls $HOME", "on main"), ("(cd x; make)", "on main")])
        # review 4c: "git" from the raw line and "commit" from the read words must not add up to a mention
        self.check("pass", [("echo commit; git status", "on main"), ("echo 'commit'; git log", "on main"),
                            ("printf commit | cat; git status", "on main")])
        self.check("net", [("echo x; git commit -m x", "on main"), ("echo x; 'gi't commit -m x", "on main")])

    def test_only_lowercase_main_and_master_are_protected(self):  # review 4b: like the Git net (refs/heads/main)
        env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin"}
        for name, branch in (("upper", "MAIN"), ("titled", "Master"), ("mixed", "mAiN")):
            subprocess.run(["git", "init", "-q", "-b", branch, str(self.base / name)], check=True, env=env)
        self.check("pass", [("git commit -m x", "upper"), ("git commit -m x", "titled"), ("git commit -m x", "mixed")])
        self.check("block", [("git commit -m x", "on main"), ("git commit -m x", "old"),
                             ("git commit --allow-empty-message -m ''", "upper")])
        self.assertNotIn("commit-on-main", names(bash("git commit -m x", cwd=str(self.base / "upper"))))

    def test_a_tag_named_like_the_branch_does_not_hide_main(self):
        """Root metadata case: with a tag main next to branch main, `symbolic-ref --short HEAD` says "heads/main"; the
        full ref refs/heads/main is read instead. Temporary repositories only."""
        env = dict(GIT_ENV)
        for name, branch, tags in (("main tagged", "main", ("main",)), ("master tagged", "master", ("master",)),
                                   ("work tagged", "work", ("work", "main", "master"))):
            path = self.base / name
            subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True, env=env)
            subprocess.run(["git", "-C", str(path), "commit", "-q", "--allow-empty", "-m", "start"], check=True, env=env)
            for tag in tags:
                subprocess.run(["git", "-C", str(path), "tag", tag], check=True, env=env)
            short = subprocess.run([guard.trusted_git(), "-C", str(path), "symbolic-ref", "--short", "HEAD"], env=env,
                                   capture_output=True, text=True).stdout.strip()
            self.assertEqual(short, f"heads/{branch}", "precondition: --short is ambiguous here")
        self.check("block", [("git commit -m x", "main tagged"), ("git commit -m x", "master tagged"),
                             (f"git -C '{self.base / 'main tagged'}' commit -m x", "work tagged")])
        self.check("pass", [("git commit -m x", "work tagged"),
                            (f"git -C '{self.base / 'work tagged'}' commit -m x", "main tagged")])

    def test_compound_lines_pass_with_a_note_for_the_git_net(self):
        """Formerly read by the shell interpreter; now the Git net decides when they run."""
        main, feature = self.base / "on main", self.base / "feature"
        self.check("net", [("git add . && git commit -m x", "on main"), (f"cd '{main}' && git add . && git commit", "feature"),
                           ("cd ../'on main'; git commit -m x", "feature"),
                           (f"(cd '{main}' && git commit -m x)", "feature"),
                           ("git checkout feature-x && git switch main && git commit -m x", "feature"),
                           ("git status\ngit commit -m x", "on main"),
                           ("git checkout -b claude/x-patch-y && git commit -m x", "on main"),
                           ("git switch -c codex/x && git commit -m x", "on main"),
                           ("git checkout main -- f.txt && git commit -m x", "feature"),
                           (f"cd '{feature}' && git commit -m x; echo ok", "on main"),
                           ("cat > x.sh <<'EOF'\ngit commit -m x\nEOF\nchmod +x x.sh", "on main")])

    def leading_cd_folders(self):
        """A subfolder sub of the main checkout and a link jump in the feature checkout that points to it."""
        main = self.base / "on main"
        (main / "sub").mkdir()
        (self.base / "feature" / "jump").symlink_to(main / "sub")
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop("CDPATH", None)
        return main, self.base / "feature"

    def test_a_leading_cd_moves_the_judged_folder(self):
        """Gangway: Mistral Vibe 3.0.4 commits as `cd <path> && git commit …` (probe 30.10.2026: passed with a note and
        landed on main). That one form is judged in the cd target, relative to the hook folder like the shell."""
        main, feature = self.leading_cd_folders()
        self.check("block", [
            (f"cd '{main}' && git commit -m x", "feature"), ("cd '../on main' && git commit -m x", "feature"),
            ("cd ../'on main' && git commit -am fix", "feature"), (f"cd {self.base.as_posix()}/old && git commit -m x", "feature"),
            (f"cd '{main}'&&git commit -m x", "feature"), ("cd sub && git commit -m x", "on main"),
            ("cd '../on main/sub' && git commit -m x", "feature"), ("cd jump && git commit -m x", "feature"),
            (f"cd '{feature}' && git -C '{main}' commit -m x", "feature"), ("cd .. && git -C 'on main' commit", "feature"),
            (f"cd '{main}' && git commit -m x  # note", "feature"), ("'cd' ../'on main' && git commit -m x", "feature"),
            (f"cd '{feature}' && git commit --allow-empty-message -m ''", "on main")])
        self.check("pass", [
            (f"cd '{feature}' && git commit -m x", "on main"), ("cd ../feature && git commit -m x", "on main"),
            (f"cd '{main}' && git -C '{feature}' commit -m x", "feature"), ("cd ../detached && git commit", "on main"),
            (f"cd '{self.base}' && git commit -m x", "on main"), ("cd ../feature/jump/../../feature && git commit", "on main"),
            (f"cd '{main}' && git status", "feature")])

    @unittest.skipUnless(WINDOWS, "Windows path forms in Git Bash")
    def test_windows_cd_targets_in_drive_and_git_bash_form(self):
        """0.31: Claude Code's Bash on Windows is Git Bash. G:\\x (quoted), G:/x and /g/x are the same folder; other
        mounts, UNC paths and backslashes in relative paths are not followed."""
        main, feature = self.leading_cd_folders()
        drive, rest = main.as_posix().split(":", 1)
        self.check("block", [
            (f"cd '{main}' && git commit -m x", "feature"), (f'cd "{main}" && git commit -m x', "feature"),
            (f"cd '{main.as_posix()}' && git commit -m x", "feature"), (f"cd '/{drive.lower()}{rest}' && git commit", "feature"),
            (f"cd '{main.as_posix().lower()}/sub' && git commit -m x", "feature"), ("cd jump && git commit -m x", "feature")])
        self.check("pass", [(f"cd '{feature}' && git commit -m x", "on main"),
                            (f"cd '/{drive.lower()}{feature.as_posix().split(':', 1)[1]}' && git commit", "on main")])
        self.check("net", [
            (f"cd {main} && git commit -m x", "feature"),  # unquoted backslashes: the shell drops them
            ("cd /tmp && git commit -m x", "feature"), ("cd '..\\on main' && git commit -m x", "feature"),
            (f"cd '{drive}:on main' && git commit -m x", "feature"), ("cd '\\\\server\\share' && git commit", "feature"),
            ("cd jump/.. && git commit -m x", "feature")])  # Windows folds `..` before links: walked by hand

    def test_every_other_cd_line_stays_with_the_git_net(self):
        main, _ = self.leading_cd_folders()
        self.check("net", [
            (f"cd '{main}'; git commit -m x", "feature"), (f"cd '{main}' || git commit -m x", "feature"),
            (f"cd '{main}' && git add . && git commit -m x", "feature"), (f"cd '{main}' &&\ngit commit -m x", "feature"),
            (f"cd '{main}' && git commit -m x; echo ok", "feature"), (f"(cd '{main}' && git commit -m x)", "feature"),
            (f"cd '{main}' && git commit -m \"$msg\"", "feature"), (f"cd '{main}' && GIT_DIR=x git commit", "feature"),
            (f"cd '{main}' && bash -c 'git commit -m x'", "feature"), (f"cd -P '{main}' && git commit -m x", "feature"),
            ("cd && git commit -m x", "feature"), ("cd - && git commit -m x", "feature"), ("cd +1 && git commit", "feature"),
            ("cd -- '../on main' && git commit", "feature"), (f"cd '{main}' sub && git commit", "feature"),
            ("cd '' && git commit -m x", "feature"), ('cd "$HOME/x" && git commit -m x', "feature"),
            ("cd ~other && git commit -m x", "feature"), (f"cd '{self.base}'/on* && git commit -m x", "feature"),
            (f"/usr/bin/cd '{main}' && git commit -m x", "feature"), (f"builtin cd '{main}' && git commit", "feature"),
            (f"cd '{main}' & git commit -m x", "feature"), (f"pushd '{main}' && git commit -m x", "feature")])
        # judge 7a/7b: the shells check the folder before `..` and try the physical path when the logical one is missing
        (main / "f.txt").write_text("x")
        self.check("net", [
            ("cd missing/../'on main' && git commit -m x", "on main"), ("cd f.txt/../sub && git commit -m x", "on main"),
            ("cd missing && git commit -m x", "on main"), (f"cd '{self.base}/missing' && git commit", "on main"),
            ("cd jump/.. && git commit -m x", "feature"),  # cd -L: feature, cd -P (set -P, chase_dots): main
            ("cd jump/../sub && git commit -m x", "feature")])  # no feature/sub: the shells go physically to main/sub
        os.environ["CDPATH"] = str(self.base / "feature")  # cd searches CDPATH first: a bare name is not followed
        self.check("net", [("cd sub && git commit -m x", "on main")])
        self.check("block", [("cd ./sub && git commit -m x", "on main"), (f"cd '{main}' && git commit -m x", "feature")])

    def test_codex_and_claude_get_the_same_answer_from_the_session_folder(self):
        payload = bash("git commit -m x", cwd=str(self.base / "on main"))
        self.assertTrue(denied(guard.decide(payload, RULES)))
        self.assertEqual(guard.decide(bash("git commit -m x", cwd=str(self.base / "feature")), RULES), {})


GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}


class BoundedPrecheckTests(unittest.TestCase):
    """Gatehouse after three Sol-high reviews of the shell interpreter (tools-judge-final.txt): the pre-check judges
    only a single simple `git … commit` of literal words and its explicit repository (`-C`, `--git-dir`, `--work-tree`,
    GIT_DIR); every other line that may commit passes with a note and the Git net decides at run time. The interpreter's
    test inputs stay here as explicit delegation cases; their run-time outcome is in test_githooks.PrecheckBoundaryTests."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        for name in [n for n in os.environ if n.startswith("GIT_")]:
            del os.environ[name]
        self.main = self.repo("main repo", "main")  # also has branch topic and an untracked file named fresh
        self.git(self.main, "branch", "topic")
        (self.main / "fresh").write_text("x")
        self.feature = self.repo("feature", "work")
        self.dash = self.repo("dash", "main")  # on topic, came from main (@{-1})
        self.git(self.dash, "checkout", "-q", "-b", "topic")

    def git(self, folder, *args):
        subprocess.run(["git", "-C", str(folder), *args], check=True, env=GIT_ENV, capture_output=True)

    def repo(self, name, branch):
        path = self.base / name
        subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True, env=GIT_ENV)
        (path / "README.md").write_text("x")
        self.git(path, "add", "README.md")
        self.git(path, "commit", "-q", "-m", "start")
        return path

    def check(self, expected, cwd, commands):
        for command in commands:
            self.assertEqual(verdict(command, cwd), expected, (command, str(cwd)))

    def test_the_explicit_repository_of_a_simple_commit_decides(self):
        main, feature = self.main, self.feature
        self.check("block", feature, [
            f"git --git-dir='{main}/.git' --work-tree='{main}' commit -m x", f"git --git-dir='{main}/.git' commit -m x",
            f"git '--git-dir={main}/.git' commit -m x", f"git -C '{self.base}' -C 'main repo' commit -m x"])
        self.check("pass", main, [f"git --git-dir='{feature}/.git' commit -m x"])
        # boundary fix: a GIT_DIR prefix is no longer read (formerly block / pass); the Git net judges where it lands
        self.check("net", feature, [f"GIT_DIR='{main}/.git' git commit -m x"])
        self.check("net", main, [f"GIT_DIR='{feature}/.git' git commit -m x"])
        tree = self.base / "linked tree"
        self.git(main, "worktree", "add", "-q", "-b", "wt", str(tree))
        self.check("pass", main, [f"git -C '{tree}' commit -m x"])
        self.check("block", tree, [f"git -C '{main}' commit -m x"])
        os.environ["GIT_DIR"] = str(main / ".git")  # inherited from the client like the shell would
        self.check("block", feature, ["git commit -m x"])
        self.check("net", feature, ["unset GIT_DIR; git commit -m x"])

    def test_quotes_comments_and_continuations_are_read_like_the_shell(self):
        self.check("block", self.main, [
            'git commit -m "a <<EOF b"', "git commit -m 'x; y && z | w'", "git commit -m \"it's\" -- README.md",
            'git commit -m "say \\"hi\\""', "git commit -m x \\\n  --no-verify", "git commit -m 'a\nb'"])
        self.check("pass", self.feature, ["git commit -m 'line\nmain x'", "git commit -m 'x' # main"])
        self.check("pass", self.main, [
            "echo 'git commit -m x'", 'echo "git commit" # $(git commit)', "git log --format='%s' -- commit",
            "command -v git", "git status", "printf '%s\\n' 'git commit'"])

    def test_every_former_interpreter_case_is_delegated_on_main_and_on_a_branch(self):
        main, feature = self.main, self.feature
        compound = [
            # file and branch checkouts, `--`, `-`
            "git checkout README.md && git commit -m x", "git checkout -- README.md; git commit -m x",
            "git checkout topic && git commit -m x", "git checkout -b fresh && git commit -m x",
            "git switch main -- && git commit -m x", "git checkout - && git commit -m x",
            # subshells, pipelines, background, substitutions, nested shells, wrappers
            f"(cd '{feature}' && git status); git commit -m x", f"cd '{feature}' | cat; git commit -m x",
            f"cd '{feature}' & git commit -m x", f"echo $(cd '{feature}') && git commit -m x",
            "bash -c 'git commit -m x'", "echo $(git commit -m x)", "echo `git commit -m x`", "sh -lc 'git commit -m x'",
            "if git commit -m x; then echo ok; fi", "command git commit -m x", "timeout 5 git commit -m x",
            f"eval \"cd '{feature}'\"; git commit -m x", "(git checkout topic) && git commit -m x",
            "git switch topic | cat; git commit -m x", "git switch main & git commit -m x",
            # heredocs, here-strings, arithmetic, redirections (Claude Code's usual commit form included)
            "cat <<'EOF'\ngit commit -m in-body\nEOF\ngit commit -m real", "bash <<'EOF'\ngit commit -m x\nEOF",
            "bash <<<'git commit -m x'", "echo $(( $(git commit -m x; echo 0) + 1 ))", "git commit -m 'x' 2>&1 | tail -3",
            "git commit -m \"$(cat <<'EOF'\nMessage \"quoted\"\nEOF\n)\"", "git commit -m x > log.txt",
            "printf 'called\\n' > probe.txt; git commit -m 'probe' .",
            # repository context the pre-check does not follow
            f"export GIT_DIR='{main}/.git'; git commit -m x", f"env GIT_DIR='{main}/.git' git commit -m x",
            f"env -C '{main}' git commit -m x", f"git '-C{main}' commit -m x", "git --exec-path=/x commit -m x",
            # control flow, failing switches, functions, variables, child exit (tools-judge-final 7a-7e)
            "false && git switch topic; git commit -m x", "git switch -c topic || true; git commit -m x",
            "for b in topic; do git switch $b; done; git commit", "case x in y) git switch topic;; esac; git commit -m x",
            "save() { git commit -m x; }; save", "function f() { git switch topic; }\nf\ngit commit",
            'target=main; git switch "$target" && git commit -m x', f"save() {{ git commit -m x; }}; GIT_DIR='{main}/.git' save",
            "{ sleep 10; git switch topic; } | git commit --allow-empty -m x",
            "git switch -c fix missing-start; git commit -m x", "git switch -b fix; git commit -m x",
            "bash -c 'exit 0; git commit -m x'", 'git commit -m "$msg"', "$GIT commit -m x",
            "git commit -m x *.md", "git -C ~other commit -m x"]
        for cwd in (main, feature, self.dash):
            self.check("net", cwd, compound)

    def test_allow_empty_message_is_blocked_in_every_literal_commit_segment(self):
        self.check("block", self.feature, [
            "git commit --allow-empty-message -m ''", "git add . && git commit --allow-empty-message -m ''",
            "(git commit --allow-empty-message -m '')", "git commit -q --allow-empty-m -F msg.txt"])
        self.check("pass", self.feature, ["git commit -m --allow-empty-message", "git commit -F --allow-empty-message",
                                          "git commit -m x -- --allow-empty-message"])
        self.check("net", self.feature, ["bash -c 'git commit --allow-empty-message -m \"\"'"])

    def test_a_branch_that_cannot_be_read_fails_open_with_a_note(self):
        with mock.patch.object(guard, "trusted_git", return_value=None):
            self.check("net", self.main, ["git commit -m x"])
        with mock.patch.object(guard.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 3)):
            self.check("net", self.main, ["git commit -m x"])
        (self.base / "foreign").mkdir()
        self.check("pass", self.base / "foreign", ["git commit -m x"])  # no repository: no commit can land
        self.check("pass", self.base / "missing", ["git commit -m x"])

    def test_an_internal_error_of_the_commit_field_keeps_the_other_rules(self):
        heavy = "python3 Tools/heavy_lock" + ".py run -- ./verify.sh run; git commit -m x"
        with mock.patch.object(guard, "shell_segments", side_effect=RuntimeError("kaputt")):
            result = guard.decide(bash(heavy, cwd=str(self.main)), RULES)
            self.assertTrue(denied(result))
            self.assertIn("[heavy-resource]", result["systemMessage"])
            result = guard.decide(bash("git commit -m x", cwd=str(self.main)), RULES)
            self.assertFalse(denied(result))
            self.assertIn("Vorabprüfung fehlgeschlagen (RuntimeError: kaputt)", result["systemMessage"])
            self.assertEqual(guard.git_commits("git commit -m x", str(self.main)), "")

    def test_the_reader_never_starts_a_git_named_by_the_line_or_its_environment(self):  # round 2, 7a
        fake = self.base / "fake bin"
        fake.mkdir()
        marker = self.base / "fake git ran"
        for name in ("git", "xcrun"):
            (fake / name).write_text(f"#!/bin/sh\ntouch '{marker}'\necho main\n")
            (fake / name).chmod(0o755)
        library = self.base / "inject.dylib"
        # boundary fix: prefixes on the line are no longer read at all (formerly blocked here); still nothing runs
        self.check("net", self.main, [
            f"PATH='{fake}' git commit -m x", f"DYLD_INSERT_LIBRARIES='{library}' LD_PRELOAD='{library}' git commit",
            f"GIT_EXEC_PATH='{fake}' GIT_CONFIG_PARAMETERS=\"'core.fsmonitor={fake}/git'\" git commit -m x",
            f"PATH='{fake}':$PATH git commit -m x", f"export PATH='{fake}'; git commit -m x",
            f"env PATH='{fake}' git commit -m x"])
        # the real inherited environment of the hook process: the branch query keeps its fixed Git and environment
        os.environ.update({"PATH": f"{fake}:{os.environ.get('PATH', '')}", "DYLD_INSERT_LIBRARIES": str(library),
                           "LD_PRELOAD": str(library), "GIT_EXEC_PATH": str(fake),
                           "GIT_CONFIG_PARAMETERS": f"'core.fsmonitor={fake}/git'"})
        self.check("block", self.main, ["git commit -m x"])
        self.check("pass", self.feature, ["git commit -m x"])
        self.assertFalse(marker.exists(), "a git or xcrun from the line's PATH ran during the review")
        self.assertIn(guard.trusted_git(), guard.git_candidates())
        env = guard.git_environment({"PATH": str(fake), "DYLD_INSERT_LIBRARIES": "x", "LD_PRELOAD": "x",
                                     "GIT_EXEC_PATH": str(fake), "GIT_CONFIG_PARAMETERS": "x", "GIT_DIR": "/r/.git"})
        self.assertEqual(env["PATH"], WINDOWS_SYSTEM_PATH if WINDOWS else "/usr/bin:/bin")
        self.assertEqual(env["GIT_DIR"], "/r/.git")
        self.assertFalse({"DYLD_INSERT_LIBRARIES", "LD_PRELOAD", "GIT_EXEC_PATH", "GIT_CONFIG_PARAMETERS"} & set(env))

    def test_broken_lines_do_not_crash_the_reader(self):
        for text in ["git commit -m 'x", "echo $(git commit", "git commit <<", ")))", "((((", "`git commit",
                     "git commit \\", "cat <<'EOF\ngit commit", "$(" * 50 + "git commit", 'git commit -m "x',
                     "git -C", "git -c", "GIT_DIR= git commit", "git {", "~", "git commit ~"]:
            self.assertIsInstance(guard.git_commits(text, str(self.main)), str, text)
            self.assertIn(verdict(text, self.main), ("block", "net", "pass"), text)
        self.check("net", self.main, ["git commit -m 'x", 'git commit -m "x'])  # unterminated quote: not judged


class HookifyParityTests(unittest.TestCase):
    """The shared rules replace hookify files in the three repositories: they must block at least what those did."""
    OLD = {
        "git-kein-force-push": r"\bgit\b.*\bpush\b.*(--force|--mirror|\s-f(\s|$)|\s\+\S)",
        "git-stash-geteilt": r"\bgit\s+(-C\s+(\"[^\"]*\"|'[^']*'|\S+)\s+)?stash(\s+(pop|clear|-u|--include-untracked))?\s*($|[;&|)])",
        "teleport-nur-frisch": r"\bclaude\s+(.*\s)?--teleport\b",
        "unity-nie-auf-checkout": r"-projectPath\s+[\"']?(/Users/ffunk/Claude/Cube Escape|\S*\.codex/worktrees)",
    }

    def test_every_command_the_old_rules_blocked_is_still_blocked(self):
        import re
        corpus = [c for hits, misses in SharedRuleTests.CASES.values() for c in hits + misses]
        for name, pattern in self.OLD.items():
            for command in corpus:
                if re.search(pattern, command, re.IGNORECASE) and name != "git-kein-force-push":
                    self.assertIn(name, names(bash(command)), command)
        for command in SharedRuleTests.CASES["git-kein-force-push"][0]:
            self.assertTrue(re.search(self.OLD["git-kein-force-push"], command, re.IGNORECASE))


class AgentProfileTests(unittest.TestCase):
    PROFILE = {"agents": {
        "ce-test-runner": {"readonly_shell": True, "bash": [
            r"[\"']?\.agents/skills/ce-testing/scripts/verify\.sh[\"']?\s+(plan|run|test|path|changes)\b",
            r"python3\s+Tools/testplan\.py\s+(plan|map-check|audit)\b"], "edit": []},
        "ce-doc-keeper": {"readonly_shell": True, "bash": [r"python3\s+Tools/check_docs\.py\b"],
                          "edit": ["Docs/*.md", "Docs/**/*.md", "*.md", ".agents/skills/*/SKILL.md"],
                          "deny_edit": ["Docs/*_NOTES*.md", "Docs/*_PROGRESS*.md"]},
    }}

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.repo = Path(self.scratch.name).resolve()
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "agent-guard.json").write_text(json.dumps(self.PROFILE))
        (self.repo / "sub").mkdir()

    def tearDown(self):
        self.scratch.cleanup()

    def run_as(self, agent, payload):
        payload = dict(payload, agent_type=agent, cwd=str(self.repo / "sub"))
        return guard.decide(payload, [])

    def test_the_main_conversation_and_unknown_agents_are_not_restricted(self):
        self.assertEqual(guard.decide(dict(bash("rm -rf build"), cwd=str(self.repo)), []), {})
        self.assertEqual(self.run_as("Explore", bash("rm -rf build")), {})

    def test_the_test_runner_runs_tests_and_reads_but_changes_nothing(self):
        for command in [".agents/skills/ce-testing/scripts/verify.sh run",
                        f"{self.repo}/.agents/skills/ce-testing/scripts/verify.sh test EditMode X y",
                        f'"{self.repo}/.agents/skills/ce-testing/scripts/verify.sh" run --no-cache',
                        "./.agents/skills/ce-testing/scripts/verify.sh path",
                        "python3 Tools/testplan.py plan", "git status --short && git diff --stat",
                        "tail -50 /tmp/log.txt | grep -i error", "find . -name '*.xml'",
                        "CE_TEST_JOBS=1 .agents/skills/ce-testing/scripts/verify.sh run --full 2>&1 | tail -40",
                        "grep -n 'a|b' file.txt", "ls ~/Library/Caches >/dev/null 2>&1 || echo none"]:
            self.assertEqual(self.run_as("ce-test-runner", bash(command)), {}, command)
        for command in ["git commit -am x", "git push origin main", "rm -rf Library",
                        ".agents/skills/ce-testing/scripts/verify.sh pull Assets/X.prefab",
                        "rm -rf /tmp/.agents/skills/ce-testing/scripts/verify.sh run",
                        "bash /elsewhere/.agents/skills/ce-testing/scripts/verify.sh run",
                        "python3 Tools/testplan.py run", "echo x > Assets/a.cs", "ls & rm -rf /tmp/x",
                        "cat $(echo secret)", "find . -name x -delete", "git stash push -m x",
                        "sed -i '' s/a/b/ f", "awk 'BEGIN{system(\"rm x\")}'"]:
            self.assertTrue(denied(self.run_as("ce-test-runner", bash(command))), command)
        self.assertTrue(denied(self.run_as("ce-test-runner", edit(str(self.repo / "Docs/A.md")))))

    def test_the_doc_keeper_edits_docs_only(self):
        for path in ["Docs/TESTING.md", "Docs/archive/OLD.md", "README.md", ".agents/skills/ce-x/SKILL.md"]:
            self.assertEqual(self.run_as("ce-doc-keeper", edit(str(self.repo / path))), {}, path)
        for path in ["Assets/Scripts/A.cs", "Tools/check_docs.py", "Docs/LASER_NOTES.md", "/etc/hosts",
                     ".claude/agent-guard.json"]:
            target = path if path.startswith("/") else str(self.repo / path)
            self.assertTrue(denied(self.run_as("ce-doc-keeper", edit(target))), path)
        (self.repo / ".agents" / "skills" / "ce-x").mkdir(parents=True)
        (self.repo / ".claude" / "skills").symlink_to("../.agents/skills")
        self.assertEqual(self.run_as("ce-doc-keeper", edit(str(self.repo / ".claude/skills/ce-x/SKILL.md"))), {},
                         "the skills link of Claude Code leads to the same files")
        self.assertEqual(self.run_as("ce-doc-keeper", bash("python3 Tools/check_docs.py")), {})
        self.assertTrue(denied(self.run_as("ce-doc-keeper", bash("python3 Tools/testplan.py plan"))))

    def test_shared_rules_still_apply_inside_subagents(self):
        result = guard.decide(dict(bash("git stash"), agent_type="ce-test-runner", cwd=str(self.repo)), RULES)
        self.assertIn("git-stash-geteilt", result["systemMessage"])


class MergeNurSightlineTests(unittest.TestCase):
    """0.22: only Sightline merges. Hits are blocked for every other session; catching up with main stays free."""
    HITS = ["gh pr merge 5", "gh pr merge 5 --squash --delete-branch", "gh pr merge --auto 5", "gh pr merge",
            "gh pr merge 5 -R ffunkfabian-hash/ce-agent-tools", "gh api -X PUT repos/o/r/pulls/5/merge",
            "gh api repos/o/r/pulls/5/merge --method PUT -f merge_method=squash",
            "gh api --method=put /repos/o/r/pulls/5/merge", "gh api -XPUT repos/o/r/pulls/5/merge",
            "gh api graphql -f query='mutation { mergePullRequest(input: {pullRequestId: \"x\"}) { clientMutationId } }'",
            "gh api graphql -f query='mutation { enablePullRequestAutoMerge(input: {}) { clientMutationId } }'",
            "gh pr create --fill && gh pr merge --auto 4", "GH_TOKEN=x gh pr merge 3", "env GH_REPO=o/r gh pr merge 3",
            "bash -c 'gh pr merge 3 --squash'", "eval 'gh pr merge 3'", "/opt/homebrew/bin/gh pr merge 3", "gh pr merge $PR"]
    MISSES = ["git merge origin/main", "git merge --no-ff origin/main -m x", "git -C /x merge main", "gh pr view 5",
              "gh pr create --fill", "gh pr list", "gh pr checks 5", "gh pr merge --help", "gh pr merge 5 -h",
              "gh api repos/o/r/pulls/5/merge", "gh api -X GET repos/o/r/pulls/5/merge",
              "gh api -X PUT repos/o/r/pulls/5/comments", "gh api repos/o/r/pulls/5", "echo 'gh pr merge 5'",
              "grep -rn 'gh pr merge' docs/", "git log --grep 'pr merge'", "ls", "gh issue comment 16 -b 'nach merge'"]
    MCP = ["mcp__github__merge_pull_request", "mcp__github__enable_pr_auto_merge"]

    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CE_ROLE", None)

    @staticmethod
    def mcp(tool, cwd=OUTSIDE_GIT):
        return {"hook_event_name": "PreToolUse", "tool_name": tool, "cwd": cwd,
                "tool_input": {"owner": "o", "repo": "r", "pullNumber": 5}}

    @staticmethod
    def session(name):
        folder = tempfile.mkdtemp(prefix="ce-sightline-", dir=OUTSIDE_GIT)
        subprocess.run(["git", "init", "-q", folder], check=True)
        if name is not None:
            (Path(folder) / ".git" / "ce-session.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        return folder

    def test_other_sessions_are_blocked_with_the_postfach_message(self):
        for command in self.HITS:
            result = guard.decide(bash(command), RULES)
            self.assertTrue(denied(result), command)
            self.assertIn("[merge-nur-sightline]", result["systemMessage"], command)
            self.assertIn("Merge macht Sightline nach Prüfung – PR-Nummer und Head-SHA ins Postfach",
                          result["systemMessage"])
        for tool in self.MCP:
            self.assertTrue(denied(guard.decide(self.mcp(tool), RULES)), tool)

    def test_catching_up_with_main_and_harmless_commands_pass(self):
        for command in self.MISSES:
            self.assertNotIn("merge-nur-sightline", names(bash(command)), command)
            self.assertFalse(denied(guard.decide(bash(command), RULES)), command)
        for tool in ("mcp__github__get_pull_request", "mcp__github__create_pull_request", "mcp__ce-search__ce_search"):
            self.assertEqual(guard.decide(self.mcp(tool), RULES), {}, tool)

    def test_the_reserved_name_sightline_may_merge(self):
        for name in ("Sightline", "Sightline · Cloud", "sightline"):
            folder = self.session(name)
            for command in self.HITS:
                self.assertEqual(guard.decide(bash(command, cwd=folder), RULES), {}, (name, command))
            for tool in self.MCP:
                self.assertEqual(guard.decide(self.mcp(tool, folder), RULES), {}, (name, tool))

    def test_other_reservations_and_look_alikes_still_block(self):
        for name in ("CE-AGENT-TOOLS 0.22 „Tollgate“ · Mac", "Sightlines-Prüfung", "NotSightline", "", None):
            folder = self.session(name)
            self.assertTrue(denied(guard.decide(bash("gh pr merge 5", cwd=folder), RULES)), name)
            self.assertTrue(denied(guard.decide(self.mcp(self.MCP[0], folder), RULES)), name)

    def test_the_role_variable_names_sightline_without_a_reservation(self):
        os.environ["CE_ROLE"] = "Sightline"
        self.assertEqual(guard.decide(bash("gh pr merge 5"), RULES), {})
        self.assertEqual(guard.decide(self.mcp(self.MCP[1]), RULES), {})
        os.environ["CE_ROLE"] = "fachsitzung"
        self.assertTrue(denied(guard.decide(bash("gh pr merge 5"), RULES)))

    def test_a_broken_reading_fails_open_and_keeps_the_other_rules(self):
        with mock.patch.object(guard, "is_sightline", side_effect=OSError("kaputt")):
            self.assertEqual(guard.decide(bash("gh pr merge 5"), RULES), {})
            self.assertTrue(denied(guard.decide(bash("git push --" + "force"), RULES)))
        with mock.patch.object(guard, "merge_line", side_effect=ValueError("kaputt")):
            self.assertEqual(guard.decide(bash("gh pr merge 5"), RULES), {})
        process = subprocess.run([sys.executable, str(PLUGIN / "hooks" / "guard.py")], text=True, capture_output=True,
                                 input=json.dumps(bash("gh pr merge 5")), check=True,
                                 env={"CLAUDE_PLUGIN_ROOT": str(PLUGIN), "PATH": "/usr/bin:/bin"})
        self.assertTrue(denied(json.loads(process.stdout)))

    def test_the_claude_hook_matcher_reaches_the_github_merge_tools(self):
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
        matcher = hooks["hooks"]["PreToolUse"][0]["matcher"]
        for tool in self.MCP + ["Bash", "Edit"]:
            self.assertRegex(tool, f"^({matcher})$")
        self.assertNotRegex("mcp__github__get_pull_request", f"^({matcher})$")


class HookProcessTests(unittest.TestCase):
    def test_the_hook_answers_json_and_survives_garbage(self):
        script = PLUGIN / "hooks" / "guard.py"
        env = {"CLAUDE_PLUGIN_ROOT": str(PLUGIN), "PATH": "/usr/bin:/bin"}
        out = subprocess.run([sys.executable, str(script)], input=json.dumps(bash("git push -f")), text=True,
                             capture_output=True, env=env, check=True).stdout
        self.assertTrue(denied(json.loads(out)))
        out = subprocess.run([sys.executable, str(script)], input="not json", text=True, capture_output=True,
                             env=env, check=True).stdout
        self.assertIn("guard error", json.loads(out)["systemMessage"])

    def test_every_hook_and_the_mcp_server_start_through_ce_python(self):
        """0.31: on Windows python3 is only the Store stub (exit 49) - every registered hook silently did nothing and the
        guard blocked nothing. The registered command itself runs the guard and blocks, on every system."""
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        commands = [hook["command"] for groups in hooks.values() for group in groups for hook in group["hooks"]]
        self.assertGreaterEqual(len(commands), 7)
        for command in commands:
            self.assertTrue(command.startswith('sh "${CLAUDE_PLUGIN_ROOT}/hooks/ce-python" "${CLAUDE_PLUGIN_ROOT}/hooks/'),
                            command)
        server = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["ce-search"]
        self.assertEqual((server["command"], server["args"][0]), ("sh", "${CLAUDE_PLUGIN_ROOT}/hooks/ce-python"))
        command = hooks["PreToolUse"][0]["hooks"][0]["command"].replace("${CLAUDE_PLUGIN_ROOT}", PLUGIN.as_posix())
        sys.path.insert(0, str(PLUGIN / "tools"))
        import heavy_lock
        done = subprocess.run([heavy_lock.posix_shell("sh"), "-c", command], input=json.dumps(bash("git push -f")),
                              text=True, capture_output=True, timeout=60, env=dict(os.environ, CLAUDE_PLUGIN_ROOT=str(PLUGIN)))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(denied(json.loads(done.stdout)))

    def test_utf8_in_and_out_on_every_system(self):
        """0.31: on Windows the pipe was cp1252 - "Á" or "←" (bytes 0x81/0x90 there) made the guard fail open and let the
        line through; the adapters wrote "›" in cp1252, invalid UTF-8 for Codex."""
        env = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(PLUGIN))
        env.pop("PYTHONUTF8", None)
        env.pop("PYTHONIOENCODING", None)
        line = "git push -f origin feature  # Á ← Ünterschied"
        done = subprocess.run([sys.executable, str(PLUGIN / "hooks" / "guard.py")], capture_output=True, env=env,
                              input=json.dumps(bash(line), ensure_ascii=False).encode("utf-8"), timeout=30)
        self.assertTrue(denied(json.loads(done.stdout.decode("utf-8"))), done.stdout)
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": OUTSIDE_GIT,
                   "tool_input": {"command": f'Unity -projectPath "{CE}" -quit  # Ä'}}
        done = subprocess.run([sys.executable, str(PLUGIN / "hooks" / "codex_guard.py")], capture_output=True, env=env,
                              input=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=30)
        answer = json.loads(done.stdout.decode("utf-8"))
        self.assertEqual(answer["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("›", answer["hookSpecificOutput"]["permissionDecisionReason"])

    def test_manifests_are_valid(self):
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
        self.assertEqual(market["plugins"][0]["name"], plugin["name"])
        self.assertEqual(market["plugins"][0]["version"], plugin["version"])
        self.assertIn("PreToolUse", hooks["hooks"])


if __name__ == "__main__":
    unittest.main()
