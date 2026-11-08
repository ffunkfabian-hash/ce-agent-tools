#!/usr/bin/env python3
"""Which model a Claude Code `Agent` call runs on (0.45 Rollcall), shared by guard.py (PreToolUse block) and
ce_report.py (Stop count), so the hook and the report never disagree.

Claude Code takes the call's `model` first, then the `model` frontmatter of the agent definition, then the main model
("inherit"). `subagent_type` "fork" always runs on the main model by design: forking means inheriting, and a `model`
there is ignored. The Nexogen audit of 08.11.2026 (`docs/pruefung-2026-11.md`) counted 18 of 200 starts on the main
model without anybody naming one (global rule: model and effort at every agent start).

`call_model(tool_input, cwd)` answers "fork", the call's model, the model of the definition, "inherit" (known to run on
the main model: no definition - the built-ins general-purpose, Explore … - or one without a model or `model: inherit`)
or "unknown" (judge round 1: the guard fails open, so what it cannot read passes). Definitions are read where Claude
Code reads them: `<plugin>:<name>` from this plugin's `agents/` (another plugin's folder is not known here: "unknown"),
a bare name from `.claude/agents/` of the folder and its parents, then `agents/` of `CLAUDE_CONFIG_DIR` (else
`~/.claude`); the first definition whose
`name` (else file stem) matches decides. "unknown" also for an unreadable definition file before the match (it may
be the one asked for), a `model` value this reader does not parse (YAML beyond a plain scalar) and any error.
Nothing here raises.
"""
import json
import os
import re
import textwrap
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
AGENT_TOOLS = ("Agent", "Task")  # Task: the tool's name before Claude Code renamed it to Agent
FORK = "fork"
INHERIT = "inherit"
UNKNOWN = "unknown"
UNPARSED = "unparsed"  # frontmatter() of a readable file whose frontmatter is not read here
FRONTMATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
PLAIN_KEY = re.compile(r"^(['\"]?)[\w.-]+\1[ \t]*:(?:[ \t]|$)")  # a top-level `key:` line of a block mapping
FIELD = re.compile(r"^(['\"]?)(name|model)\1[ \t]*:[ \t]*(.*?)[ \t]*$", re.MULTILINE)  # judge 2: also "model": x


def plugin_name(plugin_dir=PLUGIN_DIR):
    try:
        return json.loads((Path(plugin_dir) / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"]
    except Exception:  # noqa: BLE001
        return "ce-shared"


def frontmatter(path):
    """{"name": …, "model": …} of an agent definition (values without quotes or comments), {} without frontmatter,
    None when the file cannot be read, UNPARSED when its frontmatter is not a plain block mapping this reader
    understands (judge 4: a flow mapping `{name: x, model: y}` or another top-level form is "unknown", never "inherit";
    as its name is unknown too, it stands for the definition of its file stem only)."""
    try:
        match = FRONTMATTER.match(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not match:
        return {}
    lines = [line for line in match.group(1).splitlines() if line.strip() and not line.lstrip().startswith("#")]
    body = textwrap.dedent("\n".join(lines))  # judge 3/4: a wholly indented mapping, also after a comment line
    if any(line[:1] not in (" ", "\t") and not PLAIN_KEY.match(line) for line in body.splitlines()):
        return UNPARSED
    fields = {}
    for _, key, value in FIELD.findall(body):
        value = "" if value.startswith("#") else re.sub(r"\s+#.*$", "", value).strip()  # judge 3: `model: # x` is null
        if value[:1] in ("[", "{", "|", ">", "&", "*", "!") or (value[:1] in "'\"" and value[-1:] != value[:1]):
            value = None  # flow, block, anchor, tag, broken quote: not read here -> unknown (judge 3)
        else:
            value = value.strip("'\"").strip()
        fields.setdefault(key, value)
    return fields


def agent_dirs(subagent_type, cwd=None, home=None, plugin_dir=PLUGIN_DIR):
    """(name, folders to search in order) for a subagent_type; folders None for another plugin (not known here)."""
    plugin, colon, name = str(subagent_type).rpartition(":")
    if colon:
        return name, [Path(plugin_dir) / "agents"] if plugin == plugin_name(plugin_dir) else None
    folders = []
    if cwd:
        start = Path(cwd)
        folders = [folder / ".claude" / "agents" for folder in (start, *start.parents)]
    config = Path(home) / ".claude" if home else Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    folders.append(config / "agents")  # judge 3: CLAUDE_CONFIG_DIR as in session.py
    return name, folders


def profile_model(subagent_type, cwd=None, home=None, plugin_dir=PLUGIN_DIR):
    """The model the definition of `subagent_type` names; "" when it is known to name none (no definition, no model,
    `model: inherit`); None when that cannot be known (another plugin, an unreadable file without a match, an error)."""
    if not subagent_type:
        return ""
    try:
        name, folders = agent_dirs(subagent_type, cwd, home, plugin_dir)
        if folders is None:
            return None
        unreadable = False  # an unreadable file in a folder of higher priority: any of them may be the definition
        for folder in folders:
            if not folder.is_dir():
                continue
            before = unreadable
            here = False  # in the same folder only a file of that very name counts (judge 3)
            for path in sorted(folder.glob("*.md")):
                fields = frontmatter(path)
                if fields == UNPARSED:
                    if path.stem == name:
                        return None
                    continue
                if fields is None:
                    here = here or path.stem == name
                    unreadable = True
                    continue
                if (fields.get("name") or path.stem) != name:
                    continue
                if before or here or fields.get("model", "") is None:
                    return None  # judge 3: a definition before this one could not be read, or the value not parsed
                model = fields.get("model", "").lower()
                return "" if model in ("", INHERIT) else model
    except Exception:  # noqa: BLE001 - fail open
        return None
    return None if unreadable else ""  # no definition at all is a built-in (inherit), unless one could not be read


def call_model(data, cwd=None, home=None, plugin_dir=PLUGIN_DIR):
    """The model an Agent call with this tool_input runs on: "fork" (first: a fork ignores `model`), its `model`, the
    definition's model, "inherit" or "unknown"."""
    data = data if isinstance(data, dict) else {}
    kind = str(data.get("subagent_type") or "").strip()
    if kind == FORK:
        return FORK
    model = str(data.get("model") or "").strip()
    if model:
        return model
    found = profile_model(kind, cwd, home, plugin_dir)
    return UNKNOWN if found is None else found or INHERIT
