"""0.32: ce_media.sheet survives an ImageMagick without default font (CI runner: "unable to read font `(null)'")."""
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "plugins" / "ce-shared" / "tools"
sys.path.insert(0, str(TOOLS))
import ce_media


class MediaFontTests(unittest.TestCase):
    def frames(self, root):
        from PIL import Image
        folder = root / "frames"
        folder.mkdir()
        for index in range(3):
            Image.new("RGB", (20, 20), (index * 40, 10, 10)).save(folder / f"{index:02}.png")
        return str(folder)

    def test_font_option_comes_from_the_first_installed_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            font = Path(tmp) / "f.ttf"
            font.write_bytes(b"x")
            with mock.patch.object(ce_media, "FONT_CANDIDATES", ["/nonexistent/a.ttf", str(font), "/nonexistent/b.ttf"]):
                self.assertEqual(ce_media.montage_font(), ["-font", str(font)])
            with mock.patch.object(ce_media, "FONT_CANDIDATES", ["/nonexistent/a.ttf"]):
                self.assertEqual(ce_media.montage_font(), [])

    def test_montage_gets_the_font_and_a_failing_montage_falls_back_to_pillow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, calls = Path(tmp), []
            frames, out = self.frames(root), root / "sheet.png"

            def broken(args, **kwargs):
                calls.append(args)
                raise subprocess.CalledProcessError(1, args, stderr=b"montage: unable to read font `(null)'")
            with mock.patch.object(ce_media, "montage_font", return_value=["-font", "/x/f.ttf"]), \
                    mock.patch.object(ce_media.shutil, "which", return_value="/usr/bin/montage"), \
                    mock.patch.object(ce_media.subprocess, "run", side_effect=broken), \
                    contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ce_media.sheet([frames], out, 3, 300, True), 0)
            self.assertEqual(len(calls), 1)
            self.assertIn("-font", calls[0])
            self.assertIn("Pillow-Weg", err.getvalue())
            self.assertTrue(out.is_file())            # the sheet exists although ImageMagick failed

    def test_a_working_montage_does_not_run_pillow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames, out = self.frames(root), root / "sheet.png"
            with mock.patch.object(ce_media.shutil, "which", return_value="/usr/bin/montage"), \
                    mock.patch.object(ce_media.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                out.write_bytes(b"png")
                self.assertEqual(ce_media.sheet([frames], out, 3, 300, False), 0)
            run.assert_called_once()
            self.assertEqual(out.read_bytes(), b"png")


if __name__ == "__main__":
    unittest.main()
