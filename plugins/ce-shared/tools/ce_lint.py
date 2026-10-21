#!/usr/bin/env python3
"""ce_lint.py [--repo PATH] [--strict] - checks the repo against the shared standards (.ce-repo.json).

- limits: line counts of the files in "limits" (e.g. {"AGENTS.md": 150, "CLAUDE.md": 20}) - error when exceeded;
- AGENTS.md has the `##` sections Projekt, Wahrheit und Aktualität, Befehle, Regeln, Definition of Done, in this order
  (a heading may continue after the keyword: "Regeln und Verbote") - error when missing, warning when out of order;
- no VERSION_*.md / *_NOTES.md / *_PROGRESS.md style note files in the repo's main folder - error;
- version report standard ("docs.versions" folder): a report whose file name contains the current version or is
  <major>.<minor>.md - warning when the folder exists without one (no folder: silent, CHANGELOG-only repo); CHANGELOG ("docs.changelog") mentions the current version - warning when not.
- embedded tool copies (<tools_dir>, ce_sync.py): missing/edited = error, older than the plugin = warning.
Output: result line first, then one line per finding. Exit code 1 on errors (--strict: warnings too), 2 when the
configuration is unusable.
"""
from __future__ import annotations

import argparse
import fnmatch
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ce_common  # noqa: E402

REQUIRED_SECTIONS = ["Projekt", "Wahrheit und Aktualität", "Befehle", "Regeln", "Definition of Done"]
NOTE_PATTERNS = ["VERSION_*.md", "*_NOTES.md", "*_PROGRESS.md", "*_V2_NOTES.md", "NOTES*.md", "NOTIZ*.md"]


def count_lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def check_limits(root: Path, config: dict) -> list[tuple[str, str]]:
    findings = []
    for name, limit in (config.get("limits") or {}).items():
        if type(limit) is not int:
            continue  # policy flags such as no_media are not file line limits
        path = root / name
        if not path.is_file():
            findings.append(("error", f"{name}: file missing (limit {limit} lines)"))
            continue
        lines = count_lines(path)
        if lines > limit:
            findings.append(("error", f"{name}: {lines} lines, limit {limit} - move long text into docs/skills"))
    return findings


def agents_sections(text: str) -> list[str]:
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def check_sections(root: Path, config: dict | None = None) -> list[tuple[str, str]]:
    path = root / "AGENTS.md"
    if not path.is_file():
        return [("error", "AGENTS.md missing")]
    headings = agents_sections(path.read_text(encoding="utf-8", errors="replace"))
    required = ((config or {}).get("docs") or {}).get("agent_sections", REQUIRED_SECTIONS)
    if not isinstance(required, list) or not all(isinstance(name, str) for name in required):
        return [("error", "docs.agent_sections must be a list of heading names")]
    findings, positions = [], []
    for wanted in required:
        at = next((i for i, h in enumerate(headings) if h.lower().startswith(wanted.lower())), None)
        if at is None:
            findings.append(("error", f"AGENTS.md: section '## {wanted}' missing"))
        else:
            positions.append(at)
    if positions != sorted(positions):
        findings.append(("warning", "AGENTS.md: sections not in the standard order "
                         + " > ".join(required)))
    return findings


def check_note_files(root: Path) -> list[tuple[str, str]]:
    findings = []
    for entry in sorted(root.iterdir()):
        if entry.is_file() and any(fnmatch.fnmatch(entry.name.upper(), pattern.upper()) for pattern in NOTE_PATTERNS):
            findings.append(("error", f"{entry.name}: note/version file in the main folder - reports belong in "
                                      "docs.versions, decisions in ENTSCHEIDUNGEN.md"))
    return findings


def check_versions(root: Path, config: dict) -> list[tuple[str, str]]:
    try:
        version = ce_common.read_version(root, config)
    except ce_common.RepoError as error:
        return [("error", str(error))]
    findings = []
    docs = config.get("docs") or {}
    folder = docs.get("versions")
    if folder:
        reports = root / folder
        short = ".".join(version.split(".")[:2])  # 0.30.1 -> 0.30: a report per minor version
        if reports.is_dir() and not any(version in entry.name or entry.stem == short for entry in reports.iterdir()):
            findings.append(("warning", f"{folder}: no report for version {version} (or {short}.md)"))
    changelog = docs.get("changelog")
    if changelog:
        path = root / changelog
        if not path.is_file():
            findings.append(("warning", f"{changelog}: file missing"))
        elif version not in path.read_text(encoding="utf-8", errors="replace"):
            findings.append(("warning", f"{changelog}: no entry mentions version {version}"))
    return findings


def check_embedded(root: Path, config: dict) -> list[tuple[str, str]]:
    """Embedded tool copies (ce_sync.py): edited or missing = error, older than the plugin = warning."""
    import ce_sync
    return ce_sync.check(root, config, ce_sync.source_dir())


def lint(root: Path, config: dict) -> list[tuple[str, str]]:
    return (check_limits(root, config) + check_sections(root, config) + check_note_files(root) + check_versions(root, config)
            + check_embedded(root, config))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", help="repository (default: current directory)")
    parser.add_argument("--strict", action="store_true", help="warnings fail too")
    args = parser.parse_args(argv)
    try:
        root, config = ce_common.open_repo(args.repo)
    except ce_common.RepoError as error:
        print(f"ce_lint: {error}")
        return 2
    findings = lint(root, config)
    errors = [f for f in findings if f[0] == "error"]
    warnings = [f for f in findings if f[0] == "warning"]
    name = config.get("name", root.name)
    if errors:
        print(f"{name} lint: {len(errors)} error(s), {len(warnings)} warning(s)")
    elif warnings:
        print(f"{name} lint: ok with {len(warnings)} warning(s)")
    else:
        print(f"{name} lint: ok")
    for level, text in errors + warnings:
        print(f"  {level}: {text}")
    return 1 if errors or (args.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
