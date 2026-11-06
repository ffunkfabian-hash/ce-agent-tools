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
          "baseRefName": "main", "mergeable": "MERGEABLE", "additions": 30, "deletions": 5}
    pr.update(change.pop("pr", {}))
    base = {"repo": "o/r", "pr": pr, "checks": [], "files": [CHANGELOG, {"filename": "src/a.py", "status": "modified", "changes": 2, "patch": "@@\n+x\n-y"}],
            "sizes": {"src/a.py": 900}, "behind_by": 0, "base_config": CONFIG, "head_config": CONFIG,
            "versions": {"base": "1.2.0", "head": "1.3.0"}, "changelog_name": "CHANGELOG.md", "changelog": "## 1.3.0\n"}
    base.update(change)
    return base


def ci_facts(**change):
    """The session's own pull request into the default branch with finished CI: what stage 1 wants beyond a green run."""
    change.setdefault("checks", [{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}])
    change.setdefault("local_head", HEAD)
    return facts(pr={"sameRepo": True, "defaultBranch": "main", **change.pop("pr", {})}, **change)


def test_file(name, patch, status="modified"):
    return {"filename": name, "status": status, "changes": patch.count("\n"), "patch": patch}


def removed(name):
    return test_file(name, "@@\n-x = 1", status="removed")


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
        self.assertIn("CI rot: a, b", self.reasons(checks=bad))
        self.assertIn("CI läuft noch: c, d", self.reasons(checks=bad))

    def test_a_queued_run_without_a_check_entry_is_not_green(self):
        self.assertIn("CI läuft noch: check", self.reasons(runs_open=["check"]))
        done = [{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}]
        self.assertEqual(mc.evaluate(facts(checks=done))["warnings"], [])
        self.assertIn("CI läuft noch: tests", self.reasons(checks=[{"name": "tests", "status": "QUEUED"}], runs_open=["tests"]))
        self.assertEqual(self.reasons(checks=[{"name": "tests", "status": "QUEUED"}], runs_open=["tests"]).count("tests"), 1)

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

    def spared_result(self, *extra, base=ci_facts):
        return mc.evaluate(base(files=facts()["files"] + list(extra)))

    def test_a_test_deleted_with_its_source_is_green_but_needs_the_ja(self):
        test = test_file("tests/test_old.py", "@@\n-import old_tool\n-def test_x():\n-    assert old_tool.y\n-def test_z():\n-    assert old_tool.z", status="removed")
        result = self.spared_result(test, removed("tools/old_tool.py"))
        self.assertTrue(result["green"], result)
        self.assertFalse(result["ohne_ja"])
        self.assertEqual(len(result["ja_noetig_weil"]), 1)
        self.assertIn("Tests gelöscht, weil ihre Quelle entfällt: tests/test_old.py", result["ja_noetig_weil"][0])
        self.assertEqual(result["warnings"], ["Testdatei mit ihrer Quelle gelöscht, nicht gezählt: tests/test_old.py (2 Testfälle, 2 Prüfungen), Quelle: tools/old_tool.py"])

    def test_the_source_counts_by_path_tail_file_name_or_import(self):
        lines = ('"tools/old_tool.py"', '"pkg/tools/old_tool.py"', '"./tools/old_tool.py"', 'ROOT / "pkg" / "old_tool.py"', "import old_tool as t",
                 "    from pkg.tools import old_tool", "from pkg.tools.old_tool import x", "from . import old_tool", "from .old_tool import x",
                 "import os, old_tool", "const t = require('./old_tool');", "import t from './old_tool';", "export * from './old_tool';", "} from './old_tool';")
        for line in lines:
            test = test_file("tests/test_old.py", f"@@\n-{line}\n-def test_x():\n-    assert y", status="removed")
            self.assertTrue(self.spared_result(test, removed("pkg/tools/old_tool.py"))["green"], line)

    def test_a_test_whose_source_stays_is_red(self):
        test = test_file("tests/test_old.py", "@@\n-import old_tool\n-def test_x():\n-    assert y", status="removed")
        self.assertIn("Tests abgeschwächt", " ".join(self.spared_result(test)["reasons"]))
        self.assertIn("Tests abgeschwächt", " ".join(self.spared_result(test, removed("tools/unrelated.py"))["reasons"]))

    def test_only_a_deleted_non_test_file_counts_as_source(self):
        test = test_file("tests/test_old.py", '@@\n-from tests.test_helper import h\n-PATH = "src/a.py"\n-DOC = "docs/README.md"\n-def test_x():\n-    assert y', status="removed")
        others = (removed("tests/test_helper.py"), test_file("src/a.py", "@@\n-x\n+y"), removed("sub/README.md"), removed("docs/README.md"))
        for other in others:
            result = self.spared_result(test, other)
            self.assertIn("Tests abgeschwächt", " ".join(result["reasons"]), other)
            self.assertIn("gelöscht: tests/test_old.py", " ".join(result["reasons"]), other)

    def test_a_similar_word_is_no_source(self):
        cases = (("src/a.py", 'P = "data.py"'), ("src/a.py", 'P = "my-a.py"'), ("src/a.py", 'P = "other/a.py"'), ("src/run.py", "subprocess.run(x)"),
                 ("src/run.py", "from subprocess import run"), ("src/a.py", "from other.a import x"), ("tools/mock.py", "from unittest import mock"),
                 ("src/config.py", "import core as config"), ("src/config.py", 'from the config file"""'), ("pkg/tools/old_tool.py", "from pkg.old_tool import x"),
                 ("legacy/config.json", "import config"), ("src/config.py", """self.assertIn("read from 'config'", out)"""),
                 ("src/users.py", """cur.execute('select * from "users"')"""), ("src/run.py", "# moved from 'run' earlier"), ("src/run.py", "yield from 'run'"))
        for source, line in cases:
            test = test_file("tests/test_old.py", f"@@\n-{line}\n-def test_x():\n-    assert y", status="removed")
            self.assertIn("Tests abgeschwächt", " ".join(self.spared_result(test, removed(source))["reasons"]), line)

    def test_a_path_without_an_extension_stays_red(self):
        test = test_file("tests/test_old.py", '@@\n-P = "bin/tool"\n-def test_x():\n-    assert y', status="removed")
        self.assertIn("Tests abgeschwächt", " ".join(self.spared_result(test, removed("bin/tool"))["reasons"]))

    def test_only_removed_lines_name_a_source(self):
        test = test_file("tests/test_old.py", '@@\n "src/a.py"\n+"src/a.py"\n-def test_x():\n-    assert y', status="removed")
        self.assertIn("Tests abgeschwächt", " ".join(self.spared_result(test, removed("src/a.py"))["reasons"]))

    def test_in_a_mix_only_the_unfit_test_counts(self):
        fit = test_file("tests/test_old.py", "@@\n-import old_tool\n-def test_a():\n-    assert a\n-def test_b():\n-    assert b", status="removed")
        unfit = test_file("tests/test_core.py", "@@\n-def test_c():\n-    assert c", status="removed")
        result = self.spared_result(fit, unfit, removed("tools/old_tool.py"))
        self.assertEqual(result["reasons"], ["Tests abgeschwächt: 1 Testfälle und 1 Prüfungen weniger (gelöscht: tests/test_core.py)"])
        self.assertEqual(result["warnings"], ["Testdatei mit ihrer Quelle gelöscht, nicht gezählt: tests/test_old.py (2 Testfälle, 2 Prüfungen), Quelle: tools/old_tool.py"])
        self.assertIn("Tests gelöscht, weil ihre Quelle entfällt: tests/test_old.py", " ".join(result["ja_noetig_weil"]))

    def test_a_spared_file_does_not_cover_a_weakened_one(self):
        fit = test_file("tests/test_old.py", "@@\n-import old_tool\n-def test_a():\n-    assert a", status="removed")
        edited = test_file("tests/test_core.py", "@@\n-        self.assertEqual(a, 1)")
        reasons = self.spared_result(fit, edited, removed("tools/old_tool.py"))["reasons"]
        self.assertEqual(reasons, ["Tests abgeschwächt: 0 Testfälle und 1 Prüfungen weniger (gekürzt: tests/test_core.py)"])

    def test_new_tests_do_not_pay_for_a_loss_beside_a_spared_file(self):
        fit = test_file("tests/test_old.py", "@@\n-import old_tool\n-def test_a():\n-    assert a\n-def test_b():\n-    assert b", status="removed")
        new = test_file("tests/test_new.py", "@@\n+import new_tool\n+def test_a():\n+    assert a\n+def test_b():\n+    assert b", status="added")
        cut = test_file("tests/test_core.py", "@@\n-def test_c():\n-    assert c\n-def test_d():\n-    assert d")
        reasons = self.spared_result(fit, new, cut, removed("tools/old_tool.py"))["reasons"]
        self.assertEqual(reasons, ["Tests abgeschwächt: 2 Testfälle und 2 Prüfungen weniger (gekürzt: tests/test_core.py)"])
        self.assertTrue(self.spared_result(fit, new, removed("tools/old_tool.py"))["green"])

    def test_a_merely_changed_test_stays_red_when_the_source_goes(self):
        test = test_file("tests/test_core.py", "@@\n-import old_tool\n-def test_x():\n-    assert y")
        self.assertIn("Tests abgeschwächt", " ".join(self.spared_result(test, removed("tools/old_tool.py"))["reasons"]))

    def test_a_deleted_test_without_a_patch_stays_red(self):
        test = {"filename": "tests/test_old.py", "status": "removed", "changes": 900}
        self.assertEqual(self.spared_result(test, removed("tools/old_tool.py"))["reasons"], ["Testdatei gelöscht, Inhalt nicht lesbar: tests/test_old.py"])

    def test_a_deleted_helper_without_tests_needs_no_exception(self):
        helper = test_file("tests/helper.py", "@@\n-import old_tool\n-X = 1", status="removed")
        result = self.spared_result(helper, removed("tools/old_tool.py"))
        self.assertTrue(result["green"])
        self.assertTrue(result["ohne_ja"], result)

    def test_a_spared_test_writes_no_patch_receipt(self):
        test = test_file("tests/test_old.py", "@@\n-import old_tool\n-def test_x():\n-    assert y", status="removed")
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(mc, "receipt_path", return_value=Path(folder) / mc.RECEIPT_FILE):
            mc.update_receipt(self.spared_result(test, removed("tools/old_tool.py")), "")
            self.assertFalse((Path(folder) / mc.RECEIPT_FILE).exists())

    def test_a_rename_that_takes_a_test_out_of_the_run_is_red(self):
        def moved(old, new):
            return facts()["files"] + [{"filename": new, "previous_filename": old, "status": "renamed", "changes": 0}]
        for old, new in (("tests/test_a.py", "attic/a.py"), ("tests/test_a.py", "tests/a_off.py"), ("web/a.test.js", "web/a.js")):
            self.assertIn("aus dem Testlauf verschoben", self.reasons(files=moved(old, new)), new)
        for old, new in (("tests/test_a.py", "tests/test_b.py"), ("tests/test_a.py", "tests/sub/test_a.py"), ("src/a.py", "lib/a.py"),
                         ("tests/helper.py", "tests/support.py"), ("tests/fixtures/a.json", "fixtures/a.json")):
            self.assertEqual(self.reasons(files=moved(old, new)), "", new)

    def test_skip_markers_in_added_lines_are_red(self):
        for line in ("    @unittest.skip('later')", "@pytest.mark.xfail", "    [Ignore(\"x\")]", "  it.only('x', f)", "  describe.skip('x', f)", "  xit('x', f)", "        self.skipTest('later')"):
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
        self.assertEqual(mc.evaluate(facts(pr={"additions": 300, "deletions": 0}))["classification"], "Patch")
        result = mc.evaluate(facts(pr={"additions": 300, "deletions": 1}))
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

    def test_a_patch_between_the_two_limits_needs_the_ja_but_no_new_minor(self):
        """Punkte 13 und 15: 100 trennt ohne und mit Ja, 300 trennt Patch und Version."""
        for lines, classification, free in ((100, "Patch", True), (101, "Patch", False), (300, "Patch", False), (301, "Version", False)):
            result = mc.evaluate(ci_facts(pr={"additions": lines, "deletions": 0}, versions={"base": "1.2.0", "head": "1.2.1"}, changelog="1.2.1"))
            self.assertEqual((result["green"], result["classification"], result["ohne_ja"]), (True, classification, free), lines)
            self.assertEqual(result["ja_noetig_weil"], [] if free else [f"{lines} Zeilen, ohne Ja höchstens 100"], lines)
            self.assertEqual("nur Patch-Nummer erhöht" in " ".join(result["warnings"]), lines > 300, lines)

    def test_lines_are_counted_added_plus_deleted_for_the_stage_too(self):
        self.assertTrue(mc.evaluate(ci_facts(pr={"additions": 60, "deletions": 40}))["ohne_ja"])
        self.assertFalse(mc.evaluate(ci_facts(pr={"additions": 60, "deletions": 41}))["ohne_ja"])

    def test_without_core_paths_in_the_base_there_is_no_stage_one(self):
        for config in ({}, None, {**CONFIG, "core_paths": None}, {**CONFIG, "core_paths": []}, {**CONFIG, "core_paths": [""]}, {**CONFIG, "core_paths": [" ", "\t"]},
                       {**CONFIG, "core_paths": "contracts/**"}, {**CONFIG, "core_paths": [1, None]}):
            result = mc.evaluate(ci_facts(base_config=config))
            self.assertTrue(result["green"], config)
            self.assertFalse(result["ohne_ja"], config)
            self.assertEqual(result["ja_noetig_weil"], ["core_paths fehlt in der Basis"], config)
            self.assertIn("keine Stufe 1", " ".join(result["warnings"]), config)

    def test_a_list_only_in_the_pull_request_does_not_open_stage_one(self):
        result = mc.evaluate(ci_facts(base_config={}, head_config=CONFIG))
        self.assertFalse(result["ohne_ja"])

    def test_a_pull_request_that_changes_the_config_needs_the_ja(self):
        """Sonst nähme ein Patch ohne Ja den Kernpfad von der Liste oder die Prüfung weg, und der nächste Patch ginge ohne Ja durch."""
        result = mc.evaluate(ci_facts(head_config={**CONFIG, "core_paths": ["contracts/**"]}))
        self.assertEqual((result["green"], result["ohne_ja"], result["ja_noetig_weil"]), (True, False, [".ce-repo.json im PR geändert"]))
        self.assertFalse(mc.evaluate(ci_facts(head_config={**CONFIG, "check": {"quick": []}}))["ohne_ja"])
        self.assertTrue(mc.evaluate(ci_facts(base_config={**CONFIG, "version": "1.2.0"}, head_config={**CONFIG, "version": "1.3.0"}))["ohne_ja"])
        self.assertFalse(mc.evaluate(ci_facts(head_config={**CONFIG, "version": {"file": "other.json", "pattern": "x"}}))["ohne_ja"], "the check of the next patch")

    def test_a_core_path_or_a_red_run_is_never_stage_one(self):
        core = {"filename": "hooks/guard.py", "status": "modified", "changes": 1, "patch": "@@\n+x"}
        result = mc.evaluate(ci_facts(files=ci_facts()["files"] + [core]))
        self.assertEqual((result["ohne_ja"], result["ja_noetig_weil"]), (False, ["Kernpfad berührt"]))
        docs = {"filename": "contracts/a.md", "status": "modified", "changes": 1, "patch": "@@\n+x"}
        self.assertFalse(mc.evaluate(ci_facts(files=[docs]))["ohne_ja"], "a text under a core path is a core path")
        red = mc.evaluate(ci_facts(pr={"mergeable": "CONFLICTING"}))
        self.assertEqual((red["green"], red["ohne_ja"], red["ja_noetig_weil"]), (False, False, ["merge-check nicht grün"]))

    def test_no_stage_one_before_ci_has_reported(self):
        """Richter 0.35 Runde 2: right after a push the run is green with no checks at all; stage 2 keeps its warning."""
        result = mc.evaluate(ci_facts(checks=[]))
        self.assertEqual((result["green"], result["ohne_ja"], result["ja_noetig_weil"]), (True, False, ["keine erfolgreichen Checks zum Head gemeldet"]))
        self.assertFalse(mc.evaluate(ci_facts(checks=[], runs_open=["tests"]))["ohne_ja"])
        self.assertFalse(mc.evaluate(ci_facts(checks=[{"name": "tests", "status": "QUEUED"}]))["ohne_ja"])
        self.assertFalse(mc.evaluate(ci_facts(checks=[{"name": "tests", "status": "COMPLETED", "conclusion": "FAILURE"}]))["ohne_ja"])
        for conclusion in ("SKIPPED", "NEUTRAL"):
            self.assertFalse(mc.evaluate(ci_facts(checks=[{"name": "tests", "status": "COMPLETED", "conclusion": conclusion}]))["ohne_ja"], conclusion)
        self.assertTrue(mc.evaluate(ci_facts())["ohne_ja"])
        self.assertTrue(mc.evaluate(ci_facts(checks=[{"context": "ci", "state": "SUCCESS"}]))["ohne_ja"])

    def test_where_no_ci_can_come_stage_one_does_not_wait_for_it(self):
        """Richter 0.35 Runde 3, Nr 7.5: a pull request of .md files starts no CI; without a workflow file 0.37 wants the receipt instead."""
        self.assertTrue(mc.evaluate(ci_facts(checks=[], workflows=0))["ohne_ja"])  # old facts; gather now adds workflow_files, see VerifyReceiptTests
        self.assertFalse(mc.evaluate(no_ci_facts(workflows=0, verify=None))["ohne_ja"], "0.37: the receipt of ./ce verify replaces the check")
        docs = {"filename": "docs/a.md", "status": "modified", "changes": 2, "patch": "@@\n+x"}
        self.assertTrue(mc.evaluate(ci_facts(checks=[], files=[CHANGELOG, docs], workflows=2))["ohne_ja"])
        self.assertFalse(mc.evaluate(ci_facts(checks=[], workflows=1))["ohne_ja"])

    def test_stage_one_is_for_the_own_pull_request_into_the_default_branch(self):
        """Richter 0.35 Runde 3, Nr 7.1, 7.2, 7.4: a receipt survives `gh pr edit --base main`; forks and other sessions have no patch door."""
        for change, text in (({"pr": {"baseRefName": "claude/side"}}, "Basis ist nicht der Standardbranch"), ({"pr": {"defaultBranch": None}}, "Basis ist nicht der Standardbranch"),
                             ({"pr": {"sameRepo": False}}, "PR aus einem anderen Repo"), ({"local_head": "d" * 40}, "Head ist nicht der Stand dieses Ordners"),
                             ({"local_head": None}, "Head ist nicht der Stand dieses Ordners"), ({"cloud": True}, "Cloud-Sitzung: Merge-Bitte an Sightline")):
            result = mc.evaluate(ci_facts(**change))
            self.assertEqual((result["green"], result["ohne_ja"], result["ja_noetig_weil"]), (True, False, [text]), change)

    def test_what_stayed_unchecked_keeps_stage_one_shut(self):
        """Richter 0.35 Runde 3, Nr 7.8."""
        unread = {"filename": "tests/test_a.py", "status": "modified", "changes": 2}
        for change in ({"files": facts()["files"] + [unread]}, {"versions": None}, {"changelog_name": None}, {"versions": {"base": None, "head": "1.3.0"}}):
            result = mc.evaluate(ci_facts(**change))
            self.assertEqual((result["green"], result["ohne_ja"], result["ja_noetig_weil"]), (True, False, ["Teile nicht geprüft"]), change)

    def test_a_pattern_with_blanks_around_it_still_guards(self):
        core = {"filename": "hooks/guard.py", "status": "modified", "changes": 1, "patch": "@@\n+x"}
        config = {**CONFIG, "core_paths": [" hooks/guard.py\t"]}
        result = mc.evaluate(ci_facts(files=facts()["files"] + [core], base_config=config, head_config=config))
        self.assertEqual((result["core_paths_touched"], result["ohne_ja"]), (["hooks/guard.py"], False))

    def test_the_door_of_this_repository_is_on_its_own_list(self):
        """Richter 0.35 Runde 3, Nr 7.3: a patch without Ja must not change the limits of patches without Ja, nor the check that guards them."""
        config = json.loads((ROOT / ".ce-repo.json").read_text(encoding="utf-8"))
        names = [PLUGIN.relative_to(ROOT).as_posix() + "/" + name for name in ("tools/ce_merge_check.py", "hooks/guard.py", "hooks/codex_guard.py", "hooks/vibe_guard.py", "hooks/antigravity_guard.py")]
        names += [".github/workflows/check.yml", ".ce-repo.json", PLUGIN.relative_to(ROOT).as_posix() + "/agents/ce-judge.md"]
        self.assertEqual(mc.core_hits([{"filename": name} for name in names], config["core_paths"]), sorted(names))

    def test_from_100_lines_of_code_the_result_asks_for_the_judge(self):
        def run(*files):
            result = mc.evaluate(facts(files=list(files)))
            return result["code_lines"], "Richter nötig" in " ".join(result["warnings"]), result["green"]
        code = {"filename": "src/a.py", "status": "modified", "patch": "@@\n+x"}
        self.assertEqual(run(CHANGELOG, {**code, "changes": 99}), (99, False, True))
        self.assertEqual(run(CHANGELOG, {**code, "changes": 100}), (100, True, True))
        self.assertEqual(run(CHANGELOG, {**code, "changes": 60}, {**code, "filename": "tests/a.json", "changes": 40}), (100, True, True))
        self.assertEqual(run(CHANGELOG, {**code, "changes": 50}, {**code, "filename": "docs/A.MD", "changes": 400}), (50, False, True))

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


def rest_pr(pr):
    """A normalized pull request as GitHub's REST API answers it."""
    return {"number": pr["number"], "html_url": pr["url"], "title": pr["title"], "state": pr["state"].lower(), "merged": pr["state"] == "MERGED",
            "draft": pr["isDraft"], "head": {"sha": pr["headRefOid"], "ref": pr["headRefName"]}, "base": {"ref": pr["baseRefName"]},
            "mergeable": {"MERGEABLE": True, "CONFLICTING": False}.get(pr["mergeable"]), "additions": pr["additions"], "deletions": pr["deletions"]}


class FakeGh:
    """Answers `gh api` by the path of the call; a test changes the dict."""

    def __init__(self, **change):
        pr = dict(facts()["pr"])
        self.answers = {"pr": [json.dumps(rest_pr(pr))], "files": [{"filename": "CHANGELOG.md", "status": "modified", "changes": 1, "patch": "@@\n+x"},
                                                          {"filename": "gone.py", "status": "removed", "changes": 1, "patch": "@@\n-x"}],
                        "config": json.dumps({"version": {"file": "v.json", "pattern": "\"v\": \"(.+?)\""}, "docs": {"changelog": "CHANGELOG.md"}}),
                        "version": {"main": '{"v": "1.2.0"}', HEAD: '{"v": "1.3.0"}'}, "behind": 0,
                        "tree": {"truncated": False, "tree": [{"path": "CHANGELOG.md", "type": "blob", "size": 77}, {"path": "gone.py", "type": "blob", "size": 5},
                                                              {"path": "sub", "type": "commit"}]}}
        self.answers.update(change)
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        assert args[0] == "api" and "graphql" not in args and "pr" not in args[:1], args  # REST only: cloud sessions get 403 on GraphQL
        target = args[-1]
        if "/files?" in target:
            return json.dumps(self.answers["files"] if "page=1" in target else [])
        if "/check-runs" in target:
            return json.dumps({"check_runs": self.answers.get("check_runs", [])})
        if target.split("?")[0].endswith("/status"):
            return json.dumps({"state": "pending", "statuses": self.answers.get("statuses", [])})
        if "/git/trees/" in target:
            return json.dumps(self.answers["tree"])
        if "/contents/" in target and "raw" not in " ".join(args):
            return json.dumps({"type": "file", "size": 88})
        if "/actions/workflows" in target:
            return self.answers["workflows"] if "workflows" in self.answers else json.dumps({"workflows": [{"state": "active"}, {"state": "disabled_manually"}]})
        if "/actions/runs" in target:
            return json.dumps({"workflow_runs": self.answers.get("runs", [])})
        if "/compare/" in target:
            return json.dumps({"behind_by": self.answers["behind"]})
        if "/pulls/" in target:
            return self.answers["pr"].pop(0) if len(self.answers["pr"]) > 1 else self.answers["pr"][0]
        ref = target.rsplit("ref=", 1)[-1]
        if ".ce-repo.json" in target:
            return self.answers["config"]
        if "/v.json" in target:
            return self.answers["version"][ref]
        if "CHANGELOG" in target:
            return "## 1.3.0"
        return None


class GatherTests(unittest.TestCase):
    def test_gather_reads_all_facts_over_rest_and_only_sizes_files_that_exist(self):
        fake = FakeGh()
        with mock.patch.object(mc, "gh", fake):
            found = mc.gather("o/r", 7)
            result = mc.evaluate(found)
        self.assertTrue(result["green"], result)
        self.assertEqual(found["sizes"], {"CHANGELOG.md": 77})  # gone.py was removed by the pull request
        self.assertTrue(all(call[0] == "api" for call in fake.calls))

    def test_checks_come_from_check_runs_and_commit_statuses_of_the_head(self):
        fake = FakeGh(check_runs=[{"name": "a", "status": "completed", "conclusion": "failure"}, {"name": "b", "status": "in_progress", "conclusion": None},
                                  {"name": "c", "status": "completed", "conclusion": "success"}],
                      statuses=[{"context": "d", "state": "success"}, {"context": "e", "state": "error"}])
        with mock.patch.object(mc, "gh", fake):
            checks = mc.read_checks("o/r", HEAD)
            reasons = mc.evaluate(mc.gather("o/r", 7))["reasons"]
        self.assertEqual(mc.ci_state(checks), (["a", "e"], ["b"]))
        self.assertIn("CI rot: a, e", reasons)
        self.assertIn("CI läuft noch: b", reasons)
        self.assertTrue(any(f"commits/{HEAD}/check-runs" in call[-1] for call in fake.calls))

    def test_the_rest_pull_request_is_read_in_the_shape_evaluate_expects(self):
        base = dict(facts()["pr"])
        for rest, expected in ((dict(rest_pr(base)), base),
                               ({**rest_pr(base), "merged": True, "state": "closed", "mergeable": None}, {**base, "state": "MERGED", "mergeable": "UNKNOWN"}),
                               ({**rest_pr(base), "draft": True, "mergeable": False}, {**base, "isDraft": True, "mergeable": "CONFLICTING"})):
            for key in ("number", "state", "isDraft", "headRefOid", "baseRefName", "mergeable", "additions", "deletions"):
                self.assertEqual(mc.normalize_pr(rest)[key], expected[key], (key, rest))

    def test_a_merged_pull_request_is_not_asked_again_for_its_mergeability(self):
        merged = json.dumps({**rest_pr(facts()["pr"]), "merged": True, "state": "closed", "mergeable": None})
        fake = FakeGh(pr=[merged])
        slept = []
        with mock.patch.object(mc, "gh", fake):
            self.assertEqual(mc.read_pr("o/r", 7, sleep=slept.append)["state"], "MERGED")
        self.assertEqual((len(fake.calls), slept), (1, []))

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
        fake = FakeGh(pr=[json.dumps(rest_pr(pr)), json.dumps(rest_pr({**pr, "headRefOid": "d" * 40}))])
        with mock.patch.object(mc, "gh", fake), self.assertRaisesRegex(mc.NotCheckable, "während der Prüfung"):
            mc.gather("o/r", 7)

    def test_workflows_are_counted_and_unreadable_ones_keep_stage_one_waiting(self):
        with mock.patch.object(mc, "gh", FakeGh()):
            self.assertEqual(mc.gather("o/r", 7)["workflows"], 2, "a switched off workflow counts")
        for answer, count in ((json.dumps({"workflows": []}), 0), (None, 1), ("kein json", 1), ("[]", 1)):
            with mock.patch.object(mc, "gh", FakeGh(workflows=answer)):
                self.assertEqual(mc.workflow_count("o/r"), count, answer)

    def test_the_place_decides_the_cloud_fact(self):
        with mock.patch.object(mc.platform, "system", return_value="Linux"), mock.patch.dict(os.environ, {"CLAUDE_CODE_REMOTE": "1"}):
            self.assertTrue(mc.on_cloud())
        with mock.patch.object(mc.platform, "system", return_value="Darwin"):
            self.assertFalse(mc.on_cloud())

    def test_the_rest_answer_gives_the_default_branch_and_the_origin_of_the_head(self):
        same = {"full_name": "o/r", "default_branch": "main"}
        pr = mc.normalize_pr({"head": {"repo": same}, "base": {"repo": same}})
        self.assertEqual((pr["sameRepo"], pr["defaultBranch"]), (True, "main"))
        self.assertFalse(mc.normalize_pr({"head": {"repo": {"full_name": "x/r"}}, "base": {"repo": same}})["sameRepo"])
        self.assertFalse(mc.normalize_pr({"head": {"repo": None}, "base": {"repo": same}})["sameRepo"], "deleted fork")
        self.assertFalse(mc.normalize_pr({})["sameRepo"])

    def test_open_workflow_runs_of_the_head_are_read(self):
        fake = FakeGh(runs=[{"name": "check", "status": "queued"}, {"name": "alt", "status": "completed"}])
        with mock.patch.object(mc, "gh", fake):
            self.assertEqual(mc.gather("o/r", 7)["runs_open"], ["check"])

    def test_an_unknown_mergeable_state_is_asked_again(self):
        pr = dict(facts()["pr"])
        fake = FakeGh(pr=[json.dumps(rest_pr({**pr, "mergeable": "UNKNOWN"})), json.dumps(rest_pr(pr))])
        with mock.patch.object(mc, "gh", fake):
            self.assertEqual(mc.read_pr("o/r", 7, sleep=lambda _: None)["mergeable"], "MERGEABLE")

    def test_more_than_one_page_of_files_is_read(self):
        pages = {1: [{"filename": str(i)} for i in range(100)], 2: [{"filename": "last"}]}
        with mock.patch.object(mc, "gh", lambda *a: json.dumps(pages[int(a[-1].rsplit("=", 1)[1])])):
            files, capped = mc.changed_files("o/r", 7)
        self.assertEqual((len(files), capped), (101, False))

    def test_the_3000_files_limit_is_reported_and_makes_the_run_red(self):
        with mock.patch.object(mc, "gh", lambda *a: json.dumps([{"filename": "f"}] * 100)):
            files, capped = mc.changed_files("o/r", 7)
        self.assertEqual((len(files), capped), (3000, True))
        self.assertIn("3000", self.capped_reasons())

    def capped_reasons(self):
        return " ".join(mc.evaluate(facts(files_capped=True))["reasons"])

    def test_sizes_come_from_one_tree_read_and_from_contents_when_the_tree_is_cut_off(self):
        tree = {"truncated": False, "tree": [{"path": "a", "type": "blob", "size": 9}, {"path": "b", "type": "blob", "size": 8},
                                             {"path": "sub", "type": "commit"}, {"path": "dir", "type": "tree"}]}
        calls = []

        def answer(*args):
            calls.append(args)
            return json.dumps(tree)
        with mock.patch.object(mc, "gh", answer):
            self.assertEqual(mc.blob_sizes("o/r", HEAD, ["a", "sub", "dir", "missing"]), {"a": 9})
        self.assertEqual(len(calls), 1)
        self.assertEqual(mc.blob_sizes("o/r", HEAD, []), {})
        tree["truncated"] = True
        fake = FakeGh(tree=tree)
        with mock.patch.object(mc, "gh", fake):
            sizes = mc.blob_sizes("o/r", HEAD, [f"p{i}" for i in range(150)])
        self.assertEqual((len(sizes), set(sizes.values())), (150, {88}))
        self.assertEqual(len([c for c in fake.calls if "/contents/" in c[-1]]), 150)

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


OLD_COMMIT, NEW_COMMIT = "a" * 40, "b" * 40
OLD_PIN = {"version": "0.36.2", "commit": OLD_COMMIT}
NEW_PIN = {"version": "0.37.0", "commit": NEW_COMMIT}
PINNED = {**CONFIG, "tools_version": OLD_PIN}
MOVED = {**CONFIG, "tools_version": NEW_PIN}


def pin_facts(**change):
    change.setdefault("base_config", PINNED)
    change.setdefault("head_config", MOVED)
    change.setdefault("pin_problem", "")
    return ci_facts(**change)


def tools_gh(on_main="ahead", move="ahead", version="0.37.0", fail=""):
    """gh for pin_problem: what ce-agent-tools answers; `fail` names the part of a path that breaks."""
    def answer(*args):
        target = args[-1]
        if fail and fail in target:
            raise mc.NotCheckable(f"gh api: HTTP 502 ({fail})")
        if "/contents/" in target:
            return json.dumps({"version": version}) if version else None
        return json.dumps({"status": on_main if target.split("?")[0].endswith("...main") else move})
    return answer


class PinTests(unittest.TestCase):
    """Entscheidung 300: a pull request that only moves tools_version keeps stage 1, if ce-agent-tools confirms the pin."""

    def stage_two(self, **change):
        result = mc.evaluate(pin_facts(**change))
        self.assertFalse(result["ohne_ja"], result)
        return result

    def test_the_pin_alone_is_stage_one(self):
        result = mc.evaluate(pin_facts())
        self.assertEqual((result["green"], result["ohne_ja"], result["ja_noetig_weil"], result["pin_bestaetigt"]), (True, True, [], True))

    def test_a_pin_with_a_changed_core_path_list_is_stage_two(self):
        result = self.stage_two(head_config={**MOVED, "core_paths": ["contracts/**"]})
        self.assertEqual(result["ja_noetig_weil"], [".ce-repo.json im PR geändert"])
        self.stage_two(head_config={**MOVED, "check": {"quick": ["true"]}})
        self.stage_two(head_config={**MOVED, "version": {"file": "other.json", "pattern": "x"}})

    def test_only_the_pin_key_is_excused_and_a_version_string_may_move_with_it(self):
        self.assertTrue(mc.only_pin_changed({**PINNED, "version": "1.2"}, {**MOVED, "version": "1.3"}))
        self.assertFalse(mc.only_pin_changed(PINNED, PINNED))
        self.assertFalse(mc.only_pin_changed(PINNED, None))
        self.assertFalse(mc.only_pin_changed(None, MOVED))
        self.assertFalse(mc.only_pin_changed(PINNED, {**MOVED, "tools_dir": "x"}))
        self.assertTrue(mc.only_pin_changed(CONFIG, MOVED), "the key itself; pin_problem refuses a first pin")
        self.assertEqual(mc.pin_problem(CONFIG, MOVED), "tools_version nicht lesbar")

    def test_a_pin_outside_main_of_the_tools_is_stage_two(self):
        for status in ("diverged", "behind"):
            with mock.patch.object(mc, "gh", tools_gh(on_main=status)):
                problem = mc.pin_problem(PINNED, MOVED)
            self.assertIn("liegt nicht auf main von ce-agent-tools", problem)
            result = self.stage_two(pin_problem=problem)
            self.assertEqual(result["ja_noetig_weil"], [f".ce-repo.json im PR geändert (Pin nicht bestätigt: {problem})"])
        with mock.patch.object(mc, "gh", tools_gh(on_main="identical")):
            self.assertEqual(mc.pin_problem(PINNED, MOVED), "", "the tip of main is on main")

    def test_a_pin_that_goes_back_is_stage_two(self):
        back = {"version": "0.35.0", "commit": NEW_COMMIT}
        with mock.patch.object(mc, "gh", tools_gh(version="0.35.0")):
            problem = mc.pin_problem(PINNED, {**CONFIG, "tools_version": back})
        self.assertEqual(problem, "Version sinkt (0.36.2 → 0.35.0)")
        self.stage_two(head_config={**CONFIG, "tools_version": back}, pin_problem=problem)
        for move in ("behind", "identical"):
            with mock.patch.object(mc, "gh", tools_gh(move=move)):
                self.assertIn("Pin geht nicht vorwärts", mc.pin_problem(PINNED, MOVED), "an older commit behind a raised number")
        same = {**CONFIG, "tools_version": {"version": "0.36.2", "commit": NEW_COMMIT}}
        with mock.patch.object(mc, "gh", tools_gh(version="0.36.2", move="diverged")):
            self.assertIn("Pin geht nicht vorwärts", mc.pin_problem(PINNED, same))
        with mock.patch.object(mc, "gh", tools_gh(move="diverged")):
            self.assertEqual(mc.pin_problem(PINNED, MOVED), "", "an old pin on a branch, the number rises")

    def test_the_commit_must_carry_the_version_the_pin_names(self):
        with mock.patch.object(mc, "gh", tools_gh(version="0.30.0")):
            self.assertEqual(mc.pin_problem(PINNED, MOVED), f"Commit {NEW_COMMIT[:7]} trägt Version 0.30.0, nicht 0.37.0")
        with mock.patch.object(mc, "gh", tools_gh(version=None)):
            self.assertIn("trägt Version None", mc.pin_problem(PINNED, MOVED))

    def test_a_malformed_pin_is_stage_two(self):
        for value in ("0.37.0 abc", {"version": "0.37.0"}, {"version": "neu", "commit": NEW_COMMIT}, {"version": "0.37.0", "commit": "main"},
                      {"version": "0.37.0", "commit": NEW_COMMIT[:12]}):
            self.assertEqual(mc.pin_problem(PINNED, {**CONFIG, "tools_version": value}), "tools_version nicht lesbar", value)

    def test_a_failed_query_is_stage_two_and_not_an_abort(self):
        for part in ("/contents/", "...main", f"{OLD_COMMIT}..."):
            with mock.patch.object(mc, "gh", tools_gh(fail=part)):
                problem = mc.pin_problem(PINNED, MOVED)
            self.assertIn("nicht prüfbar", problem, part)
            result = self.stage_two(pin_problem=problem)
            self.assertTrue(result["green"], "merge-check itself stays answerable")
        with mock.patch.object(mc, "gh", lambda *args: "kein json"):
            self.assertIn("nicht prüfbar", mc.pin_problem(PINNED, MOVED))

    def test_without_an_answer_from_the_tools_the_pin_counts_as_unconfirmed(self):
        self.assertEqual(self.stage_two(pin_problem=None)["ja_noetig_weil"], [".ce-repo.json im PR geändert"])
        self.stage_two(pin_problem="x")

    def test_where_the_config_is_a_core_path_the_pin_alone_stays_a_patch(self):
        """Nexogen and Agent Tools list .ce-repo.json: without this the pin would be a Version and a raised patch number red."""
        listed = {**CONFIG, "core_paths": [*CONFIG["core_paths"], ".ce-repo.json"]}
        config_file = {"filename": ".ce-repo.json", "status": "modified", "changes": 2, "patch": "@@\n+x\n-y"}
        files = [CHANGELOG, config_file]
        change = {"base_config": {**listed, "tools_version": OLD_PIN}, "head_config": {**listed, "tools_version": NEW_PIN}, "files": files,
                  "versions": {"base": "1.2.0", "head": "1.2.1"}, "changelog": "## 1.2.1\n"}
        result = mc.evaluate(pin_facts(**change))
        self.assertEqual((result["green"], result["classification"], result["ohne_ja"], result["core_paths_touched"]), (True, "Patch", True, []), result)
        unconfirmed = mc.evaluate(pin_facts(pin_problem="x", **change))
        self.assertEqual((unconfirmed["classification"], unconfirmed["ohne_ja"], unconfirmed["core_paths_touched"]), ("Version", False, [".ce-repo.json"]))
        other = {"filename": "hooks/guard.py", "status": "modified", "changes": 1, "patch": "@@\n+x"}
        mixed = mc.evaluate(pin_facts(**{**change, "files": [*files, other]}))
        self.assertEqual((mixed["ohne_ja"], mixed["core_paths_touched"]), (False, ["hooks/guard.py"]))

    def test_every_other_condition_of_stage_one_stays(self):
        for change, text in (({"checks": []}, "keine erfolgreichen Checks zum Head gemeldet"), ({"local_head": "d" * 40}, "Head ist nicht der Stand dieses Ordners"),
                             ({"cloud": True}, "Cloud-Sitzung: Merge-Bitte an Sightline"), ({"pr": {"additions": 200}}, "205 Zeilen, ohne Ja höchstens 100")):
            self.assertIn(text, mc.evaluate(pin_facts(**change))["ja_noetig_weil"], change)
        self.assertFalse(mc.evaluate(pin_facts(versions={"base": "1.3.0", "head": "1.3.0"}))["green"], "the version of the repository still has to rise")

    def test_gather_asks_the_tools_only_for_a_pure_pin(self):
        def configs(head_config):
            fake = FakeGh()

            def answer(*args):
                target = args[-1]
                if mc.TOOLS_REPO in target:
                    return tools_gh()(*args)
                if ".ce-repo.json" in target:
                    return json.dumps(head_config if target.endswith(HEAD) else PINNED_GATHER)
                return fake(*args)
            return answer
        PINNED_GATHER = {**json.loads(FakeGh().answers["config"]), "tools_version": OLD_PIN}
        with mock.patch.object(mc, "gh", configs({**PINNED_GATHER, "tools_version": NEW_PIN})):
            self.assertEqual(mc.gather("o/r", 7)["pin_problem"], "")
        with mock.patch.object(mc, "gh", configs(PINNED_GATHER)):
            self.assertIsNone(mc.gather("o/r", 7)["pin_problem"])
        with mock.patch.object(mc, "gh", configs({**PINNED_GATHER, "tools_version": NEW_PIN, "lint": {}})):
            self.assertIsNone(mc.gather("o/r", 7)["pin_problem"])

    def test_the_tools_repository_is_the_one_the_loader_downloads(self):
        import ce_loader
        self.assertEqual(mc.TOOLS_REPO, ce_loader.REPO)
        self.assertTrue((ROOT / mc.TOOLS_PLUGIN).is_file())


def receipt(**change):
    return {"repo": "o/r", "pr": 7, "head": HEAD, "green": True, "check": ["sh", "check"], **change}


def no_ci_facts(**change):
    config = {**CONFIG, "check": {"quick": ["sh", "check"]}}
    change.setdefault("checks", [])
    change.setdefault("workflow_files", 0)
    change.setdefault("verify", receipt())
    return ci_facts(base_config=config, head_config=config, **change)


class VerifyReceiptTests(unittest.TestCase):
    """Entscheidung 301: without GitHub CI the receipt of ./ce verify takes the place of a green check, per head SHA."""

    def test_without_ci_and_without_a_receipt_merge_check_is_red(self):
        result = mc.evaluate(no_ci_facts(verify=None))
        self.assertFalse(result["green"])
        self.assertTrue(result["verify_noetig"])
        self.assertIn(f"Keine Quittung von ./ce verify zu Head {HEAD[:7]}: Repo ohne GitHub-CI, ./ce verify o/r 7 am Mac", " ".join(result["reasons"]))

    def test_a_receipt_of_this_head_is_green_and_takes_the_place_of_the_check(self):
        result = mc.evaluate(no_ci_facts())
        self.assertEqual((result["green"], result["ohne_ja"], result["warnings"]), (True, True, []), result)

    def test_a_new_head_makes_the_receipt_worthless(self):
        self.assertIn("gilt nicht für Head", " ".join(mc.evaluate(no_ci_facts(verify=receipt(head="d" * 40)))["reasons"]))

    def test_a_receipt_that_is_not_green_or_from_elsewhere_is_none(self):
        for change in ({"green": False}, {"green": "true"}, {"repo": "o/other"}):
            self.assertFalse(mc.evaluate(no_ci_facts(verify=receipt(**change)))["green"], change)
        for broken in ("kaputt", [], 1):
            self.assertFalse(mc.evaluate(no_ci_facts(verify=broken))["green"], broken)

    def test_a_receipt_must_have_run_the_check_of_the_base(self):
        self.assertIn("nicht das check.quick der Basis", " ".join(mc.evaluate(no_ci_facts(verify=receipt(check=["true"])))["reasons"]))

    def test_where_ci_runs_all_stays_as_it_was(self):
        for files in (1, 2):
            result = mc.evaluate(ci_facts(workflow_files=files))
            self.assertEqual((result["green"], result["ohne_ja"], result["verify_noetig"]), (True, True, False), files)
        self.assertEqual(mc.evaluate(ci_facts(checks=[], workflow_files=1))["ja_noetig_weil"], ["keine erfolgreichen Checks zum Head gemeldet"])
        self.assertFalse(mc.evaluate(ci_facts(workflow_files=None))["verify_noetig"], "unreadable: nothing is concluded")

    def test_a_workflow_left_in_the_actions_list_is_no_ci(self):
        """Formgen v2: the closed PR 13 stays in the list although main has no workflow file."""
        self.assertFalse(mc.evaluate(no_ci_facts(workflows=1, verify=None))["green"])
        self.assertTrue(mc.evaluate(no_ci_facts(workflows=1))["ohne_ja"])

    def test_a_text_change_needs_no_receipt(self):
        docs = {"filename": "docs/a.md", "status": "modified", "changes": 2, "patch": "@@\n+x"}
        result = mc.evaluate(no_ci_facts(verify=None, files=[CHANGELOG, docs]))
        self.assertEqual((result["green"], result["ohne_ja"], result["verify_noetig"]), (True, True, False))

    def test_a_rename_to_a_text_is_no_text_change(self):
        """Richter 0.37, Nr 4.2: run.py → run.md takes code out of the run, in both worlds."""
        moved = {"filename": "src/run.md", "previous_filename": "src/run.py", "status": "renamed", "changes": 0}
        self.assertFalse(mc.evaluate(no_ci_facts(verify=None, files=[CHANGELOG, moved]))["green"])
        self.assertIn("keine erfolgreichen Checks zum Head gemeldet", mc.evaluate(ci_facts(checks=[], files=[CHANGELOG, moved]))["ja_noetig_weil"])

    def test_a_base_without_check_quick_is_no_dead_end_and_no_stage_one(self):
        """Richter 0.37, Nr 4.3: ./ce verify refuses there, so a red run could never turn green, not even the pull request that adds the check."""
        for config in ({**CONFIG}, {**CONFIG, "check": {"full": ["x"]}}, {**CONFIG, "check": ["x"]}):
            result = mc.evaluate(ci_facts(checks=[], workflow_files=0, base_config=config, head_config=config))
            self.assertEqual((result["green"], result["ohne_ja"], result["verify_noetig"]), (True, False, False), config)
            self.assertIn("Teile nicht geprüft", result["ja_noetig_weil"])
            self.assertIn("ohne check.quick in der Basis", " ".join(result["warnings"]))

    def test_the_ja_does_not_replace_the_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run(["git", "init", "-q", folder], check=True)
            self.assertEqual(mc.update_receipt(mc.evaluate(no_ci_facts(verify=None)), "Ja", cwd=folder), "")

    def test_a_cloud_session_gets_no_exception(self):
        result = mc.evaluate(no_ci_facts(cloud=True, verify=None))
        self.assertFalse(result["green"])
        self.assertIn("Merge-Bitte an Sightline", " ".join(result["reasons"]))

    def test_workflow_files_counts_what_github_would_run(self):
        files = json.dumps([{"name": "check.yml"}, {"name": "b.yaml"}, {"name": "README.md"}])
        for answer, count in ((files, 2), (None, 0), ("[]", 0), ("kein json", None), ('{"type": "file"}', None)):
            with mock.patch.object(mc, "gh", lambda *args: answer):
                self.assertEqual(mc.workflow_files("o/r", "main"), count, answer)

    def test_gather_reads_the_files_of_the_base_and_the_receipt(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"CE_VERIFY_CACHE": folder}):
            path = mc.verify_path("o/r", HEAD)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(receipt()), encoding="utf-8")
            fake = FakeGh(workflows=json.dumps({"workflows": []}))
            with mock.patch.object(mc, "gh", fake):
                found = mc.gather("o/r", 7)
            self.assertEqual((found["workflow_files"], found["verify"]), (0, receipt()))
            self.assertFalse(any(".github/workflows" in call[-1] for call in fake.calls), "no workflow at all: nothing to read")
            answers = FakeGh()

            def ghost(*args):
                return None if ".github/workflows?ref=main" in args[-1] else answers(*args)
            with mock.patch.object(mc, "gh", ghost):
                self.assertEqual(mc.gather("o/r", 7)["workflow_files"], 0)
            path.write_text("kaputt", encoding="utf-8")
            self.assertIsNone(mc.read_verify("o/r", HEAD))
            self.assertIsNone(mc.read_verify("o/r", "d" * 40))


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

    def run_main(self, *args, fail=None, given=None):
        out, err = io.StringIO(), io.StringIO()
        gather = mock.Mock(side_effect=fail) if fail else mock.Mock(return_value=given or self.facts)
        with mock.patch.object(mc, "gather", gather), redirect_stdout(out), redirect_stderr(err):
            code = mc.main(["o/r", "7", *args])
        return code, out.getvalue(), err.getvalue()

    facts = ci_facts()
    needs_ja = facts | {"pr": {**facts["pr"], "additions": 150}}
    door = f"gh pr merge 7 -R o/r --match-head-commit {HEAD} --squash"

    def test_green_with_ja_writes_the_receipt_the_guard_accepts(self):
        code, out, err = self.run_main("--ja", "Ja, bitte mergen")
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual((result["green"], result["classification"], result["receipt"]), (True, "Patch", True))
        self.assertEqual(err.splitlines()[0], f"merge-check o/r#7 {HEAD[:7]}: grün · Einstufung Patch (35 Zeilen)")
        receipt = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual((receipt["pr"], receipt["head"], receipt["ja"], receipt["kind"]), (7, HEAD, "Ja, bitte mergen", "ja"))
        self.assertTrue(guard.receipt_allows(self.door, self.folder))

    def test_green_without_ja_gives_no_receipt_and_says_so(self):
        code, out, err = self.run_main(given=self.needs_ja)
        self.assertEqual((code, json.loads(out)["receipt"], self.file.exists()), (0, False, False))
        self.assertIn("--ja", err)
        self.assertIn("155 Zeilen, ohne Ja höchstens 100", err)
        self.assertFalse(guard.receipt_allows(self.door, self.folder))

    def test_a_green_stage_one_run_writes_a_patch_receipt_without_words(self):
        code, out, err = self.run_main()
        result = json.loads(out)
        self.assertEqual((code, result["ohne_ja"], result["receipt"], result["receipt_kind"]), (0, True, True, "patch"))
        receipt = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual((receipt["repo"], receipt["pr"], receipt["head"], receipt["kind"]), ("o/r", 7, HEAD, "patch"))
        self.assertNotIn("ja", receipt, "no words in a receipt nobody said them for")
        self.assertIn("Stufe 1, ohne Ja", err)
        self.assertIn(self.door, err)
        self.assertTrue(guard.receipt_allows(self.door, self.folder))
        self.assertFalse(guard.receipt_allows(self.door.replace("merge 7", "merge 8"), self.folder))

    def test_a_repo_without_core_paths_gets_no_patch_receipt_and_stage_two_still_works(self):
        without = self.facts | {"base_config": {}}
        code, out, err = self.run_main(given=without)
        self.assertEqual((code, json.loads(out)["ohne_ja"], self.file.exists()), (0, False, False))
        self.assertIn("core_paths fehlt in der Basis", err)
        self.assertFalse(guard.receipt_allows(self.door, self.folder))
        code, out, _ = self.run_main("--ja", "Ja", given=without)
        self.assertEqual((code, json.loads(out)["receipt_kind"]), (0, "ja"))
        self.assertTrue(guard.receipt_allows(self.door, self.folder))

    def test_a_run_before_ci_reported_gets_no_patch_receipt(self):
        code, out, err = self.run_main(given=ci_facts(checks=[]))
        self.assertEqual((code, json.loads(out)["receipt"], self.file.exists()), (0, False, False))
        self.assertIn("keine erfolgreichen Checks zum Head gemeldet", err)

    def test_a_red_run_gets_no_receipt_of_either_kind(self):
        red = self.facts | {"behind_by": 1}
        for args in ((), ("--ja", "Ja")):
            code, out, _ = self.run_main(*args, given=red)
            self.assertEqual((code, json.loads(out)["receipt_kind"], self.file.exists()), (1, "", False), args)

    def test_a_quote_of_blanks_is_no_ja_and_only_stage_one_is_left(self):
        self.run_main("--ja", "   ")
        self.assertEqual(json.loads(self.file.read_text(encoding="utf-8"))["kind"], "patch")
        self.run_main("--ja", "   ", given=self.needs_ja)
        self.assertFalse(self.file.exists())

    def test_every_later_run_removes_an_old_receipt(self):
        self.run_main("--ja", "Ja")
        self.assertTrue(self.file.exists())
        self.run_main(given=self.needs_ja)
        self.assertFalse(self.file.exists())
        self.run_main()
        self.assertTrue(self.file.exists(), "patch receipt")
        self.run_main(given=self.needs_ja)
        self.assertFalse(self.file.exists(), "a new head above the limit must not keep the patch receipt")
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
