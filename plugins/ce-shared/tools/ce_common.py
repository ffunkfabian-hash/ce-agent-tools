"""Shared helpers of the ce-shared tools: finding and reading .ce-repo.json, the repo version, small git helpers."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

CONFIG_NAME = ".ce-repo.json"


class RepoError(Exception):
    """A problem with the repo or its .ce-repo.json, worded for the user."""


def find_root(start: str | Path | None = None) -> Path:
    """The directory with .ce-repo.json: `start` (default: cwd) or the nearest parent that has one."""
    here = Path(start or ".").resolve()
    for folder in (here, *here.parents):
        if (folder / CONFIG_NAME).is_file():
            return folder
    raise RepoError(f"{CONFIG_NAME} not found in {here} or above")


def load_config(root: Path) -> dict:
    try:
        config = json.loads((root / CONFIG_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RepoError(f"{CONFIG_NAME} in {root} unreadable: {error}") from error
    if not isinstance(config, dict):
        raise RepoError(f"{CONFIG_NAME} in {root} is not a JSON object")
    return config


def open_repo(repo: str | None) -> tuple[Path, dict]:
    root = find_root(repo)
    return root, load_config(root)


def read_version(root: Path, config: dict) -> str:
    """The version from the file/pattern in config["version"] (group 1 of the pattern)."""
    spec = config.get("version")
    if not isinstance(spec, dict) or "file" not in spec or "pattern" not in spec:
        raise RepoError('.ce-repo.json: "version" needs "file" and "pattern"')
    path = root / spec["file"]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RepoError(f"version file {spec['file']} unreadable: {error}") from error
    try:
        match = re.search(spec["pattern"], text)
    except re.error as error:
        raise RepoError(f"version pattern invalid: {error}") from error
    if not match or not match.groups():
        raise RepoError(f"version pattern does not match in {spec['file']}")
    return match.group(1)


def git(root: Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout)


def git_out(root: Path, *args: str, timeout: float = 30) -> str | None:
    """stdout of a git command stripped, None when it failed."""
    try:
        done = git(root, *args, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None
