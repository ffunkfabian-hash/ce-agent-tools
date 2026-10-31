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
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

WINDOWS = os.name == "nt"
# (0.31) git and gh speak UTF-8; Windows would decode their output as cp1252 (a „“ in a comment is a decode error there)
UTF8 = {"encoding": "utf-8", "errors": "replace"} if WINDOWS else {}
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


RECIPIENT_SEPARATOR = re.compile(r"\s*[,;&+]\s*")  # 0.21: `→ A, B:` reaches A and B (6344404291 reached neither)


def recipients(text: str) -> list:
    """The recipients of a head line's recipient part: `TG 0.23 „Livery“, Sightline` -> both, brackets and bold aside."""
    return [part.strip(" *") for part in RECIPIENT_SEPARATOR.split(text) if part.strip(" *")]


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
        for part in recipients(recipient):
            found = RECIPIENT.match(part)
            if found and normalize(f"{found.group(1)} {found.group(2).strip()}") == key:
                return True
            if not QUOTED.search(key) and same_name(part, key):
                return True
    return False


COORDINATOR = "Sightline"
PR_RELATED = re.compile(r"/pull/\d+|\bPR\s*#?\d+|\bgemergt\b|\bmerg(?:e|en|t)\b", re.I)


def pr_related(text: str) -> bool:
    """0.21: a message about a PR or a merge (6368327242 went to „Plugin-Analyse“ only, the coordinator never heard of PR #19)."""
    return bool(PR_RELATED.search(text or ""))


def sender_of(body: str) -> str:
    first = next((line for line in (body or "").splitlines() if line.strip()), "")
    return first.split("→", 1)[0].strip(" *") if "→" in first else ""


def wanted(body: str, key: str, also_pr: bool = False) -> bool:
    """addressed(), plus with `also_pr` every PR/merge message not sent by this session (the coordinator's watcher)."""
    if addressed(body, key):
        return True
    return also_pr and pr_related(body) and not addressed(f"→ {sender_of(body)}:", key)


# 0.21 Ziel 3 (Entscheidung des Nutzers 23.10.2026, Sightline 6370961598): a user's yes may travel by post only for reversible steps and
# only as Sightline's release message in this fixed form. The sender is the head line's text – an agreement, no cryptographic proof
# (every session posts under the same GitHub account).
RELEASE_MARK = "Freigabe per Post (ce-freigabe 1)"
RELEASE_STEPS = ("commit", "push", "pr")
RELEASE_FORBIDDEN = "Merge, Tag, Release, Force-Push, Löschen"
STEP_WORDS = {"commit": "commit", "committen": "commit", "push": "push", "pushen": "push", "push ohne force": "push",
              "pr": "pr", "pr öffnen": "pr", "pull request": "pr"}
RELEASE_WORDS = re.compile(r"freigabe|freigegeben|freigeben|ja des nutzers|nutzer sagt ja", re.I)
RELEASE_TOPICS = re.compile(r"commit|push|merge|mergen|\bpr\b|release|\btag\b", re.I)
RELEASE_FIELDS = {"ziel": "Ziel", "schritte": "Schritte", "zitat": "Zitat des Nutzers", "zeit": "Zeit (UTC)"}
UTC_FORMATS = ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%MZ", "%Y-%m-%d %H:%M:%SZ")


def utc_time(text: str) -> bool:
    """A real calendar time with the UTC mark `Z` (Richter 0.21, Nr 7: `2026-99-99T88:77Z` and times without zone were accepted)."""
    for form in UTC_FORMATS:
        try:
            datetime.datetime.strptime(text, form)
            return True
        except ValueError:
            continue
    return False


def release_steps(text: str):
    """(steps, unknown words) of a `Schritte:` value, e.g. `commit, push, PR öffnen` -> (["commit", "push", "pr"], [])."""
    steps, unknown = [], []
    for word in (part.strip().lower() for part in re.split(r"[,;/]", text or "")):
        if not word:
            continue
        step = STEP_WORDS.get(word)
        (steps if step else unknown).append(step or word)
    return list(dict.fromkeys(steps)), unknown


def release_lines(recipient: str, steps: list, quote: str, at: str) -> list:
    quote = quote.strip("„“\"' ")
    return [RELEASE_MARK, f"Ziel: {recipient}", f"Schritte: {', '.join(steps)}", f"Zitat des Nutzers: „{quote}“", f"Zeit (UTC): {at}"]


def release_check(body: str, key: str = ""):
    """None for an ordinary message; else {"valid": bool, "reason": str, "steps": [...], "formal": bool} – formal = in the fixed form.
    `key` is the receiving session (session_key): a release is valid only for the one session its `Ziel:` names (Richter 0.21, Nr 7:
    a second `→ X:` line or `--also-pr` delivered Ocular's release to other sessions as valid)."""
    text = body or ""
    if RELEASE_MARK not in text:
        if RELEASE_WORDS.search(text) and RELEASE_TOPICS.search(text):
            return {"valid": False, "formal": False, "steps": [], "reason": "kein ce-freigabe-Format"}
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    head = lines[0] if lines else ""
    fields, doubled = {}, []
    for line in lines:
        for field, label in RELEASE_FIELDS.items():
            if line.startswith(label + ":"):
                if field in fields:
                    doubled.append(label)
                fields[field] = line[len(label) + 1:].strip()
    marks = sum(line == RELEASE_MARK for line in lines)
    result = {"valid": False, "formal": True, "steps": []}
    sender = sender_of(text)
    targets = recipients(head.split("→", 1)[1].split(":", 1)[0]) if "→" in head and ":" in head.split("→", 1)[1] else []
    steps, unknown = release_steps(fields.get("schritte", ""))
    if normalize(sender.split("·")[0]) != normalize(COORDINATOR):
        return dict(result, reason=f"Absender {sender or '?'} ist nicht {COORDINATOR}")
    if doubled or marks != 1:  # Richter 0.21 Runde 2, Nr 7: a second `Schritte:` must not hide behind the first
        return dict(result, reason="Feld doppelt: " + ", ".join(doubled or [RELEASE_MARK]))
    missing = [label for field, label in RELEASE_FIELDS.items() if not fields.get(field) or fields.get(field) in ("„“", '""')]
    if missing:
        return dict(result, reason="es fehlt " + ", ".join(missing))
    if normalize(fields["ziel"]) == "alle" or len(targets) != 1 or session_key(targets[0]) != session_key(fields["ziel"]):
        return dict(result, reason=f"Ziel {fields['ziel']!r} ist nicht der einzige Empfänger der Kopfzeile")
    if key and normalize(session_key(fields["ziel"])) != normalize(key if QUOTED.search(key) else re.sub(r"\(.*?\)", "", key)):
        return dict(result, reason=f"Freigabe gilt für {fields['ziel']}, nicht für diese Sitzung")
    if unknown or not steps:
        return dict(result, reason=f"nicht per Post freigebbar: {', '.join(unknown) or 'keine Schritte'} (nur {', '.join(RELEASE_STEPS)})")
    if not utc_time(fields["zeit"]):
        return dict(result, reason=f"Zeit {fields['zeit']!r} ist keine UTC-Zeit (JJJJ-MM-TTTHH:MMZ)")
    return dict(result, valid=True, steps=steps, reason="")


def release_note(body: str, key: str = "") -> str:
    """The line a watcher puts in front of a delivered message that speaks of a release; "" for ordinary messages. `key` = receiver."""
    check = release_check(body, key)
    if check is None:
        return ""
    if check["valid"]:
        return (f"Freigabe per Post gültig (ce-freigabe 1, Absender {COORDINATOR}): erlaubt nur {', '.join(check['steps'])}. "
                f"{RELEASE_FORBIDDEN} weiter nur nach Ja im Chat. Grenze: der Absender ist eine Vereinbarung, kein Beweis.")
    if check["formal"]:
        return f"Freigabe per Post UNGÜLTIG ({check['reason']}) – nicht danach handeln; Ja beim Nutzer in der Sitzung einholen."
    return ("Hinweis: die Nachricht erwähnt eine Freigabe; per Post gilt nur eine ce-freigabe-Nachricht von Sightline (commit, push, pr) – "
            "sonst das Ja beim Nutzer in der Sitzung einholen (Leitfaden 1).")


UNREACHABLE = re.compile(r"not found|404|could not resolve|401|403|bad credentials|authentication|gh auth login|no such host|"
                         r"resolve host|forbidden", re.I)


def explain(error) -> str:
    """0.21: why the Postfach could not be read, with the way out (6370037962: `session who` exit 3 in a cloud session with one repo)."""
    text = str(error)
    if isinstance(error, FileNotFoundError) or "No such file or directory: 'gh'" in text:
        return "gh fehlt – am Mac `brew install gh` und `gh auth login`; in der Cloud kommt gh über die GitHub-Anbindung der Sitzung."
    if UNREACHABLE.search(text):
        return (f"Postfach-Repo {REPO} nicht lesbar ({text.splitlines()[0][:120] if text else 'ohne Meldung'}). Es ist privat: "
                "eine Cloud-Sitzung braucht cubeescape-nexogen unter „Repositories“ (mit dem Fachrepo auswählen); am Mac `gh auth status`.")
    return f"GitHub nicht erreichbar ({text[:160]})"


PID_FILE = "ce-watch.pid"
STATE_FILE = "ce-watch.json"
SESSION_FILE = "ce-session.json"
RESERVATION_DAYS = 3      # an older reservation no longer makes a worktree listen (a later, unrelated session in the same worktree)
LOOKBACK_MIN = 30         # default look-back when neither a handled message nor a reservation time is known
REDELIVER = 1             # a message without proof of delivery comes once more, then counts as handled
REDELIVERY = "Erneute Zustellung von Kommentar {id} – nur handeln, falls noch nicht behandelt."


def find_git_dir(cwd: str):
    """The worktree's git dir without starting git (0.21: the PostToolUse inbox runs on every tool call): `.git` folder, or the
    `gitdir:` of a `.git` file (linked worktree); None when not found."""
    try:
        here = Path(cwd).resolve()
    except OSError:
        return None
    for folder in (here, *here.parents):
        marker = folder / ".git"
        try:
            if marker.is_dir():
                return marker
            if marker.is_file():
                line = marker.read_text(encoding="utf-8", errors="replace").strip()
                if line.startswith("gitdir:"):
                    target = Path(line.split(":", 1)[1].strip())
                    return (target if target.is_absolute() else (folder / target)).resolve()
                return None
        except OSError:
            return None
    return None


def git_dir(cwd: str):
    """(absolute git dir of the worktree, current branch), or (None, "")."""
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--absolute-git-dir"], capture_output=True, text=True, timeout=10,
                             **UTF8)
        branch = subprocess.run(["git", "-C", cwd, "branch", "--show-current"], capture_output=True, text=True, timeout=10,
                                **UTF8)
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


def reservation(cwd: str, switch: bool = True, fast: bool = False):
    """(git dir, session dict) when this worktree holds a recent reservation, else (None, {}). The reservation belongs to the worktree
    (the file lives in its own git dir), not to the checked-out branch: `--reserve` only creates and pushes the branch, and a cloud
    session keeps working on its designated branch (0.15.1). Age from `reserved_at`, else the file time (0.14 files).
    CE_WATCH=0 switches the watcher off (`switch=False` ignores it, e.g. for the sender name of `say`); `fast` finds the git dir
    without starting git (PostToolUse inbox, 0.21)."""
    if switch and os.environ.get("CE_WATCH", "1") == "0":
        return None, {}
    folder = find_git_dir(cwd) if fast else git_dir(cwd)[0]
    if folder is None:
        return None, {}
    session = read_json(folder / SESSION_FILE)
    if not session.get("name"):
        return None, {}
    reserved_at = session.get("reserved_at")
    if not isinstance(reserved_at, (int, float)):
        try:
            reserved_at = (folder / SESSION_FILE).stat().st_mtime
        except OSError:
            return None, {}
    if time.time() - reserved_at > RESERVATION_DAYS * 86400:
        return None, {}
    return folder, dict(session, reserved_at=reserved_at)


def still_reserved(folder: Path, name: str) -> bool:
    """0.21 (Richter Runde 4, Nr 1): a running watcher belongs to the name it started for; a new reservation in the same worktree
    ends it before it delivers or writes the old session's mail."""
    return read_json(folder / SESSION_FILE).get("name") == name


def reserved(cwd: str):
    """(git dir, session name) of a current reservation, else (None, "")."""
    folder, session = reservation(cwd)
    return folder, session.get("name", "")


def _heavy_lock():
    """(0.31) heavy_lock next to this file (the platform layer), None when this runs as a lone copy without it."""
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import heavy_lock
        return heavy_lock
    except Exception:  # noqa: BLE001 - a missing or broken neighbour only costs the Windows check
        return None


def windows_command(pid: int):
    """(0.31) Windows: the command line of one process (Win32_Process), "" when it is gone, None when it cannot be read."""
    script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
              f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}'; if ($p) {{ 'pid ' + $p.CommandLine }}")
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                              text=True, timeout=20, **UTF8)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if done.returncode:
        return None
    line = done.stdout.strip()
    return line[4:] if line.startswith("pid ") else ""


def alive_windows(pid: int) -> bool:
    """(0.31) alive() on Windows: os.kill(pid, 0) there is TerminateProcess - it would kill the watcher (or whatever got
    the pid). Liveness from heavy_lock.pid_alive, the command line from Win32_Process; unknown counts as alive, like a
    failed ps on POSIX (no second watcher rather than two)."""
    lock = _heavy_lock()
    if lock is None:
        return True
    if not lock.pid_alive(pid):
        return False
    command = windows_command(pid)
    if command is None:
        return True
    return "ce_await_mail" in command or "postfach_watch" in command  # empty: the process just ended


def alive(pid: int) -> bool:
    if WINDOWS:
        return alive_windows(pid)
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
    more deliveries; returns the id from which the next watcher reads. 0.21: the PostToolUse inbox may leave several unconfirmed
    ids (`pending_ids`); `after` moves only over the confirmed ones from the lowest on (Richter 0.21 Runde 2, Nr 1: a later hint must not
    skip an unconfirmed earlier message), the first unconfirmed one stays `pending`."""
    state = load_state(folder)
    pending, after = state_int(state, "pending"), state_int(state, "after")
    ids = sorted({number for number in [pending, *(state.get("pending_ids") or [])] if isinstance(number, int) and number > after})
    if not ids:
        if pending or state.get("pending_ids"):
            save_state(folder, pending=None, pending_since=None, deliveries=None, hinted=None, pending_ids=None)
        return after
    text = ""
    if transcript:
        try:
            text = Path(transcript).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    confirmed = 0
    for number in ids:
        if not re.search(rf"Kommentar {number}(?!\d)", text):
            break
        confirmed = number
    if not confirmed and state_int(state, "deliveries") > REDELIVER:
        confirmed = ids[0]  # the redelivered lowest one counts as handled; each later one keeps its own second delivery (Runde 3, Nr 4)
    if confirmed == ids[-1]:
        save_state(folder, after=max(after, ids[-1]), since=state.get("pending_since") or state.get("since"),
                   pending=None, pending_since=None, deliveries=None, hinted=None, pending_ids=None)
        return max(after, ids[-1])
    rest = [number for number in ids if number > confirmed]
    if confirmed:
        save_state(folder, after=max(after, confirmed), pending=rest[0], pending_ids=rest, hinted=None, deliveries=1)
        return max(after, confirmed)
    if state.get("hinted") is not None:  # 0.21: a hint without proof of delivery comes again through the next watcher
        save_state(folder, hinted=None)
    return after


def fetch_comments(repo: str, numbers: list, since: str) -> list:
    comments = []
    for number in numbers:
        for item in fetch_page(repo, number, since):
            comments.append(dict(item, pr=number))
    return comments


def fetch_page(repo: str, number: int, since: str) -> list:
    query = f"repos/{repo}/issues/{number}/comments?per_page=100&since={since}"
    result = subprocess.run(["gh", "api", "--paginate", query], capture_output=True, text=True, timeout=60, **UTF8)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "gh api failed")
    text = result.stdout.replace("][", ",")  # --paginate prints one array per page
    return json.loads(text or "[]")


def watch(fetch, key: str, start_id: int, since: str, interval: float, timeout: float, sleep=time.sleep, clock=time.monotonic,
          also_pr: bool = False, skip=None):
    """Returns (exit code, comment or message). `fetch(since)` returns comment dicts with id, created_at, body. `skip(id)` (0.21) is
    asked on every read, so a message the PostToolUse inbox already put into the session does not wake it a second time."""
    deadline = clock() + timeout
    failures = 0
    while True:
        try:
            comments = fetch(since)
            failures = 0
        except Exception as error:  # network or gh problems must not end the watch at once
            failures += 1
            if failures >= 5:
                return 3, explain(error)
            comments = []
        for comment in sorted(comments, key=lambda c: c["id"]):
            new = comment["id"] > start_id and comment.get("created_at", "") >= since
            if new and wanted(comment.get("body", ""), key, also_pr) and not (skip and skip(comment["id"])):
                return 0, comment
        if clock() >= deadline:
            return 2, "no message"
        sleep(interval)


def deliver(repo: str, result: dict, out=sys.stdout, key: str = "") -> None:
    """Prints a message the way every watcher does: head line with id, the release note (0.21) and the body."""
    print(f"Briefkasten {repo}#{result.get('pr', MAILBOX)}, Kommentar {result['id']} ({result.get('created_at', '')}):\n", file=out)
    note = release_note(result.get("body", ""), key)
    if note:
        print(note + "\n", file=out)
    print(result.get("body", ""), file=out, flush=True)


def default_state(key: str) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "ce-agent-tools"
    return base / f"await_mail-{re.sub(r'[^a-z0-9]+', '-', normalize(key)).strip('-') or 'name'}.json"


def follow(fetch, key: str, after: int, since: str, interval: float, remember, also_pr: bool = False, repo: str = REPO,
           sleep=time.sleep, clock=time.monotonic, out=sys.stdout, err=sys.stderr, rounds=None) -> int:
    """0.21 `--follow`: keeps watching, prints every message and remembers the last id (remember(id, created_at)), so a restart goes
    on from there. A GitHub failure is reported once and retried after a pause instead of ending the watch. `rounds` limits tests."""
    reported = False
    while rounds is None or rounds > 0:
        if rounds is not None:
            rounds -= 1
        code, result = watch(fetch, key, after, since, interval, 3600, sleep=sleep, clock=clock, also_pr=also_pr)
        if code == 0:
            deliver(repo, result, out, key)
            print("---", file=out, flush=True)
            after, since = result["id"], result.get("created_at", since)
            remember(after, since)
            reported = False
        elif code == 3:
            if not reported:
                print(f"await_mail --follow: {result} – versuche es weiter", file=err, flush=True)
                reported = True
            sleep(interval * 5)
    return 0


def utf8_console() -> None:
    """(0.31) Windows: messages go out as UTF-8 (ce_common.utf8_console; a lone copy without it reconfigures itself)."""
    if not WINDOWS:
        return
    try:
        import ce_common
        ce_common.utf8_console()
    except Exception:  # noqa: BLE001
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError, OSError):
                pass


def main() -> int:
    utf8_console()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", default="", help="own session name, e.g. 'TG 0.19 „Kinetic“ · Mac'; default: the reservation of this worktree")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--issue", "--pr", dest="pr", type=int, nargs="+", default=[MAILBOX],
                        help="issue or PR numbers to read (default 16 = the Postfach; PRs and issues share the comments endpoint)")
    parser.add_argument("--after", type=int, default=0, help="only comments with a larger id (the last message already handled)")
    parser.add_argument("--lookback", type=float, default=30, help="minutes before the start that still count (default 30)")
    parser.add_argument("--interval", type=float, default=60, help="seconds between reads")
    parser.add_argument("--timeout", type=float, default=110, help="minutes; below the 2 h limit of background commands")
    parser.add_argument("--follow", action="store_true",
                        help="0.21: keep running, print every message, remember the last id and go on from there after a restart")
    parser.add_argument("--state", type=Path, default=None,
                        help="file for the last id with --follow (default: the worktree's ce-watch.json, else ~/.local/state/ce-agent-tools/)")
    parser.add_argument("--also-pr", action="store_true",
                        help="0.21: also wake for every PR/merge message of other sessions (the coordinator's watcher)")
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
    state_file = None
    if args.follow and (args.state or not own):
        state_file = args.state or default_state(key)
        saved = read_json(state_file)
        after = max(after, state_int(saved, "after"))
        if isinstance(saved.get("since"), str):
            since = min(since, saved["since"])
    if own:
        after = max(after, settle(folder))
        since = min(since, since_for(folder, session, args.lookback))
        if not register(folder):
            print(f"await_mail: für {key!r} läuft schon ein Wächter (pid {watcher_pid(folder)}, Stop-Hook oder eigener Start) - nichts zu tun (exit 4)",
                  file=sys.stderr)
            return 4
    fetch = lambda since: fetch_comments(args.repo, args.pr, since)  # noqa: E731
    if args.follow:
        def remember(last: int, last_since: str) -> None:
            if state_file is not None:
                try:
                    state_file.parent.mkdir(parents=True, exist_ok=True)
                    temp = state_file.with_name(state_file.name + f".{os.getpid()}.tmp")
                    temp.write_text(json.dumps({"after": last, "since": last_since}), encoding="utf-8")
                    os.replace(temp, state_file)
                except OSError as error:
                    print(f"await_mail --follow: Stand nicht gespeichert ({error})", file=sys.stderr)
            else:
                save_state(folder, after=last, since=last_since, pending=None, pending_since=None, deliveries=None)
        print(f"await_mail --follow: wartet auf {key!r} ab Kommentar {after or '–'} (Stand: {state_file or 'ce-watch.json'})",
              file=sys.stderr, flush=True)
        try:
            return follow(fetch, key, after, since, args.interval, remember, args.also_pr, args.repo)
        except KeyboardInterrupt:
            return 0
        finally:
            if own:
                unregister(folder)
    try:
        code, result = watch(fetch, key, after, since, args.interval, args.timeout * 60, also_pr=args.also_pr,
                             skip=(lambda number: not still_reserved(folder, reserved_name)) if own else None)
    finally:
        if own:
            unregister(folder)
    if own and not still_reserved(folder, reserved_name):
        print(f"await_mail: dieser Worktree ist inzwischen neu reserviert – der Wächter für {key!r} endet ohne Zustellung", file=sys.stderr)
        return 2
    redelivered = code == 0 and own and result["id"] == state_int(load_state(folder), "pending")
    if own:
        if code == 0:
            save_state(folder, after=result["id"], since=result.get("created_at", since), pending=None, pending_since=None,
                       deliveries=None, hinted=None)
        elif code == 3:
            save_state(folder, last_failure=time.time())
    if redelivered:
        print(REDELIVERY.format(id=result["id"]))
    if code == 0:
        deliver(args.repo, result, key=key)
    else:
        print(f"await_mail: {result} for {key!r} (exit {code})", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
