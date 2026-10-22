#!/usr/bin/env python3
"""ce_githooks.py install|uninstall|status [--repo P] [--dry-run] - Git's own net under every client.

The hooks block what no agent hook can see when a client fires no hook (an IDE without hooks, a human terminal):
- pre-commit: a commit on main/master (branch of HEAD; detached HEAD is not a branch). Exception: CE_ALLOW_MAIN=1.
- pre-push, per pushed ref: an update or creation of main/master (exception CE_ALLOW_MAIN=1), deleting main/master
  (never), overwriting an existing tag and every update whose remote commit is not an ancestor of the pushed one
  (a real non-fast-forward; no exception). A remote commit missing locally cannot be an ancestor: fetch first.
Limits, by Git's protocol: pre-push sees refs and commits, not flags - `git push --force` of a fast-forward looks like a
normal push and passes (the client guards still check the flags). `--no-verify` skips both hooks. Merge commits run
pre-merge-commit, which is not covered. The hooks are a net, not a boundary.

Install per repository, reversible: a folder <git-common-dir>/ce-hooks with small sh wrappers becomes the local
core.hooksPath (shared by all worktrees). The previous hook folder (core.hooksPath of any scope, else .git/hooks) is
recorded in ce-hooks/state.json; pre-commit/pre-push run the checks and then the previous hook of the same name with the
same arguments and input, every other hook found there at install time is forwarded unchanged. Nothing in the previous
folder is moved or edited. uninstall restores the previous local core.hooksPath (or unsets it) and removes the folder.
Errors inside the checks let Git continue with a message (fail open). The wrapper, not Python, forwards to the previous
hook: only the exit code REJECT of `check` is a deliberate refusal; a missing guard script or Python and every other
code (syntax or import error, crash) leave a message and forward unchecked, keeping the previous hook's exit code.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

PROTECTED = ("refs/heads/main", "refs/heads/master")
ZERO = set("0")
FOLDER = "ce-hooks"
STATE = "state.json"
MARK = "# ce-shared git hook wrapper"
GUARDED = ("pre-commit", "pre-push")
REJECT = 86  # exit code of `check` for a deliberate refusal; any other failure is a start error
# Hooks Git looks up in core.hooksPath (githooks(5)); only those present in the previous folder get a forwarder,
# because the mere presence of some (push-to-checkout, proc-receive, ...) changes Git's behaviour.
KNOWN = ("applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit", "pre-merge-commit",
         "prepare-commit-msg", "commit-msg", "post-commit", "pre-rebase", "post-checkout", "post-merge", "pre-push",
         "pre-receive", "update", "proc-receive", "post-receive", "post-update", "reference-transaction",
         "push-to-checkout", "pre-auto-gc", "post-rewrite", "sendemail-validate", "fsmonitor-watchman",
         "p4-changelist", "p4-prepare-changelist", "p4-post-changelist", "p4-pre-submit", "post-index-change")


def git(*args, cwd=None, check=True):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip() or result.returncode}")
    return result


# ------------------------------------------------------------------------------------------------------ checks

def zero(sha):
    return set(sha) <= ZERO


def branch_name(ref):
    return ref.rsplit("/", 1)[-1]


def is_ancestor(old, new):
    """True/False, or None when Git cannot decide (an unexpected error: fail open)."""
    if git("cat-file", "-e", f"{old}^{{commit}}", check=False).returncode != 0:
        return False  # the remote commit is not here, so the pushed history cannot contain it
    code = git("merge-base", "--is-ancestor", old, new, check=False).returncode
    return {0: True, 1: False}.get(code)


def push_problems(lines, allow_main):
    problems = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        deleting, creating = zero(local_sha), zero(remote_sha)
        if remote_ref in PROTECTED:
            if deleting:
                problems.append(f"`{remote_ref}` löschen ist gesperrt (auch mit CE_ALLOW_MAIN=1).")
                continue
            if not allow_main:
                problems.append(f"Push auf `{branch_name(remote_ref)}` ({local_ref} -> {remote_ref}): nach main nur "
                                "über einen PR mit dem Ja des Nutzers; Ausnahme nur mit CE_ALLOW_MAIN=1.")
                continue
        if deleting or creating or local_sha == remote_sha:
            continue
        if remote_ref.startswith("refs/tags/"):
            problems.append(f"Tag `{remote_ref}` existiert schon auf dem Remote mit anderem Ziel: Tags nie überschreiben.")
            continue
        ancestor = is_ancestor(remote_sha, local_sha)
        if ancestor is None:
            print(f"ce-shared pre-push: Vorfahr von {remote_ref} nicht prüfbar, Push läuft weiter", file=sys.stderr)
        elif not ancestor:
            problems.append(f"Kein Fast-Forward: `{remote_ref}` steht auf {remote_sha[:12]}, das nicht in {local_ref} "
                            "enthalten ist (Force-Push überschreibt fremde Commits). Erst `git fetch`, Stand klären; "
                            "Force-Pushes nie ohne den Nutzer.")
    return problems


def check_commit(allow_main):
    ref = git("symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
    if ref in PROTECTED and not allow_main:
        return [f"Commit direkt auf `{branch_name(ref)}`: Arbeit auf einem Branch (`claude/<repo>-…`, "
                "`codex/<repo>-…`), nach main nur per PR; Ausnahme nur mit CE_ALLOW_MAIN=1."]
    return []


def previous_hook(previous, name):
    path = Path(previous) / name
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def check_hook(name, stdin=None):
    """The checks of one guarded hook: 0 to go on, REJECT for a deliberate refusal (reasons on stderr)."""
    allow_main = os.environ.get("CE_ALLOW_MAIN") == "1"
    try:
        data = (stdin or sys.stdin).read() if name == "pre-push" else None
        problems = push_problems(data.splitlines(), allow_main) if name == "pre-push" else check_commit(allow_main)
    except Exception as error:  # never wedge git
        print(f"ce-shared {name}: Prüfung fehlgeschlagen ({error}), Git läuft weiter", file=sys.stderr)
        problems = []
    if problems:
        print(f"ce-shared {name}: abgelehnt\n- " + "\n- ".join(problems), file=sys.stderr)
        print("(Netz der globalen Regel › Immer; `--no-verify` umgeht es - nur mit ausdrücklichem Ja des Nutzers.)",
              file=sys.stderr)
        return REJECT
    return 0


# ------------------------------------------------------------------------------------------------- installation

def wrapper(name, previous, guard, python):
    """sh wrapper of one hook: the check (guarded hooks only), then the previous hook with the same arguments and input.
    Only REJECT stops Git; a guard that is missing or does not start is reported and skipped (fail open)."""
    hook = f'"$previous/{name}"'
    feed = 'printf "%s" "$input" | ' if name == "pre-push" else ""  # Git's ref lines for the check and the old hook
    lines = ["#!/bin/sh", f"{MARK} ({name}); remove: {shlex.quote(python)} {shlex.quote(guard)} uninstall",
             f"previous={shlex.quote(previous)}"]
    if feed:
        lines.append('input=$(cat; echo x); input=${input%x}')  # the x keeps trailing newlines
    if name in GUARDED:
        lines += [f"guard={shlex.quote(guard)}", f"python={shlex.quote(python)}",
                  'if [ -f "$guard" ] && [ -x "$python" ]; then',
                  f'  {feed}"$python" "$guard" check {name}', "  code=$?",
                  f'  if [ "$code" -eq {REJECT} ]; then exit 1; fi',
                  f'  if [ "$code" -ne 0 ]; then echo "ce-shared: Git-Wächter startet nicht (Exit $code), {name} läuft '
                  'ohne Prüfung" >&2; fi',
                  "else",
                  f'  echo "ce-shared: Git-Wächter fehlt ($guard), {name} läuft ohne Prüfung" >&2',
                  "fi"]
    if feed:
        lines.append(f'if [ -f {hook} ] && [ -x {hook} ]; then {feed}{hook} "$@"; exit $?; fi')
    else:
        lines.append(f'if [ -f {hook} ] && [ -x {hook} ]; then exec {hook} "$@"; fi')
    lines += ["exit 0", ""]
    return "\n".join(lines)


def repo_paths(repo):
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo).stdout.strip())
    top = git("rev-parse", "--show-toplevel", cwd=repo, check=False).stdout.strip()
    return common, common / FOLDER, Path(top) if top else common


def config_get(repo, *args):
    result = git("config", *args, "--get", "core.hooksPath", cwd=repo, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def same(path, other):
    return other is not None and os.path.realpath(path) == os.path.realpath(os.path.expanduser(other))


def ours(path):
    try:
        return MARK in path.read_text(encoding="utf-8", errors="replace")[:400]
    except OSError:
        return False


def plan(repo):
    common, folder, top = repo_paths(repo)
    local = config_get(repo, "--local")
    state_file = folder / STATE
    if same(folder, local) and state_file.is_file():
        state = json.loads(state_file.read_text(encoding="utf-8"))
    elif same(folder, local):
        raise RuntimeError(f"core.hooksPath zeigt auf {folder}, aber {STATE} fehlt - von Hand prüfen")
    else:
        effective = config_get(repo, "--type=path")
        state = {"previous_local": local, "previous": effective or str(common / "hooks")}
    previous_dir = Path(state["previous"]) if os.path.isabs(state["previous"]) else top / state["previous"]
    names = [n for n in KNOWN if n in GUARDED or previous_hook(previous_dir, n)]
    return common, folder, state, names


def install(repo, guard, python, dry_run=False, out=print):
    common, folder, state, names = plan(repo)
    forwarded = [n for n in names if n not in GUARDED]
    out(f"{repo}: core.hooksPath -> {folder}; vorher {state['previous']}"
        + (f"; weitergereicht: {', '.join(forwarded)}" if forwarded else ""))
    if dry_run:
        return
    foreign = [n for n in names if (folder / n).exists() and not ours(folder / n)]
    if foreign:
        raise RuntimeError(f"{folder}: {', '.join(foreign)} sind keine ce-shared-Wrapper - nichts geändert")
    folder.mkdir(exist_ok=True)
    for item in folder.iterdir():
        if item.name != STATE and item.name not in names and ours(item):
            item.unlink()
    for name in names:
        temp = folder / f".{name}.tmp"
        temp.write_text(wrapper(name, state["previous"], guard, python), encoding="utf-8")
        temp.chmod(0o755)
        os.replace(temp, folder / name)
    (folder / STATE).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    git("config", "--local", "core.hooksPath", str(folder), cwd=repo)


def uninstall(repo, dry_run=False, out=print):
    common, folder, top = repo_paths(repo)
    local = config_get(repo, "--local")
    if not same(folder, local):
        out(f"{repo}: nicht installiert (core.hooksPath lokal: {local or '-'}), nichts geändert")
        return
    state = json.loads((folder / STATE).read_text(encoding="utf-8")) if (folder / STATE).is_file() else {}
    previous = state.get("previous_local")
    out(f"{repo}: core.hooksPath " + (f"zurück auf {previous}" if previous else "entfernt") + f"; {folder} gelöscht")
    if dry_run:
        return
    if previous:
        git("config", "--local", "core.hooksPath", previous, cwd=repo)
    else:
        git("config", "--local", "--unset", "core.hooksPath", cwd=repo)
    for item in folder.iterdir():
        if item.name == STATE or ours(item):
            item.unlink()
    if not any(folder.iterdir()):
        folder.rmdir()


def status(repo, out=print):
    common, folder, state, names = plan(repo)
    effective = git("rev-parse", "--path-format=absolute", "--git-path", "hooks", cwd=repo).stdout.strip()
    active = same(folder, effective)
    missing = [n for n in names if not (folder / n).is_file()]
    out(f"{repo}: {'aktiv' if active else 'nicht aktiv'} (Hooks aus {effective}); vorher {state['previous']}"
        + (f"; neu im vorherigen Ordner, erneut installieren: {', '.join(missing)}" if active and missing else ""))
    return 0 if active and not missing else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["check"] and len(argv) == 2:  # called by the wrappers: check <hook>
        return check_hook(argv[1])
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=["install", "uninstall", "status"])
    parser.add_argument("--repo", action="append", type=Path, help="Repository (wiederholbar), Standard: hier")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    code = 0
    for repo in args.repo or [Path.cwd()]:
        try:
            if args.command == "install":
                install(repo, str(Path(__file__).resolve()), sys.executable, args.dry_run)
            elif args.command == "uninstall":
                uninstall(repo, args.dry_run)
            else:
                code |= status(repo)
        except (OSError, RuntimeError, ValueError) as error:
            print(f"{repo}: {error}", file=sys.stderr)
            code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
