#!/usr/bin/env python3
"""Mistral Vibe tool-hook adapter of the ce-shared guard (hooks.toml; measured with vibe 3.0.4).

Input (stdin, snake_case): hook_event_name "pre_tool" or "post_tool", cwd (the session folder), parent_session_id (set in
subagents, which inherit the hook config), tool_name always qualified (file_system.bash, process.start, mcp_<server>.<tool>)
and tool_input with the model's arguments - file paths may be relative to cwd.
- A shell tool (bash, git_bash, powershell, process.start) -> one Claude Bash payload; its own cwd argument wins.
- file_path with content (write_file) -> Write, with old_string/new_string (edit) -> Edit. The argument shape decides, as
  in the Antigravity adapter, so an MCP file tool with the same arguments is judged too.
- mcp_<server>.<tool> -> mcp__<server>__<tool> (only merge-nur-sightline looks at the GitHub merge tools).
- Everything else (read_file, grep, agents.spawn, ...): no rule applies.

Output (stdout, exit code 0):
- pre_tool: blocks as {"decision": "deny", "reason"} - Vibe denies the call before the permission prompt (also with
  --auto-approve) and hands the reason to the model as the tool error. Warnings as {"system_message"} (UI only: pre_tool
  has no model context). Nothing when no rule matches.
- post_tool (after the tool ran): warnings of the same call as hook_specific_output.additional_context, which Vibe appends
  to the tool output the model sees. A block there only means the pre_tool entry is missing; it comes along as context.
Never "ask": Vibe knows only allow/deny. Errors answer with a system_message and let the call through (fail open); the hook
is never strict, so a script that does not start is Vibe's own fail-open path with a UI warning.

Install as a user hook in ~/.vibe/hooks.toml (pre_tool and post_tool, see README > Andere Clients).
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

SHELL_TOOLS = {"bash", "git_bash", "powershell"}


def text(value):
    return value if isinstance(value, str) else ""


def is_shell(tool):
    return tool.rsplit(".", 1)[-1] in SHELL_TOOLS or tool in ("process.start", "process_start")


def normalize(payload):
    tool, data = text(payload.get("tool_name")), payload.get("tool_input")
    data = data if isinstance(data, dict) else {}
    cwd = text(payload.get("cwd")) or os.getcwd()
    base = {"hook_event_name": "PreToolUse", "cwd": cwd}
    if is_shell(tool):
        if not isinstance(data.get("command"), str):
            return []
        shell_cwd = text(data.get("cwd"))
        if shell_cwd:
            base["cwd"] = os.path.join(cwd, os.path.expanduser(shell_cwd))
        return [dict(base, tool_name="Bash", tool_input={"command": data["command"]})]
    target = data.get("file_path")
    if isinstance(target, str) and target:
        path = os.path.join(cwd, os.path.expanduser(target))
        if isinstance(data.get("content"), str):
            return [dict(base, tool_name="Write", tool_input={"file_path": path, "content": data["content"]})]
        if "old_string" in data or "new_string" in data:
            return [dict(base, tool_name="Edit", tool_input={"file_path": path, "old_string": text(data.get("old_string")),
                                                             "new_string": text(data.get("new_string"))})]
        return []
    if tool.startswith("mcp_") and "." in tool:
        server, name = tool[len("mcp_"):].split(".", 1)
        return [dict(base, tool_name=f"mcp__{server}__{name}", tool_input=data)]
    return []


def render(blocks, warnings, event="pre_tool"):
    if event == "post_tool":  # a block here means the pre_tool hook was missing: tell the model afterwards
        if blocks or warnings:
            return {"hook_specific_output": {"additional_context": "\n\n".join(blocks + warnings)}}
        return None
    if blocks:
        return {"decision": "deny", "reason": "\n\n".join(blocks)}
    if warnings:
        return {"system_message": "\n\n".join(warnings)}
    return None


def on_error(message):
    return {"system_message": message}


def main(stdin=None, stdout=None):
    if client_adapter is None:
        message = (f"ce-shared guard error (vibe): client_adapter.py lädt nicht "
                   f"({LOAD_ERROR.__class__.__name__}: {LOAD_ERROR})")
        print(message, file=sys.stderr)
        (stdout or sys.stdout).write(json.dumps(on_error(message), ensure_ascii=False) + "\n")
        return 0
    seen = {"event": "pre_tool"}

    def read(payload):  # the event decides the answer's shape; render only gets the verdict
        seen["event"] = payload.get("hook_event_name") or "pre_tool"
        return normalize(payload)

    return client_adapter.run("vibe", read, lambda blocks, warnings: render(blocks, warnings, seen["event"]), on_error,
                              stdin, stdout)


if __name__ == "__main__":
    sys.exit(main())
