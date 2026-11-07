"""./ce verify (0.37 Waybill): a real git repository as the remote, real command runs in the throwaway copy; only GitHub's REST
answers are fake. merge-check reads what this writes."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "ce-shared"
sys.path[:0] = [str(PLUGIN / "tools")]
import ce_merge_check as mc  # noqa: E402
import ce_sync  # noqa: E402
import ce_verify as cv  # noqa: E402

REPO = "o/r"
GREEN = ["sh", "-c", "echo 'Ran 4 tests in 0.1s'; echo 'OK (skipped=1)'"]


def git(folder, *args):
    return subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True, text=True).stdout.strip()


@unittest.skipIf(os.name == "nt", "./ce verify läuft nur am Mac")
class VerifyCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.remote = self.folder / "remote"
        self.remote.mkdir()
        git(self.remote, "init", "-q")
        git(self.remote, "config", "uploadpack.allowAnySHA1InWant", "true")
        self.head = self.commit("a.txt", "eins\n")
        self.quick = GREEN
        self.same_repo = True
        self.say = []
        for patcher in (mock.patch.dict(os.environ, {"CE_VERIFY_CACHE": str(self.folder / "cache")}), mock.patch.object(cv, "REMOTE", self.remote.as_uri()),
                        mock.patch.object(cv, "on_mac", return_value=True), mock.patch.object(mc, "gh", self.gh)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def commit(self, name, text):
        path = self.remote / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git(self.remote, "add", ".")
        git(self.remote, "commit", "-q", "-m", name)
        return git(self.remote, "rev-parse", "HEAD")

    def gh(self, *args):
        target = args[-1]
        if "/pulls/" in target:
            head_repo = REPO if self.same_repo else "stranger/r"
            return json.dumps({"number": 7, "state": "open", "head": {"sha": self.head, "ref": "x", "repo": {"full_name": head_repo}},
                               "base": {"ref": "main", "repo": {"full_name": REPO, "default_branch": "main"}}})
        if ".ce-repo.json" in target and target.endswith("ref=main"):
            return json.dumps({"check": {"quick": self.quick}}) if self.quick is not None else None
        raise AssertionError(target)

    def verify(self, **options):
        return cv.verify(REPO, 7, say=self.say.append, **options)

    def run_main(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cv.main([REPO, "7", *extra])
        return code, json.loads(out.getvalue()), err.getvalue()

    def copies(self):
        return list((self.folder / "cache" / "runs").glob("*"))


class RunTests(VerifyCase):
    def test_a_green_run_writes_the_receipt_for_this_head(self):
        result = self.verify()
        self.assertEqual((result["green"], result["receipt"], result["tests"], result["skipped"]), (True, True, 4, 1))
        written = mc.read_verify(REPO, self.head)
        self.assertEqual((written["green"], written["head"], written["repo"], written["check"], written["tests"], written["skipped"]), (True, self.head, REPO, GREEN, 4, 1))
        self.assertEqual(self.copies(), [], "the copy is gone")
        self.assertEqual(mc.verify_problem(written, REPO, self.head, {"check": {"quick": GREEN}}), "")

    def test_the_run_stands_on_the_head_and_not_on_a_checkout(self):
        self.quick = ["sh", "-c", 'pwd > "$0"; git rev-parse HEAD >> "$0"; cat a.txt >> "$0"; ls -a >> "$0"'.replace('"$0"', str(self.folder / "where.txt"))]
        self.commit("later.txt", "nach dem Head\n")
        self.assertTrue(self.verify()["green"])
        lines = (self.folder / "where.txt").read_text(encoding="utf-8").splitlines()
        self.assertTrue(Path(lines[0]).resolve().is_relative_to((self.folder / "cache" / "runs").resolve()), lines[0])
        self.assertEqual(lines[1:3], [self.head, "eins"])
        self.assertNotIn("later.txt", lines)

    def test_a_red_run_writes_none_and_removes_an_older_one(self):
        self.assertTrue(self.verify()["green"])
        self.quick = ["sh", "-c", "exit 3"]
        code, result, err = self.run_main()
        self.assertEqual((code, result["green"], result["receipt"], result["commands"][0]["code"]), (1, False, False, 3))
        self.assertIsNone(mc.read_verify(REPO, self.head))
        self.assertIn("rot, keine Quittung", err)
        self.assertEqual(self.copies(), [])

    def test_all_commands_count(self):
        self.quick = [GREEN, ["sh", "-c", "exit 1"], GREEN]
        result = self.verify()
        self.assertEqual((result["green"], len(result["commands"]), result["tests"]), (False, 3, 8))

    def test_a_command_that_cannot_start_is_red(self):
        self.quick = [".venv/bin/python", "check"]
        result = self.verify()
        self.assertEqual((result["green"], result["commands"][0]["code"]), (False, 127))

    def test_the_check_of_the_base_runs_not_the_one_of_the_pull_request(self):
        """A pull request cannot swap its own check: this head brings a config that would pass, the base's check fails."""
        self.head = self.commit(".ce-repo.json", json.dumps({"check": {"quick": ["true"]}}))
        self.quick = ["sh", "-c", "exit 1"]
        self.assertFalse(self.verify()["green"])

    def test_out_of_time_is_not_checkable_and_nothing_of_the_group_survives(self):
        mark = self.folder / "alive"
        self.quick = ["sh", "-c", f"(sleep 2; touch {mark}) & sleep 30"]
        with self.assertRaisesRegex(mc.NotCheckable, "Zeitlimit 0 min"):
            self.verify(minutes=0)
        self.assertIsNone(mc.read_verify(REPO, self.head))
        self.assertEqual(self.copies(), [])
        time.sleep(2.5)
        self.assertFalse(mark.exists(), "the grandchild went with the group")

    def test_no_test_at_all_is_not_checkable(self):
        self.quick = ["sh", "-c", "echo 'Ran 0 tests in 0.000s'"]
        code, result, err = self.run_main()
        self.assertEqual((code, result["receipt"]), (2, False))
        self.assertIn("nicht prüfbar, keine Quittung", err)
        self.assertIsNone(mc.read_verify(REPO, self.head))

    def test_only_skips_are_no_test_either(self):
        """Richter 0.37, Nr 4.1: a missing neighbour can end as nothing but skips."""
        self.quick = ["sh", "-c", "echo 'Ran 5 tests in 0.1s'; echo 'OK (skipped=5)'"]
        code, result, err = self.run_main()
        self.assertEqual((code, result["receipt"]), (2, False))
        self.assertIn("5 Tests, 5 übersprungen", err)
        self.assertIsNone(mc.read_verify(REPO, self.head))

    def test_each_command_counts_for_itself(self):
        """0.43 Errata (Befund 0.37): only skips in one command are not paid for by the tests of another."""
        self.quick = [GREEN, ["sh", "-c", "echo 'Ran 3 tests in 0.1s'; echo 'OK (skipped=3)'"]]
        code, result, err = self.run_main()
        self.assertEqual((code, result["receipt"]), (2, False))
        self.assertIn("lief keinen einzigen Test (3 Tests, 3 übersprungen)", err)
        self.assertIsNone(mc.read_verify(REPO, self.head))

    def test_the_counts_of_each_command_stay_in_the_result(self):
        self.quick = [GREEN, ["true"], ["sh", "-c", "echo '3 passed, 1 skipped, 2 warnings in 0.12s'"]]
        result = self.verify()
        self.assertEqual((result["green"], result["tests"], result["skipped"]), (True, 8, 2))
        self.assertEqual([(item["tests"], item["skipped"]) for item in result["commands"]], [(4, 1), (None, 0), (4, 1)])

    def test_a_pytest_run_of_nothing_but_skips_is_not_checkable(self):
        """0.43 Errata (Befund 0.37): the skip check reads pytest too."""
        for line in ("======= 2 skipped in 0.05s =======", "no tests ran in 0.01s", "4 deselected in 0.02s"):
            self.quick = ["sh", "-c", f"echo '{line}'"]
            code, result, err = self.run_main()
            self.assertEqual((code, result["receipt"]), (2, False), line)
            self.assertIn("lief keinen einzigen Test", err, line)

    def test_a_term_from_outside_clears_copy_and_group(self):
        """Richter 0.37, Nr 4.8: an agent's time limit hits the tool, not the tests."""
        mark, started = self.folder / "alive", self.folder / "started"
        self.quick = ["sh", "-c", f"touch {started}; (sleep 2; touch {mark}) & sleep 30"]
        driver = self.folder / "driver.py"
        driver.write_text(f"""import json, sys
from unittest import mock
sys.path.insert(0, {str(PLUGIN / "tools")!r})
import ce_merge_check as mc, ce_verify as cv
pr = {{"number": 7, "state": "open", "head": {{"sha": {self.head!r}, "ref": "x", "repo": {{"full_name": "o/r"}}}}, "base": {{"ref": "main", "repo": {{"full_name": "o/r"}}}}}}
gh = lambda *args: json.dumps(pr if "/pulls/" in args[-1] else {{"check": {{"quick": {self.quick!r}}}}})
with mock.patch.object(mc, "gh", gh), mock.patch.object(cv, "REMOTE", {self.remote.as_uri()!r}), mock.patch.object(cv, "on_mac", return_value=True):
    sys.exit(cv.main(["o/r", "7"]))
""", encoding="utf-8")
        tool = subprocess.Popen([sys.executable, str(driver)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if started.exists():
                break
            time.sleep(0.1)
        self.assertTrue(started.exists())
        tool.terminate()
        self.assertEqual(tool.wait(10), 143)
        self.assertEqual(self.copies(), [])
        self.assertIsNone(mc.read_verify(REPO, self.head))
        time.sleep(2.5)
        self.assertFalse(mark.exists())

    def test_a_check_without_unittest_output_still_counts(self):
        self.quick = ["true"]
        result = self.verify()
        self.assertEqual((result["green"], result["tests"]), (True, None))
        self.assertIn("keine Testzahl", cv.text_line(result))

    def test_the_callers_environment_stays_out(self):
        self.quick = ["sh", "-c", f'env > {self.folder / "env.txt"}']
        with mock.patch.dict(os.environ, {"GIT_DIR": "/nowhere", "GIT_WORK_TREE": "/nowhere", "PYTHONPATH": "/x", "VIRTUAL_ENV": "/y", "KEEP_ME": "1"}):
            self.assertTrue(self.verify()["green"])
        text = (self.folder / "env.txt").read_text(encoding="utf-8")
        for name in ("GIT_DIR", "GIT_WORK_TREE", "PYTHONPATH", "VIRTUAL_ENV"):
            self.assertNotIn(name + "=", text)
        self.assertIn("KEEP_ME=1", text)
        self.assertIn("CE_VERIFY=1", text, "the sign for a check bound to the checkout")

    def test_a_callers_venv_comes_off_the_path(self):
        self.quick = ["sh", "-c", f'echo "$PATH" > {self.folder / "path.txt"}']
        with mock.patch.dict(os.environ, {"VIRTUAL_ENV": "/fremd", "PATH": f"/fremd/bin{os.pathsep}{os.environ['PATH']}"}):
            self.assertTrue(self.verify()["green"])
        self.assertNotIn("/fremd/bin", (self.folder / "path.txt").read_text(encoding="utf-8"))

    def test_the_log_keeps_what_the_command_said(self):
        self.quick = ["sh", "-c", "echo Fehlerbild; exit 1"]
        code, result, err = self.run_main()
        self.assertIn("Fehlerbild", Path(result["log"]).read_text(encoding="utf-8"))
        self.assertIn("Fehlerbild", err)


class NotCheckableTests(VerifyCase):
    def assert_refused(self, text, *, code=2):
        got, result, err = self.run_main()
        self.assertEqual((got, result["receipt"], result["green"]), (code, False, False))
        self.assertIn(text, err)
        self.assertIsNone(mc.read_verify(REPO, self.head))

    def test_only_on_the_mac(self):
        with mock.patch.object(cv, "on_mac", return_value=False):
            self.assert_refused("nur am Mac")

    def test_code_from_a_fork_does_not_run_here(self):
        self.same_repo = False
        mark = self.folder / "ran"
        self.quick = ["touch", str(mark)]
        self.assert_refused("fremder Code")
        self.assertFalse(mark.exists())

    def test_a_base_without_check_quick(self):
        self.quick = None
        self.assert_refused("check.quick der Basis main unbrauchbar")

    def test_a_config_with_a_broken_check_is_not_checkable(self):
        real = self.gh
        with mock.patch.object(mc, "gh", lambda *args: json.dumps({"check": ["x"]}) if ".ce-repo.json" in args[-1] else real(*args)):
            self.assert_refused("check.quick der Basis main unbrauchbar")

    def test_a_head_that_cannot_be_fetched(self):
        self.head = "d" * 40
        self.assert_refused("nicht geladen")
        self.assertEqual(self.copies(), [])

    def test_github_does_not_answer(self):
        def broken(*args):
            raise mc.NotCheckable("gh läuft nicht")
        with mock.patch.object(mc, "gh", broken):
            self.assert_refused("gh läuft nicht")

    def test_a_refused_run_leaves_no_older_receipt_of_that_head(self):
        self.assertTrue(self.verify()["green"])
        self.quick = None
        self.assert_refused("unbrauchbar")


class VenvTests(VerifyCase):
    def test_a_repository_with_requirements_gets_a_shared_clean_venv(self):
        self.head = self.commit("requirements-lock.txt", "# nichts\n")
        self.commit("other", "x")
        self.quick = [[".venv/bin/python", "-c", "import sys; print('Ran 1 test in 0s'); print(sys.prefix)"], [".venv/bin/pip", "--version"]]  # scripts work too
        first = self.verify()
        self.assertEqual((first["green"], first["venv"]), (True, "requirements-lock.txt"), Path(first["log"]).read_text(encoding="utf-8"))
        venvs = [item for item in (self.folder / "cache" / "venvs").glob("*") if item.is_dir()]
        self.assertEqual(len(venvs), 1)
        with mock.patch.object(cv, "step", wraps=cv.step) as step:
            self.assertTrue(self.verify()["green"])
        self.assertFalse(any("venv" in call.args[0] for call in step.call_args_list), "built once")

    def test_the_lock_file_wins_and_a_failed_install_is_not_checkable(self):
        self.commit("requirements.txt", "gibt-es-nicht==1\n")
        self.head = self.commit("requirements-lock.txt", "--no-index\ngibt-es-nicht==1\n")
        with self.assertRaisesRegex(mc.NotCheckable, "pip install -r requirements-lock.txt"):
            self.verify()
        self.assertEqual([item for item in (self.folder / "cache" / "venvs").glob("*") if item.is_dir()], [], "no half environment stays")
        self.assertIsNone(mc.read_verify(REPO, self.head))

    def test_a_half_built_environment_is_built_again(self):
        self.head = self.commit("requirements.txt", "# nichts\n")
        self.quick = [".venv/bin/python", "-c", "print('Ran 1 test in 0s')"]
        self.assertTrue(self.verify()["green"])
        ready = next(item for item in (self.folder / "cache" / "venvs").glob("*") if item.is_dir())
        (ready / cv.VENV_DONE).unlink()
        (ready / "bin" / "python").unlink()
        self.assertTrue(self.verify()["green"])

    def test_pip_runs_in_the_copy_and_not_where_the_caller_stands(self):
        """Richter 0.37, Nr 4.6: `-e .` in a requirements file must not reach a checkout."""
        self.head = self.commit("requirements.txt", "# nichts\n")
        self.quick = ["true"]
        with mock.patch.object(cv, "step", wraps=cv.step) as step:
            self.assertTrue(self.verify()["green"])
        pip = next(call for call in step.call_args_list if "pip" in map(str, call.args[0]))
        self.assertEqual(Path(pip.kwargs["cwd"]).parent.name, "runs")

    def venvs(self):
        return [item for item in (self.folder / "cache" / "venvs").glob("*") if item.is_dir()]

    def test_the_key_covers_the_files_pulled_in_with_r(self):
        """0.43 Errata (Befund 0.37): a change in an included file is a new environment."""
        self.commit("requirements.txt", "-r base.txt\n")
        self.head = self.commit("base.txt", "# eins\n")
        self.quick = ["true"]
        self.assertTrue(self.verify()["green"])
        self.head = self.commit("base.txt", "# zwei\n")
        self.assertTrue(self.verify()["green"])
        self.assertEqual(len(self.venvs()), 2)

    def test_a_local_package_gets_an_environment_of_its_own_inside_the_copy(self):
        """0.43 Errata (Befund 0.37): `-e .` in a shared environment leaves a link to a copy that is gone after the run."""
        real = cv.step

        def fake(argv, seconds, what, env=None, cwd=None):  # no network: pip would fetch setuptools for `-e .`
            if what != "venv" and not what.startswith("pip "):
                return real(argv, seconds, what, env, cwd)
            if what == "venv":
                (Path(argv[-1]) / "bin").mkdir(parents=True)
            calls.append((what, [str(part) for part in argv], cwd))
            return ""
        for files in ({"requirements.txt": "-e .\n"}, {"requirements.txt": "-r dev.txt\n", "dev.txt": "  --editable=.  # local\n"}):
            calls = []
            for name, text in files.items():
                self.head = self.commit(name, text)
            self.quick = ["sh", "-c", "test -d .venv/bin && ! test -L .venv"]
            with mock.patch.object(cv, "step", fake):
                result = self.verify()
            self.assertEqual((result["green"], result["venv"]), (True, "requirements.txt (eigene Umgebung: lokales Paket)"), files)
            self.assertEqual(self.venvs(), [], "nothing shared")
            venv, pip = calls[0][1][-1], calls[1]
            self.assertEqual(Path(venv).name, ".venv")
            self.assertEqual(Path(venv).parent, Path(pip[2]), "built in the copy")
            self.assertEqual(Path(pip[1][0]).parent.parent, Path(venv))
            self.assertEqual(self.copies(), [])

    def test_without_requirements_no_venv(self):
        self.assertIsNone(self.verify()["venv"])


class SweepTests(VerifyCase):
    def test_old_leftovers_go_and_fresh_ones_stay(self):
        cache = self.folder / "cache"
        old = time.time() - 40 * 86400
        items = {"runs/alt": True, "logs/alt.log": False, "venvs/alt": True, "receipts/o__r/alt.json": False}
        for name, is_dir in items.items():
            path = cache / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.mkdir() if is_dir else path.write_text("x", encoding="utf-8")
            os.utime(path, (old, old))
        fresh = cache / "receipts" / "o__r" / "neu.json"
        fresh.write_text("x", encoding="utf-8")
        cv.sweep()
        self.assertEqual([name for name in items if (cache / name).exists()], [])
        self.assertTrue(fresh.exists())

    def test_a_run_of_today_keeps_its_copy(self):
        run = self.folder / "cache" / "runs" / "laeuft"
        run.mkdir(parents=True)
        cv.sweep()
        self.assertTrue(run.exists())


class ToolTests(unittest.TestCase):
    def test_the_mac_is_darwin(self):
        for system, mac in (("Darwin", True), ("Linux", False), ("Windows", False)):
            with mock.patch.object(cv.platform, "system", return_value=system):
                self.assertEqual(cv.on_mac(), mac, system)

    def test_the_counts_read_unittest_summaries(self):
        self.assertEqual(cv.counts("Ran 535 tests in 480.1s\n\nOK (skipped=7)\n"), (535, 7))
        self.assertEqual(cv.counts("Ran 1 test in 0.1s\n\nFAILED (failures=1, skipped=2)\nRan 3 tests in 1s\nOK\n"), (4, 2))
        self.assertEqual(cv.counts("alles gut\n"), (None, 0))

    def test_the_counts_read_pytest_summaries(self):
        self.assertEqual(cv.counts("===== 3 passed, 1 skipped, 2 warnings in 0.12s =====\n"), (4, 1))
        self.assertEqual(cv.counts("1 failed, 2 passed, 1 error, 1 xfailed in 1.20s\n"), (5, 0))
        self.assertEqual(cv.counts("2 passed, 5 deselected in 0.10s (0:00:00)\n"), (2, 0))
        self.assertEqual(cv.counts("no tests ran in 0.01s\n"), (0, 0))
        self.assertEqual(cv.counts("Ran 2 tests in 0.1s\nOK\n3 skipped in 0.2s\n"), (5, 3))
        for text in ("3 apples in 2s\n", "$ echo '3 passed in 1s'\n", "copied 3 files in 2s\n"):
            self.assertEqual(cv.counts(text), (None, 0), text)

    def test_requirement_files_follow_r_and_c_as_pip_reads_them(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            for name, text in {"requirements.txt": "-r sub/a.txt\n--requirement=b.txt\n-cc.txt\n-r https://x/y.txt\n-r fehlt.txt\n# -r d.txt\nx==1\n",
                               "sub/a.txt": "-r ../requirements.txt\n-r e.txt\n", "sub/e.txt": "y\n", "b.txt": "", "c.txt": "z<2\n", "d.txt": "-e .\n"}.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, encoding="utf-8")
            files = cv.requirement_files(root / "requirements.txt")
            self.assertEqual([path.relative_to(root).as_posix() for path in files], ["requirements.txt", "sub/a.txt", "b.txt", "c.txt", "sub/e.txt"])
            self.assertFalse(cv.installs_itself(files), "a comment installs nothing")
            for line in ("-e .", "--editable .", "./pkg", "/abs/pkg", "~/pkg", "file:///x/pkg", "name @ file:///x/pkg", "-e git+https://h/r#egg=r"):
                (root / "b.txt").write_text(line + "\n", encoding="utf-8")
                self.assertTrue(cv.installs_itself(files), line)
            for line in ("requests==2", "-r b.txt", "--index-url https://x", "name @ https://x/p.whl"):
                (root / "b.txt").write_text(line + "\n", encoding="utf-8")
                self.assertFalse(cv.installs_itself(files), line)

    def test_the_tool_travels_with_the_others(self):
        self.assertIn("ce_verify.py", ce_sync.SYNC_FILES)
        ce = (PLUGIN / "tools" / "ce").read_text(encoding="utf-8")
        self.assertIn('verify)   exec "$py" "$tools/ce_verify.py" "$@";;', ce)

    def test_a_wrong_repository_name_stops_before_anything_runs(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            cv.main(["kein-repo", "7"])


if __name__ == "__main__":
    unittest.main()
