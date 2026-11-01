"""0.32: ce_codex_quota (summary, route) and the judge's own way choice by quota."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "plugins" / "ce-shared" / "tools"
sys.path.insert(0, str(TOOLS))
import ce_codex_quota
import ce_judge


def rate_limits(primary=None, secondary=None, plan="prolite"):
    def window(used, minutes):
        return {"used_percent": used, "window_minutes": minutes, "resets_at": int(time.time()) + 3600}
    out = {"plan_type": plan}
    if primary is not None:
        out["primary"] = window(*primary)
    if secondary is not None:
        out["secondary"] = window(*secondary)
    return out


class QuotaTests(unittest.TestCase):
    def summary(self, **kwargs):
        return ce_codex_quota.summary(("2026-11-01T10:00:00Z", rate_limits(**kwargs), "rollout.jsonl"))

    def test_import_has_no_side_effect_and_names_windows_by_minutes(self):
        out = self.summary(primary=(40, 300), secondary=(10, 10080))
        self.assertEqual(out["5h"]["left"], 60.0)
        self.assertEqual(out["weekly"]["left"], 90.0)
        self.assertEqual(self.summary(primary=(99, 10080))["weekly"]["left"], 1.0)

    def test_route_codex_with_enough_quota(self):
        way, reason = ce_codex_quota.route(self.summary(primary=(40, 300), secondary=(10, 10080)))
        self.assertEqual(way, "codex")
        self.assertIn("60.0", reason)          # the lowest window decides
        self.assertEqual(ce_codex_quota.route(self.summary(primary=(85, 10080)))[0], "codex")   # 15 % left exactly: still Codex

    def test_route_api_below_threshold_or_at_limit(self):
        way, reason = ce_codex_quota.route(self.summary(primary=(99, 10080)))
        self.assertEqual((way, "unter 15" in reason), ("api", True))
        # one low window is enough even if the other is full
        self.assertEqual(ce_codex_quota.route(self.summary(primary=(92, 300), secondary=(5, 10080)))[0], "api")
        way, reason = ce_codex_quota.route(self.summary(primary=(100, 10080)))
        self.assertEqual((way, "Limit" in reason), ("api", True))
        self.assertEqual(ce_codex_quota.route(self.summary(primary=(99, 10080)), threshold=0.5)[0], "codex")

    def test_window_already_reset_is_not_a_reason_for_the_api(self):
        """Sightline's judge, finding 1: an old low figure must not pin every review to --api."""
        old = ce_codex_quota.summary(("t", {"plan_type": "p", "primary": {"used_percent": 99, "window_minutes": 10080,
                                                                           "resets_at": int(time.time()) - 3600}}, "p"))
        self.assertEqual(old["weekly"]["left"], 1.0)
        way, reason = ce_codex_quota.route(old)
        self.assertEqual(way, "codex")
        self.assertIn("veraltet", reason)
        self.assertEqual(ce_codex_quota.route(old, now=time.time() - 7200)[0], "api")     # before the reset it still counts
        # the other window decides if only one has passed
        mixed = {"as_of": "t", "plan": "p", "5h": {"used": 99, "left": 1.0, "resets": "x", "resets_at": time.time() - 60},
                 "weekly": {"used": 90, "left": 10.0, "resets": "y", "resets_at": time.time() + 60}}
        self.assertEqual(ce_codex_quota.route(mixed)[0], "api")
        mixed["weekly"]["left"] = 60.0
        self.assertEqual(ce_codex_quota.route(mixed)[0], "codex")

    def test_unknown_or_broken_quota_keeps_codex(self):
        self.assertEqual(ce_codex_quota.route({"as_of": "x", "plan": None})[0], "codex")
        with mock.patch.object(ce_codex_quota, "newest", return_value=None):
            self.assertEqual(ce_codex_quota.route()[0], "codex")
        with mock.patch.object(ce_codex_quota, "newest", side_effect=OSError("kaputt")):
            self.assertEqual(ce_codex_quota.route()[0], "codex")

    def test_newest_reads_the_latest_rollout_entry(self):
        with tempfile.TemporaryDirectory() as home:
            day = Path(home, ".codex", "sessions", "2026", "11", "01")
            day.mkdir(parents=True)
            lines = [json.dumps({"timestamp": "2026-11-01T09:00:00Z", "payload": {"rate_limits": rate_limits(primary=(10, 10080))}}),
                     json.dumps({"timestamp": "2026-11-01T10:00:00Z", "payload": {"rate_limits": rate_limits(primary=(97, 10080))}}),
                     "not json"]
            (day / "rollout-a.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"HOME": home, "USERPROFILE": home}):
                out = ce_codex_quota.summary()
            self.assertEqual(out["weekly"]["left"], 3.0)
            self.assertEqual(ce_codex_quota.route(out)[0], "api")

    def test_cli_still_prints_and_exits(self):
        output = io.StringIO()
        with mock.patch.object(ce_codex_quota, "newest", return_value=("t", rate_limits(primary=(30, 300)), "p")), \
                mock.patch.object(sys, "argv", ["codex_quota.py", "--json"]), contextlib.redirect_stdout(output):
            self.assertEqual(ce_codex_quota.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["5h"]["left"], 70.0)
        with mock.patch.object(ce_codex_quota, "newest", return_value=None), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ce_codex_quota.main(), 1)


def args(**kwargs):
    base = dict(api=False, opus=False, model=None, codex=False)
    base.update(kwargs)
    return types.SimpleNamespace(**base)


class PickWayTests(unittest.TestCase):
    def test_explicit_flags_win_over_the_quota(self):
        low = lambda: ("api", "Rest 1 %")
        self.assertEqual(ce_judge.pick_way(args(api=True), low)[0], "api")
        for flags in ({"opus": True}, {"model": "sol"}, {"codex": True}):
            with self.subTest(flags=flags):
                self.assertEqual(ce_judge.pick_way(args(**flags), low)[0], "codex")

    def test_quota_decides_without_flags(self):
        self.assertEqual(ce_judge.pick_way(args(), lambda: ("api", "Rest 1 %")), ("api", "Rest 1 %"))
        self.assertEqual(ce_judge.pick_way(args(), lambda: ("codex", "Rest 60 %"))[0], "codex")

    def test_broken_quota_reader_is_fail_open(self):
        def broken():
            raise RuntimeError("boom")
        way, reason = ce_judge.pick_way(args(), broken)
        self.assertEqual(way, "codex")
        self.assertIn("RuntimeError", reason)
        self.assertEqual(ce_judge.pick_way(args(), lambda: ("quatsch", "?"))[0], "codex")


if __name__ == "__main__":
    unittest.main()
