#!/usr/bin/env python3
"""ce_check.py quick|full|nightly [--repo PATH] [--verbose] - runs the commands of .ce-repo.json > check.

    "check": {"quick": ["cmd", "arg"], "full": [["cmd1", "a"], ["cmd2", "b"]], "rerun_failed": ["cmd", "--failed"],
              "nightly": [["cmd", "--tier", "full"], ["bash", "scripts/check_output.sh"]]}

nightly (0.5.0): the bundled image stages of a repo - full tiers, bakes, golden hashes, determinism renders, full
regressions without a trigger - run once a day on main (the user or launchd starts it, never a GitHub workflow), not
before every commit. Repos without check.nightly report that and exit 2.

A mode is one argv or a list of argvs (run in order, all of them, from the repo root). `full` that fails runs the
optional "rerun_failed" argv once (a command that can repeat only what failed); the run counts as green only if that
repeat passes. The report starts with the result line, then the tail of every failed command (--verbose: all output).
quick is targeted by default (--no-targeted: not): it runs check.targeted if configured (else check.quick), and an
argv element "{files}" is replaced by the changed files (git diff HEAD + untracked). full first repeats what failed
in the last full run (check.rerun_failed, if the last recorded full run was red); if that still fails, the full run is
not started. A second full run within 30 minutes on an unchanged tree only gets a hint (never blocked): the full run
belongs once before finishing. State: ~/Library/Caches/CubeEscape/ce_check/ (CE_CHECK_CACHE moves it).
Exit code: 0 green, 1 red, 2 configuration problem.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ce_common  # noqa: E402

TAIL_LINES = 25
REPEAT_MINUTES = 30
REPEAT_HINT = "hint: voller Lauf nur einmal vor Abschluss - second full run within 30 minutes without a code change"


def cache_dir() -> Path:
    return Path(os.environ.get("CE_CHECK_CACHE") or Path.home() / "Library" / "Caches" / "CubeEscape" / "ce_check")


def changed_files(root: Path) -> list[str]:
    out = ce_common.git_out(root, "diff", "--name-only", "HEAD") or ""
    untracked = ce_common.git_out(root, "ls-files", "--others", "--exclude-standard") or ""
    return sorted({f for f in (out + "\n" + untracked).splitlines() if f})


def tree_hash(root: Path) -> str | None:
    """Hash of HEAD plus every uncommitted change (None: not a git repository)."""
    head = ce_common.git_out(root, "rev-parse", "HEAD")
    if head is None:
        return None
    digest = hashlib.sha256(head.encode())
    done = ce_common.git(root, "diff", "HEAD")
    digest.update(done.stdout.encode("utf-8", "replace"))
    for name in changed_files(root):
        path = root / name
        if path.is_file() and ce_common.git_out(root, "ls-files", "--error-unmatch", name) is None:
            digest.update(name.encode() + path.read_bytes())
    return digest.hexdigest()


def state_file(root: Path) -> Path:
    return cache_dir() / (hashlib.sha256(str(root).encode()).hexdigest()[:16] + ".json")


def load_state(root: Path) -> dict:
    try:
        return json.loads(state_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(root: Path, tree: str | None, green: bool) -> None:
    try:
        state_file(root).parent.mkdir(parents=True, exist_ok=True)
        state_file(root).write_text(json.dumps({"tree": tree, "time": time.time(), "green": green}), encoding="utf-8")
    except OSError:
        pass  # a missing cache only loses the hint


def commands_of(value) -> list[list[str]]:
    """A mode's value as a list of argvs (validated)."""
    if isinstance(value, list) and value and all(isinstance(part, str) for part in value):
        return [value]
    if isinstance(value, list) and value and all(isinstance(argv, list) and argv and all(isinstance(p, str) for p in argv)
                                                   for argv in value):
        return value
    raise ce_common.RepoError('.ce-repo.json: check.<mode> must be an argv list or a list of argv lists')


def run_one(argv: list[str], root: Path) -> dict:
    start = time.monotonic()
    try:
        done = subprocess.run(argv, cwd=root, capture_output=True, text=True)
        code, output = done.returncode, done.stdout + done.stderr
    except OSError as error:
        code, output = 127, f"cannot start {argv[0]}: {error}\n"
    return {"argv": argv, "code": code, "output": output, "seconds": time.monotonic() - start}


def expand(argvs: list[list[str]], files: list[str]) -> list[list[str]]:
    return [[part for item in argv for part in (files if item == "{files}" else [item])] for argv in argvs]


def run_mode(root: Path, config: dict, mode: str, targeted: bool = False) -> dict:
    check = config.get("check")
    key = "targeted" if targeted and mode == "quick" and "targeted" in (check or {}) else mode
    if not isinstance(check, dict) or key not in check:
        raise ce_common.RepoError(f'.ce-repo.json: check.{mode} missing')
    argvs = commands_of(check[key])
    if targeted:
        argvs = expand(argvs, changed_files(root))
    results = [run_one(argv, root) for argv in argvs]
    failed = [r for r in results if r["code"] != 0]
    rerun = None
    if failed and len(argvs) == 1 and mode == "full" and check.get("rerun_failed"):
        rerun = run_one(commands_of(check["rerun_failed"])[0], root)
        results.append(rerun)
    green = not failed or (rerun is not None and rerun["code"] == 0)
    return {"green": green, "results": results, "failed": failed, "rerun": rerun, "hints": []}


def run_full(root: Path, config: dict) -> dict:
    """full with the repeat rule: what failed last time first, and a hint for a pointless second full run."""
    tree, state, hints = tree_hash(root), load_state(root), []
    if state and tree and state.get("tree") == tree and time.time() - state.get("time", 0) < REPEAT_MINUTES * 60:
        hints.append(REPEAT_HINT)
    check = config.get("check") or {}
    if state and not state.get("green", True) and check.get("rerun_failed"):
        first = run_one(commands_of(check["rerun_failed"])[0], root)
        if first["code"] != 0:
            outcome = {"green": False, "results": [first], "failed": [first], "rerun": None, "hints": hints}
            outcome["hints"].append("full run skipped: the failures of the last run still fail (rerun_failed)")
            save_state(root, tree, False)
            return outcome
    outcome = run_mode(root, config, "full")
    outcome["hints"] = hints
    save_state(root, tree, outcome["green"])
    return outcome


def report(name: str, mode: str, outcome: dict, verbose: bool = False) -> str:
    seconds = sum(r["seconds"] for r in outcome["results"])
    total = len(outcome["results"])
    if outcome["green"]:
        head = f"{name} {mode}: GREEN ({total} command(s), {seconds:.0f} s)"
        if outcome["failed"]:
            head += " - after repeating only what failed"
    else:
        head = f"{name} {mode}: RED ({len(outcome['failed'])} of {total} command(s) failed, {seconds:.0f} s)"
    lines = [head]
    for result in outcome["results"]:
        mark = "ok  " if result["code"] == 0 else "FAIL"
        lines.append(f"  {mark} {' '.join(result['argv'])} (exit {result['code']}, {result['seconds']:.0f} s)")
    for result in outcome["results"]:
        if result["code"] != 0 or verbose:
            body = result["output"].rstrip().splitlines()
            shown = body if verbose else body[-TAIL_LINES:]
            lines.append(f"--- {' '.join(result['argv'])}" + ("" if verbose or len(body) <= TAIL_LINES
                                                               else f" (last {TAIL_LINES} of {len(body)} lines)"))
            lines.extend(shown)
    lines.extend(outcome.get("hints", []))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["quick", "full", "nightly"])
    parser.add_argument("--repo", help="repository (default: current directory)")
    parser.add_argument("--verbose", action="store_true", help="print the output of every command")
    parser.add_argument("--targeted", dest="targeted", action="store_true", default=None,
                        help="only for the changed files (check.targeted; default in quick)")
    parser.add_argument("--no-targeted", dest="targeted", action="store_false")
    args = parser.parse_args(argv)
    try:
        root, config = ce_common.open_repo(args.repo)
        if args.mode == "full":
            outcome = run_full(root, config)
        elif args.mode == "nightly":
            outcome = run_mode(root, config, "nightly")
        else:
            outcome = run_mode(root, config, "quick", targeted=args.targeted is not False)
    except ce_common.RepoError as error:
        print(f"ce_check: {error}")
        return 2
    print(report(config.get("name", root.name), args.mode, outcome, args.verbose))
    return 0 if outcome["green"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
