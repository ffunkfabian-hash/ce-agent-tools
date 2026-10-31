#!/usr/bin/env python3
"""Prints the Codex quota (5-hour and weekly window) from the newest rate_limits entry in ~/.codex/sessions.

Windows are named by window_minutes (300 = 5h, 10080 = weekly), not by position: plans without a 5-hour window
(e.g. plan_type "prolite") report only the weekly window as "primary".

The Codex CLI writes {"rate_limits": {"primary": {"used_percent", "window_minutes", "resets_at"}, "secondary": ...}}
into every rollout file after a model turn. The value is as fresh as the last Codex call on this machine.
Usage: python3 codex_quota.py [--json]
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

def main():
    best = newest()
    if not best:
        print("Codex-Stand unbekannt (kein rate_limits-Eintrag gefunden)")
        return 1
    stamp, rl, path = best
    out = {"as_of": stamp, "plan": rl.get("plan_type")}
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if not w:
            continue
        minutes = w.get("window_minutes")
        name = {300: "5h", 10080: "weekly"}.get(minutes, "%smin" % minutes)
        used = float(w["used_percent"])
        out[name] = {"used": used, "left": round(100 - used, 1), "resets": time.strftime("%H:%M %d.%m.", time.localtime(w["resets_at"]))}
    if "--json" in sys.argv:
        print(json.dumps(out))
    else:
        print("Codex-Stand (Stand %s, Plan %s):" % (stamp, out["plan"] or "?"))
        for name, o in out.items():
            if isinstance(o, dict):
                print("  %-6s verbraucht %5.1f %%  übrig %5.1f %%  Reset %s" % (name, o["used"], o["left"], o["resets"]))
        if "5h" not in out:
            print("  (kein 5-h-Fenster im Plan: die 5-h-Schwellen gelten für das Wochenfenster)")
    return 0

try:  # (0.31) Windows: a cp1252 console or pipe cannot take the tool's „“ → ·
    import ce_common as _console
    _console.utf8_console()
except ImportError:
    pass
sys.exit(main())
