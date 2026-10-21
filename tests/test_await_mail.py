import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("await_mail", Path(__file__).resolve().parents[1] / "plugins" / "ce-shared" / "tools" / "ce_await_mail.py")
am = importlib.util.module_from_spec(spec)
spec.loader.exec_module(am)

KEY = am.session_key("TG 0.19 „Kinetic“ · Mac")


def comment(number, body, created="2026-10-21T10:00:00Z"):
    return {"id": number, "created_at": created, "body": body}


class Addressing(unittest.TestCase):
    def test_key_ignores_kuerzel_quotes_and_place(self):
        self.assertEqual(KEY, "0.19 kinetic")
        self.assertEqual(am.session_key('TOOLGEN 0.19 "Kinetic" · Cloud'), KEY)
        self.assertEqual(am.session_key("REF Aufräumen · Mac"), "ref aufräumen")

    def test_messages_to_the_session_count(self):
        self.assertTrue(am.addressed("**Sightline · Cloud → TOOLGEN 0.19 „Kinetic“ · Mac – Freigabe des Nutzers (Chat: „Ja“): mergen.**", KEY))
        self.assertTrue(am.addressed("Erste Zeile\nSightline → TG 0.19 „Kinetic“ (Abnahme): Doku-Nachtrag", KEY))
        self.assertTrue(am.addressed("Sightline → REF Aufräumen: bitte", am.session_key("REF Aufräumen · Mac")))

    def test_messages_from_or_about_the_session_do_not(self):
        self.assertFalse(am.addressed("TOOLGEN 0.19 „Kinetic“ · Mac → Sightline: fertig", KEY))
        self.assertFalse(am.addressed("Sightline → Nutzer: TG 0.19 „Kinetic“ ist fertig", KEY))
        self.assertFalse(am.addressed("→ Sightline – 0.19 „Kinetic“ ist fertig", KEY))
        self.assertFalse(am.addressed("Sightline → TG 0.20 „Lockstep“ · Cloud: Freigabe", KEY))
        self.assertFalse(am.addressed("GEMERGT TG 0.19 „Kinetic“ · Mac – main abc", KEY))


class Watching(unittest.TestCase):
    def run_watch(self, pages, start_id=0, since="2026-10-21T09:00:00Z", timeout=120):
        calls = []

        def fetch(_since):
            calls.append(_since)
            page = pages[min(len(calls), len(pages)) - 1]
            if isinstance(page, Exception):
                raise page
            return page

        clock = iter(range(0, 10_000, 60))
        code, result = am.watch(fetch, KEY, start_id, since, 60, timeout, sleep=lambda _: None, clock=lambda: next(clock))
        return code, result, len(calls)

    def test_wakes_on_the_first_addressed_comment(self):
        pages = [[comment(1, "Sightline → TG 0.20 „Lockstep“: x")],
                 [comment(1, "Sightline → TG 0.20 „Lockstep“: x"), comment(3, "Sightline → TG 0.19 „Kinetic“: Ja"),
                  comment(2, "Sightline → TG 0.19 „Kinetic“: Doku")]]
        code, result, calls = self.run_watch(pages)
        self.assertEqual((code, result["id"], calls), (0, 2, 2))

    def test_handled_and_old_comments_are_skipped(self):
        pages = [[comment(5, "Sightline → TG 0.19 „Kinetic“: alt"),
                  comment(9, "Sightline → TG 0.19 „Kinetic“: bearbeitet", created="2026-10-20T08:00:00Z")]]
        code, result, _ = self.run_watch(pages, start_id=5)
        self.assertEqual((code, result), (2, "no message"))

    def test_times_out_without_message(self):
        code, result, calls = self.run_watch([[]], timeout=300)
        self.assertEqual((code, result), (2, "no message"))
        self.assertGreater(calls, 1)

    def test_single_network_errors_do_not_end_the_watch(self):
        pages = [RuntimeError("offline"), [comment(4, "Sightline → TG 0.19 „Kinetic“: Ja")]]
        code, result, _ = self.run_watch(pages)
        self.assertEqual((code, result["id"]), (0, 4))

    def test_repeated_network_errors_are_reported(self):
        code, result, calls = self.run_watch([RuntimeError("offline")], timeout=10_000)
        self.assertEqual((code, calls), (3, 5))
        self.assertIn("offline", result)


if __name__ == "__main__":
    unittest.main()
