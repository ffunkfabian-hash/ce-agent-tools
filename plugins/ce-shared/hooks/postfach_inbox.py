#!/usr/bin/env python3
"""Postfach during work (ce-shared 0.21): a reserved session hears its messages between two tool calls, not only when it stops.

Before 0.21 the Stop hook started the watcher, so a session that kept working heard nothing: Sightline's 6370961598 came 17:37 UTC,
one minute after Courier's reservation, and stayed unread until the user pasted it into the chat.

`hint` (Claude Code PostToolUse; Codex through codex_guard.py): reads two small files in the worktree's git dir and prints nothing
when there is no new message – no network, no context, no cost. At most every EVERY_S seconds (CE_INBOX_EVERY) it starts `fetch` as a
detached process and returns at once; the next tool call after that finds what it wrote. A new message goes into the session as
additionalContext ("Postfach … Kommentar <id>") and counts as `pending` like a wake of the Stop watcher: the next Stop settles it
against the transcript, and the Stop watcher (also one that already runs) skips what was `hinted` (ce_await_mail.watch(skip=…)).
`fetch <git dir>` (detached): one read of cubeescape-nexogen#16 from the last handled message on, the addressed new comments into
`ce-inbox.json`, bound to the reserved name (another name in a reused worktree finds nothing); one fetch at a time (`ce-inbox.lock`),
a failure is written and waits for the next interval. A body over MAX_BODY characters is cut visibly, with the link to the full text.
Nothing happens without a reservation, without `gh`, or with CE_WATCH=0 or CE_INBOX=0; every error prints nothing (fail-open).
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(PLUGIN_ROOT / "tools"))

INBOX_FILE = "ce-inbox.json"
LOCK_FILE = "ce-inbox.lock"
EVERY_S = 180            # one Postfach read every three minutes at most while the session works
LOCK_STALE_S = 120       # a fetch older than this is dead (gh has a 60 s timeout)
MAX_MESSAGES = 5
MAX_BODY = 4000

try:
    import ce_await_mail as am  # noqa: E402
except Exception:  # noqa: BLE001 - a broken install must not disturb the tool call
    am = None


def every() -> float:
    try:
        return max(30.0, float(os.environ.get("CE_INBOX_EVERY") or EVERY_S))
    except ValueError:
        return EVERY_S


def handled(state: dict) -> int:
    return max(am.state_int(state, "after"), am.state_int(state, "hinted"), am.state_int(state, "pending"))


def write_json(path: Path, value: dict) -> bool:
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
        return True
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        return False


def start_fetch(folder: Path) -> None:
    argv = [sys.executable, str(Path(__file__).resolve()), "fetch", str(folder)]
    if os.name == "nt":
        start_fetch_windows(argv)
        return
    subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def start_fetch_windows(argv: list, popen=subprocess.Popen) -> None:
    """(0.31) Windows has no sessions (start_new_session is ignored there): the read runs detached from the hook's console
    and process group, and out of its job where the client's job allows that - else inside it, as the job permits."""
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    for extra in (0x01000000, 0):  # CREATE_BREAKAWAY_FROM_JOB; refused (access denied) in a job without breakaway
        try:
            popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  creationflags=flags | extra, close_fds=True)
            return
        except OSError:
            if not extra:
                raise


def utf8_io() -> None:
    """(0.31) Windows: the client's JSON comes and goes as UTF-8, not cp1252 (a cwd with Ö, a → in a message)."""
    try:
        import ce_common
        ce_common.utf8_console(sys.stdin, sys.stdout, sys.stderr)
    except Exception:  # noqa: BLE001 - fail-open
        pass


def text_of(messages: list, key: str) -> str:
    parts = ["Postfach (cubeescape-nexogen#16) – neue Nachricht an diese Sitzung während der Arbeit (ce-shared 0.21):"]
    for item in messages:
        parts.append(f"\nKommentar {item['id']} ({item.get('created_at', '')}):")
        note = item["note"] if isinstance(item.get("note"), str) else am.release_note(item.get("body", ""), key)  # from the full text
        if note:
            parts.append(note)
        parts.append(item.get("body", "").strip())
        if item.get("truncated"):
            parts.append(f"[gekürzt auf {MAX_BODY} Zeichen – Volltext: {item.get('url') or 'cubeescape-nexogen#16'}]")
    parts.append("\nLesen und, wo nötig, nach dem laufenden Schritt darauf eingehen; Freigaben nur nach den Regeln.")
    return "\n".join(parts)


def pending_text(cwd: str, now=time.time, spawn=start_fetch, which=shutil.which) -> str:
    """The context text of new messages for this worktree ("" if none); starts a background read when the last one is old."""
    if am is None or os.environ.get("CE_INBOX", "1") == "0":
        return ""
    folder, session = am.reservation(cwd, fast=True)
    if folder is None:
        return ""
    inbox = am.read_json(folder / INBOX_FILE)
    if inbox.get("name") != session["name"]:  # Richter 0.21, Nr 1: a reused worktree must not hand over the previous session's mail
        inbox = {"checked_at": 0}
    key = am.session_key(session["name"])
    state = am.load_state(folder)
    done = handled(state)
    messages = [item for item in inbox.get("messages", []) if isinstance(item, dict) and am.state_int(item, "id") > done
                and am.addressed(str(item.get("body", "")), key)]
    try:
        checked = float(inbox.get("checked_at") or 0)
    except (TypeError, ValueError):
        checked = 0.0
    if now() - checked >= every() and which("gh") is not None:
        if write_json(folder / INBOX_FILE, dict(inbox, name=session["name"], checked_at=now())):  # claim the interval first: parallel tool calls start one read
            spawn(folder)
    if not messages:
        return ""
    top = max(messages, key=lambda item: item["id"])
    open_ids = sorted({number for number in [am.state_int(state, "pending"), *(state.get("pending_ids") or [])]
                       if isinstance(number, int) and number > am.state_int(state, "after")} | {item["id"] for item in messages})
    if not am.save_state(folder, hinted=top["id"], pending=open_ids[0], pending_ids=open_ids,
                         pending_since=state.get("pending_since") or top.get("created_at"),
                         deliveries=am.state_int(state, "deliveries") or 1):
        return ""  # without a record the same message would come on every tool call
    return text_of(sorted(messages, key=lambda item: item["id"]), key)


def hint(payload: dict, **kwargs) -> dict:
    text = pending_text(payload.get("cwd") or os.getcwd(), **kwargs)
    if not text:
        return {}
    first = text.split("Kommentar ", 1)[1].split(" ", 1)[0] if "Kommentar " in text else ""
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text},
            "systemMessage": f"Postfach: neue Nachricht (Kommentar {first}) in die Sitzung gegeben"}


def lock(folder: Path, now=time.time) -> bool:
    path = folder / LOCK_FILE
    for _ in range(2):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                if now() - path.stat().st_mtime < LOCK_STALE_S:
                    return False
                path.unlink()
            except OSError:
                return False
            continue
        except OSError:
            return False
        with os.fdopen(handle, "w") as out:
            out.write(str(os.getpid()))
        return True
    return False


def fetch(folder: Path, read=None, now=time.time) -> int:
    """One read of the Postfach for the reserved session of this git dir; writes ce-inbox.json. Always 0."""
    session = am.read_json(folder / am.SESSION_FILE)
    if not session.get("name") or not lock(folder, now):
        return 0
    try:
        state = am.load_state(folder)
        reserved_at = session.get("reserved_at")
        if not isinstance(reserved_at, (int, float)):
            try:
                reserved_at = (folder / am.SESSION_FILE).stat().st_mtime
            except OSError:
                reserved_at = now()
        since = am.since_for(folder, dict(session, reserved_at=reserved_at))
        key, done = am.session_key(session["name"]), handled(state)
        read = read or (lambda since_: am.fetch_comments(am.REPO, [am.MAILBOX], since_))
        try:
            comments = read(since)
        except Exception as error:  # noqa: BLE001 - written down, tried again after the interval
            write_json(folder / INBOX_FILE, {"name": session["name"], "checked_at": now(), "error": str(error)[:300]})
            return 0
        found = [dict(id=item["id"], created_at=item.get("created_at", ""), url=item.get("html_url", ""),
                      body=(item.get("body") or "")[:MAX_BODY], truncated=len(item.get("body") or "") > MAX_BODY,
                      note=am.release_note(item.get("body") or "", key))  # checked on the full text, before the cut
                 for item in sorted(comments, key=lambda c: c["id"])
                 if item["id"] > done and item.get("created_at", "") >= since and am.addressed(item.get("body", ""), key)]
        if am.read_json(folder / am.SESSION_FILE).get("name") != session["name"]:
            return 0  # reserved anew while reading: the result belongs to nobody
        write_json(folder / INBOX_FILE, {"name": session["name"], "checked_at": now(), "messages": found[:MAX_MESSAGES]})
    finally:
        try:
            (folder / LOCK_FILE).unlink()
        except OSError:
            pass
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else "hint"
    utf8_io()
    try:
        if mode == "fetch" and am is not None and len(argv) > 1:
            return fetch(Path(argv[1]))
        if mode != "hint":
            return 0
        try:
            payload = json.load(sys.stdin)
        except ValueError:
            payload = {}
        result = hint(payload if isinstance(payload, dict) else {})
        if result:
            print(json.dumps(result, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - never disturb a tool call
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
