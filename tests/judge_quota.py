"""Pins the Codex quota the judge reads, so the figure in ~/.codex on this machine never decides a test."""
from pathlib import Path
import sys
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "ce-shared" / "tools"))

ENOUGH_USED = 40   # 60 % left: ce_codex_quota.route keeps Codex
LOW_USED = 89      # 11 % left, under LOW_PERCENT: route sends the judge to ./ce api


def codex_quota(used_percent):
    """Patches only the reader of ~/.codex; summary, route and the threshold stay real."""
    window = {"used_percent": used_percent, "window_minutes": 10080, "resets_at": int(time.time()) + 3600}
    return mock.patch("ce_codex_quota.newest", return_value=("2026-11-05T10:00:00Z", {"primary": window}, "rollout.jsonl"))
