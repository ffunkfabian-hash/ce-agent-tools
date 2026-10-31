#!/usr/bin/env python3
"""Stop hooks of ce-shared (0.15): the Postfach watcher starts by itself, so a reserved session hears its messages.

`watch` (Stop, async + asyncRewake, timeout = MAX_HOURS): when the worktree is reserved (`<git-dir>/ce-session.json`, written by
ce_session_name.py --reserve; the worktree counts, not the checked-out branch; at most RESERVATION_DAYS old) and no watcher runs, it waits in the background for a comment in
cubeescape-nexogen#16 addressed to the session (ce_await_mail rules), reading from the last handled message or the reservation time.
A message ends it with exit 2 and the comment on stderr – Claude Code wakes the session with it. The message is only `pending` until
the next Stop finds "Kommentar <id>" in the transcript (proof of delivery); without proof it comes once more, then counts as handled.
It ends quietly when nothing comes, when the process that started it is gone (then nothing is marked), on SIGTERM, or on any error.
`remind` (Stop, synchronous): the safety net if a client does not run async hooks. A reserved worktree without a live watcher holds
the stop once (`decision: block`) with the exact command – at most once every REMIND_EVERY_H hours, never without `gh` and not after
a recent GitHub failure, so a session that cannot listen is not woken again and again. `stop_hook_active` ends a loop in one round.
Nothing happens without a reservation or with CE_WATCH=0; every error lets the session stop normally.
0.21: a message that hooks/postfach_inbox.py already handed to the working session (`hinted`) does not wake it again; a release
message gets its check line (ce_await_mail.release_note) in front.
"""
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(PLUGIN_ROOT / "tools"))

MAX_HOURS = 12            # keep equal to the timeout of the async entry in hooks.json (43200 s)
REMIND_EVERY_H = 2
GRACE_S = 5               # remind waits this long for the async watcher of the same Stop to register
INTERVAL_S = 60

try:
    import ce_await_mail as am  # noqa: E402  (tools folder of the plugin, see sys.path above)
except Exception:  # noqa: BLE001 - a broken install must not hold the session
    am = None


def windows_parent(state={}, lock=None) -> int:
    """(0.31) os.getppid for Windows: there a dead parent keeps its pid in getppid (no re-parenting to 1), so the watcher
    would never notice that its session ended. 1 once the first parent is gone or its pid belongs to a newer process."""
    pid = os.getppid()
    try:
        lock = lock or __import__("heavy_lock")
        started = lock.process_started(pid)
        first = state.setdefault(pid, started)
        return pid if lock.pid_alive(pid) and started == first else 1
    except Exception:  # noqa: BLE001 - without the check it behaves like before
        return pid


PARENT = windows_parent if os.name == "nt" else os.getppid


class SessionGone(Exception):
    """The process that started the hook ended: nobody is left to wake."""


def recipient(name: str) -> str:
    return name.split("·")[0].strip()


def start_command(name: str) -> str:
    return f'./ce session mail --name "{recipient(name)}"'


def remind(payload: dict, sleep=time.sleep, now=time.time, which=shutil.which) -> dict:
    if payload.get("stop_hook_active"):
        return {}
    folder, name = am.reserved(payload.get("cwd") or os.getcwd())
    if folder is None:
        return {}
    am.settle(folder, payload.get("transcript_path") or "")
    for _ in range(GRACE_S * 2):
        if am.watcher_pid(folder):
            return {}
        sleep(0.5)
    state = am.load_state(folder)
    recent = REMIND_EVERY_H * 3600
    if which("gh") is None or now() - float(state.get("reminded_at") or 0) < recent \
            or now() - float(state.get("last_failure") or 0) < recent:
        return {}
    am.save_state(folder, reminded_at=now())
    return {"decision": "block",
            "reason": f"Diese Sitzung ist reserviert ({recipient(name)}), aber kein Postfach-Wächter läuft – Nachrichten anderer "
                      f"Sitzungen kämen nicht an. Starte ihn im Hintergrund (run_in_background) und beende dann: "
                      f"{start_command(name)} (liest ab der letzten behandelten Nachricht; nächste Erinnerung frühestens in "
                      f"{REMIND_EVERY_H} h)."}


def hinted(folder: Path, number: int) -> bool:
    """0.21: the PostToolUse inbox already put this message into the running session (`hinted` in ce-watch.json, read fresh)."""
    return number <= am.state_int(am.load_state(folder), "hinted")


def watch(payload: dict, fetch=None, sleep=time.sleep, clock=time.monotonic, parent=PARENT, err=sys.stderr) -> int:
    folder, session = am.reservation(payload.get("cwd") or os.getcwd())
    if folder is None:
        return 0
    after = am.settle(folder, payload.get("transcript_path") or "")
    if not am.register(folder):
        return 0
    previous = signal.signal(signal.SIGTERM, lambda *_: (am.unregister(folder), os._exit(0)))
    try:
        state = am.load_state(folder)
        pending = am.state_int(state, "pending")
        since = am.since_for(folder, session)
        first_parent = parent()

        def guarded_sleep(seconds):
            if parent() != first_parent or not am.still_reserved(folder, session["name"]):  # 0.21: reserved anew -> not ours
                raise SessionGone()
            sleep(seconds)

        fetch = fetch or (lambda since_: am.fetch_comments(am.REPO, [am.MAILBOX], since_))
        try:
            code, result = am.watch(fetch, am.session_key(session["name"]), after, since, INTERVAL_S, MAX_HOURS * 3600,
                                    sleep=guarded_sleep, clock=clock, skip=lambda number: hinted(folder, number))
        except SessionGone:
            return 0
        if code == 3:
            am.save_state(folder, last_failure=time.time())
        if code != 0 or parent() != first_parent or not am.still_reserved(folder, session["name"]):  # nobody to wake: leave it unmarked
            return 0
        deliveries = am.state_int(state, "deliveries") + 1 if result["id"] == pending else 1
        saved = am.save_state(folder, pending=result["id"], pending_since=result.get("created_at", since), deliveries=deliveries)
        if not saved or am.state_int(am.load_state(folder), "pending") != result["id"]:
            am.save_state(folder, last_failure=time.time())  # without a record every Stop would wake again: stay quiet instead
            return 0
        if deliveries > 1:
            print(am.REDELIVERY.format(id=result["id"]), file=err)
        print(f"Postfach (cubeescape-nexogen#{result.get('pr', am.MAILBOX)}), Kommentar {result['id']} ({result.get('created_at', '')}):\n",
              file=err)
        note = am.release_note(result.get("body", ""), am.session_key(session["name"]))
        if note:
            print(note + "\n", file=err)
        print(result.get("body", ""), file=err)
        print("\nDer Wächter startet beim nächsten Stopp von selbst neu (ce-shared 0.15).", file=err)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous)
        am.unregister(folder)


def utf8_io() -> None:
    """(0.31) Windows: the client's JSON and the wake text on stderr as UTF-8, not cp1252 („Salvo“ → …)."""
    try:
        import ce_common
        ce_common.utf8_console(sys.stdin, sys.stdout, sys.stderr)
    except Exception:  # noqa: BLE001 - fail-open
        pass


def main(argv=None) -> int:
    mode = (argv or sys.argv[1:] or ["remind"])[0]
    utf8_io()
    if am is None:
        if mode != "watch":
            print("{}")
        return 0
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        if mode == "watch":
            return watch(payload)
        print(json.dumps(remind(payload), ensure_ascii=False))
    except Exception as error:  # noqa: BLE001 - never wedge the session end
        if mode != "watch":
            print(json.dumps({"systemMessage": f"ce-shared Postfach-Wächter: {error}"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
