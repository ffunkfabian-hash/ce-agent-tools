#!/usr/bin/env python3
"""ce_docs.py [--repo PATH] [--json REPORT] - local Markdown links and anchors, skill front matter, JSON and shell syntax.

0.5.0: the one doc checker of the repos (before: Tools/check_docs.py in Cube Escape, scripts/check_docs.py in Toolgen and the
generators, check.py in cubeescape-nexogen). Configuration in .ce-repo.json > docs:

    "docs": {"versions": "docs/versions", "changelog": "CHANGELOG.md",
             "check": ["README.md", "AGENTS.md", "CLAUDE.md", "CHANGELOG.md", "docs/**/*.md", ".agents/skills/**/SKILL.md"],
             "json": [".claude/settings.json", "data/*.json"],
             "shell": ["scripts/*.sh", ".claude/hooks/*.sh"]}

"check" lists the Markdown files (globs, relative to the repo) whose local links are followed; the default is the list above
plus Docs/**/*.md. A link counts as local when it has no scheme and no host; its target must exist, and a fragment must be a
heading anchor (GitHub slug rule) or an explicit id of the target file. Links inside code fences and inline code are ignored.
Every SKILL.md needs front matter with name and description; "json" files must parse; "shell" files pass `bash -n`.
Repo-specific checks (Cube Escape's error register) stay in the repo and call check() from here.
Exit code: 0 ok, 1 findings, 2 configuration problem.

0.32 (Panorama 2c): warnings for patterns that go stale after a week - they print as "Warnung:" lines, count separately and never
change the exit code (JSON: "warnings"):
- "Agent Tools 0.<minor>" (also ce-agent-tools / ce-shared) below 0.30 in running text (docs.stale.agent_tools_below overrides 30),
- the old rules repo `cubeescape-rules`, the lock file `heavy-run.lock`, "eingebettet in jedem Repo",
- "Standardbranch `X`" in the text where the checkout's origin/HEAD names another branch (only if origin/HEAD is known).
Not scanned: CHANGELOG*, docs/versions/**, docs/pruefung*, code fences, and lines that say they are history (früher, ehemals,
vormals, abgelöst, ersetzt, alt, "ce-docs: ok")
and version mentions that are no claim about today: after ab/seit/since/from/aus/mit/mindestens/bis/nach/Stand, in
parentheses, in link text, or on a line with a date. docs.stale.ignore adds globs.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ce_common  # noqa: E402
import heavy_lock  # noqa: E402

DEFAULT_CHECK = ["README.md", "AGENTS.md", "CLAUDE.md", "CHANGELOG.md", "docs/**/*.md", "Docs/**/*.md",
                 ".agents/skills/**/SKILL.md", ".agents/skills/README.md"]


def visible_markdown(text: str) -> str:
    """The text without code fences and HTML comments (line count kept)."""
    lines, fence = [], None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
            lines.append("\n" if line.endswith("\n") else "")
        elif marker:
            fence = marker[1]
            lines.append("\n" if line.endswith("\n") else "")
        else:
            lines.append(line)
    return re.sub(r"<!--.*?-->", lambda m: "\n" * m[0].count("\n"), "".join(lines), flags=re.S)


def heading_anchors(text: str) -> set:
    text = visible_markdown(text)
    anchors = set(re.findall(r'<(?:a|h[1-6])\b[^>]*\b(?:id|name)=["\']([^"\']+)', text))
    lines, headings = text.splitlines(), []
    for i, line in enumerate(lines):
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            headings.append(match[1])
        elif i + 1 < len(lines) and line.strip() and re.match(r"^\s{0,3}(?:=+|-+)\s*$", lines[i + 1]):
            headings.append(line.strip())
    for heading in headings:
        heading = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", heading)
        heading = html.unescape(re.sub(r"<[^>]+>", "", heading)).lower()
        slug = "".join(c for c in heading if c.isalnum() or c in "_- ").replace(" ", "-")
        candidate, suffix = slug, 0
        while candidate in anchors:
            suffix += 1
            candidate = f"{slug}-{suffix}"
        anchors.add(candidate)
    return anchors


def links(text: str):
    """(line, destination) of every link, image and reference definition outside code."""
    text = visible_markdown(text)
    text = re.sub(r"(`+)([^\n]*?)\1", lambda m: " " * len(m[0]), text)
    definitions = {}
    for match in re.finditer(r'^\s{0,3}\[([^\]]+)\]:\s*(<[^>]*>|\S+)', text, re.M):
        definitions[match[1].strip().casefold()] = match[2].strip("<>")
        yield text.count("\n", 0, match.start()) + 1, match[2].strip("<>")
    # link text may hold one nested bracket pair ([![img](x)](y)); never runs across a following link
    inline = r'!?\[(?:[^\[\]\n]|\[[^\]\n]*\])*\]\(\s*(<[^>]*>|(?:[^\s()]|\([^()]*\))+)\s*(?:["\'][^\n]*?["\'])?\s*\)'
    for match in re.finditer(inline, text):
        yield text.count("\n", 0, match.start()) + 1, match[1].strip("<>")
    for match in re.finditer(r'!?\[([^\]\n]+)\]\[([^\]\n]*)\]', text):
        key = (match[2] or match[1]).strip().casefold()
        yield text.count("\n", 0, match.start()) + 1, definitions.get(key, f"!undefined-reference:{key}")


def expand(root: Path, patterns) -> list:
    files = set()
    for pattern in patterns:
        if any(ch in pattern for ch in "*?["):
            files.update(p for p in root.glob(pattern) if p.is_file())
        elif (root / pattern).is_file():
            files.add(root / pattern)
    return sorted(p for p in files if "__pycache__" not in p.parts)


def check(root: Path, config: dict | None = None) -> tuple:
    """(rows, problems): one row per checked file {path, sha256, local_links}, problems [{path, line, error}]."""
    root = Path(root).resolve()
    docs = (config or {}).get("docs") or {}
    rows, problems, anchor_cache = [], [], {}
    for path in expand(root, docs.get("check") or DEFAULT_CHECK):
        text = path.read_text(encoding="utf-8", errors="replace")
        row = {"path": str(path.relative_to(root)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "local_links": 0}
        rows.append(row)
        if path.name == "SKILL.md" and not (text.startswith("---\n") and "\nname:" in text and "\ndescription:" in text):
            problems.append({"path": row["path"], "line": 1, "error": "SKILL.md ohne Front Matter (name, description)"})
        for line, destination in links(text):
            error = None
            if destination.startswith("!undefined-reference:"):
                error = "Nicht definierter Referenzlink: " + destination.split(":", 1)[1]
            else:
                url = urlsplit(destination)
                if url.scheme or url.netloc:
                    continue
                row["local_links"] += 1
                target = (path.parent / unquote(url.path)).resolve() if url.path else path
                if not target.exists():
                    error = "Lokales Ziel fehlt: " + destination
                elif url.fragment:
                    if target.suffix == ".md" and target.is_file():
                        if target not in anchor_cache:
                            anchor_cache[target] = heading_anchors(target.read_text(encoding="utf-8", errors="replace"))
                        if unquote(url.fragment) not in anchor_cache[target]:
                            error = "Abschnittsanker fehlt: " + destination
                    else:
                        error = "Ankerziel ist kein Markdown-Dokument: " + destination
            if error:
                problems.append({"path": row["path"], "line": line, "error": error})
    for pattern in docs.get("json") or []:
        for path in expand(root, [pattern]):
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except ValueError as error:
                problems.append({"path": str(path.relative_to(root)), "line": 0, "error": f"JSON: {error}"})
    # (0.31) Windows: Git Bash - System32's bash.exe is WSL, another system with other paths
    bash = heavy_lock.posix_shell("bash") if ce_common.WINDOWS else "bash"
    for pattern in docs.get("shell") or []:
        for path in expand(root, [pattern]):
            if bash is None:
                problems.append({"path": str(path.relative_to(root)), "line": 0,
                                 "error": "Shell-Syntax nicht prüfbar: Git Bash fehlt (Git for Windows installieren)"})
                continue
            result = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True, check=False,
                                    **ce_common.UTF8)
            if result.returncode:
                problems.append({"path": str(path.relative_to(root)), "line": 0, "error": "Shell-Syntax: " + result.stderr.strip()})
    return rows, problems


STALE_IGNORE = ["CHANGELOG*", "docs/versions/**", "Docs/versions/**", "docs/pruefung*"]
HISTORY = re.compile(r"früher|ehemals|vormals|abgelöst|\bersetzt\b|\balt(?:e[nrms]?)?\b|ce-docs:\s*ok", re.I)
STALE_PATTERNS = [
    (re.compile(r"cubeescape-rules"), "`cubeescape-rules` heißt jetzt cubeescape-nexogen"),
    (re.compile(r"heavy-run\.lock"), "`heavy-run.lock` ist nur noch die alte Gesamtsperre (Sperre je Ressource: `./ce lock <res>`)"),
    (re.compile(r"eingebettet\w*\s+in\s+jedem\s+Repo", re.I), "„eingebettet in jedem Repo“: Werkzeuge kommen über den Lader (`./ce`)"),
]
AGENT_TOOLS = re.compile(r"(?:Agent[ -]Tools|ce-agent-tools|ce-shared)\s+v?0\.(\d+)(?:\.\d+)?", re.I)
# a version named as a minimum, start or dated fact is no claim about today
VERSION_CONTEXT = re.compile(r"(?:\b(?:ab|seit|since|from|aus|mit|mindestens|min|bis|nach|stand)\b\.?|[≥(])\s*$", re.I)
DATED = re.compile(r"\d{1,2}\.\d{1,2}\.(?:\d{2}){1,2}\b|\d{4}-\d\d-\d\d")


def version_claim(line: str, match) -> bool:
    """True when `match` (an Agent-Tools version) reads like a statement about the current state."""
    if DATED.search(line) or VERSION_CONTEXT.search(line[:match.start()]) or line[match.end():].lstrip().startswith("„"):
        return False                                                  # a release named with its codename is history
    return not (line.rfind("[", 0, match.start()) > line.rfind("]", 0, match.start()))   # inside link text


DEFAULT_BRANCH = re.compile(r"Standardbranch\s*[:=]?\s*`([^`]+)`", re.I)


def origin_head(root: Path) -> str:
    """The branch origin/HEAD points to in this checkout, "" if unknown (no remote, not a git repo, git missing)."""
    try:
        done = subprocess.run(["git", "-C", str(root), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                              capture_output=True, text=True, timeout=20, **ce_common.UTF8)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip().removeprefix("origin/") if done.returncode == 0 else ""


def stale(root: Path, config: dict | None = None) -> list:
    """[{path, line, warning}] for patterns that go stale (see the module docstring); never raises on unreadable files."""
    root = Path(root).resolve()
    docs = (config or {}).get("docs") or {}
    options = docs.get("stale") if isinstance(docs.get("stale"), dict) else {}
    below = options.get("agent_tools_below") if type(options.get("agent_tools_below")) is int else 30
    ignored = {p for p in expand(root, STALE_IGNORE + [g for g in options.get("ignore") or [] if isinstance(g, str)])}
    branch, warnings = origin_head(root), []
    for path in expand(root, docs.get("check") or DEFAULT_CHECK):
        if path in ignored:
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        raw_lines = raw.splitlines()
        for number, line in enumerate(visible_markdown(raw).splitlines(), 1):   # same line numbers; the marker may sit in a comment
            if HISTORY.search(line) or (number <= len(raw_lines) and HISTORY.search(raw_lines[number - 1])):
                continue
            found = [message for pattern, message in STALE_PATTERNS if pattern.search(line)]
            found += [f"„{m[0].strip()}“ ist alt (Agent Tools ab 0.{below} gilt als aktuell)" for m in AGENT_TOOLS.finditer(line)
                      if int(m[1]) < below and version_claim(line, m)]
            if branch:
                found += [f"Standardbranch `{m[1]}` im Text, GitHub-Standard (origin/HEAD) ist `{branch}`"
                          for m in DEFAULT_BRANCH.finditer(line) if m[1] != branch]
            warnings += [{"path": str(path.relative_to(root)), "line": number, "warning": message} for message in found]
    return warnings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", help="repository (default: current directory)")
    parser.add_argument("--json", help="write {files, problems} to this path")
    args = parser.parse_args(argv)
    try:
        root, config = ce_common.open_repo(args.repo)
    except ce_common.RepoError as error:
        print(f"ce_docs: {error}")
        return 2
    rows, problems = check(root, config)
    try:  # a warning scan must never break the check
        warnings = stale(root, config)
    except Exception as error:  # noqa: BLE001
        warnings = []
        print(f"Warnung: Veralterungsprüfung übersprungen ({error.__class__.__name__}: {error})")
    if args.json:
        Path(args.json).write_text(json.dumps({"files": rows, "problems": problems, "warnings": warnings}, indent=2,
                                              ensure_ascii=False) + "\n", encoding="utf-8")
    for problem in problems:
        print(f"{problem['path']}:{problem['line']}: {problem['error']}")
    for warning in warnings:
        print(f"Warnung: {warning['path']}:{warning['line']}: {warning['warning']}")
    print(f"{len(rows)} Dateien erfasst, {sum(r['local_links'] for r in rows)} lokale Verweise geprüft, {len(problems)} Befunde"
          + (f", {len(warnings)} Warnungen (nur Hinweis)" if warnings else ""))
    return 1 if problems else 0


if __name__ == "__main__":
    try:  # (0.31) Windows: a cp1252 console or pipe cannot take the tool's „“ → ·
        import ce_common as _console
        _console.utf8_console()
    except ImportError:
        pass
    sys.exit(main(sys.argv[1:]))
