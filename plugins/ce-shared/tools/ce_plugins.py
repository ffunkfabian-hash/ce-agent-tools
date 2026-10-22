#!/usr/bin/env python3
"""Keeps every Claude Code install of the ce-agent-tools plugins on one version (0.16).

    ./ce plugins check                 # lists the installs, exit 1 if one is stale or points to a missing worktree
    ./ce plugins fix                   # dry run: what would change
    ./ce plugins fix --apply           # backup of installed_plugins.json, then stale rows -> newest install, orphans removed
    ./ce plugins fix --apply --user-only   # also drop every project/local row, only the user install stays

Why: Claude Code keeps one `scope: project` row per path in ~/.claude/plugins/installed_plugins.json (main checkout, every
worktree), each pinned to the version current when it was written. `claude plugin update` moves the user row; with
`--scope project` it did not reach the rows of worktrees (22.10.2026: six rows stayed on 0.11/0.13 after the 0.15.1 rollout).
The fix copies version, installPath and commit of the newest install whose files exist onto stale rows of any scope (also onto
a newer row whose files are gone); it never downloads. Close Claude Code before --apply: it may rewrite the file itself.
If the marketplace clone names a newer version than any install, run `claude plugin marketplace update ce-agent-tools` and
`claude plugin update ce-shared@ce-agent-tools` first. Restart sessions afterwards. CE_PLUGINS_DIR overrides the folder (tests).
"""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

MARKETPLACE = "ce-agent-tools"
COPIED = ("version", "installPath", "gitCommitSha", "lastUpdated")
LOCAL_SCOPES = ("project", "local")


def plugins_dir() -> Path:
    if os.environ.get("CE_PLUGINS_DIR"):
        return Path(os.environ["CE_PLUGINS_DIR"])
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "plugins"


def version_key(text) -> tuple:
    parts = []
    core = str(text or "").split("-")[0].split("+")[0]  # 0.16.0-rc1 sorts with 0.16.0, not above it
    for piece in core.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else -1)
    return tuple(parts)


def load(folder: Path):
    path = folder / "installed_plugins.json"
    if not path.is_file():
        return path, None
    return path, json.loads(path.read_text(encoding="utf-8"))


def marketplace_versions(folder: Path) -> dict:
    """plugin name -> version named by the local clone of the marketplace (empty when there is no clone)"""
    manifest = folder / "marketplaces" / MARKETPLACE / ".claude-plugin" / "marketplace.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {p.get("name"): p.get("version") for p in data.get("plugins", []) if isinstance(p, dict) and p.get("version")}


def ours(data) -> dict:
    """plugin key -> list of install rows, only plugins of our marketplace and only the known list format"""
    plugins = (data or {}).get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return {}
    return {key: rows for key, rows in plugins.items() if key.endswith("@" + MARKETPLACE) and isinstance(rows, list)}


def has_files(row) -> bool:
    return isinstance(row, dict) and bool(row.get("installPath")) and Path(row["installPath"]).is_dir()


def template(rows: list):
    """the newest row whose files exist; the user row wins a tie"""
    usable = [r for r in rows if has_files(r)]
    if not usable:
        return None
    return max(usable, key=lambda r: (version_key(r.get("version")), r.get("scope") == "user"))


def orphan(row: dict) -> bool:
    return row.get("scope") in LOCAL_SCOPES and bool(row.get("projectPath")) and not Path(row["projectPath"]).is_dir()


def plan(data, market: dict, user_only=False) -> list:
    """(key, row, action, detail) for every row; action is ok, stale, broken, orphan, drop or ahead"""
    out = []
    for key, rows in ours(data).items():
        best = template(rows)
        name = key.split("@")[0]
        if market.get(name) and best and version_key(market[name]) > version_key(best.get("version")):
            out.append((key, None, "ahead", f"Marketplace nennt {market[name]}, neueste Installation {best.get('version')}"))
        has_user = any(isinstance(r, dict) and r.get("scope") == "user" and r.get("version") == (best or {}).get("version")
                       for r in rows)
        for row in rows:
            if not isinstance(row, dict):
                continue
            if orphan(row):
                out.append((key, row, "orphan", "Ordner fehlt"))
            elif user_only and has_user and row.get("scope") in LOCAL_SCOPES:
                out.append((key, row, "drop", "nur noch die Nutzer-Installation"))
            elif best and not has_files(row) and version_key(row.get("version")) > version_key(best.get("version")):
                out.append((key, row, "broken", f"Dateien von {row.get('version')} fehlen -> {best.get('version')}"))
            elif best and row.get("version") != best.get("version"):
                out.append((key, row, "stale", f"{row.get('version')} -> {best.get('version')}"))
            else:
                out.append((key, row, "ok", ""))
    return out


def apply(data, steps: list) -> int:
    changed = 0
    plugins = ours(data)
    bests = {key: template(rows) for key, rows in plugins.items()}  # before removing rows: an orphan may hold the newest files
    for key, row, action, _ in steps:
        if action in ("orphan", "drop"):
            plugins[key].remove(row)
            changed += 1
        elif action in ("stale", "broken"):
            best = bests[key]
            for field in COPIED:
                if field in best:
                    row[field] = best[field]
                else:
                    row.pop(field, None)
            changed += 1
    return changed


def write(path: Path, data) -> Path:
    backup = path.with_name(f"{path.name}.bak-{time.time_ns()}")
    shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def describe(key, row, action, detail) -> str:
    if row is None:
        return f"!! {key}: {detail} – zuerst `claude plugin marketplace update {MARKETPLACE}` und `claude plugin update {key}`"
    where = row.get("projectPath") or "(Nutzer)"
    mark = {"ok": "ok", "stale": "->", "broken": "!>", "orphan": "--", "drop": "xx"}[action]
    return f"{mark} {key} {row.get('scope')} {row.get('version')}  {where}" + (f"  [{detail}]" if detail else "")


def newest_known(folder: Path, name="ce-shared"):
    """the newest version of a plugin this machine has: marketplace clone or an install of that plugin whose files exist"""
    try:
        _, data = load(folder)
    except (OSError, ValueError):
        data = None
    rows = ours(data).get(f"{name}@{MARKETPLACE}", [])
    versions = [r.get("version") for r in rows if has_files(r) and r.get("version")]
    market = marketplace_versions(folder).get(name)
    if market:
        versions.append(market)
    return max(versions, key=version_key) if versions else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Claude-Code-Installationen der ce-agent-tools-Plugins auf einer Version halten")
    parser.add_argument("mode", choices=["check", "fix"])
    parser.add_argument("--apply", action="store_true", help="fix: wirklich schreiben (mit Sicherung; Claude Code möglichst vorher beenden)")
    parser.add_argument("--user-only", action="store_true", help="fix: Projekt-/Lokal-Einträge entfernen, nur die Nutzer-Installation bleibt")
    args = parser.parse_args(argv)
    folder = plugins_dir()
    path, data = load(folder)
    if data is None:
        print(f"Keine Plugin-Installationen gefunden ({path}).")
        return 0
    if not ours(data):
        print(f"Keine Installation aus {MARKETPLACE} in {path} (oder unbekanntes Format).")
        return 0
    steps = plan(data, marketplace_versions(folder), user_only=args.mode == "fix" and args.user_only)
    for step in steps:
        print(describe(*step))
    todo = [s for s in steps if s[2] in ("stale", "broken", "orphan", "drop")]
    if args.mode == "check":
        if todo or any(s[2] == "ahead" for s in steps):
            print(f"{len(todo)} Einträge weichen ab – `./ce plugins fix` zeigt, was sich ändert.")
            return 1
        print("Alle Installationen auf einer Version.")
        return 0
    if not todo:
        print("Nichts zu ändern.")
        return 0
    if not args.apply:
        print(f"{len(todo)} Einträge würden geändert (Probelauf; mit --apply schreiben).")
        return 0
    changed = apply(data, todo)
    backup = write(path, data)
    print(f"{changed} Einträge geändert, Sicherung {backup.name}. Laufende Sitzungen erst nach einem Neustart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
