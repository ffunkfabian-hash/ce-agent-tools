"""./ce refs (0.41 Backlink): a small Unity project on disk, real files, a real git worktree for the package cache."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "ce-shared"
sys.path[:0] = [str(PLUGIN / "tools")]
import ce_refs  # noqa: E402
import ce_sync  # noqa: E402

G = {name: (char * 32) for name, char in {"script": "a", "mat": "b", "prefab": "c", "scene": "d", "fbx": "e", "gone": "1", "pkg": "2"}.items()}
YAML = "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!114 &1\n"


def put(project, relative, text, guid=None):
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if guid:
        Path(f"{path}.meta").write_text(f"fileFormatVersion: 2\nguid: {guid}\n", encoding="utf-8")


def make_project(folder):
    project = Path(folder)
    put(project, "Assets/Scripts/Bot.cs", "class Bot {}\n", G["script"])
    put(project, "Assets/Art/Glow.mat", YAML + "m_Name: Glow\n", G["mat"])
    put(project, "Assets/Prefabs/Bot.prefab", YAML + (
        f"  m_Script: {{fileID: 11500000, guid: {G['script']}, type: 3}}\n"
        f"  eyeMaterial: {{fileID: 2100000, guid: {G['mat']}, type: 2}}\n"
        f"  - {{fileID: 2100000, guid: {G['mat']}, type: 2}}\n"
        f"  brokenField: {{fileID: 2100000,\n    guid: {G['gone']}, type: 2}}\n"
        f"  packaged: {{fileID: 1, guid: {G['pkg']}, type: 3}}\n"
        "  builtin: {fileID: 10303, guid: 0000000000000000f000000000000000, type: 0}\n"), G["prefab"])
    put(project, "Assets/Scenes/Main.unity", YAML + f"  m_SourcePrefab: {{fileID: 100100000, guid: {G['prefab']}, type: 3}}\n", G["scene"])
    put(project, "Assets/Art/Bot.fbx", "binary", G["fbx"])
    Path(project / "Assets/Art/Bot.fbx.meta").write_text(
        f"fileFormatVersion: 2\nguid: {G['fbx']}\nexternalObjects:\n  second: {{fileID: 2100000, guid: {G['mat']}, type: 2}}\n", encoding="utf-8")
    put(project, "ProjectSettings/EditorBuildSettings.asset", YAML + f"  - enabled: 1\n    path: Assets/Scenes/Main.unity\n    guid: {G['scene']}\n")
    put(project, "ProjectSettings/Data.asset", YAML + f"  guid: {'9' * 32}\n  my_guid: {'8' * 32}\n")
    put(project, "Assets/Hidden~/Ignored.prefab", YAML + f"  x: {{fileID: 1, guid: {G['script']}, type: 3}}\n")
    return project


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ce_refs.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class RefsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.project = make_project(folder.name)
        self.cache = self.project / "Library" / "PackageCache"
        put(self.cache, "com.x@1/Runtime/Thing.cs", "", G["pkg"])

    def query(self, *argv):
        code, out, err = run(*argv, "--project", str(self.project), "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_who_names_the_field_and_ignores_folders_unity_ignores(self):
        rows = self.query("who", "Bot.cs")["who"]
        self.assertEqual([(r["asset"], r["fields"]) for r in rows], [("Assets/Prefabs/Bot.prefab", ["m_Script"])])

    def test_who_counts_list_entries_and_reads_meta_files(self):
        rows = {r["asset"]: r for r in self.query("who", "Assets/Art/Glow.mat")["who"]}
        self.assertEqual(rows["Assets/Prefabs/Bot.prefab"]["count"], 2)
        self.assertEqual(rows["Assets/Prefabs/Bot.prefab"]["fields"], ["eyeMaterial"])
        self.assertEqual(rows["Assets/Art/Bot.fbx"]["fields"], ["second"])

    def test_depth_follows_prefab_to_scene_to_build_settings(self):
        rows = self.query("who", G["script"], "--depth", "3")["who"]
        self.assertEqual([(r["asset"], r["depth"]) for r in rows], [
            ("Assets/Prefabs/Bot.prefab", 1), ("Assets/Scenes/Main.unity", 2), ("ProjectSettings/EditorBuildSettings.asset", 3)])

    def test_uses_lists_targets_without_builtin_resources(self):
        targets = {r["asset"] for r in self.query("uses", "Assets/Prefabs/Bot.prefab")["uses"]}
        self.assertEqual(targets, {"Assets/Scripts/Bot.cs", "Assets/Art/Glow.mat", G["gone"], G["pkg"]})

    def test_missing_finds_a_wrapped_reference_and_spares_packages_and_bare_strings(self):
        code, out, _ = run("missing", "--project", str(self.project), "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), {"missing": {G["gone"]: ["Assets/Prefabs/Bot.prefab"]}, "undecided": {}})

    def test_without_package_cache_nothing_is_claimed_missing(self):
        cache = self.cache.rename(self.project / "elsewhere")
        code, out, _ = run("missing", "--project", str(self.project), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(out)["undecided"]), sorted([G["gone"], G["pkg"]]))
        code, out, _ = run("missing", "--project", str(self.project), "--package-cache", str(cache), "--json")
        self.assertEqual((code, list(json.loads(out)["missing"])), (1, [G["gone"]]))

    def test_worktree_uses_the_package_cache_of_the_main_checkout(self):
        def git(*args):
            subprocess.run(["git", "-C", str(self.project), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True)
        (self.project / ".gitignore").write_text("Library/\n", encoding="utf-8")
        git("init", "-q")
        git("add", "-A")
        git("commit", "-qm", "x")
        with tempfile.TemporaryDirectory() as parent:
            tree = Path(parent) / "tree"
            git("worktree", "add", "-q", str(tree))
            index = ce_refs.build(tree)
            self.assertEqual(index.package_cache.resolve(), self.cache.resolve())
            self.assertEqual(list(ce_refs.missing(index)[0]), [G["gone"].encode()])
            git("worktree", "remove", "--force", str(tree))

    def test_ambiguous_name_lists_matches_and_unknown_name_fails(self):
        code, _, err = run("who", "Bot", "--project", str(self.project))
        self.assertEqual(code, 2)
        self.assertIn("Assets/Prefabs/Bot.prefab", err)
        self.assertEqual(run("who", "Nothing", "--project", str(self.project))[0], 2)

    def test_binary_asset_is_counted_not_read(self):
        put(self.project, "Assets/Data/Binary.asset", "\x00\x01", "7" * 32)
        self.assertEqual(ce_refs.build(self.project).binary, 1)

    def test_text_output_says_code_is_not_covered(self):
        code, out, _ = run("who", "Bot.cs", "--project", str(self.project))
        self.assertEqual(code, 0)
        self.assertIn("über m_Script", out)
        self.assertIn("nicht erfasst", out)

    def test_outside_a_unity_project_it_stops(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(run("stats", "--project", empty)[0], 2)

    def test_json_like_formats_are_sources(self):
        put(self.project, "Assets/Scripts/Game.asmdef", f'{{"references": ["GUID:{G["script"]}"]}}', "6" * 32)
        put(self.project, "Assets/UI/Main.uxml", f'<Style src="project://database/x.uss?fileID=1&amp;guid={G["script"]}&amp;type=3"/>', "5" * 32)
        self.assertEqual({r["asset"] for r in self.query("who", "Bot.cs")["who"]},
                         {"Assets/Prefabs/Bot.prefab", "Assets/Scripts/Game.asmdef", "Assets/UI/Main.uxml"})

    def test_crlf_files_as_on_windows(self):
        path = self.project / "Assets/Prefabs/Bot.prefab"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        meta = self.project / "Assets/Art/Glow.mat.meta"
        meta.write_bytes(meta.read_bytes().replace(b"\n", b"\r\n"))
        code, out, _ = run("missing", "--project", str(self.project), "--json")
        self.assertEqual((code, list(json.loads(out)["missing"])), (1, [G["gone"]]))
        rows = {r["asset"]: r for r in self.query("uses", "Assets/Prefabs/Bot.prefab")["uses"]}
        self.assertEqual(rows[G["gone"]]["fields"], ["brokenField"])
        self.assertEqual(rows["Assets/Art/Glow.mat"]["fields"], ["eyeMaterial"])

    def test_windows_separators_and_paths_from_the_shell_find_the_asset(self):
        for query in ("Assets\\Art\\Glow.mat", str(self.project / "Assets/Art/Glow.mat")):
            self.assertEqual(self.query("uses", query)["asset"], "Assets/Art/Glow.mat")

    def test_file_name_is_found_from_a_folder_inside_the_project(self):
        before = os.getcwd()
        os.chdir(self.project / "Assets" / "Scenes")
        self.addCleanup(os.chdir, before)
        self.assertEqual(self.query("uses", "Glow.mat")["asset"], "Assets/Art/Glow.mat")

    def test_longer_key_ending_in_guid_is_no_reference(self):
        targets = {r["guid"] for r in self.query("uses", "ProjectSettings/Data.asset")["uses"]}
        self.assertEqual(targets, {"9" * 32})

    @unittest.skipIf(os.name == "nt", "Symlinks brauchen dort Rechte")
    def test_unreadable_file_is_skipped_and_counted(self):
        (self.project / "Assets/Art/Dead.prefab").symlink_to(self.project / "nowhere")
        code, out, err = run("stats", "--project", str(self.project), "--json")
        self.assertEqual((code, json.loads(out)["unreadable"]), (0, 1))
        self.assertIn("Dead.prefab", err)

    def test_script_inside_a_dll_is_undecided_not_gone(self):
        put(self.project, "Assets/Prefabs/Ui.prefab", YAML + f"  m_Script: {{fileID: 1392445389, guid: {'3' * 32}, type: 3}}\n", "4" * 32)
        _, out, _ = run("missing", "--project", str(self.project), "--json")
        result = json.loads(out)
        self.assertEqual((list(result["missing"]), list(result["undecided"])), ([G["gone"]], ["3" * 32]))

    def test_deleted_cs_script_stays_gone(self):
        put(self.project, "Assets/Prefabs/Old.prefab", YAML + f"  m_Script: {{fileID: 11500000, guid: {'3' * 32}, type: 3}}\n", "4" * 32)
        self.assertIn("3" * 32, json.loads(run("missing", "--project", str(self.project), "--json")[1])["missing"])

    def test_local_file_package_counts_as_present(self):
        outside = self.project / "shared" / "com.y"
        put(outside, "Runtime/Thing.cs", "", G["gone"])
        put(self.project, "Packages/manifest.json", json.dumps({"dependencies": {"com.y": "file:../shared/com.y", "com.z": "1.0.0"}}))
        code, out, _ = run("missing", "--project", str(self.project), "--json")
        self.assertEqual((code, json.loads(out)["missing"]), (0, {}))

    def test_other_package_versions_in_the_cache_make_everything_undecided(self):
        put(self.project, "Packages/packages-lock.json", "{}")
        with tempfile.TemporaryDirectory() as other:
            main_checkout = Path(other)
            put(main_checkout, "Packages/packages-lock.json", '{"x": 1}')
            shutil.copytree(self.cache, main_checkout / "Library" / "PackageCache")
            shutil.rmtree(self.cache)
            with mock.patch.object(ce_refs, "package_cache", return_value=main_checkout / "Library" / "PackageCache"):
                index = ce_refs.build(self.project)
            self.assertEqual(ce_refs.missing(index)[0], {})
            self.assertIn(G["gone"].encode(), ce_refs.missing(index)[1])

    def test_registered_in_ce_and_sync(self):
        self.assertIn("ce_refs.py", ce_sync.SYNC_FILES)
        self.assertIn('refs)     exec "$py" "$tools/ce_refs.py" "$@";;', (PLUGIN / "tools" / "ce").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
