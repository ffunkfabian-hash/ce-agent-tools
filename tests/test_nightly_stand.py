"""0.42.2: a green nightly on a stand that is not origin/main is red (stand). Real git repos, the real run_one."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "ce-shared" / "tools"))
import ce_nightly  # noqa: E402

SCRIPT = "scripts/nightly.sh"
FLAT = ["sh", SCRIPT, "run"]
# writes into the report what Cube Escape's nightly.sh writes: the sha and, in brackets, the ref it tested
# (ce_check keeps the script's own output, so the report file is the evidence, as in the real nightly)
STAND_SHA = '#!/bin/sh\necho "Stand: \\`$SHA start\\` ($REF)" > "$CACHE/report_20261107_0300.md"\n'
NO_STAND = "#!/bin/sh\necho hallo\n"


def git(root, *argv):
    return subprocess.run(["git", *argv], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


@unittest.skipIf(os.name == "nt", "the fixture scripts are POSIX shell")
class StandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.lines = []
        self.cache = self.base / "cache"
        self.cache.mkdir()

    def repo(self, script):
        origin = self.base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        root = self.base / "repo"
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        git(root, "config", "user.name", "t")
        git(root, "config", "user.email", "t@example.invalid")
        (root / "scripts").mkdir()
        (root / SCRIPT).write_text(script, encoding="utf-8")
        (root / ".ce-repo.json").write_text(json.dumps({"name": "t", "check": {"quick": ["true"], "nightly": FLAT}}), encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "-qm", "start")
        git(root, "remote", "add", "origin", str(origin))
        git(root, "push", "-q", "origin", "main")
        git(root, "fetch", "-q", "origin")
        return root

    def run_one(self, root, **env):
        env.setdefault("CACHE", str(self.cache))
        with mock.patch.dict("os.environ", env), mock.patch.object(ce_nightly, "LOG_DIR", self.cache):
            return ce_nightly.run_one(root, self.lines.append, False)[0]

    def test_green_on_origin_main_stays_green(self):
        root = self.repo(STAND_SHA)
        self.assertEqual(self.run_one(root, SHA=git(root, "rev-parse", "--short", "origin/main"), REF="origin/main"), "green")

    def test_green_on_old_local_main_is_red(self):
        """The 05./06.11. case: the script tested an older local main and said so only without the bracket."""
        root = self.repo(STAND_SHA)
        old = git(root, "rev-parse", "--short", "HEAD")
        (root / "b.txt").write_text("b\n", encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "-qm", "neu")
        git(root, "push", "-q", "origin", "main")
        self.assertEqual(self.run_one(root, SHA=old, REF="origin/main"), "red (stand)")
        self.assertEqual(self.run_one(root, SHA=old, REF="main"), "red (stand)")
        self.assertTrue(any(line.startswith("STAND:") for line in self.lines))

    def test_ref_main_is_red_even_with_the_same_sha(self):
        root = self.repo(STAND_SHA)
        self.assertEqual(self.run_one(root, SHA=git(root, "rev-parse", "--short", "HEAD"), REF="main"), "red (stand)")

    def test_wrong_sha_in_report_is_red(self):
        root = self.repo(STAND_SHA)
        sha = git(root, "rev-parse", "--short", "origin/main")
        self.assertEqual(self.run_one(root, SHA=sha, REF="origin/main"), "green")
        self.assertEqual(self.run_one(root, SHA="deadbee", REF="origin/main"), "red (stand)")

    def test_missing_evidence_is_red(self):
        root = self.repo('#!/bin/sh\n: Stand:\necho hallo\n')
        self.assertEqual(self.run_one(root), "red (stand)")

    def test_repo_without_stand_report_is_not_judged(self):
        root = self.repo(NO_STAND)
        self.assertEqual(self.run_one(root), "green")
        self.assertFalse(any(line.startswith("STAND:") for line in self.lines))

    def test_failed_run_keeps_its_own_red(self):
        root = self.repo('#!/bin/sh\necho "Stand: x"\nexit 3\n')
        self.assertEqual(self.run_one(root), "red (exit 1)")  # ce_check reports its own exit 1, not the script's 3


if __name__ == "__main__":
    unittest.main()
