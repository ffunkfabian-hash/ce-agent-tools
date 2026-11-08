#!/usr/bin/env python3
"""Which model a Claude Code `Agent` call runs on (0.45 Rollcall), shared by guard.py (PreToolUse block) and
ce_report.py (Stop count), so the hook and the report never disagree.

Claude Code takes the call's `model` first, then the `model` frontmatter of the agent definition, then the main model
("inherit"). `subagent_type` "fork" always runs on the main model by design: forking means inheriting, and a `model`
there is ignored. The Nexogen audit of 08.11.2026 (`docs/pruefung-2026-11.md`) counted 18 of 200 starts on the main
model without anybody naming one (global rule: model and effort at every agent start).

`call_model(tool_input, cwd)` answers the call's model, "fork", the model of the definition, or "inherit". Definitions
are read where Claude Code reads them: `<plugin>:<name>` from this plugin's `agents/` (other plugins: unknown, so
"inherit"), a bare name from `.claude/agents/` of the folder and its parents, then `~/.claude/agents/`; the first
definition whose `name` (else file stem) matches decides, and `model: inherit` counts as none. Nothing here raises.
"""
import json
import re
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
AGENT_TOOLS = ("Agent", "Task")  # Task: the tool's name before Claude Code renamed it to Agent
FORK = "fork"
INHERIT = "inherit"
FRONTMATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
FIELD = re.compile(r"^(name|model)[ \t]*:[ \t]*(.*?)[ \t]*$", re.MULTILINE)


def plugin_name(plugin_dir=PLUGIN_DIR):
    try:
        return json.loads((Path(plugin_dir) / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"]
    except Exception:  # noqa: BLE001
        return "ce-shared"


def frontmatter(path):
    """{"name": …, "model": …} of an agent definition (values without quotes or comments), {} without frontmatter."""
    try:
        match = FRONTMATTER.match(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not match:
        return {}
    fields = {}
    for key, value in FIELD.findall(match.group(1)):
        value = re.sub(r"\s+#.*$", "", value).strip().strip("'\"").strip()
        fields.setdefault(key, value)
    return fields


def agent_dirs(subagent_type, cwd=None, home=None, plugin_dir=PLUGIN_DIR):
    """(name, folders to search in order) for a subagent_type."""
    plugin, colon, name = str(subagent_type).rpartition(":")
    if colon:
        return name, [Path(plugin_dir) / "agents"] if plugin == plugin_name(plugin_dir) else []
    folders = []
    if cwd:
        start = Path(cwd)
        folders = [folder / ".claude" / "agents" for folder in (start, *start.parents)]
    folders.append(Path(home or Path.home()) / ".claude" / "agents")
    return name, folders


def profile_model(subagent_type, cwd=None, home=None, plugin_dir=PLUGIN_DIR):
    """The model the definition of `subagent_type` names, "" when there is no definition or it names none/inherit."""
    if not subagent_type:
        return ""
    try:
        name, folders = agent_dirs(subagent_type, cwd, home, plugin_dir)
        for folder in folders:
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.md")):
                fields = frontmatter(path)
                if (fields.get("name") or path.stem) != name:
                    continue
                model = fields.get("model", "").lower()
                return "" if model in ("", INHERIT) else model
    except Exception:  # noqa: BLE001 - unknown means: no profile model
        return ""
    return ""


def call_model(data, cwd=None, home=None, plugin_dir=PLUGIN_DIR):
    """The model an Agent call with this tool_input runs on: its `model`, "fork", the definition's model or "inherit"."""
    data = data if isinstance(data, dict) else {}
    model = str(data.get("model") or "").strip()
    if model:
        return model
    kind = str(data.get("subagent_type") or "").strip()
    if kind == FORK:
        return FORK
    return profile_model(kind, cwd, home, plugin_dir) or INHERIT
