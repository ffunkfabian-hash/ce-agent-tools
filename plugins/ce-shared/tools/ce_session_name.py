#!/usr/bin/env python3
"""Proposes the session name `<Repo> <Version> „<Name>“ · <Ort>` and the branch name (CLAUDE.md > Sitzungsnamen, regeln-details.md).

Run in the working repository at the start of a session:
    python3 ~/.claude/templates/session_name.py --name Pinpoint            # next free version, name chosen by the session
    python3 ~/.claude/templates/session_name.py --task "Aufräumen"          # no version: `<Repo> <Aufgabe> · <Ort>`
    python3 ~/.claude/templates/session_name.py --name Pinpoint --version 0.96 --agent codex

Repo kürzel come from `.ce-repo.json` ("name"), the place from the real environment (never from the prompt), the next free version
from `.ce-repo.json` and the branch names of `origin` (`git ls-remote`, skipped offline).
1.15: the branch on origin IS the reservation (the Versionsregister PR is retired): `--reserve` creates the branch at HEAD and pushes
it (no code, so no user approval needed); a number that origin already has is skipped. `--topics` lists the open claude/codex
branches of origin so a session can see whether the same subject is already being worked on (then ask the user).
"""
import argparse
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

ABBREVIATIONS = {
    "cubeescape": "CE", "cubeescape-toolgen": "TG", "cubeescape-robogen": "RG", "cubeescape-cubegen": "CG",
    "cubeescape-design": "DS", "cubeescape-reference": "REF", "cubeescape-rules": "RULES",
}
CLOUD_MARKERS = ("CLAUDE_CODE_CONTAINER_ID", "CLAUDE_CODE_REMOTE", "CCR_AGENT_PROXY_ENABLED")


def abbreviation(repo_name: str) -> str:
    name = (repo_name or "").strip().lower()
    return ABBREVIATIONS.get(name, name.replace("cubeescape-", "").upper() or "?")


def detect_place(system: str, env: dict, agent: str) -> str:
    """Mac / Cloud / Codex-Mac / Codex-Cloud from the machine itself: macOS is the Mac, everything else (Linux container) the cloud."""
    base = "Mac" if system == "Darwin" else "Cloud"
    codex = agent == "codex" or (agent == "auto" and any(key.startswith("CODEX_") for key in env))
    if base == "Cloud" and agent == "auto" and not codex and not any(key in env for key in CLOUD_MARKERS):
        base = "Cloud"  # a bare Linux box is treated as a cloud session as well
    return ("Codex-" if codex else "") + base


def version_key(text: str):
    return tuple(int(part) for part in text.split("."))


def config_version(config: dict, repo: Path) -> str:
    """`.ce-repo.json` › `version` is either the number itself or `{"file": …, "pattern": …}`: the first match (group 1) in that file,
    like the shared ce_tools read it (most repos keep the number in code or at the top of the CHANGELOG)."""
    value = config.get("version", "0.0")
    if not isinstance(value, dict):
        return str(value)
    path = repo / value.get("file", "")
    if not path.is_file():
        return "0.0"
    found = re.search(value.get("pattern", ""), path.read_text(encoding="utf-8"))
    return found.group(1) if found and found.groups() else "0.0"


def next_version(current: str, branches) -> str:
    """Minor + 1 of the highest version that the config or any branch name already mentions (branches like claude/ce-0.96-pinpoint, release/0.94.0)."""
    parts = version_key(current)
    highest = parts[:2]
    for branch in branches:
        for found in re.findall(r"(?<![\d.])(\d+)\.(\d+)(?:\.\d+)?(?![\d])", branch):
            candidate = (int(found[0]), int(found[1]))
            if candidate[0] == parts[0] and candidate > highest:
                highest = candidate
    return f"{highest[0]}.{highest[1] + 1}"


def slug(text: str) -> str:
    text = text.lower().translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}))
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def session_name(abbrev: str, place: str, version: str = "", name: str = "", task: str = "") -> str:
    if version:
        quoted = f" „{name}“" if name else ""
        return f"{abbrev} {version}{quoted} · {place}"
    return f"{abbrev} {task or 'Aufgabe'} · {place}"


def branch_name(agent: str, abbrev: str, version: str = "", name: str = "", task: str = "") -> str:
    prefix = "codex" if agent == "codex" else "claude"
    parts = [abbrev.lower(), version, slug(name)] if version else [abbrev.lower(), slug(task or "aufgabe")]
    return prefix + "/" + "-".join(part for part in parts if part)


def remote_branches(repo: Path):
    try:
        out = subprocess.run(["git", "-C", str(repo), "ls-remote", "--heads", "origin"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.split("refs/heads/", 1)[-1] for line in out.stdout.splitlines() if "refs/heads/" in line]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="", help="the code name (English, chosen by the session; never reuse one from the CHANGELOG)")
    parser.add_argument("--task", default="", help="short task for sessions without a version")
    parser.add_argument("--version", default="", help="fixed version (continuation of the same work); default: next free minor")
    parser.add_argument("--agent", choices=("auto", "claude", "codex"), default="auto")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--offline", action="store_true", help="do not ask origin for branch names")
    parser.add_argument("--reserve", action="store_true", help="create the branch at HEAD and push it to origin (the reservation)")
    parser.add_argument("--topics", action="store_true", help="list the open claude/ and codex/ branches of origin (same subject open?)")
    args = parser.parse_args(argv)

    config_path = args.repo / ".ce-repo.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    abbrev = abbreviation(config.get("name", args.repo.resolve().name))
    place = detect_place(platform.system(), dict(os.environ), args.agent)
    agent = "codex" if place.startswith("Codex") else "claude"
    version = ""
    branches = [] if args.offline else remote_branches(args.repo)
    if not args.task or args.version or args.name:
        current = args.version or config_version(config, args.repo)
        version = args.version or next_version(current, branches)
    branch = branch_name(agent, abbrev, version, args.name, args.task)
    print("Sitzungsname:", session_name(abbrev, place, version, args.name, args.task))
    print("Branch:      ", branch)
    if args.topics:
        for other in sorted(b for b in branches if b.startswith(("claude/", "codex/"))):
            print("offen:       ", other)
    if args.reserve:
        return reserve(args.repo, branch)
    if version and not args.version:
        print("Hinweis:      mit --reserve den Branch auf origin anlegen (das ist die Reservierung), bevor du etwas änderst.")
    return 0


def reserve(repo: Path, branch: str) -> int:
    """The reservation: the branch at HEAD, pushed to origin. Fails (exit 1) when origin already has it."""
    if branch in remote_branches(repo):
        print(f"Branch {branch} gibt es auf origin schon - Nummer vergeben, nächste nehmen.", file=sys.stderr)
        return 1
    for command in (["git", "branch", "--force", branch, "HEAD"], ["git", "push", "-u", "origin", branch]):
        result = subprocess.run(command, cwd=repo, capture_output=True, text=True)
        if result.returncode:
            print(result.stderr.strip() or f"{' '.join(command)} fehlgeschlagen", file=sys.stderr)
            return 1
    print(f"Reserviert:   {branch} auf origin (git checkout {branch})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
