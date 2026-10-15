"""Tests of ai-relay: all provider kinds against a fake local server, fallback on limits, consent and secret redaction.
Run: python3 -m unittest discover -s tests"""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "ai-relay" / "scripts"))
import relay  # noqa: E402

SEEN = []
MODE = {"groq": 200}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.reply(200, {"models": [{"name": "qwen2.5-coder:14b"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append((self.path, body, dict(self.headers)))
        if self.path.startswith("/ollama/"):
            self.reply(200, {"message": {"content": "ollama: ok\n\nzweite Zeile"}})
        elif self.path.startswith("/groq/"):
            if MODE["groq"] == 429:
                self.reply(429, {"error": "limit"}, {"Retry-After": "120"})
            else:
                self.reply(200, {"choices": [{"message": {"content": "groq: ok"}}]})
        elif self.path.startswith("/gemini/"):
            self.reply(200, {"candidates": [{"content": {"parts": [{"text": "gemini: "}, {"text": "ok"}]}}]})
        else:
            self.reply(404, {})


def run(argv, stdin="diff --git a/x b/x\n+x = 1\n"):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = relay.main(argv, stdin_text=stdin)
    return code, out.getvalue(), err.getvalue()


class RelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Fake)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["AI_RELAY_HOME"] = os.path.join(self.tmp.name, "home")
        os.environ["AI_RELAY_CACHE"] = os.path.join(self.tmp.name, "cache")
        for name in ("AI_RELAY_LOCAL_ONLY", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
            os.environ.pop(name, None)
        SEEN.clear()
        MODE["groq"] = 200
        self.write_config(True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, consent, **extra):
        base = f"http://127.0.0.1:{self.port}"
        cfg = {"cloud_consent": consent, "order": {"default": ["ollama", "groq", "gemini"]},
               "providers": {"ollama": {"base_url": base + "/ollama"},
                             "groq": {"base_url": base + "/groq", "key_env": "GROQ_API_KEY"},
                             "gemini": {"base_url": base + "/gemini", "key_env": "GEMINI_API_KEY"},
                             "deepseek": {"base_url": base + "/groq"}}}
        cfg.update(extra)
        home = Path(os.environ["AI_RELAY_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.json").write_text(json.dumps(cfg))

    def test_ollama_first_and_trimmed(self):
        code, out, _ = run(["ask", "--task", "review", "--max-lines", "1"])
        self.assertEqual(code, 0)
        self.assertIn("ai-relay · ollama/qwen2.5-coder:14b", out)  # model picked from /api/tags
        self.assertIn("ollama: ok", out)
        self.assertIn("gekürzt", out)

    def test_cloud_blocked_without_consent(self):
        self.write_config(False)
        os.environ["GROQ_API_KEY"] = "k"
        code, out, err = run(["ask", "--task", "review", "--provider", "groq"])
        self.assertEqual(code, 3)
        self.assertIn("Cloud gesperrt", err)
        self.assertFalse(SEEN)

    def test_local_only_env_and_paths(self):
        os.environ["AI_RELAY_LOCAL_ONLY"] = "1"
        self.assertFalse(relay.cloud_allowed(relay.load_config(), "/x")[0])
        os.environ.pop("AI_RELAY_LOCAL_ONLY")
        self.write_config(True, local_only_paths=["/private/*"])
        cfg = relay.load_config()
        self.assertFalse(relay.cloud_allowed(cfg, "/private/repo")[0])
        self.assertTrue(relay.cloud_allowed(cfg, "/public/repo")[0])

    def test_fallback_on_rate_limit_sets_cooldown(self):
        os.environ["GROQ_API_KEY"] = "k"
        os.environ["GEMINI_API_KEY"] = "g"
        MODE["groq"] = 429
        self.write_config(True, local_max_chars=10)
        code, out, _ = run(["ask", "--task", "summarize", "--provider", "groq"])
        self.assertEqual(code, 3)
        self.assertIn("groq", relay.cooldowns())
        code, out, _ = run(["ask", "--task", "summarize", "--file", __file__])  # groq paused -> gemini (ollama too big)
        self.assertEqual(code, 0)
        self.assertIn("gemini/gemini-2.5-flash", out)
        self.assertIn("gemini: ok", out)

    def test_cloud_gets_redacted_text_and_headers(self):
        os.environ["GROQ_API_KEY"] = "secret-key"
        text = 'api_key = "abcdefgh12345678"\nkey AIza' + "A" * 35 + "\nplain line\n"
        code, out, _ = run(["ask", "--task", "review", "--provider", "groq"], stdin=text)
        self.assertEqual(code, 0)
        path, body, headers = SEEN[-1]
        sent = body["messages"][1]["content"]
        self.assertNotIn("abcdefgh12345678", sent)
        self.assertNotIn("AIza", sent)
        self.assertIn("plain line", sent)
        self.assertEqual(headers["Authorization"], "Bearer secret-key")
        self.assertIn("2 Geheimnisse ersetzt", out)

    def test_gemini_adapter_joins_parts(self):
        os.environ["GEMINI_API_KEY"] = "g"
        code, out, _ = run(["ask", "--task", "ask", "--prompt", "Was?", "--provider", "gemini", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["text"], "gemini: ok")
        self.assertEqual({k.lower(): v for k, v in SEEN[-1][2].items()}["x-goog-api-key"], "g")

    def test_deepseek_only_when_explicit(self):
        os.environ["DEEPSEEK_API_KEY"] = "d"
        self.write_config(True, order={"default": ["deepseek"]})
        args = ["ask", "--task", "ask", "--prompt", "Was?"]
        code, _, err = run(args)
        self.assertEqual(code, 3)
        self.assertIn("nur mit --provider deepseek", err)
        code, out, _ = run(args + ["--provider", "deepseek"])
        self.assertEqual(code, 0)

    def test_usage_limit_and_input_checks(self):
        os.environ["GROQ_API_KEY"] = "k"
        self.write_config(True, providers={"groq": {"daily_limit": 1, "base_url": f"http://127.0.0.1:{self.port}/groq"}})
        self.assertEqual(run(["ask", "--task", "review", "--provider", "groq"])[0], 0)
        code, _, err = run(["ask", "--task", "review", "--provider", "groq"])
        self.assertEqual((code, "Tageslimit" in err), (3, True))
        self.assertEqual(run(["ask", "--task", "ask"])[0], 2)  # --prompt missing
        self.assertEqual(run(["ask", "--task", "review"], stdin="")[0], 2)

    def test_dry_run_sends_nothing(self):
        code, out, _ = run(["ask", "--task", "review", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("Reihenfolge: ollama", out)
        self.assertFalse(SEEN)

    def test_init_creates_private_keys_file(self):
        os.environ["AI_RELAY_HOME"] = os.path.join(self.tmp.name, "fresh")
        run(["init"])
        mode = (Path(os.environ["AI_RELAY_HOME"]) / "keys.env").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertFalse(relay.load_config()["cloud_consent"])


if __name__ == "__main__":
    unittest.main()
