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
    if args.json:
        Path(args.json).write_text(json.dumps({"files": rows, "problems": problems}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for problem in problems:
        print(f"{problem['path']}:{problem['line']}: {problem['error']}")
    print(f"{len(rows)} Dateien erfasst, {sum(r['local_links'] for r in rows)} lokale Verweise geprüft, {len(problems)} Befunde")
    return 1 if problems else 0


if __name__ == "__main__":
    try:  # (0.31) Windows: a cp1252 console or pipe cannot take the tool's „“ → ·
        import ce_common as _console
        _console.utf8_console()
    except ImportError:
        pass
    sys.exit(main(sys.argv[1:]))
