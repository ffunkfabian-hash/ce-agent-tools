#!/usr/bin/env python3
"""ce_verify.py <owner/repo> <PR> [--minutes N] - a clean run of check.quick on the head of a pull request (0.37 Waybill).

For repositories without GitHub CI: the Mac does what a runner would do. Fetches exactly the head SHA into a throwaway folder in the
cache (never a checkout, never the worktree list of a repo), runs check.quick of the BASE branch there - a pull request cannot swap the
command, the same rule as in merge-check; the scripts and tests that command starts are the head's, weakened tests stay with merge-check
and the judge - and writes a receipt per head SHA. `./ce merge-check` reads it; a new head has none.

Only a green run writes one. A run first deletes the receipt of its head, so red, broken off, out of time or not checkable leaves none;
a command that reports tests (unittest or pytest summary) but runs none, or skips every one, is not checkable (0.43: per command).
Mac only: a cloud session has no neighbour repositories and no Mac, it sends a merge request to Sightline, who runs this. A pull
request from a fork is refused, its code would run on the Mac.

Clean means: fresh tree, no .venv from a checkout, no GIT_*/PYTHONPATH/VIRTUAL_ENV from the caller, and a caller's venv off the PATH. A repository with
requirements-lock.txt or requirements.txt gets a .venv: a link to a virtual environment in the cache, one per requirements file and
Python, built on first use; the key covers the files it pulls in with -r and -c (0.43). A file that installs the repository itself
(`-e .`, a local path) gets an environment of its own inside the copy: in a shared one it would leave a link to a deleted copy. CE_VERIFY=1 tells check.quick it stands in a copy: a check of this machine's installation against the
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
VENV_DONE = ".ce-ready"
DROPPED_ENV = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__")
FETCH_SECONDS = 1800  # Cube Escape is 600 MB
SETUP_SECONDS = 900
KEEP_RUN_DAYS, KEEP_DAYS = 1, 30
TAIL_LINES = 25
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
SKIPPED = re.compile(r"^(?:OK|FAILED) \(.*?skipped=(\d+)", re.M)
PYTEST = re.compile(r"^=*\s*(no tests ran|\d+ [a-z]+(?:, \d+ [a-z]+)*) in \d+(?:\.\d+)?s\b", re.M)  # "3 passed, 1 skipped in 0.12s"
PYTEST_RAN = {"passed", "failed", "skipped", "xfailed", "xpassed", "error", "errors"}
PYTEST_OTHER = {"deselected", "warning", "warnings", "rerun"}
INCLUDE = re.compile(r"^(?:--requirement|--constraint|-r|-c)(?:\s*=\s*|\s*)(\S+)")
LOCAL = re.compile(r"^(?:-e\b|--editable\b|\.|/|~|file:)|@\s*file:")


def on_mac():
    return platform.system() == "Darwin"


def cache():
    return mc.verify_path("x/y", "z").parent.parent.parent


def clean_env():
    env = {key: value for key, value in os.environ.items() if key not in DROPPED_ENV and not key.startswith("GIT_")}
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        env["PATH"] = os.pathsep.join(part for part in env.get("PATH", "").split(os.pathsep) if part != os.path.join(venv, "bin"))
    return env


def step(argv, seconds, what, env=None, cwd=None):
    """One preparing command; NotCheckable when it fails."""
    try:
        done = subprocess.run([str(part) for part in argv], capture_output=True, text=True, timeout=seconds, env=env or clean_env(), stdin=subprocess.DEVNULL, cwd=cwd)
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


def requirement_lines(path):
    """The lines pip reads of one requirements file, without comments and blanks."""
    lines = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = re.sub(r"(?:^|\s+)#.*$", "", line).strip()
        if line:
            lines.append(line)
    return lines


def requirement_files(found):
    """The requirements file and every local file it pulls in with -r or -c, relative to the file that names it as pip reads them.
    A missing or remote one stays out: pip then fails or fetches it, either way not from this copy."""
    files, todo = [], [found]
    while todo:
        path = todo.pop(0)
        if path in files or not path.is_file():
            continue
        files.append(path)
        for line in requirement_lines(path):
            match = INCLUDE.match(line)
            if match and "://" not in match[1]:
                todo.append((path.parent / match[1]).resolve())
    return files


def installs_itself(files):
    """True when a requirements line installs from a local path, `-e .` above all."""
    return any(LOCAL.search(line) for path in files for line in requirement_lines(path))


def build_venv(target, found, root):
    step([sys.executable, "-m", "venv", target], SETUP_SECONDS, "venv")
    step([target / "bin" / "python", "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", found], SETUP_SECONDS, f"pip install -r {found.name}", cwd=root)


def link_venv(root):
    """The name of the requirements file a .venv came from, None when the repository has neither."""
    found = next((root / name for name in REQUIREMENTS if (root / name).is_file()), None)
    if found is None or (root / ".venv").exists():
        return None
    found = found.resolve()
    files = requirement_files(found)
    if installs_itself(files):  # the link would point into this copy, which goes at the end of the run
        build_venv(root / ".venv", found, root)
        return f"{found.name} (eigene Umgebung: lokales Paket)"
    digest = hashlib.sha256(sys.version.encode() + platform.machine().encode())
    for path in files:
        digest.update(f"\0{path.relative_to(root.resolve()) if path.is_relative_to(root.resolve()) else path}\0".encode() + path.read_bytes())
    key = digest.hexdigest()[:16]
    ready = cache() / "venvs" / key
    ready.parent.mkdir(parents=True, exist_ok=True)
    gate = os.open(f"{ready}.lock", os.O_CREAT | os.O_RDWR)
    try:
        heavy_lock.lock_fd(gate, True, True)  # two runs at once: the first builds, the second uses it; built in place, scripts name this path
        if not (ready / VENV_DONE).is_file():
            shutil.rmtree(ready, ignore_errors=True)
            try:
                build_venv(ready, found, root)
            except mc.NotCheckable:
                shutil.rmtree(ready, ignore_errors=True)
                raise
            (ready / VENV_DONE).touch()
    finally:
        os.close(gate)
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


def pytest_counts(text):
    """[(tests, skipped)] of the pytest summary lines in a text; a line with a word pytest does not use is no summary."""
    found = []
    for line in PYTEST.findall(text):
        items = [] if line == "no tests ran" else [(int(number), word) for number, word in re.findall(r"(\d+) ([a-z]+)", line)]
        if all(word in PYTEST_RAN | PYTEST_OTHER for _, word in items):
            found.append((sum(n for n, word in items if word in PYTEST_RAN), sum(n for n, word in items if word == "skipped")))
    return found


def counts(text):
    """(tests, skipped) over all unittest and pytest summaries in the output of one command; tests is None when there is none."""
    ran = [int(found) for found in RAN.findall(text)]
    pytest = pytest_counts(text)
    if not ran and not pytest:
        return None, 0
    return sum(ran) + sum(n for n, _ in pytest), sum(int(found) for found in SKIPPED.findall(text)) + sum(n for _, n in pytest)


def write_receipt(result, quick):
    path = mc.verify_path(result["repo"], result["head"])
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {"repo": result["repo"], "pr": result["pr"], "head": result["head"], "base": result["base"], "green": True, "check": quick,
               "tests": result["tests"], "skipped": result["skipped"], "seconds": result["seconds"], "time": time.time()}
    temp = path.with_suffix(f".{os.getpid()}.tmp")  # two runs of one head must not share it
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
    quick = mc.quick_of(mc.read_config(repo, base))  # gh failures are NotCheckable already
    try:
        argvs = ce_check.commands_of(quick)
    except ce_common.RepoError as error:
        raise mc.NotCheckable(f"check.quick der Basis {base} unbrauchbar ({mc.quick_text(quick)}; {mc.QUICK_FORM}); "
                              f"im Repo berichtigen, verify und merge-check prüfen bis dahin nichts: {error}") from error
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
        try:
            venv = link_venv(root)
        except OSError as error:
            raise mc.NotCheckable(f"Umgebung nicht gebaut: {error}") from error
        ready = time.monotonic()
        deadline = ready + minutes * 60
        env, commands = {**clean_env(), "CE_VERIFY": "1"}, []
        for argv in argvs:
            begin, offset = time.monotonic(), log.stat().st_size if log.exists() else 0
            code = run_command(argv, root, log, env, deadline)
            with log.open("rb") as out:
                out.seek(offset)
                ran, skips = counts(out.read().decode("utf-8", errors="replace"))
            commands.append({"argv": argv, "code": code, "seconds": round(time.monotonic() - begin, 1), "tests": ran, "skipped": skips})
            if code is None:
                raise mc.NotCheckable(f"Zeitlimit {minutes} min überschritten bei {' '.join(argv)}; Protokoll {log}")
        for item in commands:  # a neighbour that is missing can end as nothing but skips, in one command beside others that run (0.43)
            if item["tests"] is not None and item["tests"] <= item["skipped"]:
                raise mc.NotCheckable(f"{' '.join(item['argv'])} lief keinen einzigen Test ({item['tests']} Tests, {item['skipped']} übersprungen); Protokoll {log}")
        counted = [item for item in commands if item["tests"] is not None]
        tests = sum(item["tests"] for item in counted) if counted else None
        skipped = sum(item["skipped"] for item in counted)
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
    for name in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):  # a time limit from outside still clears the copy and the process group
        if name is not None:
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
