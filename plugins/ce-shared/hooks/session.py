#!/usr/bin/env python3
"""SessionStart, PostToolUse and Stop hook of ce-shared: what a session must see at the start, while it works and at the end.

SessionStart (additionalContext): the repo's docs/LESSONS.md (or Docs/) - unless the repo's CLAUDE.md already imports
it (0.33: it came twice in 7 repos, in Cube Escape 8,950 characters per session) - the session-name proposal of
ce_session_name.py, and one line naming the tools every session is expected to use (ce_search, heavy_lock --resource,
session_name --reserve, ./check, pruefung.md). Nothing is installed. Since 0.16 it also compares the loaded ce-shared
version with the newest this machine knows and warns visibly when the session runs an older one.
PostToolUse (0.33 Earshot, `pulse`): every PULSE_EVERY tool calls a cheap look at the transcript - record count and the
input of the last request - and, crossing a limit, one hint as additionalContext for the model: over 300 records
(Trimline: "über 300 Nachrichten" came 1,177 times as a Stop warning that only the user saw), over 150,000 input tokens.
Each hint once per session and threshold step (hint_state).
Stop: runs ce_report.py on the transcript and appends its line and hints to report.md in the worktree (untracked) -
the measurement; no systemMessage any more (0.33: those warnings reached only the user). check-before-stop - code or
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
CLAUDE_FILES = ("CLAUDE.md", "CLAUDE.local.md", os.path.join(".claude", "CLAUDE.md"))
IMPORT = re.compile(r"^\s*@(\S+)", re.MULTILINE)
LESSONS_IMPORT = re.compile(r"^(\./)?[Dd]ocs/LESSONS\.md$")
PULSE_EVERY = 20              # PostToolUse: look at the transcript every 20th tool call
MESSAGE_LIMIT = int(os.environ.get("CE_MESSAGE_LIMIT") or 300)  # records (user + assistant) per session (› Sitzungen kurz halten); the variable is a probe aid
INPUT_LIMIT = 150_000         # input tokens of one request (ce_report.MAX_CONTEXT_TOKENS)
TAIL_BYTES = 512 * 1024       # the last requests of the transcript are enough for the current input
RECORD_MARKS = (b'"type":"user"', b'"type":"assistant"')
USAGE = re.compile(rb'"usage":\{([^{}]*)\}')
TOKEN = re.compile(rb'"(input_tokens|cache_creation_input_tokens|cache_read_input_tokens)":(\d+)')
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


def lessons_imported(root: Path) -> bool:
    """True when the repo's CLAUDE.md (or CLAUDE.local.md, .claude/CLAUDE.md) imports docs/LESSONS.md itself or
    through a file it imports (one level, e.g. AGENTS.md) - then the file is in the start context already."""
    for name in CLAUDE_FILES:
        first = imports(root / name)
        if any(LESSONS_IMPORT.match(entry) for entry in first):
            return True
        for entry in first:
            if not entry.startswith(("/", "~")) and any(LESSONS_IMPORT.match(e) for e in imports(root / entry)):
                return True
    return False


def lessons(root: Path) -> str:
    if lessons_imported(root):
        return ""
    for candidate in (root / "docs" / "LESSONS.md", root / "Docs" / "LESSONS.md"):
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8", errors="replace")
            return f"===== {candidate.relative_to(root)} (Lehren, immer geladen) =====\n{text[:12000]}"
    return ""


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
    """(records, input tokens of the last request) of a transcript - a byte scan, no JSON parsing: the record count is
    the number of user/assistant lines (as ce_report counts them), the input the sum of input, cache-creation and
    cache-read tokens of the last usage block in the tail of the file. (0, 0) when unreadable."""
    try:
        size = path.stat().st_size
        records = 0
        with open(path, "rb") as handle:
            for line in handle:
                if any(mark in line for mark in RECORD_MARKS):
                    records += 1
            handle.seek(max(0, size - TAIL_BYTES))
            tail = handle.read()
    except OSError:
        return 0, 0
    last_input = 0
    for match in USAGE.finditer(tail):
        tokens = {name.decode(): int(value) for name, value in TOKEN.findall(match.group(1))}
        if tokens:
            last_input = sum(tokens.values())
    return records, last_input


def due(payload: dict, key: str, every: int = 0) -> bool:
    if hint_state is None:
        return True
    return hint_state.due(str(payload.get("session_id") or ""), key, every)


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
    if last_input > INPUT_LIMIT and due(payload, "input>limit"):
        hints.append(f"Letzte Anfrage {last_input:,} Input-Tokens (> {INPUT_LIMIT:,}): Kontext ist groß – Etappe "
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
            hints = ce_report.hints(info)
            unchecked = changed_code(root) and info["checks"] == 0
            if unchecked:
                hints.append("Code/Assets geändert, aber kein ./check oder Testlauf in dieser Sitzung (check-before-stop, DoD 2)")
            report = root / "report.md"
            try:
                with open(report, "a", encoding="utf-8") as handle:
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
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
