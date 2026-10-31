"""Optional waiter telemetry. It never decides or changes ownership of a resource lock."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import uuid


def identity(pid):
    if os.name == 'nt':  # no ps -o lstart: the creation time (0.31)
        try:
            import heavy_lock
            return heavy_lock.process_started(pid)
        except (ImportError, OSError, ValueError):
            return None
    try:
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True, text=True, timeout=2)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def folder(lock_path):
    return Path(lock_path).parent / '.ce-lock-waiters'


def announce(lock_path, resource, what, checkout, blocking_path):
    """Best effort: missing telemetry must never weaken or block the real flock."""
    try:
        root = folder(lock_path)
        if root.is_symlink():
            return None
        root.mkdir(mode=0o700, exist_ok=True)
        started = identity(os.getpid())
        if not started:
            return None
        target = root / (str(os.getpid()) + '-' + uuid.uuid4().hex + '.json')
        data = dict(version=1, pid=os.getpid(), process_start=started, resource=resource or 'shared',
                    what=what, checkout=checkout, blocking_path=blocking_path,
                    since=datetime.now(timezone.utc).isoformat())
        with target.open('x', encoding='utf-8') as stream:
            json.dump(data, stream)
        return target
    except (OSError, ValueError, TypeError):
        return None


def forget(path):
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def waiting(lock_path):
    """Only live, identity-matching records; older clients do not publish records."""
    result = []
    root = folder(lock_path)
    if root.is_symlink():
        return result
    for path in root.glob('*.json'):
        try:
            if path.is_symlink() or path.stat().st_size > 16384:
                continue
            data = json.loads(path.read_text(encoding='utf-8'))
            if (data.get('version') != 1 or type(data.get('pid')) is not int or data['pid'] <= 0
                    or data.get('resource') not in ('shared', 'unity', 'blender', 'gpu')
                    or not isinstance(data.get('process_start'), str)
                    or identity(data['pid']) != data['process_start']):
                continue
            stamp = datetime.fromisoformat(data['since'])
            if stamp.tzinfo is None:
                continue
            result.append(data)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return sorted(result, key=lambda row: row['since'])
