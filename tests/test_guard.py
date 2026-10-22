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
             "Unity -projectPath /Users/ffunk/.codex/worktrees/abc/CubeEscape"],
            ['Unity -batchmode -projectPath "/Users/ffunk/Library/Caches/CubeEscape/verify/x-mcp"',
             "unity command eval --project-path /Users/ffunk/Library/Caches/CubeEscape/verify/x-mcp",
             "unity editors --installed"]),
        "unity-cli-ziel-nennen": (
            ["unity mcp", "unity command eval 'return 1;'", "unity list", "unity pipeline install",
             "cd /x && unity command quit"],
            ["unity command eval 'return 1;' --project-path /tmp/copy-mcp", "unity editors --installed",
             "unity pipeline list", "unity status", "unity_mcp.sh start", "unity --version"]),
        "heavy-resource": (
            ["python3 Tools/heavy_lock.py run --what x -- ./verify.sh run", "python3 scripts/heavy_lock.py run -- bash check_output.sh"],
            ["python3 Tools/heavy_lock.py run --resource unity -- ./verify.sh run", "python3 Tools/heavy_lock.py status"]),
        "commit-on-main": (
            ["git checkout main && git commit -m x", "git switch main && git add . && git commit -am y"],
            ["git checkout claude/ce-0.106-fastlane && git commit -m x", "git commit -m 'auf branch'", "git checkout main",
             "git checkout main && git checkout -b fix && git commit -m x", "echo 'git checkout main && git commit'"]),
    }
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
                         set(self.CASES) | set(self.WARN_CASES) | {"actions-sparsam"}, "a new rule needs cases here")
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

    def test_workflow_edits_warn_but_do_not_block(self):
        result = guard.decide(edit("/r/.github/workflows/tests.yml"), RULES)
        self.assertIn("actions-sparsam", result.get("systemMessage", ""))
        self.assertFalse(denied(result))
        self.assertEqual(guard.decide(edit("/r/docs/workflows.md"), RULES), {})

    def test_multiedit_and_write_are_file_events(self):
        payload = {"tool_name": "Write", "tool_input": {"file_path": "/r/.github/workflows/a.yaml", "content": "x"}}
        self.assertEqual(names(payload), {"actions-sparsam"})

    def test_the_old_force_push_false_positive_is_gone(self):
        # The hookify rule matched "-f" anywhere after the word push - even a test of a file named after the rule.
        command = 'for n in git-kein-force-push other; do [ -f ".claude/hookify.$n.local.md" ] && cat "$n"; done'
        self.assertNotIn("git-kein-force-push", names(bash(command)))


class CommitOnMainTests(unittest.TestCase):
    """commit-on-main judges the branch the commit really lands on, in real repositories (Gatehouse: a plain
    `git commit` in a checkout already on main used to pass)."""

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

    def blocked(self, command, cwd):
        return "commit-on-main" in names(bash(command, cwd=str(self.base / cwd)))

    def test_the_real_branch_decides(self):
        main, feature = self.base / "on main", self.base / "feature"
        for command, cwd in [("git commit -m x", "on main"), ("git commit -am 'fix'", "old"),
                             ("git add . && git commit -m x", "on main"), ('git -c user.name=x commit -m "y"', "on main"),
                             ("GIT_AUTHOR_NAME=x git commit --amend --no-edit", "on main"),
                             (f'git -C "{main}" commit -m x', "feature"), (f"cd '{main}' && git commit -m x", "feature"),
                             ("cd ../'on main'; git commit -m x", "feature"), (f"(cd '{main}' && git commit -m x)", "feature"),
                             ("git checkout feature-x && git switch main && git commit -m x", "feature"),
                             ("git status\ngit commit -m x", "on main")]:
            self.assertTrue(self.blocked(command, cwd), (command, cwd))
        for command, cwd in [("git commit -m x", "feature"), ("git commit -m x", "detached"), ("git commit -m x", "."),
                             ("git checkout -b claude/x-patch-y && git commit -m x", "on main"),
                             ("git switch -c codex/x && git commit -m x", "on main"),
                             ("git checkout main -- f.txt && git commit -m x", "feature"),
                             (f'git -C "{feature}" commit -m x', "on main"), (f"cd '{feature}' && git commit -m x", "on main"),
                             ("echo 'git commit -m x'", "on main"), ('echo "run: git commit" # git commit', "on main"),
                             ("git log --grep commit", "on main"), ("git commit-tree HEAD^{tree} -m x", "on main"),
                             ("cat > x.sh <<'EOF'\ngit commit -m x\nEOF\nchmod +x x.sh", "on main"),
                             ("git checkout main", "feature")]:
            self.assertFalse(self.blocked(command, cwd), (command, cwd))

    def test_codex_and_claude_get_the_same_answer_from_the_session_folder(self):
        payload = bash("git commit -m x", cwd=str(self.base / "on main"))
        self.assertTrue(denied(guard.decide(payload, RULES)))
        self.assertEqual(guard.decide(bash("git commit -m x", cwd=str(self.base / "feature")), RULES), {})


GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}


class ShellContextTests(unittest.TestCase):
    """Gatehouse review (Sol high): file checkouts, `--`, subshell folders, quoted heredoc-like text and other
    repositories (--git-dir, --work-tree, GIT_DIR) must neither let a commit on main pass nor block one on a branch."""

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

    def check(self, blocked, cwd, commands):
        for command in commands:
            self.assertEqual("commit-on-main" in names(bash(command, cwd=str(cwd))), blocked, (command, str(cwd)))

    def test_a_file_checkout_keeps_the_branch_and_a_branch_checkout_changes_it(self):
        self.check(True, self.main, [
            "git checkout README.md && git commit -m x", "git checkout -- README.md; git commit -m x",
            "git checkout . && git commit -am x", "git checkout '*.md' && git commit -m x",
            "git checkout HEAD && git commit -m x", "git checkout topic README.md && git commit -m x",
            "git checkout -p topic && git commit -m x", "git checkout fresh && git commit -m x"])
        self.check(False, self.main, [
            "git checkout topic && git commit -m x", "git checkout HEAD~0 && git commit -m x",
            "git checkout --detach && git commit -m x", "git branch fresh && git checkout fresh && git commit -m x",
            "git checkout -b fresh && git commit -m x", "git checkout new-from-remote && git commit -m x"])
        self.git(self.dash, "update-ref", "refs/remotes/origin/master", "HEAD")
        self.check(True, self.dash, [
            "git checkout main -- && git commit -m x", "git checkout - && git commit -m x",
            "git checkout -t origin/master && git commit -m x"])
        self.check(False, self.dash, [
            "git checkout -- main && git commit -m x", "git checkout main -- README.md && git commit -m x",
            "git switch main && git switch - && git commit -m x"])

    def test_switch_with_a_double_dash_still_switches(self):
        self.check(True, self.dash, ["git switch main -- && git commit -m x", "git switch -- main && git commit -m x",
                                     "git switch --discard-changes main && git commit -m x"])
        self.check(False, self.main, ["git switch topic -- && git commit -m x", "git switch -c fix -- && git commit -m x"])

    def test_subshells_pipelines_and_substitutions_keep_their_folder_and_branch(self):
        feature, main = self.feature, self.main
        self.check(True, main, [
            f"(cd '{feature}' && git status); git commit -m x", f"(cd '{feature}'); git commit -m x",
            f"cd '{feature}' | cat; git commit -m x", f"cd '{feature}' & git commit -m x",
            f"echo $(cd '{feature}') && git commit -m x", f"bash -c \"cd '{feature}'\" && git commit -m x",
            f"pushd '{feature}' && popd && git commit -m x", f"cd '{feature}' && cd - && git commit -m x",
            "bash -c 'git commit -m x'", "echo $(git commit -m x)",
            "echo `git commit -m x`", "sh -lc 'git commit -m x'", "if git commit -m x; then echo ok; fi",
            "command git commit -m x", "timeout 5 git commit -m x", "/usr/bin/git commit -m x",
            f"cd '{feature}' &&\ncd '{main}' && git commit -m x"])
        self.check(False, main, [
            f"{{ cd '{feature}'; }}; git commit -m x", f"(cd '{feature}' && git commit -m x)",
            f"eval \"cd '{feature}'\"; git commit -m x", f"cd '{feature}' && (cd '{main}') && git commit -m x",
            "cd ../feature && git commit -m x", "command -v git commit",
            "(git checkout topic) && git commit -m x"])  # round 2: the switch outlives the subshell (HEAD is on disk)
        self.check(True, feature, [f"(cd '{main}' && git commit -m x)", f"cd '{main}' | cat\ncd '{main}'; git commit"])

    def test_quotes_comments_and_heredocs_are_read_like_the_shell(self):
        self.check(True, self.main, [
            'echo "<<EOF"\ngit commit -m x', "echo '<<EOF' && git commit -m x", "echo x \\<<EOF\ngit commit -m x",
            "# cat <<EOF\ngit commit -m x", 'git commit -m "a <<EOF b"\necho done',
            "cat <<'EOF'\ngit commit -m in-body\nEOF\ngit commit -m real", "cat > notes.txt <<EOF\n$(git commit -m x)\nEOF",
            "bash <<'EOF'\ngit commit -m x\nEOF", "echo $((1<<2)); git commit -m x", 'cat <<<"<<EOF"; git commit -m x',
            "printf '%s\\n' \"it's\" && git commit -m x", "git commit -m 'x' 2>&1 | tail -3"])
        self.check(False, self.main, [
            "cat > notes.txt <<'EOF'\n$(git commit -m x)\nEOF", "cat <<-EOF\n\tgit commit -m x\n\tEOF\necho ok",
            'echo "git commit" # $(git commit)', "echo \"git commit -m x\"; echo 'git commit'",
            "cat <<EOF\ngit commit -m x\nEOF", "git log --format='%s' -- commit"])
        self.check(False, self.feature, ["git commit -m 'line\nmain x'", "git commit -m \"$(printf 'a\\nmain b')\""])

    def test_the_effective_repository_decides(self):
        main, feature = self.main, self.feature
        self.check(True, feature, [
            f"git --git-dir='{main}/.git' --work-tree='{main}' commit -m x", f"git --git-dir='{main}/.git' commit -m x",
            f"git '--git-dir={main}/.git' commit -m x", f"GIT_DIR='{main}/.git' git commit -m x",
            f"export GIT_DIR='{main}/.git'; git commit -m x",
            f"env GIT_DIR='{main}/.git' GIT_WORK_TREE='{main}' git commit -m x",
            f"export GIT_DIR='{main}/.git' && bash -c 'git commit -m x'",
            f"GIT_DIR='{main}/.git'; export GIT_DIR; git commit -m x"])
        self.check(False, feature, [
            f"export GIT_DIR='{main}/.git'; unset GIT_DIR; git commit -m x", f"GIT_DIR='{main}/.git' true; git commit -m x",
            f"GIT_DIR='{main}/.git'; git commit -m x",
            f"git --git-dir='{main}/.git' switch -c fix && git --git-dir='{main}/.git' commit -m x",
            f"git -C '{main}' switch -c fix && cd '{main}' && git commit -m x"])
        self.check(False, main, [f"git --git-dir='{feature}/.git' commit -m x", f"GIT_DIR='{feature}/.git' git commit -m x"])
        tree = self.base / "linked tree"
        self.git(main, "worktree", "add", "-q", "-b", "wt", str(tree))
        self.check(False, main, [f"git -C '{tree}' commit -m x"])
        self.check(True, tree, [f"git -C '{main}' commit -m x"])
        os.environ["CE_MAIN"] = str(main)
        self.check(True, feature, ['cd "$CE_MAIN" && git commit -m x'])
        os.environ["GIT_DIR"] = str(main / ".git")  # inherited from the client like the shell would
        self.check(True, feature, ["git commit -m x"])
        self.check(False, feature, ["unset GIT_DIR; git commit -m x"])

    # Round 2 (tools-judge-round2.txt): HEAD is repository state, folder and variables are process state; control flow
    # and failing switches are possible paths; functions run when called; env and attached -C; stdin scripts; $((…)).

    def test_branch_switches_outlive_subshells_but_folders_do_not(self):  # 4, 7b, coordinator-subshell-head-finding
        self.check(False, self.main, [
            "(git switch topic); git commit -m x", "bash -c 'git switch topic'; git commit -m x",
            "echo $(git switch topic) && git commit -m x", "git switch topic | cat; git commit -m x"])
        self.check(True, self.main, [f"(cd '{self.feature}' && git switch -c fix); git commit -m x"])  # other repo
        self.check(True, self.dash, [
            "(git switch main); git commit -m x", 'bash -c "git switch main"; git commit -m x',
            "x=$(git switch main); git commit -m x", "git switch main | cat; git commit -m x",
            "git switch main & git commit -m x", "(git checkout main) && git commit -m x"])

    def test_paths_that_may_not_run_and_failing_switches_never_rule_main_out(self):  # 7c
        self.check(True, self.main, [
            "false && git switch topic; git commit -m x", "git switch -c topic; git commit -m x",
            "git checkout -b topic; git commit -m x", "git switch -c topic || true; git commit -m x",
            "test -f nope && git switch topic; git commit -m x", "if false; then git switch topic; fi; git commit -m x",
            "while false; do git switch topic; done; git commit -m x", "for b in topic; do git switch $b; done; git commit",
            "git switch maybe-remote; git commit -m x", "git branch topic && git switch topic; git commit -m x",
            "case x in y) git switch topic;; esac; git commit -m x", "git checkout - ; git commit -m x"])
        self.check(False, self.main, [
            "git switch topic; git commit -m x", "git switch -c fresh2; git commit -m x",
            "git switch -c topic || exit 1; git commit -m x", "if git switch -c fix; then git commit -m x; fi",
            "git switch topic && git commit -m x || echo failed", "true || git switch main; git switch topic && git commit"])
        self.check(True, self.dash, ["git switch main || exit 1; git commit -m x"])

    def test_a_function_runs_when_it_is_called_not_when_it_is_defined(self):  # 7d
        self.check(False, self.main, [
            "save() { git commit -m x; }; printf ready", "function save { git commit -m x; }", "f() ( git commit )",
            "f() { git switch topic; }; f && git commit -m x", "function f() { git switch topic; }\nf\ngit commit"])
        self.check(True, self.main, [
            "save() { git commit -m x; }; save", "f() { git switch topic; }; git commit -m x",
            f"f() ( cd '{self.feature}' ); f; git commit -m x", "f() { f; }; f; git commit -m x"])

    def test_env_options_and_an_attached_dash_c_change_the_repository(self):  # 7e
        main, feature = self.main, self.feature
        self.check(True, feature, [f"env -C '{main}' git commit -m x", f"env --chdir='{main}' git commit -m x",
                                   f"git '-C{main}' commit -m x", f"env -C '{self.base}' -S 'git -C \"{main.name}\" commit'"])
        self.check(False, main, [f"env -C '{feature}' git commit -m x", f"git '-C{feature}' commit -m x"])
        os.environ["GIT_DIR"] = str(feature / ".git")
        self.check(False, main, ["git commit -m x", f"env -i GIT_DIR='{feature}/.git' git commit -m x"])
        self.check(True, main, ["env -u GIT_DIR git commit -m x", "env -i git commit -m x", "env --unset=GIT_DIR git commit",
                                "env -i bash -c 'git commit -m x'"])

    def test_shell_scripts_on_standard_input_are_read(self):  # 7f
        self.check(True, self.main, [
            "bash - <<'EOF'\ngit commit -m x\nEOF", "bash <<<'git commit -m x'", 'sh -s <<<"git commit -m x"',
            "zsh -s -- <<'EOF'\ngit commit -m x\nEOF", "bash 0<<<'git commit -m x'"])
        self.check(False, self.main, [
            "bash 3<<'EOF'\ngit commit -m x\nEOF", "bash <<<'git commit -m x' </dev/null", "cat <<<'git commit -m x'",
            "bash script.sh <<<'git commit -m x'"])

    def test_substitutions_inside_arithmetic_run(self):  # 7g
        self.check(True, self.main, ["echo $(( $(git commit -m x; echo 0) + 1 ))", "x=$(( `git commit -m x` ))",
                                     'echo "$(( $(git commit -m x) ))"', "echo $(( 1 + $(( $(git commit) )) ))"])
        self.check(False, self.main, ["echo $((1 << 2)) git commit", "echo $(( 2 * (3 + 4) ))"])

    def test_the_reader_never_starts_a_git_named_by_the_line_or_its_environment(self):  # 7a
        fake = self.base / "fake bin"
        fake.mkdir()
        marker = self.base / "fake git ran"
        for name in ("git", "xcrun"):
            (fake / name).write_text(f"#!/bin/sh\ntouch '{marker}'\necho main\n")
            (fake / name).chmod(0o755)
        library = self.base / "inject.dylib"
        self.check(True, self.main, [
            f"PATH='{fake}':$PATH git commit -m x", f"export PATH='{fake}'; git commit -m x",
            f"env PATH='{fake}' git commit -m x", f"DYLD_INSERT_LIBRARIES='{library}' LD_PRELOAD='{library}' git commit",
            f"GIT_EXEC_PATH='{fake}' GIT_CONFIG_PARAMETERS=\"'core.fsmonitor={fake}/git'\" git commit -m x"])
        os.environ["PATH"] = f"{fake}:{os.environ.get('PATH', '')}"
        self.check(True, self.main, ["git commit -m x"])
        self.check(False, self.feature, ["git commit -m x"])
        self.assertFalse(marker.exists(), "a git or xcrun from the line's PATH ran during the review")
        self.assertIn(guard.trusted_git(), guard.GIT_CANDIDATES)
        env = guard.git_environment({"PATH": str(fake), "DYLD_INSERT_LIBRARIES": "x", "LD_PRELOAD": "x",
                                     "GIT_EXEC_PATH": str(fake), "GIT_CONFIG_PARAMETERS": "x", "GIT_DIR": "/r/.git"})
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual(env["GIT_DIR"], "/r/.git")
        self.assertFalse({"DYLD_INSERT_LIBRARIES", "LD_PRELOAD", "GIT_EXEC_PATH", "GIT_CONFIG_PARAMETERS"} & set(env))

    def test_broken_lines_do_not_crash_the_reader(self):
        for text in ["git commit -m 'x", "echo $(git commit", "git commit <<", ")))", "((((", "`git commit",
                     "git commit \\", "cat <<'EOF\ngit commit", "$(" * 50 + "git commit"]:
            self.assertIsInstance(guard.git_commits(text, str(self.main)), str, text)


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

    def test_manifests_are_valid(self):
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
        self.assertEqual(market["plugins"][0]["name"], plugin["name"])
        self.assertEqual(market["plugins"][0]["version"], plugin["version"])
        self.assertIn("PreToolUse", hooks["hooks"])


if __name__ == "__main__":
    unittest.main()
