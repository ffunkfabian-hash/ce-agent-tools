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
    python3 heavy_lock.py status                            who holds which resource right now: session, branch,
                                                            client, running time, command, waiting runs (0.24)

Contract (this file is the one copy; the repositories call it through CE_TOOLS and keep only thin wrappers):
- an exclusive flock(2) on ~/Library/Caches/CubeEscape/heavy-<resource>.lock (Windows 0.31: LockFileEx on
  %LOCALAPPDATA%\\CubeEscape\\heavy-<resource>.lock, cache_dir()), held by the waiting process for the
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
  with Groups. Windows (0.31): the group is a job object (_Job: kill on close, breakaway allowed), Ctrl+C/Ctrl+Break
  go on as Ctrl+Break, a second signal ends the job;
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
except ImportError:  # Windows: LockFileEx below
    fcntl = None

WINDOWS = os.name == "nt"


def cache_dir(*parts: str) -> str:
    """CubeEscape's cache folder on this system (locks, check state, search index, nightly logs): macOS (and Linux, as
    before) ~/Library/Caches/CubeEscape, Windows %LOCALAPPDATA%\\CubeEscape (0.31). The one place for that path."""
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "CubeEscape", *parts)
    return os.path.join(os.path.expanduser("~"), "Library", "Caches", "CubeEscape", *parts)


DEFAULT = cache_dir("heavy-run.lock")
RESOURCES = ("unity", "blender", "gpu")
# never inherited (O_CLOEXEC; Windows: O_NOINHERIT, binary so the holder line keeps its bytes)
OPEN_FLAGS = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_BINARY", 0)
POLL_SECONDS = 2.0
REPORT_SECONDS = 300.0  # a waiting run says every five minutes that it still waits
GROUP_POLL = 0.1  # how often run_group looks whether the process group of a finished command is empty
STRAGGLER_SECONDS = 5.0  # after this long it says which group still keeps the lock
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# the signals a holder passes on to its groups (Windows has no HUP; Ctrl+Break is its second console signal)
FORWARDED = ((signal.SIGTERM, signal.SIGBREAK, signal.SIGINT) if WINDOWS
             else (signal.SIGTERM, signal.SIGHUP, signal.SIGINT))


# Platform layer (0.31 "Crossover"). POSIX keeps flock(2), pread/pwrite and process groups exactly as before. Windows:
# LockFileEx on one byte far behind the holder line (a locked range cannot be read by others there, the line must stay
# readable) - shared and exclusive like flock, freed when the handle closes or the process dies; a job object as the
# process group. Kept in this file: repositories load heavy_lock.py on its own (spec_from_file_location, Blender).
if WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _LOCK_OFFSET_HIGH = 0x40000000  # byte 2**62: never data
    _ERROR_LOCK_VIOLATION, _ERROR_INVALID_PARAMETER, _STILL_ACTIVE = 33, 87, 259
    _CREATE_SUSPENDED, _CREATE_BREAKAWAY_FROM_JOB = 0x4, 0x01000000
    _PROCESS_TERMINATE, _PROCESS_SET_QUOTA, _PROCESS_SUSPEND_RESUME = 0x1, 0x100, 0x800
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _JOB_LIMIT_BREAKAWAY_OK, _JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x800, 0x2000

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p), ("Offset", wintypes.DWORD),
                    ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC_LIMIT), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _ACCOUNTING(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    for _name, _res, _args in (
            ("LockFileEx", wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                           wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]),
            ("UnlockFileEx", wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                             ctypes.POINTER(_OVERLAPPED)]),
            ("OpenProcess", wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
            ("CloseHandle", wintypes.BOOL, [wintypes.HANDLE]),
            ("GetExitCodeProcess", wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]),
            ("GetProcessTimes", wintypes.BOOL, [wintypes.HANDLE] + [ctypes.POINTER(ctypes.c_uint64)] * 4),
            ("CreateJobObjectW", wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
            ("SetInformationJobObject", wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]),
            ("QueryInformationJobObject", wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                          wintypes.DWORD, ctypes.c_void_p]),
            ("AssignProcessToJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
            ("TerminateJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT])):
        _function = getattr(_k32, _name)
        _function.restype, _function.argtypes = _res, _args
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtResumeProcess.restype, _ntdll.NtResumeProcess.argtypes = ctypes.c_long, [wintypes.HANDLE]

    def _range():
        return ctypes.byref(_OVERLAPPED(0, 0, 0, _LOCK_OFFSET_HIGH, None))


def lock_fd(fd: int, exclusive: bool = True, wait: bool = False) -> None:
    """flock(fd, LOCK_EX/LOCK_SH [| LOCK_NB]) on every system: BlockingIOError when another holder has it and wait is
    False. On Windows the locks of one process exclude each other too (flock: per open file), like a second open."""
    if fcntl is not None:
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | (0 if wait else fcntl.LOCK_NB))
        return
    flags = (2 if exclusive else 0) | (0 if wait else 1)  # LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY
    if not _k32.LockFileEx(msvcrt.get_osfhandle(fd), flags, 0, 1, 0, _range()):
        error = ctypes.get_last_error()
        if error == _ERROR_LOCK_VIOLATION:
            raise BlockingIOError(error, "locked by another holder")
        raise ctypes.WinError(error)


def unlock_fd(fd: int) -> None:
    """Frees the lock at once (Windows frees a closed handle's lock only "when resources allow")."""
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        _k32.UnlockFileEx(msvcrt.get_osfhandle(fd), 0, 1, 0, _range())


def read_at(fd: int, size: int, offset: int = 0) -> bytes:
    if hasattr(os, "pread"):
        return os.pread(fd, size, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, size)


def write_at(fd: int, data: bytes, offset: int = 0) -> None:
    if hasattr(os, "pwrite"):
        os.pwrite(fd, data, offset)
        return
    os.lseek(fd, offset, os.SEEK_SET)
    os.write(fd, data)


def pid_alive(pid: int) -> bool:
    """True while a process with that pid exists. Never os.kill(pid, 0) on Windows: there it is TerminateProcess."""
    if not WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER  # access denied: it exists
    try:
        code = wintypes.DWORD()
        return not _k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == _STILL_ACTIVE
    finally:
        _k32.CloseHandle(handle)


def process_started(pid: int) -> str | None:
    """Windows: the creation time of a live process (a reused pid gets another one), None when it cannot be read."""
    if not WINDOWS:
        return None
    handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        times = [ctypes.c_uint64() for _ in range(4)]
        if not _k32.GetProcessTimes(handle, *map(ctypes.byref, times)) or not pid_alive(pid):
            return None
        return "filetime %d" % times[0].value
    finally:
        _k32.CloseHandle(handle)


class _Job:
    """Windows' process group: a job object that the command joins while it is still suspended, so every process it
    starts belongs to it too. alive() while any of them runs. Kill on close: when the wrapper dies (TerminateProcess,
    a closed console), the job's processes die with it - the lock is never free while a part still runs (stricter than
    POSIX, where only a SIGKILL of the wrapper alone parts them). Breakaway allowed: a child that asks to leave
    (CREATE_BREAKAWAY_FROM_JOB, the counterpart of start_new_session) is not waited for. Without a job (assignment
    refused) only the command itself is waited for."""

    def __init__(self, child: subprocess.Popen):
        self.child, self.pid, self.handle, self.signals = child, child.pid, None, 0
        process = _k32.OpenProcess(_PROCESS_TERMINATE | _PROCESS_SET_QUOTA | _PROCESS_SUSPEND_RESUME, False, child.pid)
        try:
            job = _k32.CreateJobObjectW(None, None)
            if job and process:
                limits = _EXTENDED_LIMIT()
                limits.BasicLimitInformation.LimitFlags = _JOB_LIMIT_KILL_ON_JOB_CLOSE | _JOB_LIMIT_BREAKAWAY_OK
                if (_k32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                        and _k32.AssignProcessToJobObject(job, process)):
                    self.handle, job = job, None
            if job:
                _k32.CloseHandle(job)
        finally:
            resumed = bool(process) and _ntdll.NtResumeProcess(process) == 0
            if process:
                _k32.CloseHandle(process)
            if not resumed:  # never leave a suspended command behind
                child.kill()
                self.close()
                raise OSError("heavy lock: could not start %s (suspended start failed)" % child.args)

    def __str__(self):
        return str(self.pid)

    def alive(self) -> bool:
        if self.handle:
            info = _ACCOUNTING()
            if _k32.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                return info.ActiveProcesses > 0
        return self.child.poll() is None

    def signal(self, _signum) -> None:
        """First signal: Ctrl+Break for the command's console group (Python, sh, Blender end on it). Again, or without a
        shared console: the whole job ends (TerminateJobObject) - a GUI program such as Unity has no console."""
        self.signals += 1
        if self.signals == 1:
            try:
                os.kill(self.pid, signal.CTRL_BREAK_EVENT)
                return
            except OSError:
                pass
        if self.handle:
            _k32.TerminateJobObject(self.handle, 1)
        else:
            with contextlib.suppress(OSError):
                self.child.kill()

    def close(self) -> None:
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None


def posix_shell(name: str = "sh") -> str | None:
    """Windows: Git for Windows' sh or bash (next to git.exe), else one on PATH - never System32's bash.exe, which is
    WSL (another system with other paths). POSIX: the name itself."""
    if not WINDOWS:
        return name
    import shutil
    git = shutil.which("git")
    candidates = []
    if git:
        root = os.path.dirname(os.path.dirname(os.path.realpath(git)))  # …\Git\cmd\git.exe -> …\Git
        candidates += [os.path.join(root, "bin", name + ".exe"), os.path.join(root, "usr", "bin", name + ".exe")]
    candidates += [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", name + ".exe")]
    found = shutil.which(name)
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32").lower()
    if found and not os.path.dirname(os.path.realpath(found)).lower().startswith(system32):
        candidates.append(found)
    return next((path for path in candidates if os.path.isfile(path)), None)


def script_argv(argv: list) -> list:
    """argv as this system can start it. Windows starts only programs (a script is WinError 193): an existing script
    file with a shebang (sh/bash/zsh, python/python3) or ending in .sh goes through Git Bash or this Python; zsh
    scripts run in bash (there is no zsh). Unchanged on POSIX and for programs."""
    argv = list(argv)
    if not WINDOWS or not argv or not os.path.isfile(str(argv[0])):
        return argv
    script = str(argv[0])
    if os.path.splitext(script)[1].lower() in (".exe", ".com", ".bat", ".cmd"):
        return argv
    try:
        with open(script, "rb") as handle:
            first = handle.readline(256).decode("utf-8", "replace")
    except OSError:
        return argv
    words = first[2:].split() if first.startswith("#!") else []
    if words and os.path.basename(words[0]) == "env":
        words = [word for word in words[1:] if not word.startswith("-")]
    program = os.path.basename(words[0]).lower() if words else ""
    if program.startswith("python"):
        return [sys.executable, *argv]
    if program in ("bash", "zsh") or (not program and script.lower().endswith(".sh")) or program == "sh":
        shell = posix_shell("sh" if program == "sh" or not program else "bash")
        if shell:
            return [shell, *argv]
    return argv


def spawn_group(argv, **popen):
    """(child, group): the command in a process group of its own - POSIX start_new_session (its pid is the group id),
    Windows a job object (_Job) and a console group of its own (scripts through script_argv)."""
    if not WINDOWS:
        child = subprocess.Popen(argv, start_new_session=True, **popen)
        return child, child.pid
    flags = popen.pop("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED
    child = subprocess.Popen(script_argv(argv), creationflags=flags, **popen)
    return child, _Job(child)


def signal_group(group, signum) -> None:
    if isinstance(group, int):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(group, signum)
    else:
        group.signal(signum)


def close_group(group) -> None:
    if not isinstance(group, int):
        group.close()


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
    try:
        fd = os.open(lock_path(resource), os.O_RDONLY | OPEN_FLAGS)
    except OSError:
        return None
    try:
        try:
            lock_fd(fd, exclusive=False)
        except BlockingIOError:
            return describe(read_at(fd, 4096).decode("utf-8", "replace"))
        unlock_fd(fd)
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


# Who holds a lock, in words (0.24 "Nametag"): read from the holder line, the process table and the checkout's
# reservation at status time, so it also names holders written by older tool versions. Best effort: any failure
# leaves the part out, never the status.
CLIENTS = (("claude", "Claude Code"), ("codex", "Codex"), ("antigravity", "Antigravity"), ("gemini", "Gemini"),
           ("cursor", "Cursor"), ("terminal", "Terminal"), ("iterm", "iTerm"))
STALE_RESERVATION_S = 3 * 24 * 3600  # like ce_postfach: an older reservation may belong to an earlier session
COMMAND_CHARS = 160


WINDOWS_PS = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-CimInstance Win32_Process | ForEach-Object "
              "{ '{0} {1} {2}' -f $_.ProcessId, $_.ParentProcessId, $_.CommandLine }")


def process_table(runner=subprocess.run) -> dict:
    """pid -> (parent pid, command line) of every process (ps -A; Windows: Win32_Process, same line format); empty
    when the listing is missing or fails."""
    command = (["powershell", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_PS] if WINDOWS
               else ["ps", "-A", "-o", "pid=,ppid=,command="])
    try:
        out = runner(command, capture_output=True, text=True, timeout=20 if WINDOWS else 5,
                     **({"encoding": "utf-8", "errors": "replace"} if WINDOWS else {})).stdout
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    table = {}
    for line in (out or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return table


def client_of(pid: int, table: dict) -> str | None:
    """The agent or terminal the holder runs under: the first ancestor whose program name (or app bundle) is known."""
    seen = set()
    while pid in table and pid not in seen and pid > 1:
        seen.add(pid)
        ppid, command = table[pid]
        programs = [os.path.basename(token).lower() for token in command.split()[:2]]  # "node …/claude" counts too
        for key, name in CLIENTS:
            if any(program.startswith(key) for program in programs) or "/%s.app/" % key in command.lower():
                return name
        pid = ppid
    return None


def children_of(pid: int, table: dict, limit: int = 2) -> list:
    """Command lines of the holder's descendants, nearest first (what actually runs under the lock)."""
    found, level = [], [pid]
    for _depth in range(4):
        level = [child for child, (ppid, _cmd) in sorted(table.items()) if ppid in level]
        found += [table[child][1] for child in level if table[child][1]]
        if len(found) >= limit or not level:
            break
    return found[:limit]


def session_of(checkout: str | None, now: float | None = None) -> dict:
    """Reserved session name, model and branch of a checkout (<git-dir>/ce-session.json, else just the branch)."""
    if not checkout:
        return {}
    git = lambda *args: subprocess.run(["git", "-C", checkout, *args], capture_output=True, text=True,
                                       timeout=5).stdout.strip()
    try:
        git_dir, branch = git("rev-parse", "--absolute-git-dir"), git("branch", "--show-current")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    found = {"branch": branch} if branch else {}
    try:
        with open(os.path.join(git_dir, "ce-session.json"), encoding="utf-8") as handle:
            saved = json.load(handle)
        if isinstance(saved, dict) and saved.get("name"):
            found["name"] = str(saved["name"])
            if saved.get("model"):
                found["model"] = str(saved["model"])
            age = (now or time.time()) - float(saved.get("reserved_at") or 0)
            if age > STALE_RESERVATION_S:
                found["stale"] = True
    except (OSError, ValueError, TypeError):
        pass
    return found


def who(checkout: str | None, pid=None, table: dict | None = None) -> str:
    """'session: X (model) · branch b · client C' for a holder or waiter."""
    found = session_of(checkout)
    parts = []
    if found.get("name"):
        model = " (%s)" % found["model"] if found.get("model") else ""
        old = ", older reservation – may be an earlier session" if found.get("stale") else ""
        parts.append("session: %s%s%s" % (found["name"], model, old))
    else:
        parts.append("no reserved session")
    if found.get("branch"):
        parts.append("branch %s" % found["branch"])
    client = client_of(int(pid), table) if table and str(pid).isdigit() else None
    if client:
        parts.append("client %s" % client)
    return " · ".join(parts)


def running_for(since: str | None, now: float | None = None) -> str:
    try:
        minutes = ((now or time.time()) - time.mktime(time.strptime(since, "%Y-%m-%d %H:%M:%S"))) / 60
    except (TypeError, ValueError, OverflowError):
        return ""
    return "running %d min" % minutes if minutes >= 0 else ""


def details(resource: str | None = None, table: dict | None = None) -> list:
    """Lines under the holder of `resource`: who (session, branch, client, running time) and what runs."""
    try:
        with open(lock_path(resource), encoding="utf-8") as handle:
            info = json.loads(handle.read())
        if not isinstance(info, dict):
            return []
    except (OSError, ValueError):
        return []
    table = process_table() if table is None else table
    lines = ["; ".join(filter(None, (who(info.get("checkout"), info.get("pid"), table), running_for(info.get("since")))))]
    pid = info.get("pid")
    if isinstance(pid, int) and pid in table:
        lines.append("command: " + table[pid][1][:COMMAND_CHARS])
        lines += ["runs:    " + child[:COMMAND_CHARS] for child in children_of(pid, table)]
    elif isinstance(pid, int) and table:
        lines.append("PID %d is not in the process table (holder line may be stale)" % pid)
    return [line for line in lines if line]


def waiters(resource: str | None = None, table: dict | None = None) -> list:
    """'waiting: what (session …) since …' for every run with telemetry that waits for `resource`."""
    try:
        import ce_lock_status
        rows = ce_lock_status.waiting(lock_path(resource))
    except Exception:
        return []
    name = resource or "shared"
    return ["waiting: %s – %s, since %s UTC" % (row.get("what", "?"), who(row.get("checkout"), row.get("pid"), table),
                                                str(row.get("since", "?"))[:16].replace("T", " "))
            for row in rows if row.get("resource") == name]


def status_lines(table: dict | None = None) -> list:
    """`status`: one line per resource ('free' or the holder), then who and what for held ones and the waiters.
    shared is the old global lock: callers without a resource use it, resource runs pass it as a barrier (gpu
    exclusively), so while a gpu run holds it there is no name in the file."""
    lines = []
    for resource in (None,) + RESOURCES:
        name = resource or "shared"
        text = holder(resource)
        if text is None:
            lines.append("%-8s free" % name)
        else:
            if table is None:
                table = process_table()
            if resource is None and text == "unknown":
                gpu = holder("gpu")
                text = ("barrier, held exclusively by the gpu run below (old callers without a resource wait)" if gpu
                        else "barrier, held by a run that wrote no name")
                lines.append("%-8s %s" % (name, text))
            else:
                lines.append("%-8s %s" % (name, text))
                lines += ["%-8s %s" % ("", line) for line in details(resource, table)]
        lines += ["%-8s %s" % ("", line) for line in waiters(resource, table)]
    return lines


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
        if self.skipped:
            return self
        if self.resource == "gpu" and os.environ.get("CE_HEAVY_HELD"):
            raise ValueError("acquire exclusive gpu lock before other resources")
        path = lock_path(self.resource)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | OPEN_FLAGS, 0o644)  # never inherited (O_CLOEXEC, PEP 446)
        start = last = time.monotonic()
        announced = False
        waiter = None
        gate = None
        try:
            if self.resource:
                gate = os.open(os.environ.get("CE_HEAVY_LOCK_FILE") or DEFAULT, os.O_RDWR | os.O_CREAT | OPEN_FLAGS, 0o644)
            locks = ([(gate, self.resource == "gpu")] if gate is not None else []) + [(fd, True)]
            for descriptor, exclusive in locks:
              while True:
                try:
                    lock_fd(descriptor, exclusive)
                    break
                except BlockingIOError:
                    who = describe(read_at(descriptor, 4096).decode("utf-8", "replace"))
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
            if WINDOWS and gate is not None:  # a gate taken before the wait was interrupted
                with contextlib.suppress(OSError):
                    unlock_fd(gate)
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
        write_at(fd, (json.dumps(self.info) + "\n").encode())
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
            if WINDOWS:  # POSIX frees on close; Windows only "when resources allow" - free it now
                for descriptor in (self.fd, self._gate_fd):
                    if descriptor is not None:
                        with contextlib.suppress(OSError):
                            unlock_fd(descriptor)
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


def group_alive(pgid) -> bool:
    """True while any process of the group still exists (EPERM: one runs that this user may not signal). Windows: the
    group is a _Job (spawn_group)."""
    if not isinstance(pgid, int):
        return pgid.alive()
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
    child, group = spawn_group(argv, env=env, **popen)  # POSIX: its pid is the group id; Windows: a job object

    def forward(signum, _frame):
        signal_group(group, signum)

    previous = {}
    for sig in FORWARDED:
        try:
            previous[sig] = signal.signal(sig, forward)
        except ValueError:  # not the main thread
            pass
    try:
        while True:
            try:  # Windows: an endless wait (WaitForSingleObject) never runs the signal handlers - wait in steps
                code = child.wait(GROUP_POLL if WINDOWS else None)
                break
            except (KeyboardInterrupt, subprocess.TimeoutExpired):  # forwarded already; keep waiting for the child
                continue
        wait_groups([group], out,
                    lambda alive: "heavy lock: command ended (exit %s), the lock stays until the rest of its process "
                                  "group %d ends" % (code, child.pid))
        return code
    finally:
        close_group(group)
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

    SIGNALS = FORWARDED

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
            signal_group(pgid, signum)

    def popen(self, command: list[str], **popen) -> subprocess.Popen:
        """subprocess.Popen in a process group of its own, waited for when the block ends."""
        if self.signum is not None:
            raise Interrupted(self.signum)
        self.quiet = True  # a signal between fork and append would miss the new group
        try:
            child, group = spawn_group(command, **popen)  # POSIX: its pid is the group id; Windows: a job object
            self.pgids.append(group)
            self.children.append(child)
        finally:
            self.quiet = False
        if self.signum is not None:  # it came while the child started
            self._forward(self.signum, [group])
            raise Interrupted(self.signum)
        return child

    def __exit__(self, kind, error, _tb):
        self.quiet = True
        try:
            wait_groups(self.pgids, self.out,
                        lambda alive: "heavy lock: the lock stays until the process group(s) %s end"
                        % ", ".join(map(str, alive)), self.children)
        finally:
            for group in self.pgids:
                close_group(group)
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
        print("\n".join(status_lines()))
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
