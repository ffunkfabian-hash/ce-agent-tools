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
import shlex
import subprocess
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


def field_value(field, tool, data, cwd=None):
    if tool == "Bash":
        if field == "git_commit":
            return git_commits(data.get("command", ""), cwd or os.getcwd())
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


def condition_holds(condition, tool, data, cwd=None):
    value = field_value(condition.get("field", ""), tool, data, cwd)
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


def matching_rules(rules, tool, data, cwd=None):
    event = "bash" if tool == "Bash" else "file" if tool in FILE_TOOLS else None
    if event is None:
        return []
    return [rule for rule in rules if rule.get("event", "all") in (event, "all")
            and all(condition_holds(c, tool, data, cwd) for c in rule["conditions"])]


# ---------------------------------------------------------------------------------------------- git commit field
#
# commit-on-main needs the branch a `git commit` really lands on. The line is tokenised and parsed like the shell does
# (quotes, comments, heredocs, substitutions also inside $((…)), lists, && and ||, pipelines, subshells, groups,
# if/while/for/case, functions) and then run on the set of states it can reach: a switch that may fail or sits on a
# path that may not run never rules main out. Folder and variables stay inside a subshell, branch switches are on
# disk and do not. Branches are read with the system's Git (fixed path, small fixed environment), never with a PATH
# or loader variable taken from the line. What cannot be known before the line runs (script files, a folder made on
# the way, input from a file) leaves the branch unknown; the Git net (githooks/) covers that.

GIT_VALUE_OPTIONS = ("-c", "--namespace", "--config-env")
GIT_CONTEXT_OPTIONS = ("-C", "--git-dir", "--work-tree")
SWITCH_NEW = ("-b", "-B", "-c", "-C", "--orphan", "--create", "--force-create")
SHELLS = ("sh", "bash", "zsh", "dash", "ksh")
WRAPPERS = {"env": (), "command": (), "builtin": (), "exec": ("-a",), "nohup": (), "time": (), "nice": ("-n",),
            "timeout": ("-s", "-k", "--signal", "--kill-after")}  # value: options taking a value
ENV_OPTIONS = {"-u": "unset", "--unset": "unset", "-C": "chdir", "--chdir": "chdir", "-S": "split",
               "--split-string": "split", "-P": "path"}
GIT_CANDIDATES = ("/usr/bin/git", "/opt/homebrew/bin/git", "/usr/local/bin/git")
GIT_REPOSITORY_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")
MAX_STATES, MAX_DEPTH = 256, 12
UNKNOWN = object()  # standard input only known when the line runs
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")
CURRENT, PREVIOUS = "<current>", "<previous>"  # branch at the start of the line / of @{-1}, resolved on demand


class Heredoc:
    def __init__(self, delimiter, strip_tabs, quoted):
        self.delimiter, self.strip_tabs, self.quoted, self.body, self.subs = delimiter, strip_tabs, quoted, "", []
        self.fd = None  # file descriptor it feeds; None = standard input


def shell_tokens(text, i=0, nested=False):
    """Tokens of a shell line: ("word", text without quotes), ("op", control operator, "(" or ")"), ("sub", tokens of
    a $(…), `…` or <(…), also inside $((…))), ("redirect", (fd, operator)) before a redirection target, ("herestring",
    fd) before its word and ("heredoc", Heredoc). Quotes, backslashes, comments and heredoc bodies are read the way the
    shell reads them. nested: stop at the ")" that closes a $( or <( and return its index."""
    tokens, word, started, depth, pending, n = [], [], False, 0, [], len(text)

    def end_word():
        nonlocal word, started
        if started:
            tokens.append(("word", "".join(word)))
        word, started = [], False

    while i < n:
        c, two = text[i], text[i:i + 2]
        if c == "\\":
            if two != "\\\n":
                word.append(text[i + 1:i + 2])
                started = True
            i += 2
        elif c == "'" or two == "$'":
            end = start = i + 1 + (c == "$")
            while end < n and text[end] != "'":
                end += 2 if c == "$" and text[end] == "\\" else 1
            word.append(text[start:end])
            started, i = True, end + 1
        elif c == '"':
            value, subs, i = double_quoted(text, i + 1, '"')
            word.append(value)
            tokens += [("sub", sub) for sub in subs]
            started = True
        elif text.startswith("$((", i):
            end, subs = arithmetic(text, i + 3)
            tokens += [("sub", sub) for sub in subs]
            word.append(text[i:end])
            started, i = True, end
        elif two in ("$(", "<(", ">("):
            if c != "$":
                end_word()
            sub, end = shell_tokens(text, i + 2, nested=True)
            tokens.append(("sub", sub))
            word.append(text[i:end + 1])
            started, i = True, end + 1
        elif c == "`":
            end = backtick_end(text, i + 1)
            tokens.append(("sub", shell_tokens(text[i + 1:end])[0]))
            word.append(text[i:end + 1])
            started, i = True, end + 1
        elif c == "#" and not started:
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif c in " \t":
            end_word()
            i += 1
        elif c == "\n":
            end_word()
            tokens.append(("op", "\n"))
            i += 1
            for doc in pending:
                i = read_heredoc(text, i, doc)
            pending = []
        elif c in "()":
            end_word()
            if c == ")" and nested and depth == 0:
                return tokens, i
            depth += 1 if c == "(" else -1
            tokens.append(("op", c))
            i += 1
        elif c in "<>" or two == "&>":
            fd = None
            if started and c != "&" and "".join(word).isdigit():
                fd, word, started = int("".join(word)), [], False  # a file descriptor: 2>&1, 3<<EOF
            end_word()
            if text.startswith("<<<", i):
                tokens.append(("herestring", fd))
                i += 3
            elif two == "<<":
                doc, i = heredoc_operator(text, i + 2)
                doc.fd = fd
                pending.append(doc)
                tokens.append(("heredoc", doc))
            else:
                start, i = i, i + 1
                while i < n and text[i] in "<>&|":
                    i += 1
                tokens.append(("redirect", (fd, text[start:i])))
        elif c in ";&|":
            end_word()
            op = two if two in ("&&", "||", "|&", ";;", ";&") else c
            tokens.append(("op", op))
            i += len(op)
        else:
            word.append(c)
            started, i = True, i + 1
    end_word()
    return tokens, n


def double_quoted(text, i, closing):
    """Value and substitutions of a double-quoted text from i up to `closing` (None: to the end, a heredoc body)."""
    value, subs, n = [], [], len(text)
    while i < n:
        c = text[i]
        if closing and c == closing:
            return "".join(value), subs, i + 1
        if c == "\\" and i + 1 < n and (text[i + 1] in "$`\\\n" or text[i + 1] == closing):
            value.append("" if text[i + 1] == "\n" else text[i + 1])
            i += 2
        elif text.startswith("$((", i):
            end, inner = arithmetic(text, i + 3)
            subs += inner
            value.append(text[i:end])
            i = end
        elif text.startswith("$(", i):
            sub, end = shell_tokens(text, i + 2, nested=True)
            subs.append(sub)
            value.append(text[i:end + 1])
            i = end + 1
        elif c == "`":
            end = backtick_end(text, i + 1)
            subs.append(shell_tokens(text[i + 1:end])[0])
            value.append(text[i:end + 1])
            i = end + 1
        else:
            value.append(c)
            i += 1
    return "".join(value), subs, n


def arithmetic(text, i):
    """End index and command substitutions of the arithmetic expansion whose "$((" ends just before i."""
    depth, subs = 2, []
    while i < len(text) and depth:
        if text.startswith("$((", i):
            depth, i = depth + 2, i + 3
        elif text.startswith("$(", i):
            sub, end = shell_tokens(text, i + 2, nested=True)
            subs.append(sub)
            i = end + 1
        elif text[i] == "`":
            end = backtick_end(text, i + 1)
            subs.append(shell_tokens(text[i + 1:end])[0])
            i = end + 1
        else:
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            i += 1
    return i, subs


def backtick_end(text, i):
    while i < len(text) and text[i] != "`":
        i += 2 if text[i] == "\\" else 1
    return min(i, len(text))


def heredoc_operator(text, i):
    """The heredoc after `<<` at i: delimiter (quotes removed; quoted means a literal body) and the index after it."""
    strip = text.startswith("-", i)
    i += strip
    while i < len(text) and text[i] in " \t":
        i += 1
    delimiter, quoted = [], False
    while i < len(text) and text[i] not in " \t\n;&|()<>":
        c = text[i]
        if c in "'\"":
            end = text.find(c, i + 1)
            end = len(text) if end < 0 else end
            delimiter.append(text[i + 1:end])
            quoted, i = True, end + 1
        elif c == "\\":
            delimiter.append(text[i + 1:i + 2])
            quoted, i = True, i + 2
        else:
            delimiter.append(c)
            i += 1
    return Heredoc("".join(delimiter), strip, quoted), i


def read_heredoc(text, i, doc):
    """Reads the body of `doc` from line start i up to its delimiter line; returns the index after that line."""
    lines = []
    while i < len(text):
        end = text.find("\n", i)
        end = len(text) if end < 0 else end
        line, i = text[i:end], end + 1
        if (line.lstrip("\t") if doc.strip_tabs else line) == doc.delimiter:
            break
        lines.append(line)
    doc.body = "\n".join(lines)
    if not doc.quoted:  # an unquoted body expands $(…) and `…` when the line runs
        doc.subs = double_quoted(doc.body, 0, None)[1]
    return min(i, len(text))


def unwrap(words, env):
    """The program behind env, command, exec, nohup, nice, time and timeout, and what env changes for it:
    (words, folder of env -C or None, clear for env -i). Assignments and env -u go to `env` (None = unset)."""
    folder, clear = None, False
    while words and os.path.basename(words[0]) in WRAPPERS:
        head, rest = os.path.basename(words[0]), words[1:]
        if head == "command" and rest[:1] in (["-v"], ["-V"]):
            return [], None, False  # only names what would run
        while rest and (rest[0].startswith("-") or (head == "env" and ASSIGNMENT.match(rest[0]))):
            arg, rest = rest[0], rest[1:]
            if head != "env":
                rest = rest[1:] if arg in WRAPPERS[head] and rest else rest
            elif ASSIGNMENT.match(arg):
                name, value = arg.split("=", 1)
                env[name] = value
            elif arg in ("-i", "-", "--ignore-environment"):
                clear = True
                env.clear()
            else:
                option, equals, value = arg.partition("=") if arg.startswith("--") else (arg[:2], "", arg[2:])
                action = ENV_OPTIONS.get(option)
                if action and not equals and not value and rest:
                    value, rest = rest[0], rest[1:]
                if action == "unset":
                    env[value] = None
                elif action == "chdir":
                    folder = value
                elif action == "split":
                    rest = shlex.split(value) + rest  # env -S 'git commit': the program is in the string
        if head == "timeout" and rest:
            rest = rest[1:]  # the duration
        words = rest
    return words, folder, clear


def shell_script(args, stdin):
    """The script `sh|bash|zsh <args>` runs: the -c text; else its standard input (no file operand, `-` or -s) when
    that is known text; else None (a script file, or input only known when the line runs)."""
    command_mode, stdin_mode, i = False, False, 0
    while i < len(args) and args[i].startswith(("-", "+")) and args[i] not in ("-", "--"):
        if args[i] in ("-o", "+o", "-O", "+O"):
            i += 1
        elif not args[i].startswith("--"):
            command_mode |= "c" in args[i][1:]
            stdin_mode |= "s" in args[i][1:]
        i += 1
    if command_mode:
        i += i < len(args) and args[i] == "--"
        return args[i] if i < len(args) else None
    i += i < len(args) and args[i] == "--"
    if stdin_mode or i >= len(args) or args[i] == "-":
        return stdin if isinstance(stdin, str) else None
    return None


def switch_target(command, args, kind):
    """What `git checkout|switch <args>` does to HEAD: (branch, how) - branch a name ("-" = the previous one) or None
    (detached), how "create" (fails when the branch exists), "sure" or "maybe" (may fail: not a known branch yet) - or
    (..., "files") when HEAD stays. kind(name, path_possible) -> "branch" | "new" | "commit" | "path", like checkout."""
    names, paths, dashdash, detach, track, i = [], [], False, False, False, 0
    while i < len(args):
        arg = args[i]
        option, _, value = arg.partition("=")
        if dashdash:
            paths.append(arg)
        elif arg == "--":
            dashdash = True
        elif option in SWITCH_NEW:
            name = value if "=" in arg else args[i + 1] if i + 1 < len(args) else None
            return name, "sure" if option in ("-B", "-C", "--force-create") else "create"
        elif arg[:2] in SWITCH_NEW and not arg.startswith("--"):  # -bname
            return arg[2:], "sure" if arg[:2] in ("-B", "-C") else "create"
        elif arg in ("-d", "--detach"):
            detach = True
        elif arg in ("-t", "--track") or arg.startswith("--track="):
            track = True
        elif arg in ("-p", "--patch") or arg.startswith("--pathspec-from-file"):
            return ..., "files"
        elif arg == "--conflict":
            i += 1
        elif arg == "-" or not arg.startswith("-"):
            names.append(arg)
        i += 1
    if command == "switch":  # no paths: `switch main --` and `switch -- main` both switch
        names, paths = names + paths, []
    if detach:
        return (None, "sure") if not paths else (..., "files")
    if paths or len(names) != 1:
        return ..., "files"  # checkout <tree-ish> <paths>: files only; or nothing named
    name = names[0]
    if track:
        return name.split("/", 1)[-1], "create"  # -t origin/x makes and checks out x
    if name in ("HEAD", "@"):
        return ..., "files"
    if name == "-" or name.startswith("@{-"):
        return "-", "maybe"
    found = kind(name, command == "checkout" and not dashdash)
    if found == "path":
        return ..., "files"
    if found == "commit":
        return (None, "sure") if command == "checkout" else (..., "files")  # switch wants --detach: an error
    return name, "sure" if found == "branch" else "maybe"


# The shell grammar the reader follows. Nodes: ("list", [(and_or, terminator)]), ("andor", [(None|"&&"|"||",
# pipeline)]), ("pipeline", negate, [command]), ("simple", tokens), ("subshell", list), ("group", list), ("if", cond,
# then, else), ("loop", cond, body), ("for", subs, body), ("case", subs, [list]), ("function", name, command).

SEPARATORS = (";", "\n", "&")


class Parser:
    """Recursive descent over shell_tokens. Lenient: a token it cannot place is skipped, so a broken line still yields
    what it contains."""

    def __init__(self, tokens):
        self.tokens, self.i = tokens, 0

    def peek(self, ahead=0):
        j = self.i + ahead
        return self.tokens[j] if j < len(self.tokens) else ("eof", None)

    def word(self, *values):
        return self.peek()[0] == "word" and self.peek()[1] in values

    def op(self, *values):
        return self.peek()[0] == "op" and self.peek()[1] in values

    def skip(self, kind, value):
        if self.peek() == (kind, value):
            self.i += 1

    def newlines(self):
        while self.op("\n"):
            self.i += 1

    def until(self, stop):
        """Skips to the word `stop`, returning the substitutions on the way (for/case headers)."""
        subs = []
        while self.peek()[0] != "eof" and not self.word(stop):
            kind, value = self.peek()
            subs += [value] if kind == "sub" else []
            self.i += 1
        self.skip("word", stop)
        return subs

    def parse_list(self, stop_words=(), stop_ops=()):
        items = []
        while True:
            while self.op("\n", ";"):
                self.i += 1
            if self.peek()[0] == "eof" or self.word(*stop_words) or self.op(*stop_ops):
                return ("list", items)
            start = self.i
            node = self.and_or()
            terminator = self.peek()[1] if self.op(*SEPARATORS) else None
            self.i += terminator is not None
            if self.i == start:
                self.i += 1  # a token that fits nowhere: a stray ")" or "fi"
                continue
            items.append((node, terminator))

    def and_or(self):
        parts = [(None, self.pipeline())]
        while self.op("&&", "||"):
            operator = self.peek()[1]
            self.i += 1
            self.newlines()
            parts.append((operator, self.pipeline()))
        return ("andor", parts)

    def pipeline(self):
        negate = False
        while self.word("!", "time"):
            negate ^= self.peek()[1] == "!"
            self.i += 1
        commands = [self.command()]
        while self.op("|", "|&"):
            self.i += 1
            self.newlines()
            commands.append(self.command())
        return ("pipeline", negate, commands)

    def command(self):
        if self.op("("):
            self.i += 1
            node = ("subshell", self.parse_list(stop_ops=(")",)))
            self.skip("op", ")")
        elif self.word("{"):
            self.i += 1
            node = ("group", self.parse_list(stop_words=("}",)))
            self.skip("word", "}")
        elif self.word("if"):
            node = self.if_clause()
        elif self.word("while", "until"):
            self.i += 1
            condition = self.parse_list(stop_words=("do",))
            self.skip("word", "do")
            node = ("loop", condition, self.parse_list(stop_words=("done",)))
            self.skip("word", "done")
        elif self.word("for", "select"):
            self.i += 1
            subs = self.until("do")
            node = ("for", subs, self.parse_list(stop_words=("done",)))
            self.skip("word", "done")
        elif self.word("case"):
            self.i += 1
            node = ("case", self.until("in"), self.case_clauses())
        elif self.word("function"):
            self.i += 1
            name = self.peek()[1]
            self.i += 1
            if self.op("(") and self.peek(1) == ("op", ")"):
                self.i += 2
            self.newlines()
            return ("function", name, self.command())
        else:
            tokens = []
            while self.peek()[0] in ("word", "sub", "redirect", "heredoc", "herestring"):
                tokens.append(self.peek())
                self.i += 1
            if len(tokens) == 1 and tokens[0][0] == "word" and self.op("(") and self.peek(1) == ("op", ")"):
                self.i += 2
                self.newlines()
                return ("function", tokens[0][1], self.command())
            return ("simple", tokens)
        while self.peek()[0] in ("redirect", "heredoc", "herestring"):  # `{ …; } >log`, `done <<EOF`
            self.i += 1 + (self.peek()[0] != "heredoc" and self.peek(1)[0] == "word")
        return node

    def if_clause(self):
        self.i += 1  # if / elif
        condition = self.parse_list(stop_words=("then",))
        self.skip("word", "then")
        then = self.parse_list(stop_words=("elif", "else", "fi"))
        if self.word("elif"):
            return ("if", condition, then, self.if_clause())  # the elif chain shares one fi
        other = None
        if self.word("else"):
            self.i += 1
            other = self.parse_list(stop_words=("fi",))
        self.skip("word", "fi")
        return ("if", condition, then, other)

    def case_clauses(self):
        clauses = []
        while True:
            while self.op("\n", ";"):
                self.i += 1
            if self.peek()[0] == "eof" or self.word("esac"):
                self.skip("word", "esac")
                return clauses
            start = self.i
            while self.peek()[0] != "eof" and not self.op(")"):  # the patterns
                self.i += 1
            self.skip("op", ")")
            clauses.append(self.parse_list(stop_words=("esac",), stop_ops=(";;", ";&")))
            if self.op(";;", ";&"):
                self.i += 1
            if self.i == start:
                self.i += 1


def parse(tokens):
    return Parser(tokens).parse_list()


class ShellState:
    """One possible state of the shell during the line. Process state - folder, exported variables (None = unset),
    shell variables not exported yet (local), functions - stays inside a subshell; repository state - per repository
    the branch an earlier checkout/switch left (heads), the one before it (before) and branches made on the way - is
    on disk and outlives the subshell that changed it."""

    def __init__(self, folder):
        self.folder, self.oldpwd, self.stack, self.env, self.local, self.functions = folder, None, [], {}, {}, {}
        self.clean = False  # env -i started this shell: nothing inherited
        self.heads, self.before, self.made = {}, {}, {}

    def copy(self):
        twin = self.with_repo(self)
        return twin

    def with_repo(self, other):
        """This process state with the repository state of `other` (a subshell has ended)."""
        twin = ShellState(self.folder)
        twin.oldpwd, twin.stack, twin.env, twin.local, twin.functions, twin.clean = \
            self.oldpwd, list(self.stack), dict(self.env), dict(self.local), dict(self.functions), self.clean
        twin.heads, twin.before = dict(other.heads), dict(other.before)
        twin.made = {key: set(names) for key, names in other.made.items()}
        return twin

    def key(self):
        ordered = lambda items: tuple(sorted(items, key=repr))
        return (self.folder, self.oldpwd, self.clean, tuple(self.stack), ordered(self.env.items()),
                ordered(self.local.items()),
                ordered((name, id(body)) for name, body in self.functions.items()), ordered(self.heads.items()),
                ordered(self.before.items()), ordered((key, ordered(names)) for key, names in self.made.items()))

    def environment(self, extra=None, clear=False):
        env = {} if clear or self.clean else dict(os.environ)
        for name, value in [*([] if clear else self.env.items()), *(extra or {}).items()]:
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return env

    def exported(self, name):
        return self.env[name] is not None if name in self.env else not self.clean and name in os.environ

    def expand(self, word, extra=None):
        env = self.environment(extra)
        if word == "~" or word.startswith("~/"):
            word = env.get("HOME", "~") + word[1:]
        return re.sub(r"\$(\w+)|\$\{(\w+)\}", lambda m: env.get(m.group(1) or m.group(2), m.group(0)), word)

    def change_folder(self, program, args):
        """Applies cd/pushd/popd; True when it surely succeeds (the folder exists now)."""
        args = [a for a in args if a == "-" or not a.startswith("-")]  # cd -P, -L, -e, -@, --
        if program == "popd":
            if self.stack:
                self.oldpwd, self.folder = self.folder, self.stack.pop()
            return bool(self.stack) or self.folder is not None
        if program == "pushd":
            self.stack.append(self.folder)
        target = (self.oldpwd or self.folder) if args[:1] == ["-"] else self.expand(args[0] if args else "~")
        self.oldpwd, self.folder = self.folder, os.path.normpath(os.path.join(self.folder, target))
        return os.path.isdir(self.folder)


def unique(states):
    seen, kept = set(), []
    for state in states:
        key = state.key()
        if key not in seen:
            seen.add(key)
            kept.append(state)
    if len(kept) > MAX_STATES:
        raise RuntimeError(f"Shellzeile hat mehr als {MAX_STATES} mögliche Abläufe")
    return kept


def trusted_git():
    """The Git binary of the system, never one named by PATH - the command under review may set PATH."""
    return next((path for path in GIT_CANDIDATES if os.path.isfile(path) and os.access(path, os.X_OK)), None)


def git_environment(effective):
    """Environment of a read-only Git query: fixed PATH and locale, HOME and DEVELOPER_DIR of the hook process itself,
    and only the repository variables of the command under review - no PATH, loader or Git config variables from it."""
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    env.update({name: os.environ[name] for name in ("HOME", "DEVELOPER_DIR") if os.environ.get(name)})
    env.update({name: effective[name] for name in GIT_REPOSITORY_VARIABLES if effective.get(name)})
    return env


class CommitFinder:
    """Runs the parsed line on the set of states it can reach. Each node maps states to (success, failure) states:
    `&&` continues on success, `||` on failure, `;` on both; a branch switch may fail unless it surely works; a
    subshell, pipeline element, background job, substitution or `sh -c` keeps its folder and variables but not its
    branch switches. Every `git commit` notes "<branch> <command>" for each state it can run in."""

    def __init__(self):
        self.lines, self.answers, self.git_binary = [], {}, trusted_git()

    def run(self, node, states, top, depth=0):
        """(success, failure) states after `node`. top: the line's own shell, where `exit` ends everything."""
        if not states:
            return [], []
        if depth > MAX_DEPTH:
            return states, states
        kind = node[0]
        if kind == "list":
            status = (states, [])
            for and_or, terminator in node[1]:
                if terminator == "&":  # background: may or may not have happened when the next command runs
                    done = self.nested(and_or, states, depth)
                    states = unique(states + done[0] + done[1])
                    status = (states, [])
                else:
                    status = self.run(and_or, states, top, depth)
                    states = unique(status[0] + status[1])
            return status
        if kind == "andor":
            success, failure = self.run(node[1][0][1], states, top, depth)
            for operator, pipeline in node[1][1:]:
                if operator == "&&":
                    success, more = self.run(pipeline, success, top, depth)
                    failure = unique(failure + more)
                else:
                    more, failure = self.run(pipeline, failure, top, depth)
                    success = unique(success + more)
            return success, failure
        if kind == "pipeline":
            negate, commands = node[1], node[2]
            if len(commands) == 1:
                status = self.run(commands[0], states, top, depth)
            else:  # every element runs in a subshell
                for command in commands[:-1]:
                    done = self.nested(command, states, depth)
                    states = unique(done[0] + done[1])
                status = self.nested(commands[-1], states, depth)
            return status[::-1] if negate else status
        if kind == "subshell":
            return self.nested(node[1], states, depth)
        if kind == "group":
            return self.run(node[1], states, top, depth)
        if kind == "if":
            success, failure = self.run(node[1], states, top, depth)
            then = self.run(node[2], success, top, depth)
            other = self.run(node[3], failure, top, depth) if node[3] else (failure, [])
            return unique(then[0] + other[0]), unique(then[1] + other[1])
        if kind in ("loop", "for", "case"):  # any number of passes, any clause: everything reachable, status unknown
            if kind != "loop":
                states = self.substitutions(node[1], states, depth)
            parts = [node[1], node[2]] if kind == "loop" else [node[2]] if kind == "for" else node[2]
            reached = list(states)
            for _ in range(2):
                for part in parts:
                    done = self.run(part, reached, top, depth + 1)
                    reached = unique(reached + done[0] + done[1])
            return reached, reached
        if kind == "function":
            defined = []
            for state in states:
                state = state.copy()
                state.functions[node[1]] = node[2]
                defined.append(state)
            return defined, []
        success, failure = [], []
        for state in states:
            done = self.simple(node[1], state, top, depth)
            success, failure = success + done[0], failure + done[1]
        return unique(success), unique(failure)

    def nested(self, node, states, depth, prepare=None):
        """A subshell: its folder and variables stay inside, its branch switches do not."""
        success, failure = [], []
        for state in states:
            child = state.copy()
            if prepare:
                prepare(child)
            done = self.run(node, [child], False, depth + 1)
            success += [state.with_repo(inner) for inner in done[0]]
            failure += [state.with_repo(inner) for inner in done[1]]
        return unique(success), unique(failure)

    def substitutions(self, subs, states, depth):
        for sub in subs:
            done = self.nested(parse(sub), states, depth)
            states = unique(done[0] + done[1])
        return states

    def simple(self, tokens, state, top, depth):
        words, subs, stdin, target = [], [], None, None
        for kind, value in tokens:
            if kind == "sub":
                subs.append(value)
            elif kind == "word" and target:
                stdin = value if target == "herestring" else stdin
                target = None
            elif kind == "word":
                words.append(value)
            elif kind == "heredoc":
                subs += value.subs
                stdin = value.body if value.fd in (None, 0) else stdin
            elif kind == "herestring":
                target = "herestring" if value in (None, 0) else "other"
            elif kind == "redirect":
                fd, operator = value
                stdin = UNKNOWN if operator.startswith("<") and fd in (None, 0) else stdin
                target = "other"
        success, failure = [], []
        for current in self.substitutions(subs, [state], depth):
            done = self.command(words, stdin, current, top, depth)
            success, failure = success + done[0], failure + done[1]
        return success, failure

    def command(self, words, stdin, state, top, depth):
        env = {}
        while words and ASSIGNMENT.match(words[0]):
            name, value = words[0].split("=", 1)
            env[name] = value
            words = words[1:]
        if not words:  # NAME=value alone reaches later commands only once NAME is exported
            state = state.copy()
            for name, value in env.items():
                (state.env if state.exported(name) else state.local)[name] = value
            return [state], []
        words, chdir, clear = unwrap(words, env)
        if not words:
            return [state], [state]
        program, args = os.path.basename(words[0]), words[1:]
        if program in state.functions:
            return self.run(state.functions[program], [state], top, depth + 1)
        if program in ("true", ":"):
            return [state], []
        if program == "false":
            return [], [state]
        if program == "exit":
            return ([], []) if top else ([state], [state])  # nested: only that shell ends, conservatively go on
        if program in ("cd", "pushd", "popd", "export", "unset"):
            changed = state.copy()
            if program in ("cd", "pushd", "popd"):
                return [changed], [] if changed.change_folder(program, args) else [state]
            for arg in args:
                if program == "unset" and not arg.startswith("-"):
                    changed.env[arg] = None
                    changed.local.pop(arg, None)
                elif program == "export":
                    name, value = arg.split("=", 1) if ASSIGNMENT.match(arg) else (arg, changed.local.get(arg))
                    changed.env.update({name: value} if value is not None else {})
            return [changed], []
        if program == "eval":
            return self.run(parse(shell_tokens(" ".join(args))[0]), [state], top, depth + 1)
        if program in SHELLS:
            script = shell_script(args, stdin)
            if script is None:
                return [state], [state]

            def prepare(child):
                child.env, child.clean = ({}, True) if clear else (child.env, child.clean)
                child.env.update(env)
                child.folder = os.path.join(child.folder, child.expand(chdir)) if chdir else child.folder
            return self.nested(parse(shell_tokens(script)[0]), [state], depth, prepare)
        if program == "git":
            return self.git(args, state, env, chdir, clear)
        return [state], [state]

    def ask(self, repo, *args):
        """stdout of a read-only `git <repository options> <args>` as the line would run it, None on failure."""
        folder, context, env = repo
        key = (folder, tuple(context), tuple(sorted(env.items())), args)
        if key not in self.answers:
            self.answers[key] = None
            if self.git_binary:
                try:
                    result = subprocess.run([self.git_binary, *context, *args], cwd=folder, env=env,
                                            capture_output=True, text=True, timeout=3)
                    self.answers[key] = result.stdout.strip() if result.returncode == 0 else None
                except (OSError, subprocess.SubprocessError, ValueError):
                    pass
        return self.answers[key]

    def branch(self, repo, value):
        if value == CURRENT:
            return self.ask(repo, "symbolic-ref", "-q", "--short", "HEAD")
        if value == PREVIOUS:
            name = self.ask(repo, "rev-parse", "--symbolic-full-name", "@{-1}") or ""
            return name[len("refs/heads/"):] if name.startswith("refs/heads/") else None
        return value

    def known_branch(self, repo, key, state, name):
        return name in state.made.get(key, ()) or bool(self.ask(repo, "rev-parse", "-q", "--verify",
                                                                 f"refs/heads/{name}"))

    def git(self, args, state, extra, chdir, clear):
        here = os.path.join(state.folder, state.expand(chdir, extra)) if chdir else state.folder
        folder, context, i = here, [], 0
        while i < len(args) and args[i].startswith("-"):
            name, equals, value = args[i].partition("=")
            if args[i].startswith("-C") and args[i] != "-C":  # -C<path>: Git 2.54 rejects it, following it is safe
                name, equals, value = "-C", "=", args[i][2:]
            if name in GIT_CONTEXT_OPTIONS and (equals or i + 1 < len(args)):
                if not equals:
                    i += 1
                    value = args[i]
                value = state.expand(value, extra)
                context += [name, value]
                here = os.path.join(here, value) if name == "-C" else here
            elif args[i] == "--bare":
                context.append(args[i])
            elif args[i] in GIT_VALUE_OPTIONS:
                i += 1
            i += 1
        if i >= len(args):
            return [state], [state]
        repo = (folder, context, git_environment(state.environment(extra, clear)))
        found = self.ask(repo, "rev-parse", "--absolute-git-dir")
        key = os.path.realpath(found) if found else ("?", os.path.normpath(here), repr(context),
                                                       repo[2].get("GIT_DIR"))
        sub, rest = args[i], [state.expand(a, extra) for a in args[i + 1:]]
        if sub in ("checkout", "switch"):
            target, how = switch_target(sub, rest, lambda name, path_possible: self.kind(
                repo, key, here, state, name, path_possible))
            if target is ...:
                return [state], [state]
            if how == "create" and target and self.known_branch(repo, key, state, target):
                return [], [state]  # the branch exists: creating it fails, HEAD stays
            switched, now = state.copy(), state.heads.get(key, CURRENT)
            if target == "-":
                target = self.branch(repo, state.before.get(key, PREVIOUS))
            switched.before[key], switched.heads[key] = now, target
            if how == "create" and target:
                switched.made.setdefault(key, set()).add(target)
            return [switched], [state] if how == "maybe" else []
        if sub == "branch":
            flags, names = [a for a in rest if a.startswith("-")], [a for a in rest if not a.startswith("-")]
            changed = state.copy()
            if {"-m", "-M", "--move"} & set(flags):
                if len(names) == 1 or (len(names) == 2 and names[0] == self.branch(repo, state.heads.get(key, CURRENT))):
                    changed.heads[key] = names[-1]  # the checked-out branch was renamed
                return [changed], [state]
            if names and set(flags) <= {"-f", "--force", "-t", "--track", "--no-track", "-q", "--quiet"}:
                if not {"-f", "--force"} & set(flags) and self.known_branch(repo, key, state, names[0]):
                    return [], [state]
                changed.made.setdefault(key, set()).add(names[0])
                return [changed], []
            return [state], [state]
        if sub == "commit":
            branch = self.branch(repo, state.heads.get(key, CURRENT))
            line = f"{branch or '-'} " + shlex.join(["git", *args]).replace("\n", "\\n")
            if line not in self.lines:
                self.lines.append(line)
        return [state], [state]

    def kind(self, repo, key, here, state, name, path_possible):
        if self.known_branch(repo, key, state, name):
            return "branch"
        if self.ask(repo, "rev-parse", "-q", "--verify", f"{name}^{{commit}}"):
            return "commit"
        if path_possible and (name.startswith(":") or any(c in name for c in "*?[")
                              or os.path.lexists(os.path.join(here, name))):
            return "path"
        return "new"  # unknown now: made from a remote branch, or an error that changes nothing


def git_commits(command, cwd):
    """One line "<branch> <git commit ...>" per branch a `git commit` of the shell line can land on ("-" = no branch:
    detached, no repository, unknown)."""
    if not re.search(r"\bgit\b", command) or "commit" not in command:
        return ""
    finder = CommitFinder()
    finder.run(parse(shell_tokens(command)[0]), [ShellState(cwd)], True)
    return "\n".join(finder.lines)


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
    hits = matching_rules(rules, tool, data, payload.get("cwd"))
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
