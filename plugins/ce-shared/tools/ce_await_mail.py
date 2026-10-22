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
"""
import argparse
import json
import re
import subprocess
import sys
import time

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
    parser.add_argument("--name", required=True, help="own session name, e.g. 'TG 0.19 „Kinetic“ · Mac'")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--issue", "--pr", dest="pr", type=int, nargs="+", default=[MAILBOX],
                        help="issue or PR numbers to read (default 16 = the Postfach; PRs and issues share the comments endpoint)")
    parser.add_argument("--after", type=int, default=0, help="only comments with a larger id (the last message already handled)")
    parser.add_argument("--lookback", type=float, default=30, help="minutes before the start that still count (default 30)")
    parser.add_argument("--interval", type=float, default=60, help="seconds between reads")
    parser.add_argument("--timeout", type=float, default=110, help="minutes; below the 2 h limit of background commands")
    args = parser.parse_args()

    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - args.lookback * 60))
    key = session_key(args.name)
    code, result = watch(lambda since: fetch_comments(args.repo, args.pr, since), key, args.after, since,
                         args.interval, args.timeout * 60)
    if code == 0:
        print(f"Briefkasten {args.repo}#{result['pr']}, Kommentar {result['id']} ({result['created_at']}):\n")
        print(result["body"])
    else:
        print(f"await_mail: {result} for {key!r} (exit {code})", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
