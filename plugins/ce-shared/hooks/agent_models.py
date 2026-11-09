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

# 0.46 judge round 5: the narrow YAML plain_yaml() reads, with the whole value of every `name` key in its line; any
# other form (escapes, explicit keys, aliases, block scalars, other tags, multi-line scalars, a value on the next line,
# YAML errors) may hide every name.
ANCHOR = r"&[^\s,\[\]{}]+"
NODE_PROPS = r"(?:(?:" + ANCHOR + r"|!!str|!<tag:yaml\.org,2002:str>)[ \t]+)*"  # anchor and str tags before a node
QUOTED = r"\"[^\"\\\n]*\"|'[^'\n]*'(?!')"  # closed in its line, without an escape (`\` or `''`)
FIRST = r"(?:[^\s\-?:,\[\]{}#&*!|>'\"%@`]|[-?:](?=[^\s,\[\]{}]))"  # a plain scalar starts with no indicator
END = r"[ \t]*(?:(?<=[ \t])#[^\n]*)?(?:\n|\Z)"  # the rest of a line: spaces and a comment after a space
LINE_END = re.compile(END)
TOP_PROPS = re.compile(r"(?:(?:" + ANCHOR + r"|!!map|!<tag:yaml\.org,2002:map>)(?:[ \t]+|(?=\n|\Z)))+" + END)
ITEM = re.compile(r"-[ \t]+(?=[^\s#])")
BLOCK_KEY = re.compile(r"(" + NODE_PROPS + r")([\w.-]+|" + QUOTED + r")[ \t]*:(?=[ \t]|\n|\Z)")
FLOW_KEY = re.compile(r"(" + NODE_PROPS + r")([\w.-]+|" + QUOTED + r")[ \t]*:(?=[ \t,\]}]|\n|\Z)")
BLOCK_SCALAR = re.compile(NODE_PROPS + r"(" + QUOTED + "|" + FIRST + r"(?:[^:#\n]|:(?=\S)|(?<=\S)#)*)")
FLOW_SCALAR = re.compile(NODE_PROPS + r"(" + QUOTED + "|" + FIRST + r"(?:[^:#,\[\]{}\n]|(?<=\S)#)*)")
FLOW_PROPS = re.compile(NODE_PROPS + r"(?=[{\[])")
SPACES = re.compile(r"[ \t]*(?:(?<=[ \t])#[^\n]*)?")  # spaces and a comment, within the line
GAP = re.compile(r"(?:[ \t\n]|(?<=\s)#[^\n]*)*")  # spaces, line breaks and comments
LINE_BREAKS = re.compile("[\r\x85\u2028\u2029\ufeff\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # YAML 1.1 breaks, controls


class Unparsed:
    """frontmatter() of a readable file whose frontmatter is not read here (judge 4: never "inherit"). 0.46 Nameplate:
    it stands for its file stem and for every name a `name` key in it shows (judge 5 of 0.45: `!!map` or `&definition`
    on top of `name: worker` in profile.md blocked "worker"). `any` - it may be every definition, like an unreadable
    file - unless it is plain_yaml() and shows a `name` key (judge round 5: a positive list). Its names are then those
    of real `name` keys (judge round 6: not `# {name: general-purpose}` in a comment or a string)."""

    def __init__(self, text):
        names = plain_yaml(text)
        self.any = not names  # not plain or no `name` key
        self.names = names or set().union(*(name_values(text, match) for match in NAME_KEY.finditer(text))) - {""}

    def __repr__(self):
        return f"Unparsed(names={sorted(self.names)}, any={self.any})"


def name_values(text, match):
    """The names the scalar after a NAME_KEY match may show (anchors and str tags skipped, quotes taken off; a plain
    one both as cut at a flow indicator and as a whole): a best guess where plain_yaml() does not read the file."""
    end = text.find("\n", match.end())
    rest = re.sub(r"\A[ \t]*" + STR_PROPERTIES, "", text[match.end():len(text) if end < 0 else end]).strip()
    if rest[:1] in ("'", '"'):
        close = rest.find(rest[0], 1)
        return {rest[1:close if close > 0 else None].strip()}
    whole = re.split(r"[ \t]#", rest, maxsplit=1)[0].strip()  # block context: `,` `}` `]` belong to the scalar
    return {whole, re.split(r"[,}\]]", whole, maxsplit=1)[0].strip()}  # flow context: they end it


def deeper_next_line(text, end, column):
    """Whether the next line after `end` with content (comments end a plain scalar) is indented beyond `column`."""
    for line in text[end + 1:].splitlines():
        if line.strip():
            return not line.lstrip().startswith("#") and len(line) - len(line.lstrip(" \t")) > column
    return False


class NotPlain(Exception):
    """Raised inside plain_yaml() for a form outside its grammar."""


def need(found):
    if not found:
        raise NotPlain
    return found


def unquoted(scalar):
    return scalar[1:-1] if scalar[:1] in ("'", '"') else scalar.strip()


def name_of(key, names):
    """The collector for the value of `key`: `names` for a `name` key, else None."""
    return names if unquoted(key) == "name" else None


def scalar_value(found, names):
    """The end of a scalar of BLOCK_SCALAR or FLOW_SCALAR; the value of a `name` (not empty) goes to `names`."""
    need(found)
    if names is not None:
        value = unquoted(found.group(1))
        names.update(need({value, value.strip()} - {""}))
    return found.end()


def flow(text, at, names):
    """The end of the flow collection opening at text[at]: mappings of FLOW_KEY pairs, sequences of nodes, nested; a
    line break only after an opening bracket or a comma and before a comma or a closing bracket (judge rounds 3/4)."""
    close = "}" if text[at] == "{" else "]"
    at = GAP.match(text, at + 1).end()
    while text[at:at + 1] != close:
        name = None
        if close == "}":
            key = need(FLOW_KEY.match(text, at))
            name, at = name_of(key.group(2), names), SPACES.match(text, key.end()).end()
        if text[at:at + 1] in (",", "}", "\n", ""):
            if name is not None or close == "]":
                raise NotPlain  # a null `name` or an empty entry
        elif props := FLOW_PROPS.match(text, at):
            if name is not None:
                raise NotPlain  # a `name` that is no string
            at = flow(text, props.end(), names)
        else:
            at = scalar_value(FLOW_SCALAR.match(text, at), name)
        at = SPACES.match(text, at).end()
        if text[at:at + 1] == "\n":
            at = GAP.match(text, at).end()
        if text[at:at + 1] == ",":
            at = GAP.match(text, at + 1).end()
        elif text[at:at + 1] != close:
            raise NotPlain
    return at + 1


def block_value(text, at, name, names):
    """(end of the line, whether the value is empty: a nested block may follow) for the value of a block entry; `name`
    collects the value of a `name` key."""
    if empty := LINE_END.match(text, at):
        if name is not None:
            raise NotPlain  # a null `name` or a node in the next lines
        return empty.end(), True
    at = re.compile(r"[ \t]*").match(text, at).end()
    if props := FLOW_PROPS.match(text, at):
        if name is not None:
            raise NotPlain
        at = flow(text, props.end(), names)
    else:
        at = scalar_value(BLOCK_SCALAR.match(text, at), name)
    return need(LINE_END.match(text, at)).end(), False


def plain_yaml(text):
    """0.46 Nameplate (judge round 5): the values of all `name` keys (at any depth) of a frontmatter in a narrow YAML,
    None for any other form:
    blank and comment lines, one line of `!!map` and anchors on top, block mappings and sequences (`- key: value`,
    `- value`) of `key: value` entries with a plain or quoted key (no escape, anchor and str tag allowed) and a value
    that is a plain scalar ending in its line, a quoted one closed in its line without escape, a flow collection or
    nothing (a nested block follows); flow collections of such pairs and nodes, also over lines, also as the whole
    frontmatter. Indentation as YAML has it: a deeper line only below an entry without value."""
    text = text.replace("\r\n", "\n")
    if LINE_BREAKS.search(text):
        return None
    names, stack, opener, top, at = set(), [], None, True, 0  # open block collections (column, "map"/"seq"), column of a `key:`
    try:
        while at < len(text):
            start, line_end = at, text.find("\n", at) % (len(text) + 1)  # no line break: the end of the text
            stripped = text[at:line_end].strip(" \t")
            if not stripped or stripped[0] == "#":
                at = line_end + 1
                continue
            column = len(text[at:line_end]) - len(text[at:line_end].lstrip(" "))
            at += column
            if text[at] == "\t" or top is None:
                raise NotPlain  # a tab in the indentation, content after a flow frontmatter
            if not stack and top and (props := TOP_PROPS.match(text, at)):
                at, top = props.end(), False  # `!!map` / `&definition` on top, only once
                continue
            if not stack and text[at] in "{[":
                at, top = need(LINE_END.match(text, flow(text, at, names))).end(), None  # the whole frontmatter
                continue
            top = False
            item = ITEM.match(text, at)
            key = BLOCK_KEY.match(text, item.end() if item else at)
            kind = "seq" if item else "map"
            if not stack:
                stack.append((column, kind))
            elif opener is not None and (column > opener or column == opener and kind == "seq"
                                         and stack[-1] == (column, "map")):
                stack.append((column, kind))  # a nested block below `key:`, a sequence also at its column
            else:
                while stack[-1][0] > column:
                    stack.pop()
                if kind == "map" and stack[-2:] == [(column, "map"), (column, "seq")]:
                    stack.pop()  # the next key after a sequence at the column of its key
                if stack[-1] != (column, kind):
                    raise NotPlain  # a scalar that goes on, another indentation, a key among items
            opener = None
            if key:
                if item:
                    stack.append((key.start() - start, "map"))  # `- key: value`: a mapping at the key's column
                at, empty = block_value(text, key.end(), name_of(key.group(2), names), names)
                opener = key.start() - start if empty else None
            elif item:
                at = block_value(text, item.end(), None, names)[0]  # `- value`
            else:
                raise NotPlain
    except (NotPlain, IndexError):
        return None
    return names


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
