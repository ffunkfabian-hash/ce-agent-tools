#!/usr/bin/env python3
"""ce_merge_check.py <owner/repo> <PR> [--sha SHA] [--ja ZITAT] - the mechanical half of a merge review (0.34 Gatekeeper).

Reads one pull request from GitHub (`gh api`, REST only, read only: cloud sessions get HTTP 403 on GraphQL) and answers green or red with reasons, plus the classification
Patch or Version. Logic errors stay with the judge and Sightline: this only finds what a machine can find (Trimline 12).

Checks: open and no draft, head SHA (against --sha), base branch contained, conflicts, CI, files over 5 MB, report.md in the
diff, deleted or weakened tests, version file and changelog against .ce-repo.json, core paths (.ce-repo.json > core_paths
of the base branch, so a pull request cannot take its own files off the list).

Classification (regeln-details > Patch-Aufträge): Version = over VERSION_LINES changed lines or a core path touched, else Patch.
A core path on a mere patch number is red; size alone only warns.

Merge stages (regeln-details > Freigaben und Merge, 0.35): `ohne_ja` is true for stage 1 - green, at most NO_JA_LINES lines, no core
path, core_paths listed in the base, .ce-repo.json unchanged. No human stands at this door, so it also wants what a green run alone
does not say: the session's own pull request (same repository, head = this folder) into the default branch (a receipt survives
`gh pr edit --base`), no cloud session (stage 3), nothing left unchecked, and a successful check where CI can come at all (right
after a push none has reported yet; a repository without workflows and a pull request of .md files wait for nothing).
`ja_noetig_weil` names what stands against it. Everything
else is stage 2 (after the user's Ja) or stage 3 (merge request to Sightline). From JUDGE_LINES lines of code (diff without .md) a
warning asks for the judge; whether one ran is not visible here, that stays a rule.

Output: the result as JSON on stdout, one text line on stderr. Exit 0 green, 1 red, 2 not checkable (never green).

Receipt: every run first removes the receipt of this worktree; a green run then writes a new one to <git dir>/ce-merge-check.json:
kind `ja` with --ja "<the user's words>", else kind `patch` for stage 1. With it the guard lets this session run
`gh pr merge <PR> -R <repo> --match-head-commit <SHA>` for 30 minutes (guard.py > merge_attempt, rule merge-tuer). The quote
records the Ja, it does not prove it.
"""
import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ce_common  # noqa: E402
import ce_session_name  # noqa: E402

BIG_FILE = 5_000_000  # bytes; the same limit as BIG_BINARY in githooks/ce_githooks.py (a test compares them)
VERSION_LINES = 300  # classification: over this a change is a Version (Punkt 15)
NO_JA_LINES = 100  # stage 1: over this a merge needs the user's Ja (Punkt 13)
JUDGE_LINES = 100  # from this many lines of code a patch needs the judge too (regeln-details > Patch-Aufträge)
RECEIPT_FILE = "ce-merge-check.json"  # read by guard.py
PAGE = 100
MAX_PAGES = 30
MERGEABLE_TRIES = 4
UTF8 = {"encoding": "utf-8", "errors": "replace"}

TEST_PATH = re.compile(r"(?:^|/)(?:tests?|Tests?|__tests__)/|(?:^|/)test_[^/]+$|_test\.\w+$|\.(?:test|spec)\.\w+$|Tests?\.cs$")
TEST_CODE = re.compile(r"\.(?:py|cs|js|jsx|ts|tsx|go|sh)$")
TEST_NAME = re.compile(r"(?:^|/)test_[^/]+$|_test\.\w+$|\.(?:test|spec)\.\w+$|Tests?\.cs$")
TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test|^\s*\[(?:Test|UnityTest|TestCase|Fact|Theory)\b|^\s*(?:it|test)\s*\(")
ASSERTION = re.compile(r"\bassert|\bAssert\.|\bexpect\(")
SKIP = re.compile(r"^\s*@(?:unittest\.skip|pytest\.mark\.(?:skip|xfail))\b|^\s*\[Ignore\b|^\s*(?:it|test|describe)\.(?:skip|only)\(|"
                  r"^\s*x(?:it|describe)\(|^\s*self\.skipTest\(")
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


def paged(path, key=None):
    """All items of a REST list (`key` names the list inside an object answer); stops after MAX_PAGES. Second value: capped?"""
    items, page = [], 1
    while True:
        data = gh_json("api", f"{path}{'&' if '?' in path else '?'}per_page={PAGE}&page={page}")
        batch = data.get(key) or [] if key else data
        items += batch
        if len(batch) < PAGE:
            return items, False
        if page >= MAX_PAGES:
            return items, True
        page += 1


def changed_files(repo, number):
    """(files, capped): GitHub lists at most 3000 files of a pull request."""
    return paged(f"repos/{repo}/pulls/{number}/files")


def blob_sizes(repo, head, paths):
    """{path: bytes} for the given paths at the head: one recursive tree read; when GitHub cuts the tree off, one contents read
    per path. Submodules and the like have no size."""
    wanted = set(paths)
    if not wanted:
        return {}
    tree = gh_json("api", f"repos/{repo}/git/trees/{head}?recursive=1")
    if not tree.get("truncated"):
        return {i["path"]: i["size"] for i in tree.get("tree") or [] if i.get("type") == "blob" and i.get("path") in wanted and "size" in i}
    sizes = {}
    for path in sorted(wanted):
        found = gh("api", f"repos/{repo}/contents/{quote(path)}?ref={head}")
        try:
            info = json.loads(found) if found else None
        except ValueError:
            info = None
        if isinstance(info, dict) and info.get("type") == "file" and "size" in info:
            sizes[path] = info["size"]
    return sizes


def normalize_pr(raw):
    """The REST pull request in the shape evaluate() reads (GraphQL names: OPEN/MERGED, MERGEABLE/CONFLICTING/UNKNOWN)."""
    state = "MERGED" if raw.get("merged") else str(raw.get("state") or "").upper()
    mergeable = raw.get("mergeable")
    base_repo, head_repo = (raw.get("base") or {}).get("repo") or {}, (raw.get("head") or {}).get("repo") or {}
    return {"number": raw.get("number"), "url": raw.get("html_url"), "title": raw.get("title"), "state": state,
            "isDraft": bool(raw.get("draft")), "headRefOid": (raw.get("head") or {}).get("sha"),
            "headRefName": (raw.get("head") or {}).get("ref"), "baseRefName": (raw.get("base") or {}).get("ref"),
            "defaultBranch": base_repo.get("default_branch"),
            "sameRepo": bool(base_repo.get("full_name")) and head_repo.get("full_name") == base_repo.get("full_name"),
            "mergeable": {True: "MERGEABLE", False: "CONFLICTING"}.get(mergeable, "UNKNOWN"),
            "additions": raw.get("additions") or 0, "deletions": raw.get("deletions") or 0}


def read_pr(repo, number, sleep=time.sleep):
    """GitHub computes mergeability on demand: the first answers can be null (a closed pull request keeps it null)."""
    for attempt in range(MERGEABLE_TRIES):
        pr = normalize_pr(gh_json("api", f"repos/{repo}/pulls/{number}"))
        if pr["mergeable"] != "UNKNOWN" or pr["state"] != "OPEN":
            break
        if attempt + 1 < MERGEABLE_TRIES:
            sleep(3)
    return pr


def read_checks(repo, head):
    """The checks of a head commit in the shape of GraphQL's statusCheckRollup: check runs and commit statuses."""
    runs, _ = paged(f"repos/{repo}/commits/{head}/check-runs", "check_runs")
    statuses, _ = paged(f"repos/{repo}/commits/{head}/status", "statuses")
    return ([{"name": run.get("name"), "status": str(run.get("status")).upper(), "conclusion": str(run.get("conclusion") or "").upper()}
             for run in runs] +
            [{"context": item.get("context"), "state": str(item.get("state")).upper()} for item in statuses])


def workflow_count(repo):
    """GitHub workflows of the repository, switched off ones too (a session could switch one off before the push): 0 means no CI
    will ever report. Unreadable counts as one, so stage 1 keeps waiting for a check."""
    text = gh("api", f"repos/{repo}/actions/workflows?per_page=100")
    try:
        found = json.loads(text).get("workflows") if text else None
    except (ValueError, AttributeError):
        found = None
    return 1 if found is None else len(found)


def on_cloud():
    return ce_session_name.detect_place(platform.system(), dict(os.environ), "auto").endswith("Cloud")


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
    files, capped = changed_files(repo, number)
    present = [f["filename"] for f in files if f.get("status") != "removed"]
    base_config, head_config = read_config(repo, base), read_config(repo, head)
    spec = version_spec(base_config) or version_spec(head_config)  # the base decides: a pull request cannot swap its own check
    versions = {"base": version_at(repo, spec, base), "head": version_at(repo, spec, head)} if spec else None
    changelog_name = changelog_of(base_config) or changelog_of(head_config)
    compare = gh_json("api", f"repos/{repo}/compare/{quote(base)}...{head}?per_page=1")
    runs = gh_json("api", f"repos/{repo}/actions/runs?head_sha={head}&per_page=100").get("workflow_runs") or []
    facts = {"repo": repo, "workflows": workflow_count(repo), "local_head": ce_common.git_out(Path.cwd(), "rev-parse", "HEAD"), "cloud": on_cloud(),
             "runs_open": [run.get("name") or "?" for run in runs if run.get("status") != "completed"], "pr": pr, "checks": read_checks(repo, head), "files": files, "files_capped": capped, "sizes": blob_sizes(repo, head, present), "behind_by": compare.get("behind_by"),
             "base_config": base_config, "head_config": head_config, "versions": versions, "changelog_name": changelog_name,
             "changelog": read_file(repo, changelog_name, head) if changelog_name else None}
    now = (gh_json("api", f"repos/{repo}/pulls/{number}").get("head") or {}).get("sha")
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
    regexes = [glob_regex(p.strip()) for p in patterns if isinstance(p, str) and p.strip()]
    names = {name for f in files for name in (f["filename"], f.get("previous_filename")) if name}
    return sorted(name for name in names if any(r.match(name) for r in regexes))


def ci_state(rollup):
    """(failed names, pending names) of the checks (read_checks): check runs carry status/conclusion, commit statuses state."""
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


def ci_passed(rollup):
    """True when at least one check ended in success; SKIPPED and NEUTRAL say nothing about the head."""
    return any("SUCCESS" in (item.get("conclusion"), item.get("state")) for item in rollup or [])


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


def config_without_version(config):
    """A patch may raise a version kept right there; a {file, pattern} entry is part of the check and stays compared."""
    return {key: value for key, value in (config or {}).items() if not (key == "version" and isinstance(value, str))}


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
    checks = facts.get("checks")
    failed, pending = ci_state(checks)
    pending += [name for name in facts.get("runs_open") or [] if name not in pending]  # a queued run has no check entry yet
    if not checks and not pending:
        warnings.append("Keine Checks zum Head gemeldet: direkt nach einem Push kann der CI-Lauf noch fehlen")
    if failed:
        reasons.append(f"CI rot: {', '.join(failed[:5])}")
    if pending:
        reasons.append(f"CI läuft noch: {', '.join(pending[:5])}")
    sizes = facts.get("sizes") or {}
    big = sorted(((size, name) for name, size in sizes.items() if size > BIG_FILE), reverse=True)
    if big:
        reasons.append("Dateien über 5 MB: " + ", ".join(f"{name} ({size / 1e6:.1f} MB)" for size, name in big[:5]))
    if facts.get("files_capped"):
        reasons.append("Mehr als 3000 Dateien im PR: GitHub listet nicht alle, nicht prüfbar")
    if any(f["filename"] == "report.md" and f.get("status") != "removed" for f in files):
        reasons.append("report.md im Diff")
    test_reasons, unread = test_changes(files)
    reasons += test_reasons
    if unread:
        warnings.append(f"Testdateien ohne Diff von GitHub, nicht bewertet: {', '.join(unread[:5])}")
    config = facts.get("base_config") or {}
    patterns = config.get("core_paths")
    listed = isinstance(patterns, list) and any(isinstance(p, str) and p.strip() for p in patterns)  # an empty list guards nothing either
    if not listed:
        patterns = []
        warnings.append("core_paths fehlt in .ce-repo.json der Basis: Kernpfade nicht geprüft, keine Stufe 1")
    config_changed = listed and config_without_version(facts.get("head_config")) != config_without_version(config)  # the next patch would profit
    touched = core_hits(files, patterns)
    lines = (pr.get("additions") or 0) + (pr.get("deletions") or 0)
    code_lines = sum(f.get("changes") or 0 for f in files if not f["filename"].lower().endswith(".md"))
    classification = "Version" if touched or lines > VERSION_LINES else "Patch"
    version_problems, version_warnings = version_reasons(facts, classification, bool(touched))
    reasons += version_problems
    warnings += version_warnings
    if code_lines >= JUDGE_LINES:
        warnings.append(f"{code_lines} Zeilen Code (ohne .md): Richter nötig (`./ce judge`); ob einer lief, sieht merge-check nicht")
    ci_due = facts.get("workflows", 1) > 0 and not all(f["filename"].lower().endswith(".md") for f in files)
    unchecked = bool(unread) or any("nicht geprüft" in text for text in version_warnings)
    stage_one = ((bool(reasons), "merge-check nicht grün"), (ci_due and not ci_passed(checks), "keine erfolgreichen Checks zum Head gemeldet"),
                 (not listed, "core_paths fehlt in der Basis"), (config_changed, ".ce-repo.json im PR geändert"), (bool(touched), "Kernpfad berührt"),
                 (lines > NO_JA_LINES, f"{lines} Zeilen, ohne Ja höchstens {NO_JA_LINES}"),
                 (pr.get("baseRefName") != pr.get("defaultBranch"), "Basis ist nicht der Standardbranch"),
                 (not pr.get("sameRepo"), "PR aus einem anderen Repo"), (facts.get("local_head") != head, "Head ist nicht der Stand dieses Ordners"),
                 (bool(facts.get("cloud")), "Cloud-Sitzung: Merge-Bitte an Sightline"), (unchecked, "Teile nicht geprüft"))
    why = [text for failed, text in stage_one if failed]
    return {"repo": facts["repo"], "pr": pr["number"], "url": pr.get("url"), "head": head, "base": pr["baseRefName"],
            "green": not reasons, "classification": classification, "lines": lines, "code_lines": code_lines, "files": len(files),
            "ohne_ja": not why, "ja_noetig_weil": why, "core_paths_touched": touched, "reasons": reasons, "warnings": warnings}


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
    """Drops the old receipt in every case; a new one needs green and either the user's words (kind `ja`) or stage 1 (kind
    `patch`). The kind written, "" for none."""
    path = receipt_path(cwd)
    if path is None:
        return ""
    path.unlink(missing_ok=True)
    if not (result and result["green"]):
        return ""
    kind = "ja" if ja.strip() else "patch" if result.get("ohne_ja") is True else ""
    if not kind:
        return ""
    receipt = {"repo": result["repo"], "pr": result["pr"], "head": result["head"], "green": True, "kind": kind, "time": now()}
    if kind == "ja":
        receipt["ja"] = ja.strip()
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(receipt, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return kind


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
    kind = update_receipt(result, args.ja)
    result["receipt"], result["receipt_kind"] = bool(kind), kind
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    print(text_line(result), file=sys.stderr)
    if kind:
        stage = "Stufe 1, ohne Ja" if kind == "patch" else "Stufe 2, nach Ja"
        print(f"Merge-Quittung ({stage}), 30 Minuten aus diesem Ordner: gh pr merge {result['pr']} -R {result['repo']} "
              f"--match-head-commit {result['head']} --squash", file=sys.stderr)
    elif result["green"]:
        print(f"Keine Merge-Quittung ({'; '.join(result['ja_noetig_weil'])}): nach dem Ja des Nutzers erneut mit --ja \"<sein Wortlaut>\".",
              file=sys.stderr)
    return 0 if result["green"] else 1


if __name__ == "__main__":
    sys.exit(main())
