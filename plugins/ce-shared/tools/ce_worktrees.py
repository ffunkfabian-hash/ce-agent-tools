#!/usr/bin/env python3
"""ce_worktrees.py [--repos PATH ...] [--active-minutes N] [--projects DIR] - all worktrees of the Cube Escape repos.

Per worktree: branch, clean/dirty, ahead/behind of the base branch (main), and the session that uses the folder, as far
as it can be found: Claude Code keeps sessions of a folder in ~/.claude/projects/<folder path with / and . as ->/*.jsonl
(newest file = latest session, "active" when it changed within --active-minutes, default 60), and running claude/codex
processes whose working directory is the worktree (lsof, when available). A worktree is marked "NICHT LÖSCHEN" when
a session uses it (active or running process), when it has open changes, or when its branch has commits main lacks.
Output: table by default (--list: long form, --json: data). Default repos: all eight standard repos from repos.json (CE_REPOS = path list separated by ':' overrides). Read-only.
"""
from __future__ import annotations

import argparse
import json
import ntpath
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ce_common  # noqa: E402

HOME = Path.home()
DEFAULT_REPO_NAMES = ["cubeescape", "cubeescape-cubegen", "cubeescape-robogen", "cubeescape-toolgen",
                      "cubeescape-formgen", "cubeescape-motiongen", "cubeescape-nexogen", "ce-agent-tools"]
DEFAULT_REPOS = [HOME / "Claude" / "Cube Escape", HOME / "cubegen", HOME / "Claude" / "cubeescape-robogen",
                 HOME / "Claude" / "cubeescape-toolgen"]
DO_NOT_DELETE = "NICHT LÖSCHEN"


def default_repos(home: Path | None = None) -> list[Path]:
    return ce_common.standard_repos(DEFAULT_REPO_NAMES, home or HOME) or DEFAULT_REPOS


def parse_worktrees(porcelain: str) -> list[dict]:
    """`git worktree list --porcelain` -> [{"path", "head", "branch" (None: detached), "bare", "prunable"}]."""
    result, current = [], None
    for line in porcelain.splitlines() + [""]:
        if not line.strip():
            if current:
                result.append(current)
            current = None
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = {"path": value, "head": "", "branch": None, "bare": False, "prunable": False}
        elif current is not None:
            if key == "HEAD":
                current["head"] = value
            elif key == "branch":
                current["branch"] = value.removeprefix("refs/heads/")
            elif key == "bare":
                current["bare"] = True
            elif key == "prunable":
                current["prunable"] = True
    return result


def project_dir_name(path: str) -> str:
    """How Claude Code names the session folder of a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def codex_sessions_by_cwd(codex_dir: Path | None = None) -> dict[str, dict]:
    """Newest Codex rollout session per working directory from ~/.codex/sessions/*/*/*/rollout-*.jsonl."""
    root = Path(os.environ.get("CE_CODEX_SESSIONS") or codex_dir or (HOME / ".codex" / "sessions"))
    if not root.is_dir():
        return {}
    found: dict[str, dict] = {}
    try:
        files = sorted(root.glob("*/*/*/rollout-*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return {}
    now = time.time()
    for file in files:
        try:
            age_minutes = (now - file.stat().st_mtime) / 60
            with file.open("r", encoding="utf-8", errors="replace") as handle:
                first = handle.readline()
            if not first:
                continue
            record = json.loads(first)
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            cwd = payload.get("cwd")
            if not isinstance(cwd, str) or not cwd:
                continue
            session_id = str(payload.get("session_id") or payload.get("id") or file.stem)
            for key in {cwd, str(Path(cwd).resolve()) if Path(cwd).exists() else cwd}:
                prev = found.get(key)
                if prev is None or age_minutes < prev["age_minutes"]:
                    found[key] = {"id": session_id, "age_minutes": age_minutes, "title": f"codex:{session_id[:8]}"}
        except (OSError, ValueError):
            continue
    return found


def windows_key(path: str) -> str:
    """(0.31) Windows: one spelling per folder - git writes C:/x, Python and Codex C:\\x, case does
    not matter there. Forward slashes, lower case, no trailing separator."""
    return ntpath.normcase(ntpath.normpath(path)).replace("\\", "/").rstrip("/")


def _norm_Variants(path: str) -> set[str]:
    p = Path(path)
    out = {path.rstrip("/") or "/"}
    if p.exists():
        try:
            out.add(str(p.resolve()).rstrip("/") or "/")
        except OSError:
            pass
    if ce_common.WINDOWS:
        return {windows_key(variant) for variant in out}
    return out


def latest_session(path: str, projects: Path, codex_sessions: dict[str, dict] | None = None,
                   all_worktrees: list[str] | None = None) -> dict | None:
    """{"id", "age_minutes"} of the newest Claude or Codex session file of this folder, None if there is none."""
    folder = projects / project_dir_name(path)
    candidates: list[dict] = []
    try:
        files = [f for f in folder.glob("*.jsonl") if f.is_file()]
    except OSError:
        files = []
    if files:
        newest = max(files, key=lambda f: f.stat().st_mtime)
        candidates.append({"id": newest.stem, "age_minutes": (time.time() - newest.stat().st_mtime) / 60,
                           "title": session_title(newest)})
    if codex_sessions:
        target_variants = _norm_Variants(path)
        other_variants: set[str] = set()
        for wt in all_worktrees or []:
            wt_vars = _norm_Variants(wt)
            if not (wt_vars & target_variants):
                other_variants |= wt_vars
        for cwd_key, info in codex_sessions.items():
            norm_cwd = cwd_key.rstrip("/") or "/"
            if ce_common.WINDOWS:
                norm_cwd = windows_key(cwd_key)
            matched_targets = [t for t in target_variants if norm_cwd == t or norm_cwd.startswith(t + "/")]
            if not matched_targets:
                continue
            best_target_len = max(len(t) for t in matched_targets)
            deeper_other = any(
                (norm_cwd == o or norm_cwd.startswith(o + "/")) and len(o) > best_target_len
                for o in other_variants
            )
            if not deeper_other:
                candidates.append(info)
    if not candidates:
        return None
    return min(candidates, key=lambda item: item["age_minutes"])


def session_title(file: Path) -> str | None:
    """The newest customTitle/aiTitle/summary in the last 256 KB of a session file (None: none found)."""
    try:
        with file.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 262144))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"customTitle"' not in line and '"aiTitle"' not in line and '"type":"summary"' not in line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        title = data.get("customTitle") or data.get("aiTitle") or data.get("summary")
        if title:
            return str(title)
    return None


def process_cwds() -> dict[str, list[str]]:
    """{working directory: [command names]} of running claude/codex/antigravity processes (empty when lsof is unavailable)."""
    try:
        done = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fcn"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    found: dict[str, list[str]] = {}
    command = ""
    for line in done.stdout.splitlines():
        if line.startswith("c"):
            command = line[1:]
        elif line.startswith("n") and re.search(r"claude|codex|antigrav|gemini|language_server", command, re.I):
            found.setdefault(line[1:], []).append(command)
    return found


def worktree_state(item: dict, base: str, projects: Path, cwds: dict[str, list[str]], active_minutes: float,
                   codex_sessions: dict[str, dict] | None = None,
                   all_worktrees: list[str] | None = None) -> dict:
    path = Path(item["path"])
    state = dict(item, exists=path.is_dir(), dirty=None, ahead=None, behind=None, session=None, processes=[])
    if state["exists"]:
        status = ce_common.git_out(path, "status", "--porcelain")
        state["dirty"] = None if status is None else len(status.splitlines())
        counts = ce_common.git_out(path, "rev-list", "--left-right", "--count", f"{base}...HEAD")
        if counts:
            behind, ahead = counts.split()
            state["behind"], state["ahead"] = int(behind), int(ahead)
    state["session"] = latest_session(item["path"], projects, codex_sessions, all_worktrees)
    state["processes"] = cwds.get(item["path"], [])
    if ce_common.WINDOWS and not state["processes"]:  # (0.31) git's C:/x against C:\x of the process listing
        key = windows_key(item["path"])
        state["processes"] = [name for cwd, names in cwds.items() if windows_key(cwd) == key for name in names]
    reasons = []
    if state["processes"]:
        reasons.append("Prozess läuft: " + ", ".join(sorted(set(state["processes"]))))
    if state["session"] and state["session"]["age_minutes"] <= active_minutes:
        reasons.append(f"Sitzung aktiv ({state['session']['age_minutes']:.0f} min)")
    if state["dirty"]:
        reasons.append(f"{state['dirty']} offene Änderung(en)")
    if state["ahead"]:
        reasons.append(f"{state['ahead']} Commit(s) nicht in {base}")
    state["reasons"] = reasons
    state["protected"] = bool(reasons)
    return state


def collect(repos: list[Path], projects: Path, base: str = "main", active_minutes: float = 60,
            cwds: dict[str, list[str]] | None = None, codex_dir: Path | None = None) -> list[tuple[Path, list[dict]]]:
    cwds = process_cwds() if cwds is None else cwds
    codex_sessions = codex_sessions_by_cwd(codex_dir)
    repo_items: list[tuple[Path, list[dict] | None]] = []
    all_worktrees: list[str] = []
    for repo in repos:
        listing = ce_common.git_out(repo, "worktree", "list", "--porcelain") if repo.is_dir() else None
        if listing is None:
            repo_items.append((repo, None))
            continue
        items = [w for w in parse_worktrees(listing) if not w["bare"]]
        repo_items.append((repo, items))
        all_worktrees.extend(w["path"] for w in items)
    result = []
    for repo, items in repo_items:
        if items is None:
            result.append((repo, []))
        else:
            result.append((repo, [worktree_state(w, base, projects, cwds, active_minutes, codex_sessions, all_worktrees) for w in items]))
    return result


def format_state(state: dict) -> str:
    if not state["exists"]:
        return f"  {state['path']}  [Ordner fehlt{', prunable' if state['prunable'] else ''}]"
    clean = "?" if state["dirty"] is None else ("sauber" if not state["dirty"] else f"{state['dirty']} geändert")
    if state["ahead"] is None:
        track = "?"
    else:
        track = f"+{state['ahead']}/-{state['behind']}"
    session = "-"
    if state["session"]:
        session = f"{state['session']['id'][:8]} ({state['session']['age_minutes'] / 60:.0f} h her)"
        if state["session"]["age_minutes"] < 120:
            session = f"{state['session']['id'][:8]} ({state['session']['age_minutes']:.0f} min her)"
    mark = f"  <- {DO_NOT_DELETE} ({'; '.join(state['reasons'])})" if state["protected"] else ""
    return f"  {state['path']}\n    {state['branch'] or '(detached)'} | {clean} | {track} | Sitzung {session}{mark}"


def short_session(state: dict) -> str:
    session = state["session"]
    if not session:
        return "-"
    age = session["age_minutes"]
    when = f"{age:.0f} min" if age < 120 else f"{age / 60:.0f} h"
    title = (session.get("title") or session["id"][:8]).replace("\n", " ")
    return f"{title[:40]} ({when})"


def table_rows(collected) -> list[list[str]]:
    rows = [["Repo", "Worktree", "Branch", "Sitzung", "sauber?", "+ahead/-behind", "Hinweis"]]
    for repo, states in collected:
        for state in states:
            path = Path(state["path"])
            name = "(Hauptordner)" if path == repo else path.name
            if not state["exists"]:
                rows.append([repo.name, name, "-", "-", "Ordner fehlt", "-", "prunable" if state["prunable"] else ""])
                continue
            clean = "?" if state["dirty"] is None else ("ja" if not state["dirty"] else f"nein ({state['dirty']})")
            track = "?" if state["ahead"] is None else f"+{state['ahead']}/-{state['behind']}"
            note = f"{DO_NOT_DELETE}: {'; '.join(state['reasons'])}" if state["protected"] else ""
            rows.append([repo.name, name, state["branch"] or "(detached)", short_session(state), clean, track, note])
    return rows


def format_table(rows: list[list[str]]) -> str:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    lines = []
    for row in rows:
        lines.append(" | ".join(cell.ljust(w) for cell, w in zip(row, widths)) + " | " + row[-1])
        if row is rows[0]:
            lines.append("-+-".join("-" * w for w in widths) + "-+-" + "-" * 7)
    return "\n".join(line.rstrip() for line in lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repos", nargs="+", help="repositories (default: the eight standard repos from repos.json or CE_REPOS)")
    parser.add_argument("--base", default="main", help="base branch for ahead/behind")
    parser.add_argument("--active-minutes", type=float, default=60)
    parser.add_argument("--projects", default=str(HOME / ".claude" / "projects"), help="Claude Code session folder")
    parser.add_argument("--table", action="store_true", help="compact table (default output)")
    parser.add_argument("--list", action="store_true", help="the long per-worktree listing instead of the table")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    if args.repos:
        repos = [Path(r) for r in args.repos]
    elif os.environ.get("CE_REPOS"):
        repos = [Path(r) for r in os.environ["CE_REPOS"].split(os.pathsep) if r]
    else:
        repos = default_repos(HOME)
    collected = collect(repos, Path(args.projects), args.base, args.active_minutes)
    if args.json:
        print(json.dumps([{"repo": str(repo), "worktrees": states} for repo, states in collected], indent=1))
        return 0
    total = sum(len(states) for _, states in collected)
    protected = sum(1 for _, states in collected for s in states if s["protected"])
    print(f"{total} Worktree(s) in {len(collected)} Repo(s), {protected} geschützt ({DO_NOT_DELETE})")
    if not args.list:
        print(format_table(table_rows(collected)))
        return 0
    for repo, states in collected:
        print(f"{repo.name}:" + ("" if states else " (kein Repo oder keine Worktrees)"))
        for state in states:
            print(format_state(state))
    return 0


if __name__ == "__main__":
    try:  # (0.31) Windows: a cp1252 console or pipe cannot take the tool's „“ → ·
        import ce_common as _console
        _console.utf8_console()
    except ImportError:
        pass
    sys.exit(main(sys.argv[1:]))
