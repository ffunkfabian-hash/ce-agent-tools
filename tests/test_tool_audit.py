"""Regressions for approval, search coverage, and evidence sizing."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import shutil
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "plugins/ce-shared/tools"
sys.path.insert(0, str(TOOLS))
import ce_judge
import ce_media
import ce_search


class ToolAuditTests(unittest.TestCase):
    def test_pillow_contact_sheet_composites_transparency_on_the_sheet_background(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / 'alpha.png', Path(tmp) / 'sheet.png'
            with Image.new('RGBA', (16, 16), (255, 0, 0, 0)) as icon:
                for x in range(4, 12):
                    for y in range(4, 12): icon.putpixel((x, y), (0, 255, 0, 128))
                icon.save(source)
            with mock.patch.object(ce_media.shutil, 'which', return_value=None):
                self.assertEqual(ce_media.sheet([source], output, 1, 20, False), 0)
            with Image.open(output) as sheet:
                self.assertEqual(sheet.getpixel((2, 2)), (32, 32, 32))
                self.assertEqual(sheet.getpixel((10, 10)), (16, 144, 16))

    def test_judge_stderr_only_cannot_approve(self):
        names = ["a.py"]
        def git(*args, **kwargs):
            return "abc" if args[0] == "rev-parse" else ""
        for stdout, stderr, expected in [("", "Freigabe", 1), ("Freigabe", "warning", 0)]:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ce_judge, "git", side_effect=git), \
                 mock.patch.object(ce_judge, "diff_of", return_value=("+changed", names)), \
                 mock.patch.object(ce_judge.shutil, "which", return_value="codex"), \
                 mock.patch.object(ce_judge.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout, stderr)), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ce_judge.main(["--repo", tmp, "--base", "main"]), expected)

    def test_invalid_index_root_fails_without_refreshing_existing_index(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CE_SEARCH_EMBED": "0"}):
            root = Path(tmp) / "valid"
            root.mkdir()
            (root / "README.md").write_text("stable indexed content", encoding="utf-8")
            index = Path(tmp) / "search.sqlite"
            ce_search.build([root], index, log=lambda *_: None)
            db = sqlite3.connect(index)
            db.execute("UPDATE meta SET value='preserve-this-built' WHERE key='built'")
            db.commit(); db.close()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = ce_search.main(["index", "--repo", str(Path(tmp) / "missing"), "--index", str(index)])
            self.assertNotEqual(result, 0)
            self.assertEqual(ce_search.status(index)["built"], "preserve-this-built")
            self.assertTrue(ce_search.search("stable indexed", index=index))

    def test_csharp_searchable_in_cli_and_mcp_without_breaking_fts(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CE_SEARCH_EMBED": "0"}):
            root = Path(tmp) / "repo"
            root.mkdir()
            (root / "Sample.cs").write_text("// C# language uses Roslyn compiler\n", encoding="utf-8")
            index = Path(tmp) / "search.sqlite"
            ce_search.build([root], index, log=lambda *_: None)
            self.assertTrue(ce_search.search("Roslyn", index=index))
            with mock.patch.dict(os.environ, {"CE_SEARCH_INDEX": str(index)}):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(ce_search.main(["query", "C#"]), 0)
                self.assertIn("Sample.cs", out.getvalue())
                request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "ce_search", "arguments": {"query": "C#"}}}
                out = io.StringIO()
                with mock.patch.object(ce_search.sys, "stdin", io.StringIO(json.dumps(request) + "\n")), \
                     contextlib.redirect_stdout(out):
                    self.assertEqual(ce_search.serve(), 0)
                payload = json.loads(out.getvalue())
                self.assertIn("Sample.cs", payload["result"]["content"][0]["text"])

    def test_contact_sheet_exports_fit_width_with_imagemagick_and_pillow(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames = root / "frames"
            frames.mkdir()
            for index in range(7):
                Image.new("RGB", (24 + index, 18), (index * 20, 40, 80)).save(frames / f"{index:02}.png")

            magick = shutil.which("magick") or shutil.which("montage")
            self.assertIsNotNone(magick, "ImageMagick is required for the export-path regression")
            for label, width in [("default", 1280), ("small-valid", 180)]:
                out = root / f"{label}-magick.png"
                with mock.patch.object(ce_media.shutil, "which", side_effect=shutil.which):
                    self.assertEqual(ce_media.sheet([str(frames)], out, 6, width, False), 0)
                with Image.open(out) as sheet:
                    self.assertLessEqual(sheet.width, width, f"ImageMagick {label} output is too wide")

            for label, width in [("default", 1280), ("small-valid", 180)]:
                out = root / f"{label}-pillow.png"
                with mock.patch.object(ce_media.shutil, "which", return_value=None):
                    self.assertEqual(ce_media.sheet([str(frames)], out, 6, width, False), 0)
                with Image.open(out) as sheet:
                    self.assertLessEqual(sheet.width, width, f"Pillow {label} output is too wide")

            invalid = root / "invalid.png"
            with mock.patch.object(ce_media.shutil, "which", return_value=None), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(ce_media.sheet([str(frames)], invalid, 6, 20, False), 2)
            self.assertFalse(invalid.exists())


if __name__ == "__main__":
    unittest.main()
