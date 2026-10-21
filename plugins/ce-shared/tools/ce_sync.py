#!/usr/bin/env python3
"""ce_sync.py [--repo PATH | --all] [--check] - embedded copies of the shared tools in each repo.

The repos must not depend on the plugin at run time (Codex, cloud sessions and worktrees do not have it). So the tools
are copied into <repo>/<tools_dir> (.ce-repo.json "tools_dir", default scripts/ce_tools), standard library only, imports
relative to their own folder. Every copy starts with

    # GENERATED from ce-agent-tools <version> - do not edit
    # ce-sha256: <sha256 of the original file>

--check changes nothing and exits 1 when a copy is missing, was edited (checksum mismatch: error), or differs from the
plugin's current file (older version: warning in ce_lint, failure here). Without a plugin next to it (an embedded copy
of this script) --check verifies the checksums only, plus the plugin if CE_TOOLS or the default plugin path exists.
--all: the four standard repos (CE_REPOS = ':'-separated list overrides).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ce_common  # noqa: E402

SYNC_FILES = ["heavy_lock.py", "ce_common.py", "ce_check.py", "ce_lint.py", "ce_release.py", "ce_worktrees.py",
              "ce_sync.py", "ce_docs.py", "ce_nightly.py", "ce_session_name.py", "ce_await_mail.py",
              "ce_codex_quota.py", "ce_codex_cloud.py", "ce_contract.py", "ce_interfaces.py", "ce_search.py", "ce_report.py", "ce_imagediff.py", "ce_media.py",
              "ce_judge.py", "ce_api.py", "ce_api_budget.py", "ce"]
DATA_DIRS = ["contracts", "agents"]  # copied verbatim next to the tools (plugins/ce-shared/contracts -> <tools_dir>/contracts)
DEFAULT_TOOLS_DIR = "scripts/ce_tools"
DEFAULT_PLUGIN_TOOLS = Path.home() / ".claude" / "plugins" / "marketplaces" / "ce-agent-tools" / "plugins" / "ce-shared" / "tools"
HEADER = "# GENERATED from ce-agent-tools {version} - do not edit"
SUM_LINE = "# ce-sha256: {digest}"
HEADER_RE = re.compile(r"^# GENERATED from ce-agent-tools (\S+) - do not edit$")
SUM_RE = re.compile(r"^# ce-sha256: ([0-9a-f]{64})$")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plugin_version(source: Path) -> str | None:
    try:
        return json.loads((source.parent / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return None


def source_dir() -> Path | None:
    """The plugin's tools folder: this folder when it is the plugin (plugin.json above), else CE_TOOLS / default path."""
    here = Path(__file__).resolve().parent
    candidates = [here, Path(os.environ["CE_TOOLS"]) if os.environ.get("CE_TOOLS") else None, DEFAULT_PLUGIN_TOOLS]
    for folder in candidates:
        if folder and plugin_version(folder) and all((folder / name).is_file() for name in SYNC_FILES):
            return folder
    return None


def embed(original: str, version: str) -> str:
    """The original file with the two header lines (after a shebang, if any)."""
    lines = original.split("\n", 1) if original.startswith("#!") else None
    header = HEADER.format(version=version) + "\n" + SUM_LINE.format(digest=digest(original)) + "\n"
    if lines:
        return lines[0] + "\n" + header + (lines[1] if len(lines) > 1 else "")
    return header + original


def unembed(copy: str) -> tuple[str | None, str | None, str]:
    """(version, stored digest, original text) of an embedded copy; version/digest None when the header is missing."""
    shebang = ""
    body = copy
    if copy.startswith("#!"):
        shebang, _, body = copy.partition("\n")
        shebang += "\n"
    first, _, rest = body.partition("\n")
    second, _, rest2 = rest.partition("\n")
    head, tail = HEADER_RE.match(first), SUM_RE.match(second)
    if not head or not tail:
        return None, None, copy
    return head.group(1), tail.group(1), shebang + rest2


def tools_dir_of(root: Path, config: dict) -> Path:
    return root / config.get("tools_dir", DEFAULT_TOOLS_DIR)


def sync(root: Path, config: dict, source: Path) -> list[str]:
    version = plugin_version(source) or "?"
    target = tools_dir_of(root, config)
    target.mkdir(parents=True, exist_ok=True)
    changed = []
    for name in SYNC_FILES:
        new = embed((source / name).read_text(encoding="utf-8"), version)
        path = target / name
        if not path.is_file() or path.read_text(encoding="utf-8") != new:
            path.write_text(new, encoding="utf-8")
            path.chmod(0o755)
            changed.append(name)
    for folder in DATA_DIRS:
        origin = source.parent / folder
        if not origin.is_dir():
            continue
        for item in sorted(p for p in origin.iterdir() if p.suffix in (".json", ".md")):
            path = target / folder / item.name
            path.parent.mkdir(parents=True, exist_ok=True)
            text = item.read_text(encoding="utf-8")
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
                changed.append(f"{folder}/{item.name}")
    return changed


def check(root: Path, config: dict, source: Path | None = None) -> list[tuple[str, str]]:
    """Findings (level, text): error = missing or edited copy, warning = intact but differs from the plugin."""
    target = tools_dir_of(root, config)
    where = target.relative_to(root) if target.is_relative_to(root) else target
    findings = []
    if not target.is_dir():
        return [("error", f"{where}: embedded tools missing - run ce_sync.py --repo {root}")]
    for name in SYNC_FILES:
        path = target / name
        if not path.is_file():
            findings.append(("error", f"{where}/{name}: missing - run ce_sync.py"))
            continue
        version, stored, original = unembed(path.read_text(encoding="utf-8"))
        if stored is None or digest(original) != stored:
            findings.append(("error", f"{where}/{name}: edited by hand (checksum/header mismatch) - change the plugin, "
                                      "then run ce_sync.py"))
            continue
        if source is not None:
            current = (source / name).read_text(encoding="utf-8")
            if digest(current) != stored:
                findings.append(("warning", f"{where}/{name}: from ce-agent-tools {version}, plugin has "
                                            f"{plugin_version(source) or '?'} - run ce_sync.py"))
    for folder in DATA_DIRS:
        origin = source.parent / folder if source else target / folder
        if not origin.is_dir():
            findings.append(("error", f"{where}/{folder}: missing data directory"))
            continue
        for item in sorted(origin.iterdir()):
            if item.suffix not in (".json", ".md"):
                continue
            copy = target / folder / item.name
            if not copy.is_file() or copy.read_bytes() != item.read_bytes():
                findings.append(("error", f"{where}/{folder}/{item.name}: missing or changed - run ce_sync.py"))
    return findings


def repos_of(args) -> list[Path]:
    if args.repo:
        return [Path(args.repo)]
    if os.environ.get("CE_REPOS"):
        return [Path(r) for r in os.environ["CE_REPOS"].split(":") if r]
    import ce_worktrees
    return list(ce_worktrees.DEFAULT_REPOS)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", help="repository (default: current directory)")
    parser.add_argument("--all", action="store_true", help="the four standard repos")
    parser.add_argument("--check", action="store_true", help="change nothing, exit 1 on deviating copies")
    args = parser.parse_args(argv)
    if not args.all and not args.repo:
        args.repo = "."
    source = source_dir()
    if not args.check and source is None:
        print("ce_sync: plugin tools not found (set CE_TOOLS) - cannot write copies")
        return 2
    bad = 0
    for repo in repos_of(args):
        try:
            root, config = ce_common.open_repo(str(repo))
        except ce_common.RepoError as error:
            print(f"{repo.name}: {error}")
            bad += 1
            continue
        if args.check:
            findings = check(root, config, source)
            bad += bool(findings)
            print(f"{config.get('name', root.name)}: " + ("embedded tools ok" if not findings else "DEVIATES"))
            for level, text in findings:
                print(f"  {level}: {text}")
        else:
            changed = sync(root, config, source)
            print(f"{config.get('name', root.name)}: " + (f"updated {', '.join(changed)}" if changed else "up to date"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
