#!/usr/bin/env python3
"""ce_tidy.py [--apply | --nightly] [--json] [--repos PATH ...] [--category K ...] - leftovers of finished sessions (0.18).

Implements Nexogen regeln-details › Aufräumen nach dem Merge (user decision 23.10.2026). Default is a dry run: a table
Kategorie · Pfad · Größe · Grund · löschbar. `--apply` removes every row marked löschbar, `--nightly` only the rows of
categories whose repo entry says `"nightly": true` (ce_nightly.py), `--json` prints the rows for machines.

Categories:
  worktree  worktrees from ce_worktrees.collect: deletable only when merged (no commit main lacks), clean, without an
            active session (default 24 h) or a process inside, and below a worktree root (<main>/.claude/worktrees,
            ~/.codex/worktrees, .ce-repo.json › tidy.worktree_roots, CE_TIDY_WORKTREE_ROOTS). Removed with
            `git worktree remove` (never --force; ignored files go with it, the reason names them), then `git worktree prune`.
  befehl    repo-owned cleanup commands, .ce-repo.json › tidy.commands: [{"name", "probe": [argv], "apply": [argv],
            "nightly": bool, "timeout": s}], run in the main checkout (probe in the dry run, apply with --apply); argv must
            start with a script inside the main checkout, optionally behind python3/python/sh/bash - no other program.
  ausgabe   generator outputs, .ce-repo.json › tidy.paths: [{"name", "root", "glob": "*", "keep": N,
            "older_than_days": D, "nightly": bool}]: the newest `keep` entries stay, the rest only when older than D days;
            root inside the main checkout or ~/Library/Caches, nothing tracked by git.
  beleg     evidence stores outside the repos (~/Library/Caches/*/videos, tidy.evidence: [{"root", "nightly"}]): version
            folders (0.112, 0.112.1 …); the two newest versions stay, for a repo store also the repo version and the one before.
Never: main checkouts (or a folder holding one) and their Library, ~/Claude/_rettung, the home folder, anything named or
holding "NICHT LÖSCHEN", anything outside its configured root, repos with a hold in repos.json. Files go to the trash
(~/.Trash, CE_TRASH), never rm; a move across volumes is refused. An error stops only its category of that repo.
Exit code: 0 fine, 1 at least one error or failed removal.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ce_common  # noqa: E402
import ce_worktrees  # noqa: E402

CATEGORIES = ("worktree", "befehl", "ausgabe", "beleg")
MARKERS = ("nicht löschen", "nicht_löschen", "nicht-löschen", "nicht loeschen", "nicht_loeschen", "nicht-loeschen")
VERSION_DIR = re.compile(r"v?(\d+)\.(\d+)(?:\.\d+)*")
ACTIVE_HOURS = 24


class Context:
    """Paths the safety rules depend on; all overridable for tests (CE_TIDY_HOME, CE_HOME, CE_TRASH)."""

    def __init__(self, home: Path | None = None, mains: list[Path] | None = None):
        self.home = Path(home or os.environ.get("CE_TIDY_HOME") or Path.home()).resolve()
        base = Path(os.environ.get("CE_HOME") or self.home / "Claude")
        self.rescue = real(base / "_rettung")
        self.trash = Path(os.environ.get("CE_TRASH") or self.home / ".Trash")
        self.caches = real(self.home / "Library" / "Caches")
        self.mains = [real(m) for m in mains or []]


def real(path: Path) -> Path:
    """Absolute path with resolved parents but the last part kept, so a symlink is judged (and moved) as the link."""
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.realpath(path.parent)) / path.name if path.name else Path(os.path.realpath(path))


def inside(path: Path, root: Path) -> bool:
    """True when `path` lies strictly below `root` (both through real())."""
    path, root = real(path), real(root)
    return path != root and path.is_relative_to(root)


def has_marker(path: Path) -> bool:
    def marked(name: str) -> bool:
        return any(m in unicodedata.normalize("NFC", name).casefold() for m in MARKERS)
    if any(marked(part) for part in path.parts):
        return True
    try:
        return path.is_dir() and not path.is_symlink() and any(marked(child.name) for child in path.iterdir())
    except OSError:
        return True  # unreadable: better protected


def protected(path: Path, ctx: Context) -> str | None:
    """Why `path` may never be removed, None when the global rules allow it."""
    p = real(path)
    if p == ctx.home or p in ctx.home.parents or p == Path(p.anchor):
        return "Home-Ordner oder darüber"
    for main in ctx.mains:
        if p == main or main.is_relative_to(p):
            return "Haupt-Checkout"
        if p.is_relative_to(main) and "Library" in p.relative_to(main).parts[:2]:
            return "Library eines Haupt-Checkouts"
    if p == ctx.rescue or p.is_relative_to(ctx.rescue) or ctx.rescue.is_relative_to(p):
        return "_rettung"
    if has_marker(p):
        return "NICHT LÖSCHEN"
    return None


def size_of(path: Path) -> int | None:
    """Bytes on disk (du -skP, links not followed), None when unknown."""
    try:
        done = subprocess.run(["du", "-skP", str(path)], capture_output=True, text=True, timeout=300)
        return int(done.stdout.split()[0]) * 1024 if done.stdout.strip() else None
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def human(size: int | None) -> str:
    if size is None:
        return "?"
    for unit, step in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if size >= step:
            return f"{size / step:.1f} {unit}"
    return f"{size} B"


def row(category: str, repo: str, path, reason: str, deletable: bool, nightly: bool = False, size=None, **extra) -> dict:
    return dict(category=category, repo=repo, path=str(path), size=size, reason=reason, deletable=deletable,
                nightly=nightly, **extra)


def tidy_config(main: Path) -> dict:
    """.ce-repo.json › tidy of the main checkout ({} when the file or the entry is missing)."""
    if not (main / ce_common.CONFIG_NAME).is_file():
        return {}
    value = ce_common.load_config(main).get("tidy") or {}
    if not isinstance(value, dict):
        raise ce_common.RepoError(".ce-repo.json: tidy ist kein Objekt")
    return value


def entries(config: dict, key: str) -> list[dict]:
    value = config.get(key) or []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ce_common.RepoError(f".ce-repo.json: tidy.{key} ist keine Liste von Objekten")
    return value


def expand(root: str, main: Path, ctx: Context) -> Path:
    if root.startswith("~/"):
        return ctx.home / root[2:]
    path = Path(root)
    return path if path.is_absolute() else main / path


# --- category worktree -------------------------------------------------------------------------------------------

def worktree_roots(main: Path, config: dict, ctx: Context) -> list[Path]:
    roots = [main / ".claude" / "worktrees", ctx.home / ".codex" / "worktrees"]
    roots += [Path(p) for p in os.environ.get("CE_TIDY_WORKTREE_ROOTS", "").split(":") if p]
    extra = config.get("worktree_roots") or []
    if not isinstance(extra, list) or not all(isinstance(p, str) for p in extra):
        raise ce_common.RepoError(".ce-repo.json: tidy.worktree_roots ist keine Liste von Pfaden")
    roots += [expand(p, main, ctx) for p in extra]
    return [real(r) for r in roots]


def ignored_count(path: Path) -> int:
    listing = ce_common.git_out(path, "status", "--porcelain", "--ignored")
    return sum(1 for line in (listing or "").splitlines() if line.startswith("!!"))


def worktree_rows(repo: Path, main: Path, states: list[dict], config: dict, ctx: Context, cwds: dict,
                  base: str, active_minutes: float, sizes: bool, projects: Path) -> list[dict]:
    roots = worktree_roots(main, config, ctx)
    nightly = bool((config.get("worktrees") or {}).get("nightly")) if isinstance(config.get("worktrees"), dict) else False
    result = []
    for index, state in enumerate(states):
        path = Path(state["path"])
        if index == 0 or real(path) == main:
            continue  # the main checkout is never a leftover and is not listed
        if not state["exists"]:
            result.append(row("worktree", repo.name, path, "Ordner fehlt – git worktree prune", bool(state["prunable"]),
                              nightly, 0, main=str(main), kind="prune"))
            continue
        reasons = list(state["reasons"])
        if state["dirty"] is None:
            reasons.append("Status unbekannt")
        if state["ahead"] is None:
            reasons.append(f"nicht mit {base} vergleichbar")
        inner = processes_inside(path, cwds)
        if inner and not state["processes"]:
            reasons.append("Prozess im Ordner: " + ", ".join(inner))
        if not any(inside(path, r) for r in roots):
            reasons.append("außerhalb der Worktree-Wurzeln")
        guard = protected(path, ctx)
        if guard:
            reasons.append(guard)
        if reasons:
            reason = "; ".join(reasons)
        else:
            session = state["session"]
            last = f"letzte Sitzung vor {session['age_minutes'] / 60:.0f} h" if session else "keine Sitzung gefunden"
            reason = f"gemergt, sauber, {last}"
            ignored = ignored_count(path)
            if ignored:
                reason += f"; {ignored} ignorierte Einträge gehen mit"
        result.append(row("worktree", repo.name, path, reason, not reasons, nightly, size_of(path) if sizes else None,
                          main=str(main), kind="worktree", branch=state["branch"], roots=[str(r) for r in roots],
                          projects=str(projects), active_minutes=active_minutes))
    return result


def processes_inside(path: Path, cwds: dict) -> list[str]:
    return sorted({c for cwd, cmds in cwds.items() if real(Path(cwd)).is_relative_to(real(path)) for c in cmds})


def recheck(item: dict, ctx: Context, live: dict) -> None:
    """Right before a removal: every protection again, since the dry run may be old (raises when one applies now)."""
    for main in ([item["main"]] if item.get("main") else []) + list(item.get("mains") or []):
        hold = ce_common.repo_hold(Path(main), ctx.home)
        if hold:
            raise RuntimeError(f"inzwischen gesperrt: {hold}")
    if item.get("kind") in ("prune", "command"):
        return
    path = Path(item["path"])
    guard = protected(path, ctx)
    if guard:
        raise RuntimeError(f"geschützt: {guard}")
    if item.get("kind") != "worktree":
        return
    if not any(inside(path, Path(r)) for r in item["roots"]):
        raise RuntimeError("außerhalb der Worktree-Wurzeln")
    if "cwds" not in live:
        live["cwds"], live["codex"] = ce_worktrees.process_cwds(), ce_worktrees.codex_sessions_by_cwd()
    inner = processes_inside(path, live["cwds"])
    if inner:
        raise RuntimeError("inzwischen Prozess im Ordner: " + ", ".join(inner))
    session = ce_worktrees.latest_session(str(path), Path(item["projects"]), live["codex"])
    if session and session["age_minutes"] <= item["active_minutes"]:
        raise RuntimeError(f"inzwischen Sitzung aktiv ({session['age_minutes']:.0f} min)")


def remove_worktree(item: dict, base: str) -> str:
    main, path = Path(item["main"]), Path(item["path"])
    if item.get("kind") == "worktree":
        if ce_common.git_out(path, "status", "--porcelain") != "":
            raise RuntimeError("inzwischen nicht mehr sauber")
        if ce_common.git_out(path, "rev-list", "--count", f"{base}..HEAD") != "0":
            raise RuntimeError(f"inzwischen Commits, die {base} fehlen")
        done = ce_common.git(main, "worktree", "remove", str(path), timeout=600)
        if done.returncode:
            raise RuntimeError(done.stderr.strip() or "git worktree remove fehlgeschlagen")
    done = ce_common.git(main, "worktree", "prune")
    if done.returncode:
        raise RuntimeError(done.stderr.strip() or "git worktree prune fehlgeschlagen")
    return "entfernt" if item.get("kind") == "worktree" else "bereinigt"


# --- category befehl ---------------------------------------------------------------------------------------------

INTERPRETERS = ("python3", "python", "sh", "bash")


def argv_of(value, main: Path) -> list[str] | None:
    """The command as argv; only a script of the repo itself (directly or behind python3/sh/bash), never any other program."""
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise ce_common.RepoError("tidy.commands: probe/apply muss eine Liste von Wörtern sein")
    position = 1 if value[0] in INTERPRETERS else 0
    if len(value) <= position:
        raise ce_common.RepoError(f"tidy.commands: {value[0]} ohne Skript")
    script = value[position]
    target = real(main / script)
    if Path(script).is_absolute() or not inside(target, main) or not Path(os.path.realpath(target)).is_relative_to(main) \
            or not target.is_file():
        raise ce_common.RepoError(f"tidy.commands: {script} ist kein Skript im Haupt-Checkout (nur eigene Aufräumskripte)")
    return [*value[:position], str(target), *value[position + 1:]]


def run_command(argv: list[str], main: Path, timeout: float) -> tuple[int, str]:
    done = subprocess.run(argv, cwd=main, capture_output=True, text=True, timeout=timeout)
    lines = [line.strip() for line in (done.stdout + done.stderr).splitlines() if line.strip()]
    return done.returncode, " / ".join(lines[-3:])[:300]


def command_rows(repo: Path, main: Path, config: dict, probe: bool) -> list[dict]:
    result = []
    for entry in entries(config, "commands"):
        name = str(entry.get("name") or "befehl")
        apply = argv_of(entry.get("apply"), main)
        if apply is None:
            raise ce_common.RepoError(f"tidy.commands {name}: apply fehlt")
        check = argv_of(entry.get("probe"), main)
        timeout = float(entry.get("timeout") or 600)
        if check and probe:
            code, text = run_command(check, main, timeout)
            reason = f"{name}: {text or 'keine Ausgabe'}" + (f" (Exit {code})" if code else "")
        else:
            reason = f"{name}: {' '.join(entry['apply'])}" + ("" if check else " (kein Probelauf)")
        result.append(row("befehl", repo.name, main, reason, True, bool(entry.get("nightly")), None, kind="command",
                          argv=apply, timeout=timeout, main=str(main)))
    return result


# --- category ausgabe --------------------------------------------------------------------------------------------

def output_rows(repo: Path, main: Path, config: dict, ctx: Context, sizes: bool) -> list[dict]:
    result = []
    now = time.time()
    for entry in entries(config, "paths"):
        name = str(entry.get("name") or entry.get("root") or "ausgabe")
        if not isinstance(entry.get("root"), str) or not entry["root"]:
            raise ce_common.RepoError(f"tidy.paths {name}: root fehlt")
        root = real(expand(entry["root"], main, ctx))
        if not (inside(root, main) or inside(root, ctx.caches)):
            raise ce_common.RepoError(f"tidy.paths {name}: {root} liegt weder im Haupt-Checkout noch in ~/Library/Caches")
        if protected(root, ctx) == "_rettung" or has_marker(root):
            raise ce_common.RepoError(f"tidy.paths {name}: {root} ist geschützt")
        if not root.is_dir():
            continue
        try:
            keep, days = int(entry.get("keep") or 0), float(entry.get("older_than_days") or 0)
        except (TypeError, ValueError) as error:
            raise ce_common.RepoError(f"tidy.paths {name}: keep/older_than_days keine Zahl") from error
        if keep < 0 or days < 0:
            raise ce_common.RepoError(f"tidy.paths {name}: keep/older_than_days negativ")
        pattern = str(entry.get("glob") or "*")
        children = sorted((c for c in root.iterdir() if fnmatch.fnmatch(c.name, pattern)),
                          key=lambda c: c.lstat().st_mtime, reverse=True)
        for position, child in enumerate(children):
            age = (now - child.lstat().st_mtime) / 86400
            reasons = []
            if position < keep:
                reasons.append(f"unter den neuesten {keep}")
            if age < days:
                reasons.append(f"jünger als {days:g} Tage")
            if not inside(child, root):
                reasons.append("außerhalb der Wurzel")
            guard = protected(child, ctx)
            if guard:
                reasons.append(guard)
            if inside(child, main) and ce_common.git_out(main, "ls-files", "--", str(real(child).relative_to(main))):
                reasons.append("im Git verfolgt (Lieferstand)")
            reason = "; ".join(reasons) if reasons else f"{name}: {age:.0f} Tage alt"
            result.append(row("ausgabe", repo.name, child, reason, not reasons, bool(entry.get("nightly")),
                              size_of(child) if sizes else None, kind="trash", root=str(root), main=str(main)))
    return result


# --- category beleg ----------------------------------------------------------------------------------------------

def version_key(name: str) -> tuple[int, int] | None:
    match = VERSION_DIR.fullmatch(name)
    return (int(match.group(1)), int(match.group(2))) if match else None


def evidence_stores(repos: list[tuple[Path, Path, dict]], ctx: Context) -> dict[Path, dict]:
    """{store root: {"nightly", "repos", "mains", "currents"}} from the default glob and tidy.evidence; a store named by several
    repos keeps every repo (each one's version and hold count), and removes at night only when all of them say nightly."""
    stores: dict[Path, dict] = {}
    try:
        defaults = sorted(ctx.caches.glob("*/videos"))
    except OSError:
        defaults = []
    for store in defaults:
        stores[real(store)] = {"nightly": False, "repos": [], "mains": [], "currents": [], "default": True}
    for repo, main, config in repos:
        for entry in entries(config, "evidence"):
            if not isinstance(entry.get("root"), str) or not entry["root"]:
                raise ce_common.RepoError("tidy.evidence: root fehlt")
            store = real(expand(entry["root"], main, ctx))
            info = stores.setdefault(store, {"nightly": False, "repos": [], "mains": [], "currents": []})
            info["nightly"] = bool(entry.get("nightly")) and (not info["repos"] or info["nightly"])
            if info.pop("default", False):
                info["nightly"] = bool(entry.get("nightly"))
            info["repos"].append(repo.name)
            info["mains"].append(str(main))
            try:
                current = version_key(ce_common.read_version(main, ce_common.load_config(main)))
            except ce_common.RepoError:
                current = None
            if current:
                info["currents"].append(current)
    return stores


def evidence_rows(store: Path, info: dict, ctx: Context, sizes: bool) -> list[dict]:
    if not inside(store, ctx.home) or protected(store, ctx):
        raise ce_common.RepoError(f"Belegablage {store} ist geschützt oder liegt außerhalb des Home-Ordners")
    if not store.is_dir():
        return []
    folders = [(version_key(c.name), c) for c in store.iterdir() if c.is_dir() and not c.is_symlink()]
    known = sorted({key for key, _ in folders if key}, reverse=True)
    keep = set(known[:2])
    for current in info["currents"]:
        keep |= {current, *[k for k in known if k < current][:1]}
    result = []
    for key, child in sorted(folders, key=lambda item: (item[0] or (-1, -1), item[1].name)):
        if key is None:
            continue  # not a version folder: not evidence of a version, left alone
        reasons = []
        if key in keep:
            reasons.append("aktuelle oder vorige Version")
        guard = protected(child, ctx)
        if guard:
            reasons.append(guard)
        reason = "; ".join(reasons) if reasons else f"älter als {'.'.join(map(str, known[1]))}"
        result.append(row("beleg", ", ".join(info["repos"]) or "-", child, reason, not reasons, info["nightly"],
                          size_of(child) if sizes else None, kind="trash", root=str(store), mains=info["mains"]))
    return result


# --- collect, apply, output --------------------------------------------------------------------------------------

def to_trash(item: dict, ctx: Context) -> str:
    path = Path(item["path"])
    guard = protected(path, ctx)
    if guard or not inside(path, Path(item["root"])):
        raise RuntimeError(f"geschützt: {guard or 'außerhalb der Wurzel'}")
    if not ctx.trash.is_dir():
        raise RuntimeError(f"Papierkorb fehlt: {ctx.trash}")
    if os.lstat(path).st_dev != os.stat(ctx.trash).st_dev:
        raise RuntimeError("anderes Volume als der Papierkorb – nicht verschoben")
    target = ctx.trash / path.name
    if target.exists() or target.is_symlink():
        target = ctx.trash / f"{path.name} {dt.datetime.now():%Y-%m-%d %H.%M.%S.%f}"
    os.rename(path, target)
    return f"im Papierkorb: {target.name}"


def main_checkout(repo: Path, states: list[dict]) -> Path:
    return real(Path(states[0]["path"])) if states else real(repo)


def collect(repos: list[Path], categories=CATEGORIES, projects: Path | None = None, base: str = "main",
            active_minutes: float = ACTIVE_HOURS * 60, cwds: dict | None = None, ctx: Context | None = None,
            probe: bool = True, sizes: bool = True) -> tuple[list[dict], list[str], Context]:
    """(rows, errors, context) for the given repos; an error stops only one category of one repo."""
    errors: list[str] = []
    cwds = ce_worktrees.process_cwds() if cwds is None else cwds
    projects = projects or (Path.home() / ".claude" / "projects")
    collected = ce_worktrees.collect(repos, projects, base, active_minutes, cwds)
    mains = [main_checkout(repo, states) for repo, states in collected]
    ctx = ctx or Context()
    known = set(ctx.mains) | set(mains) | {real(r) for r in repos} | {real(r) for r in ce_worktrees.default_repos()}
    ctx.mains = sorted(known)
    rows: list[dict] = []
    configs = []
    for (repo, states), main in zip(collected, mains):
        if not repo.is_dir():
            errors.append(f"{repo}: kein Ordner")
            continue
        try:
            config = tidy_config(main)
        except ce_common.RepoError as error:
            errors.append(f"{repo.name}: {error}")
            config = {}
        configs.append((repo, main, config))
        hold = ce_common.repo_hold(main, ctx.home)
        steps = {"worktree": lambda: worktree_rows(repo, main, states, config, ctx, cwds, base, active_minutes, sizes, projects),
                 "befehl": lambda: command_rows(repo, main, config, probe),
                 "ausgabe": lambda: output_rows(repo, main, config, ctx, sizes)}
        for category, step in steps.items():
            if category not in categories:
                continue
            try:
                found = step()
            except Exception as error:  # noqa: BLE001 - one category never stops the others
                errors.append(f"{repo.name} {category}: {error}")
                continue
            if hold:
                for item in found:
                    item["deletable"], item["reason"] = False, f"Repo gesperrt: {hold}; {item['reason']}"
            rows += found
    if "beleg" in categories:
        try:
            stores = evidence_stores(configs, ctx)
        except Exception as error:  # noqa: BLE001
            errors.append(f"beleg: {error}")
            stores = {}
        for store, info in stores.items():
            try:
                found = evidence_rows(store, info, ctx, sizes)
                hold = next((h for h in (ce_common.repo_hold(Path(m), ctx.home) for m in info["mains"]) if h), None)
                for item in found if hold else []:
                    item["deletable"], item["reason"] = False, f"Repo gesperrt: {hold}; {item['reason']}"
                rows += found
            except Exception as error:  # noqa: BLE001
                errors.append(f"beleg {store}: {error}")
    return rows, errors, ctx


def apply(rows: list[dict], ctx: Context, base: str = "main", nightly_only: bool = False, cwds: dict | None = None) -> list[str]:
    """Carries out the deletable rows (only nightly ones with nightly_only); sets row["done"], returns errors."""
    errors = []
    live = {} if cwds is None else {"cwds": cwds, "codex": ce_worktrees.codex_sessions_by_cwd()}
    for item in rows:
        if not item["deletable"] or (nightly_only and not item["nightly"]):
            continue
        try:
            recheck(item, ctx, live)
            if item["kind"] in ("worktree", "prune"):
                item["done"] = remove_worktree(item, base)
            elif item["kind"] == "command":
                code, text = run_command(item["argv"], Path(item["main"]), item["timeout"])
                item["done"] = f"Exit {code}: {text}"
                if code:
                    raise RuntimeError(f"Exit {code}: {text}")
            else:
                item["done"] = to_trash(item, ctx)
        except Exception as error:  # noqa: BLE001 - one failed row never stops the rest
            item["done"] = f"Fehler: {error}"
            errors.append(f"{item['path']}: {error}")
    return errors


def short(path: str, ctx: Context) -> str:
    home = str(ctx.home)
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def format_table(rows: list[dict], ctx: Context) -> str:
    table = [["Kategorie", "Pfad", "Größe", "Grund", "löschbar"]]
    for item in rows:
        mark = "ja" if item["deletable"] else "nein"
        if item["deletable"] and item["nightly"]:
            mark += " (nachts)"
        if item.get("done"):
            mark += f" → {item['done']}"
        size = "?" if item["kind"] == "command" else human(item["size"])
        table.append([item["category"], short(item["path"], ctx), size, item["reason"], mark])
    widths = [max(len(r[i]) for r in table) for i in range(len(table[0]))]
    widths[3] = min(widths[3], 90)
    lines = []
    for line in table:
        lines.append(" | ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip())
        if line is table[0]:
            lines.append("-+-".join("-" * w for w in widths))
    return "\n".join(lines)


def summary(rows: list[dict]) -> str:
    free = [r for r in rows if r["deletable"]]
    total = sum(r["size"] or 0 for r in free)
    return f"{len(rows)} Einträge, {len(free)} löschbar, zusammen {human(total)}"


def default_repos() -> list[Path]:
    if os.environ.get("CE_REPOS"):
        return [Path(r) for r in os.environ["CE_REPOS"].split(":") if r]
    return ce_worktrees.default_repos()


def run(argv: list[str], out=print) -> int:
    parser = argparse.ArgumentParser(prog="ce tidy", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="remove every row marked löschbar (trash, git worktree remove)")
    mode.add_argument("--nightly", action="store_true", help="remove only rows of categories with nightly: true")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--repos", nargs="+", help="repositories (default: the standard repos, CE_REPOS overrides)")
    parser.add_argument("--category", action="append", choices=CATEGORIES, help="only these categories (repeatable)")
    parser.add_argument("--base", default="main", help="branch a worktree must be merged into (default main)")
    parser.add_argument("--active-hours", type=float, default=ACTIVE_HOURS, help="a session younger than this protects its worktree")
    parser.add_argument("--projects", default=str(Path.home() / ".claude" / "projects"), help="Claude Code session folder")
    parser.add_argument("--no-size", action="store_true", help="skip the sizes (faster)")
    args = parser.parse_args(argv)
    repos = [Path(r).expanduser() for r in args.repos] if args.repos else default_repos()
    rows, errors, ctx = collect(repos, tuple(args.category or CATEGORIES), Path(args.projects), args.base,
                                args.active_hours * 60, sizes=not args.no_size)
    if args.apply or args.nightly:
        errors += apply(rows, ctx, args.base, nightly_only=args.nightly)
    if args.json:
        out(json.dumps({"rows": rows, "errors": errors, "applied": bool(args.apply or args.nightly)}, indent=1,
                       ensure_ascii=False, default=str))
    else:
        title = "Aufräumen" if args.apply else "Nachtlauf-Aufräumen" if args.nightly else "Probelauf (nichts gelöscht; --apply räumt auf)"
        out(f"ce tidy – {title}: {summary(rows)}")
        if rows:
            out(format_table(rows, ctx))
        for error in errors:
            out(f"Fehler: {error}")
    return 1 if errors else 0


def main(argv: list[str]) -> int:
    return run(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
