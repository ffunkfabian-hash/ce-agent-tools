#!/usr/bin/env python3
"""ce_pin.py <version> [--dry-run] [--only IDS] [--skip IDS] [--wait-minutes N] - one pin round over all active repositories (0.42 Ratchet).

Moves .ce-repo.json > tools_version of every active repository to a released version of ce-agent-tools without a session per repository.
Active: Nexogen's repos.json on GitHub, without `status: stillgelegt` (the field also holds free text, Formgen v2) and `hold`; a repository
without tools_version (Agent Tools, Nexogen) has no pin. Everything is read from GitHub, no checkout on this Mac is touched.

Per repository: tools_version, the loader ce, the patch number of the version file, the changelog entry, what ce_pin_repos.json adds (the
data of this tool: entry wording and further lines), and a `ce-tools@<commit>` in a workflow that followed the old pin. A workflow on
another commit stays and is named. A version kept as plain text stops that repository: no version file, no number to raise.

Run, one repository after the other, each stopping on its own: first branch claude/<repo>-patch-pin<number> and pull request from a throwaway
copy in the cache (sparse and partial, Cube Escape is 600 MB), then ./ce verify where the base has no workflow file, the wait for CI, and
./ce merge-check from that copy (only there its head is "this folder", the condition of stage 1). A second run continues at an open pull
request, and at a branch that holds exactly this plan as one commit on the current base (a push whose pull request failed); any other branch stops,
nothing is overwritten, and so does one whose pull request was closed. Content counts, not the title: an open pull request whose head differs from the
plan keeps its row but gets no merge line. The plan is made anew, so a moved base or a new date in an entry also counts as a difference, the safe side. --dry-run reads and shows every diff (an open pull request has none to show): no copy, no commit, no push, no pull request.

It never merges. The guard reads Bash lines: a `gh pr merge` started from here would pass the merge door unseen, a fourth way beside Sightline
and the two receipts (guard.py > merge_attempt). So a stage 1 row carries the plain merge line for Sightline, every other row what is missing;
after the user's Ja a session runs `./ce merge-check <repo> <PR> --ja "<words>"` itself. Sightline's door has no time limit, so a line that has grown old
gets a new `./ce pin` first: that continues at the open pull request and checks from a copy again. A real run is Mac only: a cloud session sends a request.

Output: the diffs (dry run), one table row per repository on stdout, progress on stderr. Exit 0 all fine, 1 a repository stopped or is red, 2 no such version.
"""
import argparse
import difflib
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ce_common  # noqa: E402
import ce_merge_check as mc  # noqa: E402
import ce_session_name  # noqa: E402
import ce_sync  # noqa: E402
import ce_verify  # noqa: E402
import heavy_lock  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA = HERE / "ce_pin_repos.json"
DATA_SCHEMA = 1
REGISTRY_REPO = "ffunkfabian-hash/cubeescape-nexogen"
REGISTRY_BRANCH = "main"
LOADER_SOURCE = "plugins/ce-shared/tools/ce_loader.py"
WORKFLOWS = ".github/workflows"
ACTION_PIN = re.compile(r"ce-agent-tools/\.github/actions/ce-tools@([0-9a-f]{40})")
REMOTE = ce_verify.REMOTE
WAIT_MINUTES = 20
POLL_SECONDS = 30
KEEP_RUN_DAYS = 1
GIT_SECONDS = 600
VERIFY_SECONDS = ce_verify.MINUTES * 60 + ce_verify.FETCH_SECONDS + ce_verify.SETUP_SECONDS + 300  # the tool's own limit only starts after fetch and setup
CHECK_SECONDS = 300
CREDENTIALS = ("-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential")


class Stop(Exception):
    """This repository ends here, worded for the table; the others go on."""


class Skip(Exception):
    """Nothing to do for this repository: no error."""


def say(text):
    print(text, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------------------------------- reading

def read_raw(repo, path, ref):
    """A file at a ref with its line ends, None when missing. mc.read_file reads in text mode: CRLF would become LF and the whole file a diff."""
    target = f"repos/{repo}/contents/{quote(path)}?ref={quote(ref)}"
    try:
        done = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github.raw+json", target], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise mc.NotCheckable(f"gh läuft nicht: {error}") from error
    if done.returncode == 0:
        return done.stdout.decode("utf-8")
    problem = done.stderr.decode("utf-8", "replace")
    if "HTTP 404" in problem or "Not Found" in problem:
        return None
    raise mc.NotCheckable(f"{path} in {repo}: {problem.strip()[:200]}")


def registry():
    text = read_raw(REGISTRY_REPO, "repos.json", REGISTRY_BRANCH)
    repos = json.loads(text).get("repos") if text else None
    if not isinstance(repos, list):
        raise mc.NotCheckable("repos.json von Nexogen nicht lesbar")
    return repos


def target_commit(version):
    """The newest commit on main of ce-agent-tools whose plugin.json carries this version; None when there is none."""
    commits = mc.gh_json("api", f"repos/{mc.TOOLS_REPO}/commits?sha={mc.TOOLS_BRANCH}&path={quote(mc.TOOLS_PLUGIN)}&per_page=100")
    for item in commits:
        text = read_raw(mc.TOOLS_REPO, mc.TOOLS_PLUGIN, item["sha"])
        real = json.loads(text).get("version") if text else ""
        if real == version:
            return item["sha"]
        if mc.version_key(real) < mc.version_key(version):
            return None
    return None


def workflow_names(repo, ref):
    text = mc.gh("api", f"repos/{repo}/contents/{WORKFLOWS}?ref={quote(ref)}")
    found = json.loads(text) if text else []
    return [f"{WORKFLOWS}/{item['name']}" for item in found if str(item.get("name")).endswith((".yml", ".yaml"))]


def pulls_of(repo, branch, state):
    owner = repo.split("/")[0]
    return mc.gh_json("api", f"repos/{repo}/pulls?state={state}&head={quote(owner + ':' + branch, safe='')}&per_page=5")


def branch_exists(repo, branch):
    return mc.gh("api", f"repos/{repo}/branches/{quote(branch, safe='')}") is not None


def own_commit(repo, branch, base_sha, title):
    """The head of a branch when its one commit has this title on this very base, else None. What it holds says differences()."""
    found = mc.gh_json("api", f"repos/{repo}/commits/{quote(branch, safe='')}")
    parents = [item.get("sha") for item in found.get("parents") or []]
    return found.get("sha") if parents == [base_sha] and str((found.get("commit") or {}).get("message")).strip() == title else None


def differences(repo, base_sha, head, plan):
    """The files in which a head is not this plan: others than planned, or planned ones with other content. Empty when it is exactly the plan."""
    listed = mc.gh_json("api", f"repos/{repo}/compare/{base_sha}...{head}?per_page=100").get("files") or []
    names = {item["filename"] for item in listed}
    other = names ^ set(plan["changes"])
    other |= {path for path in names & set(plan["changes"]) if read_raw(repo, path, head) != plan["changes"][path]}
    return sorted(other)


# --------------------------------------------------------------------------------------------------- planning

def pin_number(version):
    """The word of branch and entry: 0.42.0 is 42, 0.41.1 is 41-1, 1.2.0 is 1-2."""
    parts = version.split(".")
    parts = parts[1:] if parts[0] == "0" else parts
    while len(parts) > 1 and parts[-1] == "0":
        parts.pop()
    return "-".join(parts)


def bump(version):
    """The next patch number: 0.115.9 is 0.115.10, 0.45 is 0.45.1."""
    parts = [int(part) for part in version.split(".")] + [0] * (3 - version.count(".") - 1)
    parts[-1] += 1
    return ".".join(str(part) for part in parts)


def newline_of(text):
    return "\r\n" if "\r\n" in text else "\n"


def set_pin(text, config, version, commit):
    """.ce-repo.json with the new pin, changed in place: a rewrite through json.dumps would reformat what the repository wrote by hand."""
    block = re.search(r'"tools_version"\s*:\s*\{[^{}]*\}', text)
    if not block:
        raise Stop("tools_version nicht als Block lesbar")
    old = config["tools_version"]
    new = block[0].replace(f'"{old["version"]}"', f'"{version}"').replace(old["commit"], commit)
    out = text[:block.start()] + new + text[block.end():]
    if json.loads(out) != dict(config, tools_version=dict(old, version=version, commit=commit)):
        raise Stop("tools_version nicht eindeutig ersetzbar")
    return out


def add_entry(text, heading, body):
    """A changelog entry above the first entry."""
    first = re.search(r"^## ", text, re.M)
    if not first:
        raise Stop("Changelog hat keinen Eintrag `## `")
    line = newline_of(text)
    return text[:first.start()] + heading + line * 2 + body + line * 2 + text[first.start():]


def add_line(text, after, new):
    """One more line below the line starting with `after`."""
    found = re.search("^" + re.escape(after) + r"[^\n]*\n", text, re.M)
    if not found:
        raise Stop(f"Zeile `{after}` nicht gefunden")
    return text[:found.end()] + new + newline_of(text) + text[found.end():]


def plan_repo(key, config, config_text, read, workflows, target, data, today):
    """The new text of every file of this pin; `read` gives a file at the base, None when it is missing. Writes nothing."""
    version, commit = target
    old_pin = mc.pin_of(config)
    if old_pin is None:
        raise Stop("tools_version ungültig")
    spec = mc.version_spec(config)
    if spec is None:
        raise Stop("`version` in .ce-repo.json ist kein {file, pattern}: keine Versionsdatei, keine Nummer zu erhöhen")
    rules = {**data["defaults"], **data["repos"].get(key, {})}
    changes, notes = {ce_common.CONFIG_NAME: set_pin(config_text, config, version, commit)}, []

    def current(path):
        text = changes[path] if path in changes else read(path)
        if text is None:
            raise Stop(f"{path} fehlt")
        return text

    found = re.search(spec["pattern"], current(spec["file"]))
    if not found or not found.groups():
        raise Stop(f"Version nicht lesbar in {spec['file']}")
    old_version, new_version = found.group(1), bump(found.group(1))
    words = {"version": new_version, "old_version": old_version, "number": pin_number(version), "tools_version": version, "old_tools": old_pin[0],
             "commit7": commit[:7], "date": today.strftime("%d.%m.%Y")}
    if spec["file"] != rules["changelog"]:
        changes[spec["file"]] = current(spec["file"])[:found.start(1)] + new_version + current(spec["file"])[found.end(1):]
    changes[rules["changelog"]] = add_entry(current(rules["changelog"]), rules["heading"].format(**words), rules["body"].format(**words))
    for extra in rules.get("also", []):
        changes[extra["file"]] = add_line(current(extra["file"]), extra["after"].format(**words), extra["line"].format(**words))
    loader = read_loader(commit)
    changes["ce"] = ce_sync.embed(loader, version)
    for path in workflows:
        text = current(path)
        for other in sorted(set(ACTION_PIN.findall(text)) - {old_pin[1]}):
            notes.append(f"{path} bleibt auf {other[:7]}")
        if old_pin[1] in ACTION_PIN.findall(text):
            changes[path] = text.replace(f"ce-tools@{old_pin[1]}", f"ce-tools@{commit}")
    check = re.search(spec["pattern"], changes[spec["file"]])
    if not check or check.group(1) != new_version:
        raise Stop(f"Muster von `version` liest nach der Änderung nicht {new_version}: Überschrift und Muster passen nicht zusammen")
    changes = {path: text for path, text in changes.items() if text != read(path)}
    return {"changes": changes, "old": {path: read(path) or "" for path in changes}, "version": (old_version, new_version), "notes": notes}


def read_loader(commit):
    text = read_raw(mc.TOOLS_REPO, LOADER_SOURCE, commit)
    if text is None:
        raise Stop(f"Commit {commit[:7]} hat keinen Lader")
    return text


def diff_of(plan):
    """(unified diff, changed lines) of a plan."""
    lines = []
    for path in sorted(plan["changes"]):
        lines += difflib.unified_diff(plan["old"][path].splitlines(True), plan["changes"][path].splitlines(True), f"a/{path}", f"b/{path}", n=1)
    count = sum(1 for line in lines if line[:1] in "+-" and line[:3] not in ("+++", "---"))
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines), count


def forecast(config, plan, lines):
    """What merge-check will say about the stage, as far as the plan shows it."""
    patterns = config.get("core_paths")
    if not (isinstance(patterns, list) and any(isinstance(item, str) and item.strip() for item in patterns)):
        return "Stufe 2: core_paths leer"
    touched = [name for name in mc.core_hits([{"filename": path} for path in plan["changes"]], patterns) if name != ce_common.CONFIG_NAME]
    if touched:
        return f"Stufe 2: Kernpfad {', '.join(touched)}"
    return f"Stufe 2: {lines} Zeilen" if lines > mc.NO_JA_LINES else "Stufe 1 möglich"


# ---------------------------------------------------------------------------------------------- throwaway copy

def cache():
    return Path(os.environ.get("CE_PIN_CACHE") or heavy_lock.cache_dir("ce_pin"))


def sweep(now=None):
    """Copies a hard kill left behind; only inside this tool's folder."""
    now = now or time.time()
    for item in (cache() / "runs").glob("*"):
        try:
            if now - item.lstat().st_mtime > KEEP_RUN_DAYS * 86400:
                shutil.rmtree(item, ignore_errors=True)
        except OSError:  # a parallel run got there first
            pass


def git_env():
    return {**ce_verify.clean_env(), "GIT_LFS_SKIP_SMUDGE": "1", "GIT_TERMINAL_PROMPT": "0"}


def git(root, what, *args):
    return ce_verify.step(["git", "-C", root, *CREDENTIALS, *args], GIT_SECONDS, what, git_env())


def open_copy(repo, sha, paths):
    """This commit in a new folder with just these files: the trees come, blobs on demand, never a checkout of the repository."""
    (cache() / "runs").mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=repo.split("/")[-1] + "-", dir=cache() / "runs"))
    try:
        ce_verify.step(["git", "init", "-q", root], 60, "git init", git_env())
        git(root, "git remote", "remote", "add", "origin", REMOTE.format(repo=repo))
        git(root, f"Commit {sha[:7]} nicht geladen", "fetch", "-q", "--depth", "1", "--no-tags", "--filter=blob:none", "origin", sha)
        git(root, "sparse-checkout", "sparse-checkout", "set", "--no-cone", *[f"/{path}" for path in paths])
        git(root, "checkout", "-c", f"core.hooksPath={os.devnull}", "checkout", "-q", "--detach", "FETCH_HEAD")
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root


def commit_and_push(root, plan, branch, title):
    for path, text in plan["changes"].items():
        (root / path).write_bytes(text.encode("utf-8"))  # bytes: line ends as they are; the mode of ./ce stays
    git(root, "git add", "add", "--", *plan["changes"])
    git(root, "git commit", "commit", "-q", "-m", title)
    head = git(root, "git rev-parse", "rev-parse", "HEAD")
    git(root, f"Push nach {branch}", "push", "-q", "origin", f"HEAD:refs/heads/{branch}")  # no force: a branch that exists stops the run before this
    return head


def run_tool(name, args, cwd, seconds):
    """(exit code, JSON answer) of a tool next to this one; it says its own lines on stderr."""
    try:
        done = subprocess.run([sys.executable, str(HERE / name), *args], cwd=cwd, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                              timeout=seconds, env=ce_verify.clean_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Stop(f"{name}: {error}") from error
    try:
        return done.returncode, json.loads(done.stdout)
    except ValueError as error:
        raise Stop(f"{name}: keine JSON-Antwort (Exit {done.returncode})") from error


# ------------------------------------------------------------------------------------------------------- a run

def new_row(entry):
    return {"repo": entry.get("name") or entry.get("id"), "pin": "", "version": "", "where": "", "lines": "", "check": "", "state": "", "notes": [], "bad": False, "merge": ""}


def pr_body(version, commit, plan, lines):
    files = ", ".join(f"`{path}`" for path in sorted(plan["changes"]))
    return (f"**Fertig.** Pin auf Agent Tools {version} (`{commit[:12]}`), `./ce` als Lader dieses Stands.\n\n- Geändert: {files} ({lines} Zeilen).\n"
            "- Geprüft: CI oder `./ce verify` und `./ce merge-check`, Stand im Lauf von `./ce pin`.\n\nAngelegt von `./ce pin`.\n")


def prepare(entry, target, data, dry, today, row):
    """Up to the pull request; the job for finish(), None in a dry run."""
    repo, key = entry["github"], entry.get("id")
    version, commit = target
    if entry.get("status") == "stillgelegt":
        raise Skip("stillgelegt")
    if entry.get("hold"):
        raise Skip(f"gesperrt: {entry['hold']}")
    base = mc.gh_json("api", f"repos/{repo}")["default_branch"]
    sha = mc.gh_json("api", f"repos/{repo}/commits/{quote(base)}")["sha"]
    text = read_raw(repo, ce_common.CONFIG_NAME, sha)
    config = json.loads(text) if text else {}
    if "tools_version" not in config:
        raise Skip("kein Pin")
    old = mc.pin_of(config)
    row["pin"] = f"{old[0] if old else '?'} → {version}"
    if old == target:
        raise Skip("schon auf diesem Stand")
    if old and mc.version_key(old[0]) > mc.version_key(version):
        raise Skip(f"steht auf {old[0]}, neuer als das Ziel")
    no_ci = mc.workflow_files(repo, base) == 0
    row["check"] = "./ce verify" if no_ci else "GitHub-CI"
    branch = f"claude/{ce_session_name.patch_word(config.get('name', repo.split('/')[-1]))}-patch-pin{pin_number(version)}"
    row["where"] = branch
    plan = plan_repo(key, config, text, functools.lru_cache(maxsize=None)(lambda path: read_raw(repo, path, sha)), workflow_names(repo, sha), target, data, today)
    diff, lines = diff_of(plan)
    title = f"Patch pin{pin_number(version)} {plan['version'][1]}: Pin auf Agent Tools {version}"
    row["version"] = " → ".join(plan["version"])
    row["lines"] = str(lines)
    row["notes"] = plan["notes"]
    pulls = pulls_of(repo, branch, "open")
    if pulls:
        pull = pulls[0]
        head = pull["head"]["sha"]
        differs = differences(repo, sha, head, plan)
        row["where"] = f"PR {pull['number']}"
        row["state"] = "PR offen, ein echter Lauf setzt hier fort"
        row["notes"] = plan["notes"] + ([f"Head weicht vom Plan ab: {', '.join(differs)}"] if differs else [])
        if dry:
            return None
        return {"repo": repo, "pr": pull["number"], "head": head, "base": base, "no_ci": no_ci, "config": config, "copy": open_copy(repo, head, [ce_common.CONFIG_NAME]), "row": row, "differs": differs}
    head = None
    if branch_exists(repo, branch):
        gone = pulls_of(repo, branch, "all")
        if gone:
            raise Stop(f"Branch {branch} besteht, PR {gone[0]['number']} ist geschlossen: nichts angelegt, Branch prüfen oder löschen")
        head = own_commit(repo, branch, sha, title)
        if head is None or differences(repo, sha, head, plan):
            raise Stop(f"Branch {branch} besteht ohne offenen PR und ist nicht genau der Plan dieses Laufs (Basis, Titel, Inhalt oder Datum im Eintrag): nichts überschrieben, Branch prüfen oder löschen")
    row["state"] = forecast(config, plan, lines)
    if dry:
        if head:
            row["state"] += "; Branch gepusht, ein echter Lauf legt den PR an"
        print(f"### {row['repo']} ({repo})\n```diff\n{diff}```\n", flush=True)
        return None
    root = open_copy(repo, head or sha, sorted(plan["changes"]))
    job = {"repo": repo, "base": base, "no_ci": no_ci, "config": config, "copy": root, "row": row, "differs": []}
    try:
        job["head"] = head or commit_and_push(root, plan, branch, title)
        try:
            answer = mc.gh("pr", "create", "-R", repo, "--base", base, "--head", branch, "--title", title, "--body", pr_body(version, commit, plan, lines))
        except mc.NotCheckable as error:
            raise Stop(f"Branch {branch} gepusht, PR nicht angelegt, ein zweiter Lauf holt das bei gleicher Basis nach: {error}") from error
        number = re.search(r"/pull/(\d+)", answer or "")
        if not number:
            raise Stop(f"Branch {branch} gepusht, PR nicht angelegt, ein zweiter Lauf holt das bei gleicher Basis nach: {(answer or '').strip()[:100]}")
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    job["pr"] = int(number[1])
    row["where"] = f"PR {job['pr']}"
    say(f"pin {repo}: PR {job['pr']} angelegt, Head {job['head'][:7]}")
    return job


def wait_for_ci(repo, head, seconds, sleep=time.sleep):
    """Until a check has come and none is open; merge-check says what the wait did not settle."""
    deadline = time.monotonic() + seconds
    while True:
        checks = mc.read_checks(repo, head)
        _, pending = mc.ci_state(checks)
        runs = mc.gh_json("api", f"repos/{repo}/actions/runs?head_sha={head}&per_page=100").get("workflow_runs") or []
        if (checks and not pending and all(run.get("status") == "completed" for run in runs)) or time.monotonic() >= deadline:
            return
        sleep(POLL_SECONDS)


def finish(job, minutes):
    """verify where needed, the wait for CI, then merge-check from the copy. Fills the row."""
    repo, number, head, row = job["repo"], job["pr"], job["head"], job["row"]
    if job["no_ci"]:
        if mc.verify_problem(mc.read_verify(repo, head), repo, head, job["config"]):
            code, answer = run_tool("ce_verify.py", [repo, str(number)], job["copy"], VERIFY_SECONDS)
            if code:
                raise Stop(f"./ce verify: {answer.get('error') or answer.get('reason') or f'Exit {code}'}")
        row["check"] = "./ce verify grün"
    else:
        say(f"pin {repo}: wartet auf CI, höchstens {minutes} min")
        wait_for_ci(repo, head, minutes * 60)
    code, answer = run_tool("ce_merge_check.py", [repo, str(number), "--sha", head], job["copy"], CHECK_SECONDS)
    if code == 2 or "green" not in answer:
        raise Stop(f"merge-check nicht prüfbar: {answer.get('error', '')}")
    if not answer["green"]:
        row["bad"] = True
        row["state"] = "merge-check rot: " + "; ".join(answer.get("reasons") or [])
    elif job["differs"]:
        row["state"] = "merge-check grün, keine Merge-Zeile: Head ist nicht dieser Plan, der Nutzer entscheidet"
    elif answer.get("ohne_ja") is True:
        row["state"] = "merge-check grün, Stufe 1"
        row["merge"] = f"gh pr merge {number} -R {repo} --match-head-commit {head} --squash"
    else:
        row["state"] = "merge-check grün, Stufe 2: " + "; ".join(answer.get("ja_noetig_weil") or [])


def guarded(row, step, *args):
    """A step of one repository: whatever stops it stays in its row."""
    try:
        return step(*args)
    except Skip as reason:
        row["state"] = f"ausgelassen: {reason}"
    except (Stop, mc.NotCheckable, OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, re.error, subprocess.SubprocessError) as error:
        row["state"] = f"gestoppt: {error}"
        row["bad"] = True
    return None


def table(rows):
    head = ("Repo", "Pin", "Version", "Branch oder PR", "Zeilen", "Prüfung", "Stand")
    keys = ("repo", "pin", "version", "where", "lines", "check", "state")
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for row in rows:
        cells = {**row, "state": "; ".join([row["state"], *row["notes"]])}
        lines.append("| " + " | ".join(str(cells[key]).replace("|", "/").replace("\n", " ") for key in keys) + " |")
    return "\n".join(lines)


def ids(text):
    return {part.strip() for part in text.split(",") if part.strip()}


def main(argv=None):
    ce_common.utf8_console()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("version", help="Version von ce-agent-tools, z. B. 0.42.0")
    parser.add_argument("--dry-run", action="store_true", help="alles zeigen, nichts schreiben")
    parser.add_argument("--only", default="", help="nur diese ids aus repos.json, mit Komma")
    parser.add_argument("--skip", default="", help="diese ids auslassen, mit Komma")
    parser.add_argument("--wait-minutes", type=int, default=WAIT_MINUTES, help="Wartezeit auf CI je Repo")
    args = parser.parse_args(argv)
    version = args.version + ".0" if args.version.count(".") == 1 else args.version
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        parser.error("Version wie 0.42.0 erwartet")
    if not args.dry_run and (mc.on_cloud() or not ce_verify.on_mac()):
        say("./ce pin läuft nur am Mac; Cloud-Sitzungen schicken eine Bitte an Sightline")
        return 2
    try:
        data = json.loads(DATA.read_text(encoding="utf-8"))
        if data.get("schema_version") != DATA_SCHEMA:
            raise ValueError(f"{DATA.name}: schema_version {data.get('schema_version')!r}, erwartet {DATA_SCHEMA}")
        commit = target_commit(version)
        entries = registry()
    except (mc.NotCheckable, OSError, ValueError) as error:
        say(f"pin: nicht lesbar: {error}")
        return 2
    if commit is None:
        say(f"pin: Version {version} liegt nicht auf {mc.TOOLS_BRANCH} von ce-agent-tools")
        return 2
    only, skip = ids(args.only), ids(args.skip)
    unknown = (only | skip) - {entry.get("id") for entry in entries}
    if unknown:
        say(f"pin: unbekannte ids: {', '.join(sorted(unknown))}")
        return 2
    say(f"pin: Ziel Agent Tools {version} ({commit[:12]}){', Trockenlauf' if args.dry_run else ''}")
    if not args.dry_run:
        sweep()
    rows, jobs, today = [], [], date.today()
    try:
        for entry in entries:
            if (only and entry.get("id") not in only) or entry.get("id") in skip:
                continue
            row = new_row(entry)
            rows.append(row)
            job = guarded(row, prepare, entry, (version, commit), data, args.dry_run, today, row)
            if job:
                jobs.append(job)
        for job in jobs:
            guarded(job["row"], finish, job, args.wait_minutes)
    finally:
        for job in jobs:
            shutil.rmtree(job["copy"], ignore_errors=True)
    print(table(rows))
    merges = [row["merge"] for row in rows if row["merge"]]
    if merges:
        print("\nStufe 1, nichts ist gemergt; Sightline führt jede Zeile allein aus, nach 30 Minuten erst nach einem neuen ./ce pin:\n" + "\n".join(merges))
    return 1 if any(row["bad"] for row in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
