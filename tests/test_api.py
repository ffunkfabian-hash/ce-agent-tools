"""API boundaries are exercised offline with synthetic credentials and transport."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from decimal import Decimal
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

TOOLS = Path(__file__).resolve().parents[1] / "plugins/ce-shared/tools"
sys.path.insert(0, str(TOOLS))
import ce_api
from ce_api_budget import BudgetLedger


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy_path = self.root / ".ce-api.json"
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("A synthetic test question", encoding="utf-8")
        (self.root / "system.txt").write_text("A synthetic stable system prompt", encoding="utf-8")
        self.policy = {
            "schema_version": 1, "pricing_verified": date.today().isoformat(),
            "monthly_usd": "1", "request_usd": "0.1", "max_input_tokens": 5000,
            "max_output_tokens": 100, "default_model": "haiku", "system_file": "system.txt",
            "prices_per_million": {
                "haiku": {"input": "0.1", "output": "0.5", "write_5m": "0.125", "read": "0.01"},
                "sonnet": {"input": "2", "output": "10", "write_5m": "2.5", "read": "0.1"},
                "opus": {"input": "4", "output": "20", "write_5m": "5", "read": "0.2"},
            },
        }
        self.write_policy()
        self.ledger_path = self.root / "budget.json"
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(ce_api, "AUTH_DIR", self.root / "isolated-auth").start()
        mock.patch.object(ce_api, "LEDGER", self.ledger_path).start()
        # Every test is offline even if an expected mock is accidentally omitted.
        self.network = mock.patch.object(ce_api.urllib.request, "build_opener",
                                         side_effect=AssertionError("Network is forbidden in tests")).start()
        self.original_access_token = ce_api.access_token
        self.token = mock.patch.object(ce_api, "access_token", return_value="synthetic-test-token").start()
        self.calls = []
        self.response = {"content": [{"type": "text", "text": "One"}, {"type": "text", "text": "Two"}],
                         "stop_reason": "end_turn", "usage": {"input_tokens": 100,
                         "cache_creation_input_tokens": 400, "cache_read_input_tokens": 500, "output_tokens": 30}}

    def write_policy(self):
        self.policy_path.write_text(json.dumps(self.policy), encoding="utf-8")

    def transport(self, endpoint, payload, token):
        self.calls.append((endpoint, payload, token))
        if endpoint == "messages/count_tokens":
            return {"input_tokens": 1000}
        return self.response

    def ask(self, **overrides):
        values = {"policy_path": self.policy_path, "policy": self.policy,
                  "prompt_path": self.prompt, "system_path": None, "model": None,
                  "output_tokens": None}
        values.update(overrides)
        return ce_api.ask(**values)

    def run_main(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = ce_api.main(["--config", str(self.policy_path), "ask",
                                  "--prompt-file", str(self.prompt), *extra])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_auth_environment_removes_all_inherited_anthropic_overrides(self):
        environment = {"PATH": "/synthetic/bin", "OTHER": "preserved",
                       "ANTHROPIC_API_KEY": "secret-api-key", "ANTHROPIC_BASE_URL": "https://invalid",
                       "ANTHROPIC_AUTH_TOKEN": "secret-token", "ANTHROPIC_CONFIG_DIR": "/old",
                       "ANTHROPIC_PROFILE": "old", "ANTHROPIC_UNKNOWN_OVERRIDE": "bad"}
        with mock.patch.dict(ce_api.os.environ, environment, clear=True):
            isolated = ce_api.ant_environment()
        self.assertEqual({key: value for key, value in isolated.items() if key.startswith("ANTHROPIC_")},
                         {"ANTHROPIC_CONFIG_DIR": str(self.root / "isolated-auth")})
        self.assertEqual(isolated["OTHER"], "preserved")
        self.assertEqual(environment["ANTHROPIC_API_KEY"], "secret-api-key")

    def test_ant_uses_dedicated_profile_and_never_displays_failure_credentials(self):
        secret = "synthetic-secret-DO-NOT-DISPLAY"
        with mock.patch.object(ce_api.shutil, "which", return_value="/fake/ant"), \
             mock.patch.object(ce_api.subprocess, "run", return_value=subprocess.CompletedProcess(
                 [], 1, stdout=secret, stderr=secret)) as run:
            with self.assertRaises(ce_api.ApiError) as caught:
                ce_api.ant_command("auth", "print-credentials", "--access-token")
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(run.call_args.args[0], ["/fake/ant", "--profile", "nexogen", "auth",
                                               "print-credentials", "--access-token"])
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertEqual(run.call_args.kwargs["env"]["ANTHROPIC_CONFIG_DIR"], str(self.root / "isolated-auth"))
        with mock.patch.object(ce_api.shutil, "which", return_value="/fake/ant"), \
             mock.patch.object(ce_api.subprocess, "run", side_effect=subprocess.TimeoutExpired(
                 "ant", 60, output=secret, stderr=secret)):
            with self.assertRaises(ce_api.ApiError) as caught:
                ce_api.ant_command("auth", "print-credentials", "--access-token")
        self.assertNotIn(secret, str(caught.exception))

    def test_redirects_and_nonallowlisted_endpoints_are_rejected(self):
        with self.assertRaises(ce_api.ApiError):
            ce_api.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid")
        with self.assertRaises(ce_api.ApiError):
            ce_api.post("../secret", {}, "synthetic-test-token")
        self.network.assert_not_called()

    def test_http_failure_and_timeout_are_sanitized_and_not_retried(self):
        secret = "synthetic-secret-DO-NOT-DISPLAY"
        for failure in (urllib.error.HTTPError("https://invalid", 401, secret, {}, io.BytesIO(secret.encode())),
                        TimeoutError(secret)):
            with self.subTest(failure=type(failure).__name__):
                opener = mock.Mock()
                opener.open.side_effect = failure
                self.network.side_effect = None
                self.network.return_value = opener
                with self.assertRaises(ce_api.ApiError) as caught:
                    ce_api.post("messages", {}, "synthetic-test-token")
                self.assertNotIn(secret, str(caught.exception))
                self.assertIn(ce_api.NoRedirect, self.network.call_args.args)
                self.assertEqual(opener.open.call_count, 1)
                request = opener.open.call_args.args[0]
                self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
                self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-test-token")

    def test_invalid_expired_future_nan_and_duplicate_policy_fails_closed(self):
        valid = json.loads(json.dumps(self.policy))
        invalid = []
        for delta in (-31, 1):
            invalid.append({**valid, "pricing_verified": (date.today() + timedelta(days=delta)).isoformat()})
        for field, value in (("schema_version", True), ("monthly_usd", "NaN"),
                             ("request_usd", "-1"), ("max_input_tokens", True),
                             ("max_output_tokens", 0), ("default_model", "unknown")):
            invalid.append({**valid, field: value})
        bad_price = json.loads(json.dumps(valid))
        bad_price["prices_per_million"]["haiku"]["read"] = "NaN"
        invalid.append(bad_price)
        for policy in invalid:
            with self.subTest(policy=policy):
                self.policy_path.write_text(json.dumps(policy))
                with self.assertRaises(ce_api.ApiError):
                    ce_api.load_policy(self.policy_path)
        for text in ('{"schema_version":1,"schema_version":1}', '{"schema_version":NaN}', "null", ""):
            self.policy_path.write_text(text)
            with self.assertRaises(ce_api.ApiError):
                ce_api.load_policy(self.policy_path)
        self.token.assert_not_called()
        self.network.assert_not_called()

    def test_count_reserve_generate_settle_order_and_cold_cache_ceiling(self):
        events = []
        parent = self
        class RecordingLedger(BudgetLedger):
            def reserve(self, amount):
                events.append(("reserve", amount))
                return super().reserve(amount)
            def settle(self, reservation, actual):
                events.append(("settle", actual))
                return super().settle(reservation, actual)
        def transport(endpoint, payload, token):
            events.append((endpoint, None))
            if endpoint == "messages":
                state = json.loads(parent.ledger_path.read_text())
                self.assertEqual(len(state["reservations"]), 1)
                self.assertEqual(state["spent"], "0")
            return self.transport(endpoint, payload, token)
        with mock.patch.object(ce_api, "post", side_effect=transport), \
             mock.patch.object(ce_api, "BudgetLedger", RecordingLedger):
            result = self.ask()
        self.assertEqual([event[0] for event in events], ["messages/count_tokens", "reserve", "messages", "settle"])
        self.assertEqual(events[1][1], Decimal("0.0002195"))
        self.assertEqual(events[3][1], Decimal("0.00008"))
        self.assertEqual(result["budget"]["reservations"], {})
        self.assertEqual(result["usage"], self.response["usage"])
        self.assertEqual(result["cache_read_fraction"], 0.5)
        self.assertEqual(result["text"], "One\nTwo")
        system = self.calls[0][1]["system"]
        self.assertEqual(system[0]["cache_control"], {"type": "ephemeral", "ttl": "5m"})
        self.assertNotIn("max_tokens", self.calls[0][1])
        self.assertEqual(self.calls[1][1]["max_tokens"], 100)

    def test_missing_or_nontext_system_file_is_a_controlled_policy_error(self):
        for value in (None, False, 7, [], {}, "", "   "):
            with self.subTest(value=value):
                self.policy["system_file"] = value
                self.write_policy()
                with self.assertRaises(ce_api.ApiError):
                    ce_api.load_policy(self.policy_path)
        del self.policy["system_file"]
        self.write_policy()
        with self.assertRaises(ce_api.ApiError):
            ce_api.load_policy(self.policy_path)
        self.token.assert_not_called()
        self.network.assert_not_called()

    def test_request_and_monthly_limits_stop_before_inference(self):
        for limit in ("request_usd", "monthly_usd"):
            with self.subTest(limit=limit), mock.patch.object(ce_api, "post", side_effect=self.transport):
                self.calls.clear()
                self.policy[limit] = "0.00001"
                with self.assertRaises((ce_api.ApiError, ce_api.BudgetError)):
                    self.ask()
                self.assertEqual([item[0] for item in self.calls], ["messages/count_tokens"])
                self.policy[limit] = "0.1" if limit == "request_usd" else "1"

    def test_failures_and_incomplete_usage_keep_full_reservation_without_retry(self):
        scenarios = [ce_api.ApiError("API error"), ce_api.ApiError("Timeout; unknown result"),
                     {"content": [], "usage": {}},
                     {"content": [], "usage": {**self.response["usage"], "output_tokens": True}}]
        for index, scenario in enumerate(scenarios):
            with self.subTest(scenario=index):
                ledger_path = self.root / f"pending-{index}.json"
                endpoints = []
                def transport(endpoint, payload, token):
                    endpoints.append(endpoint)
                    if endpoint == "messages/count_tokens":
                        return {"input_tokens": 1000}
                    if isinstance(scenario, Exception):
                        raise scenario
                    return scenario
                with mock.patch.object(ce_api, "LEDGER", ledger_path), \
                     mock.patch.object(ce_api, "post", side_effect=transport):
                    with self.assertRaises(ce_api.ApiError):
                        self.ask()
                self.assertEqual(endpoints, ["messages/count_tokens", "messages"])
                state = json.loads(ledger_path.read_text())
                self.assertEqual(state["spent"], "0")
                self.assertEqual([item["amount"] for item in state["reservations"].values()], ["0.0002195"])

    def test_json_and_text_stdout_have_correct_cache_counts_and_costs(self):
        with mock.patch.object(ce_api, "post", side_effect=self.transport):
            code, stdout, stderr = self.run_main("--json")
            self.assertEqual(code, 0)
            result = json.loads(stdout)
            self.assertEqual(result["usage"]["cache_read_input_tokens"], 500)
            self.assertEqual(result["estimated_cost_usd"], "0.00008")
            self.assertEqual(stderr, "")
            code, stdout, stderr = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "One\nTwo\n")
        self.assertIn("Cache-Lesen 500 Tokens", stderr)
        self.assertNotIn("synthetic-test-token", stdout + stderr)

    def test_dry_run_only_counts_without_inference_or_reservation(self):
        with mock.patch.object(ce_api, "post", side_effect=self.transport):
            code, stdout, stderr = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        result = json.loads(stdout)
        self.assertTrue(result["dry_run"])
        self.assertEqual([item[0] for item in self.calls], ["messages/count_tokens"])
        self.assertEqual(result["counted_input_tokens"], 1000)
        self.assertEqual(result["reserved_input_tokens"], 1356)
        self.assertEqual(result["maximum_estimated_usd"], "0.0002195")
        self.assertEqual(result["budget"]["reservations"], {})
        self.assertEqual(result["budget"]["spent"], "0")
        self.assertEqual(stderr, "")

    def test_access_token_rejects_exported_json_or_header_injection(self):
        for exported in ("", '{"token":"synthetic-test-token"}', "synthetic\nInjected: value", "a b"):
            with self.subTest(exported=exported), \
                 mock.patch.object(ce_api, "ant_command", return_value=exported):
                with self.assertRaises(ce_api.ApiError) as caught:
                    self.original_access_token()
                self.assertNotIn("synthetic-test-token", str(caught.exception))

    def test_transport_rejects_nonobject_duplicate_nan_and_invalid_utf8_json(self):
        for body in (b"null", b"[]", b'{"usage":1,"usage":2}', b'{"usage":NaN}', b"\xff"):
            with self.subTest(body=body):
                response = mock.MagicMock()
                response.__enter__.return_value.read.return_value = body
                opener = mock.Mock()
                opener.open.return_value = response
                self.network.side_effect = None
                self.network.return_value = opener
                with self.assertRaises(ce_api.ApiError):
                    ce_api.post("messages", {}, "synthetic-test-token")
                self.assertEqual(opener.open.call_count, 1)
                response.__enter__.return_value.read.assert_called_once_with(4_000_001)

    def test_bad_token_counts_stop_before_reservation_or_generation(self):
        for counted in (0, -1, True, 5001, "1000", None):
            with self.subTest(counted=counted), \
                 mock.patch.object(ce_api, "post", return_value={"input_tokens": counted}) as post:
                with self.assertRaises(ce_api.ApiError):
                    self.ask()
                self.assertEqual(post.call_count, 1)
                self.assertEqual(post.call_args.args[0], "messages/count_tokens")
                self.assertFalse(self.ledger_path.exists())

    def test_response_overrun_is_charged_and_model_text_is_not_printed(self):
        self.response["usage"]["output_tokens"] = 10000
        self.response["content"][0]["text"] = "synthetic-result-should-not-print"
        with mock.patch.object(ce_api, "post", side_effect=self.transport):
            code, stdout, stderr = self.run_main("--json")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertNotIn("synthetic-result-should-not-print", stderr)
        self.assertEqual([item[0] for item in self.calls], ["messages/count_tokens", "messages"])
        state = json.loads(self.ledger_path.read_text())
        self.assertEqual(state["spent"], "0.005065")
        self.assertEqual(state["reservations"], {})

    def test_stale_prices_allow_diagnostics_but_prevent_asking(self):
        self.policy["pricing_verified"] = (date.today() - timedelta(days=31)).isoformat()
        self.write_policy()
        for command in ("budget", "status"):
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = ce_api.main(["--config", str(self.policy_path), command])
            self.assertEqual(result, 0)
            self.assertIsInstance(json.loads(stdout.getvalue()), dict)
            self.assertNotIn("synthetic-test-token", stdout.getvalue() + stderr.getvalue())
        self.token.reset_mock()
        with mock.patch.object(ce_api, "post") as post:
            code, stdout, stderr = self.run_main("--dry-run")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.token.assert_not_called()
        post.assert_not_called()

    def test_explicit_zero_output_limit_is_rejected_before_authentication(self):
        with self.assertRaises(ce_api.ApiError):
            self.ask(output_tokens=0)
        self.token.assert_not_called()
        self.network.assert_not_called()


def png_bytes(width, height):
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"


def jpeg_bytes(width, height):
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03" + b"\x00" * 9
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


class ImageTests(ApiTests):
    """Image attachments (0.25): off unless the policy allows them, validated before authentication."""

    def setUp(self):
        super().setUp()
        self.png = self.root / "render.png"
        self.png.write_bytes(png_bytes(1024, 768))
        self.jpg = self.root / "concept.jpg"
        self.jpg.write_bytes(jpeg_bytes(1280, 720))

    def allow(self, images=2, size=3_750_000):
        self.policy["max_images"], self.policy["max_image_bytes"] = images, size
        self.write_policy()

    def test_images_are_refused_without_policy_permission_before_any_call(self):
        with mock.patch.object(ce_api, "post", side_effect=self.transport):
            with self.assertRaisesRegex(ce_api.ApiError, "nicht freigegeben"):
                self.ask(image_paths=[self.png])
        self.assertEqual(self.calls, [])
        self.token.assert_not_called()

    def test_png_and_jpeg_go_before_the_text_in_count_and_generation(self):
        self.allow()
        with mock.patch.object(ce_api, "post", side_effect=self.transport):
            result = self.ask(image_paths=[self.jpg, self.png])
        self.assertEqual([call[0] for call in self.calls], ["messages/count_tokens", "messages"])
        for _, payload, _ in self.calls:
            content = payload["messages"][0]["content"]
            self.assertEqual([block["type"] for block in content], ["image", "image", "text"])
            self.assertEqual([block["source"]["media_type"] for block in content[:2]], ["image/jpeg", "image/png"])
            self.assertEqual(ce_api.base64.b64decode(content[1]["source"]["data"]), self.png.read_bytes())
            self.assertEqual(content[2]["text"], "A synthetic test question")
        self.assertEqual(result["text"], "One\nTwo")

    def test_dry_run_reports_image_count(self):
        self.allow()
        with mock.patch.object(ce_api, "post", side_effect=self.transport):
            code, stdout, _ = self.run_main("--dry-run", "--image", str(self.png))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["images"], 1)
        self.assertEqual([call[0] for call in self.calls], ["messages/count_tokens"])

    def test_text_only_payload_is_unchanged(self):
        self.allow()
        with mock.patch.object(ce_api, "post", side_effect=self.transport):
            self.ask()
        self.assertEqual(self.calls[0][1]["messages"][0]["content"], "A synthetic test question")

    def test_bad_images_fail_before_authentication(self):
        self.allow(images=1, size=200)
        text = self.root / "notes.png"
        text.write_text("not an image", encoding="utf-8")
        huge = self.root / "huge.png"
        huge.write_bytes(png_bytes(9000, 10))
        big = self.root / "big.jpg"
        big.write_bytes(jpeg_bytes(100, 100) + b"\x00" * 300)
        cases = [([self.png, self.jpg], "Zu viele Bilder"), ([text], "Nur PNG oder JPEG"),
                 ([huge], "über 8000 px"), ([big], "Bild zu groß"), ([self.root / "missing.png"], "nicht lesbar")]
        with mock.patch.object(ce_api, "post", side_effect=self.transport):
            for paths, message in cases:
                with self.subTest(message=message), self.assertRaisesRegex(ce_api.ApiError, message):
                    self.ask(image_paths=paths)
        self.assertEqual(self.calls, [])
        self.token.assert_not_called()

    def test_invalid_image_policy_fails_closed(self):
        for images, size in ((9, 1000), ("2", 1000), (True, 1000), (-1, 1000), (2, None), (2, 4_000_000)):
            with self.subTest(images=images, size=size):
                self.policy["max_images"], self.policy["max_image_bytes"] = images, size
                self.write_policy()
                with self.assertRaises(ce_api.ApiError):
                    ce_api.load_policy(self.policy_path)
