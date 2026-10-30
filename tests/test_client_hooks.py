"""Codex, Antigravity and Vibe adapters of the guard: real-shaped inputs and outputs of the clients, parity with Claude's
decide(), fail-open. Codex shapes: tests/fixtures/codex-hooks-0.162.1 (generated schemas of openai/codex at tag
rust-v0.162.1, codex-rs/hooks/schema/generated). Antigravity shapes: docs/hooks?tab=ide (Antigravity 2.x). Vibe shapes:
tests/fixtures/vibe (logged from vibe 3.0.4 tool hooks) and the hook section of Vibe's bundled vibe skill.
These are adapter tests with client-shaped stdin - not a measurement of a running client."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "plugins" / "ce-shared" / "hooks"
SCHEMAS = Path(__file__).resolve().parent / "fixtures" / "codex-hooks-0.162.1"
sys.path[:0] = [str(HOOKS), str(Path(__file__).resolve().parent)]
import antigravity_guard  # noqa: E402
import client_adapter  # noqa: E402
import codex_guard  # noqa: E402
import guard  # noqa: E402
import vibe_guard  # noqa: E402
from test_guard import OUTSIDE_GIT, RULES, SharedRuleTests  # noqa: E402

HEAVY = "python3 Tools/heavy_lock" + ".py run -- ./verify.sh run"  # split: the session's own guard reads this file too
BLOCK_CASES = {"commit-on-main": "git commit --allow-empty-message -m ''", "heavy-resource": HEAVY,
               "git-kein-force-push": "git push --force origin main"}
WARN_CASES = {"heavy-unlocked": "bash .agents/skills/ce-testing/scripts/verify.sh run --full",
              "output-cap": "cat logs/toolgen_report.json", "judge-template": "codex exec -m gpt-6-luna 'Prüfe' < d.txt"}
QUIET = ["git status", "git commit -m 'auf branch'", "python3 Tools/heavy_lock.py status", "ls -la"]


def validate(value, schema, root=None):
    """The subset of JSON Schema draft 7 the Codex schemas use; returns a list of problems."""
    root = root or schema
    if "$ref" in schema:
        return validate(value, root["definitions"][schema["$ref"].split("/")[-1]], root)
    problems = [p for part in schema.get("allOf", []) for p in validate(value, part, root)]
    kinds = schema.get("type")
    kinds = [kinds] if isinstance(kinds, str) else kinds or []
    python = {"object": dict, "string": str, "boolean": bool, "null": type(None), "array": list}
    if kinds and not any(isinstance(value, python[k]) for k in kinds):
        problems.append(f"{value!r} is not {kinds}")
    if "const" in schema and value != schema["const"]:
        problems.append(f"{value!r} != {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{value!r} not in {schema['enum']}")
    if isinstance(value, dict) and "object" in kinds:
        properties = schema.get("properties", {})
        problems += [f"missing {k}" for k in schema.get("required", []) if k not in value]
        if schema.get("additionalProperties") is False:
            problems += [f"unexpected {k}" for k in value if k not in properties]
        for key, item in value.items():
            if key in properties and properties[key] is not True:
                problems += [f"{key}: {p}" for p in validate(item, properties[key], root)]
    return problems


INPUT_SCHEMA = json.loads((SCHEMAS / "pre-tool-use.command.input.schema.json").read_text())
OUTPUT_SCHEMA = json.loads((SCHEMAS / "pre-tool-use.command.output.schema.json").read_text())


def codex_input(tool, tool_input, **extra):
    return dict({"session_id": "019a-session", "transcript_path": None, "cwd": "/Users/x/repo",
                 "hook_event_name": "PreToolUse", "model": "gpt-6-luna", "permission_mode": "default",
                 "turn_id": "turn-1", "tool_use_id": "call_1", "tool_name": tool, "tool_input": tool_input}, **extra)


def antigravity_input(name, args, **extra):
    return dict({"toolCall": {"name": name, "args": args}, "stepIdx": 19,
                 "conversationId": "ec33ebf9-0cba-4100-8142-c61503f6c587", "workspacePaths": ["/workspace/project"],
                 "transcriptPath": "~/.gemini/antigravity-ide/brain/ec33/.system_generated/logs/transcript.jsonl",
                 "artifactDirectoryPath": "~/.gemini/antigravity-ide/brain/ec33", "modelName": "gemini-3.6-flash-medium"},
                **extra)


def vibe_input(tool, tool_input, event="pre_tool", **extra):
    return dict({"session_id": "c6b65d40-8c7f", "cwd": "/Users/x/repo", "parent_session_id": None,
                 "hook_event_name": event, "transcript_path": "/Users/x/.vibe/logs/session/s.json", "tool_name": tool,
                 "tool_call_id": "QNLC7SgwB", "tool_input": tool_input}, **extra)


VIBE_MEASURED = Path(__file__).resolve().parent / "fixtures" / "vibe" / "measured-3.0.4-tool-hooks.json"


def answer(module, payload):
    """(stdout text, parsed JSON or None) of one adapter run with the payload as stdin."""
    out = io.StringIO()
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    with mock.patch.dict(os.environ, {"CE_HOOK_LOG": ""}), contextlib.redirect_stderr(io.StringIO()):
        code = module.main(io.StringIO(raw), out)
    assert code == 0
    text = out.getvalue()
    return text, json.loads(text) if text.strip() else None


def patch(*sections):
    return "*** Begin Patch\n" + "".join(sections) + "*** End Patch\n"


class CodexAdapterTests(unittest.TestCase):
    def codex(self, tool, tool_input, **extra):
        payload = codex_input(tool, tool_input, **extra)
        self.assertEqual(validate(payload, INPUT_SCHEMA), [], "test input must look like Codex 0.162.1")
        text, output = answer(codex_guard, payload)
        if output is not None:
            self.assertEqual(validate(output, OUTPUT_SCHEMA), [], text)
            for unsupported in ("continue", "stopReason", "suppressOutput", "decision"):
                self.assertNotIn(unsupported, output, "Codex fails such a hook and runs the tool")
            self.assertNotEqual((output.get("hookSpecificOutput") or {}).get("permissionDecision"), "ask")
        return output

    def assert_denied(self, output, rule):
        specific = output["hookSpecificOutput"]
        self.assertEqual((specific["hookEventName"], specific["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn(f"[{rule}]", specific["permissionDecisionReason"])
        self.assertNotIn("additionalContext", specific)

    def assert_warned(self, output, rule):
        specific = output["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", specific)
        self.assertIn(f"[{rule}]", specific["additionalContext"])
        self.assertIn(f"[{rule}]", output["systemMessage"])

    def test_blocking_rules_deny_the_shell_command(self):
        for rule, command in BLOCK_CASES.items():
            self.assert_denied(self.codex("Bash", {"command": command}), rule)

    def test_warning_rules_add_model_context_without_blocking(self):
        for rule, command in WARN_CASES.items():
            self.assert_warned(self.codex("Bash", {"command": command}), rule)

    def test_quiet_commands_print_nothing(self):
        for command in QUIET:
            self.assertIsNone(self.codex("Bash", {"command": command}), command)

    def test_an_argv_command_is_judged_as_its_shell_line(self):
        self.assert_denied(self.codex("Bash", {"command": ["bash", "-lc", BLOCK_CASES["commit-on-main"]]}),
                           "commit-on-main")
        self.assertIsNone(self.codex("Bash", {"command": ["git", "status"]}))

    def test_apply_patch_is_judged_per_file(self):
        workflow = "*** Add File: .github/workflows/ci.yml\n+on: push\n"
        self.assert_warned(self.codex("apply_patch", {"command": patch(workflow)}), "actions-sparsam")
        readme = "*** Update File: README.md\n@@\n-old\n+new\n"
        self.assertIsNone(self.codex("apply_patch", {"command": patch(readme)}))
        second = "*** Update File: .github/workflows/nightly.yaml\n@@\n-a\n+b\n"
        output = self.codex("apply_patch", {"command": patch(readme, workflow, second)})
        self.assertEqual(output["systemMessage"].count("[actions-sparsam]"), 1, "one warning per rule")
        moved = "*** Update File: docs/ci.yml\n*** Move to: .github/workflows/ci.yml\n@@\n-a\n+b\n"
        self.assert_warned(self.codex("apply_patch", {"command": patch(moved)}), "actions-sparsam")
        deleted = "*** Delete File: .github/workflows/old.yml\n"
        self.assert_warned(self.codex("apply_patch", {"command": patch(deleted)}), "actions-sparsam")

    def test_patch_paths_become_absolute_and_moves_share_their_hunks(self):
        files = codex_guard.patch_files(patch("*** Update File: a.txt\n*** Move to: /abs/b.txt\n@@\n-x\n+y\n"), "/r")
        self.assertEqual(files, [("update", "/r/a.txt", "x", "y"), ("move", "/abs/b.txt", "x", "y")])

    def test_mcp_and_other_function_tools_are_not_shell_commands(self):
        self.assertIsNone(self.codex("mcp__ce-search__ce_search", {"query": "git push --force origin main"}))
        self.assertIsNone(self.codex("update_plan", {"plan": [{"step": HEAVY, "status": "pending"}]}))

    def test_codex_blocks_merges_by_other_sessions_and_lets_sightline_through(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CE_ROLE", None)
            for command in ("gh pr merge 5 --squash", "gh api -X PUT repos/o/r/pulls/5/merge"):
                self.assert_denied(self.codex("Bash", {"command": command}), "merge-nur-sightline")
            self.assert_denied(self.codex("mcp__github__merge_pull_request", {"pullNumber": 5}), "merge-nur-sightline")
            self.assert_denied(self.codex("mcp__github__enable_pr_auto_merge", {"pullNumber": 5}),
                               "merge-nur-sightline")
            self.assertIsNone(self.codex("Bash", {"command": "git merge origin/main"}))
            self.assertIsNone(self.codex("Bash", {"command": "gh pr view 5"}))
            self.assertIsNone(self.codex("mcp__github__get_pull_request", {"pullNumber": 5}))
            os.environ["CE_ROLE"] = "sightline"
            self.assertIsNone(self.codex("Bash", {"command": "gh pr merge 5"}))
            self.assertIsNone(self.codex("mcp__github__merge_pull_request", {"pullNumber": 5}))

    def test_claude_subagent_profiles_do_not_apply_to_codex_agents(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / ".claude").mkdir()
            (Path(folder) / ".claude" / "agent-guard.json").write_text(json.dumps(
                {"agents": {"worker": {"readonly_shell": True, "bash": [], "edit": []}}}))
            self.assertIsNone(self.codex("Bash", {"command": "rm -rf build"}, cwd=folder, agent_id="a1",
                                         agent_type="worker"))
            self.assertIsNotNone(guard.decide({"tool_name": "Bash", "tool_input": {"command": "rm -rf build"},
                                               "cwd": folder, "agent_type": "worker"}, []),
                                 "the same profile still binds the Claude subagent")

    def test_errors_fail_open_with_a_message(self):
        for raw in ["not json", "[1, 2]", ""]:
            text, output = answer(codex_guard, raw)
            self.assertIn("guard error (codex)", output["systemMessage"])
            self.assertNotIn("hookSpecificOutput", output)
        with mock.patch.object(guard, "decide", side_effect=RuntimeError("kaputt")):
            text, output = answer(codex_guard, codex_input("Bash", {"command": BLOCK_CASES["commit-on-main"]}))
        self.assertEqual(output, {"systemMessage": "ce-shared guard error (codex): kaputt"})
        self.assertEqual(validate(output, OUTPUT_SCHEMA), [])


class AntigravityAdapterTests(unittest.TestCase):
    DECISIONS = {"allow", "deny"}  # never auto_approve/ask: the guard only blocks or stays out of the way

    def antigravity(self, name, args, **extra):
        text, output = answer(antigravity_guard, antigravity_input(name, args, **extra))
        self.assertIn(output["decision"], self.DECISIONS, text)
        self.assertLessEqual(set(output), {"decision", "reason"})
        return output

    def test_the_documented_example_is_allowed_without_reason(self):
        args = {"CommandLine": "npm test", "Cwd": "/workspace/project", "WaitMsBeforeAsync": 5000}
        self.assertEqual(self.antigravity("run_command", args), {"decision": "allow"})

    def test_blocking_rules_deny_run_command(self):
        for rule, command in BLOCK_CASES.items():
            output = self.antigravity("run_command", {"CommandLine": command, "Cwd": "/workspace/project"})
            self.assertEqual(output["decision"], "deny", command)
            self.assertIn(f"[{rule}]", output["reason"])

    def test_warnings_allow_with_the_rule_as_reason(self):
        for rule, command in WARN_CASES.items():
            output = self.antigravity("run_command", {"CommandLine": command})
            self.assertEqual(output["decision"], "allow")
            self.assertIn(f"[{rule}]", output["reason"])

    def test_file_tools_are_file_events(self):
        cases = [("write_to_file", {"TargetFile": "/workspace/project/.github/workflows/ci.yml", "Overwrite": False,
                                    "CodeContent": "on: push", "Description": "CI"}),
                 ("replace_file_content", {"TargetFile": ".github/workflows/ci.yml", "Instruction": "x",
                                           "TargetContent": "a", "ReplacementContent": "b", "AllowMultiple": False,
                                           "StartLine": 1, "EndLine": 2}),
                 ("multi_replace_file_content", {"TargetFile": "/w/.github/workflows/ci.yaml", "Instruction": "x",
                                                 "ReplacementChunks": [{"TargetContent": "a",
                                                                        "ReplacementContent": "b"}]})]
        for name, args in cases:
            output = self.antigravity(name, args)
            self.assertEqual(output["decision"], "allow", name)
            self.assertIn("[actions-sparsam]", output["reason"], name)
        self.assertEqual(self.antigravity("write_to_file", {"TargetFile": "/w/README.md", "CodeContent": "x"}),
                         {"decision": "allow"})

    def test_relative_targets_use_cwd_or_the_first_workspace(self):
        payloads = antigravity_guard.normalize(antigravity_input("replace_file_content", {"TargetFile": "a.md"}))
        self.assertEqual(payloads[0]["tool_input"]["file_path"], "/workspace/project/a.md")
        payloads = antigravity_guard.normalize(antigravity_input("run_command", {"CommandLine": "ls", "Cwd": "/c"}))
        self.assertEqual(payloads[0]["cwd"], "/c")

    def test_the_argument_shape_decides_not_the_tool_name(self):
        output = self.antigravity("shell_exec", {"CommandLine": BLOCK_CASES["heavy-resource"]})
        self.assertEqual(output["decision"], "deny", "a renamed command tool is still a command")
        for name, args in [("view_file", {"AbsolutePath": "/w/.github/workflows/ci.yml"}),
                           ("grep_search", {"SearchPath": "/w", "Query": "git push --force"}),
                           ("mcp_tool", {"ServerName": "x", "Arguments": {"cmd": HEAVY}}),
                           ("search_web", {"query": HEAVY})]:
            self.assertEqual(self.antigravity(name, args), {"decision": "allow"}, name)

    def test_arguments_as_json_text_are_read(self):
        output = self.antigravity("run_command", json.dumps({"CommandLine": BLOCK_CASES["commit-on-main"]}))
        self.assertEqual(output["decision"], "deny")

    def test_errors_fail_open_with_a_message(self):
        for raw in ["not json", "[1]", ""]:
            text, output = answer(antigravity_guard, raw)
            self.assertEqual(output["decision"], "allow")
            self.assertIn("guard error (antigravity)", output["reason"])
        with mock.patch.object(guard, "decide", side_effect=RuntimeError("kaputt")):
            text, output = answer(antigravity_guard,
                                  antigravity_input("run_command", {"CommandLine": BLOCK_CASES["heavy-resource"]}))
        self.assertEqual(output, {"decision": "allow", "reason": "ce-shared guard error (antigravity): kaputt"})


class VibeAdapterTests(unittest.TestCase):
    """Vibe's structured response: decision allow|deny, reason, system_message, hook_specific_output.{tool_input,
    additional_context}. Anything else on stdout is Vibe's failure path (fail open with a UI warning)."""
    KEYS = {"decision", "reason", "system_message", "hook_specific_output"}

    def vibe(self, tool, tool_input, event="pre_tool", **extra):
        text, output = answer(vibe_guard, vibe_input(tool, tool_input, event, **extra))
        if output is not None:
            self.assertLessEqual(set(output), self.KEYS, text)
            self.assertIn(output.get("decision", "allow"), {"allow", "deny"}, text)
            if output.get("decision") == "deny":
                self.assertTrue(output["reason"], "Vibe requires a reason with deny")
            self.assertNotIn("tool_input", output.get("hook_specific_output") or {}, "the guard never rewrites")
        return output

    def test_blocking_rules_deny_every_shell_tool(self):
        for tool in ("file_system.bash", "process.start", "bash", "file_system.git_bash", "file_system.powershell"):
            for rule, command in BLOCK_CASES.items():
                output = self.vibe(tool, {"timeout": 120, "command": command})
                self.assertEqual(output["decision"], "deny", (tool, command))
                self.assertIn(f"[{rule}]", output["reason"])

    def test_warnings_reach_the_ui_before_and_the_model_after_the_call(self):
        for rule, command in WARN_CASES.items():
            output = self.vibe("file_system.bash", {"command": command})
            self.assertEqual(set(output), {"system_message"}, command)
            self.assertIn(f"[{rule}]", output["system_message"])
            output = self.vibe("file_system.bash", {"command": command}, "post_tool", tool_status="success")
            self.assertIn(f"[{rule}]", output["hook_specific_output"]["additional_context"])
            self.assertNotIn("decision", output, "a post_tool deny would replace the tool output")

    def test_a_block_after_the_call_only_informs_the_model(self):
        output = self.vibe("file_system.bash", {"command": BLOCK_CASES["heavy-resource"]}, "post_tool")
        self.assertEqual(set(output), {"hook_specific_output"})
        self.assertIn("[heavy-resource]", output["hook_specific_output"]["additional_context"])

    def test_quiet_commands_and_other_tools_print_nothing(self):
        for command in QUIET:
            self.assertIsNone(self.vibe("file_system.bash", {"timeout": 120, "command": command}), command)
            self.assertIsNone(self.vibe("file_system.bash", {"command": command}, "post_tool"), command)
        for tool, args in [("file_system.read_file", {"file_path": ".github/workflows/ci.yml"}),
                           ("file_system.grep", {"pattern": BLOCK_CASES["git-kein-force-push"], "path": "."}),
                           ("agents.spawn", {"task": HEAVY, "config_name": "explore"}),
                           ("web_search", {"query": HEAVY}),
                           ("mcp_ce_search.ce_search", {"query": BLOCK_CASES["git-kein-force-push"]}),
                           ("file_system.bash", {"timeout": 120})]:
            self.assertIsNone(self.vibe(tool, args), tool)

    def test_file_tools_are_file_events_with_paths_relative_to_cwd(self):
        for tool, args in [("file_system.write_file", {"file_path": ".github/workflows/ci.yml", "content": "on: push"}),
                           ("file_system.edit", {"file_path": "/w/.github/workflows/ci.yaml", "old_string": "a",
                                                 "new_string": "b", "replace_all": False})]:
            self.assertIn("[actions-sparsam]", self.vibe(tool, args)["system_message"], tool)
        self.assertIsNone(self.vibe("file_system.write_file", {"file_path": "notes.txt", "content": "hello"}))
        payloads = vibe_guard.normalize(vibe_input("file_system.edit", {"file_path": "a.md", "old_string": "x",
                                                                        "new_string": "y"}))
        self.assertEqual(payloads, [{"hook_event_name": "PreToolUse", "cwd": "/Users/x/repo", "tool_name": "Edit",
                                     "tool_input": {"file_path": "/Users/x/repo/a.md", "old_string": "x",
                                                    "new_string": "y"}}])

    def test_a_shell_cwd_argument_wins_over_the_session_folder(self):
        payloads = vibe_guard.normalize(vibe_input("process.start", {"command": "ls", "cwd": "sub"}))
        self.assertEqual(payloads[0]["cwd"], "/Users/x/repo/sub")
        payloads = vibe_guard.normalize(vibe_input("file_system.bash", {"command": "ls", "cwd": "/abs"}))
        self.assertEqual(payloads[0]["cwd"], "/abs")

    def test_vibe_blocks_merges_by_other_sessions_and_lets_sightline_through(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CE_ROLE", None)
            self.assertEqual(self.vibe("file_system.bash", {"command": "gh pr merge 5 --squash"})["decision"], "deny")
            for tool in ("mcp_github.merge_pull_request", "mcp_github.enable_pr_auto_merge"):
                output = self.vibe(tool, {"pullNumber": 5})
                self.assertEqual(output["decision"], "deny", tool)
                self.assertIn("[merge-nur-sightline]", output["reason"])
            self.assertIsNone(self.vibe("mcp_github.get_pull_request", {"pullNumber": 5}))
            os.environ["CE_ROLE"] = "sightline"
            self.assertIsNone(self.vibe("mcp_github.merge_pull_request", {"pullNumber": 5}))

    def test_subagent_calls_get_the_same_verdict(self):
        output = self.vibe("file_system.bash", {"command": BLOCK_CASES["heavy-resource"]},
                           parent_session_id="726550b5-6040")
        self.assertEqual(output["decision"], "deny")

    def test_errors_fail_open_with_a_message(self):
        for raw in ["not json", "[1]", ""]:
            text, output = answer(vibe_guard, raw)
            self.assertEqual(set(output), {"system_message"})
            self.assertIn("guard error (vibe)", output["system_message"])
        with mock.patch.object(guard, "decide", side_effect=RuntimeError("kaputt")):
            text, output = answer(vibe_guard, vibe_input("file_system.bash", {"command": BLOCK_CASES["heavy-resource"]}))
        self.assertEqual(output, {"system_message": "ce-shared guard error (vibe): kaputt"})


class RealBranchTests(unittest.TestCase):
    """The measured client inputs of a commit on main (it passed the lexical rule). Their line is compound
    (`printf … > file; git commit …`): the bounded pre-check passes it with a note in both clients and the Git net
    rejects it at run time (test_githooks.PrecheckBoundaryTests). The same input with only its `git commit` part is
    denied before it runs - on main only, in the folder each client reports."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        for name, branch in (("main repo", "main"), ("work", "codex/x")):
            subprocess.run(["git", "init", "-q", "-b", branch, str(self.base / name)], check=True,
                           env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin"})
        self.measured = json.loads((SCHEMAS / "measured-plain-commit.json").read_text())["input"]

    def test_the_measured_line_is_left_to_the_git_net_in_both_clients(self):
        self.assertEqual(validate(self.measured, INPUT_SCHEMA), [])
        command = self.measured["tool_input"]["command"]
        for repo in ("main repo", "work"):
            _, output = answer(codex_guard, dict(self.measured, cwd=str(self.base / repo)))
            self.assertEqual(validate(output, OUTPUT_SCHEMA), [])
            self.assertNotIn("permissionDecision", output["hookSpecificOutput"], repo)
            self.assertIn("Git-Netz (pre-commit)", output["hookSpecificOutput"]["additionalContext"], repo)
            _, output = answer(antigravity_guard, antigravity_input("run_command", {"CommandLine": command},
                                                                    workspacePaths=[str(self.base / repo)]))
            self.assertEqual(output["decision"], "allow", repo)
            self.assertIn("Git-Netz (pre-commit)", output["reason"], repo)

    def test_the_plain_commit_of_the_measured_input_is_denied_on_main_only(self):
        command = self.measured["tool_input"]["command"].split("; ", 1)[1]
        self.assertTrue(command.startswith("git commit -m"), command)
        measured = dict(self.measured, tool_input=dict(self.measured["tool_input"], command=command))
        _, output = answer(codex_guard, dict(measured, cwd=str(self.base / "main repo")))
        self.assertIn("[commit-on-main]", output["hookSpecificOutput"]["permissionDecisionReason"])
        _, output = answer(codex_guard, dict(measured, cwd=str(self.base / "work")))
        self.assertIsNone(output)
        _, output = answer(antigravity_guard, antigravity_input("run_command", {"CommandLine": command},
                                                                workspacePaths=[str(self.base / "main repo")]))
        self.assertEqual(output["decision"], "deny")
        _, output = answer(antigravity_guard, antigravity_input(
            "run_command", {"CommandLine": command, "Cwd": str(self.base / "work")},
            workspacePaths=[str(self.base / "main repo")]))
        self.assertEqual(output, {"decision": "allow"}, "Cwd of the command wins over the workspace")

    def test_the_measured_antigravity_inputs_cli_without_cwd_and_ide_with_cwd(self):
        folder = Path(__file__).resolve().parent / "fixtures" / "antigravity"
        for name in ("measured-cli-1.4.5-plain-commit.json", "measured-ide-2.5.5-plain-commit.json"):
            for repo, decision in (("main repo", "deny"), ("work", "allow")):
                payload = json.loads((folder / name).read_text().replace("<workspace>", str(self.base / repo)))["input"]
                _, output = answer(antigravity_guard, payload)
                self.assertEqual(output["decision"], "allow", (name, repo, "measured compound line"))
                self.assertIn("Git-Netz (pre-commit)", output["reason"], (name, repo))
                args = payload["toolCall"]["args"]
                args["CommandLine"] = args["CommandLine"].split("; ", 1)[1]  # its plain `git commit`
                _, output = answer(antigravity_guard, payload)
                self.assertEqual(output["decision"], decision, (name, repo))

    def test_the_measured_vibe_inputs_deny_the_commit_on_main_only(self):
        inputs = json.loads(VIBE_MEASURED.read_text())["inputs"]
        commit = next(i for i in inputs if "git commit" in i["tool_input"].get("command", ""))
        for repo, decision in (("main repo", "deny"), ("work", None)):
            _, output = answer(vibe_guard, dict(commit, cwd=str(self.base / repo)))
            self.assertEqual((output or {}).get("decision"), decision, repo)
        quiet = [i for i in inputs if i["hook_event_name"] == "pre_tool" and "git commit" not in json.dumps(i)]
        self.assertEqual({i["tool_name"] for i in quiet}, {"file_system.bash", "file_system.write_file",
                                                           "file_system.edit", "process.start", "file_system.read_file"})
        for payload in quiet:
            self.assertIsNone(answer(vibe_guard, dict(payload, cwd=str(self.base / "main repo")))[1], payload)
        workflow = next(i for i in inputs if i["hook_event_name"] == "post_tool")
        _, output = answer(vibe_guard, dict(workflow, cwd=str(self.base / "work")))
        self.assertIn("[actions-sparsam]", output["hook_specific_output"]["additional_context"])


class ParityTests(unittest.TestCase):
    """The adapters add no rule and lose none: every shell case of the Claude tests gets the same verdict."""

    def test_every_shared_shell_case_has_the_claude_verdict_in_every_client(self):
        corpus = [c for hits, misses in list(SharedRuleTests.CASES.values()) + list(SharedRuleTests.WARN_CASES.values())
                  for c in hits + misses] + SharedRuleTests.DELEGATED
        for command in corpus:
            claude = guard.decide({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": OUTSIDE_GIT}, RULES)
            claude_kind = "deny" if "hookSpecificOutput" in claude else "warn" if claude else "none"
            _, codex = answer(codex_guard, codex_input("Bash", {"command": command}, cwd=OUTSIDE_GIT))
            codex_kind = ("none" if codex is None else
                          "deny" if codex["hookSpecificOutput"].get("permissionDecision") == "deny" else "warn")
            _, agy = answer(antigravity_guard,
                            antigravity_input("run_command", {"CommandLine": command, "Cwd": OUTSIDE_GIT}))
            agy_kind = "deny" if agy["decision"] == "deny" else "warn" if "reason" in agy else "none"
            _, vibe = answer(vibe_guard, vibe_input("file_system.bash", {"command": command}, cwd=OUTSIDE_GIT))
            vibe_kind = "none" if vibe is None else "deny" if vibe.get("decision") == "deny" else "warn"
            self.assertEqual((codex_kind, agy_kind, vibe_kind), (claude_kind,) * 3, command)


class AdapterProcessTests(unittest.TestCase):
    def run_script(self, name, payload, env=None):
        result = subprocess.run([sys.executable, str(HOOKS / name)], input=json.dumps(payload), text=True,
                                capture_output=True, env=dict({"PATH": "/usr/bin:/bin"}, **(env or {})))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_scripts_answer_on_stdout_exit_zero_and_log_on_request(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "hooks.jsonl"
            out = self.run_script("codex_guard.py", codex_input("Bash", {"command": BLOCK_CASES["heavy-resource"]}),
                                  {"CE_HOOK_LOG": str(log)})
            self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
            out = self.run_script("antigravity_guard.py",
                                  antigravity_input("run_command", {"CommandLine": "git status"}),
                                  {"CE_HOOK_LOG": str(log)})
            self.assertEqual(json.loads(out), {"decision": "allow"})
            out = self.run_script("vibe_guard.py", vibe_input("file_system.bash", {"command": HEAVY}),
                                  {"CE_HOOK_LOG": str(log)})
            self.assertEqual(json.loads(out)["decision"], "deny")
            entries = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([e["client"] for e in entries], ["codex", "antigravity", "vibe"])
            self.assertEqual(json.loads(entries[0]["input"])["tool_name"], "Bash")
            self.assertEqual(entries[1]["output"], {"decision": "allow"})

    def test_an_unwritable_log_does_not_change_the_answer(self):
        out = self.run_script("codex_guard.py", codex_input("Bash", {"command": "git status"}),
                              {"CE_HOOK_LOG": "/nonexistent/dir/x.jsonl"})
        self.assertEqual(out, "")

    def test_a_guard_that_does_not_load_still_gets_an_open_answer_out(self):
        for broken in ("def broken(:\n", "import ce_module_that_does_not_exist\n"):
            with tempfile.TemporaryDirectory() as folder:
                hooks = Path(folder) / "hooks"
                hooks.mkdir()
                for name in ("client_adapter.py", "codex_guard.py", "antigravity_guard.py", "vibe_guard.py"):
                    (hooks / name).write_text((HOOKS / name).read_text())
                (hooks / "guard.py").write_text(broken)
                for name, payload in (("codex_guard.py", codex_input("Bash", {"command": "git status"})),
                                      ("antigravity_guard.py", antigravity_input("run_command", {"CommandLine": "ls"})),
                                      ("vibe_guard.py", vibe_input("file_system.bash", {"command": "ls"}))):
                    result = subprocess.run([sys.executable, str(hooks / name)], input=json.dumps(payload), text=True,
                                            capture_output=True, env={"PATH": "/usr/bin:/bin"})
                    answer = json.loads(result.stdout)
                    self.assertEqual(result.returncode, 0, (name, broken))
                    self.assertIn("guard.py lädt nicht", answer.get("systemMessage") or answer.get("reason")
                                  or answer.get("system_message"))
                    self.assertEqual(answer.get("decision", "allow"), "allow")

    def test_each_client_entry_answers_open_in_its_format_when_a_shared_module_does_not_load(self):
        """Round 2 (7h): the entries import client_adapter (and it guard) before run(); a syntax or import error in
        either must still give exit 0 and a valid answer of that client, as a real process."""
        for module in ("client_adapter.py", "guard.py"):
            for broken in ("def broken(:\n", "import ce_module_that_does_not_exist\n"):
                with tempfile.TemporaryDirectory() as folder:
                    hooks = Path(folder) / "hooks"
                    hooks.mkdir()
                    for name in ("client_adapter.py", "codex_guard.py", "antigravity_guard.py", "vibe_guard.py",
                                 "guard.py"):
                        (hooks / name).write_text((HOOKS / name).read_text())
                    (hooks / module).write_text(broken)
                    for name, payload in (("codex_guard.py", codex_input("Bash", {"command": "git commit -m x"})),
                                          ("antigravity_guard.py",
                                           antigravity_input("run_command", {"CommandLine": "git commit -m x"})),
                                          ("vibe_guard.py", vibe_input("file_system.bash",
                                                                       {"command": "git commit -m x"}))):
                        result = subprocess.run([sys.executable, str(hooks / name)], input=json.dumps(payload),
                                                text=True, capture_output=True, env={"PATH": "/usr/bin:/bin"})
                        answer = json.loads(result.stdout)
                        case = (module, broken, name)
                        self.assertEqual(result.returncode, 0, case)
                        self.assertIn("lädt nicht", answer.get("systemMessage") or answer.get("reason")
                                      or answer.get("system_message"), case)
                        if name == "codex_guard.py":
                            self.assertEqual(validate(answer, OUTPUT_SCHEMA), [], case)
                            self.assertNotIn("hookSpecificOutput", answer, case)
                        elif name == "vibe_guard.py":
                            self.assertEqual(set(answer), {"system_message"}, case)
                        else:
                            self.assertEqual(answer["decision"], "allow", case)

    def test_adapters_use_their_own_rules_not_an_inherited_plugin_root(self):
        with tempfile.TemporaryDirectory() as folder:
            out = self.run_script("codex_guard.py", codex_input("Bash", {"command": BLOCK_CASES["heavy-resource"]}),
                                  {"CLAUDE_PLUGIN_ROOT": folder})
        self.assertIn("deny", out)


if __name__ == "__main__":
    unittest.main()
