"""./ce merge-check (0.34 Gatekeeper): green, red with reasons and the classification from facts; the GitHub reading against a
fake gh; the receipt the guard reads."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "ce-shared"
sys.path[:0] = [str(PLUGIN / "tools"), str(PLUGIN / "hooks"), str(PLUGIN / "githooks")]
import ce_githooks  # noqa: E402
import ce_merge_check as mc  # noqa: E402
import guard  # noqa: E402

HEAD = "c" * 40
CONFIG = {"version": {"file": "v.json", "pattern": "x"}, "docs": {"changelog": "CHANGELOG.md"}, "core_paths": ["contracts/**", "hooks/guard.py"]}
CHANGELOG = {"filename": "CHANGELOG.md", "status": "modified", "changes": 3, "patch": "@@ -1 +1,3 @@\n+## 1.3.0"}


def facts(**change):
    pr = {"number": 7, "url": "u", "title": "t", "state": "OPEN", "isDraft": False, "headRefOid": HEAD, "headRefName": "claude/x",
          "baseRefName": "main", "mergeable": "MERGEABLE", "additions": 30, "deletions": 5, "statusCheckRollup": []}
    pr.update(change.pop("pr", {}))
    base = {"repo": "o/r", "pr": pr, "files": [CHANGELOG, {"filename": "src/a.py", "status": "modified", "changes": 2, "patch": "@@\n+x\n-y"}],
            "sizes": {"src/a.py": 900}, "behind_by": 0, "base_config": CONFIG, "head_config": CONFIG,
            "versions": {"base": "1.2.0", "head": "1.3.0"}, "changelog_name": "CHANGELOG.md", "changelog": "## 1.3.0\n"}
    base.update(change)
    return base


def test_file(name, patch, status="modified"):
    return {"filename": name, "status": status, "changes": patch.count("\n"), "patch": patch}


class EvaluateTests(unittest.TestCase):
    def reasons(self, **change):
        return " | ".join(mc.evaluate(facts(**change))["reasons"])

    def test_a_clean_small_pull_request_is_a_green_patch(self):
        result = mc.evaluate(facts())
        self.assertTrue(result["green"], result)
        self.assertEqual((result["classification"], result["lines"]), ("Patch", 35))
        self.assertEqual(result["warnings"], ["Keine Checks zum Head gemeldet: direkt nach einem Push kann der CI-Lauf noch fehlen"])
        self.assertEqual(mc.text_line(result), f"merge-check o/r#7 {HEAD[:7]}: grün · Einstufung Patch (35 Zeilen)")

    def test_the_text_line_of_a_red_result_names_the_reasons(self):
        line = mc.text_line(mc.evaluate(facts(pr={"mergeable": "CONFLICTING"}, behind_by=2)))
        self.assertIn("rot · Einstufung Patch (35 Zeilen) · main nicht enthalten (2 Commits zurück); Konflikt mit der Basis", line)

    def test_state_head_base_and_conflicts(self):
        self.assertIn("nicht offen", self.reasons(pr={"state": "MERGED"}))
        self.assertIn("Entwurf", self.reasons(pr={"isDraft": True}))
        self.assertIn("nicht lesbar", self.reasons(behind_by=None))
        self.assertIn("Mergebarkeit unbekannt", self.reasons(pr={"mergeable": "UNKNOWN"}))
        self.assertIn("erwartet", " ".join(mc.evaluate(facts(), "d" * 40)["reasons"]))
        self.assertTrue(mc.evaluate(facts(), HEAD[:10])["green"])

    def test_ci_reads_check_runs_and_commit_statuses(self):
        good = [{"name": "a", "status": "COMPLETED", "conclusion": "SUCCESS"}, {"name": "b", "status": "COMPLETED", "conclusion": "SKIPPED"},
                {"context": "c", "state": "SUCCESS"}]
        self.assertEqual(mc.ci_state(good), ([], []))
        self.assertEqual(mc.ci_state([]), ([], []))
        bad = [{"name": "a", "status": "COMPLETED", "conclusion": "FAILURE"}, {"context": "b", "state": "ERROR"},
               {"name": "c", "status": "IN_PROGRESS", "conclusion": ""}, {"context": "d", "state": "PENDING"}]
        self.assertEqual(mc.ci_state(bad), (["a", "b"], ["c", "d"]))
        self.assertIn("CI rot: a, b", self.reasons(pr={"statusCheckRollup": bad}))
        self.assertIn("CI läuft noch: c, d", self.reasons(pr={"statusCheckRollup": bad}))

    def test_a_queued_run_without_a_check_entry_is_not_green(self):
        self.assertIn("CI läuft noch: check", self.reasons(runs_open=["check"]))
        done = [{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}]
        self.assertEqual(mc.evaluate(facts(pr={"statusCheckRollup": done}))["warnings"], [])
        self.assertIn("CI läuft noch: tests", self.reasons(pr={"statusCheckRollup": [{"name": "tests", "status": "QUEUED"}]}, runs_open=["tests"]))
        self.assertEqual(self.reasons(pr={"statusCheckRollup": [{"name": "tests", "status": "QUEUED"}]}, runs_open=["tests"]).count("tests"), 1)

    def test_files_over_the_limit_are_red_and_the_limit_is_exact(self):
        self.assertIn("big.bin (5.0 MB)", self.reasons(sizes={"big.bin": mc.BIG_FILE + 1}))
        self.assertEqual(self.reasons(sizes={"big.bin": mc.BIG_FILE}), "")

    def test_both_limits_are_the_same(self):
        self.assertEqual(mc.BIG_FILE, ce_githooks.BIG_BINARY)
        self.assertEqual(mc.RECEIPT_FILE, guard.RECEIPT_FILE)

    def test_report_md_only_when_it_arrives_at_the_root(self):
        files = facts()["files"]
        self.assertIn("report.md im Diff", self.reasons(files=files + [{"filename": "report.md", "status": "added"}]))
        self.assertEqual(self.reasons(files=files + [{"filename": "report.md", "status": "removed"}]), "")
        self.assertEqual(self.reasons(files=files + [{"filename": "docs/report.md", "status": "added"}]), "")

    def test_weakened_tests_are_red(self):
        files = facts()["files"]
        cases = [test_file("tests/test_a.py", "@@\n-    def test_x(self):\n-        self.assertTrue(y)"),
                 test_file("tests/test_a.py", "@@\n-        self.assertEqual(a, 1)"),
                 test_file("Assets/Tests/ATests.cs", "@@\n-    [Test]\n-    public void X() { Assert.That(y); }"),
                 test_file("web/a.test.js", "@@\n-  it('x', () => { expect(y).toBe(1); });"),
                 test_file("tests/test_gone.py", "@@\n-def test_x():\n-    assert y", status="removed")]
        for case in cases:
            self.assertIn("Tests abgeschwächt", self.reasons(files=files + [case]), case)
        self.assertIn("gelöscht: tests/test_gone.py", self.reasons(files=files + [cases[-1]]))

    def test_a_rename_that_takes_a_test_out_of_the_run_is_red(self):
        def moved(old, new):
            return facts()["files"] + [{"filename": new, "previous_filename": old, "status": "renamed", "changes": 0}]
        for old, new in (("tests/test_a.py", "attic/a.py"), ("tests/test_a.py", "tests/a_off.py"), ("web/a.test.js", "web/a.js")):
            self.assertIn("aus dem Testlauf verschoben", self.reasons(files=moved(old, new)), new)
        for old, new in (("tests/test_a.py", "tests/test_b.py"), ("tests/test_a.py", "tests/sub/test_a.py"), ("src/a.py", "lib/a.py"),
                         ("tests/helper.py", "tests/support.py"), ("tests/fixtures/a.json", "fixtures/a.json")):
            self.assertEqual(self.reasons(files=moved(old, new)), "", new)

    def test_skip_markers_in_added_lines_are_red(self):
        for line in ("    @unittest.skip('later')", "@pytest.mark.xfail", "    [Ignore(\"x\")]", "  it.only('x', f)", "  describe.skip('x', f)", "  xit('x', f)"):
            case = test_file("tests/test_a.py", "@@\n+" + line)
            self.assertIn("Tests übersprungen", self.reasons(files=facts()["files"] + [case]), line)

    def test_no_false_alarm_for_tests_that_move_grow_or_only_mention_markers(self):
        moved = [test_file("tests/test_old.py", "@@\n-def test_x():\n-    assert y", status="removed"),
                 test_file("tests/test_new.py", "@@\n+def test_x():\n+    assert y", status="added")]
        edited = [test_file("tests/test_a.py", "@@\n-        self.assertEqual(a, 1)\n+        self.assertEqual(a, 2)")]
        grown = [test_file("tests/test_a.py", "@@\n+    def test_y(self):\n+        self.assertTrue(z)")]
        data = [test_file("tests/test_a.py", "@@\n+    CASES = ['@unittest.skip', 'it.only(']\n+    @unittest.skipUnless(WINDOWS, 'x')")]
        fixture = [test_file("tests/fixtures/old.json", "@@\n-{}", status="removed")]
        code = [test_file("src/assertions.py", "@@\n-    assert y")]
        for files in (moved, edited, grown, data, fixture, code):
            self.assertEqual(self.reasons(files=facts()["files"] + files), "", files)

    def test_a_deleted_test_github_shows_no_diff_for_is_red_a_changed_one_warns(self):
        gone = {"filename": "tests/test_big.py", "status": "removed", "changes": 9000}
        self.assertIn("Inhalt nicht lesbar", self.reasons(files=facts()["files"] + [gone]))
        changed = {"filename": "tests/test_big.py", "status": "modified", "changes": 9000}
        result = mc.evaluate(facts(files=facts()["files"] + [changed]))
        self.assertTrue(result["green"])
        self.assertIn("nicht bewertet", " ".join(result["warnings"]))

    def test_version_and_changelog(self):
        self.assertIn("Version nicht erhöht (1.2.0 → 1.2.0)", self.reasons(versions={"base": "1.2.0", "head": "1.2.0"}))
        self.assertIn("Version nicht erhöht (1.10.0 → 1.9.0)", self.reasons(versions={"base": "1.10.0", "head": "1.9.0"}))
        self.assertEqual(self.reasons(versions={"base": "1.9.0", "head": "1.10.0"}, changelog="## 1.10.0"), "")
        self.assertIn("Version im PR nicht lesbar", self.reasons(versions={"base": "1.2.0", "head": None}))
        self.assertIn("CHANGELOG.md nicht geändert", self.reasons(files=[facts()["files"][1]]))
        self.assertIn("nennt Version 1.3.0 nicht", self.reasons(changelog="## 1.2.0"))

    def test_a_doc_patch_without_a_new_version_only_warns(self):
        files = [{"filename": "docs/a.md", "status": "modified", "changes": 2, "patch": "@@\n+x"}]
        result = mc.evaluate(facts(files=files, versions={"base": "1.2.0", "head": "1.2.0"}))
        self.assertTrue(result["green"], result)
        self.assertIn("Version nicht erhöht", " ".join(result["warnings"]))
        self.assertIn("Version nicht erhöht", self.reasons(files=files + [{"filename": "a.py", "status": "added"}],
                                                          versions={"base": "1.2.0", "head": "1.2.0"}))

    def test_a_repo_without_the_entries_warns_instead_of_going_red(self):
        for change, text in (({"head_config": None, "base_config": None, "versions": None}, "nicht geprüft"), ({"versions": None}, "Version nicht geprüft"),
                             ({"changelog_name": None}, "CHANGELOG nicht geprüft"), ({"base_config": {}}, "core_paths fehlt"),
                             ({"versions": {"base": None, "head": "1.3.0"}}, "Erhöhung nicht geprüft")):
            result = mc.evaluate(facts(**change))
            self.assertTrue(result["green"], change)
            self.assertIn(text, " ".join(result["warnings"]), change)

    def test_a_config_removed_by_the_pull_request_is_red(self):
        self.assertIn("im PR entfernt", self.reasons(head_config=None))

    def test_classification_by_size_and_core_paths(self):
        self.assertEqual(mc.evaluate(facts(pr={"additions": 100, "deletions": 0}))["classification"], "Patch")
        result = mc.evaluate(facts(pr={"additions": 100, "deletions": 1}))
        self.assertEqual((result["classification"], result["green"]), ("Version", True))
        core = {"filename": "contracts/deep/a.json", "status": "modified", "changes": 1, "patch": "@@\n+x"}
        result = mc.evaluate(facts(files=facts()["files"] + [core]))
        self.assertEqual((result["classification"], result["core_paths_touched"]), ("Version", ["contracts/deep/a.json"]))

    def test_a_version_on_a_patch_number_is_red_for_core_paths_and_a_warning_for_size(self):
        versions = {"base": "1.2.0", "head": "1.2.1"}
        core = {"filename": "hooks/guard.py", "status": "modified", "changes": 1, "patch": "@@\n+x"}
        result = mc.evaluate(facts(files=facts()["files"] + [core], versions=versions, changelog="1.2.1"))
        self.assertIn("nur Patch-Nummer erhöht", " ".join(result["reasons"]))
        result = mc.evaluate(facts(pr={"additions": 300}, versions=versions, changelog="1.2.1"))
        self.assertTrue(result["green"])
        self.assertIn("nur Patch-Nummer erhöht", " ".join(result["warnings"]))

    def test_core_paths_come_from_the_base_so_a_pull_request_cannot_drop_its_own(self):
        core = {"filename": "hooks/guard.py", "status": "modified", "changes": 1, "patch": "@@\n+x"}
        result = mc.evaluate(facts(files=facts()["files"] + [core], head_config={**CONFIG, "core_paths": []}))
        self.assertEqual(result["classification"], "Version")

    def test_a_rename_out_of_a_core_path_counts(self):
        moved = {"filename": "src/guard.py", "previous_filename": "hooks/guard.py", "status": "renamed", "changes": 0}
        self.assertEqual(mc.core_hits([moved], CONFIG["core_paths"]), ["hooks/guard.py"])

    def test_globs(self):
        cases = [("a/**", "a/b/c.py", True), ("a/**", "ab/c.py", False), ("a/*.py", "a/b.py", True), ("a/*.py", "a/b/c.py", False),
                 ("**/Save*.cs", "Assets/Scripts/SaveData.cs", True), ("**/Save*.cs", "SaveData.cs", True), ("a/", "a/b/c", True),
                 ("a/b.py", "a/b.py", True), ("a/b.py", "a/b.pyc", False), ("a.b", "aXb", False), ("Save?.cs", "SaveA.cs", True)]
        for pattern, path, hit in cases:
            self.assertEqual(bool(mc.glob_regex(pattern).match(path)), hit, (pattern, path))


class FakeGh:
    """Answers `gh` by the words of the call; a test changes the dict."""

    def __init__(self, **change):
        pr = dict(facts()["pr"])
        self.answers = {"pr": [json.dumps(pr)], "files": [{"filename": "CHANGELOG.md", "status": "modified", "changes": 1, "patch": "@@\n+x"},
                                                          {"filename": "gone.py", "status": "removed", "changes": 1, "patch": "@@\n-x"}],
                        "config": json.dumps({"version": {"file": "v.json", "pattern": "\"v\": \"(.+?)\""}, "docs": {"changelog": "CHANGELOG.md"}}),
                        "version": {"main": '{"v": "1.2.0"}', HEAD: '{"v": "1.3.0"}'}, "behind": 0}
        self.answers.update(change)
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("pr", "view"):
            return self.answers["pr"].pop(0) if len(self.answers["pr"]) > 1 else self.answers["pr"][0]
        if args[0] == "api" and args[1] == "graphql":
            return json.dumps({"data": {"repository": {"f0": {"byteSize": 77}}}})
        target = args[-1]
        if "/files?" in target:
            return json.dumps(self.answers["files"] if "page=1" in target else [])
        if "/actions/runs" in target:
            return json.dumps({"workflow_runs": self.answers.get("runs", [])})
        if "/compare/" in target:
            return json.dumps({"behind_by": self.answers["behind"]})
        ref = target.rsplit("ref=", 1)[-1]
        if ".ce-repo.json" in target:
            return self.answers["config"]
        if "/v.json" in target:
            return self.answers["version"][ref]
        if "CHANGELOG" in target:
            return "## 1.3.0"
        return None


class GatherTests(unittest.TestCase):
    def test_gather_reads_all_facts_and_only_sizes_files_that_exist(self):
        fake = FakeGh()
        with mock.patch.object(mc, "gh", fake):
            result = mc.evaluate(mc.gather("o/r", 7))
        self.assertTrue(result["green"], result)
        size_call = next(call for call in fake.calls if call[1] == "graphql")
        self.assertIn("CHANGELOG.md", " ".join(size_call))
        self.assertNotIn("gone.py", " ".join(size_call))

    def test_what_the_base_demands_stays_when_the_pull_request_drops_it(self):
        config = json.loads(FakeGh().answers["config"])
        fake = FakeGh()

        def answer(*args):
            if ".ce-repo.json" in args[-1] and args[-1].endswith(HEAD):
                return json.dumps({"name": "ohne Version"})
            return fake(*args)
        with mock.patch.object(mc, "gh", answer):
            found = mc.gather("o/r", 7)
        self.assertEqual(found["versions"], {"base": "1.2.0", "head": "1.3.0"})
        self.assertEqual(found["changelog_name"], config["docs"]["changelog"])
        fake.answers["version"][HEAD] = "{}"
        with mock.patch.object(mc, "gh", answer):
            self.assertIn("Version im PR nicht lesbar", mc.evaluate(mc.gather("o/r", 7))["reasons"])

    def test_a_pull_request_cannot_point_the_version_check_at_another_file(self):
        fake = FakeGh()

        def answer(*args):
            if ".ce-repo.json" in args[-1] and args[-1].endswith(HEAD):
                return json.dumps({"version": {"file": "decoy.json", "pattern": "(.+)"}, "docs": {"changelog": "CHANGELOG.md"}})
            if "decoy.json" in args[-1]:
                return "9.9.9"
            return fake(*args)
        fake.answers["version"][HEAD] = '{"v": "1.2.0"}'
        with mock.patch.object(mc, "gh", answer):
            self.assertIn("Version nicht erhöht (1.2.0 → 1.2.0)", mc.evaluate(mc.gather("o/r", 7))["reasons"])

    def test_a_push_during_the_check_makes_the_run_not_checkable(self):
        pr = dict(facts()["pr"])
        fake = FakeGh(pr=[json.dumps(pr), json.dumps({**pr, "headRefOid": "d" * 40})])
        with mock.patch.object(mc, "gh", fake), self.assertRaisesRegex(mc.NotCheckable, "während der Prüfung"):
            mc.gather("o/r", 7)

    def test_open_workflow_runs_of_the_head_are_read(self):
        fake = FakeGh(runs=[{"name": "check", "status": "queued"}, {"name": "alt", "status": "completed"}])
        with mock.patch.object(mc, "gh", fake):
            self.assertEqual(mc.gather("o/r", 7)["runs_open"], ["check"])

    def test_an_unknown_mergeable_state_is_asked_again(self):
        pr = dict(facts()["pr"])
        fake = FakeGh(pr=[json.dumps({**pr, "mergeable": "UNKNOWN"}), json.dumps(pr)])
        with mock.patch.object(mc, "gh", fake):
            self.assertEqual(mc.read_pr("o/r", 7, sleep=lambda _: None)["mergeable"], "MERGEABLE")

    def test_more_than_one_page_of_files_is_read(self):
        pages = {1: [{"filename": str(i)} for i in range(100)], 2: [{"filename": "last"}]}
        with mock.patch.object(mc, "gh", lambda *a: json.dumps(pages[int(a[-1].rsplit("=", 1)[1])])):
            self.assertEqual(len(mc.changed_files("o/r", 7)), 101)

    def test_many_paths_are_sized_in_chunks(self):
        queries = []

        def answer(*args):
            queries.append(args)
            return json.dumps({"data": {"repository": {f"f{i}": {"byteSize": i} for i in range(100)}}})
        with mock.patch.object(mc, "gh", answer):
            self.assertEqual(len(mc.blob_sizes("o/r", HEAD, [f"p{i}" for i in range(150)])), 150)
        self.assertEqual(len(queries), 2)

    def test_gh_failures_are_not_checkable_and_404_is_none(self):
        failed = subprocess.CompletedProcess([], 1, "", "HTTP 500: boom")
        missing = subprocess.CompletedProcess([], 1, "", "gh: Not Found (HTTP 404)")
        with mock.patch.object(mc.subprocess, "run", return_value=failed):
            with self.assertRaises(mc.NotCheckable):
                mc.gh("api", "x")
        with mock.patch.object(mc.subprocess, "run", return_value=missing):
            self.assertIsNone(mc.gh("api", "x"))
        with mock.patch.object(mc.subprocess, "run", side_effect=OSError("kein gh")):
            with self.assertRaises(mc.NotCheckable):
                mc.gh("api", "x")


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = temp.name
        subprocess.run(["git", "init", "-q", self.folder], check=True)
        self.file = Path(self.folder) / ".git" / mc.RECEIPT_FILE
        self.previous = os.getcwd()
        os.chdir(self.folder)
        self.addCleanup(os.chdir, self.previous)

    def run_main(self, *args, fail=None):
        out, err = io.StringIO(), io.StringIO()
        gather = mock.Mock(side_effect=fail) if fail else mock.Mock(return_value=self.facts)
        with mock.patch.object(mc, "gather", gather), redirect_stdout(out), redirect_stderr(err):
            code = mc.main(["o/r", "7", *args])
        return code, out.getvalue(), err.getvalue()

    facts = facts()

    def test_green_with_ja_writes_the_receipt_the_guard_accepts(self):
        code, out, err = self.run_main("--ja", "Ja, bitte mergen")
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual((result["green"], result["classification"], result["receipt"]), (True, "Patch", True))
        self.assertEqual(err.splitlines()[0], f"merge-check o/r#7 {HEAD[:7]}: grün · Einstufung Patch (35 Zeilen)")
        receipt = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual((receipt["pr"], receipt["head"], receipt["ja"]), (7, HEAD, "Ja, bitte mergen"))
        self.assertTrue(guard.receipt_allows(f"gh pr merge 7 -R o/r --match-head-commit {HEAD} --squash", self.folder))

    def test_green_without_ja_gives_no_receipt_and_says_so(self):
        code, out, err = self.run_main()
        self.assertEqual((code, json.loads(out)["receipt"], self.file.exists()), (0, False, False))
        self.assertIn("--ja", err)

    def test_every_later_run_removes_an_old_receipt(self):
        self.run_main("--ja", "Ja")
        self.assertTrue(self.file.exists())
        self.run_main()
        self.assertFalse(self.file.exists())
        self.run_main("--ja", "Ja")
        code, out, _ = self.run_main("--ja", "Ja", "--sha", "d" * 40)
        self.assertEqual((code, json.loads(out)["green"], self.file.exists()), (1, False, False))
        self.run_main("--ja", "Ja")
        code, out, err = self.run_main("--ja", "Ja", fail=mc.NotCheckable("offline"))
        self.assertEqual((code, json.loads(out)["error"], self.file.exists()), (2, "offline", False))
        self.assertIn("nicht prüfbar · offline", err)

    def test_a_run_that_breaks_off_leaves_no_receipt_also_while_it_runs(self):
        self.run_main("--ja", "Ja")
        seen = []

        def look(*_):
            seen.append(self.file.exists())
            raise ValueError("unerwartet")
        with mock.patch.object(mc, "gather", look), self.assertRaises(ValueError):
            mc.main(["o/r", "7", "--ja", "Ja"])
        self.assertEqual((seen, self.file.exists()), ([False], False))

    def test_ce_names_the_command(self):
        text = (PLUGIN / "tools" / "ce").read_text(encoding="utf-8")
        self.assertIn('merge-check) exec "$py" "$tools/ce_merge_check.py"', text)


if __name__ == "__main__":
    unittest.main()
