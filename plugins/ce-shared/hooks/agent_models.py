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
be the one asked for), a `model` value this reader does not parse (YAML beyond a plain scalar), a frontmatter it does
not parse whose file stem or `name` key shows the name asked for (0.46 Nameplate: `!!map` on top of `name: worker`)
and any error. Nothing here raises.
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
FRONTMATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
PLAIN_KEY = re.compile(r"^(['\"]?)[\w.-]+\1[ \t]*:(?:[ \t]|$)")  # a top-level `key:` line of a block mapping
FIELD = re.compile(r"^(['\"]?)(name|model)\1[ \t]*:[ \t]*(.*?)[ \t]*$", re.MULTILINE)  # judge 2: also "model": x
# 0.46: a `name` key anywhere in a frontmatter not read here - at a line start (indented, after `- ` or `? `) or in a
# flow mapping after `{` or `,`, also behind an anchor or a str tag; the value is read from the end of the match on
KEY_START = r"(?:^[ \t]*(?:[-?][ \t]+)*|[{\[,][ \t]*)"
STR_PROPERTIES = r"(?:(?:&\S+|!!str|!<tag:yaml\.org,2002:str>|!)(?:[ \t]+|$))*"  # anchors and tags that keep a string
NAME_KEY = re.compile(KEY_START + r"(?P<entry>" + STR_PROPERTIES + r"(['\"]?)name\2)[ \t]*:", re.MULTILINE)
# quoted scalars (opening after a space or an indicator, so `it's` is no quote) and comments: no flow brackets there
QUOTED_OR_COMMENT = re.compile(r"(?:^|(?<=[\s{\[,:]))(?:\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\n]|'')*')|(?:^|(?<=[ \t]))#.*$",
                               re.MULTILINE)
# a key this reader cannot read that may be `name` (judge rounds 1 and 2): escapes in a quoted key, an alias as key,
# an explicit key `? …` anywhere (also `{? name: x}` in a flow mapping), a tag other than str on a key
HIDDEN_KEY = re.compile(r"\"[^\"\n]*\\[^\"\n]*\"[ \t]*:|'[^'\n]*''[^'\n]*'[ \t]*:|(?:^|[\s{\[,])\*[^\s,\]}]+[ \t]*:"
                        r"|(?:^|[\s{\[,])\?(?:[ \t]|$)"
                        r"|(?:^|[\s{\[,])!(?!(?:!str|<tag:yaml\.org,2002:str>)[ \t])(?!\s)\S*[ \t]+[\w\"'.-][^:\n]*:", re.MULTILINE)


class Unparsed:
    """frontmatter() of a readable file whose frontmatter is not read here (judge 4: never "inherit"). 0.46 Nameplate:
    it stands for its file stem and for every name a `name` key in it shows (judge 5 of 0.45: `!!map` or `&definition`
    on top of `name: worker` in profile.md blocked "worker"). `any` - it may be every definition, like an unreadable
    file - when a `name` value is not read here (an alias, a block scalar, an escape, a value on the next line), when no
    `name` key shows or when a key may be a hidden `name`."""

    def __init__(self, text):
        values = [name_values(text, match) for match in NAME_KEY.finditer(text)]
        self.names = set().union(*(found for found in values if found))
        self.any = None in values or not values or bool(HIDDEN_KEY.search(text))

    def __repr__(self):
        return f"Unparsed(names={sorted(self.names)}, any={self.any})"


def name_values(text, match):
    """The names the scalar after a NAME_KEY match may be (anchors and str tags skipped; a plain one both as cut at a
    flow indicator and as a whole), None when this reader cannot be sure:
    another tag, an alias, a block or flow node, an escape, a plain scalar that goes on in the next line, a value on
    the next line."""
    end = text.find("\n", match.end())
    end = len(text) if end < 0 else end
    rest = re.sub(r"\A[ \t]*" + STR_PROPERTIES, "", text[match.end():end]).strip()
    if rest[:1] == '"':
        close = rest.find('"', 1)
        return {rest[1:close].strip()} - {""} or None if close > 0 and "\\" not in rest[1:close] else None
    if rest[:1] == "'":
        close = rest.find("'", 1)
        return {rest[1:close].strip()} - {""} or None if close > 0 and rest[close + 1:close + 2] != "'" else None
    whole = re.split(r"[ \t]#", rest, maxsplit=1)[0].strip()  # block context: `,` `}` `]` belong to the scalar
    head = re.split(r"[,}\]]", whole, maxsplit=1)[0].strip()  # flow context: they end it
    if not head or head[0] in "*|>[{#!&@`%":
        return None
    follow = next((line.strip() for line in text[end + 1:].splitlines() if line.strip()), "")
    if follow[:1] in ("", "}", "]", ",", "#"):
        return {head, whole}  # nothing, a flow end or a comment follows: the scalar ends in its line
    before = text[:match.start("entry")]
    if flow_depth(QUOTED_OR_COMMENT.sub("", before)):  # judge rounds 3/4: in a flow collection
        return {head, whole} if head != whole else None  # not ended in its line: it may go on at any indentation
    if deeper_next_line(text, end, match.start("entry") - (before.rfind("\n") + 1)):
        return None  # block context: it goes on in a line indented beyond its entry (tag and anchor included)
    return {head, whole}


def flow_depth(text):
    """Open `{`/`[` in text without quotes and comments; a closer without opener (plain text) never goes below 0."""
    depth = 0
    for char in re.findall(r"[{}\[\]]", text):
        depth = depth + 1 if char in "{[" else max(0, depth - 1)
    return depth


def deeper_next_line(text, end, column):
    """Whether the next line after `end` with content (comments end a plain scalar) is indented beyond `column`."""
    for line in text[end + 1:].splitlines():
        if line.strip():
            return not line.lstrip().startswith("#") and len(line) - len(line.lstrip(" \t")) > column
    return False


def plugin_name(plugin_dir=PLUGIN_DIR):
    try:
        return json.loads((Path(plugin_dir) / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"]
    except Exception:  # noqa: BLE001
        return "ce-shared"


def frontmatter(path):
    """{"name": …, "model": …} of an agent definition (values without quotes or comments), {} without frontmatter,
    None when the file cannot be read, an Unparsed when its frontmatter is not a plain block mapping this reader
    understands (judge 4: a flow mapping `{name: x, model: y}`, a tag `!!map` or anchor `&a` on top, another top-level
    form) or its `name` value is not a plain scalar (0.46: `name: &n worker`): "unknown" for the names it may carry."""
    try:
        match = FRONTMATTER.match(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not match:
        return {}
    lines = [line for line in match.group(1).splitlines() if line.strip() and not line.lstrip().startswith("#")]
    body = textwrap.dedent("\n".join(lines))  # judge 3/4: a wholly indented mapping, also after a comment line
    if any(line[:1] not in (" ", "\t") and not PLAIN_KEY.match(line) for line in body.splitlines()):
        return Unparsed(match.group(1))
    fields = {}
    for match in FIELD.finditer(body):
        key, value = match.group(2), match.group(3)
        value = "" if value.startswith("#") else re.sub(r"\s+#.*$", "", value).strip()  # judge 3: `model: # x` is null
        if (value[:1] in ("[", "{", "|", ">", "&", "*", "!") or (value[:1] in "'\"" and value[-1:] != value[:1])
                or (value[:1] == '"' and "\\" in value) or (value[:1] == "'" and "''" in value[1:-1])
                or deeper_next_line(body, match.end(), 0)):
            value = None  # flow, block, anchor, tag, broken quote, escape, a value in the next line (0.46): unknown
        else:
            value = value.strip("'\"").strip()
        fields[key] = value if fields.get(key, value) == value else None  # 0.46: a key twice with two values
    return Unparsed(body) if fields.get("name", "") is None else fields


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
                if isinstance(fields, Unparsed):
                    if path.stem == name or name in fields.names:
                        return None  # 0.46: it may be the definition asked for -> unknown, never a block
                    unreadable = unreadable or fields.any  # a name not read here: like an unreadable file
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
