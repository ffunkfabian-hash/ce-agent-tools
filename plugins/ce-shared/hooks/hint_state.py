#!/usr/bin/env python3
"""Hint throttle of ce-shared (0.33 Earshot): a hint goes to the model once per session, not at every call.

Trimline (Nexogen 1.24, 04.11.2026) counted about 3,750 hook warnings in 26 days that only the user saw. Since 0.33 the
hooks hand hints to the model as additionalContext - and a hint that comes with every matching call would lengthen the
run instead of shortening it. `due(session_id, key, every)` says whether a hint with this key may be shown now in this
session: at its first hit, and again at every `every`-th further hit (0: never again). The counter lives in one small
JSON file per session below the CubeEscape cache folder (`CE_HOOK_STATE` overrides the folder; tests use a temp dir).
Without a session id every hint is due. Any error means due - a hint too many is better than a lost one (fail open).
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
KEEP_DAYS = 14


def state_dir() -> Path:
    custom = os.environ.get("CE_HOOK_STATE")
    if custom:
        return Path(custom)
    try:
        sys.path.insert(0, str(PLUGIN_ROOT / "tools"))
        import heavy_lock  # type: ignore
        return Path(heavy_lock.cache_dir("hooks"))
    except Exception:  # noqa: BLE001 - no cache folder, a temp folder does
        return Path(tempfile.gettempdir()) / "ce-shared-hooks"


def state_file(session_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(session_id))[:80]
    return state_dir() / f"hints-{safe}.json"


def load(session_id: str) -> dict:
    try:
        data = json.loads(state_file(session_id).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(session_id: str, data: dict) -> None:
    path = state_file(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temp, path)


def count(session_id: str, key: str) -> int:
    """Counts one hit of `key` in the session and returns the new count (1 at the first hit); 1 without a session."""
    if not session_id:
        return 1
    try:
        data = load(session_id)
        data[key] = int(data.get(key, 0)) + 1
        save(session_id, data)
        return data[key]
    except Exception:  # noqa: BLE001
        return 1


def due(session_id: str, key: str, every: int = 0) -> bool:
    """True when the hint `key` may be shown now: at the first hit of the session and at every `every`-th hit after it."""
    hits = count(session_id, key)
    return hits == 1 or (every > 0 and (hits - 1) % every == 0)


def prune(days: int = KEEP_DAYS) -> None:
    """Removes state files of sessions older than `days` (called at SessionStart; errors are ignored)."""
    try:
        cutoff = time.time() - days * 86400
        for path in state_dir().glob("hints-*.json"):
            if path.stat().st_mtime < cutoff:
                path.unlink()
    except Exception:  # noqa: BLE001
        pass
