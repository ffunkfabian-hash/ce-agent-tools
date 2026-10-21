"""Tests of the ce-shared guard: every shared rule with matching and non-matching commands, parity with the hookify
rules it replaces, and the subagent profiles. Run: python3 -m unittest discover -s tests"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "ce-shared"
sys.path.insert(0, str(PLUGIN / "hooks"))
import guard  # noqa: E402

RULES = guard.load_rules(PLUGIN / "rules")
CE = "/Users/ffunk/Claude/Cube Escape"


def bash(command, **extra):
    return dict({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}, **extra)


def edit(path, text="x", **extra):
    return dict({"hook_event_name": "PreToolUse", "tool_name": "Edit",
                 "tool_input": {"file_path": path, "old_string": "a", "new_string": text}}, **extra)


def names(payload):
    tool, data = payload["tool_name"], payload["tool_input"]
    return {rule["name"] for rule in guard.matching_rules(RULES, tool, data)}


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
            ["git checkout claude/ce-0.106-fastlane && git commit -m x", "git commit -m 'auf branch'", "git checkout main"]),
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
