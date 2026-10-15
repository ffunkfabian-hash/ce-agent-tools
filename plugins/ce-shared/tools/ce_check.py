#!/usr/bin/env python3
"""ce_check.py quick|full [--repo PATH] [--verbose] - runs the commands of .ce-repo.json > check.

    "check": {"quick": ["cmd", "arg"], "full": [["cmd1", "a"], ["cmd2", "b"]], "rerun_failed": ["cmd", "--failed"]}

A mode is one argv or a list of argvs (run in order, all of them, from the repo root). `full` that fails runs the
optional "rerun_failed" argv once (a command that can repeat only what failed); the run counts as green only if that
repeat passes. The report starts with the result line, then the tail of every failed command (--verbose: all output).
Exit code: 0 green, 1 red, 2 configuration problem.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ce_common  # noqa: E402

TAIL_LINES = 25


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


def run_mode(root: Path, config: dict, mode: str) -> dict:
    check = config.get("check")
    if not isinstance(check, dict) or mode not in check:
        raise ce_common.RepoError(f'.ce-repo.json: check.{mode} missing')
    results = [run_one(argv, root) for argv in commands_of(check[mode])]
    failed = [r for r in results if r["code"] != 0]
    rerun = None
    if failed and mode == "full" and check.get("rerun_failed"):
        rerun = run_one(commands_of(check["rerun_failed"])[0], root)
        results.append(rerun)
    green = not failed or (rerun is not None and rerun["code"] == 0)
    return {"green": green, "results": results, "failed": failed, "rerun": rerun}


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
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["quick", "full"])
    parser.add_argument("--repo", help="repository (default: current directory)")
    parser.add_argument("--verbose", action="store_true", help="print the output of every command")
    args = parser.parse_args(argv)
    try:
        root, config = ce_common.open_repo(args.repo)
        outcome = run_mode(root, config, args.mode)
    except ce_common.RepoError as error:
        print(f"ce_check: {error}")
        return 2
    print(report(config.get("name", root.name), args.mode, outcome, args.verbose))
    return 0 if outcome["green"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
