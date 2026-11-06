#!/usr/bin/env python3
"""ce_verify.py <owner/repo> <PR> [--minutes N] - a clean run of check.quick on the head of a pull request (0.37 Waybill).

For repositories without GitHub CI: the Mac does what a runner would do. Fetches exactly the head SHA into a throwaway folder in the
cache (never a checkout, never the worktree list of a repo), runs check.quick of the BASE branch there - a pull request cannot swap its own
check, the same rule as in merge-check - and writes a receipt per head SHA. `./ce merge-check` reads it; a new head has none.

Only a green run writes one. A run first deletes the receipt of its head, so red, broken off, out of time or not checkable leaves none.
Mac only: a cloud session has no neighbour repositories and no Mac, it sends a merge request to Sightline, who runs this. A pull
request from a fork is refused, its code would run on the Mac.

Clean means: fresh tree, no .venv from a checkout, no GIT_*/PYTHONPATH/VIRTUAL_ENV from the caller. A repository with
requirements-lock.txt or requirements.txt gets a .venv: a link to a virtual environment in the cache, one per requirements file and
Python, built on first use. CE_VERIFY=1 tells check.quick it stands in a copy: a check of this machine's installation against the
checkout (Nexogen's links) has to skip itself there, or it is red in every copy. HOME stays: neighbour repositories are read from their checkout on this Mac, each repository pins the commit
itself (Formgen v2: config.toml > [ballon]); a missing one shows as red tests. The receipt names tests and skips, the numbers to compare.
No lock of its own, quick tiers are light (heavy_lock.py); a check.quick that needs Blender or Unity runs as
`./ce lock <resource> -- ./ce verify ...`.

Folder (heavy_lock.cache_dir("ce_verify"), CE_VERIFY_CACHE moves it): receipts/, logs/ (one per head, kept for a red run), venvs/, runs/.
Output: the result as JSON on stdout, one text line on stderr. Exit 0 green, 1 red, 2 not checkable (never a receipt).
"""
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ce_check  # noqa: E402
import ce_common  # noqa: E402
import ce_merge_check as mc  # noqa: E402
import heavy_lock  # noqa: E402

MINUTES = 60  # Formgen v2 needs about 9
REMOTE = "https://github.com/{repo}.git"
REQUIREMENTS = ("requirements-lock.txt", "requirements.txt")
DROPPED_ENV = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__")
FETCH_SECONDS = 1800  # Cube Escape is 600 MB
SETUP_SECONDS = 900
KEEP_RUN_DAYS, KEEP_DAYS = 1, 30
TAIL_LINES = 25
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
SKIPPED = re.compile(r"^(?:OK|FAILED) \(.*?skipped=(\d+)", re.M)


def on_mac():
    return platform.system() == "Darwin"


def cache():
    return mc.verify_path("x/y", "z").parent.parent.parent


def clean_env():
    return {key: value for key, value in os.environ.items() if key not in DROPPED_ENV and not key.startswith("GIT_")}


def step(argv, seconds, what, env=None):
    """One preparing command; NotCheckable when it fails."""
    try:
        done = subprocess.run([str(part) for part in argv], capture_output=True, text=True, timeout=seconds, env=env or clean_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise mc.NotCheckable(f"{what}: {error}") from error
    if done.returncode:
        raise mc.NotCheckable(f"{what}: {(done.stderr or done.stdout).strip()[-300:]}")
    return done.stdout.strip()


def fetch(repo, head, target):
    """Exactly this commit, without history and without any local repository. Large files stay pointers: quick tiers do not read them."""
    env = {**clean_env(), "GIT_LFS_SKIP_SMUDGE": "1", "GIT_TERMINAL_PROMPT": "0"}
    step(["git", "init", "-q", target], 60, "git init", env)
    step(["git", "-C", target, "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", "fetch", "-q", "--depth", "1", "--no-tags",
          REMOTE.format(repo=repo), head], FETCH_SECONDS, f"Head {head[:7]} nicht geladen", env)
    step(["git", "-C", target, "-c", f"core.hooksPath={os.devnull}", "checkout", "-q", "--detach", "FETCH_HEAD"], FETCH_SECONDS, "checkout", env)
    found = step(["git", "-C", target, "rev-parse", "HEAD"], 60, "git rev-parse", env)
    if found != head:
        raise mc.NotCheckable(f"Kopie steht auf {found[:7]}, nicht auf {head[:7]}")


def link_venv(root):
    """The name of the requirements file a .venv came from, None when the repository has neither."""
    found = next((root / name for name in REQUIREMENTS if (root / name).is_file()), None)
    if found is None or (root / ".venv").exists():
        return None
    key = hashlib.sha256(found.read_bytes() + sys.version.encode() + platform.machine().encode()).hexdigest()[:16]
    ready = cache() / "venvs" / key
    ready.parent.mkdir(parents=True, exist_ok=True)
    if not ready.is_dir():
        build = Path(tempfile.mkdtemp(prefix=".build-", dir=ready.parent))
        try:
            step([sys.executable, "-m", "venv", build], SETUP_SECONDS, "venv")
            step([build / "bin" / "python", "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", found], SETUP_SECONDS, f"pip install -r {found.name}")
            try:
                build.rename(ready)  # two runs at once: the first wins, the second uses it
            except OSError:
                if not ready.is_dir():
                    raise
        finally:
            shutil.rmtree(build, ignore_errors=True)
    os.utime(ready)
    (root / ".venv").symlink_to(ready, target_is_directory=True)
    return found.name


def run_command(argv, root, log, env, deadline):
    """Exit code of one command in a process group of its own, None when time ran out. Nothing of the group outlives it."""
    resolved = ce_check._resolve_argv(argv, root)
    with log.open("ab") as out:
        out.write(f"$ {' '.join(argv)}\n".encode())
        out.flush()
        try:
            child, group = heavy_lock.spawn_group(resolved, cwd=root, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
        except OSError as error:
            out.write(f"cannot start {argv[0]}: {error}\n".encode())
            return 127
        try:
            return child.wait(max(deadline - time.monotonic(), 1))
        except subprocess.TimeoutExpired:
            return None
        finally:
            heavy_lock.signal_group(group, signal.SIGKILL)
            child.wait()
            heavy_lock.close_group(group)


def counts(text):
    """(tests, skipped) over all unittest summaries in a log; tests is None when there is none."""
    ran = [int(found) for found in RAN.findall(text)]
    return (sum(ran) if ran else None), sum(int(found) for found in SKIPPED.findall(text))


def write_receipt(result, quick):
    path = mc.verify_path(result["repo"], result["head"])
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {"repo": result["repo"], "pr": result["pr"], "head": result["head"], "base": result["base"], "green": True, "check": quick,
               "tests": result["tests"], "skipped": result["skipped"], "seconds": result["seconds"], "time": time.time()}
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(receipt, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def sweep(now=None):
    """What older runs left: a copy after a hard kill, old logs, receipts and virtual environments. Only inside this tool's own folder."""
    now = now or time.time()
    for pattern, days in (("runs/*", KEEP_RUN_DAYS), ("logs/*", KEEP_DAYS), ("venvs/*", KEEP_DAYS), ("receipts/*/*.json", KEEP_DAYS)):
        for item in cache().glob(pattern):
            try:
                if now - item.lstat().st_mtime > days * 86400:
                    shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)
            except OSError:  # a parallel run got there first
                pass


def verify(repo, number, minutes=MINUTES, say=None):
    """The result of one run; raises NotCheckable. A receipt only at the very end of a green run."""
    say = say or (lambda text: print(text, file=sys.stderr, flush=True))
    if not on_mac():
        raise mc.NotCheckable("./ce verify läuft nur am Mac; Cloud-Sitzungen schicken eine Merge-Bitte an Sightline")
    pr = mc.normalize_pr(mc.gh_json("api", f"repos/{repo}/pulls/{number}"))
    head, base = pr["headRefOid"], pr["baseRefName"]
    if not pr["sameRepo"]:
        raise mc.NotCheckable("PR aus einem anderen Repo: fremder Code läuft nicht am Mac")
    mc.verify_path(repo, head).unlink(missing_ok=True)  # first: an old receipt must not outlive a run that breaks off
    try:
        quick = ((mc.read_config(repo, base) or {}).get("check") or {}).get("quick")
        argvs = ce_check.commands_of(quick)
    except ce_common.RepoError as error:
        raise mc.NotCheckable(f"check.quick der Basis {base} unbrauchbar: {error}") from error
    sweep()
    (cache() / "runs").mkdir(parents=True, exist_ok=True)
    (cache() / "logs").mkdir(parents=True, exist_ok=True)
    log = cache() / "logs" / f"{repo.replace('/', '__')}-{head[:12]}.log"
    log.unlink(missing_ok=True)
    root = Path(tempfile.mkdtemp(prefix=f"{head[:12]}-", dir=cache() / "runs"))
    say(f"verify {repo}#{number} {head[:7]}: läuft, Protokoll {log}")
    start = time.monotonic()
    try:
        fetch(repo, head, root)
        venv = link_venv(root)
        ready = time.monotonic()
        deadline = ready + minutes * 60
        env, commands = {**clean_env(), "CE_VERIFY": "1"}, []
        for argv in argvs:
            begin = time.monotonic()
            code = run_command(argv, root, log, env, deadline)
            commands.append({"argv": argv, "code": code, "seconds": round(time.monotonic() - begin, 1)})
            if code is None:
                raise mc.NotCheckable(f"Zeitlimit {minutes} min überschritten bei {' '.join(argv)}; Protokoll {log}")
        tests, skipped = counts(log.read_text(encoding="utf-8", errors="replace"))
        if tests == 0:
            raise mc.NotCheckable(f"check.quick lief ohne einen einzigen Test; Protokoll {log}")
        green = all(item["code"] == 0 for item in commands)
        result = {"repo": repo, "pr": number, "head": head, "base": base, "green": green, "tests": tests, "skipped": skipped, "venv": venv,
                  "commands": commands, "setup_seconds": round(ready - start, 1), "seconds": round(time.monotonic() - ready, 1), "log": str(log)}
        if green:
            write_receipt(result, quick)
        result["receipt"] = green
        return result
    finally:
        shutil.rmtree(root, ignore_errors=True)


def text_line(result):
    size = f"{result['tests']} Tests, {result['skipped']} übersprungen" if result["tests"] is not None else "keine Testzahl"
    state = "grün, Quittung geschrieben" if result["green"] else "rot, keine Quittung"
    return f"verify {result['repo']}#{result['pr']} {result['head'][:7]}: {state} · {size} · {result['seconds']:.0f} s (+ {result['setup_seconds']:.0f} s Aufbau)"


def main(argv=None):
    ce_common.utf8_console()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("repo", help="owner/repo")
    parser.add_argument("pr", type=int)
    parser.add_argument("--minutes", type=int, default=MINUTES, help=f"Zeitlimit für check.quick (Standard {MINUTES})")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        parser.error("owner/repo erwartet")
    for name in (signal.SIGTERM, signal.SIGHUP):  # a time limit from outside still clears the copy and the process group
        signal.signal(name, lambda signum, _frame: sys.exit(128 + signum))
    try:
        result = verify(args.repo, args.pr, args.minutes)
    except mc.NotCheckable as error:
        print(json.dumps({"repo": args.repo, "pr": args.pr, "green": False, "receipt": False, "error": str(error)}, ensure_ascii=False))
        print(f"verify {args.repo}#{args.pr}: nicht prüfbar, keine Quittung · {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    if not result["green"]:
        tail = Path(result["log"]).read_text(encoding="utf-8", errors="replace").splitlines()[-TAIL_LINES:]
        print("\n".join(tail), file=sys.stderr)
    print(text_line(result), file=sys.stderr)
    return 0 if result["green"] else 1


if __name__ == "__main__":
    sys.exit(main())
