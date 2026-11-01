#!/usr/bin/env python3
"""Prints the Codex quota (5-hour and weekly window) from the newest rate_limits entry in ~/.codex/sessions.

Windows are named by window_minutes (300 = 5h, 10080 = weekly), not by position: plans without a 5-hour window
(e.g. plan_type "prolite") report only the weekly window as "primary".

The Codex CLI writes {"rate_limits": {"primary": {"used_percent", "window_minutes", "resets_at"}, "secondary": ...}}
into every rollout file after a model turn. The value is as fresh as the last Codex call on this machine.
Usage: python3 codex_quota.py [--json]

0.32: `summary()` and `route()` are importable (no exit at import); `./ce judge` uses `route()` to pick Codex or `--api`
on its own: less than LOW_PERCENT left in any window, or a window at its limit -> "api".
"""
import glob, json, os, sys, time

def newest(limit_files=12):
    files = sorted(glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*.jsonl")), key=os.path.getmtime, reverse=True)
    best = None
    for path in files[:limit_files]:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 400_000))
            lines = fh.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            rl = (obj.get("payload") or {}).get("rate_limits") or obj.get("rate_limits")
            if rl and rl.get("primary"):
                stamp = obj.get("timestamp", "")
                if best is None or stamp > best[0]:
                    best = (stamp, rl, path)
                break
    return best

LOW_PERCENT = 15.0   # CLAUDE.md (Nexogen): under 15 % left or at the limit -> Opus judges (0.32: ./ce judge does it itself)


def summary(best=None):
    """{"as_of", "plan", "5h": {...}, "weekly": {...}} from the newest rate_limits entry, or None if there is none."""
    best = best or newest()
    if not best:
        return None
    stamp, rl, _path = best
    out = {"as_of": stamp, "plan": rl.get("plan_type")}
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if not w:
            continue
        minutes = w.get("window_minutes")
        name = {300: "5h", 10080: "weekly"}.get(minutes, "%smin" % minutes)
        used = float(w["used_percent"])
        out[name] = {"used": used, "left": round(100 - used, 1), "resets": time.strftime("%H:%M %d.%m.", time.localtime(w["resets_at"])),
                     "resets_at": w["resets_at"]}
    return out


def route(out=None, threshold=LOW_PERCENT, now=None):
    """("codex"|"api", reason): the judge's way by quota. Unknown or unreadable quota keeps Codex (the old behavior)."""
    try:
        out = summary() if out is None else out
    except (OSError, ValueError, KeyError, TypeError):
        out = None
    windows = {name: o for name, o in (out or {}).items() if isinstance(o, dict)}
    # A window whose reset time has passed was reset since the last Codex call: its old figure says nothing now. Without this an
    # old low figure would send every review to --api, Codex would never run again and the figure would never be rewritten.
    clock = time.time() if now is None else now
    stale = [name for name, o in windows.items() if isinstance(o.get("resets_at"), (int, float)) and o["resets_at"] < clock]
    windows = {name: o for name, o in windows.items() if name not in stale}
    if not windows:
        return "codex", "Codex-Stand unbekannt" if not stale else "Codex-Stand veraltet (Reset seit %s vorbei)" % ", ".join(stale)
    name, low = min(windows.items(), key=lambda item: item[1]["left"])
    if low["left"] <= 0:
        return "api", "Codex-Limit erreicht (%s, Reset %s)" % (name, low["resets"])
    if low["left"] < threshold:
        return "api", "Codex-Rest %.1f %% (%s) unter %g %%" % (low["left"], name, threshold)
    return "codex", "Codex-Rest %.1f %% (%s)" % (low["left"], name)


def main():
    out = summary()
    if not out:
        print("Codex-Stand unbekannt (kein rate_limits-Eintrag gefunden)")
        return 1
    if "--json" in sys.argv:
        print(json.dumps(out))
    else:
        print("Codex-Stand (Stand %s, Plan %s):" % (out["as_of"], out["plan"] or "?"))
        for name, o in out.items():
            if isinstance(o, dict):
                print("  %-6s verbraucht %5.1f %%  übrig %5.1f %%  Reset %s" % (name, o["used"], o["left"], o["resets"]))
        if "5h" not in out:
            print("  (kein 5-h-Fenster im Plan: die 5-h-Schwellen gelten für das Wochenfenster)")
    return 0


if __name__ == "__main__":
    try:  # (0.31) Windows: a cp1252 console or pipe cannot take the tool's „“ → ·
        import ce_common as _console
        _console.utf8_console()
    except ImportError:
        pass
    sys.exit(main())
