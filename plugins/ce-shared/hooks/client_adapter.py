"""Shared runner of the Codex and Antigravity adapters around guard.decide.

An adapter turns the client's PreToolUse input into zero or more Claude-shaped payloads (tool_name Bash/Edit/Write/
MultiEdit, tool_input with Claude's field names), lets guard.decide judge each one with this plugin's rules and renders
the combined verdict in the client's own output format. The decision logic stays in guard.py alone.

Claude subagent profiles (.claude/agent-guard.json) are not applied here: the payloads never carry agent_type, because
those profiles describe Claude Code subagents, not Codex or Antigravity agents of the same name.

Any internal error lets the call through with a message (fail open). With CE_HOOK_LOG=<file> in the client's
environment every input and output is appended as a JSON line - the measurement of a real client run.
"""
import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import guard  # noqa: E402
except Exception as error:  # a guard.py that does not load must still get an answer out (fail open)
    guard, GUARD_ERROR = None, error

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"


def verdict(payloads, rules=None):
    """(blocks, warnings) of all payloads, each message once - a patch touching two workflows warns once."""
    if guard is None:
        raise RuntimeError(f"guard.py lädt nicht ({GUARD_ERROR.__class__.__name__}: {GUARD_ERROR})")
    rules = guard.load_rules(RULES_DIR) if rules is None else rules
    blocks, warnings = [], []
    for payload in payloads:
        result = guard.decide(payload, rules)
        denial = (result.get("hookSpecificOutput") or {}).get("permissionDecisionReason")
        target, message = (blocks, denial) if denial else (warnings, result.get("systemMessage"))
        if message and message not in target:
            target.append(message)
    return blocks, warnings


def log(client, raw, output):
    path = os.environ.get("CE_HOOK_LOG")
    if not path:
        return
    try:
        entry = {"client": client, "time": datetime.datetime.now().isoformat(timespec="seconds"),
                 "cwd": os.getcwd(), "input": raw, "output": output}
        with open(os.path.expanduser(path), "a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as error:  # the log is a probe aid, never a reason to fail
        print(f"ce-shared: CE_HOOK_LOG nicht schreibbar: {error}", file=sys.stderr)


def run(client, normalize, render, on_error, stdin=None, stdout=None):
    """Read the client's JSON, print its answer (None = print nothing). Always exit code 0."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    raw = None
    try:
        raw = stdin.read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Eingabe ist kein JSON-Objekt")
        output = render(*verdict(normalize(payload)))
    except Exception as error:  # never wedge a session
        output = on_error(f"ce-shared guard error ({client}): {error}")
        print(f"ce-shared guard error ({client}): {error}", file=sys.stderr)
    if output is not None:
        stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
    log(client, raw, output)
    return 0
