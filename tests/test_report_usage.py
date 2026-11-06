"""Claude token usage is reported from deduplicated, validated transcript counters."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "plugins/ce-shared/tools"
sys.path.insert(0, str(TOOLS))
import ce_report


def record(message_id, usage=None, *, timestamp="2026-10-21T12:00:00Z", model="claude-sonnet", **extra):
    message = {"id": message_id, "model": model, "role": "assistant"}
    if usage is not None:
        message["usage"] = usage
    return {"type": "assistant", "timestamp": timestamp, "message": message, **extra}


class ReportUsageTests(unittest.TestCase):
    def analyse_records(self, records, since=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.jsonl"
            path.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")
            return ce_report.analyse(path, since)

    def test_input_split_and_observed_read_fraction_use_disjoint_anthropic_fields(self):
        info = self.analyse_records([record("a", {"input_tokens": 100, "cache_creation_input_tokens": 40,
                                                      "cache_read_input_tokens": 60, "output_tokens": 12})])
        usage = info["usage"]
        self.assertEqual((usage["fresh_input_tokens"], usage["cache_creation_input_tokens"],
                          usage["cache_read_input_tokens"], usage["total_input_tokens"]), (100, 40, 60, 200))
        self.assertEqual(usage["cache_read_fraction"], .3)
        self.assertEqual((usage["average_input_tokens_per_request"], usage["max_input_tokens_per_request"]), (200, 200))
        line = ce_report.one_line(info)
        for expected in ("Input gesamt 200 Tokens", "frisch 100", "Cache-Schreiben 40", "Cache-Lesen 60", "30.0%", "Messabdeckung 1/1"):
            self.assertIn(expected, line)

    def test_stream_updates_take_componentwise_maximum_once_per_message_id(self):
        info = self.analyse_records([
            record("a", {"input_tokens": 100, "cache_creation_input_tokens": 5}),
            record("a", {"input_tokens": 80, "cache_creation_input_tokens": 8,
                          "cache_read_input_tokens": 50, "output_tokens": 3}),
            record("b", {"input_tokens": 10, "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0, "output_tokens": 2}),
        ])
        usage = info["usage"]
        self.assertEqual(usage["requests"], 2)
        self.assertEqual((usage["fresh_input_tokens"], usage["cache_creation_input_tokens"], usage["cache_read_input_tokens"]), (110, 8, 50))
        self.assertEqual((usage["average_input_tokens_per_request"], usage["max_input_tokens_per_request"]), (84, 158))
        self.assertEqual(usage["output_tokens"], 5)

    def test_partial_updates_complete_fields_but_unavailable_fields_stay_unknown(self):
        info = self.analyse_records([
            record("a", {"input_tokens": 4}),
            record("a", {"cache_creation_input_tokens": 2, "cache_read_input_tokens": 1}),
            record("b", {"input_tokens": 7}),
        ])
        usage = info["usage"]
        self.assertEqual((usage["requests"], usage["input_measured_requests"], usage["input_coverage"]), (2, 1, .5))
        self.assertEqual(usage["total_input_tokens"], 7)
        self.assertIn("Messabdeckung 1/2", ce_report.one_line(info))
        self.assertNotIn("50.0%", ce_report.one_line(info))

    def test_invalid_token_types_and_synthetic_usage_do_not_become_measurements(self):
        info = self.analyse_records([
            record("bad", {"input_tokens": True, "cache_creation_input_tokens": -1,
                            "cache_read_input_tokens": float("nan"), "output_tokens": "12"}),
            record("fake", {"input_tokens": 1000, "cache_creation_input_tokens": 1,
                             "cache_read_input_tokens": 1, "output_tokens": 4}, synthetic=True),
        ])
        usage = info["usage"]
        self.assertEqual(usage["requests"], 1)
        self.assertEqual(usage["input_measured_requests"], 0)
        self.assertIsNone(usage["total_input_tokens"])
        self.assertIsNone(usage["cache_read_fraction"])
        self.assertIsNone(usage["output_tokens"])
        self.assertIn("Usage unvollständig", ce_report.one_line(info))

    def test_synthetic_model_and_nonassistant_records_are_excluded(self):
        info = self.analyse_records([
            record("synthetic-model", {"input_tokens": 100, "cache_creation_input_tokens": 0,
                                        "cache_read_input_tokens": 0, "output_tokens": 1}, model="<synthetic>"),
            {"type": "user", "message": {"id": "foreign", "model": "claude-sonnet", "role": "user",
                                             "usage": {"input_tokens": 100, "cache_creation_input_tokens": 0,
                                                       "cache_read_input_tokens": 0, "output_tokens": 1}}},
            {"type": "assistant", "message": {"id": "missing-role", "model": "claude-sonnet",
                                                  "usage": {"input_tokens": 10, "cache_creation_input_tokens": 0,
                                                            "cache_read_input_tokens": 0, "output_tokens": 1}}},
        ])
        usage = info["usage"]
        self.assertEqual(usage["requests"], 1)
        self.assertEqual(usage["total_input_tokens"], 10)

    def test_absent_usage_and_antigravity_style_records_remain_compatible(self):
        info = self.analyse_records([{"type": "USER_INPUT", "content": "private prompt"},
                                     {"type": "GENERIC", "content": "ok"}])
        self.assertEqual(info["messages"], 1)
        self.assertEqual(info["usage"]["requests"], 0)
        self.assertIsNone(info["usage"]["total_input_tokens"])
        self.assertIn("Modell-Usage nicht verfügbar", ce_report.one_line(info))
        self.assertNotIn("private prompt", ce_report.one_line(info))

    def test_zero_observed_input_does_not_claim_a_zero_percent_cache_quote(self):
        info = self.analyse_records([record("zero", {"input_tokens": 0, "cache_creation_input_tokens": 0,
                                                        "cache_read_input_tokens": 0, "output_tokens": 0})])
        self.assertEqual(info["usage"]["total_input_tokens"], 0)
        self.assertIsNone(info["usage"]["cache_read_fraction"])
        self.assertIn("Cache-Lesequote nicht berechenbar", ce_report.one_line(info))

    def test_since_filter_excludes_older_usage_request(self):
        cutoff = 1_792_581_600  # 2026-10-21T12:00:00Z
        info = self.analyse_records([
            record("old", {"input_tokens": 90, "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0, "output_tokens": 1}, timestamp="2026-10-20T12:00:00Z"),
            record("new", {"input_tokens": 10, "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0, "output_tokens": 1}),
        ], since=cutoff)
        self.assertEqual(info["usage"]["requests"], 1)
        self.assertEqual(info["usage"]["total_input_tokens"], 10)

    def test_hints_flag_high_peak_context_and_repeated_cache_creation_after_ttl_expiry(self):
        high_peak = self.analyse_records([
            record("a", {"input_tokens": 200, "cache_creation_input_tokens": 180_000,
                          "cache_read_input_tokens": 0, "output_tokens": 50}),
        ])
        high_hints = ce_report.hints(high_peak)
        self.assertTrue(any("Spitzen-Input 180,200 Tokens" in h for h in high_hints))
        self.assertFalse(any("Cache-Schreiben" in h for h in high_hints))

        thrashing = self.analyse_records([
            record("a", {"input_tokens": 100, "cache_creation_input_tokens": 80_000,
                          "cache_read_input_tokens": 0, "output_tokens": 20}),
            record("b", {"input_tokens": 100, "cache_creation_input_tokens": 85_000,
                          "cache_read_input_tokens": 0, "output_tokens": 20}),
            record("c", {"input_tokens": 100, "cache_creation_input_tokens": 90_000,
                          "cache_read_input_tokens": 0, "output_tokens": 20}),
        ])
        thrash_hints = ce_report.hints(thrashing)
        self.assertTrue(any("Cache-Schreiben (255,000 Tokens) > 2× Spitzen-Input (90,100)" in h for h in thrash_hints))
        self.assertTrue(any("möglichen wiederholten Cache-Neuaufbau" in h for h in thrash_hints))

    def info(self, messages, agents):
        return {"messages": messages, "agents": agents, "usage": {}, "checks_repeated_same_command": 0,
                "big_doc_reads": 0, "searches": 0, "largest_output_bytes": 0}

    def delegation(self, messages, agents):
        return [h for h in ce_report.hints(self.info(messages, agents)) if "0 Unteragenten" in h]

    def test_hint_many_messages_without_subagents(self):
        """0.32 (Panorama 2): a long session that never delegated gets a hint - not a block, not at 150 itself."""
        [hint] = self.delegation(151, {})
        self.assertIn("über 150 Nachrichten", hint)
        self.assertIn("ce-searcher", hint)
        self.assertEqual(self.delegation(150, {}), [])                  # the threshold is "more than 150"
        self.assertEqual(self.delegation(10, {}), [])
        self.assertEqual(self.delegation(900, {"sonnet": 1}), [])       # one subagent of any kind is enough
        self.assertEqual(self.delegation(900, {"inherit": 2}), [])
        long_text = " ".join(ce_report.hints(self.info(400, {})))
        self.assertIn("über 300 Nachrichten", long_text)               # the older hint stays next to it
        self.assertIn("0 Unteragenten", long_text)

    def test_the_hint_is_not_part_of_the_one_line_summary(self):
        self.assertNotIn("0 Unteragenten:", ce_report.one_line(dict(self.analyse_records([record("a", {"input_tokens": 1})]), messages=200)))



class SessionInputLimitTests(unittest.TestCase):
    """0.36.1: 300,000 input tokens for the session named exactly "Sightline", 150,000 for every other (Entscheidung 295)."""

    def repo(self, tmp, session_file=None):
        import subprocess
        root = Path(tmp)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        if session_file is not None:
            (root / ".git" / "ce-session.json").write_text(session_file, encoding="utf-8")
        return root

    def peak(self, tokens):
        usage = {"input_tokens": tokens, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 1}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text(json.dumps(record("a", usage)) + "\n", encoding="utf-8")
            return ce_report.analyse(path)

    def flagged(self, root, tokens):
        return any("Spitzen-Input" in h for h in ce_report.hints(self.peak(tokens), ce_report.input_limit(root)))

    def test_sightline_below_300000_is_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.repo(tmp, '{"name": "Sightline"}')
            self.assertEqual(ce_report.input_limit(root), 300_000)
            self.assertFalse(self.flagged(root, 250_000))

    def test_sightline_above_300000_is_flagged_with_its_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.repo(tmp, '{"name": "Sightline"}')
            texts = " ".join(ce_report.hints(self.peak(310_000), ce_report.input_limit(root)))
            self.assertIn("Spitzen-Input 310,000 Tokens (> 300,000)", texts)

    def test_homing_another_session_in_the_sightline_checkout_keeps_150000(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.repo(tmp, '{"name": "Sightline", "session_id": "s1"}')
            self.assertEqual(ce_report.input_limit(root, "s1"), 300_000)
            self.assertEqual(ce_report.input_limit(root, "stranger"), 150_000)
            self.assertEqual(ce_report.input_limit(root), 300_000, "no id known: the name decides as before")

    def test_other_session_above_150000_is_flagged(self):
        for name in ("CE-AGENT-TOOLS Patch x · Mac", "Sightline 2", "sightline", ""):
            with tempfile.TemporaryDirectory() as tmp:
                root = self.repo(tmp, json.dumps({"name": name}))
                self.assertEqual(ce_report.input_limit(root), 150_000, name)
                self.assertTrue(self.flagged(root, 200_000), name)

    def test_missing_or_broken_session_file_keeps_150000(self):
        for content in (None, "{kaputt", "[]", '{"model": "x"}', '{"name": 7}'):
            with tempfile.TemporaryDirectory() as tmp:
                root = self.repo(tmp, content)
                self.assertEqual(ce_report.input_limit(root), 150_000, content)
                self.assertTrue(self.flagged(root, 200_000), content)
        with tempfile.TemporaryDirectory() as tmp:  # no git at all
            self.assertEqual(ce_report.input_limit(tmp), 150_000)

    def test_cli_main_uses_the_limit_of_the_working_folder(self):
        """0.36.2: `ce_report.main` (the CLI) reads the session name from the current folder: Sightline quiet, others flagged at 200,000."""
        import contextlib, io, os
        usage = {"input_tokens": 200_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 1}
        for name, flagged in (("Sightline", False), ("Anderes", True)):
            with tempfile.TemporaryDirectory() as tmp:
                root = self.repo(tmp, json.dumps({"name": name}))
                path = Path(tmp) / "t.jsonl"
                path.write_text(json.dumps(record("a", usage)) + "\n", encoding="utf-8")
                out, cwd = io.StringIO(), os.getcwd()
                os.chdir(root)
                try:
                    with contextlib.redirect_stdout(out):
                        self.assertEqual(ce_report.main([str(path)]), 0)
                finally:
                    os.chdir(cwd)
                self.assertEqual("Spitzen-Input 200,000 Tokens (> 150,000)" in out.getvalue(), flagged, name)

if __name__ == "__main__":
    unittest.main()
