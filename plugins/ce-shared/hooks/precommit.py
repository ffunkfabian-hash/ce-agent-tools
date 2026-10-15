#!/usr/bin/env python3
"""PreToolUse hook of ce-shared: before `git commit`, lint changed docs/skills (warns, never blocks).

When the commit contains a *.md file or a skill (SKILL.md, .agents/skills/**), it runs tools/ce_lint.py for the repo
(if the repo has a .ce-repo.json) and the repo's own check_docs.py (Tools/, tools/ or scripts/), and reports their
result lines as a systemMessage. Any error, timeout or missing tool lets the commit through silently - it only warns.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
LINT = PLUGIN_ROOT / "tools" / "ce_lint.py"
DOC_CHECKS = ("Tools/check_docs.py", "tools/check_docs.py", "scripts/check_docs.py")
SKILL_CHECK = ".agents/skills/check_skills.py"
COMMIT = re.compile(r"(?:^|[;&|(]\s*)git\s+(?:-[Cc]\s+\S+\s+)*commit(?![\w-])")
TIMEOUT = 8


def is_commit(command):
    return bool(COMMIT.search(command or ""))


def is_doc(path):
    lower = path.lower()
    return lower.endswith(".md") or "/skills/" in "/" + lower or lower.endswith("skill.md")


def commit_files(cwd, command):
    """Files the commit will contain: staged ones, plus tracked changes for `commit -a`."""
    args = ["git", "-C", cwd, "diff", "--name-only"]
    files = set(subprocess.run(args + ["--cached"], capture_output=True, text=True, timeout=TIMEOUT).stdout.split("\n"))
    if re.search(r"\s-[a-zA-Z]*a[a-zA-Z]*\b|\s--all\b", command):
        files |= set(subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT).stdout.split("\n"))
    return sorted(f for f in files if f)


def repo_root(cwd):
    done = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                          timeout=TIMEOUT)
    return Path(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None


def run_tool(argv, root):
    done = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=TIMEOUT)
    lines = [line for line in (done.stdout + done.stderr).strip().splitlines() if line.strip()]
    return done.returncode, lines


def decide(payload):
    if payload.get("tool_name") != "Bash":
        return {}
    command = (payload.get("tool_input") or {}).get("command", "")
    if not is_commit(command):
        return {}
    cwd = payload.get("cwd") or os.getcwd()
    root = repo_root(cwd)
    if root is None or not any(is_doc(f) for f in commit_files(str(root), command)):
        return {}
    notes = []
    if (root / ".ce-repo.json").is_file() and LINT.is_file():
        code, lines = run_tool([sys.executable, str(LINT), "--repo", str(root)], root)
        if code != 0:
            notes.append("ce_lint: " + "\n".join(lines[:12]))
    for relative in DOC_CHECKS:
        if (root / relative).is_file():
            code, lines = run_tool([sys.executable, relative], root)
            if code != 0:
                notes.append(f"{relative}: " + "\n".join(lines[-12:]))
            break
    if (root / SKILL_CHECK).is_file():
        code, lines = run_tool([sys.executable, SKILL_CHECK], root)
        if code != 0:
            notes.append(f"{SKILL_CHECK}: " + "\n".join(lines[-12:]))
    if not notes:
        return {}
    return {"systemMessage": "**[vor-commit-doku]** Docs/Skills im Commit, Prüfung meldet:\n" + "\n\n".join(notes)
            + "\nNur eine Warnung - der Commit läuft."}


def main():
    try:
        print(json.dumps(decide(json.load(sys.stdin))))
    except Exception:  # never wedge a session, never block a commit
        print(json.dumps({}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
