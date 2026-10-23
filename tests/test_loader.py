"""Tests of the loader ce_loader.py (0.20 "Lodestar"): CE_TOOLS, plugin, cache, download, offline - and ce_sync/ce_lint
for repos that name tools_version instead of keeping an embedded copy.
Run: python3 -m unittest discover -s tests"""
import contextlib
import io
import json
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "ce-shared"
sys.path.insert(0, str(PLUGIN / "tools"))
import ce_lint  # noqa: E402
import ce_loader  # noqa: E402
import ce_sync  # noqa: E402

COMMIT = "a" * 40
OTHER = "b" * 40
FAKE_CE = '#!/bin/sh\necho "fake ce $* in $(pwd)"\n'


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def plugin_files(version, marker="v1"):
    """The files of a fake plugins/ce-shared at `version`."""
    return {".claude-plugin/plugin.json": json.dumps({"name": "ce-shared", "version": version}),
            "tools/ce": FAKE_CE, "tools/ce_check.py": f"# {marker}\n", "contracts/x.json": "{}\n"}


def make_tarball(path, commit, files, comment=None, extra=None):
    """A GitHub-like tarball: top folder ce-agent-tools-<commit>, pax comment = commit (or `comment`)."""
    top = f"ce-agent-tools-{commit}"
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": commit if comment is None else comment}) as tar:
        def add(name, data, mode=0o644):
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
        add("README.md", b"repo\n")
        for name, text in files.items():
            add(f"plugins/ce-shared/{name}", text.encode(), 0o755 if name.endswith("/ce") else 0o644)
        for info, data in extra or []:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


class LoaderCase(unittest.TestCase):
    """A repo with tools_version, an empty cache, no installed plugin, a tarball of COMMIT at 0.20.0 as file URL."""

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name).resolve()
        self.addCleanup(lambda: ce_loader.remove_tree(self.base / "cache"))
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.pin("0.20.0", COMMIT)
        self.tarball = make_tarball(self.base / "tools.tar.gz", COMMIT, plugin_files("0.20.0"))
        self.env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.base),
                    "CE_TOOLS_CACHE": str(self.base / "cache"), "CLAUDE_CONFIG_DIR": str(self.base / "claude"),
                    "CE_TOOLS_URL": "file://" + str(self.base) + "/{commit}.tar.gz",
                    "CE_TOOLS_GIT": str(self.base / "no-such-repo")}
        shutil.copy(self.tarball, self.base / f"{COMMIT}.tar.gz")

    def pin(self, version, commit, **more):
        self.config = {"name": "demo", "tools_version": {"version": version, "commit": commit}, **more}
        (self.repo / ".ce-repo.json").write_text(json.dumps(self.config), encoding="utf-8")

    def resolve(self, **env):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            tools, source = ce_loader.resolve(self.repo, self.config, dict(self.env, **env))
        return tools, source, err.getvalue()

    def offline(self):
        (self.base / f"{COMMIT}.tar.gz").unlink(missing_ok=True)

    def install_plugin(self, version, commit):
        folder = self.base / "claude" / "plugins" / "cache" / "ce-shared" / version
        for name, text in plugin_files(version, "plugin").items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(text, encoding="utf-8")
        entry = {"scope": "user", "installPath": str(folder), "version": version, "gitCommitSha": commit}
        (self.base / "claude" / "plugins" / "installed_plugins.json").write_text(
            json.dumps({"version": 2, "plugins": {ce_loader.PLUGIN_KEY: [entry]}}), encoding="utf-8")
        return folder / "tools"


class Resolution(LoaderCase):
    def test_empty_cache_downloads_verifies_and_reuses(self):
        tools, source, err = self.resolve()
        self.assertEqual(source, "download")
        self.assertEqual(tools, self.base / "cache" / f"0.20.0-{COMMIT[:12]}" / "tools")
        self.assertIn("geladen nach", err)
        self.assertFalse((tools.parent / "README.md").exists(), "only plugins/ce-shared is cached")
        self.assertEqual((tools / "ce_check.py").read_text(), "# v1\n")
        self.assertFalse(os.access(tools / "ce_check.py", os.W_OK), "cache files are read-only")
        self.assertTrue(os.access(tools / "ce", os.X_OK))
        self.offline()
        self.assertEqual(self.resolve()[:2], (tools, "cache"))

    def test_ce_tools_comes_first_and_names_another_version(self):
        self.install_plugin("0.20.0", COMMIT)
        other = self.base / "checkout" / "tools"
        for name, text in plugin_files("0.21.0").items():
            (other.parent / name).parent.mkdir(parents=True, exist_ok=True)
            (other.parent / name).write_text(text)
        tools, source, err = self.resolve(CE_TOOLS=str(other))
        self.assertEqual((tools, source), (other, "CE_TOOLS"))
        self.assertIn("CE_TOOLS hat ce-agent-tools 0.21.0", err)
        with self.assertRaises(ce_loader.LoaderError):
            self.resolve(CE_TOOLS=str(self.base / "nothing"))

    def test_matching_plugin_is_used_without_cache_or_network(self):
        plugin = self.install_plugin("0.20.0", COMMIT)
        self.offline()
        self.assertEqual(self.resolve()[:2], (plugin, "plugin"))
        self.assertFalse((self.base / "cache").exists())

    def test_plugin_with_other_commit_or_version_is_not_used(self):
        self.install_plugin("0.20.0", OTHER)
        self.assertEqual(self.resolve()[1], "download")
        self.install_plugin("0.19.0", COMMIT)
        self.assertEqual(self.resolve()[1], "cache")

    def test_manipulated_cache_is_fetched_again(self):
        tools = self.resolve()[0]
        for change in ("edit", "add", "remove"):
            ce_loader.make_writable(tools.parent)
            target = tools / ("json.py" if change == "add" else "ce_check.py")
            if change == "remove":
                target.unlink()
            else:
                target.write_text("print('planted')\n")
            again, source, err = self.resolve()
            self.assertEqual(source, "download", change)
            self.assertIn("verändert", err)
            self.assertEqual((again / "ce_check.py").read_text(), "# v1\n")
            self.assertFalse((again / "json.py").exists())
        self.assertEqual(self.resolve()[1], "cache")

    def test_planted_bytecode_counts_as_a_change_and_python_writes_none(self):
        tools = self.resolve()[0]
        imported = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(tools)!r}); import ce_check"],
                                  capture_output=True, text=True, env=dict(self.env, PYTHONDONTWRITEBYTECODE=""))
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertFalse((tools / "__pycache__").exists(), "read-only cache: Python writes no bytecode")
        self.assertEqual(self.resolve()[1], "cache")
        ce_loader.make_writable(tools.parent)
        (tools / "__pycache__").mkdir()
        (tools / "__pycache__" / "ce_check.cpython-312.pyc").write_bytes(b"planted")
        again, source, err = self.resolve()
        self.assertEqual(source, "download")
        self.assertIn("__pycache__/ce_check.cpython-312.pyc unbekannt", err)
        self.assertFalse((again / "__pycache__").exists())

    def test_broken_manifest_is_fetched_again(self):
        tools = self.resolve()[0]
        manifest = tools.parent / ce_loader.MANIFEST
        good = manifest.read_text()
        for broken in ('{"version": "0.20.0", "commit": "%s", "files": null}' % COMMIT, '[]', '{"files": {"a": 1}}', 'x'):
            ce_loader.make_writable(tools.parent)
            manifest.write_text(broken)
            again, source, err = self.resolve()
            self.assertEqual(source, "download", broken)
            self.assertIn("Manifest", err)
            self.assertEqual(manifest.read_text(), good)

    def test_manipulated_cache_offline_stops_and_is_not_used(self):
        tools = self.resolve()[0]
        ce_loader.make_writable(tools.parent)
        (tools / "ce_check.py").write_text("print('planted')\n")
        self.offline()
        with self.assertRaises(ce_loader.LoaderError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, ce_loader.EXIT_UNAVAILABLE)
        self.assertIn("kein gültiger Cache", str(caught.exception))

    def test_offline_with_empty_cache_names_the_remedy(self):
        self.offline()
        with self.assertRaises(ce_loader.LoaderError) as caught:
            self.resolve()
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, 3)
        for part in ("0.20.0", COMMIT[:12], "Download:", "git:", "Abhilfe", "CE_TOOLS="):
            self.assertIn(part, message)
        self.assertFalse((self.base / "cache").exists() and any((self.base / "cache").iterdir()), "no half-filled cache")

    def test_archive_of_another_commit_is_rejected(self):
        make_tarball(self.base / f"{COMMIT}.tar.gz", COMMIT, plugin_files("0.20.0"), comment=OTHER)
        with self.assertRaises(ce_loader.LoaderError) as caught:
            self.resolve()
        self.assertIn("nicht zu", str(caught.exception))

    def test_unsafe_archive_members_are_rejected(self):
        link = tarfile.TarInfo(f"ce-agent-tools-{COMMIT}/plugins/ce-shared/tools/evil")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        dots = tarfile.TarInfo(f"ce-agent-tools-{COMMIT}/plugins/ce-shared/../../x.py")
        dots.size = 0
        for extra in ([(link, None)], [(dots, b"")]):
            make_tarball(self.base / f"{COMMIT}.tar.gz", COMMIT, plugin_files("0.20.0"), extra=extra)
            with self.assertRaises(ce_loader.LoaderError):
                self.resolve()

    def test_version_mismatch_and_bad_pin_are_config_errors(self):
        self.pin("0.19.0", COMMIT)
        with self.assertRaises(ce_loader.LoaderError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, ce_loader.EXIT_CONFIG)
        self.assertIn("enthält ce-shared 0.20.0", str(caught.exception))
        for value in ("0.20.0", {"version": "0.20.0", "commit": "v0.20.0"}, {"version": "x", "commit": COMMIT}, {}):
            self.config = {"tools_version": value}
            with self.assertRaises(ce_loader.LoaderError) as caught:
                self.resolve()
            self.assertEqual(caught.exception.code, ce_loader.EXIT_CONFIG, value)

    def test_git_fetch_when_the_download_fails(self):
        source = self.base / "upstream"
        source.mkdir()
        git(source, "init", "-q", "-b", "main")
        for name, text in plugin_files("0.20.0", "git").items():
            (source / "plugins" / "ce-shared" / name).parent.mkdir(parents=True, exist_ok=True)
            (source / "plugins" / "ce-shared" / name).write_text(text)
        git(source, "add", "-A")
        git(source, "commit", "-q", "-m", "tools")
        git(source, "config", "uploadpack.allowAnySHA1InWant", "true")
        commit = git(source, "rev-parse", "HEAD")
        self.pin("0.20.0", commit)
        tools, source_name, _ = self.resolve(CE_TOOLS_GIT=str(source))
        self.assertEqual(source_name, "download")
        self.assertEqual((tools / "ce_check.py").read_text(), "# git\n")
        self.pin("0.20.0", OTHER)
        with self.assertRaises(ce_loader.LoaderError):
            self.resolve(CE_TOOLS_GIT=str(source))

    def test_missing_python_certificates_fall_back_to_the_system_bundle(self):
        calls = []

        def fake(url, context):
            calls.append(context)
            if context is None:
                raise urllib.error.URLError(ssl.SSLCertVerificationError("unable to get local issuer certificate"))
            return b"data"
        bundle = self.base / "cert.pem"
        bundle.write_text("x")
        with mock.patch.object(ce_loader, "_read_url", fake), mock.patch.object(ce_loader, "CA_BUNDLES", (str(bundle),)), \
                mock.patch.object(ce_loader.ssl, "create_default_context", return_value="ctx") as ctx:
            self.assertEqual(ce_loader.download(COMMIT, {}), b"data")
        self.assertEqual(calls, [None, "ctx"])
        ctx.assert_called_once_with(cafile=str(bundle))

    def test_repo_with_embedded_copy_still_runs_it(self):
        self.config = {"tools_dir": "tools/ce_tools"}
        self.assertEqual(self.resolve()[:2], (self.repo / "tools" / "ce_tools", "tools_dir"))


class Entry(LoaderCase):
    """The loader as the repo's ./ce, run as a process."""

    def setUp(self):
        super().setUp()
        shutil.copy(PLUGIN / "tools" / "ce_loader.py", self.repo / "ce")

    def run_ce(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(self.repo / "ce"), *args], cwd=cwd or self.base,
                              capture_output=True, text=True, env=self.env)

    def test_runs_the_pinned_dispatcher_in_the_repo_root(self):
        done = self.run_ce("check", "--full")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), f"fake ce check --full in {self.repo}")
        self.assertIn("geladen nach", done.stderr)
        again = self.run_ce("search", "serve")
        self.assertEqual(again.stderr, "", "a cache hit says nothing (stdout may be an MCP channel)")

    def test_python_tools_leave_the_cache_intact_even_when_writable(self):
        """Cloud probe 23.10.2026: as root the read-only cache did not stop Python, each call wrote __pycache__ and the
        next call fetched again. Writable cache = root; two runs importing modules, the second must be a cache hit."""
        files = dict(plugin_files("0.20.0"), **{"tools/ce": '#!/bin/sh\nexec python3 "$(dirname "$0")/ce_check.py" "$@"\n',
                                                "tools/ce_check.py": "import ce_helper\nprint('ran', ce_helper.X)\n",
                                                "tools/ce_helper.py": "X = 1\n"})
        make_tarball(self.base / f"{COMMIT}.tar.gz", COMMIT, files)
        first = self.run_ce("check")
        self.assertEqual(first.stdout.strip(), "ran 1", first.stderr)
        tools = self.base / "cache" / f"0.20.0-{COMMIT[:12]}" / "tools"
        ce_loader.make_writable(tools.parent)
        self.offline()
        self.env["PYTHONPYCACHEPREFIX"] = str(self.base / "inherited")  # the tools get the loader's prefix instead
        for _ in range(2):
            again = self.run_ce("check")
            self.assertEqual(again.stdout.strip(), "ran 1", again.stderr)
            self.assertNotIn("verändert", again.stderr)
        self.assertFalse((tools / "__pycache__").exists())
        self.assertTrue(list((self.base / "cache" / ".pycache").rglob("ce_helper*.pyc")), "bytecode lands in the prefix")
        self.assertFalse(list((self.base / "inherited").rglob("ce_helper*.pyc")))
        self.assertIn("(cache,", self.run_ce("tools", "where").stdout)

    def test_tools_where_fetch_and_offline_exit_code(self):
        fetch = self.run_ce("tools", "fetch")
        self.assertEqual(fetch.returncode, 0, fetch.stderr)
        self.assertIn("(download, ce-agent-tools 0.20.0)", fetch.stdout)
        self.assertIn("(cache, ce-agent-tools 0.20.0)", self.run_ce("tools", "where").stdout)
        self.offline()
        self.pin("0.20.0", OTHER)
        done = self.run_ce("check")
        self.assertEqual(done.returncode, 3)
        self.assertEqual(done.stdout, "")
        self.assertIn("Abhilfe", done.stderr)

    def real_tarball(self):
        """The real plugins/ce-shared of this checkout as the archive of COMMIT; (files, version)."""
        real = {path.relative_to(PLUGIN).as_posix(): path.read_text() for path in sorted(PLUGIN.rglob("*"))
                if path.is_file() and "__pycache__" not in path.parts}
        make_tarball(self.base / f"{COMMIT}.tar.gz", COMMIT, real)
        return real, ce_sync.plugin_version(PLUGIN / "tools")

    def test_fetch_fills_the_cache_even_when_the_plugin_matches(self):
        self.install_plugin("0.20.0", COMMIT)
        self.assertIn("(plugin,", self.run_ce("tools", "where").stdout)
        fetch = self.run_ce("tools", "fetch")
        self.assertEqual(fetch.returncode, 0, fetch.stderr)
        self.assertIn(f"cache/0.20.0-{COMMIT[:12]}/tools (download,", fetch.stdout)
        self.assertIn("(cache,", self.run_ce("tools", "fetch").stdout)

    def test_migration_from_an_embedded_copy(self):
        """The documented path: ./ce tools pin in a repo that still has its copy, ./ce sync, then ./ce runs the pin."""
        (self.repo / "ce").unlink()
        old = {"name": "demo", "tools_dir": "scripts/ce_tools"}
        (self.repo / ".ce-repo.json").write_text(json.dumps(old))
        ce_sync.sync(self.repo, old, ce_sync.source_dir())
        real, version = self.real_tarball()
        copy = ["sh", str(self.repo / "scripts" / "ce_tools" / "ce")]
        pinned = subprocess.run([*copy, "tools", "pin", COMMIT], cwd=self.repo, capture_output=True, text=True, env=self.env)
        self.assertEqual(pinned.returncode, 0, pinned.stdout + pinned.stderr)
        config = json.loads((self.repo / ".ce-repo.json").read_text())
        self.assertEqual(config, {"name": "demo", "tools_version": {"version": version, "commit": COMMIT}})
        self.assertEqual(ce_sync.unembed((self.repo / "ce").read_text())[2], real["tools/ce_loader.py"])
        self.offline()
        done = self.run_ce("tools", "where")
        self.assertIn(f"(cache, ce-agent-tools {version})", done.stdout, done.stderr)
        findings = ce_sync.check(self.repo, config, None)
        self.assertEqual([level for level, _ in findings], ["warning"], "the old copy stays until git rm")

    def test_pin_writes_tools_version(self):
        self.pin("0.19.0", OTHER, tools_dir="tools/ce_tools")
        refused = self.run_ce("tools", "pin", COMMIT)
        self.assertEqual(refused.returncode, 2, "a commit without loader (before 0.20) is refused")
        self.assertIn("hat keinen Lader", refused.stderr)
        self.assertEqual(json.loads((self.repo / ".ce-repo.json").read_text()), self.config)
        ce_loader.remove_tree(self.base / "cache")
        _, version = self.real_tarball()
        done = self.run_ce("tools", "pin", COMMIT)
        self.assertEqual(done.returncode, 0, done.stderr)
        config = json.loads((self.repo / ".ce-repo.json").read_text())
        self.assertEqual(config["tools_version"], {"version": version, "commit": COMMIT})
        self.assertNotIn("tools_dir", config)
        self.assertEqual(self.run_ce("tools", "pin", "v0.20.0").returncode, 2)


class SyncWithoutCopy(LoaderCase):
    """ce_sync/ce_lint for a repo with tools_version: only ./ce, checked like a copy."""

    def check(self, source=None):
        return ce_sync.check(self.repo, self.config, source)

    def test_sync_writes_only_the_loader_and_check_accepts_it(self):
        source = ce_sync.source_dir()
        self.assertEqual(ce_sync.sync(self.repo, self.config, source), ["ce"])
        self.assertEqual(sorted(p.name for p in self.repo.iterdir()), [".ce-repo.json", "ce"])
        version, _, original = ce_sync.unembed((self.repo / "ce").read_text())
        self.assertEqual(original, (source / "ce_loader.py").read_text())
        self.assertTrue(os.access(self.repo / "ce", os.X_OK))
        self.assertEqual(ce_sync.sync(self.repo, self.config, source), [])
        self.pin(ce_sync.plugin_version(source), COMMIT)
        self.assertEqual(self.check(source), [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ce_lint.check_embedded(self.repo, self.config) == [], True)
            self.assertEqual(ce_sync.main(["--repo", str(self.repo), "--check"]), 0)
        self.assertIn("loader ok", out.getvalue())

    def test_missing_or_edited_loader_and_bad_pin_are_errors(self):
        self.assertEqual([level for level, _ in self.check()], ["error"])
        ce_sync.sync(self.repo, self.config, ce_sync.source_dir())
        (self.repo / "ce").write_text((self.repo / "ce").read_text() + "# tweak\n")
        self.assertIn("edited by hand", self.check()[0][1])
        ce_sync.sync(self.repo, self.config, ce_sync.source_dir())
        self.config = {"tools_version": {"version": "0.20.0", "commit": "main"}}
        self.assertEqual([level for level, _ in self.check()], ["error"])

    def test_older_loader_other_version_and_left_over_copy_are_warnings(self):
        ce_sync.sync(self.repo, self.config, ce_sync.source_dir())
        newer = self.base / "newer" / "tools"
        newer.mkdir(parents=True)
        (newer / "ce_loader.py").write_text((ce_sync.source_dir() / "ce_loader.py").read_text() + "# newer\n")
        (newer.parent / ".claude-plugin").mkdir()
        (newer.parent / ".claude-plugin" / "plugin.json").write_text('{"version": "9.0.0"}')
        (self.repo / "tools" / "ce_tools").mkdir(parents=True)
        (self.repo / "tools" / "ce_tools" / "ce_check.py").write_text("old copy\n")
        (self.repo / ".claude" / "agents").mkdir(parents=True)
        (self.repo / ".claude" / "agents" / "ce-judge.md").symlink_to("../../tools/ce_tools/agents/ce-judge.md")
        self.pin("0.20.0", COMMIT, tools_dir="tools/ce_tools")
        findings = self.check(newer)
        self.assertEqual({level for level, _ in findings}, {"warning"}, findings)
        text = " ".join(t for _, t in findings)
        for part in ("loader from ce-agent-tools", "plugin has 9.0.0", "tools_dir is ignored", "left over",
                     "link into a removed copy", "ce-shared:ce-judge"):
            self.assertIn(part, text)
        self.assertTrue((self.repo / "tools" / "ce_tools" / "ce_check.py").exists(), "sync/check never delete")



class CiStep(LoaderCase):
    """.github/actions/ce-tools: the run script of the composite action, executed with a local upstream."""
    ACTION = PLUGIN.parent.parent / ".github" / "actions" / "ce-tools"

    def script(self):
        lines = (self.ACTION / "action.yml").read_text().split("\n")
        start = next(i for i, line in enumerate(lines) if line.strip() == "run: |") + 1
        return "\n".join(line[8:] for line in lines[start:])

    def upstream(self, version):
        source = self.base / "upstream"
        source.mkdir(exist_ok=True)
        if not (source / ".git").exists():
            git(source, "init", "-q", "-b", "main")
            git(source, "config", "uploadpack.allowAnySHA1InWant", "true")
        for name, text in plugin_files(version, "ci").items():
            (source / "plugins" / "ce-shared" / name).parent.mkdir(parents=True, exist_ok=True)
            (source / "plugins" / "ce-shared" / name).write_text(text)
        git(source, "add", "-A")
        git(source, "commit", "-q", "-m", version)
        return source, git(source, "rev-parse", "HEAD")

    def run_step(self, source):
        temp = self.base / "runner"
        temp.mkdir(exist_ok=True)
        env = dict(self.env, GITHUB_ACTION_PATH=str(self.ACTION), RUNNER_TEMP=str(temp), CE_PIN_REPO=str(self.repo),
                   GITHUB_ENV=str(temp / "env"), GITHUB_OUTPUT=str(temp / "out"), CE_TOOLS_GIT=str(source),
                   CE_ACTION_REF="")
        return subprocess.run(["bash", "-c", self.script()], capture_output=True, text=True, env=env), temp

    def test_fetches_the_pinned_commit_outside_the_workspace_and_sets_ce_tools(self):
        source, commit = self.upstream("0.20.0")
        self.pin("0.20.0", commit)
        done, temp = self.run_step(source)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        tools = temp / "ce-agent-tools" / "plugins" / "ce-shared" / "tools"
        self.assertEqual((temp / "env").read_text(), f"CE_TOOLS={tools}\n")
        self.assertEqual((tools / "ce_check.py").read_text(), "# ci\n")
        shutil.copy(PLUGIN / "tools" / "ce_loader.py", self.repo / "ce")
        self.offline()
        ran = subprocess.run([sys.executable, str(self.repo / "ce"), "check"], capture_output=True, text=True,
                             env=dict(self.env, CE_TOOLS=str(tools)))
        self.assertEqual(ran.stdout.strip(), f"fake ce check in {self.repo}", ran.stderr)

    def test_wrong_version_or_bad_pin_fails_the_step(self):
        source, commit = self.upstream("0.20.0")
        self.pin("0.19.0", commit)
        done, temp = self.run_step(source)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("hat Version 0.20.0", done.stdout)
        self.assertFalse((temp / "env").exists())
        self.pin("0.20.0", "main")
        done, _ = self.run_step(source)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("::error::", done.stderr)


if __name__ == "__main__":
    unittest.main()
