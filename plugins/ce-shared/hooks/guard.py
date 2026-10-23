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


def field_value(field, tool, data, cwd=None, facts=None):
    if field == "merge_attempt":
        return merge_attempt(tool, data, cwd or os.getcwd())
    if tool == "Bash":
        if field == "git_commit":
            facts = facts or commit_facts(data.get("command", ""), cwd or os.getcwd())
            return "\n".join(facts.lines)
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


def condition_holds(condition, tool, data, cwd=None, facts=None):
    value = field_value(condition.get("field", ""), tool, data, cwd, facts)
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


def matching_rules(rules, tool, data, cwd=None, facts=None):
    event = "bash" if tool == "Bash" else "file" if tool in FILE_TOOLS else "mcp" if tool.startswith("mcp__") else None
    if event is None:
        return []
    return [rule for rule in rules if rule.get("event", "all") in (event, "all")
            and all(condition_holds(c, tool, data, cwd, facts) for c in rule["conditions"])]


# ---------------------------------------------------------------------------------------------- git commit field
#
# commit-on-main judges one statically clear form before the line runs: a whole line that is a single simple command
# `git [options] commit …` of literal words (quotes, backslashes and comments are read like the shell does, also inside
# a word: 'gi't, g\it; no $, `, glob or brace character). Its branch comes from the folder of the hook input,
# `git -C`, `--git-dir`, `--work-tree`, `--bare` and GIT_DIR/GIT_WORK_TREE/GIT_COMMON_DIR inherited from the client,
# read with the system's Git (fixed path, small fixed environment) - never a git, PATH or loader named by the line.
# Everything else that may run a commit (prefix assignments NAME=value, operators, subshells, variables, substitutions,
# functions, wrappers, scripts, redirections, pipelines) is not simulated: it passes with a note and the Git net
# (githooks/: pre-commit sees the real HEAD) decides when it runs. --allow-empty-message is blocked in every literal
# `git … commit` segment of the line.

GIT_CANDIDATES = ("/usr/bin/git", "/opt/homebrew/bin/git", "/usr/local/bin/git")
GIT_REPOSITORY_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")
GIT_CONTEXT_OPTIONS = ("-C", "--git-dir", "--work-tree")  # followed (with their value) by the branch query
GIT_FLAGS = ("--bare", "--no-pager", "-P", "-p", "--paginate", "--no-optional-locks", "--literal-pathspecs",
             "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs", "--no-replace-objects", "--no-advice")
COMMIT_VALUE_OPTIONS = ("-m", "--message", "-F", "--file", "-C", "--reuse-message", "-c", "--reedit-message",
                        "--fixup", "--squash", "--author", "--date", "-t", "--template", "--cleanup", "--trailer",
                        "--pathspec-from-file")
INERT = ("echo", "printf", "grep", "egrep", "rg", "cat", "head", "tail", "less", "man", "true", "false", "test", "[")
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")
MENTIONS_COMMIT = re.compile(r"\bgit\b[\s\S]*\bcommit\b", re.IGNORECASE)  # only decides whether a note is due
OPERATORS = ("&&", "||", ";;", "<<<", "<<", ">>", "&>", "|&", "&", "|", ";", "<", ">", "(", ")", "\n")
NET = ("das Git-Netz (pre-commit) prüft den tatsächlichen Branch beim Ausführen; ob es in diesem Repo aktiv ist, "
       "zeigt `ce_githooks.py status`")


class CommitFacts:
    """lines: "<branch> git commit[ --allow-empty-message]" per judged commit ("-" no branch, "?" not judged) for the
    rule; notes: why a possible commit was left to the Git net."""

    def __init__(self, lines=(), notes=()):
        self.lines, self.notes = list(lines), list(notes)


def shell_segments(text):
    """The words of a shell line, split into segments at unquoted operator characters, and the first construct that
    makes the line more than one simple command of literal words (None if there is none). A word is (text, literal):
    quotes and backslashes removed; literal False when it holds an expansion ($, `, glob or brace characters, ~user).
    A heredoc ends the reading - its body is no command line."""
    segments, words, chars, why = [], [], [], None
    literal, started, i, n = True, False, 0, len(text)

    def end_word():
        nonlocal chars, literal, started
        if started:
            words.append(("".join(chars), literal))
        chars, literal, started = [], True, False

    while i < n:
        c = text[i]
        if c == "\\":
            if text[i + 1:i + 2] != "\n":  # backslash-newline continues the line
                chars.append(text[i + 1:i + 2])
                started = True
            i += 2
        elif c == "'":
            end = text.find("'", i + 1)
            why = why or ("offenes Anführungszeichen" if end < 0 else None)
            end = n if end < 0 else end
            chars.append(text[i + 1:end])
            started, i = True, end + 1
        elif c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and text[i + 1:i + 2] in ('"', "\\", "$", "`", "\n"):
                    chars.append(text[i + 1] if text[i + 1] != "\n" else "")
                    i += 2
                    continue
                if text[i] in "$`":
                    literal, why = False, why or "Variable oder Befehlsersetzung"
                chars.append(text[i])
                i += 1
            why = why or ("offenes Anführungszeichen" if i >= n else None)
            started, i = True, i + 1
        elif c == "#" and not started:  # a comment up to the end of the line
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif c in " \t":
            end_word()
            i += 1
        elif c == "\n" and not text[i:].strip():
            break  # trailing whitespace
        elif any(text.startswith(op, i) for op in OPERATORS):
            op = next(op for op in OPERATORS if text.startswith(op, i))
            why = why or ("Zeilenumbruch" if op == "\n" else f"Operator `{op}`")
            end_word()
            segments.append(words)
            words = []
            if op == "<<":
                break  # heredoc
            i += len(op)
        else:
            if c in "$`":
                why = why or "Variable oder Befehlsersetzung"
                literal = False
            elif c in "*?[" or (c == "{" and brace_expansion(text, i, started)):
                why = why or "Muster, Klammererweiterung oder Gruppe"
                literal = False
            elif c == "~" and not started:
                home, rest = os.environ.get("HOME"), text[i + 1:i + 2]
                if home and (rest in ("", "/", " ", "\t", "\n") or rest in ";&|<>()"):
                    chars.append(home)
                    started, i = True, i + 1
                    continue
                why, literal = why or "Tilde-Erweiterung", False
            chars.append(c)
            started, i = True, i + 1
    end_word()
    segments.append(words)
    return [words for words in segments if words], why


def brace_expansion(text, i, started):
    """True for an unquoted { at i that the shell expands or reads as a group: `{ …; }`, {a,b}, {1..3}."""
    if not started and text[i + 1:i + 2] in ("", " ", "\t", "\n"):
        return True
    end = text.find("}", i)
    inner = text[i + 1:end] if end > 0 else ""
    return not re.search(r"[\s'\"]", inner) and ("," in inner or ".." in inner)


def commit_call(words):
    """What one segment runs: None (no `git commit`), a str (why it may run one that is not judged here) or
    (git context options, commit arguments) of a literal `git [options] commit …`."""
    text = " ".join(word for word, _ in words)
    if ASSIGNMENT.match(words[0][0]):  # NAME=value prefixes (or a quoted name that only looks like one): not read
        return "Präfix-Zuweisung `NAME=…` wird vorab nicht ausgewertet" if MENTIONS_COMMIT.search(text) else None
    program, literal = words[0]
    if not literal or os.path.basename(program) != "git":
        if not MENTIONS_COMMIT.search(text) or (literal and os.path.basename(program) in INERT):
            return None
        return ("Programmname erst beim Ausführen bekannt" if not literal
                else f"`{os.path.basename(program)}` kann `git commit` starten: Wrapper, Shell, Funktion oder Skript")
    context, i = [], 1
    while i < len(words) and words[i][0].startswith("-"):
        arg, literal = words[i]
        name, equals, value = arg.partition("=")
        if not literal:
            return "Variable oder Ersetzung in den Git-Optionen"
        if arg in GIT_CONTEXT_OPTIONS or (equals and name in GIT_CONTEXT_OPTIONS[1:]):
            if not equals:
                if i + 1 >= len(words) or not words[i + 1][1]:
                    return "Git-Option ohne festen Wert"
                i, value = i + 1, words[i + 1][0]
            context += [name, value]
        elif arg == "--bare":
            context.append(arg)
        elif arg == "-c":
            i += 1  # a config value does not move HEAD
        elif arg not in GIT_FLAGS:
            return f"Git-Option `{arg}` wird vorab nicht ausgewertet"
        i += 1
    if i >= len(words):
        return None
    subcommand, literal = words[i]
    if not literal:
        return "Git-Unterbefehl erst beim Ausführen bekannt" if MENTIONS_COMMIT.search(text) else None
    return (context, [word for word, _ in words[i + 1:]]) if subcommand == "commit" else None


def allows_empty_message(args):
    """True when the commit arguments set --allow-empty-message (Git also takes an unambiguous prefix)."""
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg == "--":
            return False
        elif arg in COMMIT_VALUE_OPTIONS or (re.fullmatch(r"-[a-zA-Z]+", arg) and arg[-1] in "mFCct"
                                             and not re.search(r"[mFCct]", arg[1:-1])):
            skip = True  # its value follows: `-m --allow-empty-message` is a message
        elif len(arg) > len("--allow-empty") and "--allow-empty-message".startswith(arg):
            return True
    return False


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


def read_branch(cwd, context):
    """(branch or None, problem or None) of HEAD where the commit would run. No repository there (or no such folder):
    no commit can land, (None, None). Detached HEAD: (None, None). Anything else unexpected is a problem (fail open)."""
    if not os.path.isdir(cwd):
        return None, None
    git = trusted_git()
    if git is None:
        return None, f"kein System-Git gefunden ({', '.join(GIT_CANDIDATES)})"
    try:
        result = subprocess.run([git, *context, "symbolic-ref", "-q", "HEAD"], cwd=cwd, capture_output=True,
                                text=True, timeout=3, env=git_environment(os.environ))
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return None, f"Branch nicht lesbar ({error.__class__.__name__}: {error})"
    if result.returncode == 0:  # full ref: --short gives "heads/main" when a tag main exists too
        ref = result.stdout.strip()
        return (ref[len("refs/heads/"):] or None) if ref.startswith("refs/heads/") else None, None
    if result.returncode == 1 or "not a git repository" in result.stderr or "cannot change to" in result.stderr:
        return None, None
    detail = (result.stderr.strip().splitlines() or [f"Exit {result.returncode}"])[-1]
    return None, f"Branch nicht lesbar (git: {detail[:200]})"


def commit_facts(command, cwd):
    """CommitFacts of a shell line. Never raises: an internal error becomes a note, the other rules still run."""
    try:
        segments, why = shell_segments(command)
        calls = [commit_call(words) for words in segments]
        lines = ["? git commit --allow-empty-message" for call in calls
                 if isinstance(call, tuple) and allows_empty_message(call[1])]
        if why is None and len(calls) == 1 and isinstance(calls[0], tuple):
            context, args = calls[0]
            branch, problem = read_branch(cwd, context)
            line = f"{branch or '-'} git commit" + (" --allow-empty-message" if allows_empty_message(args) else "")
            return CommitFacts([line], [f"Vorabprüfung: {problem}; nicht gesperrt, {NET}."] if problem else [])
        reasons = [call for call in calls if isinstance(call, str)]
        words = " ".join(word for segment in segments for word, _ in segment)
        if not any(isinstance(call, tuple) for call in calls) and not reasons and (
                why is None or not (MENTIONS_COMMIT.search(command) or MENTIONS_COMMIT.search(words))):
            return CommitFacts(lines)  # no commit in sight: echo 'git commit', git log --grep commit, make && ls
        reason = why or reasons[0]
        return CommitFacts(lines, [f"Vorabprüfung: möglicher `git commit`, Branch vorab nicht sicher bestimmbar "
                                   f"({reason}); nicht gesperrt, {NET}."])
    except Exception as error:  # never lose the other rules over this field
        return CommitFacts([], [f"Vorabprüfung fehlgeschlagen ({error.__class__.__name__}: {error}); nicht gesperrt, "
                                f"{NET}."])


def git_commits(command, cwd):
    """The git_commit field: one line per judged commit of the shell line (see CommitFacts)."""
    return "\n".join(commit_facts(command, cwd).lines)


# -------------------------------------------------------------------------------------------- merge attempt field
#
# merge-nur-sightline (0.22): only Sightline merges. The field merge_attempt is "merge" when the call would merge a pull
# request or switch on its auto-merge and the session is not Sightline, else "". Bash: a `gh pr merge` or `gh api` call
# (PUT …/pulls/N/merge, GraphQL mergePullRequest / enablePullRequestAutoMerge) in any segment of the line, also behind
# NAME=value, env/command/exec/time/nohup and `bash|sh|zsh -c '…'` / eval; the words come from shell_segments, so quotes
# are read like the shell does. Other tools: the GitHub MCP merge tools. `git merge` is no PR merge and stays free
# (catching up with main in a working branch). Sightline = CE_ROLE=sightline, or the name reserved in the git dir of the
# session's folder (ce-session.json, written by `ce session reserve`). What cannot be read is not Sightline - except an
# error while reading, which lets the call through (fail open).

MERGE_TOOLS = ("mcp__github__merge_pull_request", "mcp__github__enable_pr_auto_merge")
MERGE_MUTATIONS = ("mergePullRequest", "enablePullRequestAutoMerge")
SESSION_FILE = "ce-session.json"
SIGHTLINE = re.compile(r"sightline(\s|·|$)", re.IGNORECASE)
WRAPPERS = ("env", "command", "exec", "time", "nohup")
SHELLS = ("bash", "sh", "zsh")


def merge_words(words):
    """True when the words of one segment run a PR merge (gh pr merge, gh api merge call or merge mutation)."""
    words = [word for word, _ in words]
    while words and (ASSIGNMENT.match(words[0]) or os.path.basename(words[0]) in WRAPPERS):
        words = words[1:]
    if not words:
        return False
    program, args = os.path.basename(words[0]), words[1:]
    if program in SHELLS or program == "eval":
        script = args[args.index("-c") + 1:] if "-c" in args else args if program == "eval" else []
        return any(merge_line(" ".join(script) if program == "eval" else script[0])) if script else False
    if program != "gh" or len(args) < 2:
        return False
    if args[0] == "pr" and args[1] == "merge":
        return not {"--help", "-h"} & set(args[2:])
    if args[0] != "api":
        return False
    method = ""
    for i, word in enumerate(args):
        name, equals, value = word.partition("=")
        if word in ("-X", "--method") and i + 1 < len(args):
            method = args[i + 1]
        elif equals and name == "--method":
            method = value
        elif word.startswith("-X") and len(word) > 2:
            method = word[2:]
    if any(mutation in word for word in args for mutation in MERGE_MUTATIONS):
        return True
    return method.upper() == "PUT" and any(re.search(r"/pulls/\d+/merge/?$", word) for word in args)


def merge_line(command):
    """[True] per merging segment of a shell line (a list, so a nested -c script can be passed on)."""
    segments, _ = shell_segments(command)
    return [True for words in segments if merge_words(words)]


def is_sightline(cwd):
    """True for the Sightline session. Raises on a failed read (the caller fails open)."""
    if os.environ.get("CE_ROLE", "").strip().lower() == "sightline":
        return True
    git = trusted_git()
    if git is None or not os.path.isdir(cwd):
        return False
    result = subprocess.run([git, "rev-parse", "--absolute-git-dir"], cwd=cwd, capture_output=True, text=True,
                            timeout=3, env=git_environment(os.environ))
    if result.returncode != 0:
        return False
    try:
        name = json.loads((Path(result.stdout.strip()) / SESSION_FILE).read_text(encoding="utf-8")).get("name", "")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(name, str) and SIGHTLINE.match(name.strip()) is not None


def merge_attempt(tool, data, cwd):
    """"merge" for a PR merge by a session that is not Sightline, else ""."""
    try:
        if tool == "Bash":
            attempt = bool(merge_line(data.get("command", "")))
        else:
            attempt = tool in MERGE_TOOLS
        return "merge" if attempt and not is_sightline(cwd) else ""
    except Exception:  # never lose a call over this field
        return ""


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
    cwd = payload.get("cwd")
    commit_rules = [r["name"] for r in rules if any(c.get("field") == "git_commit" for c in r["conditions"])]
    facts = commit_facts(data.get("command", ""), cwd or os.getcwd()) if tool == "Bash" and commit_rules else None
    hits = matching_rules(rules, tool, data, cwd, facts)
    blocks = [f"**[{r['name']}]**\n{r['message']}" for r in hits if r.get("action") == "block"]
    warnings = [f"**[{r['name']}]**\n{r['message']}" for r in hits if r.get("action") != "block"]
    warnings += [f"**[{commit_rules[0]}]**\n{note}" for note in (facts.notes if facts else [])]
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
