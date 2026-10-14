#!/usr/bin/env python3
"""PreToolUse guard of the ce-shared plugin (Cube Escape, Cubegen, robot generator).

Two jobs, one hook:
1. Shared rules: every rules/*.md of this plugin, in hookify's format (event bash|file, conditions on command,
   file_path, content, new_text, old_text; operators regex_match (case-insensitive search), contains, not_contains,
   equals, starts_with, ends_with; action warn|block). hookify itself only reads .claude/ of the project, so rules that
   all three repositories share live here once instead of three times.
2. Subagent profiles: inside a subagent the hook input names it (agent_type). If the repository has a profile for it in
   .claude/agent-guard.json, Bash commands must match its allow list and Edit/Write paths its edit list - tools of a
   subagent stay as narrow as its job (test runner: run and read, never commit; doc keeper: only docs).

Output follows hookify: warnings as systemMessage, blocks as permissionDecision "deny". Any internal error lets the
call through with a message - a broken guard must never wedge a session.
"""
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
RULES_DIR = PLUGIN_ROOT / "rules"
PROFILE_FILE = Path(".claude") / "agent-guard.json"
FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


# --------------------------------------------------------------------------------------------------------- rules

def parse_rule(text):
    """hookify's subset of YAML front matter: scalars and a list of condition mappings."""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    meta, conditions, current = {}, [], None
    for raw in parts[1].splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.strip()
        if raw.startswith("  ") and stripped.startswith("- "):
            current = {}
            conditions.append(current)
            stripped = stripped[2:]
        if raw.startswith("  ") and current is not None and ":" in stripped:
            key, value = stripped.split(":", 1)
            current[key.strip()] = value.strip()
            continue
        if ":" in stripped:
            key, value = stripped.split(":", 1)
            meta[key.strip()] = value.strip()
            current = None
    meta["conditions"] = conditions
    meta["message"] = parts[2].strip()
    meta["enabled"] = meta.get("enabled", "true").lower() != "false"
    return meta


def load_rules(folder=RULES_DIR):
    rules = []
    for path in sorted(Path(folder).glob("*.md")):
        rule = parse_rule(path.read_text(encoding="utf-8"))
        if rule and rule["enabled"] and rule["conditions"]:
            rules.append(rule)
    return rules


def field_value(field, tool, data):
    if tool == "Bash":
        return data.get("command", "") if field == "command" else None
    if tool in FILE_TOOLS:
        if field == "file_path":
            return data.get("file_path") or data.get("notebook_path", "")
        if tool == "MultiEdit" and field in ("content", "new_text", "new_string"):
            return " ".join(edit.get("new_string", "") for edit in data.get("edits", []))
        if field == "content":
            return data.get("content") or data.get("new_string") or data.get("new_source", "")
        if field in ("new_text", "new_string"):
            return data.get("new_string", "")
        if field in ("old_text", "old_string"):
            return data.get("old_string", "")
    return None


def condition_holds(condition, tool, data):
    value = field_value(condition.get("field", ""), tool, data)
    if value is None:
        return False
    pattern, operator = condition.get("pattern", ""), condition.get("operator", "regex_match")
    if operator == "regex_match":
        try:
            return re.search(pattern, value, re.IGNORECASE) is not None
        except re.error:
            return False
    return {
        "contains": lambda: pattern in value,
        "not_contains": lambda: pattern not in value,
        "equals": lambda: pattern == value,
        "starts_with": lambda: value.startswith(pattern),
        "ends_with": lambda: value.endswith(pattern),
    }.get(operator, lambda: False)()


def matching_rules(rules, tool, data):
    event = "bash" if tool == "Bash" else "file" if tool in FILE_TOOLS else None
    if event is None:
        return []
    return [rule for rule in rules if rule.get("event", "all") in (event, "all")
            and all(condition_holds(c, tool, data) for c in rule["conditions"])]


# ------------------------------------------------------------------------------------------------ agent profiles

# Read-only shell commands every profile may use when it sets "readonly_shell": true.
READONLY = [
    r"(cat|head|tail|wc|ls|pwd|date|echo|printf|true|test|basename|dirname|realpath|stat|du|df|sort|uniq|cut|tr|diff|"
    r"jq|file|which|whoami|uname|sw_vers|nproc|vm_stat|ps|pgrep|uptime)\b",
    r"(grep|egrep|rg)\b",
    r"sed\s+-n\b",
    r"find\b(?!.*\s-(delete|exec|execdir|ok|okdir|fprint\w*|fls)\b)",
    r"cd\b",
    r"git(\s+-C\s+(\"[^\"]*\"|'[^']*'|\S+))?\s+(status|log|diff|show|describe|rev-parse|branch\s+--show-current|"
    r"worktree\s+list|ls-files|blame|shortlog|tag\s+(-l|--list|-n)|stash\s+list|remote\s+-v|config\s+--get)\b",
]
SAFE_REDIRECT = re.compile(r"(\d?>&\d|\d?>\s*/dev/null|&>\s*/dev/null)")
ENV_PREFIX = re.compile(r"^(\w+=(\"[^\"]*\"|'[^']*'|\S*)\s+)+")


def find_profile_file(cwd):
    for folder in (Path(cwd), *Path(cwd).parents):
        if (folder / PROFILE_FILE).is_file():
            return folder, folder / PROFILE_FILE
    return None, None


def split_commands(command):
    """Simple commands of a shell line, split at ; & | && || and newlines outside quotes. Also returns whether a
    redirection into a file (>) or a substitution ($(…), `…`, <(…), heredoc) appears outside single quotes."""
    parts, current, quote, risky = [], [], None, False
    text = SAFE_REDIRECT.sub(" ", command)
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            if char == quote:
                quote = None
            elif quote == '"' and (char == "`" or text.startswith("$(", i)):
                risky = True
            current.append(char)
        elif char in "'\"":
            quote = char
            current.append(char)
        elif char in ";&|\n":
            parts.append("".join(current))
            current = []
        elif char == ">" or char == "`" or text.startswith("$(", i) or text.startswith("<(", i) \
                or text.startswith("<<", i):
            risky = True
            current.append(char)
        else:
            current.append(char)
        i += 1
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()], risky


def relative_paths(command, root):
    """Absolute paths into the repository become relative ones, so profiles name scripts once ("./x" and "x" alike)."""
    for prefix in {str(root), str(Path(root).resolve())}:
        command = command.replace(prefix + "/", "")
    return re.sub(r"(^|[\s\"'])\./", r"\1", command)


def bash_problem(command, profile):
    """None if every part of the command is allowed, else the reason."""
    parts, risky = split_commands(command)
    if risky:
        return "Umleitungen in Dateien, Befehlsersetzung und Heredocs sind in diesem Subagent nicht erlaubt"
    allowed = list(profile.get("bash", []))
    if profile.get("readonly_shell"):
        allowed += READONLY
    for part in parts:
        part = ENV_PREFIX.sub("", part).strip()
        if not part:
            continue
        if not any(re.match(pattern, part) for pattern in allowed):
            return f"nicht freigegeben: `{part[:120]}`"
    return None


def edit_problem(path, root, profile):
    try:
        relative = Path(os.path.abspath(path)).resolve().relative_to(root).as_posix()  # symlinks: .claude/skills -> .agents/skills
    except ValueError:
        return "nur Dateien in diesem Repository"
    if any(fnmatch.fnmatchcase(relative, pattern) for pattern in profile.get("deny_edit", [])):
        return f"`{relative}` ist für diesen Subagent gesperrt"
    if not any(fnmatch.fnmatchcase(relative, pattern) for pattern in profile.get("edit", [])):
        return f"`{relative}` liegt außerhalb der freigegebenen Pfade"
    return None


def agent_problem(payload):
    agent = payload.get("agent_type")
    if not agent:
        return None
    root, profile_file = find_profile_file(payload.get("cwd") or os.getcwd())
    if profile_file is None:
        return None
    profile = json.loads(profile_file.read_text(encoding="utf-8")).get("agents", {}).get(agent)
    if profile is None:
        return None
    tool, data = payload.get("tool_name", ""), payload.get("tool_input") or {}
    if tool == "Bash":
        problem = bash_problem(relative_paths(data.get("command", ""), root), profile)
    elif tool in FILE_TOOLS:
        problem = edit_problem(data.get("file_path") or data.get("notebook_path", ""), root.resolve(), profile)
    else:
        return None
    if problem:
        return f"**[{agent}]** {problem}. Erlaubt ist nur, was `.claude/agent-guard.json` für diesen Subagent freigibt."
    return None


# ------------------------------------------------------------------------------------------------------- output

def decide(payload, rules=None):
    rules = load_rules() if rules is None else rules
    tool, data = payload.get("tool_name", ""), payload.get("tool_input") or {}
    hits = matching_rules(rules, tool, data)
    blocks = [f"**[{r['name']}]**\n{r['message']}" for r in hits if r.get("action") == "block"]
    warnings = [f"**[{r['name']}]**\n{r['message']}" for r in hits if r.get("action") != "block"]
    problem = agent_problem(payload)
    if problem:
        blocks.append(problem)
    if blocks:
        message = "\n\n".join(blocks)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": message}, "systemMessage": message}
    if warnings:
        return {"systemMessage": "\n\n".join(warnings)}
    return {}


def main():
    try:
        payload = json.load(sys.stdin)
        print(json.dumps(decide(payload)))
    except Exception as error:  # never wedge a session
        print(json.dumps({"systemMessage": f"ce-shared guard error: {error}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
