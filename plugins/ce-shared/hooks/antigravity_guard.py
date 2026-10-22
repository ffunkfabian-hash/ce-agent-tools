#!/usr/bin/env python3
"""Antigravity PreToolUse adapter of the ce-shared guard (hooks.json; measured with IDE 2.5.5 and CLI agy 1.4.5).

Input (stdin, camelCase): toolCall {name, args} with PascalCase arguments, plus conversationId, workspacePaths, ...
The hook runs in the folder of hooks.json, not in the workspace: the shell's folder comes from args.Cwd (the IDE sends
it, the CLI does not), else the first workspace path.
- args.CommandLine (run_command) -> one Claude Bash payload.
- args.TargetFile -> one file payload: CodeContent (write_to_file) as Write, TargetContent/ReplacementContent
  (replace_file_content) as Edit, ReplacementChunks (multi_replace_file_content) as MultiEdit.
  The argument shape decides, not the tool name: the docs list run_command/write_to_file/..., the installed language
  server derives names from its step types - a renamed tool with the same arguments is still judged.
- Everything else (view_file, MCP, browser, ...): no rule applies.

Output (stdout): {"decision": "deny", "reason"} blocks; warnings and "no rule" answer {"decision": "allow"} (the docs:
"delegating to the next hook or default policy" - never auto_approve), a warning with its text as reason. Errors answer
allow with the error as reason. Exit code always 0.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import client_adapter  # noqa: E402
except Exception as error:  # a runner that does not load must still answer the client (fail open)
    client_adapter, LOAD_ERROR = None, error


def text(value):
    return value if isinstance(value, str) else ""


def normalize(payload):
    call = payload.get("toolCall") or {}
    args = call.get("args") or {}
    if isinstance(args, str):
        args = json.loads(args)
    if not isinstance(args, dict):
        return []
    workspaces = [p for p in payload.get("workspacePaths") or [] if isinstance(p, str)]
    cwd = text(args.get("Cwd")) or (workspaces[0] if workspaces else os.getcwd())
    base = {"hook_event_name": "PreToolUse", "cwd": cwd}
    if isinstance(args.get("CommandLine"), str):
        return [dict(base, tool_name="Bash", tool_input={"command": args["CommandLine"]})]
    target = args.get("TargetFile")
    if not isinstance(target, str) or not target:
        return []
    path = os.path.join(cwd, os.path.expanduser(target))
    if "CodeContent" in args:
        return [dict(base, tool_name="Write", tool_input={"file_path": path, "content": text(args["CodeContent"])})]
    chunks = args.get("ReplacementChunks")
    if isinstance(chunks, list):
        edits = [{"old_string": text(c.get("TargetContent")), "new_string": text(c.get("ReplacementContent"))}
                 for c in chunks if isinstance(c, dict)]
        return [dict(base, tool_name="MultiEdit", tool_input={"file_path": path, "edits": edits})]
    return [dict(base, tool_name="Edit", tool_input={"file_path": path, "old_string": text(args.get("TargetContent")),
                                                     "new_string": text(args.get("ReplacementContent"))})]


def render(blocks, warnings):
    if blocks:
        return {"decision": "deny", "reason": "\n\n".join(blocks)}
    if warnings:
        return {"decision": "allow", "reason": "\n\n".join(warnings)}
    return {"decision": "allow"}


def on_error(message):
    return {"decision": "allow", "reason": message}


def main(stdin=None, stdout=None):
    if client_adapter is None:
        message = (f"ce-shared guard error (antigravity): client_adapter.py lädt nicht "
                   f"({LOAD_ERROR.__class__.__name__}: {LOAD_ERROR})")
        print(message, file=sys.stderr)
        (stdout or sys.stdout).write(json.dumps(on_error(message), ensure_ascii=False) + "\n")
        return 0
    return client_adapter.run("antigravity", normalize, render, on_error, stdin, stdout)


if __name__ == "__main__":
    sys.exit(main())
