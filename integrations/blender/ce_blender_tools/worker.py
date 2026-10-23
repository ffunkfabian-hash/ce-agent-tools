"""Standalone Python entry point delegating to the existing ce-shared adapters."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import traceback


def run(request, directory):
    tools = Path(request["tools_root"])
    sys.path.insert(0, str(tools))
    import ce_blender_preflight as preflight
    import ce_turnaround as turnaround

    kind = request["kind"]
    profile = Path(request["profile"])
    if kind.startswith("profile_"):
        core = preflight if kind == "profile_preflight" else turnaround
        parsed = core.load_profile(profile)
        return {"ok": True, "summary": f"Profil gültig: {parsed['generator']}", "issues": []}
    common = ["--profile", str(profile), "--blender", request["blender"]]
    if kind == "preflight":
        report_path = directory / "preflight.json"
        code = preflight.main(common + ["--asset", request["asset"], "--out", str(report_path)])
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
        return {"ok": code == 0, "summary": "Preflight bestanden" if code == 0 else "Preflight fehlgeschlagen; Protokoll prüfen",
                "issues": report.get("issues", []), "output": str(report_path)}
    if kind != "turnaround":
        raise ValueError("Unbekannter Auftrag")
    output = directory / "preview"
    argv = common + ["--input", request["asset"], "--out", str(output), "--frames", str(request["frames"]),
                     "--resolution", str(request["resolution"]), "--fps", str(request["fps"])]
    if request["clip"]:
        argv.append("--clip")
    code = turnaround.main(argv)
    return {"ok": code == 0, "summary": "Turnaround fertig" if code == 0 else "Turnaround fehlgeschlagen; Protokoll prüfen",
            "issues": [], "output": str(output)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args(argv)
    directory = args.request.resolve().parent
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        if os.name == "posix":
            identity = {"job_id": request["job_id"], "pid": os.getpid(), "pgid": os.getpgrp(),
                        "supervisor_pid": os.getppid()}
            identity_tmp = directory / "worker-identity.tmp"
            identity_tmp.write_text(json.dumps(identity), encoding="utf-8")
            identity_tmp.replace(directory / "worker-identity.json")
        result = run(request, directory)
    except Exception as exc:
        traceback.print_exc()
        result = {"ok": False, "summary": str(exc), "issues": []}
    temporary = directory / "result.tmp"
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(directory / "result.json")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
