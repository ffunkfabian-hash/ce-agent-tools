#!/usr/bin/env python3
"""ce_nightly.py [--all | --repo PATH ...] [--dry-run] [install|uninstall|status] - the one nightly run for all repos.

0.5.0: runs `ce_check.py nightly` of every repo in turn (CE_REPOS = ':'-separated paths, else the standard checkouts under
~/Claude), each inside its own heavy locks, writes one report to ~/Library/Caches/CubeEscape/nightly/ce_nightly-<date>.log and
sends a macOS notification. Repos without check.nightly are skipped with a note. 0.18: afterwards `ce_tidy.py --nightly` (removes
only categories with nightly: true in .ce-repo.json › tidy, lists the rest; --dry-run: probe only). Never a GitHub workflow (private repos pay minutes).

    python3 ce_nightly.py --all                   run now, all repos
    python3 ce_nightly.py --repo ~/Claude/cubeescape-toolgen
    python3 ce_nightly.py install [--hour 3]      LaunchAgent com.cubeescape.ce-nightly (launchd runs a missed time after wake)
    python3 ce_nightly.py uninstall | status

Exit code: 0 every nightly green, 1 at least one red, 2 nothing to run.
0.31 Windows: report in %LOCALAPPDATA%/CubeEscape/nightly, CE_REPOS ';'-separated, SetThreadExecutionState instead of
caffeinate; install/uninstall exit 2 - launchd is macOS-only, the Windows Task Scheduler is not set up by this tool.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import json
import contextlib
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import heavy_lock  # noqa: E402  (0.31: cache folder and Windows layer)
from ce_common import UTF8, WINDOWS, utf8_console  # noqa: E402
HOME = Path.home()
DEFAULT_REPOS = ["cubeescape", "cubeescape-toolgen", "cubeescape-robogen", "cubeescape-cubegen", "cubeescape-formgen",
                 "cubeescape-motiongen"]
LOG_DIR = Path(heavy_lock.cache_dir("nightly"))  # 0.31: %LOCALAPPDATA%\CubeEscape\nightly on Windows
LABEL = "com.cubeescape.ce-nightly"


def repos(args) -> list[Path]:
    if args.repo:
        return [Path(p).expanduser().resolve() for p in args.repo]
    env = os.environ.get("CE_REPOS")
    if env:
        return [Path(p).expanduser().resolve() for p in env.split(os.pathsep if WINDOWS else ":") if p]  # 0.31: C:\ has a colon
    base = Path(os.environ.get("CE_HOME", HOME / "Claude"))
    from ce_common import standard_repos
    return standard_repos(DEFAULT_REPOS, HOME)


def has_nightly(root: Path) -> bool:
    try:
        import json
        return "nightly" in (json.loads((root / ".ce-repo.json").read_text(encoding="utf-8")).get("check") or {})
    except (OSError, ValueError):
        return False


def uses_loader(root: Path) -> bool:
    try:
        import json
        return "tools_version" in json.loads((root / ".ce-repo.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False


def run_one(root: Path, log, dry_run: bool) -> tuple[str, float]:
    tools = root / "Tools" / "ce_tools" / "ce_check.py"
    if not tools.is_file():
        tools = root / "scripts" / "ce_tools" / "ce_check.py"
    if not tools.is_file():
        tools = root / "tools" / "ce_tools" / "ce_check.py"
    command = [sys.executable, str(tools if tools.is_file() else HERE / "ce_check.py"), "nightly", "--repo", str(root)]
    if uses_loader(root):  # 0.20: no copy - ./ce loads the pinned tools_version
        command = [sys.executable, str(root / "ce"), "check", "--nightly"]
    log(f"=== {root.name}: {' '.join(command)}")
    if dry_run:
        return "dry-run", 0.0
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=root, capture_output=True, text=True, check=True,
                            **UTF8).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True,
                           **UTF8).stdout.strip()
    if branch != "main" or dirty:
        log("Nachtlauf benötigt einen sauberen main-Checkout; keine fremden Arbeitsstände ausführen")
        return "red (checkout)", 0.0
    start = time.monotonic()
    with subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **UTF8) as proc:
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
        lines = []
        def repo_log(line):
            lines.append(line)
            log(line)
        try:
            state, seconds = run_one(root, repo_log, args.dry_run)
        except (OSError, subprocess.SubprocessError) as error:
            state, seconds = "red (start)", 0.0
            repo_log(str(error))
        results.append((root.name, state, seconds))
        if state.startswith("red") and getattr(args, "report_issues", False) and not args.dry_run:
            report_issue(root, lines, log)
    tidy(args, targets, log)
    # the search index follows the repos (ce_search.py): a few seconds, never a reason for a red night
    try:
        if args.dry_run:
            raise RuntimeError("dry-run: kein Index-Schreibzugriff")
        import ce_search
        index_roots = ce_search.repos(None) if not args.repo else [r for r in targets if r.is_dir()]
        summary = ce_search.build(index_roots, None, log=log)
        log(f"=== Suchindex: {summary['total_chunks']} Abschnitte, {summary['seconds']} s, Embeddings {'ja' if summary['embeddings'] else 'nein'}")
    except Exception as error:  # noqa: BLE001
        log(f"=== Suchindex: übersprungen ({error})")
    log("--- Ergebnis")
    for name, state, seconds in results:
        log(f"{name:24} {state:14} {seconds / 60:5.1f} min")
    handle.close()
    red = [name for name, state, _ in results if state.startswith("red")]
    executed = any(state == "green" or state.startswith("red") for _, state, _ in results)
    if not args.dry_run:
        notify("CE Nachtlauf", ("rot: " + ", ".join(red)) if red else "alle grün" if executed else "keine Prüfungen ausgeführt")
    print(f"Bericht: {report}")
    return 1 if red else 0 if executed or args.dry_run else 2


def tidy(args, targets: list[Path], log) -> None:
    """0.18: ce_tidy.py --nightly (only categories with nightly: true remove, the rest is a probe in the report); never red."""
    try:
        import ce_tidy
        command = ([] if args.dry_run else ["--nightly"]) + (["--repos", *map(str, targets)] if args.repo else [])
        log(f"=== Aufräumen: ce_tidy {' '.join(command) or '(Probelauf)'}")
        code = ce_tidy.run(command, out=log)
        if code:
            log("=== Aufräumen: mit Fehlern (siehe oben), Nachtlauf bleibt davon unberührt")
    except Exception as error:  # noqa: BLE001
        log(f"=== Aufräumen: übersprungen ({error})")


def report_issue(root: Path, lines: list[str], log) -> None:
    """Opt-in external reporting, one open issue per repo; never turn reporting failure into a green test."""
    title = "Nachtlauf fehlgeschlagen"
    try:
        current = subprocess.run(["gh", "issue", "list", "--state", "open", "--search", title + " in:title", "--json", "title"],
                                 cwd=root, capture_output=True, text=True, check=True)
        if any(item["title"] == title for item in json.loads(current.stdout)):
            log("Nachtlauf-Issue bereits offen")
            return
        body = "Automatischer Nachtlauf fehlgeschlagen. Ausschnitt:\n\n```text\n" + "\n".join(lines[-30:])[-4000:] + "\n```"
        subprocess.run(["gh", "issue", "create", "--title", title, "--label", "eingang", "--body-file", "-"],
                       input=body, cwd=root, capture_output=True, text=True, check=True)
        log("Nachtlauf-Issue angelegt")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        log(f"Issue-Meldung fehlgeschlagen: {error}")


ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


@contextlib.contextmanager
def awake_windows(kernel32=None):
    """0.31: Windows' caffeinate -i - SetThreadExecutionState keeps the system from sleeping while this thread runs,
    ES_CONTINUOUS alone at the end gives it back. Best effort: without kernel32 the run goes on as before."""
    try:
        if kernel32 is None:
            import ctypes
            kernel32 = ctypes.windll.kernel32
        kept = bool(kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
    except (AttributeError, OSError):
        kept = False
    try:
        yield
    finally:
        if kept:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)


@contextlib.contextmanager
def awake():
    if WINDOWS:
        with awake_windows():
            yield
        return
    child = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) if shutil.which("caffeinate") else None
    try:
        yield
    finally:
        if child is not None:
            child.terminate()
            child.wait(timeout=5)


def plist_path() -> Path:
    return HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"


WINDOWS_SCHEDULER = ("ce_nightly plant unter Windows nicht: launchd gibt es nur auf macOS, der Planer dort ist die "
                     "Windows-Aufgabenplanung (schtasks) und wird von diesem Werkzeug nicht eingerichtet")


def install(hour: int, report_issues: bool = False, repo_paths: list[str] | None = None) -> int:
    if WINDOWS:  # 0.31: no plist, no launchctl, no os.getuid
        print(f"Nicht installiert: {WINDOWS_SCHEDULER}. Von Hand: {sys.executable} {Path(__file__).resolve()} --all")
        return 2
    legacy = HOME / "Library" / "LaunchAgents" / "com.cubeescape.nightly.plist"
    if legacy.exists():
        print(f"Bestehenden CE-Nachtlauf zuerst abgleichen/deaktivieren: {legacy}; keine zweite Planung angelegt")
        return 2
    plist = {"Label": LABEL, "ProgramArguments": [sys.executable, str(Path(__file__).resolve()),
                         *([part for root in repo_paths for part in ["--repo", str(Path(root).expanduser().resolve())]] if repo_paths else ["--all"]),
                         *(["--report-issues"] if report_issues else [])],
             "StartCalendarInterval": {"Hour": hour, "Minute": 0}, "StandardOutPath": str(LOG_DIR / "launchd.out"),
             "StandardErrorPath": str(LOG_DIR / "launchd.err"), "EnvironmentVariables": {"PATH": os.environ.get("PATH", "")}}
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    plist_path().parent.mkdir(parents=True, exist_ok=True)
    with open(plist_path(), "wb") as handle:
        plistlib.dump(plist, handle)
    if not shutil.which("launchctl"):
        print(f"Konfiguration geschrieben, nicht aktiviert (launchctl fehlt): {plist_path()}")
        return 2
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"
    loaded = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
    if loaded.returncode == 0:
        stopped = subprocess.run(["launchctl", "bootout", target], capture_output=True, text=True)
        if stopped.returncode:
            print(f"Nachtlauf nicht neu geladen: {stopped.stderr.strip()}")
            return 1
    started = subprocess.run(["launchctl", "bootstrap", domain, str(plist_path())], capture_output=True, text=True)
    checked = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
    if started.returncode or checked.returncode:
        print(f"Nachtlauf nicht aktiviert: {(started.stderr or checked.stderr).strip()}")
        return 1
    print(f"aktiviert: {plist_path()} ({hour:02d}:00 täglich)")
    return 0


def uninstall() -> int:
    if WINDOWS:
        print(f"Nicht entfernt: {WINDOWS_SCHEDULER}")
        return 2
    if not shutil.which("launchctl"):
        print("Nicht entfernt: launchctl fehlt; Aktivierung nicht prüfbar")
        return 2
    target = f"gui/{os.getuid()}/{LABEL}"
    loaded = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
    if loaded.returncode == 0:
        stopped = subprocess.run(["launchctl", "bootout", target], capture_output=True, text=True)
        checked = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
        if stopped.returncode or checked.returncode == 0:
            print(f"Nicht entfernt: Nachtlauf konnte nicht entladen werden: {stopped.stderr.strip()}")
            return 1
    plist_path().unlink(missing_ok=True)
    print("entfernt")
    return 0


def status() -> int:
    if WINDOWS:
        print(f"nicht installiert ({WINDOWS_SCHEDULER})")
        logs = sorted(LOG_DIR.glob("ce_nightly-*.log"))
        if logs:
            print(f"letzter Bericht: {logs[-1]}")
        return 0
    checked = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True) if shutil.which("launchctl") else None
    if checked is not None and checked.returncode == 0:
        print("aktiviert" + (" (plist fehlt)" if not plist_path().exists() else ""))
    elif not plist_path().exists():
        print("nicht installiert")
    elif checked is None:
        print("konfiguriert, Aktivierung nicht prüfbar (launchctl fehlt)")
    else:
        print("konfiguriert, nicht aktiviert")
    logs = sorted(LOG_DIR.glob("ce_nightly-*.log"))
    if logs:
        print(f"letzter Bericht: {logs[-1]}")
        print("\n".join(logs[-1].read_text(encoding="utf-8").strip().splitlines()[-8:]))
    return 0


def main(argv: list[str]) -> int:
    utf8_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["run", "install", "uninstall", "status"], default="run")
    parser.add_argument("--all", action="store_true", help="all repos (default when no --repo is given)")
    parser.add_argument("--repo", action="append", help="repository path (repeatable)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report-issues", action="store_true", help="failed runs create an eingang issue (opt-in)")
    parser.add_argument("--hour", type=int, default=3)
    args = parser.parse_args(argv)
    if not 0 <= args.hour <= 23:
        parser.error("--hour muss zwischen 0 und 23 liegen")
    if args.command == "install":
        return install(args.hour, args.report_issues, args.repo)
    if args.command == "uninstall":
        return uninstall()
    if args.command == "status":
        return status()
    with awake() if not args.dry_run else contextlib.nullcontext():
        return run(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
