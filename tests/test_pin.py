"""./ce pin (0.42 Ratchet): a real git repository as the remote, real copies and pushes; only GitHub's REST answers and the two tools are fake."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "ce-shared"
sys.path[:0] = [str(PLUGIN / "tools")]
import ce_merge_check as mc  # noqa: E402
import ce_pin as cp  # noqa: E402
import ce_sync  # noqa: E402

OLD, NEW = "a" * 40, "b" * 40
TARGET = ("0.42.0", NEW)
LOADER = "#!/usr/bin/env python3\nprint('loader')\n"
DATA = json.loads(cp.DATA.read_text(encoding="utf-8"))
TODAY = date(2026, 11, 7)
SPEC = {"file": "CHANGELOG.md", "pattern": r"(?m)^## (\d+\.\d+(?:\.\d+)?)"}


def config(**fields):
    return {"name": "cubeescape-x", "version": SPEC, "tools_version": {"version": "0.41.0", "commit": OLD}, "core_paths": ["src/core.py"], **fields}


def dumps(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def plan(value, files, key="x", workflows=()):
    text = dumps(value)
    with mock.patch.object(cp, "read_loader", return_value=LOADER):
        return cp.plan_repo(key, value, text, files.get, list(workflows), TARGET, DATA, TODAY)


class WordTests(unittest.TestCase):
    def test_pin_number(self):
        self.assertEqual([cp.pin_number(v) for v in ("0.42.0", "0.41.1", "1.2.0", "1.0.0")], ["42", "41-1", "1-2", "1"])

    def test_bump(self):
        self.assertEqual([cp.bump(v) for v in ("0.115.9", "0.45", "1.2.3")], ["0.115.10", "0.45.1", "1.2.4"])

    def test_tool_is_wired_up(self):
        self.assertIn("ce_pin.py", ce_sync.SYNC_FILES)
        entry = (PLUGIN / "tools" / "ce").read_text(encoding="utf-8")
        self.assertIn('pin)      exec "$py" "$tools/ce_pin.py" "$@";;', entry)
        self.assertIn("pin <version>", "".join(entry.split("\n")[1:12]))  # the help prints these lines

    def test_data_uses_known_words(self):
        words = dict.fromkeys(("version", "old_version", "number", "tools_version", "old_tools", "commit7", "date"), "1")
        for rules in (DATA["defaults"], *DATA["repos"].values()):
            for text in [rules.get("heading", ""), rules.get("body", "")] + [item[part] for item in rules.get("also", []) for part in ("after", "line")]:
                text.format(**words)


class PlanTests(unittest.TestCase):
    def test_version_in_the_changelog_is_raised_by_the_entry(self):
        result = plan(config(), {"CHANGELOG.md": "# C\n\n## 0.45 Lantern\n", "ce": "old\n", ".ce-repo.json": ""})
        self.assertEqual(result["version"], ("0.45", "0.45.1"))
        self.assertEqual(result["changes"]["CHANGELOG.md"], "# C\n\n## 0.45.1 Patch pin42\n\n- Pin auf Agent Tools 0.42.0 (`bbbbbbb`), Lader `ce` folgt; sonst keine Änderung.\n\n## 0.45 Lantern\n")
        self.assertEqual(json.loads(result["changes"][".ce-repo.json"])["tools_version"], {"version": "0.42.0", "commit": NEW})
        self.assertIn("GENERATED from ce-agent-tools 0.42.0", result["changes"]["ce"])

    def test_config_keeps_its_hand_formatting(self):
        value = config()
        text = dumps(value).replace('"core_paths": [\n    "src/core.py"\n  ]', '"core_paths": ["src/core.py"]')
        with mock.patch.object(cp, "read_loader", return_value=LOADER):
            result = cp.plan_repo("x", value, text, {"CHANGELOG.md": "## 1.0\n", "ce": "x\n"}.get, [], TARGET, DATA, TODAY)
        self.assertIn('"core_paths": ["src/core.py"]', result["changes"][".ce-repo.json"])

    def test_version_file_apart_and_extra_line_of_cube_escape(self):
        value = config(version={"file": "Game.cs", "pattern": r'Number\s*=\s*"([^"]+)"'})
        files = {"Game.cs": 'Number = "0.115.9";\r\n', "CHANGELOG.md": "# Ä\n\n## 0.115.9 (Patch)\n", "ce": "x\n",
                 "Docs/ROADMAP.md": "| **0.115.8** | a |\n| **0.115.9** | b |\n| **0.115.7** | c |\n"}
        result = plan(value, files, "ce")
        self.assertEqual(result["changes"]["Game.cs"], 'Number = "0.115.10";\r\n')  # CRLF stays
        self.assertTrue(result["changes"]["CHANGELOG.md"].startswith("# Ä\n\n## 0.115.10 (Patch)\n\nPin-Runde:"))
        self.assertEqual(result["changes"]["Docs/ROADMAP.md"].split("\n")[2].split(" | ")[:3], ["| **0.115.10**", "**Pin42**", "*Patch, 07.11.2026.* Pin-Runde: `tools_version` 0.42.0, Lader folgt. Kein Code, kein Formatwechsel. |"])

    def test_missing_anchor_stops(self):
        value = config(version={"file": "Game.cs", "pattern": 'N="([^"]+)"'})
        with self.assertRaisesRegex(cp.Stop, "nicht gefunden"):
            plan(value, {"Game.cs": 'N="0.1.0"\n', "CHANGELOG.md": "## 0.1.0\n", "ce": "x\n", "Docs/ROADMAP.md": "| **0.0.9** | a |\n"}, "ce")

    def test_plain_text_version_stops(self):
        with self.assertRaisesRegex(cp.Stop, "kein \\{file, pattern\\}"):
            plan(config(version="0.38.0"), {"CHANGELOG.md": "## 0.38.5\n", "ce": "x\n"})

    def test_heading_the_pattern_cannot_read_stops(self):
        rules = {"defaults": {**DATA["defaults"], "heading": "## Patch pin{number}"}, "repos": {}}
        with mock.patch.object(cp, "read_loader", return_value=LOADER), self.assertRaisesRegex(cp.Stop, "liest nach der Änderung"):
            cp.plan_repo("x", config(), dumps(config()), {"CHANGELOG.md": "## 0.1.0\n", "ce": "x\n"}.get, [], TARGET, rules, TODAY)

    def test_workflow_moves_only_when_it_followed_the_pin(self):
        line = "uses: ffunkfabian-hash/ce-agent-tools/.github/actions/ce-tools@{}\n"
        other = "c" * 40
        files = {"CHANGELOG.md": "## 0.1.0\n", "ce": "x\n", ".github/workflows/a.yml": line.format(OLD), ".github/workflows/b.yml": line.format(other)}
        result = plan(config(), files, workflows=[".github/workflows/a.yml", ".github/workflows/b.yml"])
        self.assertEqual(result["changes"][".github/workflows/a.yml"], line.format(NEW))
        self.assertNotIn(".github/workflows/b.yml", result["changes"])
        self.assertEqual(result["notes"], [".github/workflows/b.yml bleibt auf ccccccc"])

    def test_forecast(self):
        result = {"changes": {".ce-repo.json": "", "ce": "", "CHANGELOG.md": ""}}
        self.assertEqual(cp.forecast(config(), result, 10), "Stufe 1 möglich")
        self.assertEqual(cp.forecast(config(core_paths=[]), result, 10), "Stufe 2: core_paths leer")
        self.assertEqual(cp.forecast(config(core_paths=["ce", ".ce-repo.json"]), result, 10), "Stufe 2: Kernpfad ce")
        self.assertEqual(cp.forecast(config(), result, 101), "Stufe 2: 101 Zeilen")


def git(folder, *args):
    return subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True, text=True).stdout.strip()


@unittest.skipIf(os.name == "nt", "der Lauf braucht git mit file:// und das Modus-Bit")
class RunCase(unittest.TestCase):
    """Two repositories on one remote folder each; gh and the two tools answer from here."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.entries = [{"id": "nx", "name": "Nexogen", "github": "o/nx"}, {"id": "gone", "name": "Alt", "github": "o/gone", "status": "stillgelegt"},
                        {"id": "held", "name": "Halt", "github": "o/held", "hold": "Prüfung"}, {"id": "a", "name": "Eins", "github": "o/a"},
                        {"id": "b", "name": "Zwei", "github": "o/b", "status": "Öffentlich auf GitHub, Standardbranch main"}]
        self.heads, self.pulls, self.branches, self.calls, self.tools, self.workflows = {}, {}, set(), [], [], {"o/a": ["check.yml"], "o/b": []}
        self.make("o/a", "cubeescape-a")
        self.make("o/b", "cubeescape-b")
        self.configs = {"o/nx": {"name": "nx"}}
        self.check = {"green": True, "ohne_ja": True, "ja_noetig_weil": [], "reasons": []}
        self.verify = (0, {"green": True})
        self.pr_fails = False
        for patcher in (mock.patch.dict(os.environ, {"CE_PIN_CACHE": str(self.folder / "cache"), "CE_VERIFY_CACHE": str(self.folder / "verify")}),
                        mock.patch.object(cp, "REMOTE", str(self.folder / "remote-{repo}")), mock.patch.object(cp, "registry", lambda: self.entries),
                        mock.patch.object(cp, "target_commit", return_value=NEW), mock.patch.object(cp, "read_raw", self.read_raw), mock.patch.object(mc, "gh", self.gh),
                        mock.patch.object(cp, "run_tool", self.run_tool), mock.patch.object(cp, "wait_for_ci", self.wait), mock.patch.object(mc, "on_cloud", return_value=False),
                        mock.patch.object(cp.ce_verify, "on_mac", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def make(self, repo, name):
        remote = self.folder / f"remote-{repo}"
        remote.mkdir(parents=True)
        git(remote, "init", "-q", "-b", "main")
        git(remote, "config", "uploadpack.allowAnySHA1InWant", "true")
        git(remote, "config", "uploadpack.allowFilter", "true")
        (remote / ".ce-repo.json").write_text(dumps(config(name=name)), encoding="utf-8")
        (remote / "CHANGELOG.md").write_text("# C\n\n## 0.1.0\n", encoding="utf-8")
        (remote / "ce").write_text("old\n", encoding="utf-8")
        (remote / "ce").chmod(0o755)
        (remote / "big.bin").write_text("nie in der Kopie\n", encoding="utf-8")
        (remote / ".github" / "workflows").mkdir(parents=True)
        (remote / ".github" / "workflows" / "check.yml").write_text("on: push\n", encoding="utf-8")
        git(remote, "add", ".")
        git(remote, "commit", "-q", "-m", "start")
        self.heads[repo] = git(remote, "rev-parse", "HEAD")

    def read_raw(self, repo, path, ref):
        if repo == mc.TOOLS_REPO:
            return LOADER
        if repo in getattr(self, "configs", {}) and path == ".ce-repo.json":
            return dumps(self.configs[repo])
        target = self.folder / f"remote-{repo}"
        done = subprocess.run(["git", "-C", str(target), "show", f"{ref}:{path}"], capture_output=True, text=True)
        return done.stdout if done.returncode == 0 else None

    def gh(self, *args):
        self.calls.append(args)
        target = args[-1] if args[0] == "api" else ""
        if args[:2] == ("pr", "create"):
            repo = args[args.index("-R") + 1]
            if self.pr_fails:
                raise mc.NotCheckable("HTTP 502")
            self.pulls[repo] = {"number": 7, "head": {"sha": "?"}}
            return f"https://github.com/{repo}/pull/7\n"
        if args[0] != "api":
            raise AssertionError(args)
        repo = "/".join(target.split("/")[1:3]).split("?")[0]
        if "/pulls?" in target:
            pull = self.pulls.get(repo)
            return json.dumps([pull] if pull else [])
        if "/branches/" in target:
            return "{}" if target.rsplit("/", 1)[1].replace("%2F", "/") in self.branches else None
        if "/commits/claude" in target:
            remote, branch = self.folder / f"remote-{repo}", target.rsplit("/", 1)[1].replace("%2F", "/")
            return json.dumps({"sha": git(remote, "rev-parse", branch), "parents": [{"sha": git(remote, "rev-parse", f"{branch}~1")}],
                               "commit": {"message": git(remote, "log", "-1", "--format=%B", branch)}})
        if "/commits/" in target:
            return json.dumps({"sha": self.heads.get(repo, "f" * 40)})
        if "/contents/.github/workflows" in target:
            names = self.workflows.get(repo, [])
            return json.dumps([{"name": name} for name in names]) if names else None
        return json.dumps({"default_branch": "main"})

    def run_tool(self, name, args, cwd, seconds):
        head = git(cwd, "rev-parse", "HEAD")
        self.tools.append((name, args, head))
        if name == "ce_verify.py":
            return self.verify
        return (0 if self.check.get("green") else 1), self.check

    def wait(self, repo, head, seconds):
        self.tools.append(("wait", repo, head))

    def run_pin(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cp.main(["0.42.0", *argv])
        return code, out.getvalue(), err.getvalue()

    def branch_files(self, repo, branch="claude/a-patch-pin42"):
        remote = self.folder / f"remote-{repo}"
        return git(remote, "diff", "--name-only", "main", branch).split()


class DryRunTests(RunCase):
    def test_writes_nothing_and_shows_all(self):
        code, out, _ = self.run_pin("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("-old", out)
        self.assertIn("+## 0.1.1 Patch pin42", out)
        self.assertNotIn(("pr", "create"), [call[:2] for call in self.calls])
        self.assertEqual(self.tools, [])
        self.assertFalse((self.folder / "cache").exists())
        for repo in ("o/a", "o/b"):
            self.assertEqual(git(self.folder / f"remote-{repo}", "branch", "--format=%(refname:short)"), "main")

    def test_rows_of_active_and_left_out_repositories(self):
        _, out, _ = self.run_pin("--dry-run")
        rows = {line.split(" | ")[0][2:]: line for line in out.splitlines() if line.startswith("| ")}
        self.assertIn("ausgelassen: kein Pin", rows["Nexogen"])
        self.assertIn("ausgelassen: stillgelegt", rows["Alt"])
        self.assertIn("ausgelassen: gesperrt: Prüfung", rows["Halt"])
        self.assertIn("claude/a-patch-pin42 | 13 | GitHub-CI | Stufe 1 möglich", rows["Eins"])
        self.assertIn("./ce verify", rows["Zwei"])  # free text in status is no shutdown

    def test_only_and_unknown_ids(self):
        _, out, _ = self.run_pin("--dry-run", "--only", "a")
        self.assertNotIn("Zwei", out)
        self.assertEqual(self.run_pin("--dry-run", "--skip", "nix")[0], 2)

    def test_version_not_on_main_is_exit_2(self):
        with mock.patch.object(cp, "target_commit", return_value=None):
            self.assertEqual(self.run_pin("--dry-run")[0], 2)


class RealRunTests(RunCase):
    def test_pushes_only_the_pin_and_keeps_the_mode(self):
        code, out, _ = self.run_pin()
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(self.branch_files("o/a")), [".ce-repo.json", "CHANGELOG.md", "ce"])
        remote = self.folder / "remote-o/a"
        self.assertTrue(git(remote, "ls-tree", "claude/a-patch-pin42", "ce").startswith("100755"))
        self.assertIn("big.bin", git(remote, "ls-tree", "--name-only", "claude/a-patch-pin42"))  # the sparse copy drops nothing
        self.assertEqual(git(remote, "log", "-1", "--format=%s", "claude/a-patch-pin42"), "Patch pin42 0.1.1: Pin auf Agent Tools 0.42.0")

    def test_merge_check_runs_in_a_copy_on_the_head_and_copies_go(self):
        self.run_pin()
        head = git(self.folder / "remote-o/a", "rev-parse", "claude/a-patch-pin42")
        self.assertIn(("ce_merge_check.py", ["o/a", "7", "--sha", head], head), self.tools)
        self.assertEqual(list((self.folder / "cache" / "runs").iterdir()), [])

    def test_repository_without_ci_is_verified_before_merge_check(self):
        self.run_pin()
        names = [item[0] for item in self.tools if item[1:2] and item[1][0:1] == ["o/b"]]
        self.assertEqual(names, ["ce_verify.py", "ce_merge_check.py"])
        self.assertEqual([item[1] for item in self.tools if item[0] == "wait"], ["o/a"])

    def test_stage_one_gives_the_line_and_never_merges(self):
        _, out, _ = self.run_pin()
        head = git(self.folder / "remote-o/a", "rev-parse", "claude/a-patch-pin42")
        self.assertIn(f"gh pr merge 7 -R o/a --match-head-commit {head} --squash", out)
        self.assertIn("nichts ist gemergt", out)
        self.assertFalse([call for call in self.calls if call[:2] == ("pr", "merge") or "/merge" in call[-1]])
        self.assertNotIn('"merge"', (PLUGIN / "tools" / "ce_pin.py").read_text(encoding="utf-8").replace('row["merge"]', "").replace('"merge": ""', ""))

    def test_stage_two_and_red_give_no_line(self):
        self.check = {"green": True, "ohne_ja": False, "ja_noetig_weil": ["core_paths fehlt in der Basis"], "reasons": []}
        code, out, _ = self.run_pin("--only", "a")
        self.assertEqual(code, 0)
        self.assertIn("Stufe 2: core_paths fehlt in der Basis", out)
        self.assertNotIn("gh pr merge", out)

    def test_red_merge_check_is_exit_1(self):
        self.check = {"green": False, "ohne_ja": False, "reasons": ["CI rot: check"]}
        code, out, _ = self.run_pin("--only", "a")
        self.assertEqual(code, 1)
        self.assertIn("merge-check rot: CI rot: check", out)

    def test_one_stop_leaves_the_rest_running(self):
        (self.folder / "remote-o/a" / "CHANGELOG.md").write_text("keine Einträge\n", encoding="utf-8")
        git(self.folder / "remote-o/a", "commit", "-q", "-am", "leer")
        self.heads["o/a"] = git(self.folder / "remote-o/a", "rev-parse", "HEAD")
        code, out, _ = self.run_pin()
        self.assertEqual(code, 1)
        self.assertIn("gestoppt: Version nicht lesbar", out)
        self.assertIn("merge-check grün, Stufe 1", out)

    def test_red_verify_stops_only_its_repository(self):
        self.verify = (1, {"green": False, "error": "3 Tests rot"})
        code, out, _ = self.run_pin()
        self.assertEqual(code, 1)
        self.assertIn("gestoppt: ./ce verify: 3 Tests rot", out)
        self.assertFalse([item for item in self.tools if item[0] == "ce_merge_check.py" and item[1][0] == "o/b"])
        self.assertTrue([item for item in self.tools if item[0] == "ce_merge_check.py" and item[1][0] == "o/a"])

    def foreign_branch(self):
        remote = self.folder / "remote-o/a"
        git(remote, "branch", "claude/a-patch-pin42")
        git(remote, "checkout", "-q", "claude/a-patch-pin42")
        (remote / "fremd.txt").write_text("x\n", encoding="utf-8")
        git(remote, "add", ".")
        git(remote, "commit", "-q", "-m", "fremd")
        git(remote, "checkout", "-q", "main")
        self.branches.add("claude/a-patch-pin42")
        return git(remote, "rev-parse", "claude/a-patch-pin42")

    def test_foreign_branch_without_pull_request_stops_and_stays(self):
        head = self.foreign_branch()
        code, out, _ = self.run_pin("--only", "a")
        self.assertEqual(code, 1)
        self.assertIn("trägt nicht den Commit dieses Laufs", out)
        self.assertEqual(git(self.folder / "remote-o/a", "rev-parse", "claude/a-patch-pin42"), head)
        self.assertEqual(self.pulls, {})

    def test_failed_pull_request_names_the_push_and_a_second_run_finishes(self):
        self.pr_fails = True
        code, out, _ = self.run_pin("--only", "a")
        self.assertEqual(code, 1)
        self.assertIn("gepusht, PR nicht angelegt, ein zweiter Lauf holt das nach: HTTP 502", out)
        self.assertEqual(list((self.folder / "cache" / "runs").iterdir()), [])
        head = git(self.folder / "remote-o/a", "rev-parse", "claude/a-patch-pin42")
        self.pr_fails = False
        self.branches.add("claude/a-patch-pin42")
        code, out, _ = self.run_pin("--only", "a")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.tools[-1], ("ce_merge_check.py", ["o/a", "7", "--sha", head], head))
        self.assertEqual(git(self.folder / "remote-o/a", "rev-parse", "claude/a-patch-pin42"), head)  # nothing pushed again
        self.assertEqual(list((self.folder / "cache" / "runs").iterdir()), [])

    def test_failed_push_stops_and_leaves_no_copy(self):
        hook = self.folder / "remote-o/a" / ".git" / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        code, out, _ = self.run_pin("--only", "a")
        self.assertEqual(code, 1)
        self.assertIn("gestoppt: Push nach claude/a-patch-pin42", out)
        self.assertEqual(self.pulls, {})
        self.assertEqual(list((self.folder / "cache" / "runs").iterdir()), [])

    def test_merge_check_not_checkable_stops_the_row(self):
        with mock.patch.object(self, "check", {"green": False, "error": "gh weg"}), mock.patch.object(cp, "run_tool", lambda *args: (2, self.check)):
            code, out, _ = self.run_pin("--only", "a")
        self.assertEqual(code, 1)
        self.assertIn("gestoppt: merge-check nicht prüfbar: gh weg", out)

    def test_a_fitting_verify_receipt_saves_the_second_run(self):
        self.pulls["o/b"] = {"number": 9, "head": {"sha": self.heads["o/b"]}}
        receipt = {"green": True, "repo": "o/b", "head": self.heads["o/b"], "check": None}
        with mock.patch.object(mc, "read_verify", return_value=receipt):
            self.run_pin("--only", "b")
        self.assertEqual([item[0] for item in self.tools], ["ce_merge_check.py"])

    def test_note_of_a_workflow_stays_in_a_real_row(self):
        line = "uses: ffunkfabian-hash/ce-agent-tools/.github/actions/ce-tools@" + "c" * 40 + "\n"
        remote = self.folder / "remote-o/a"
        (remote / ".github" / "workflows" / "check.yml").write_text(line, encoding="utf-8")
        git(remote, "commit", "-q", "-am", "wf")
        self.heads["o/a"] = git(remote, "rev-parse", "HEAD")
        _, out, _ = self.run_pin("--only", "a")
        self.assertIn("merge-check grün, Stufe 1; .github/workflows/check.yml bleibt auf ccccccc", out)

    def test_second_run_continues_at_the_open_pull_request(self):
        self.pulls["o/a"] = {"number": 9, "head": {"sha": self.heads["o/a"]}}
        code, out, _ = self.run_pin("--only", "a")
        self.assertEqual(code, 0)
        self.assertIn("PR 9", out)
        self.assertEqual(self.tools[-1], ("ce_merge_check.py", ["o/a", "9", "--sha", self.heads["o/a"]], self.heads["o/a"]))
        self.assertNotIn(("pr", "create"), [call[:2] for call in self.calls])

    def test_current_or_newer_pin_is_left_alone(self):
        self.configs["o/a"] = config(tools_version={"version": "0.42.0", "commit": NEW})
        self.configs["o/b"] = config(tools_version={"version": "0.43.0", "commit": OLD})
        code, out, _ = self.run_pin()
        self.assertEqual(code, 0)
        self.assertIn("ausgelassen: schon auf diesem Stand", out)
        self.assertIn("ausgelassen: steht auf 0.43.0, neuer als das Ziel", out)

    def test_cloud_and_other_systems_refuse_a_real_run(self):
        with mock.patch.object(mc, "on_cloud", return_value=True):
            self.assertEqual(self.run_pin()[0], 2)
            self.assertEqual(self.run_pin("--dry-run")[0], 0)
        with mock.patch.object(cp.ce_verify, "on_mac", return_value=False):
            self.assertEqual(self.run_pin()[0], 2)

    def test_unknown_data_schema_is_exit_2(self):
        with mock.patch.object(cp, "DATA_SCHEMA", 2):
            self.assertEqual(self.run_pin("--dry-run")[0], 2)

    def test_sweep_keeps_young_copies(self):
        runs = self.folder / "cache" / "runs"
        (runs / "alt").mkdir(parents=True)
        (runs / "jung").mkdir()
        os.utime(runs / "alt", (1, 1))
        cp.sweep()
        self.assertEqual([item.name for item in runs.iterdir()], ["jung"])


class ToolCallTests(unittest.TestCase):
    """run_tool itself, against a fake tool next to a fake HERE."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        patcher = mock.patch.object(cp, "HERE", self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tool(self, text):
        (self.folder / "tool.py").write_text(text, encoding="utf-8")

    def test_answer_and_exit_code(self):
        self.tool("import json,sys\nprint(json.dumps({'a': sys.argv[1]}))\nsys.exit(1)\n")
        self.assertEqual(cp.run_tool("tool.py", ["x"], self.folder, 30), (1, {"a": "x"}))

    def test_no_json_stops(self):
        self.tool("print('kaputt')\n")
        with self.assertRaisesRegex(cp.Stop, "keine JSON-Antwort"):
            cp.run_tool("tool.py", [], self.folder, 30)

    def test_time_limit_stops(self):
        self.tool("import time\ntime.sleep(30)\n")
        with self.assertRaisesRegex(cp.Stop, "tool.py"):
            cp.run_tool("tool.py", [], self.folder, 0.2)


class ReadingTests(unittest.TestCase):
    def test_target_commit_walks_down_and_stops(self):
        versions = {"c1": "0.43.0", "c2": "0.42.0", "c3": "0.41.1"}
        with mock.patch.object(mc, "gh_json", return_value=[{"sha": sha} for sha in versions]), \
                mock.patch.object(cp, "read_raw", lambda repo, path, ref: json.dumps({"version": versions[ref]})):
            self.assertEqual(cp.target_commit("0.42.0"), "c2")
            self.assertIsNone(cp.target_commit("0.42.5"))

    def test_raw_read_keeps_crlf(self):
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"a\r\nb\r\n", b"")):
            self.assertEqual(cp.read_raw("o/r", "x", "main"), "a\r\nb\r\n")

    def test_raw_read_of_a_missing_file_is_none(self):
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"gh: Not Found (HTTP 404)")):
            self.assertIsNone(cp.read_raw("o/r", "x", "main"))

    def test_wait_ends_when_checks_are_done_or_time_is_up(self):
        answers = iter([[], [{"name": "c", "status": "IN_PROGRESS", "conclusion": ""}], [{"name": "c", "status": "COMPLETED", "conclusion": "SUCCESS"}]])
        sleeps = []
        with mock.patch.object(mc, "read_checks", lambda repo, head: next(answers)), mock.patch.object(mc, "gh_json", return_value={"workflow_runs": []}):
            cp.wait_for_ci("o/r", "h", 60, sleeps.append)
        self.assertEqual(len(sleeps), 2)
        with mock.patch.object(mc, "read_checks", return_value=[]), mock.patch.object(mc, "gh_json", return_value={"workflow_runs": []}):
            cp.wait_for_ci("o/r", "h", 0, sleeps.append)
        self.assertEqual(len(sleeps), 2)


if __name__ == "__main__":
    unittest.main()
