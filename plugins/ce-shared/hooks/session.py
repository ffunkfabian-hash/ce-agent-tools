#!/usr/bin/env python3
"""SessionStart and Stop hook of ce-shared (0.5.0): the rules that a session must see at the start and the measurement at the end.

SessionStart (additionalContext): the repo's docs/LESSONS.md (or Docs/), the session-name proposal of ce_session_name.py, and
one line naming the tools every session is expected to use (ce_search, heavy_lock --resource, session_name --reserve,
./check, pruefung.md). Nothing is installed.
Stop: runs ce_report.py on the transcript, appends its line to report.md in the worktree (untracked) and warns (systemMessage)
when the diff touches code/assets but no ./check or test ran in this session (check-before-stop), or when a long doc was read
without any search first. It never blocks; any error lets the session end.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
TOOLS = PLUGIN_ROOT / "tools"
TIMEOUT = 15
TOOL_LINE = ("Werkzeuge dieser Sitzung (Pflicht, globale Regeln): `ce_search` (MCP, vor dem Lesen langer Docs) · "
             "`session_name.py --name <Name> --reserve` vor der ersten Änderung (Patch: Branch claude/<repo>-patch-<kurzname>) · "
             "`heavy_lock --resource unity|blender|gpu` für schwere Läufe · `./check` vor Commit · Richter mit `templates/pruefung.md` · "
             "Bericht = PR-Beschreibung, Ist-Zeit als Warten/Rechnen/Lesen/Umsetzen.")


def run(argv, cwd=None, timeout=TIMEOUT):
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def lessons(root: Path) -> str:
    for candidate in (root / "docs" / "LESSONS.md", root / "Docs" / "LESSONS.md"):
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8", errors="replace")
            return f"===== {candidate.relative_to(root)} (Lehren, immer geladen) =====\n{text[:12000]}"
    return ""


def session_start(payload: dict) -> dict:
    root = Path(payload.get("cwd") or os.getcwd())
    parts = [TOOL_LINE]
    proposal = run([sys.executable, str(TOOLS / "ce_session_name.py"), "--task", "Auftrag", "--offline"], cwd=root)
    if proposal:
        parts.append("Sitzungsname (Vorschlag ohne Version; mit --name <Name> --reserve für eine Version):\n" + proposal)
    text = lessons(root)
    if text:
        parts.append(text)
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n\n".join(parts)}}


def changed_code(root: Path) -> bool:
    out = run(["git", "diff", "--name-only", "HEAD"], cwd=root) + "\n" + run(["git", "ls-files", "--others", "--exclude-standard"], cwd=root)
    return any(line.strip().endswith((".cs", ".py", ".sh", ".json", ".blend", ".unity", ".prefab", ".asset")) for line in out.splitlines())


def stop(payload: dict) -> dict:
    root = Path(payload.get("cwd") or os.getcwd())
    transcript = payload.get("transcript_path")
    warnings = []
    if transcript and Path(transcript).is_file():
        sys.path.insert(0, str(TOOLS))
        try:
            import ce_report  # type: ignore
            info = ce_report.analyse(Path(transcript))
            line = ce_report.one_line(info)
            warnings += ce_report.hints(info)
            if changed_code(root) and info["checks"] == 0:
                warnings.append("Code/Assets geändert, aber kein ./check oder Testlauf in dieser Sitzung (check-before-stop, DoD 2)")
            report = root / "report.md"
            try:
                with open(report, "a", encoding="utf-8") as handle:
                    handle.write("\n" + line + "\n" + "".join("  Hinweis: " + w + "\n" for w in warnings))
            except OSError:
                pass
        except Exception as error:  # noqa: BLE001 - never wedge the session end
            warnings.append(f"ce_report: {error}")
    if warnings:
        return {"systemMessage": "ce-shared Stop: " + " | ".join(warnings)}
    return {}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    event = payload.get("hook_event_name") or ("Stop" if payload.get("transcript_path") and "stop_hook_active" in payload else "SessionStart")
    try:
        result = stop(payload) if event == "Stop" else session_start(payload)
    except Exception as error:  # noqa: BLE001
        result = {"systemMessage": f"ce-shared session hook: {error}"}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
