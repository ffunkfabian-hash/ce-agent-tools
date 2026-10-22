#!/usr/bin/env python3
"""Waits for a Briefkasten comment addressed to this session and prints it (regeln-details.md › Briefkasten, Wächter).

Start it in the background when the session stops for the user (before a merge, with a question):
    python3 ~/.claude/templates/await_mail.py --name "TG 0.19 „Kinetic“ · Mac"              # reads the Postfach (issue 16)
    python3 ~/.claude/templates/await_mail.py --name "…" --issue 16 3                        # plus an old Briefkasten PR
Claude Code: Bash with run_in_background; the session wakes when the script exits. Exit 0 = message printed (read it, act on it),
exit 2 = timeout without message (start again silently with the same arguments), exit 3 = GitHub unreachable several times in a row (report once).

A comment counts as addressed when a line holds `→ <recipient>` and the recipient names this session: same version and name
(`0.19 „Kinetic“`), regardless of repo kürzel, quote style and place; `→ alle:` reaches every session but the sender (0.14). Considered are comments from `--lookback` minutes before the start on (a reply
that came while the session was still busy is not lost); after handling message N, start again with `--after N`.
The script only reads; what a message may trigger is decided by the rules, not here.
0.15: without `--name` the name comes from the reservation (`<git-dir>/ce-session.json`, ce_session_name.py --reserve). A watcher for the
reserved session registers in `<git-dir>/ce-watch.pid` (atomic, stale files taken over) and records the handled comment in
`ce-watch.json`, shared with the Stop hook of ce-shared (hooks/postfach_watch.py) that starts the watcher by itself. It reads from the
last handled message or the reservation time, not only the look-back. Exit 4 = a watcher for this session already runs,
exit 5 = no --name and no reservation.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = "ffunkfabian-hash/cubeescape-nexogen"
MAILBOX = 16  # 1.15: the one Postfach issue (before: Briefkasten PR 1 = Sightline, one PR per recipient)
QUOTED = re.compile(r"(\d+\.\d+(?:\.\d+)?)\s*[„“\"']([^“”\"']+)[“”\"']")
RECIPIENT = re.compile(r"[\s*]*(?:[A-Za-zÄÖÜäöü-]+\s+)?" + QUOTED.pattern)


def session_key(name: str) -> str:
    """`0.19 kinetic` for `TG 0.19 „Kinetic“ · Mac`; without version and quoted name the whole name (lower case, without place)."""
    match = QUOTED.search(name)
    if match:
        return f"{match.group(1)} {match.group(2).strip().lower()}"
    return name.split("·")[0].strip().lower()


def normalize(text: str) -> str:
    """Spelling-proof form of a name or key: lower case, umlauts spelt out, hyphens as spaces (0.14: `Cross-Talk` = `Cross Talk`,
    `Über` = `ueber`), so a name derived from a branch slug reaches the same watcher."""
    text = text.lower().translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "-": " ", "_": " "}))
    return re.sub(r"\s+", " ", text).strip()


def same_name(recipient: str, key: str) -> bool:
    """A recipient without version is the whole name: place (`· Mac`) and a note in brackets aside, `CE Patch laser` is not
    `CE Patch laser-seeder` (0.14, word boundary instead of prefix)."""
    return normalize(re.sub(r"\(.*?\)", "", recipient.split("·")[0])) == key


def addressed(body: str, key: str) -> bool:
    """True when a line of the comment sends something to the session with this key (`… → [Kürzel] <Version> „<Name>“ …`)."""
    key = normalize(key if QUOTED.search(key) else re.sub(r"\(.*?\)", "", key))  # a note in brackets belongs to neither side
    lines = body.splitlines()
    first = next((index for index, line in enumerate(lines) if line.strip()), -1)
    for index, line in enumerate(lines):
        if "→" not in line:
            continue
        sender, rest = line.split("→", 1)
        recipient = rest.split(":", 1)[0]
        if index == first and normalize(recipient.strip(" *")) == "alle" and ":" in rest and sender.strip(" *"):
            if not addressed(f"→ {sender.strip(' *')}:", key):
                return True  # 0.14: a message to everybody (head line only), except the sender's own watcher
            continue
        found = RECIPIENT.match(recipient)
        if found and normalize(f"{found.group(1)} {found.group(2).strip()}") == key:
            return True
        if not QUOTED.search(key) and same_name(recipient.strip(" *"), key):
            return True
    return False


PID_FILE = "ce-watch.pid"
STATE_FILE = "ce-watch.json"
SESSION_FILE = "ce-session.json"
RESERVATION_DAYS = 7      # an older reservation no longer makes a worktree listen (a later, unrelated session on the same branch)
LOOKBACK_MIN = 30         # default look-back when neither a handled message nor a reservation time is known
REDELIVER = 1             # a message without proof of delivery comes once more, then counts as handled
REDELIVERY = "Erneute Zustellung von Kommentar {id} – nur handeln, falls noch nicht behandelt."


def git_dir(cwd: str):
    """(absolute git dir of the worktree, current branch), or (None, "")."""
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--absolute-git-dir"], capture_output=True, text=True, timeout=10)
        branch = subprocess.run(["git", "-C", cwd, "branch", "--show-current"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    if out.returncode or not out.stdout.strip():
        return None, ""
    return Path(out.stdout.strip()), branch.stdout.strip()


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def reservation(cwd: str):
    """(git dir, session dict) when the worktree's current branch is the reserved one and the reservation is recent, else (None, {}).
    CE_WATCH=0 switches the watcher off."""
    if os.environ.get("CE_WATCH", "1") == "0":
        return None, {}
    folder, branch = git_dir(cwd)
    if folder is None:
        return None, {}
    session = read_json(folder / SESSION_FILE)
    if not session.get("name") or session.get("branch") != branch:
        return None, {}
    reserved_at = session.get("reserved_at")
    if isinstance(reserved_at, (int, float)) and time.time() - reserved_at > RESERVATION_DAYS * 86400:
        return None, {}
    return folder, session


def reserved(cwd: str):
    """(git dir, session name) of a current reservation, else (None, "")."""
    folder, session = reservation(cwd)
    return folder, session.get("name", "")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a reused pid of another program does not count (left-over file after SIGKILL or a restart)
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return True
    return "ce_await_mail" in command or "postfach_watch" in command  # empty: the process just ended


def watcher_pid(folder: Path) -> int:
    """The pid of a running watcher for this worktree (Stop hook or this script), else 0."""
    try:
        pid = int((folder / PID_FILE).read_text().strip())
    except (OSError, ValueError):
        return 0
    return pid if pid > 0 and alive(pid) else 0


def load_state(folder: Path) -> dict:
    return read_json(folder / STATE_FILE)


def state_int(state: dict, key: str) -> int:
    try:
        return int(state.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def save_state(folder: Path, **values) -> bool:
    """Atomic update of `ce-watch.json` (temp file + os.replace, so a concurrent reader never sees half a file); False if it failed."""
    state = load_state(folder)
    state.update(values)
    state = {key: value for key, value in state.items() if value is not None}
    temp = folder / f"{STATE_FILE}.{os.getpid()}.tmp"
    try:
        temp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temp, folder / STATE_FILE)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        return False
    return True


def register(folder: Path) -> bool:
    """Claims the worktree's watcher slot atomically; a stale file (dead or foreign pid) is taken over once."""
    for _ in range(2):
        try:
            handle = os.open(folder / PID_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                seen = (folder / PID_FILE).read_text().strip()
            except OSError:
                continue
            if watcher_pid(folder):
                return False
            aside = folder / f"{PID_FILE}.{os.getpid()}.stale"
            try:  # move the stale file aside; if another starter replaced it meanwhile, put theirs back
                os.rename(folder / PID_FILE, aside)
                if aside.read_text().strip() != seen:
                    os.rename(aside, folder / PID_FILE)
                    return False
                aside.unlink()
            except OSError:
                return False
            continue
        except OSError:
            return False
        with os.fdopen(handle, "w") as out:
            out.write(str(os.getpid()))
        return True
    return False


def unregister(folder: Path) -> None:
    try:
        if (folder / PID_FILE).read_text().strip() == str(os.getpid()):
            (folder / PID_FILE).unlink()
    except OSError:
        pass


def since_for(folder: Path, session: dict, lookback_min: float = LOOKBACK_MIN) -> str:
    """Where a new watcher starts reading: after the last handled message, else from the reservation, else the look-back."""
    state = load_state(folder)
    if isinstance(state.get("since"), str):
        return state["since"]
    reserved_at = session.get("reserved_at")
    start = reserved_at if isinstance(reserved_at, (int, float)) else time.time() - lookback_min * 60
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(min(start, time.time() - lookback_min * 60)))


def settle(folder: Path, transcript: str = "") -> int:
    """A message the Stop hook woke the session with (`pending`) counts as handled once the transcript shows it, or after REDELIVER
    more deliveries; returns the id from which the next watcher reads."""
    state = load_state(folder)
    pending, after = state_int(state, "pending"), state_int(state, "after")
    if not pending:
        return after
    seen = False
    if transcript:
        try:
            seen = f"Kommentar {pending}" in Path(transcript).read_text(encoding="utf-8", errors="replace")
        except OSError:
            seen = False
    if seen or state_int(state, "deliveries") > REDELIVER:
        save_state(folder, after=max(after, pending), since=state.get("pending_since") or state.get("since"),
                   pending=None, pending_since=None, deliveries=None)
        return max(after, pending)
    return after


def fetch_comments(repo: str, numbers: list, since: str) -> list:
    comments = []
    for number in numbers:
        for item in fetch_page(repo, number, since):
            comments.append(dict(item, pr=number))
    return comments


def fetch_page(repo: str, number: int, since: str) -> list:
    query = f"repos/{repo}/issues/{number}/comments?per_page=100&since={since}"
    result = subprocess.run(["gh", "api", "--paginate", query], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "gh api failed")
    text = result.stdout.replace("][", ",")  # --paginate prints one array per page
    return json.loads(text or "[]")


def watch(fetch, key: str, start_id: int, since: str, interval: float, timeout: float, sleep=time.sleep, clock=time.monotonic):
    """Returns (exit code, comment or message). `fetch(since)` returns comment dicts with id, created_at, body."""
    deadline = clock() + timeout
    failures = 0
    while True:
        try:
            comments = fetch(since)
            failures = 0
        except Exception as error:  # network or gh problems must not end the watch at once
            failures += 1
            if failures >= 5:
                return 3, f"GitHub not reachable ({error})"
            comments = []
        for comment in sorted(comments, key=lambda c: c["id"]):
            new = comment["id"] > start_id and comment.get("created_at", "") >= since
            if new and addressed(comment.get("body", ""), key):
                return 0, comment
        if clock() >= deadline:
            return 2, "no message"
        sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", default="", help="own session name, e.g. 'TG 0.19 „Kinetic“ · Mac'; default: the reservation of this worktree")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--issue", "--pr", dest="pr", type=int, nargs="+", default=[MAILBOX],
                        help="issue or PR numbers to read (default 16 = the Postfach; PRs and issues share the comments endpoint)")
    parser.add_argument("--after", type=int, default=0, help="only comments with a larger id (the last message already handled)")
    parser.add_argument("--lookback", type=float, default=30, help="minutes before the start that still count (default 30)")
    parser.add_argument("--interval", type=float, default=60, help="seconds between reads")
    parser.add_argument("--timeout", type=float, default=110, help="minutes; below the 2 h limit of background commands")
    args = parser.parse_args()

    folder, session = reservation(os.getcwd())
    reserved_name = session.get("name", "")
    name = args.name or reserved_name
    if not name:
        print("await_mail: --name fehlt und dieser Worktree ist nicht reserviert (ce_session_name.py --reserve) - exit 5", file=sys.stderr)
        return 5
    key = session_key(name)
    own = folder is not None and normalize(session_key(reserved_name)) == normalize(key)
    after = args.after
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - args.lookback * 60))
    if folder is not None and not own:
        print(f"await_mail: Hinweis – dieser Worktree ist als {reserved_name!r} reserviert, gewartet wird auf {key!r} (kein gemeinsamer Stand)",
              file=sys.stderr)
    if own:
        after = max(after, settle(folder))
        since = min(since, since_for(folder, session, args.lookback))
        if not register(folder):
            print(f"await_mail: für {key!r} läuft schon ein Wächter (pid {watcher_pid(folder)}, Stop-Hook oder eigener Start) - nichts zu tun (exit 4)",
                  file=sys.stderr)
            return 4
    try:
        code, result = watch(lambda since: fetch_comments(args.repo, args.pr, since), key, after, since,
                             args.interval, args.timeout * 60)
    finally:
        if own:
            unregister(folder)
    redelivered = code == 0 and own and result["id"] == state_int(load_state(folder), "pending")
    if own:
        if code == 0:
            save_state(folder, after=result["id"], since=result.get("created_at", since), pending=None, pending_since=None, deliveries=None)
        elif code == 3:
            save_state(folder, last_failure=time.time())
    if redelivered:
        print(REDELIVERY.format(id=result["id"]))
    if code == 0:
        print(f"Briefkasten {args.repo}#{result['pr']}, Kommentar {result['id']} ({result['created_at']}):\n")
        print(result["body"])
    else:
        print(f"await_mail: {result} for {key!r} (exit {code})", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
