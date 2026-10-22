#!/usr/bin/env python3
"""One heavy run per resource at a time on this Mac - the queues shared by Cube Escape, Cubegen, the robot generator
and Toolgen.

Heavy steps wait for the lock of the resource they use instead of running side by side (0.5.0 "Split Rail": before,
one lock for everything made every session wait behind every other; 20 min to 2 h of waiting per session):

    unity    full Unity regression, player measurements, unity_check.py, Unity builds
    blender  Blender standard/full tiers, surface bakes, check_output.sh, golden renders
    gpu      Metal renders and anything else that needs the GPU for itself

Unity and Blender may run at once after memory/load validation; GPU is exclusive against both.
Contact sheets and docs are light; render/bake load still needs coordination regardless of the tier name.

    python3 heavy_lock.py run --resource unity|blender|gpu [--what LABEL] [--checkout PATH] -- <command ...>
    python3 heavy_lock.py status                            who holds which resource right now

Contract (this file is the one copy; the repositories call it through CE_TOOLS and keep only thin wrappers):
- an exclusive flock(2) on ~/Library/Caches/CubeEscape/heavy-<resource>.lock, held by the waiting process for the
  whole heavy step; the file is never deleted. Without a resource (old callers, CE_HEAVY_RESOURCE unset) the old
  shared file heavy-run.lock is used: such steps still queue behind each other, and behind resource-aware ones through a shared/exclusive migration barrier;
- the holder writes one JSON line {"pid", "what", "checkout", "since"} into it; a waiting run prints it (and again every
  five minutes); pause_sessions.py spares that pid and its children;
- the lock's file descriptor is never inherited (O_CLOEXEC, close_fds): a background child (caffeinate, a Blender
  worker, Unity) would otherwise keep the lock after the run;
- `run` / run_group / run_forwarding start the command in a process group of its own (start_new_session), pass
  TERM/HUP/INT on to the whole group and return only when the group is empty: a grandchild (Blender under
  check_output.sh) keeps the lock even when its parent died first. Children that leave the group on purpose
  (their own start_new_session) do not. A holder that starts its heavy children itself (testplan.py) does the same
  with Groups;
- CE_HEAVY_LOCK=0 skips the queue; CE_HEAVY_HELD=<resource|1> marks the children of a holder, so a heavy step on the
  same resource inside another one does not wait for its own parent (a step on another resource queues for that one). CE_HEAVY_RESOURCE names the resource for callers that cannot pass it.
  CE_HEAVY_LOCK_FILE moves the lock directory and file (tests only): the resource is added to its stem.

API (union of the former copies): held() / HeavyLock (acquire, release, state, .held, .skipped, info access like a
dict; all take resource=), holder(resource), holder_pid(wait, resource), holder_pids(), skipped(), lock_path(resource),
RESOURCES, run_group() = run_forwarding(), Groups, Interrupted.
Pure Python (no bpy): generate_robot.py imports it inside Blender.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - no flock (Windows): nothing to queue on
    fcntl = None

DEFAULT = os.path.join(os.path.expanduser("~"), "Library", "Caches", "CubeEscape", "heavy-run.lock")
RESOURCES = ("unity", "blender", "gpu")
POLL_SECONDS = 2.0
REPORT_SECONDS = 300.0  # a waiting run says every five minutes that it still waits
GROUP_POLL = 0.1  # how often run_group looks whether the process group of a finished command is empty
STRAGGLER_SECONDS = 5.0  # after this long it says which group still keeps the lock
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_name(resource: str | None = None) -> str | None:
    """The resource of a step: the argument, else CE_HEAVY_RESOURCE, else None (the shared file). Unknown names fail
    loudly - a typo would silently open a new queue."""
    name = (resource or os.environ.get("CE_HEAVY_RESOURCE") or "").strip().lower() or None
    if name is not None and name not in RESOURCES:
        raise ValueError("unknown heavy resource %r (one of %s)" % (name, ", ".join(RESOURCES)))
    return name


def lock_path(resource: str | None = None) -> str:
    """The lock file: heavy-<resource>.lock next to the shared heavy-run.lock (CE_HEAVY_LOCK_FILE moves both)."""
    base = os.environ.get("CE_HEAVY_LOCK_FILE") or DEFAULT
    name = resource_name(resource)
    if name is None:
        return base
    stem, ext = os.path.splitext(base)
    stem = stem[:-4] if stem.endswith("-run") else stem
    return "%s-%s%s" % (stem, name, ext or ".lock")


def skipped(env=None, resource: str | None = None) -> str | None:
    """Why this process does not queue (None: it does). CE_HEAVY_HELD names what the parent holds ("1" = the shared
    file or the exclusive GPU gate, else a resource list): the same resource does not wait. A legacy global
    step cannot upgrade named resources; acquire the global/GPU barrier first."""
    env = os.environ if env is None else env
    if env.get("CE_HEAVY_LOCK", "1").strip() == "0":
        return "CE_HEAVY_LOCK=0"
    held = env.get("CE_HEAVY_HELD", "")
    name = resource_name(resource)
    if held and name is None and not ({"1", "gpu"} & set(held.split(","))):
        raise ValueError("legacy shared lock must be acquired before named resources")
    if held and (name is None or name in held.split(",") or "1" in held.split(",") or "gpu" in held.split(",")):
        return "the parent holds the lock (CE_HEAVY_HELD=%s)" % held
    return None


def describe(text: str) -> str:
    """The holder line of the lock file in words (whatever tool wrote it)."""
    try:
        info = json.loads(text)
        return "%s (PID %s, %s, since %s)" % (info.get("what", "?"), info.get("pid", "?"), info.get("checkout", "?"),
                                              info.get("since", "?"))
    except (ValueError, AttributeError):
        return text.strip() or "unknown"


def holder(resource: str | None = None) -> str | None:
    """The current holder of that resource's lock in words, or None while nobody holds it."""
    if fcntl is None:
        return None
    try:
        fd = os.open(lock_path(resource), os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return describe(os.pread(fd, 4096, 0).decode("utf-8", "replace"))
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def holder_pids(wait: float = 1.0) -> list[int]:
    """The PIDs of every current holder (shared file and each resource) - pause_sessions.py spares them."""
    pids = []
    for resource in (None,) + RESOURCES:
        pid = holder_pid(wait, resource)
        if pid is not None:
            pids.append(pid)
    return pids


def holder_pid(wait: float = 1.0, resource: str | None = None) -> int | None:
    """The PID the holder wrote (pause_sessions.py spares it and its children), None while the lock is free. A holder
    that has just taken the lock writes its line a moment later - waits up to `wait` seconds for it."""
    deadline = time.monotonic() + wait
    while holder(resource) is not None:
        try:
            with open(lock_path(resource), encoding="utf-8") as handle:
                return int(json.loads(handle.read()).get("pid"))
        except (OSError, ValueError, TypeError, AttributeError):
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)
    return None


class HeavyLock:
    """Waits for and holds the lock: acquire() blocks until it is ours (or skipped), release() frees it - closing the
    file descriptor (also at process exit) frees it too. While held, os.environ has CE_HEAVY_HELD=1 for children.
    The holder line is readable like a dict (lock["pid"], lock["what"] ...).

    state() is one line for reports: "held, waited 12 s" / "skipped (CE_HEAVY_LOCK=0)" / ..."""

    def __init__(self, what: str, checkout: str | None = None, out=None, poll: float = POLL_SECONDS,
                 report: float = REPORT_SECONDS, resource: str | None = None):
        self.what, self.checkout = what, checkout or os.getcwd()
        self.resource = resource_name(resource)
        self.out = out or (lambda line: print(line, flush=True))
        self.poll, self.report = poll, report
        self._gate_fd: int | None = None
        self.fd: int | None = None
        self.skipped: str | None = None
        self.waited = 0.0
        self.info: dict = {}
        self._previous_held: str | None = None

    @property
    def held(self) -> bool:
        return self.fd is not None

    def __getitem__(self, key):
        return self.info[key]

    def acquire(self) -> "HeavyLock":
        if self.fd is not None or self.skipped:
            return self
        self.skipped = skipped(resource=self.resource)
        if fcntl is None:  # pragma: no cover
            self.skipped = self.skipped or "no flock on this system"
        if self.skipped:
            return self
        if self.resource == "gpu" and os.environ.get("CE_HEAVY_HELD"):
            raise ValueError("acquire exclusive gpu lock before other resources")
        path = lock_path(self.resource)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)  # never inherited (O_CLOEXEC, PEP 446)
        start = last = time.monotonic()
        announced = False
        waiter = None
        gate = None
        try:
            if self.resource:
                gate = os.open(os.environ.get("CE_HEAVY_LOCK_FILE") or DEFAULT, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
            locks = ([(gate, fcntl.LOCK_EX if self.resource == "gpu" else fcntl.LOCK_SH)] if gate is not None else []) + [(fd, fcntl.LOCK_EX)]
            for descriptor, mode in locks:
              while True:
                try:
                    fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    who = describe(os.pread(descriptor, 4096, 0).decode("utf-8", "replace"))
                    if who == "unknown":
                        who = "; ".join(filter(None, (holder(r) for r in RESOURCES))) or "shared/resource barrier"
                    waiting_path = (os.environ.get("CE_HEAVY_LOCK_FILE") or DEFAULT) if descriptor == gate else path
                    now = time.monotonic()
                    if not announced:
                        try:
                            import ce_lock_status
                            waiter = ce_lock_status.announce(path, self.resource, self.what, self.checkout, waiting_path)
                        except Exception:
                            pass  # older embedded installations still use the same real lock
                        self.out("heavy lock (%s): waiting for %s - held by %s (CE_HEAVY_LOCK=0 skips the queue)"
                                 % (self.resource or "shared", waiting_path, who))
                        announced = True
                    elif now - last >= self.report:
                        self.out("heavy lock: still waiting after %.0f min for %s" % ((now - start) / 60, who))
                        last = now
                    time.sleep(self.poll)
        except BaseException:
            if waiter is not None:
                ce_lock_status.forget(waiter)
            os.close(fd)
            if gate is not None:
                os.close(gate)
            raise
        if waiter is not None:
            ce_lock_status.forget(waiter)
        self._gate_fd = gate
        self.waited = time.monotonic() - start
        if announced:
            self.out("heavy lock: free after %.0f s" % self.waited)
        self.info = {"pid": os.getpid(), "what": self.what, "checkout": self.checkout,
                     "since": time.strftime("%Y-%m-%d %H:%M:%S")}
        if self.resource:
            self.info["resource"] = self.resource
        os.ftruncate(fd, 0)
        os.pwrite(fd, (json.dumps(self.info) + "\n").encode(), 0)
        self.fd = fd
        self._previous_held = os.environ.get("CE_HEAVY_HELD")
        os.environ["CE_HEAVY_HELD"] = ",".join(filter(None, [self._previous_held, ("1" if self.resource == "gpu" else self.resource or "1")]))
        return self

    def release(self) -> None:
        if self.fd is None:
            return
        if self._previous_held is None:
            os.environ.pop("CE_HEAVY_HELD", None)
        else:
            os.environ["CE_HEAVY_HELD"] = self._previous_held
        try:
            os.ftruncate(self.fd, 0)  # still holding it: nobody reads an old holder afterwards
        finally:
            os.close(self.fd)
            self.fd = None
            if self._gate_fd is not None:
                os.close(self._gate_fd)
                self._gate_fd = None

    def state(self) -> str:
        if self.skipped:
            return "skipped (%s)" % self.skipped
        if self.fd is not None:
            return "held, waited %.0f s" % self.waited
        return "not held"


@contextlib.contextmanager
def held(what: str, checkout: str | None = None, out=None, poll: float = POLL_SECONDS, resource: str | None = None):
    """Waits for the lock of `resource` (unity, blender, gpu; None = shared file), holds it while the block runs
    (yields the HeavyLock; .skipped says why it did not queue)."""
    lock = HeavyLock(what, checkout, out, poll, resource=resource).acquire()
    try:
        yield lock
    finally:
        lock.release()


def group_alive(pgid: int) -> bool:
    """True while any process of the group still exists (EPERM: one runs that this user may not signal)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_groups(pgids: list[int], out, say, children=()) -> None:
    """Returns once every process group has ended; after STRAGGLER_SECONDS says once which still keep the lock.
    children: this process's own children (Popen) - reaped on the way, a zombie would keep its group alive."""
    start = time.monotonic()
    told = False
    while True:
        for child in children:
            child.poll()
        alive = [pgid for pgid in pgids if group_alive(pgid)]
        if not alive:
            return
        if not told and time.monotonic() - start >= STRAGGLER_SECONDS:
            out(say(alive))
            told = True
        try:
            time.sleep(GROUP_POLL)
        except KeyboardInterrupt:  # already forwarded to the group; keep waiting
            continue


def run_group(argv: list, env=None, out=None, **popen) -> int:
    """Runs argv in a process group of its own and returns its exit code once the whole group has ended. A TERM/HUP/INT
    for this process (a tool's time limit, Ctrl+C) goes on to the whole group, so the lock is never free while any part
    of the heavy step still runs - also not when the command itself died before its children (only SIGKILL of this
    wrapper alone can still part them). Handlers are put back afterwards (None - set outside Python, e.g. in Blender -
    cannot be put back and is left alone)."""
    out = out or (lambda line: print(line, flush=True))
    child = subprocess.Popen(argv, env=env, start_new_session=True, **popen)  # its pid is the group id

    def forward(signum, _frame):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child.pid, signum)

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, forward)
        except ValueError:  # not the main thread
            pass
    try:
        while True:
            try:
                code = child.wait()
                break
            except KeyboardInterrupt:  # forwarded already; keep waiting for the child
                continue
        wait_groups([child.pid], out,
                    lambda alive: "heavy lock: command ended (exit %s), the lock stays until the rest of its process "
                                  "group %d ends" % (code, child.pid))
        return code
    finally:
        for sig, handler in previous.items():
            if handler is not None:
                signal.signal(sig, handler)


run_forwarding = run_group  # name used by the robot generator / Toolgen


class Interrupted(Exception):
    """A TERM/HUP/INT reached a holder while its Groups block ran - raised in the block, passed on once the groups ended."""

    def __init__(self, signum: int):
        super().__init__("signal %s" % signum)
        self.signum = signum


class Groups:
    """The heavy children of a process that holds the lock itself instead of wrapping one command with `run` - the full
    regression of testplan.py:

        with held("..."), Groups() as groups:
            child = groups.popen([...])        # a process group of its own (start_new_session)

    While the block runs, TERM/HUP/INT go on to every group and raise Interrupted in the block (no new child starts
    after it); leaving the block waits until every group is empty, before held() frees the lock. Only the main thread
    may enter it (signal handlers). Children that leave on purpose (their own start_new_session) are not waited for."""

    SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)

    def __init__(self, out=None):
        self.out = out or (lambda line: print(line, flush=True))
        self.pgids: list[int] = []
        self.children: list[subprocess.Popen] = []
        self.signum: int | None = None
        self.quiet = False  # while a child starts or the block ends: a signal is passed on but raises nothing
        self.previous = {}

    def __enter__(self) -> "Groups":
        for sig in self.SIGNALS:
            self.previous[sig] = signal.signal(sig, self._signal)
        return self

    def _signal(self, signum, _frame):
        self._forward(signum)
        if self.signum is None:
            self.signum = signum
        if not self.quiet:
            raise Interrupted(self.signum)

    def _forward(self, signum, pgids=None):
        for pgid in self.pgids if pgids is None else pgids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signum)

    def popen(self, command: list[str], **popen) -> subprocess.Popen:
        """subprocess.Popen in a process group of its own, waited for when the block ends."""
        if self.signum is not None:
            raise Interrupted(self.signum)
        self.quiet = True  # a signal between fork and append would miss the new group
        try:
            child = subprocess.Popen(command, start_new_session=True, **popen)  # its pid is the group id
            self.pgids.append(child.pid)
            self.children.append(child)
        finally:
            self.quiet = False
        if self.signum is not None:  # it came while the child started
            self._forward(self.signum, [child.pid])
            raise Interrupted(self.signum)
        return child

    def __exit__(self, kind, error, _tb):
        self.quiet = True
        try:
            wait_groups(self.pgids, self.out,
                        lambda alive: "heavy lock: the lock stays until the process group(s) %s end"
                        % ", ".join(map(str, alive)), self.children)
        finally:
            for sig, handler in self.previous.items():
                if handler is not None:
                    signal.signal(sig, handler)
        if kind is None and self.signum is not None:  # the signal came while the block ended
            raise Interrupted(self.signum)
        return False


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--resource", choices=RESOURCES, help="which resource the step needs (default: CE_HEAVY_RESOURCE "
                     "or the shared file)")
    run.add_argument("--what", help="what runs (shown to waiting runs)")
    run.add_argument("--checkout", help="where it runs")
    run.add_argument("rest", nargs=argparse.REMAINDER)
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "status":
        for resource in (None,) + RESOURCES:
            print("%-8s %s" % (resource or "shared", holder(resource) or "free"))
        return 0
    command = args.rest[1:] if args.rest[:1] == ["--"] else args.rest
    if not command:
        parser.error("run -- <command ...>")
    with held(args.what or os.path.basename(command[0]), args.checkout, resource=args.resource) as lock:
        if lock.skipped:
            print("heavy lock: %s" % lock.state(), flush=True)
        # close_fds=False: fds the caller passed on on purpose (copy locks) stay with the child; this lock does not -
        # it is not inheritable.
        return run_group(command, close_fds=False)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
