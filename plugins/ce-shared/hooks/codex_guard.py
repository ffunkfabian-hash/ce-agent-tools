#!/usr/bin/env python3
"""Codex PreToolUse adapter of the ce-shared guard (Codex hooks, checked against codex-cli 0.162.1).

Input (stdin, snake_case): tool_name "Bash" (shell and unified exec) with tool_input.command, "apply_patch" with the
patch text in tool_input.command, "mcp__<server>__<tool>" or a local function name with its own arguments.
- Bash -> one Claude Bash payload.
- apply_patch -> one Write/Edit payload per file of the patch (Add/Update/Delete/Move to), so the file rules see each path.
- Everything else (MCP, update_plan, ...) is neither a shell command nor a file edit for the rules: no output.

Output (stdout): blocks as hookSpecificOutput.permissionDecision "deny" plus systemMessage, warnings as
hookSpecificOutput.additionalContext (model) plus systemMessage (UI), nothing when no rule matches. Never "ask",
"continue" or "stopReason" - Codex treats those as a failed hook and runs the tool anyway. Exit code always 0.

Install as a user hook (~/.codex/hooks.json, matcher "^(Bash|apply_patch)$"); Codex runs it only after /hooks trust.
0.21: Codex has no Stop hook, so new Postfach messages of a reserved worktree come along as additionalContext of this hook
(postfach_inbox.pending_text: file reads only, a detached read at most every three minutes; nothing when there is no message).
It is no rule and never blocks; a broken inbox only means no hint.
"""
import json
import os
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import client_adapter  # noqa: E402
except Exception as error:  # a runner that does not load must still answer the client (fail open)
    client_adapter, LOAD_ERROR = None, error
try:
    import postfach_inbox  # noqa: E402
except Exception:  # noqa: BLE001 - no inbox, no hint
    postfach_inbox = None
MAIL = {"cwd": ""}

PATCH_MARKERS = {"*** Add File: ": "add", "*** Update File: ": "update", "*** Delete File: ": "delete",
                 "*** Move to: ": "move"}
SHELLS = {"bash", "sh", "zsh", "/bin/bash", "/bin/sh", "/bin/zsh", "/usr/bin/bash", "/usr/bin/zsh"}


def shell_text(command):
    """Shell line of a Bash tool_input.command - a string today; an argv list [shell, -lc, line] is unwrapped."""
    if isinstance(command, str):
        return command
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        if len(command) == 3 and command[0] in SHELLS and command[1] in ("-c", "-lc"):
            return command[2]
        return shlex.join(command)
    return ""


def patch_files(text, cwd):
    """[(kind, absolute path, removed text, added text)] of an apply_patch body (apply_patch's own envelope)."""
    files, current = [], None
    for line in text.splitlines():
        marker = next((m for m in PATCH_MARKERS if line.startswith(m)), None)
        if marker:
            path = line[len(marker):].strip()
            if path:
                kind = PATCH_MARKERS[marker]
                hunks = (current[2], current[3]) if kind == "move" and current else ([], [])
                current = [kind, os.path.join(cwd, path), *hunks]  # a move shares the hunks of its update
                files.append(current)
            continue
        if line.startswith("*** "):  # Begin/End Patch, End of File
            continue
        if current is None:
            continue
        if line.startswith("+"):
            current[3].append(line[1:])
        elif line.startswith("-"):
            current[2].append(line[1:])
    return [(kind, path, "\n".join(old), "\n".join(new)) for kind, path, old, new in files]


def normalize(payload):
    tool, data = payload.get("tool_name", ""), payload.get("tool_input")
    data = data if isinstance(data, dict) else {}
    cwd = data.get("workdir") or payload.get("cwd") or os.getcwd()
    MAIL["cwd"] = cwd
    if tool == "Bash":
        return [{"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": cwd,
                 "tool_input": {"command": shell_text(data.get("command"))}}]
    if tool == "apply_patch":
        payloads = []
        for kind, path, old, new in patch_files(shell_text(data.get("command")), cwd):
            if kind == "add":
                payloads.append({"tool_name": "Write", "tool_input": {"file_path": path, "content": new}})
            else:  # update, delete, move: the destination of a move gets the same hunks
                payloads.append({"tool_name": "Edit", "tool_input": {"file_path": path, "old_string": old,
                                                                     "new_string": new}})
        return [dict(p, hook_event_name="PreToolUse", cwd=cwd) for p in payloads]
    return []


def mail_hint(cwd):
    if postfach_inbox is None or not cwd:
        return ""
    try:
        return postfach_inbox.pending_text(cwd)
    except Exception:  # noqa: BLE001 - the guard answer must not depend on the Postfach
        return ""


def render(blocks, warnings):
    note = "" if blocks else mail_hint(MAIL["cwd"])
    if note:
        warnings = [*warnings, note]
    if blocks:
        message = "\n\n".join(blocks)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": message}, "systemMessage": message}
    if warnings:
        message = "\n\n".join(warnings)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": message},
                "systemMessage": message}
    return None


def on_error(message):
    return {"systemMessage": message}


def main(stdin=None, stdout=None):
    if client_adapter is None:
        message = (f"ce-shared guard error (codex): client_adapter.py lädt nicht "
                   f"({LOAD_ERROR.__class__.__name__}: {LOAD_ERROR})")
        print(message, file=sys.stderr)
        (stdout or sys.stdout).write(json.dumps(on_error(message), ensure_ascii=False) + "\n")
        return 0
    return client_adapter.run("codex", normalize, render, on_error, stdin, stdout)


if __name__ == "__main__":
    sys.exit(main())
