#!/usr/bin/env python3
"""ce_nightly.py [--all | --repo PATH ...] [--dry-run] [install|uninstall|status] - the one nightly run for all repos.

0.5.0: runs `ce_check.py nightly` of every repo in turn (CE_REPOS = ':'-separated paths, else the standard checkouts under
~/Claude), each inside its own heavy locks, writes one report to ~/Library/Caches/CubeEscape/nightly/ce_nightly-<date>.log and
sends a macOS notification. Repos without check.nightly are skipped with a note. Never a GitHub workflow (private repos pay minutes).

    python3 ce_nightly.py --all                   run now, all repos
    python3 ce_nightly.py --repo ~/Claude/cubeescape-toolgen
    python3 ce_nightly.py install [--hour 3]      LaunchAgent com.cubeescape.ce-nightly (launchd runs a missed time after wake)
    python3 ce_nightly.py uninstall | status

Exit code: 0 every nightly green, 1 at least one red, 2 nothing to run.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
DEFAULT_REPOS = ["cubeescape", "cubeescape-toolgen", "cubeescape-robogen", "cubeescape-cubegen", "cubeescape-formgen",
                 "cubeescape-motiongen"]
LOG_DIR = HOME / "Library" / "Caches" / "CubeEscape" / "nightly"
LABEL = "com.cubeescape.ce-nightly"


def repos(args) -> list[Path]:
    if args.repo:
        return [Path(p).expanduser().resolve() for p in args.repo]
    env = os.environ.get("CE_REPOS")
    if env:
        return [Path(p).expanduser().resolve() for p in env.split(":") if p]
    base = Path(os.environ.get("CE_HOME", HOME / "Claude"))
    return [base / name for name in DEFAULT_REPOS if (base / name).is_dir()]


def has_nightly(root: Path) -> bool:
    try:
        import json
        return "nightly" in (json.loads((root / ".ce-repo.json").read_text(encoding="utf-8")).get("check") or {})
    except (OSError, ValueError):
        return False


def run_one(root: Path, log, dry_run: bool) -> tuple[str, float]:
    tools = root / "Tools" / "ce_tools" / "ce_check.py"
    if not tools.is_file():
        tools = root / "scripts" / "ce_tools" / "ce_check.py"
    if not tools.is_file():
        tools = root / "tools" / "ce_tools" / "ce_check.py"
    command = [sys.executable, str(tools if tools.is_file() else HERE / "ce_check.py"), "nightly", "--repo", str(root)]
    log(f"=== {root.name}: {' '.join(command)}")
    if dry_run:
        return "dry-run", 0.0
    start = time.monotonic()
    with subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
        for line in proc.stdout:
            log(line.rstrip("\n"))
        code = proc.wait()
    return ("green" if code == 0 else f"red (exit {code})"), time.monotonic() - start


def notify(title: str, text: str) -> None:
    if shutil.which("osascript"):
        subprocess.run(["osascript", "-e", f'display notification "{text}" with title "{title}"'], check=False)


def run(args) -> int:
    targets = [r for r in repos(args)]
    if not targets:
        print("ce_nightly: keine Repos gefunden (CE_REPOS setzen oder --repo)")
        return 2
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    report = LOG_DIR / f"ce_nightly-{dt.date.today():%Y-%m-%d}.log"
    handle = open(report, "a", encoding="utf-8")

    def log(line: str) -> None:
        print(line, flush=True)
        handle.write(line + "\n")
        handle.flush()

    log(f"ce_nightly {dt.datetime.now():%Y-%m-%d %H:%M} - {len(targets)} Repos")
    results = []
    for root in targets:
        if not has_nightly(root):
            log(f"=== {root.name}: kein check.nightly in .ce-repo.json - übersprungen")
            results.append((root.name, "übersprungen", 0.0))
            continue
        state, seconds = run_one(root, log, args.dry_run)
        results.append((root.name, state, seconds))
    log("--- Ergebnis")
    for name, state, seconds in results:
        log(f"{name:24} {state:14} {seconds / 60:5.1f} min")
    handle.close()
    red = [name for name, state, _ in results if state.startswith("red")]
    notify("CE Nachtlauf", ("rot: " + ", ".join(red)) if red else "alle grün")
    print(f"Bericht: {report}")
    return 1 if red else 0


def plist_path() -> Path:
    return HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install(hour: int) -> int:
    plist = {"Label": LABEL, "ProgramArguments": [sys.executable, str(Path(__file__).resolve()), "--all"],
             "StartCalendarInterval": {"Hour": hour, "Minute": 0}, "StandardOutPath": str(LOG_DIR / "launchd.out"),
             "StandardErrorPath": str(LOG_DIR / "launchd.err"), "EnvironmentVariables": {"PATH": os.environ.get("PATH", "")}}
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    plist_path().parent.mkdir(parents=True, exist_ok=True)
    with open(plist_path(), "wb") as handle:
        plistlib.dump(plist, handle)
    if shutil.which("launchctl"):
        subprocess.run(["launchctl", "unload", str(plist_path())], check=False, capture_output=True)
        subprocess.run(["launchctl", "load", str(plist_path())], check=False)
    print(f"installiert: {plist_path()} ({hour:02d}:00 täglich)")
    return 0


def uninstall() -> int:
    if plist_path().exists():
        if shutil.which("launchctl"):
            subprocess.run(["launchctl", "unload", str(plist_path())], check=False, capture_output=True)
        plist_path().unlink()
        print("entfernt")
    else:
        print("nicht installiert")
    return 0


def status() -> int:
    print("installiert" if plist_path().exists() else "nicht installiert")
    logs = sorted(LOG_DIR.glob("ce_nightly-*.log"))
    if logs:
        print(f"letzter Bericht: {logs[-1]}")
        print("\n".join(logs[-1].read_text(encoding="utf-8").strip().splitlines()[-8:]))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["run", "install", "uninstall", "status"], default="run")
    parser.add_argument("--all", action="store_true", help="all repos (default when no --repo is given)")
    parser.add_argument("--repo", action="append", help="repository path (repeatable)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--hour", type=int, default=3)
    args = parser.parse_args(argv)
    if args.command == "install":
        return install(args.hour)
    if args.command == "uninstall":
        return uninstall()
    if args.command == "status":
        return status()
    return run(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
