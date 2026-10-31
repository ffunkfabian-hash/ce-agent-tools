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
--all: the four standard repos (CE_REPOS = os.pathsep-separated list overrides: ':', Windows ';').

0.20 "Lodestar": the same promise without a copy. A repo that names `tools_version` ({"version", "commit"}) in
.ce-repo.json keeps only the loader ce_loader.py as its ./ce (same header); the loader runs exactly that version from
CE_TOOLS, the installed plugin, a cache or the public repo - so the repo still does not depend on the plugin. For such a
repo sync writes ./ce only, --check reports a missing/edited loader or a malformed tools_version as error, an older
loader, another plugin version or a left-over embedded copy as warning. It never deletes the old copy.
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
              "ce_judge.py", "ce_api.py", "ce_api_budget.py", "ce_vibe.py", "ce"]
SYNC_FILES += ["ce_console.py", "ce_lock_status.py", "ce_blender_preflight.py", "ce_turnaround.py"]
SYNC_FILES += ["ce_postfach.py"]  # 0.14: ./ce session say|who, announcement of ce_session_name.py --reserve
SYNC_FILES += ["ce_plugins.py"]  # 0.16: ./ce plugins check|fix
SYNC_FILES += ["ce_tidy.py"]  # 0.18: ./ce tidy (needs ce_common, ce_worktrees)
SYNC_FILES += ["ce_loader.py"]  # 0.20: check of repos with tools_version, `tools pin` for a repo leaving its copy
LOADER, LOADER_TARGET = "ce_loader.py", "ce"  # 0.20: repos with tools_version keep only the loader, as ./ce
LEGACY_TOOLS_DIRS = ["scripts/ce_tools", "tools/ce_tools", "Tools/ce_tools"]
DATA_DIRS = ["contracts", "agents"]  # copied verbatim next to the tools (plugins/ce-shared/contracts -> <tools_dir>/contracts)
DEFAULT_SYNC_NAMES = ["cubeescape", "cubeescape-cubegen", "cubeescape-robogen", "cubeescape-toolgen"]
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


def uses_loader(config: dict) -> bool:
    return "tools_version" in config


def sync_loader(root: Path, source: Path) -> list[str]:
    new = embed((source / LOADER).read_text(encoding="utf-8"), plugin_version(source) or "?")
    path = root / LOADER_TARGET
    if path.is_file() and path.read_text(encoding="utf-8") == new:
        return []
    path.write_text(new, encoding="utf-8", newline="")  # (0.31) LF on Windows too, like the plugin
    path.chmod(0o755)
    return [LOADER_TARGET]


def check_loader(root: Path, config: dict, source: Path | None) -> list[tuple[str, str]]:
    """Findings for a repo without a copy: tools_version, its ./ce loader, left-over copies."""
    findings = []
    try:
        import ce_loader
        version, commit = ce_loader.parse_pin(config)
    except ImportError:
        version = commit = None
        findings.append(("error", f"{LOADER} missing next to ce_sync.py - cannot check tools_version"))
    except ce_loader.LoaderError as error:
        version = commit = None
        findings.append(("error", str(error)))
    path = root / LOADER_TARGET
    if not path.is_file():
        findings.append(("error", f"./{LOADER_TARGET}: loader missing - run ce_sync.py --repo {root}"))
    else:
        have, stored, original = unembed(path.read_text(encoding="utf-8"))
        if stored is None or digest(original) != stored:
            findings.append(("error", f"./{LOADER_TARGET}: edited by hand (checksum/header mismatch) - change "
                                      f"{LOADER} in the plugin, then run ce_sync.py"))
        elif source is not None and (source / LOADER).is_file() \
                and digest((source / LOADER).read_text(encoding="utf-8")) != stored:
            findings.append(("warning", f"./{LOADER_TARGET}: loader from ce-agent-tools {have}, plugin has "
                                        f"{plugin_version(source) or '?'} - run ce_sync.py"))
    if source is not None and version and plugin_version(source) != version:
        findings.append(("warning", f"tools_version {version} ({commit[:12]}), plugin has {plugin_version(source) or '?'}"))
    if "tools_dir" in config:
        findings.append(("warning", "tools_dir is ignored next to tools_version - remove it from .ce-repo.json"))
    for name in dict.fromkeys(LEGACY_TOOLS_DIRS + ([config["tools_dir"]] if config.get("tools_dir") else [])):
        if (root / name / "ce_check.py").is_file():
            findings.append(("warning", f"{name}: embedded copy left over next to tools_version - remove it"))
    agents = root / ".claude" / "agents"
    for link in sorted(agents.iterdir()) if agents.is_dir() else []:
        if link.is_symlink() and not link.exists():
            findings.append(("warning", f".claude/agents/{link.name}: link into a removed copy - remove it "
                                        f"(the plugin brings ce-shared:{link.stem})"))
    return findings


def sync(root: Path, config: dict, source: Path) -> list[str]:
    if uses_loader(config):
        return sync_loader(root, source)
    version = plugin_version(source) or "?"
    target = tools_dir_of(root, config)
    target.mkdir(parents=True, exist_ok=True)
    changed = []
    for name in SYNC_FILES:
        new = embed((source / name).read_text(encoding="utf-8"), version)
        path = target / name
        if not path.is_file() or path.read_text(encoding="utf-8") != new:
            path.write_text(new, encoding="utf-8", newline="")  # (0.31) Windows would write CRLF
            path.chmod(0o755)
            changed.append(name)
    for folder in DATA_DIRS:
        origin = source.parent / folder
        if not origin.is_dir():
            continue
        for item in sorted(p for p in origin.iterdir() if p.suffix in (".json", ".md")):
            path = target / folder / item.name
            path.parent.mkdir(parents=True, exist_ok=True)
            data = item.read_bytes()  # (0.31) byte copies, as check() compares them (Windows text mode writes CRLF)
            if not path.is_file() or path.read_bytes() != data:
                path.write_bytes(data)
                changed.append(f"{folder}/{item.name}")
    return changed


def check(root: Path, config: dict, source: Path | None = None) -> list[tuple[str, str]]:
    """Findings (level, text): error = missing or edited copy, warning = intact but differs from the plugin."""
    if uses_loader(config):
        return check_loader(root, config, source)
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
        return [Path(r) for r in os.environ["CE_REPOS"].split(os.pathsep) if r]  # (0.31) ":", Windows ";"
    found = ce_common.standard_repos(DEFAULT_SYNC_NAMES)
    if found:
        return found
    home = Path.home()
    return [home / "Claude" / "Cube Escape", home / "cubegen", home / "Claude" / "cubeescape-robogen",
            home / "Claude" / "cubeescape-toolgen"]


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
        name = config.get("name", root.name)
        if not args.check:
            hold = ce_common.repo_hold(root)
            if hold:
                print(f"{name}: BLOCKED (hold in repos.json: {hold})")
                if not args.all:
                    bad += 1
                continue
        if args.check:
            findings = check(root, config, source)
            bad += bool(findings)
            ok = "loader ok" if uses_loader(config) else "embedded tools ok"
            print(f"{name}: " + (ok if not findings else "DEVIATES"))
            for level, text in findings:
                print(f"  {level}: {text}")
        else:
            changed = sync(root, config, source)
            print(f"{name}: " + (f"updated {', '.join(changed)}" if changed else "up to date"))
    return 1 if bad else 0


if __name__ == "__main__":
    try:  # (0.31) Windows: a cp1252 console or pipe cannot take the tool's „“ → ·
        import ce_common as _console
        _console.utf8_console()
    except ImportError:
        pass
    sys.exit(main(sys.argv[1:]))
