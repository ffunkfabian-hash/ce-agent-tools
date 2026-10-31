"""0.23: the model line survives an older reservation, a second reservation and the three-day limit (Postfach comments 6381261710, 6373473967)."""
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_postfach_courier import Origin, pf, git  # noqa: E402

NAME = "CE-AGENT-TOOLS 0.23 „Modellzeile“ · Mac"


class ModelSurvives(Origin):
    def session_file(self) -> Path:
        return Path(git(self.repo, "rev-parse", "--absolute-git-dir")) / "ce-session.json"

    def test_remembering_the_same_name_again_keeps_the_model(self):
        self.reserve("--name", "Modellzeile", "--model", "Opus 5.5, medium", "--no-announce")
        saved = json.loads(self.session_file().read_text(encoding="utf-8"))
        pf.sn.remember(self.repo, saved["name"], saved["branch"])  # a later write without --model (patch, worktree switch)
        self.assertEqual(self.say("x").stdout.splitlines()[1], "Opus 5.5, medium")

    def test_another_name_does_not_inherit_the_model(self):
        self.reserve("--name", "Modellzeile", "--model", "Opus 5.5, medium", "--no-announce")
        saved = json.loads(self.session_file().read_text(encoding="utf-8"))
        pf.sn.remember(self.repo, "CE-AGENT-TOOLS 0.99 „Anderes“ · Mac", saved["branch"])
        self.assertEqual(self.say("x").stdout.splitlines()[1], "Modell und Aufwand: nicht angegeben")

    def test_reservation_older_than_three_days_still_gives_the_model(self):
        self.reserve("--name", "Modellzeile", "--model", "Opus 5.5, medium", "--no-announce")
        git(self.repo, "checkout", "-q", "claude/ce-agent-tools-0.21-modellzeile")
        record = json.loads(self.session_file().read_text(encoding="utf-8"))
        record["reserved_at"] = time.time() - 5 * 86400
        self.session_file().write_text(json.dumps(record), encoding="utf-8")
        out = self.say("x")
        self.assertEqual(out.stdout.splitlines()[1], "Opus 5.5, medium", out.stderr)

    def test_model_from_the_announcement_when_the_file_has_none(self):
        body = pf.announcement(NAME, "claude/x", "abc", "ce-agent-tools", "Auftrag", "Opus 5.5, medium")
        fetcher = lambda since: [{"id": 1, "created_at": "2026-10-20T10:00:00Z", "body": body}]
        self.assertEqual(pf.announced_model(NAME, fetcher), "Opus 5.5, medium")
        other = lambda since: [{"id": 1, "created_at": "2026-10-20T10:00:00Z", "body": body.replace(NAME, "TG 0.1 „X“ · Mac")}]
        self.assertEqual(pf.announced_model(NAME, other), "")
        none = pf.announcement(NAME, "claude/x", "abc", "ce-agent-tools", "Auftrag", "")
        self.assertEqual(pf.announced_model(NAME, lambda since: [{"id": 1, "body": none}]), "")

    def test_unreadable_postfach_gives_no_model_without_error(self):
        def broken(since):
            raise RuntimeError("offline")
        self.assertEqual(pf.announced_model(NAME, broken), "")


if __name__ == "__main__":
    unittest.main()
