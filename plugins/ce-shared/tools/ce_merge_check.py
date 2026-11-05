#!/usr/bin/env python3
"""ce_merge_check.py <owner/repo> <PR> [--sha SHA] [--ja ZITAT] - the mechanical half of a merge review (0.34 Gatekeeper).

Reads one pull request from GitHub (`gh`, read only) and answers green or red with reasons, plus the classification
Patch or Version. Logic errors stay with the judge and Sightline: this only finds what a machine can find (Trimline 12).

Checks: open and no draft, head SHA (against --sha), base branch contained, conflicts, CI, files over 5 MB, report.md in the
diff, deleted or weakened tests, version file and changelog against .ce-repo.json, core paths (.ce-repo.json > core_paths
of the base branch, so a pull request cannot take its own files off the list). Version = over PATCH_LINES changed lines or
a core path touched, else Patch. A core path on a mere patch number is red; size alone only warns, the rule says "etwa".

Output: the result as JSON on stdout, one text line on stderr. Exit 0 green, 1 red, 2 not checkable (never green).

Receipt: every run first removes the receipt of this worktree; a green run with --ja "<the user's words>" writes a new one to
<git dir>/ce-merge-check.json. With it the guard lets this session run `gh pr merge <PR> -R <repo> --match-head-commit <SHA>`
for 30 minutes (guard.py > merge_attempt, rule merge-nur-sightline). The quote records the Ja, it does not prove it.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ce_common  # noqa: E402

BIG_FILE = 5_000_000  # bytes; the same limit as BIG_BINARY in githooks/ce_githooks.py (a test compares them)
PATCH_LINES = 100
RECEIPT_FILE = "ce-merge-check.json"  # read by guard.py
SIZE_CHUNK = 100
MERGEABLE_TRIES = 4
UTF8 = {"encoding": "utf-8", "errors": "replace"}
PR_FIELDS = "number,url,title,state,isDraft,headRefOid,headRefName,baseRefName,mergeable,additions,deletions,statusCheckRollup"

TEST_PATH = re.compile(r"(?:^|/)(?:tests?|Tests?|__tests__)/|(?:^|/)test_[^/]+$|_test\.\w+$|\.(?:test|spec)\.\w+$|Tests?\.cs$")
TEST_CODE = re.compile(r"\.(?:py|cs|js|jsx|ts|tsx|go|sh)$")
TEST_NAME = re.compile(r"(?:^|/)test_[^/]+$|_test\.\w+$|\.(?:test|spec)\.\w+$|Tests?\.cs$")
TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test|^\s*\[(?:Test|UnityTest|TestCase|Fact|Theory)\b|^\s*(?:it|test)\s*\(")
ASSERTION = re.compile(r"\bassert|\bAssert\.|\bexpect\(")
SKIP = re.compile(r"^\s*@(?:unittest\.skip|pytest\.mark\.(?:skip|xfail))\b|^\s*\[Ignore\b|^\s*(?:it|test|describe)\.(?:skip|only)\(|"
                  r"^\s*x(?:it|describe)\(")
CI_BAD = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
CI_FINE = {"SUCCESS", "NEUTRAL", "SKIPPED"}


class NotCheckable(Exception):
    """GitHub or the input cannot answer; worded for the user."""


# ----------------------------------------------------------------------------------------------- reading GitHub

def gh(*args):
    """stdout of a gh call; raises NotCheckable. A 404 comes back as None."""
    try:
        done = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, **UTF8)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NotCheckable(f"gh läuft nicht: {error}") from error
    if done.returncode == 0:
        return done.stdout
    if "HTTP 404" in done.stderr or "Not Found" in done.stderr:
        return None
    raise NotCheckable(f"gh {' '.join(args[:2])}: {done.stderr.strip()[:200]}")


def gh_json(*args):
    text = gh(*args)
    if text is None:
        raise NotCheckable(f"nicht gefunden: {' '.join(args[:3])}")
    try:
        return json.loads(text)
    except ValueError as error:
        raise NotCheckable(f"gh {args[0]}: keine JSON-Antwort") from error


def read_file(repo, path, ref):
    """Text of a file at a ref, None when it does not exist there."""
    return gh("api", "-H", "Accept: application/vnd.github.raw+json", f"repos/{repo}/contents/{quote(path)}?ref={quote(ref)}")


def read_config(repo, ref):
    text = read_file(repo, ce_common.CONFIG_NAME, ref)
    try:
        config = json.loads(text) if text else None
    except ValueError:
        return None
    return config if isinstance(config, dict) else None


def changed_files(repo, number):
    files, page = [], 1
    while True:
        batch = gh_json("api", f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}")
        files += batch
        if len(batch) < 100 or page >= 30:  # GitHub lists at most 3000 files
            return files
        page += 1


def blob_sizes(repo, head, paths):
    """{path: bytes} from one GraphQL object lookup per chunk; submodules and the like have no size."""
    owner, name = repo.split("/", 1)
    sizes = {}
    for start in range(0, len(paths), SIZE_CHUNK):
        chunk = paths[start:start + SIZE_CHUNK]
        fields = " ".join(f"f{i}: object(expression: {json.dumps(f'{head}:{path}')}) {{ ... on Blob {{ byteSize }} }}"
                          for i, path in enumerate(chunk))
        query = f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {fields} }} }}"
        data = gh_json("api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}")
        found = ((data.get("data") or {}).get("repository")) or {}
        for i, path in enumerate(chunk):
            size = (found.get(f"f{i}") or {}).get("byteSize")
            if size is not None:
                sizes[path] = size
    return sizes


def read_pr(repo, number, sleep=time.sleep):
    """GitHub computes mergeability on demand: the first answers can be UNKNOWN."""
    for attempt in range(MERGEABLE_TRIES):
        pr = gh_json("pr", "view", str(number), "-R", repo, "--json", PR_FIELDS)
        if pr.get("mergeable") != "UNKNOWN":
            break
        if attempt + 1 < MERGEABLE_TRIES:
            sleep(3)
    return pr


def version_at(repo, spec, ref):
    """The version string at a ref, None when the file or the pattern gives none."""
    text = read_file(repo, spec["file"], ref)
    try:
        match = re.search(spec["pattern"], text) if text else None
    except re.error:
        return None
    return match.group(1) if match and match.groups() else None


def version_spec(config):
    spec = (config or {}).get("version")
    return spec if isinstance(spec, dict) and spec.get("file") and spec.get("pattern") else None


def changelog_of(config):
    return ((config or {}).get("docs") or {}).get("changelog")


def gather(repo, number):
    """Everything evaluate() needs; the only part of this tool that talks to GitHub."""
    pr = read_pr(repo, number)
    head, base = pr["headRefOid"], pr["baseRefName"]
    files = changed_files(repo, number)
    present = [f["filename"] for f in files if f.get("status") != "removed"]
    base_config, head_config = read_config(repo, base), read_config(repo, head)
    spec = version_spec(base_config) or version_spec(head_config)  # the base decides: a pull request cannot swap its own check
    versions = {"base": version_at(repo, spec, base), "head": version_at(repo, spec, head)} if spec else None
    changelog_name = changelog_of(base_config) or changelog_of(head_config)
    compare = gh_json("api", f"repos/{repo}/compare/{quote(base)}...{head}")
    facts = {"repo": repo, "pr": pr, "files": files, "sizes": blob_sizes(repo, head, present), "behind_by": compare.get("behind_by"),
             "base_config": base_config, "head_config": head_config, "versions": versions, "changelog_name": changelog_name,
             "changelog": read_file(repo, changelog_name, head) if changelog_name else None}
    now = gh_json("pr", "view", str(number), "-R", repo, "--json", "headRefOid").get("headRefOid")
    if now != head:  # the file list follows the pull request, not the SHA: a push in between mixes two states
        raise NotCheckable(f"Head hat sich während der Prüfung geändert ({head[:7]} → {str(now)[:7]})")
    return facts


# ------------------------------------------------------------------------------------------------------- judging

def glob_regex(pattern):
    """A core_paths pattern: `*` stays inside a folder, `**` crosses folders, a trailing / means everything below."""
    pattern = pattern + "**" if pattern.endswith("/") else pattern
    parts, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(parts) + r"\Z")


def core_hits(files, patterns):
    regexes = [glob_regex(p) for p in patterns if isinstance(p, str) and p]
    names = {name for f in files for name in (f["filename"], f.get("previous_filename")) if name}
    return sorted(name for name in names if any(r.match(name) for r in regexes))


def ci_state(rollup):
    """(failed names, pending names) of statusCheckRollup: check runs carry status/conclusion, commit statuses state."""
    failed, pending = [], []
    for item in rollup or []:
        name = item.get("name") or item.get("context") or "?"
        if "state" in item and "status" not in item:
            state = item.get("state")
        elif item.get("status") != "COMPLETED":
            state = "PENDING"
        else:
            state = item.get("conclusion")
        if state in CI_BAD:
            failed.append(name)
        elif state not in CI_FINE:
            pending.append(name)
    return failed, pending


def test_changes(files):
    """(reasons, unread files): removed test code that nothing replaces, and new skip markers. Counts run over the whole
    pull request, so a test that moves to another file is no loss."""
    defs = asserts = 0
    skips, deleted, unread, renamed = [], [], [], []
    for f in files:
        name, old = f["filename"], f.get("previous_filename") or ""
        if TEST_PATH.search(old) and TEST_CODE.search(old) and (not TEST_PATH.search(name) or (TEST_NAME.search(old) and not TEST_NAME.search(name))):
            renamed.append(old)  # no line changes, but the runner no longer finds it
        if not TEST_PATH.search(name) or not TEST_CODE.search(name):
            continue
        if f.get("status") == "removed":
            deleted.append(name)
        patch = f.get("patch")
        if patch is None:
            if f.get("changes"):
                unread.append(name)
            continue
        for line in patch.split("\n"):
            sign, text = line[:1], line[1:]
            if sign not in "+-" or not sign:
                continue
            step = 1 if sign == "+" else -1
            defs += step if TEST_DEF.search(text) else 0
            asserts += step if ASSERTION.search(text) else 0
            if sign == "+" and SKIP.search(text):
                skips.append(name)
    reasons = []
    if defs < 0 or asserts < 0:
        where = f" (gelöscht: {', '.join(deleted[:5])})" if deleted else ""
        reasons.append(f"Tests abgeschwächt: {-min(defs, 0)} Testfälle und {-min(asserts, 0)} Prüfungen weniger{where}")
    elif any(name in unread for name in deleted):
        reasons.append(f"Testdatei gelöscht, Inhalt nicht lesbar: {', '.join(n for n in deleted if n in unread)}")
    if renamed:
        reasons.append(f"Tests aus dem Testlauf verschoben: {', '.join(renamed[:5])}")
    if skips:
        reasons.append(f"Tests übersprungen: {', '.join(sorted(set(skips))[:5])}")
    return reasons, [name for name in unread if name not in deleted]


def version_key(text):
    return tuple(int(part) for part in re.findall(r"\d+", text or ""))


def version_reasons(facts, classification, touched_core):
    """(reasons, warnings) of the version file and the changelog, as .ce-repo.json describes them."""
    versions, name = facts.get("versions"), facts.get("changelog_name")
    reasons, warnings = [], []
    if facts.get("head_config") is None:
        if facts.get("base_config") is None:
            return [], [f"{ce_common.CONFIG_NAME} fehlt oder ist unlesbar: Version und CHANGELOG nicht geprüft"]
        reasons.append(f"{ce_common.CONFIG_NAME} im PR entfernt oder unlesbar")
    if versions is None:
        warnings.append("Versionsdatei in .ce-repo.json nicht angegeben: Version nicht geprüft")
    elif not versions["head"]:
        reasons.append("Version im PR nicht lesbar")
    elif not versions["base"]:
        warnings.append("Version der Basis nicht lesbar: Erhöhung nicht geprüft")
    else:
        old, new = version_key(versions["base"]), version_key(versions["head"])
        if new <= old:
            reasons.append(f"Version nicht erhöht ({versions['base']} → {versions['head']})")
        elif classification == "Version" and new[:2] == old[:2]:
            text = f"Einstufung Version, aber nur Patch-Nummer erhöht ({versions['base']} → {versions['head']})"
            (reasons if touched_core else warnings).append(text)
    if not name:
        warnings.append("docs.changelog fehlt in .ce-repo.json: CHANGELOG nicht geprüft")
    elif name not in {f["filename"] for f in facts["files"]}:
        reasons.append(f"{name} nicht geändert")
    elif versions and versions["head"] and versions["head"] not in (facts.get("changelog") or ""):
        reasons.append(f"{name} nennt Version {versions['head']} nicht")
    if all(f["filename"].endswith(".md") for f in facts["files"]):  # doc patches without a number are practice in critic, formgen
        return [], reasons + warnings
    return reasons, warnings


def evaluate(facts, expected_sha=""):
    pr, files = facts["pr"], facts["files"]
    head = pr["headRefOid"]
    reasons, warnings = [], []
    if pr.get("state") != "OPEN":
        reasons.append(f"PR ist nicht offen ({pr.get('state')})")
    if pr.get("isDraft"):
        reasons.append("PR ist ein Entwurf")
    if expected_sha and not head.startswith(expected_sha.lower()):
        reasons.append(f"Head ist {head[:12]}, erwartet {expected_sha[:12]}")
    behind = facts.get("behind_by")
    if behind is None:
        reasons.append(f"Stand gegen {pr['baseRefName']} nicht lesbar")
    elif behind:
        reasons.append(f"{pr['baseRefName']} nicht enthalten ({behind} Commits zurück)")
    mergeable = pr.get("mergeable")
    if mergeable == "CONFLICTING":
        reasons.append("Konflikt mit der Basis")
    elif mergeable != "MERGEABLE":
        reasons.append(f"Mergebarkeit unbekannt ({mergeable})")
    failed, pending = ci_state(pr.get("statusCheckRollup"))
    if failed:
        reasons.append(f"CI rot: {', '.join(failed[:5])}")
    if pending:
        reasons.append(f"CI läuft noch: {', '.join(pending[:5])}")
    sizes = facts.get("sizes") or {}
    big = sorted(((size, name) for name, size in sizes.items() if size > BIG_FILE), reverse=True)
    if big:
        reasons.append("Dateien über 5 MB: " + ", ".join(f"{name} ({size / 1e6:.1f} MB)" for size, name in big[:5]))
    if any(f["filename"] == "report.md" and f.get("status") != "removed" for f in files):
        reasons.append("report.md im Diff")
    test_reasons, unread = test_changes(files)
    reasons += test_reasons
    if unread:
        warnings.append(f"Testdateien ohne Diff von GitHub, nicht bewertet: {', '.join(unread[:5])}")
    config = facts.get("base_config") or {}
    patterns = config.get("core_paths")
    if not isinstance(patterns, list):
        patterns = []
        warnings.append("core_paths fehlt in .ce-repo.json der Basis: Kernpfade nicht geprüft")
    touched = core_hits(files, patterns)
    lines = (pr.get("additions") or 0) + (pr.get("deletions") or 0)
    classification = "Version" if touched or lines > PATCH_LINES else "Patch"
    version_problems, version_warnings = version_reasons(facts, classification, bool(touched))
    reasons += version_problems
    warnings += version_warnings
    return {"repo": facts["repo"], "pr": pr["number"], "url": pr.get("url"), "head": head, "base": pr["baseRefName"],
            "green": not reasons, "classification": classification, "lines": lines, "files": len(files),
            "core_paths_touched": touched, "reasons": reasons, "warnings": warnings}


def text_line(result):
    where = f"{result['repo']}#{result['pr']} {result['head'][:7]}"
    size = f"Einstufung {result['classification']} ({result['lines']} Zeilen)"
    if result["green"]:
        return f"merge-check {where}: grün · {size}"
    return f"merge-check {where}: rot · {size} · " + "; ".join(result["reasons"])


# ------------------------------------------------------------------------------------------------------- receipt

def receipt_path(cwd=None):
    git_dir = ce_common.git_out(Path(cwd or os.getcwd()), "rev-parse", "--absolute-git-dir")
    return Path(git_dir) / RECEIPT_FILE if git_dir else None


def update_receipt(result, ja, cwd=None, now=time.time):
    """Drops the old receipt in every case; a new one needs green and the user's words. True when one was written."""
    path = receipt_path(cwd)
    if path is None:
        return False
    path.unlink(missing_ok=True)
    if not (result and result["green"] and ja.strip()):
        return False
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"repo": result["repo"], "pr": result["pr"], "head": result["head"], "green": True, "ja": ja.strip(),
                                "time": now()}, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return True


def main(argv=None):
    ce_common.utf8_console()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("repo", help="owner/repo")
    parser.add_argument("pr", type=int)
    parser.add_argument("--sha", default="", help="erwartete Head-SHA")
    parser.add_argument("--ja", default="", help="wörtliches Ja des Nutzers: ein grüner Lauf schreibt damit die Merge-Quittung")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        parser.error("owner/repo erwartet")
    update_receipt(None, "")  # first: an old receipt must not outlive a run that breaks off
    try:
        result = evaluate(gather(args.repo, args.pr), args.sha)
    except NotCheckable as error:
        print(json.dumps({"repo": args.repo, "pr": args.pr, "green": False, "error": str(error)}, ensure_ascii=False))
        print(f"merge-check {args.repo}#{args.pr}: nicht prüfbar · {error}", file=sys.stderr)
        return 2
    result["receipt"] = update_receipt(result, args.ja)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    print(text_line(result), file=sys.stderr)
    if result["green"] and not result["receipt"]:
        print("Keine Merge-Quittung: nach dem Ja des Nutzers erneut mit --ja \"<sein Wortlaut>\".", file=sys.stderr)
    return 0 if result["green"] else 1


if __name__ == "__main__":
    sys.exit(main())
