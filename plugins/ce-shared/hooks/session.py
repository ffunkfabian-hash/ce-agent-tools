#!/usr/bin/env python3
"""SessionStart, PostToolUse and Stop hook of ce-shared: what a session must see at the start, while it works and at the end.

SessionStart (additionalContext): the repo's docs/LESSONS.md (or Docs/) - unless the repo's CLAUDE.md already imports
it (0.33: it came twice in 7 repos, in Cube Escape 8,950 characters per session) - the session-name proposal of
ce_session_name.py, and one line naming the tools every session is expected to use (ce_search, heavy_lock --resource,
session_name --reserve, ./check, pruefung.md). Nothing is installed. Since 0.16 it also compares the loaded ce-shared
version with the newest this machine knows and warns visibly when the session runs an older one. 0.36: the hard core
(hooks/kern.md, a word-for-word copy of Nexogen templates/kern.md, tests/test_kern.py) comes first - a cloud container has
no global CLAUDE.md - unless the memory files the hook can name already carry every rule. Unsure means it is shown.
PostToolUse (0.33 Earshot, `pulse`): every PULSE_EVERY tool calls a cheap look at the transcript - record count and the
input of the last request - and, crossing a limit, one hint as additionalContext for the model: over 300 records
(Trimline: "über 300 Nachrichten" came 1,177 times as a Stop warning that only the user saw), over 150,000 input tokens.
Each hint once per session and threshold step (hint_state).
Stop: runs ce_report.py on the transcript and appends its line and hints to ce-session-stats.log in the git dir - the
measurement. 0.35: no longer to report.md, whose text becomes the PR description (statistics stood in PR #37); no
systemMessage either (0.33: those warnings reached only the user). check-before-stop - code or
assets changed but no ./check or test ran - goes to the model as additionalContext once per session and never while
stop_hook_active (probe 04.11.2026: a Stop additionalContext makes the model answer once more; repeated, it would loop).
It never blocks; any error lets the session end.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
TOOLS = PLUGIN_ROOT / "tools"
TIMEOUT = 15
TOOL_LINE = ("Werkzeuge dieser Sitzung (Pflicht, globale Regeln): `ce_search` (MCP, vor dem Lesen langer Docs) · "
             "`./ce session reserve <Name> --about … --model …` vor der ersten Änderung (Patch: `./ce session patch <kurzname>`; meldet im Postfach an, den Wächter startet ab ce-shared 0.15 der Stop-Hook) · "
             "`heavy_lock --resource unity|blender|gpu` für schwere Läufe · `./check` vor Commit · Richter mit `templates/pruefung.md` · "
             "Bericht = PR-Beschreibung, Ist-Zeit als Warten/Rechnen/Lesen/Umsetzen.")
KERN_FILE = Path(__file__).resolve().parent / "kern.md"
KERN_RULE = re.compile(r"^\d+\. .+$", re.MULTILINE)
KERN_HEAD = "Harter Kern (Plugin-Kopie von Nexogen `templates/kern.md`; weicht eine geladene Fassung ab, gilt die neuere):"
KERN_MISSING = "WARNUNG: Der harte Kern fehlt im Plugin (hooks/kern.md); er steht in Nexogen `templates/kern.md`, Plugin neu installieren."
STATS_FILE = "ce-session-stats.log"  # in the git dir of the worktree: outside the tree, gone with the worktree
CLAUDE_FILES = ("CLAUDE.md", "CLAUDE.local.md", os.path.join(".claude", "CLAUDE.md"))
IMPORT = re.compile(r"^\s*@(\S+)", re.MULTILINE)
PULSE_EVERY = 20              # PostToolUse: look at the transcript every 20th tool call
MESSAGE_LIMIT = int(os.environ.get("CE_MESSAGE_LIMIT") or 300)  # records (user + assistant) per session (› Sitzungen kurz halten); the variable is a probe aid
INPUT_LIMIT = 150_000         # input tokens of one request (ce_report.MAX_CONTEXT_TOKENS); 300,000 for Sightline (input_limit)
TAIL_BYTES = 512 * 1024       # the last requests of the transcript are enough for the current input
RECORD_MARK = re.compile(rb'"type":\s*"(user|assistant)"')  # a record line, with or without blanks after the colon
USAGE_MARK = b'"usage"'
INPUT_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
CHECK_HINT = ("Code/Assets geändert, aber kein ./check oder Testlauf in dieser Sitzung (check-before-stop, DoD 2): "
              "jetzt `./ce check quick` plus gezielte Prüfung ausführen oder im Bericht begründen.")

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import hint_state  # noqa: E402
except Exception:  # noqa: BLE001 - without the throttle every hint is due
    hint_state = None

# (0.31) Windows decodes pipes as cp1252; git and the tools write UTF-8. Empty on POSIX.
UTF8 = {"encoding": "utf-8", "errors": "replace"} if os.name == "nt" else {}


def utf8_io() -> None:
    """(0.31) Windows: the client's JSON in and out as UTF-8 - a cp1252 stdout cannot take → and the hook would die
    without an answer. Fail-open; nothing on POSIX."""
    if os.name != "nt":
        return
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def run(argv, cwd=None, timeout=TIMEOUT):
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, **UTF8).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def imports(path: Path) -> list:
    """The `@file` imports of a Claude memory file (relative to its folder), [] when it does not exist."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return IMPORT.findall(text)


def resolved(path: Path, entry: str):
    """The file an `@entry` of `path` names: relative to the importing file's folder, as Claude Code reads it;
    None for `~` and absolute paths (outside the repo)."""
    if entry.startswith(("/", "~")):
        return None
    try:
        return (path.parent / entry).resolve()
    except OSError:
        return None


def lessons_imported(root: Path, target: Path) -> bool:
    """True when the repo's CLAUDE.md (or CLAUDE.local.md, .claude/CLAUDE.md) imports `target` (the LESSONS file)
    itself or through a file it imports (one level, e.g. AGENTS.md) - then it is in the start context already.
    Imports resolve relative to the importing file (judge 0.33, 6): `.claude/CLAUDE.md` with `@docs/LESSONS.md`
    names `.claude/docs/LESSONS.md`, not the repo's."""
    def same(path) -> bool:  # by inode: a case-insensitive file system or a link names the same file differently
        try:
            return path is not None and os.path.samefile(path, target)
        except OSError:
            return False
    for name in CLAUDE_FILES:
        path = root / name
        for entry in imports(path):
            first = resolved(path, entry)
            if same(first):
                return True
            if first is not None and any(same(resolved(first, e)) for e in imports(first)):
                return True
    return False


def lessons(root: Path) -> str:
    for candidate in (root / "docs" / "LESSONS.md", root / "Docs" / "LESSONS.md"):
        if candidate.is_file():
            if lessons_imported(root, candidate):
                return ""
            text = candidate.read_text(encoding="utf-8", errors="replace")
            return f"===== {candidate.relative_to(root)} (Lehren, immer geladen) =====\n{text[:12000]}"
    return ""


def memory_text(root: Path) -> str:
    """What the session has loaded as far as the hook can name it: the user's global CLAUDE.md, the repo's memory files
    and their imports (one level). A parent folder's CLAUDE.md is not read: a miss only shows the core once too often."""
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    texts = []
    for path in [config / "CLAUDE.md"] + [root / name for name in CLAUDE_FILES]:
        for item in [path] + [resolved(path, entry) for entry in imports(path)]:
            try:
                texts.append(item.read_text(encoding="utf-8", errors="replace") if item else "")
            except OSError:
                pass
    return "\n".join(texts)


def kern_block(root: Path, source: Path = None) -> str:
    """The hard core for the start context, empty when the session has every rule word for word already; an older
    global file with another wording does not count, so the core is shown."""
    try:
        rules = KERN_RULE.findall((source or KERN_FILE).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        rules = []
    if not rules:
        return KERN_MISSING
    try:
        loaded = memory_text(root)
    except Exception:  # noqa: BLE001 - a broken look must not cost the session its context; unsure means shown
        loaded = ""
    return "" if all(rule in loaded for rule in rules) else KERN_HEAD + "\n" + "\n".join(rules)


def loaded_version(plugin_root: Path = PLUGIN_ROOT) -> str:
    try:
        return json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")).get("version") or ""
    except (OSError, ValueError):
        return ""


def version_hint(plugin_root: Path = PLUGIN_ROOT, folder=None) -> str:
    """a warning when this session loaded an older ce-shared than the machine has (0.16), else empty"""
    sys.path.insert(0, str(plugin_root / "tools"))
    import ce_plugins  # type: ignore
    loaded = loaded_version(plugin_root)
    newest = ce_plugins.newest_known(folder or ce_plugins.plugins_dir())
    if not loaded or not newest or ce_plugins.version_key(loaded) >= ce_plugins.version_key(newest):
        return ""
    return (f"ce-shared {loaded} geladen, auf diesem Rechner gibt es {newest}: Hooks und Werkzeuge dieser Sitzung sind veraltet. "
            f"Am Mac `./ce plugins check` – es nennt den Schritt (Update holen oder `./ce plugins fix --apply`); danach Sitzung neu starten.")


def session_start(payload: dict) -> dict:
    root = Path(payload.get("cwd") or os.getcwd())
    parts = [TOOL_LINE]
    kern = kern_block(root)
    if kern:
        parts.insert(0, kern)
    try:
        hint = version_hint()
    except Exception:  # noqa: BLE001 - a broken check must not cost the session its context
        hint = ""
    if hint:
        parts.insert(0, "WARNUNG: " + hint)
    proposal = run([sys.executable, str(TOOLS / "ce_session_name.py"), "--task", "Auftrag", "--offline"], cwd=root)
    if proposal:
        parts.append("Sitzungsname (Vorschlag ohne Version; mit --name <Name> --reserve für eine Version):\n" + proposal)
    text = lessons(root)
    if text:
        parts.append(text)
    if hint_state is not None:
        hint_state.prune()
    result = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n\n".join(parts)}}
    if hint:
        result["systemMessage"] = hint
    return result


# ------------------------------------------------------------------------------------------------- PostToolUse

def transcript_measure(path: Path) -> tuple:
    """(records, input tokens of the last request) of a transcript: the record count is a byte scan for user/assistant
    lines (as ce_report counts them); the input is read from the last record of the tail that carries a usage object
    (parsed as JSON, so nested `cache_creation` objects or blanks do not matter - judge 0.33, 4.1): input +
    cache-creation + cache-read tokens. (0, 0) when unreadable."""
    try:
        size = path.stat().st_size
        records = 0
        with open(path, "rb") as handle:
            for line in handle:
                if RECORD_MARK.search(line):
                    records += 1
            handle.seek(max(0, size - TAIL_BYTES))
            tail = handle.read()
    except OSError:
        return 0, 0
    for line in reversed(tail.splitlines()):
        if USAGE_MARK not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:  # the cut first line of the tail, or no record
            continue
        message = record.get("message") if isinstance(record, dict) else None
        usage = (message or {}).get("usage") if isinstance(message, dict) else None
        usage = usage if isinstance(usage, dict) else record.get("usage") if isinstance(record, dict) else None
        if isinstance(usage, dict):
            return records, sum(int(usage.get(f) or 0) for f in INPUT_FIELDS if isinstance(usage.get(f), (int, float)))
    return records, 0


def due(payload: dict, key: str, every: int = 0) -> bool:
    if hint_state is None:
        return True
    return hint_state.due(str(payload.get("session_id") or ""), key, every)


def input_limit(payload: dict) -> int:
    """The input limit of this session (ce_report.input_limit: Sightline 300,000, all others INPUT_LIMIT); never raises."""
    try:
        if str(TOOLS) not in sys.path:
            sys.path.insert(0, str(TOOLS))
        import ce_report  # type: ignore
        return ce_report.input_limit(payload.get("cwd") or os.getcwd())
    except Exception:  # noqa: BLE001 - a hint limit must not wedge a tool call
        return INPUT_LIMIT


def pulse(payload: dict) -> dict:
    """PostToolUse: every PULSE_EVERY tool calls, hints on the session limits as additionalContext (once each)."""
    transcript = payload.get("transcript_path")
    if not transcript or not Path(transcript).is_file():
        return {}
    if hint_state is not None and hint_state.count(str(payload.get("session_id") or ""), "pulse") % PULSE_EVERY != 1:
        return {}
    records, last_input = transcript_measure(Path(transcript))
    hints = []
    if records > MESSAGE_LIMIT and due(payload, f"messages>{records // MESSAGE_LIMIT * MESSAGE_LIMIT}"):
        hints.append(f"Sitzung über {MESSAGE_LIMIT} Nachrichten ({records}): Etappe abschließen und an eine frische "
                     "Sitzung übergeben (› Sitzungen kurz halten, Übergabe nur ohne Verlauf).")
    limit = input_limit(payload) if last_input > INPUT_LIMIT else INPUT_LIMIT  # no git call below the default
    if last_input > limit and due(payload, "input>limit"):
        hints.append(f"Letzte Anfrage {last_input:,} Input-Tokens (> {limit:,}): Kontext ist groß – Etappe "
                     "abschließen und an eine frische Sitzung übergeben (› Sitzungen kurz halten).")
    if not hints:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n".join(hints)}}


# -------------------------------------------------------------------------------------------------------- Stop

def changed_code(root: Path) -> bool:
    out = run(["git", "diff", "--name-only", "HEAD"], cwd=root) + "\n" + run(["git", "ls-files", "--others", "--exclude-standard"], cwd=root)
    return any(line.strip().endswith((".cs", ".py", ".sh", ".json", ".blend", ".unity", ".prefab", ".asset")) for line in out.splitlines())


def stop(payload: dict) -> dict:
    root = Path(payload.get("cwd") or os.getcwd())
    transcript = payload.get("transcript_path")
    errors, unchecked = [], False
    if transcript and Path(transcript).is_file():
        sys.path.insert(0, str(TOOLS))
        try:
            import ce_report  # type: ignore
            info = ce_report.analyse(Path(transcript))
            line = ce_report.one_line(info)
            hints = ce_report.hints(info, ce_report.input_limit(root))
            unchecked = changed_code(root) and info["checks"] == 0
            if unchecked:
                hints.append("Code/Assets geändert, aber kein ./check oder Testlauf in dieser Sitzung (check-before-stop, DoD 2)")
            git_dir = run(["git", "rev-parse", "--absolute-git-dir"], cwd=root)
            try:
                if git_dir:
                    with open(Path(git_dir) / STATS_FILE, "a", encoding="utf-8") as handle:
                        handle.write("\n" + line + "\n" + "".join("  Hinweis: " + w + "\n" for w in hints))
            except OSError:
                pass
        except Exception as error:  # noqa: BLE001 - never wedge the session end
            errors.append(f"ce_report: {error}")
    result = {}
    if unchecked and not payload.get("stop_hook_active") and due(payload, "check-before-stop"):
        result["hookSpecificOutput"] = {"hookEventName": "Stop", "additionalContext": CHECK_HINT}
    if errors:
        result["systemMessage"] = "ce-shared Stop: " + " | ".join(errors)
    return result


def main() -> int:
    utf8_io()
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    event = payload.get("hook_event_name") or ("Stop" if payload.get("transcript_path") and "stop_hook_active" in payload else "SessionStart")
    try:
        if event == "Stop":
            result = stop(payload)
        elif event == "PostToolUse":
            result = pulse(payload)
        else:
            result = session_start(payload)
    except Exception as error:  # noqa: BLE001
        result = {"systemMessage": f"ce-shared session hook: {error}"}
        if event not in ("Stop", "PostToolUse"):  # 0.36: the model does not see a systemMessage; the core survives any other part
            result["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": kern_block(Path(str(payload.get("cwd") or ".")))}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
