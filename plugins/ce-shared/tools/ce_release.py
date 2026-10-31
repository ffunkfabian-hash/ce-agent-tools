#!/usr/bin/env python3
"""ce_release.py [--repo PATH] [--execute] [--no-push] [--remote origin] - release of the current version.

Dry run is the default and changes nothing. It checks version (from .ce-repo.json), CHANGELOG entry, clean tree, tag
<tag_prefix><version> and branch <branch_prefix><version> do not exist yet, the remote has neither, and prints the git
commands it would run. Only with --execute it creates the annotated tag and the release branch at HEAD and pushes both
(never --force; --no-push stops after the local tag/branch). Merging is not part of it.
Exit code: 0 ready/done, 1 blocked or a step failed, 2 configuration problem.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ce_common  # noqa: E402


def plan(root: Path, config: dict, remote: str, check_remote: bool = True) -> dict:
    """Findings and the commands for a release of the current version: {"version", "problems", "notes", "commands"}."""
    version = ce_common.read_version(root, config)
    release = config.get("release") or {}
    tag = f"{release.get('tag_prefix', 'v')}{version}"
    branch = f"{release.get('branch_prefix', 'release/')}{version}"
    problems, notes = [], []

    changelog = (config.get("docs") or {}).get("changelog")
    if changelog:
        path = root / changelog
        if not path.is_file():
            problems.append(f"{changelog} missing")
        elif version not in path.read_text(encoding="utf-8", errors="replace"):
            problems.append(f"{changelog} has no entry for {version}")
    else:
        notes.append('no "docs.changelog" configured - CHANGELOG not checked')

    if ce_common.git_out(root, "rev-parse", "--git-dir") is None:
        problems.append("not a git repository")
        return {"version": version, "tag": tag, "branch": branch, "problems": problems, "notes": notes, "commands": []}
    status = ce_common.git_out(root, "status", "--porcelain")
    if status:
        problems.append(f"working tree not clean ({len(status.splitlines())} change(s))")
    if ce_common.git_out(root, "rev-parse", "-q", "--verify", f"refs/tags/{tag}"):
        problems.append(f"tag {tag} exists already")
    if ce_common.git_out(root, "rev-parse", "-q", "--verify", f"refs/heads/{branch}"):
        problems.append(f"branch {branch} exists already")
    if check_remote:
        listing = ce_common.git_out(root, "ls-remote", "--heads", "--tags", remote, timeout=20)
        if listing is None:
            notes.append(f"remote {remote} not reachable - remote refs not checked")
        else:
            refs = {line.split("\t")[-1] for line in listing.splitlines()}
            for ref in (f"refs/tags/{tag}", f"refs/heads/{branch}"):
                if ref in refs:
                    problems.append(f"{ref} exists on {remote}")
    head = ce_common.git_out(root, "rev-parse", "--short", "HEAD") or "?"
    notes.append(f"HEAD is {head} on {ce_common.git_out(root, 'rev-parse', '--abbrev-ref', 'HEAD') or '?'}")

    commands = [["git", "tag", "-a", tag, "-m", f"{config.get('name', root.name)} {version}"],
                ["git", "branch", branch],
                ["git", "push", remote, branch, tag]]
    return {"version": version, "tag": tag, "branch": branch, "problems": problems, "notes": notes,
            "commands": commands}


def execute(root: Path, commands: list[list[str]], push: bool) -> tuple[bool, list[str]]:
    lines = []
    for argv in commands:
        if not push and argv[1] == "push":
            lines.append(f"skipped (--no-push): {' '.join(argv)}")
            continue
        done = ce_common.git(root, *argv[1:], timeout=120)
        lines.append(f"{'ok  ' if done.returncode == 0 else 'FAIL'} {' '.join(argv)}")
        if done.returncode != 0:
            lines.append((done.stderr or done.stdout).strip())
            return False, lines
    return True, lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", help="repository (default: current directory)")
    parser.add_argument("--execute", action="store_true", help="really tag, branch and push (default: dry run)")
    parser.add_argument("--no-push", action="store_true", help="with --execute: create tag and branch locally only")
    parser.add_argument("--remote", default="origin")
    args = parser.parse_args(argv)
    try:
        root, config = ce_common.open_repo(args.repo)
        result = plan(root, config, args.remote)
    except ce_common.RepoError as error:
        print(f"ce_release: {error}")
        return 2
    name = config.get("name", root.name)
    blocked = bool(result["problems"])
    mode = "EXECUTE" if args.execute else "dry run"
    print(f"{name} release {result['version']} ({mode}): {'BLOCKED' if blocked else 'ready'}")
    for problem in result["problems"]:
        print(f"  problem: {problem}")
    for note in result["notes"]:
        print(f"  note: {note}")
    if blocked:
        return 1
    if not args.execute:
        for command in result["commands"]:
            print("  would run: " + " ".join(command))
        print("  nothing changed - add --execute to do it")
        return 0
    ok, lines = execute(root, result["commands"], push=not args.no_push)
    for line in lines:
        print("  " + line)
    return 0 if ok else 1


if __name__ == "__main__":
    try:  # (0.31) Windows: a cp1252 console or pipe cannot take the tool's „“ → ·
        import ce_common as _console
        _console.utf8_console()
    except ImportError:
        pass
    sys.exit(main(sys.argv[1:]))
