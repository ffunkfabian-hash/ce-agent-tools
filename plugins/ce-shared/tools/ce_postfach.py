#!/usr/bin/env python3
"""Writes to the Postfach (cubeescape-nexogen#16) so sessions can talk to each other without remembering the format (0.14).

    ./ce session say --to Sightline "Frage am Bild" "KG03: Griff links oder rechts?"   # sender from the branch
    ./ce session say --to "TG 0.23 „Livery“" --model "Opus 5.5, high" "Anker" "y −0,25 m"
    ./ce session who                                                                     # who announced itself lately

The sender is the session name (`--name`, else derived from the current branch: `claude/tg-0.23-livery` -> `TG 0.23 „Livery“ · Mac`).
A message is `<Sender> → <Empfänger>: <Thema>`, the model line and at most ten lines (regeln-details.md › Briefkasten); recipient
`alle` reaches every session that watches with `ce_await_mail.py`. `--dry-run` prints the comment instead of posting it. Posting uses
`gh api` like the watcher; nothing is posted for a repository whose origin is not a GitHub URL (local paths, bare test repos) or with
CE_MAILBOX=0. The sender name is the one `--reserve` wrote to `<git-dir>/ce-session.json`, else derived from the branch.
`who` lists the `angemeldet` messages of the last days (written by `ce_session_name.py --reserve`) with the latest message of each sender.
"""
import argparse
import datetime
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ce_await_mail as am  # noqa: E402
import ce_session_name as sn  # noqa: E402

REPO, MAILBOX = am.REPO, am.MAILBOX
GITHUB_ORIGIN = re.compile(r"^(?:https://(?:[^@/]+@)?|git@|ssh://git@)github\.com[:/]")
MAX_LINES = 10
ANNOUNCED = "angemeldet"
VERSIONED = re.compile(r"^(?:claude|codex|gemini)/[a-z0-9-]+?-(\d+\.\d+(?:\.\d+)?)-([a-z0-9-]+)$")
PATCH = re.compile(r"^(?:claude|codex|gemini)/.+?-patch-([a-z0-9-]+)$")


def sender_from_branch(branch: str, abbrev: str, place: str) -> str:
    """The session name a branch stands for, or "" when the branch carries none (main, release/…, a free name)."""
    found = VERSIONED.match(branch or "")
    if found:
        return sn.session_name(abbrev, place, found.group(1), found.group(2).replace("-", " ").title())
    found = PATCH.match(branch or "")
    if found:
        return patch_name(abbrev, place, found.group(1))
    return ""


def patch_name(abbrev: str, place: str, short: str) -> str:
    return f"{abbrev} Patch {sn.slug(short)} · {place}"


def recipient_name(name: str) -> str:
    """A recipient is written without the place (`TG 0.23 „Livery“`), so it reaches the session wherever it runs."""
    return name.split("·")[0].strip()


def format_message(sender: str, recipient: str, topic: str, model: str = "", lines=()) -> str:
    lines = [line for line in lines if line.strip()]
    if len(lines) > MAX_LINES:
        raise ValueError(f"höchstens {MAX_LINES} Zeilen nach Kopf und Modellzeile ({len(lines)}); Langes als Link auf PR oder Bericht")
    if not topic.strip() or "\n" in topic:
        raise ValueError("Thema: genau eine nicht leere Zeile")
    head = f"{sender} → {recipient_name(recipient)}: {topic.strip()}"
    return "\n".join([head, model.strip() or "Modell und Aufwand: nicht angegeben", *lines]) + "\n"


def on_github(repo: Path) -> bool:
    """Only a repository that lives on GitHub talks to the Postfach (tests and scratch clones with a local origin stay silent)."""
    if os.environ.get("CE_MAILBOX", "1") == "0":
        return False
    return bool(GITHUB_ORIGIN.match(sn_git(repo, "remote", "get-url", "origin")))


def sn_git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def post(body: str, repo: str = REPO, issue: int = MAILBOX, runner=subprocess.run) -> dict:
    """Posts the comment with `gh api` (same transport as the watcher); returns GitHub's answer (id, html_url). Raises on failure."""
    result = runner(["gh", "api", "-X", "POST", f"repos/{repo}/issues/{issue}/comments", "--input", "-"],
                    input=json.dumps({"body": body}), capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError((result.stderr or "").strip() or "gh api failed")
    return json.loads(result.stdout or "{}")


def send(body: str, repo_dir: Path, dry_run: bool = False, runner=subprocess.run, out=sys.stdout, err=sys.stderr) -> int:
    """Posts or explains why not; prints the text so a session without GitHub can paste it. 0 = posted (or dry run), 1 = not posted."""
    if dry_run:
        print(body, end="", file=out)
        return 0
    if not on_github(repo_dir):
        print("Postfach: kein GitHub-Origin oder CE_MAILBOX=0 - nicht gesendet. Text zum Einfügen in cubeescape-nexogen#16:\n", file=err)
        print(body, end="", file=err)
        return 1
    try:
        answer = post(body, runner=runner)
    except subprocess.TimeoutExpired:
        print("Postfach: keine Antwort von GitHub in 60 s - vor dem Einfügen in cubeescape-nexogen#16 prüfen, ob der Kommentar schon steht:\n", file=err)
        print(body, end="", file=err)
        return 1
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Postfach: nicht gesendet ({error}). Text zum Einfügen in cubeescape-nexogen#16:\n", file=err)
        print(body, end="", file=err)
        return 1
    print(f"Postfach: gesendet, Kommentar {answer.get('id')} {answer.get('html_url', '')}".rstrip(), file=out)
    return 0


def announcement(sender: str, branch: str, base: str, repo: str, about: str = "", model: str = "", to: str = "Sightline") -> str:
    lines = [f"Repo `{repo}`, Branch `{branch}` (Basis `{base}`)."]
    if about:
        lines.append(f"Auftrag: {about}")
    lines.append(f"Nachrichten an diese Sitzung: `{recipient_name(sender)}`; Wächter `./ce session mail --name \"{recipient_name(sender)}\"`.")
    return format_message(sender, to, ANNOUNCED, model, lines)


def head_of(body: str):
    """(sender, recipient, topic) of a Postfach comment's first line, or None."""
    first = (body or "").strip().splitlines()[0] if (body or "").strip() else ""
    if "→" not in first or ":" not in first.split("→", 1)[1]:
        return None
    sender, rest = first.split("→", 1)
    recipient, topic = rest.split(":", 1)
    return sender.strip(" *"), recipient.strip(" *"), topic.strip(" *")


def sessions(comments: list) -> list:
    """Announced sessions, newest first: sender, time of the announcement, its second-to-last lines, and the sender's latest message."""
    seen = {}
    for item in sorted(comments, key=lambda c: c["id"]):
        head = head_of(item.get("body", ""))
        if not head:
            continue
        key = am.normalize(recipient_name(head[0]))
        if head[2].lower().startswith(ANNOUNCED):
            seen[key] = {"sender": head[0], "announced": item.get("created_at", ""), "last": head[2], "last_at": item.get("created_at", ""),
                         "detail": next((line for line in item["body"].splitlines()[2:] if line.startswith("Repo ")), "")}
        elif key in seen:
            seen[key].update(last=head[2], last_at=item.get("created_at", ""))
    return sorted(seen.values(), key=lambda s: s["last_at"], reverse=True)


def fetch(since: str, repo: str = REPO, issue: int = MAILBOX) -> list:
    result = subprocess.run(["gh", "api", "--paginate", f"repos/{repo}/issues/{issue}/comments?per_page=100&since={since}"],
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "gh api failed")
    return json.loads(result.stdout.replace("][", ",") or "[]")


def own_name(repo_dir: Path, agent: str = "auto") -> str:
    config_path = repo_dir / ".ce-repo.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    abbrev = sn.abbreviation(config.get("name", repo_dir.resolve().name))
    place = sn.detect_place(platform.system(), dict(os.environ), agent)
    branch = sn_git(repo_dir, "branch", "--show-current")
    git_dir = sn_git(repo_dir, "rev-parse", "--absolute-git-dir")
    try:  # the name written at the reservation wins (exact spelling); the branch is only the fallback
        saved = json.loads((Path(git_dir) / sn.SESSION_FILE).read_text(encoding="utf-8")) if git_dir else {}
    except (OSError, ValueError):
        saved = {}
    if saved.get("branch") == branch and saved.get("name"):
        return saved["name"]
    return sender_from_branch(branch, abbrev, place)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    say = sub.add_parser("say", help="Nachricht ins Postfach")
    say.add_argument("topic", help="Thema (eine Zeile)")
    say.add_argument("lines", nargs="*", help=f"höchstens {MAX_LINES} weitere Zeilen")
    say.add_argument("--to", default="Sightline", help="Empfänger: Sitzungsname, Rolle oder `alle` (Standard Sightline)")
    say.add_argument("--name", default="", help="eigener Sitzungsname; Standard: aus dem Branch")
    say.add_argument("--model", default="", help="Modell und Aufwand, z. B. 'Opus 5.5, high'")
    say.add_argument("--agent", choices=("auto", "claude", "codex", "gemini"), default="auto")
    say.add_argument("--repo", type=Path, default=Path.cwd())
    say.add_argument("--dry-run", action="store_true", help="nur anzeigen, nicht senden")
    who = sub.add_parser("who", help="angemeldete Sitzungen der letzten Tage")
    who.add_argument("--days", type=float, default=3)
    args = parser.parse_args(argv)

    if args.command == "who":
        since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            found = sessions(fetch(since))
        except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as error:
            print(f"Postfach nicht lesbar: {error}", file=sys.stderr)
            return 3
        for entry in found:
            print(f"{entry['sender']}\n  angemeldet {entry['announced']}  {entry['detail']}\n  zuletzt {entry['last_at']}: {entry['last']}")
        if not found:
            print(f"keine Anmeldung in den letzten {args.days:g} Tagen")
        return 0

    sender = args.name or own_name(args.repo, args.agent)
    if not sender:
        print("Sitzungsname unbekannt (Branch ohne Version/Patch): --name \"<Repo> <Version> „<Name>“ · <Ort>\" angeben", file=sys.stderr)
        return 2
    try:
        body = format_message(sender, args.to, args.topic, args.model, args.lines)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    return send(body, args.repo, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
