"""Tests of ce_tidy.py (0.18 Sweepline): every protection has a case that is refused and a twin that is allowed.
Run: python3 -m unittest discover -s tests"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "ce-shared"
sys.path.insert(0, str(PLUGIN / "tools"))
import ce_nightly  # noqa: E402
import ce_tidy  # noqa: E402
import ce_worktrees  # noqa: E402

OLD = time.time() - 3 * 86400


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def write(path, text="x"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TidyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name)) / "home"
        (self.home / ".Trash").mkdir(parents=True)
        (self.home / ".codex" / "sessions").mkdir(parents=True)
        self.projects = self.home / ".claude" / "projects"
        self.projects.mkdir(parents=True)
        self.main = self.home / "Claude" / "repo"
        self.main.mkdir(parents=True)
        git(self.main, "init", "-q", "-b", "main")
        write(self.main / ".gitignore", "Library/\nout/\n")
        write(self.main / "README.md", "r")
        git(self.main, "add", ".")
        git(self.main, "commit", "-qm", "init")
        env = {"CE_TIDY_HOME": str(self.home), "CE_HOME": str(self.home / "Claude"), "CE_TRASH": str(self.home / ".Trash"),
               "CE_CODEX_SESSIONS": str(self.home / ".codex" / "sessions"), "CE_TIDY_WORKTREE_ROOTS": ""}
        patches = [mock.patch.dict(os.environ, env), mock.patch.object(ce_worktrees, "default_repos", return_value=[]),
                   mock.patch.object(ce_worktrees, "process_cwds", return_value={}),
                   mock.patch.object(ce_tidy.ce_common, "repo_hold", return_value=None)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def worktree(self, name, root=None):
        path = (root or self.main / ".claude" / "worktrees") / name
        path.parent.mkdir(parents=True, exist_ok=True)
        git(self.main, "worktree", "add", "-q", str(path), "-b", f"b-{len(list(path.parent.iterdir()))}-{abs(hash(name))}")
        return path

    def config(self, tidy, version="0.1.0"):
        write(self.main / "VERSION", version)
        write(self.main / ".ce-repo.json", json.dumps({"name": "repo", "version": {"file": "VERSION", "pattern": "(.+)"},
                                                      "tidy": tidy}))

    def collect(self, categories=ce_tidy.CATEGORIES, cwds=None):
        rows, errors, ctx = ce_tidy.collect([self.main], categories, self.projects, cwds=cwds or {}, sizes=False)
        return {Path(r["path"]): r for r in rows}, errors, ctx

    def row(self, path, **kw):
        rows, errors, _ = self.collect(**kw)
        self.assertIn(path, rows, (rows, errors))
        return rows[path]

    def session(self, path, mtime):
        file = write(self.projects / ce_worktrees.project_dir_name(str(path)) / "s.jsonl", "{}\n")
        os.utime(file, (mtime, mtime))


class WorktreeTests(TidyCase):
    def test_merged_clean_worktree_is_removed_with_apply(self):
        path = self.worktree("done")
        item = self.row(path)
        self.assertTrue(item["deletable"], item)
        rows, _, ctx = self.collect()
        self.assertEqual(ce_tidy.apply(list(rows.values()), ctx), [])
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), git(self.main, "worktree", "list"))

    def test_dirty_worktree_is_protected(self):
        path = self.worktree("dirty")
        write(path / "new.txt")
        self.assertFalse(self.row(path)["deletable"])

    def test_unmerged_commit_is_protected(self):
        path = self.worktree("ahead")
        write(path / "f.txt")
        git(path, "add", ".")
        git(path, "commit", "-qm", "work")
        item = self.row(path)
        self.assertFalse(item["deletable"])
        self.assertIn("nicht in main", item["reason"])

    def test_active_session_protects_but_old_session_does_not(self):
        young, old = self.worktree("young"), self.worktree("old")
        self.session(young, time.time() - 3600)
        self.session(old, OLD)
        rows, _, _ = self.collect()
        self.assertFalse(rows[young]["deletable"])
        self.assertTrue(rows[old]["deletable"], rows[old])

    def test_process_in_worktree_or_subfolder_protects(self):
        top, sub, free = self.worktree("top"), self.worktree("sub"), self.worktree("free")
        (sub / "deep").mkdir()
        rows, _, _ = self.collect(cwds={str(top): ["claude"], str(sub / "deep"): ["codex"]})
        self.assertFalse(rows[top]["deletable"])
        self.assertFalse(rows[sub]["deletable"])
        self.assertIn("Prozess im Ordner", rows[sub]["reason"])
        self.assertTrue(rows[free]["deletable"])

    def test_main_checkout_is_never_listed_or_removed(self):
        rows, _, ctx = self.collect()
        self.assertNotIn(self.main, rows)
        self.assertEqual(ce_tidy.protected(self.main, ctx), "Haupt-Checkout")
        self.assertEqual(ce_tidy.protected(self.main.parent, ctx), "Haupt-Checkout")
        self.assertEqual(ce_tidy.protected(self.main / "Library", ctx), "Library eines Haupt-Checkouts")
        self.assertIsNone(ce_tidy.protected(self.main / ".claude" / "worktrees" / "x" / "Library", ctx))

    def test_outside_worktree_roots_is_protected_until_configured(self):
        path = self.worktree("elsewhere", self.home / "Library" / "Caches" / "nightly")
        self.assertIn("außerhalb der Worktree-Wurzeln", self.row(path)["reason"])
        self.config({"worktree_roots": ["~/Library/Caches/nightly"]})
        self.assertTrue(self.row(path)["deletable"])

    def test_do_not_delete_marker_in_name_or_inside(self):
        named, marked, plain = self.worktree("tmp-NICHT LÖSCHEN"), self.worktree("marked"), self.worktree("plain")
        write(marked / "NICHT_LOESCHEN.txt")
        git(marked, "add", ".")
        git(marked, "commit", "-qm", "m")
        git(self.main, "merge", "-q", "--ff-only", git(marked, "branch", "--show-current"))
        rows, _, _ = self.collect()
        self.assertFalse(rows[named]["deletable"])
        self.assertFalse(rows[marked]["deletable"])
        self.assertIn("NICHT LÖSCHEN", rows[marked]["reason"])
        self.assertTrue(rows[plain]["deletable"])

    def test_decomposed_umlaut_marker_counts(self):
        self.assertTrue(ce_tidy.has_marker(Path("/x/NICHT LO\u0308SCHEN")))

    def test_rescue_folder_is_protected(self):
        rescue = self.home / "Claude" / "_rettung"
        path = self.worktree("w", rescue)
        self.config({"worktree_roots": [str(rescue)]})
        item = self.row(path)
        self.assertFalse(item["deletable"])
        self.assertIn("_rettung", item["reason"])

    def test_worktree_that_became_dirty_before_apply_stays(self):
        path = self.worktree("race")
        rows, _, ctx = self.collect()
        self.assertTrue(rows[path]["deletable"])
        write(path / "late.txt")
        errors = ce_tidy.apply([rows[path]], ctx)
        self.assertTrue(path.exists())
        self.assertIn("nicht mehr sauber", errors[0])

    def test_apply_rechecks_every_protection(self):
        cases = {"process": lambda p: {"cwds": {str(p / "sub"): ["claude"]}},
                 "session": lambda p: self.session(p, time.time() - 60),
                 "marker": lambda p: [(self.main / ".git" / "info" / "exclude").write_text("*.keep\n"), write(p / "NICHT LÖSCHEN.keep")],
                 "hold": lambda p: {"hold": "Abbau"}}
        for name, change in cases.items():
            path = self.worktree(f"late-{name}")
            (path / "sub").mkdir()
            rows, _, ctx = self.collect()
            self.assertTrue(rows[path]["deletable"], (name, rows[path]))
            extra = change(path) or {}
            extra = extra if isinstance(extra, dict) else {}
            with mock.patch.object(ce_tidy.ce_common, "repo_hold", return_value=extra.get("hold")):
                errors = ce_tidy.apply([rows[path]], ctx, cwds=extra.get("cwds", {}))
            self.assertTrue(path.exists(), name)
            self.assertTrue(errors and "inzwischen" in errors[0] or "geschützt" in errors[0], (name, errors))

    def test_missing_folder_is_pruned(self):
        path = self.worktree("gone")
        subprocess.run(["mv", str(path), str(self.home / ".Trash" / "gone")], check=True)
        rows, _, ctx = self.collect()
        self.assertEqual(rows[path]["kind"], "prune")
        ce_tidy.apply([rows[path]], ctx)
        self.assertNotIn(str(path), git(self.main, "worktree", "list"))

    def test_hold_protects_every_row(self):
        path = self.worktree("held")
        with mock.patch.object(ce_tidy.ce_common, "repo_hold", return_value="Abbau läuft"):
            item = self.row(path)
        self.assertFalse(item["deletable"])
        self.assertIn("Repo gesperrt", item["reason"])


class OutputAndEvidenceTests(TidyCase):
    def outputs(self, names):
        for index, name in enumerate(names):
            folder = self.main / "out" / name
            write(folder / "a.bin")
            os.utime(folder, (OLD - index * 86400, OLD - index * 86400))

    def test_keep_newest_and_age_rule(self):
        self.outputs(["new", "mid", "old"])
        self.config({"paths": [{"name": "gen", "root": "out", "keep": 1, "older_than_days": 1}]})
        rows, _, _ = self.collect()
        self.assertFalse(rows[self.main / "out" / "new"]["deletable"])
        self.assertTrue(rows[self.main / "out" / "mid"]["deletable"])
        self.assertTrue(rows[self.main / "out" / "old"]["deletable"])
        self.config({"paths": [{"name": "gen", "root": "out", "keep": 0, "older_than_days": 30}]})
        rows, _, _ = self.collect()
        self.assertFalse(rows[self.main / "out" / "old"]["deletable"])

    def test_tracked_output_is_kept(self):
        write(self.main / "build" / "ship" / "x.txt")
        write(self.main / "build" / "tmp" / "x.txt")
        git(self.main, "add", "build/ship")
        git(self.main, "commit", "-qm", "ship")
        self.config({"paths": [{"root": "build"}]})
        rows, _, _ = self.collect()
        self.assertIn("im Git verfolgt", rows[self.main / "build" / "ship"]["reason"])
        self.assertTrue(rows[self.main / "build" / "tmp"]["deletable"])

    def test_library_of_main_checkout_is_never_cleared(self):
        write(self.main / "Library" / "Artifacts" / "a")
        self.config({"paths": [{"root": "Library"}]})
        item = self.row(self.main / "Library" / "Artifacts")
        self.assertFalse(item["deletable"])

    def test_root_outside_repo_and_caches_is_an_error_only_for_its_category(self):
        path = self.worktree("ok")
        self.config({"paths": [{"root": str(self.home / "Documents")}]})
        rows, errors, _ = self.collect()
        self.assertTrue(any("weder im Haupt-Checkout" in e for e in errors), errors)
        self.assertTrue(rows[path]["deletable"])

    def test_symlink_is_moved_as_link_and_target_survives(self):
        target = write(self.home / "Documents" / "keep" / "doc.txt").parent
        (self.main / "out").mkdir()
        os.symlink(target, self.main / "out" / "link")
        self.config({"paths": [{"root": "out"}]})
        rows, _, ctx = self.collect()
        self.assertEqual(ce_tidy.apply([rows[self.main / "out" / "link"]], ctx), [])
        self.assertTrue((target / "doc.txt").exists())
        self.assertTrue((self.home / ".Trash" / "link").is_symlink())

    def test_dry_run_changes_nothing_and_apply_uses_trash(self):
        self.outputs(["a"])
        self.config({"paths": [{"root": "out"}]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ce_tidy.main(["--repos", str(self.main), "--projects", str(self.projects), "--no-size", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertFalse(data["applied"])
        self.assertTrue((self.main / "out" / "a").exists())
        with contextlib.redirect_stdout(io.StringIO()):
            ce_tidy.main(["--repos", str(self.main), "--projects", str(self.projects), "--no-size", "--apply"])
        self.assertFalse((self.main / "out" / "a").exists())
        self.assertTrue((self.home / ".Trash" / "a" / "a.bin").exists())

    def test_missing_standard_repos_are_skipped_but_named_ones_are_errors(self):
        """Cloud probe 23.10.2026 (Agent Tools 0.20): without --repos the fallback Mac paths gave exit 1 in the cloud."""
        args = ["--projects", str(self.projects), "--no-size", "--json"]
        with mock.patch.object(ce_worktrees, "default_repos", return_value=[self.main, self.home / "nowhere"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(ce_tidy.main(args), 0)
            self.assertEqual(json.loads(out.getvalue())["errors"], [])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ce_tidy.main(["--repos", str(self.home / "nowhere"), *args]), 1)

    def test_missing_trash_refuses(self):
        self.outputs(["a"])
        self.config({"paths": [{"root": "out"}]})
        os.rmdir(self.home / ".Trash")
        rows, _, ctx = self.collect()
        errors = ce_tidy.apply(list(rows.values()), ctx)
        self.assertTrue((self.main / "out" / "a").exists())
        self.assertIn("Papierkorb fehlt", errors[0])

    def test_other_volume_refuses(self):
        self.outputs(["a"])
        self.config({"paths": [{"root": "out"}]})
        rows, _, ctx = self.collect()
        real_lstat = os.lstat
        with mock.patch.object(ce_tidy.os, "lstat", side_effect=lambda p: mock.Mock(st_dev=-1) if str(p).endswith("/a") else real_lstat(p)):
            errors = ce_tidy.apply([rows[self.main / "out" / "a"]], ctx)
        self.assertIn("anderes Volume", errors[0])
        self.assertTrue((self.main / "out" / "a").exists())

    def store(self, versions):
        store = self.home / "Library" / "Caches" / "Proj" / "videos"
        for version in versions:
            write(store / version / "clip.mp4")
        return store

    def test_evidence_keeps_two_newest_versions(self):
        store = self.store(["0.110", "0.111", "0.112", "0.112.1", "notes"])
        rows, _, _ = self.collect(categories=("beleg",))
        self.assertTrue(rows[store / "0.110"]["deletable"])
        for kept in ("0.111", "0.112", "0.112.1"):
            self.assertFalse(rows[store / kept]["deletable"], kept)
        self.assertNotIn(store / "notes", rows)

    def test_evidence_of_repo_keeps_current_and_previous_repo_version(self):
        store = self.store(["0.108", "0.109", "0.110", "0.111", "0.112"])
        self.config({"evidence": [{"root": "~/Library/Caches/Proj/videos"}]}, version="0.109.0")
        rows, _, _ = self.collect(categories=("beleg",))
        self.assertEqual([p.name for p, r in sorted(rows.items()) if r["deletable"]], ["0.110"])

    def test_evidence_of_held_repo_is_kept_at_collect_and_apply(self):
        store = self.store(["0.1", "0.2", "0.3"])
        self.config({"evidence": [{"root": "~/Library/Caches/Proj/videos"}]}, version="0.3.0")
        with mock.patch.object(ce_tidy.ce_common, "repo_hold", return_value="Abbau"):
            rows, _, _ = self.collect(categories=("beleg",))
        self.assertFalse(rows[store / "0.1"]["deletable"])
        self.assertIn("Repo gesperrt", rows[store / "0.1"]["reason"])
        rows, _, ctx = self.collect(categories=("beleg",))
        self.assertTrue(rows[store / "0.1"]["deletable"])
        with mock.patch.object(ce_tidy.ce_common, "repo_hold", return_value="Abbau"):
            errors = ce_tidy.apply([rows[store / "0.1"]], ctx)
        self.assertTrue((store / "0.1").exists())
        self.assertIn("gesperrt", errors[0])

    def test_store_shared_by_two_repos_keeps_both_versions_and_holds(self):
        store = self.store(["0.1", "0.2", "0.3", "0.4", "0.5", "0.6"])
        other = self.home / "Claude" / "other"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        write(other / "VERSION", "0.2.0")
        write(other / ".ce-repo.json", json.dumps({"name": "other", "version": {"file": "VERSION", "pattern": "(.+)"},
                                                  "tidy": {"evidence": [{"root": "~/Library/Caches/Proj/videos", "nightly": True}]}}))
        git(other, "add", ".")
        git(other, "commit", "-qm", "i")
        self.config({"evidence": [{"root": "~/Library/Caches/Proj/videos"}]}, version="0.4.0")
        rows, _, ctx = ce_tidy.collect([other, self.main], ("beleg",), self.projects, cwds={}, sizes=False)
        rows = {Path(r["path"]): r for r in rows}
        self.assertEqual(sorted(p.name for p, r in rows.items() if r["deletable"]), [])
        self.assertFalse(rows[store / "0.1"]["nightly"])
        rows, _, _ = ce_tidy.collect([self.main, other], ("beleg",), self.projects, cwds={}, sizes=False)
        self.assertFalse(any(r["nightly"] for r in rows))
        write(self.main / "VERSION", "0.6.0")
        write(other / "VERSION", "0.6.0")
        held = lambda root, home=None: "Abbau" if Path(root) == other else None
        with mock.patch.object(ce_tidy.ce_common, "repo_hold", side_effect=held):
            rows2, _, _ = ce_tidy.collect([other, self.main], ("beleg",), self.projects, cwds={}, sizes=False)
        self.assertTrue(rows2 and all(not r["deletable"] for r in rows2))
        rows, _, ctx = ce_tidy.collect([self.main, other], ("beleg",), self.projects, cwds={}, sizes=False)
        target = next(r for r in rows if r["path"].endswith("/0.1"))
        self.assertTrue(target["deletable"])
        with mock.patch.object(ce_tidy.ce_common, "repo_hold", side_effect=held):
            errors = ce_tidy.apply([target], ctx)
        self.assertTrue((store / "0.1").exists())
        self.assertIn("gesperrt", errors[0])

    def test_evidence_store_in_rescue_is_refused(self):
        rescue_store = self.home / "Claude" / "_rettung" / "videos"
        write(rescue_store / "0.1" / "a")
        for version in ("0.2", "0.3"):
            write(rescue_store / version / "a")
        self.config({"evidence": [{"root": str(rescue_store)}]})
        rows, errors, _ = self.collect(categories=("beleg",))
        self.assertFalse(rows)
        self.assertTrue(any("geschützt" in e for e in errors), errors)


class CommandAndNightlyTests(TidyCase):
    def script(self, name, code):
        write(self.main / "tools" / name, code)
        return ["python3", f"tools/{name}"]

    def test_command_probe_in_dry_run_apply_with_apply(self):
        probe = self.script("probe.py", "print('3 Testkopien, 2 löschbar')")
        apply = self.script("apply.py", "open('applied','w').write('1')")
        self.config({"commands": [{"name": "testkopien", "probe": probe, "apply": apply}]})
        rows, _, ctx = self.collect(categories=("befehl",))
        item = next(iter(rows.values()))
        self.assertIn("2 löschbar", item["reason"])
        self.assertFalse((self.main / "applied").exists())
        self.assertEqual(ce_tidy.apply([item], ctx), [])
        self.assertTrue((self.main / "applied").exists())

    def test_failing_command_is_reported(self):
        self.config({"commands": [{"name": "bad", "apply": self.script("bad.py", "raise SystemExit(3)")}]})
        rows, _, ctx = self.collect(categories=("befehl",))
        self.assertIn("Exit 3", ce_tidy.apply(list(rows.values()), ctx)[0])

    def test_only_scripts_of_the_repo_run(self):
        outside = write(self.home / "outside.sh", "touch pwned")
        os.symlink(outside, self.main / "link.sh")
        self.script("ok.sh", "true")
        for argv in (["rm", "-rf", "out"], ["python3", "-c", "print(1)"], ["sh", str(outside)], ["../../outside.sh"],
                     ["sh", "link.sh"], ["python3"], ["tools/missing.py"]):
            self.config({"commands": [{"name": "x", "apply": argv}]})
            rows, errors, _ = self.collect(categories=("befehl",))
            self.assertFalse(rows, argv)
            self.assertTrue(any("kein Skript" in e or "ohne Skript" in e for e in errors), (argv, errors))
        self.config({"commands": [{"name": "x", "apply": ["sh", "tools/ok.sh", "--apply"]}]})
        rows, errors, _ = self.collect(categories=("befehl",))
        self.assertEqual(errors, [])

    def test_negative_keep_or_age_is_a_config_error(self):
        write(self.main / "out" / "a" / "x")
        for entry in ({"root": "out", "keep": -1}, {"root": "out", "older_than_days": -2}, {"root": "out", "keep": "viele"}):
            self.config({"paths": [entry]})
            rows, errors, _ = self.collect(categories=("ausgabe",))
            self.assertFalse(rows, entry)
            self.assertTrue(errors, entry)

    def test_nightly_applies_only_nightly_categories(self):
        path = self.worktree("w")
        write(self.home / "Library" / "Caches" / "Proj" / "videos" / "0.1" / "a")
        self.config({"commands": [{"name": "n", "apply": self.script("n.py", "open('nightly','w')"), "nightly": True}]})
        rows, _, ctx = self.collect()
        ce_tidy.apply(list(rows.values()), ctx, nightly_only=True)
        self.assertTrue((self.main / "nightly").exists())
        self.assertTrue(path.exists())
        self.config({"worktrees": {"nightly": True}})
        rows, _, ctx = self.collect()
        ce_tidy.apply(list(rows.values()), ctx, nightly_only=True)
        self.assertFalse(path.exists())

    def test_ce_nightly_runs_tidy_nightly_or_probe(self):
        lines = []
        args = mock.Mock(dry_run=False, repo=[str(self.main)])
        with mock.patch.object(ce_tidy, "run", return_value=0) as run:
            ce_nightly.tidy(args, [self.main], lines.append)
        self.assertIn("--nightly", run.call_args.args[0])
        args.dry_run = True
        with mock.patch.object(ce_tidy, "run", return_value=0) as run:
            ce_nightly.tidy(args, [self.main], lines.append)
        self.assertNotIn("--nightly", run.call_args.args[0])

    def test_ce_nightly_survives_tidy_crash(self):
        lines = []
        with mock.patch.object(ce_tidy, "run", side_effect=RuntimeError("boom")):
            ce_nightly.tidy(mock.Mock(dry_run=False, repo=None), [self.main], lines.append)
        self.assertTrue(any("boom" in line for line in lines))

    def test_dispatcher_knows_tidy(self):
        self.assertIn("tidy)", (PLUGIN / "tools" / "ce").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
