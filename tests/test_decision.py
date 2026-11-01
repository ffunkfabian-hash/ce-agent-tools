"""0.32 (Panorama 8): ./ce session say --frage … --standard … --frist …  - format and display only, never an answer."""
import contextlib
import datetime
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "plugins" / "ce-shared" / "tools"
sys.path.insert(0, str(TOOLS))
import ce_await_mail as am
import ce_postfach as pf

NAME = "TG 0.1 „Probe“ · Cloud"
NOW = datetime.datetime(2026, 11, 1, 10, 0, tzinfo=datetime.timezone.utc)


def say(*args):
    sent, err = [], io.StringIO()
    with mock.patch.object(pf, "send", side_effect=lambda body, repo, dry_run=False, **kw: sent.append(body) or 0), \
            contextlib.redirect_stderr(err):
        code = pf.main(["say", "--name", NAME, "--model", "Sonnet 5.5, medium", "--dry-run", *args])
    return code, "".join(sent), err.getvalue()


class FristTests(unittest.TestCase):
    def test_valid_deadlines(self):
        for text, minutes in (("60m", 60), ("90", 90), ("2h", 120), ("1 min", 1), ("24h", 1440), (" 15M ", 15)):
            with self.subTest(text=text):
                self.assertEqual(am.parse_frist(text), minutes)

    def test_invalid_deadlines(self):
        for text in ("", "0", "0m", "25h", "1441", "abc", "-5m", "1d", "6.5h", None):
            with self.subTest(text=text), self.assertRaises(ValueError):
                am.parse_frist(text)


class FormatTests(unittest.TestCase):
    def test_question_with_default_and_deadline(self):
        code, out, err = say("--frage", "Griff links (A) oder rechts (B)?", "--standard", "A", "--frist", "60m")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"{NAME} → Sightline: Entscheidung nötig: Griff links (A) oder rechts (B)?")
        self.assertEqual(lines[1], "Sonnet 5.5, medium")
        self.assertEqual(lines[2], "Standard: A")
        self.assertTrue(lines[3].startswith("Frist: 60m, bis 20"), lines[3])
        self.assertIn("entscheidet niemand", lines[4])
        self.assertEqual(pf.head_of(out)[2], "Entscheidung nötig: Griff links (A) oder rechts (B)?")

    def test_default_deadline_extra_lines_and_other_recipient(self):
        code, out, err = say("--frage", "Farbe rot oder blau?", "--standard", "nein", "--to", "Motiongen 0.38", "Kontext: Griffvarianten")
        self.assertEqual(code, 0, err)
        self.assertIn("Frist: 60m", out)
        self.assertTrue(out.rstrip().endswith("Kontext: Griffvarianten"))
        self.assertIn("→ Motiongen 0.38: Entscheidung nötig", out)

    def test_incomplete_or_stray_arguments_are_refused(self):
        for args, text in ((("--frage", "A oder B?"), "--standard"),
                           (("--frage", "A oder B?", "--standard", "A", "--frist", "gleich"), "Frist"),
                           (("--frage", "A oder B?", "--standard", "A", "--frist", "48h"), "Frist"),
                           (("--standard", "A", "Thema"), "gehören zu --frage"),
                           (("--frist", "30m", "Thema"), "gehören zu --frage"),
                           ((), "Thema fehlt"),
                           (("--frage", "A oder B?", "--standard", "x" * 200), "--standard")):
            with self.subTest(args=args):
                code, out, err = say(*args)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(text, err)

    def test_question_and_release_exclude_each_other(self):
        code, out, err = say("--frage", "A oder B?", "--standard", "A", "--freigabe", "commit")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("schließen sich aus", err)

    def test_plain_messages_are_unchanged(self):
        code, out, err = say("Fertig", "PR 1")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], f"{NAME} → Sightline: Fertig")
        self.assertNotIn("Standard:", out)


class DisplayTests(unittest.TestCase):
    def body(self, standard="A", frist="60m"):
        lines, _deadline = am.decision_lines(standard, frist, NOW)
        return "\n".join([f"{NAME} → Sightline: Entscheidung nötig: A oder B?", "Modell", *lines])

    def test_note_shows_default_deadline_and_that_nothing_is_answered(self):
        note = am.decision_note(self.body(), NOW + datetime.timedelta(minutes=20))
        self.assertIn("ENTSCHEIDUNGSFRAGE (Standard A, Frist bis 2026-11-01T11:00Z, noch 40 min)", note)
        self.assertIn("auch nicht der Standard", note)
        self.assertIn("Frist abgelaufen", am.decision_note(self.body(), NOW + datetime.timedelta(hours=2)))

    def test_other_messages_get_no_note(self):
        for body in ("", f"{NAME} → Sightline: Fertig\nModell", "kein Kopf", f"{NAME} → Sightline: Frage ohne Marke\nStandard: A",
                     f"{NAME} → Sightline: PR\nEntscheidung nötig: im Text"):
            with self.subTest(body=body):
                self.assertEqual(am.decision_note(body), "")

    def test_watcher_delivery_prints_the_note_before_the_body(self):
        out = io.StringIO()
        am.deliver("o/r", {"id": 7, "created_at": "t", "body": self.body()}, out=out)
        text = out.getvalue()
        self.assertLess(text.index("ENTSCHEIDUNGSFRAGE"), text.index("Standard: A"))
        out = io.StringIO()
        am.deliver("o/r", {"id": 8, "created_at": "t", "body": f"{NAME} → Sightline: Fertig\nM"}, out=out)
        self.assertNotIn("ENTSCHEIDUNGSFRAGE", out.getvalue())


if __name__ == "__main__":
    unittest.main()
