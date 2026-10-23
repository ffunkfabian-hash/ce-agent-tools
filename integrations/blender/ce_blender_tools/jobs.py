"""UI-independent process adapter; validation and rendering live in ce-shared."""
from __future__ import annotations

import json
import os
import signal
import time
import uuid
from pathlib import Path
import subprocess
import tempfile

KINDS = {"profile_preflight", "profile_turnaround", "preflight", "turnaround"}


def current_asset(filepath: str, saved: bool, dirty: bool) -> str:
    if not saved or not filepath:
        raise ValueError("Aktuelle Szene ist ungespeichert. Selbst speichern oder Datenträgerdatei wählen.")
    if dirty:
        raise ValueError("Aktuelle Szene hat ungespeicherte Änderungen. Selbst speichern oder ausdrücklich Datenträgerdatei wählen.")
    return filepath


def prepare_request(*, tools_root, python, blender, cache, kind, profile, asset="", frames=1, resolution=640, clip=False, fps=30):
    """Create an isolated job directory, returning argv and paths, never a shell string."""
    if kind not in KINDS:
        raise ValueError("Unbekannter Auftrag")
    tools = Path(tools_root).expanduser().resolve()
    for filename in ("ce_blender_preflight.py", "ce_turnaround.py", "heavy_lock.py", "ce_media.py"):
        if not (tools / filename).is_file():
            raise ValueError(f"Werkzeug fehlt: {tools / filename}")
    # Keep a selected virtualenv's executable path: resolving its symlink would
    # switch to the base interpreter and discard the environment's packages.
    interpreter = Path(python).expanduser().absolute()
    if not interpreter.is_file():
        raise ValueError("Externen Python-3-Interpreter wählen (nicht die Blender-Anwendung).")
    if not Path(profile).is_file():
        raise ValueError("Vorhandenes JSON-Profil wählen.")
    if kind in {"preflight", "turnaround"} and not Path(asset).is_file():
        raise ValueError("Vorhandene .blend- oder .fbx-Datei wählen.")
    cache = Path(cache).expanduser().resolve()
    cache.mkdir(parents=True, exist_ok=True)
    job = Path(tempfile.mkdtemp(prefix="ce-blender-", dir=cache))
    request = {"job_id": uuid.uuid4().hex, "tools_root": str(tools), "blender": str(blender), "kind": kind,
               "profile": str(Path(profile).resolve()), "asset": str(Path(asset).resolve()) if asset else "",
               "frames": frames, "resolution": resolution, "clip": clip, "fps": fps}
    request_file = job / "request.json"
    request_file.write_text(json.dumps(request), encoding="utf-8")
    command = [str(interpreter), str(Path(__file__).with_name("worker.py")), "--request", str(request_file)]
    if kind in {"preflight", "turnaround"}:
        # The outer supervisor holds the lock until the entire worker group ends,
        # even when the worker itself exits before its renderer has cleaned up.
        command = [str(interpreter), str(tools / "heavy_lock.py"), "run", "--resource", "blender",
                   "--what", f"Blender sidebar {kind}", "--", *command]
    return command, job


class Job:
    def __init__(self, command, directory, *, cancel_grace=5.0):
        self.directory = Path(directory)
        self.cancelling = False
        self.cancel_grace = cancel_grace
        self.cancelled_at = None
        self.escalated = False
        self.request = json.loads((self.directory / "request.json").read_text(encoding="utf-8"))
        self.log = (self.directory / "process.log").open("wb")
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=self.log,
                                            stderr=subprocess.STDOUT, start_new_session=os.name == "posix")
        except Exception:
            self.log.close()
            raise

    def poll(self):
        code = self.process.poll()
        if code is None:
            self._escalate_if_due()
            return None
        self.log.close()
        if self.cancelling:
            return {"ok": False, "summary": "Abgebrochen; Prozessgruppe beendet", "issues": []}
        try:
            result = json.loads((self.directory / "result.json").read_text(encoding="utf-8"))
            if (not isinstance(result, dict) or type(result.get("ok")) is not bool
                    or not isinstance(result.get("summary"), str) or not isinstance(result.get("issues", []), list)):
                raise ValueError("Ungültiges Ergebnisformat")
            if code != 0:
                result["ok"] = False
            return result
        except (OSError, ValueError) as exc:
            return {"ok": False, "summary": f"Prozess beendet ({code}), Ergebnis fehlt: {exc}", "issues": []}

    def cancel(self):
        """Request TERM from the supervisor; poll keeps tracking its group barrier."""
        if self.cancelling:
            return
        self.cancelling = True
        self.cancelled_at = time.monotonic()
        if self.process.poll() is None:
            try:
                # Only the supervisor receives TERM here. Its existing run_group
                # forwards to the worker group and waits for every descendant.
                self.process.terminate()
            except ProcessLookupError:
                pass

    def _escalate_if_due(self):
        """Kill only the attested worker group; the lock supervisor stays alive."""
        if (not self.cancelling or self.escalated or os.name != "posix"
                or time.monotonic() - self.cancelled_at < self.cancel_grace):
            return
        if self.request["kind"] in {"preflight", "turnaround"}:
            try:
                identity = json.loads((self.directory / "worker-identity.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            # The worker attests the fresh request nonce and its direct supervisor.
            # Its session group remains owned by that supervisor's run_group
            # barrier even if the worker leader has already exited.
            if (identity.get("job_id") != self.request["job_id"]
                    or identity.get("supervisor_pid") != self.process.pid
                    or type(identity.get("pid")) is not int or identity["pid"] <= 1
                    or identity.get("pgid") != identity["pid"]
                    or identity["pgid"] == self.process.pid):
                return
            pgid = identity["pgid"]
        else:
            # Profile checks have no supervisor/renderer and are their own session.
            pgid = self.process.pid
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.escalated = True
