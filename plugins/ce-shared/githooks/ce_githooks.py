#!/usr/bin/env python3
"""ce_githooks.py install|uninstall|status [--repo P] [--dry-run] - Git's own net under every client.

The hooks block what no agent hook can see when a client fires no hook (an IDE without hooks, a human terminal):
- pre-commit: a commit on main/master (branch of HEAD; detached HEAD is not a branch). Exception: CE_ALLOW_MAIN=1.
- pre-push, per pushed ref: an update or creation of main/master (exception CE_ALLOW_MAIN=1), deleting main/master
  (never), overwriting an existing tag and every update whose remote commit is not an ancestor of the pushed one
  (a real non-fast-forward; no exception). A remote commit missing locally cannot be an ancestor: fetch first.
- pre-push into a public GitHub repository (0.34, Trimline 28; a push there is visible at once, a merge check comes too
  late): new binary files over 5 MB in the pushed commits, and added lines with a recognizable secret. Text files of any
  size pass, placeholders pass, a found secret is named by type and place, never printed. Visibility comes from `gh`;
  no `gh` or no answer skips this part with a message (fail open), other hosts and private repositories skip it silently.
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
import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
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
BIG_BINARY = 5_000_000  # bytes; the same limit as BIG_FILE in tools/ce_merge_check.py (a test compares them)
BINARY_PROBE = 8000  # Git's own rule: a NUL in the first 8000 bytes makes a file binary
SCAN_SECONDS = 20  # a huge first push must not hang: the secret scan stops with a message
LONGEST_LINE = 100_000
SHOWN = 10
# Only formats with a fixed prefix and length: a generic "password = ..." would fire on every test fixture.
SECRETS = (("GitHub-Token", re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})")),
           ("AWS-Zugangsschlüssel", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")),
           ("Anthropic-Schlüssel", re.compile(r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{32,}")),
           ("OpenAI-Schlüssel", re.compile(r"(?<![A-Za-z0-9_-])sk-(?:proj-[A-Za-z0-9_-]{40,}|[A-Za-z0-9]{40,})")),
           ("Google-API-Schlüssel", re.compile(r"(?<![A-Za-z0-9_-])AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])")),
           ("Slack-Token", re.compile(r"(?<![A-Za-z0-9_-])xox[abeoprs]-[0-9]{6,}-[A-Za-z0-9-]{10,}")),
           ("Stripe-Live-Schlüssel", re.compile(r"(?<![A-Za-z0-9_])[rs]k_live_[A-Za-z0-9]{20,}")),
           ("npm-Token", re.compile(r"(?<![A-Za-z0-9_])npm_[A-Za-z0-9]{36}(?![A-Za-z0-9])")),
           ("PyPI-Token", re.compile(r"(?<![A-Za-z0-9_])pypi-AgE[A-Za-z0-9_-]{50,}")))
PLACEHOLDER = re.compile(r"example|sample|dummy|fake|placeholder|changeme|your|redacted|0123456|1234567", re.IGNORECASE)
KEY_HEAD = re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----")
KEY_BODY = re.compile(r"[A-Za-z0-9+/=]{40,}")  # a key has a body; a bare header is documentation
GITHUB_URL = re.compile(r"github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?/?$")


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


def is_placeholder(token):
    """A documented example, not a key: placeholder words, or too few different characters for a random string."""
    return PLACEHOLDER.search(token) is not None or len(set(token)) < 12


def secrets_in_line(text):
    """Names of the secrets in one line of added text."""
    return [name for name, pattern in SECRETS for match in pattern.finditer(text) if not is_placeholder(match.group())]


def scan_diff(lines, deadline=None):
    """(hits, finished) of the output of `git log -p -U0`: hits are (type, path, commit), from added lines only, so a secret
    that is already out does not stop every later push."""
    hits, commit, path, in_header, key_open = [], "?", "?", False, False
    for raw in lines:
        if deadline is not None and time.monotonic() > deadline:
            return hits, False
        line = raw.rstrip("\r\n")
        if line.startswith("commit:"):
            commit, in_header = line[7:], False
        elif line.startswith("diff --git "):
            in_header, key_open = True, False
        elif in_header:
            if line.startswith("+++ b/"):
                path = line[6:]
            in_header = not line.startswith("@@")
        elif line.startswith("+") and len(line) <= LONGEST_LINE:
            text = line[1:]
            hits += [(name, path, commit) for name in secrets_in_line(text)]
            head = KEY_HEAD.search(text)
            body = KEY_BODY.search(text[head.end():].replace("\\n", "")) if head else None
            if body or (key_open and KEY_BODY.fullmatch(text.strip())):
                hits.append(("privater Schlüssel", path, commit))
            key_open = bool(head) and not body
        elif not line.startswith("@@"):
            key_open = False
    return hits, True


def push_urls(remote):
    """Where a push of this remote goes, after insteadOf and pushInsteadOf; [] when it is no configured remote."""
    return git("remote", "get-url", "--push", "--all", remote, check=False).stdout.split()


def fetch_urls(remote):
    return git("remote", "get-url", remote, check=False).stdout.split() or None


def ranges(lines, remote):
    """rev-list arguments of each pushed ref: its new commits, those the target remote does not have yet. Other remotes do not
    count, a commit known to a private remote is still new to a public one. Tracking refs show the fetch URL, so they only
    count when it is the one push target: a pushurl, a second URL, pushInsteadOf or a URL as remote leave the old ref value."""
    tracked = [f"--remotes={remote}"] if push_urls(remote) == fetch_urls(remote) else []
    found = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4 or zero(parts[1]) or parts[1] == parts[3]:
            continue
        known = [] if zero(parts[3]) or git("cat-file", "-e", f"{parts[3]}^{{commit}}", check=False).returncode else [parts[3]]
        found.append([parts[1], "--not", *tracked, *known])
    return found


def big_binaries(args):
    """(path, bytes) of the binary blobs over the limit that these commits add."""
    objects = git("rev-list", "--objects", *args).stdout
    batch = subprocess.run(["git", "cat-file", "--batch-check=%(objecttype) %(objectname) %(objectsize) %(rest)"], input=objects,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    found = []
    for line in batch.stdout.splitlines():
        parts = line.split(" ", 3)
        if len(parts) == 4 and parts[0] == "blob" and int(parts[2]) > BIG_BINARY:
            reader = subprocess.Popen(["git", "cat-file", "blob", parts[1]], stdout=subprocess.PIPE)
            probe = reader.stdout.read(BINARY_PROBE)
            reader.kill()
            reader.wait()
            reader.stdout.close()
            if b"\0" in probe:
                found.append((parts[3], int(parts[2])))
    return found


def diff_secrets(args):
    """Merge commits show what the merge itself added (--diff-merges=remerge, Git 2.35): a plain `git log -p` shows them no diff.
    An older Git refuses the option, which ends in the caller's message instead of an empty, clean-looking scan."""
    command = ["git", "log", "-p", "-U0", "--diff-merges=remerge", "--no-color", "--no-ext-diff", "--no-textconv", "--src-prefix=a/",
               "--dst-prefix=b/", "--format=commit:%h", *args]
    reader = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    try:
        hits, finished = scan_diff(reader.stdout, time.monotonic() + SCAN_SECONDS)
        if finished and reader.wait() != 0:
            raise RuntimeError(f"git log endet mit Exit {reader.returncode}, Geheimnis-Suche ungeprüft")
        return hits, finished
    finally:
        reader.kill()
        reader.wait()
        reader.stdout.close()


def remote_slugs(remote, url=""):
    """owner/repo of every GitHub push target, none for other hosts. A pushurl replaces the fetch URL; the raw config values count
    next to Git's URL for the hook, because insteadOf rewrites that one."""
    configured = git("config", "--get-all", f"remote.{remote}.pushurl", check=False).stdout.split()
    configured = configured or git("config", "--get-all", f"remote.{remote}.url", check=False).stdout.split()[:1]
    matches = [GITHUB_URL.search(item) for item in (url, *configured, *push_urls(remote), remote) if item]
    return sorted({match.group(1) for match in matches if match})


def visibility(slug):
    """"public", "private", "internal", or None when GitHub gives no answer."""
    try:
        done = subprocess.run(["gh", "api", f"repos/{slug}", "--jq", ".visibility"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def public_slug(slugs):
    """The first public repository of the targets, None when there is none. Anything unread is reported."""
    for slug in slugs:
        seen = visibility(slug)
        if seen is None:
            print(f"ce-shared pre-push: Sichtbarkeit von {slug} nicht lesbar, Binärdateien und Geheimnisse ungeprüft", file=sys.stderr)
        if seen == "public":
            return slug
    return None


def public_problems(lines, remote, url=""):
    """The checks of a push into a public repository. Anything that cannot be read lets the push go on with a message."""
    pushed = [args for args in ranges(lines, remote) if git("rev-list", "-n", "1", *args).stdout.strip()]
    slug = public_slug(remote_slugs(remote, url)) if pushed else None
    if slug is None:
        return []
    binaries, secrets = set(), set()
    for args in pushed:
        binaries.update(big_binaries(args))
        hits, finished = diff_secrets(args)
        secrets.update(hits)
        if not finished:
            print(f"ce-shared pre-push: Geheimnis-Suche nach {SCAN_SECONDS} s abgebrochen, Rest ungeprüft", file=sys.stderr)
    problems = [f"Binärdatei über 5 MB in öffentliches Repo {slug}: `{path}` ({size / 1e6:.1f} MB)." for path, size in sorted(binaries)]
    problems += [f"Mögliches Geheimnis in öffentlichem Repo {slug}: {name} in `{path}`, Commit {commit}. Entfernen und den "
                 "Schlüssel ersetzen, wenn er echt ist." for name, path, commit in sorted(secrets)]
    return problems[:SHOWN] + ([f"… und {len(problems) - SHOWN} weitere."] if len(problems) > SHOWN else [])


def check_commit(allow_main):
    ref = git("symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
    if ref in PROTECTED and not allow_main:
        return [f"Commit direkt auf `{branch_name(ref)}`: Arbeit auf einem Branch (`claude/<repo>-…`, "
                "`codex/<repo>-…`), nach main nur per PR; Ausnahme nur mit CE_ALLOW_MAIN=1."]
    return []


def previous_hook(previous, name):
    path = Path(previous) / name
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def check_hook(name, stdin=None, remote="origin", url=""):
    """The checks of one guarded hook: 0 to go on, REJECT for a deliberate refusal (reasons on stderr). Wrappers from before
    0.34 pass no remote, so origin is the default."""
    allow_main = os.environ.get("CE_ALLOW_MAIN") == "1"
    data = None
    try:
        data = (stdin or sys.stdin).read() if name == "pre-push" else None
        problems = push_problems(data.splitlines(), allow_main) if name == "pre-push" else check_commit(allow_main)
    except Exception as error:  # never wedge git
        print(f"ce-shared {name}: Prüfung fehlgeschlagen ({error}), Git läuft weiter", file=sys.stderr)
        problems = []
    if name == "pre-push" and not problems and data:
        try:
            problems = public_problems(data.splitlines(), remote, url)
        except Exception as error:  # a broken scan must not hide the checks above or wedge git
            print(f"ce-shared {name}: Prüfung fehlgeschlagen ({error}), Git läuft weiter", file=sys.stderr)
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
                  f'  {feed}"$python" "$guard" check {name} "$@"', "  code=$?",
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
        temp.write_bytes(wrapper(name, state["previous"], guard, python).encode("utf-8"))  # LF also on Windows (sh)
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
    if os.name == "nt":  # Git Bash shows UTF-8 like the wrappers' own lines, not the cp1252 of a Windows pipe (0.31)
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(AttributeError, ValueError):
                stream.reconfigure(encoding="utf-8", errors="replace")
    if argv[:1] == ["check"] and len(argv) >= 2:  # called by the wrappers: check <hook> [Git's hook arguments]
        return check_hook(argv[1], remote=argv[2] if len(argv) > 2 else "origin", url=argv[3] if len(argv) > 3 else "")
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
