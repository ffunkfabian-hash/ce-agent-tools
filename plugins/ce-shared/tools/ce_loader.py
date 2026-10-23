#!/usr/bin/env python3
"""ce - loader of the shared tools for a repo without an embedded copy (ce-agent-tools 0.20 "Lodestar").

A repo names the tools it was checked with in .ce-repo.json:

    "tools_version": {"version": "0.20.0", "commit": "<40 hex digits of ce-agent-tools>"}

and keeps this file as its ./ce (ce_sync.py writes it with a checksum header; it is the only file from ce-agent-tools in
the repo). `./ce <command>` runs `ce <command>` of exactly those tools, taken from the first of
  1. CE_TOOLS (a plugins/ce-shared/tools folder) - used as it is, another version is named on stderr;
  2. the installed Claude Code plugin ce-shared@ce-agent-tools, when installed_plugins.json records this commit;
  3. the cache ~/.cache/ce-agent-tools/<version>-<commit12> (CE_TOOLS_CACHE), read-only, every file (also __pycache__)
     checked against its manifest; tools run from it with PYTHONPYCACHEPREFIX=<cache>/.pycache, so Python never
     writes or reads bytecode inside a checked version (read-only does not stop root in cloud sessions);
  4. a download of plugins/ce-shared from the public repo into that cache: GitHub's tarball (the commit must be the
     archive's pax comment), else `git fetch` of the commit. A changed cache is fetched again, never used.
Standard library only, messages on stderr (stdout stays the tool's, e.g. the MCP server of `./ce search serve`).
Without tools and without network it stops with exit 3 and says what to do; a wrong tools_version is exit 2.

    ./ce tools where          folder and source of the tools (CE_TOOLS, plugin, cache, download)
    ./ce tools fetch          fill the cache for tools_version, even when CE_TOOLS or the plugin would serve (setup scripts)
    ./ce tools pin <commit>   set tools_version to this ce-agent-tools commit and write its loader as ./ce
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = "ffunkfabian-hash/ce-agent-tools"
TARBALL_URL = "https://codeload.github.com/" + REPO + "/tar.gz/{commit}"
GIT_URL = "https://github.com/" + REPO + ".git"
PLUGIN_KEY = "ce-shared@ce-agent-tools"
SUBTREE = ("plugins", "ce-shared")
CONFIG_NAME = ".ce-repo.json"
MANIFEST = ".ce-manifest.json"
CA_BUNDLES = ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt")  # python.org Python on macOS ships none
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)*$")
MAX_DOWNLOAD = 64 * 1024 * 1024
TIMEOUT = 60
EXIT_CONFIG, EXIT_UNAVAILABLE = 2, 3


class LoaderError(Exception):
    """A reason the tools cannot be provided, worded for the user; `code` is the exit code."""

    def __init__(self, message: str, code: int = EXIT_UNAVAILABLE):
        super().__init__(message)
        self.code = code


def say(text: str) -> None:
    print(f"ce: {text}", file=sys.stderr)


def find_root(start: Path) -> Path:
    for folder in (start, *start.parents):
        if (folder / CONFIG_NAME).is_file():
            return folder
    raise LoaderError(f"{CONFIG_NAME} nicht gefunden in {start} oder darüber", EXIT_CONFIG)


def load_config(root: Path) -> dict:
    try:
        config = json.loads((root / CONFIG_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LoaderError(f"{root / CONFIG_NAME} unlesbar: {error}", EXIT_CONFIG) from error
    if not isinstance(config, dict):
        raise LoaderError(f"{root / CONFIG_NAME} ist kein JSON-Objekt", EXIT_CONFIG)
    return config


def parse_pin(config: dict) -> tuple[str, str]:
    """(version, commit) of .ce-repo.json › tools_version; LoaderError (exit 2) when missing or malformed."""
    value = config.get("tools_version")
    shape = 'tools_version muss {"version": "0.20.0", "commit": "<40 Hex-Zeichen>"} sein'
    if not isinstance(value, dict):
        raise LoaderError(f"{CONFIG_NAME}: {shape}", EXIT_CONFIG)
    version, commit = value.get("version"), value.get("commit")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise LoaderError(f"{CONFIG_NAME}: tools_version.version {version!r} ungültig - {shape}", EXIT_CONFIG)
    if not isinstance(commit, str) or not COMMIT_RE.match(commit):
        raise LoaderError(f"{CONFIG_NAME}: tools_version.commit {commit!r} ungültig (voller Commit, kein Tag) - {shape}",
                          EXIT_CONFIG)
    return version, commit


def plugin_version(tools: Path) -> str | None:
    try:
        return json.loads((tools.parent / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def installed_plugin(version: str, commit: str, env) -> Path | None:
    """tools/ of an installed ce-shared whose entry in installed_plugins.json has exactly this commit and version."""
    config_dir = Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        data = json.loads((config_dir / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
        entries = data["plugins"][PLUGIN_KEY]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("gitCommitSha") != commit or entry.get("version") != version:
            continue
        tools = Path(str(entry.get("installPath") or "")) / "tools"
        if entry.get("installPath") and (tools / "ce").is_file() and plugin_version(tools) == version:
            return tools
    return None


def cache_root(env) -> Path:
    if env.get("CE_TOOLS_CACHE"):
        return Path(env["CE_TOOLS_CACHE"]).expanduser()
    return Path(env.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ce-agent-tools"


def cache_dir(env, version: str, commit: str) -> Path:
    return cache_root(env) / f"{version}-{commit[:12]}"


def tree_files(folder: Path) -> dict[str, str]:
    """{relative path: sha256} of every file below `folder` except the manifest; links count as changes. __pycache__ counts
    too: tools run from the cache with PYTHONPYCACHEPREFIX (run_env), so a .pyc in it did not come from the loader."""
    files = {}
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if rel.as_posix() == MANIFEST:
            continue
        if path.is_symlink():
            files[rel.as_posix()] = "symlink"
        elif path.is_file():
            files[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def cache_problem(folder: Path, version: str, commit: str) -> str | None:
    """None when `folder` is an intact cache of this version and commit, else what is wrong with it."""
    try:
        manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
        expected = manifest["files"]
    except (OSError, ValueError, KeyError, TypeError):
        return "Manifest fehlt oder unlesbar"
    if not isinstance(expected, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in expected.items()):
        return "Manifest ohne gültige Dateiliste"
    if manifest.get("commit") != commit or manifest.get("version") != version:
        return f"Manifest nennt {manifest.get('version')} ({str(manifest.get('commit'))[:12]})"
    actual = tree_files(folder)
    for name in sorted(set(expected) | set(actual)):
        if expected.get(name) != actual.get(name):
            return f"{name} {'fehlt' if name not in actual else 'unbekannt' if name not in expected else 'verändert'}"
    return None


def make_writable(folder: Path) -> None:
    for path in [folder, *folder.rglob("*")]:
        if not path.is_symlink():
            path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))


def remove_tree(folder: Path) -> None:
    """Delete a cache folder (ours, read-only by design)."""
    if folder.is_symlink() or folder.is_file():
        folder.unlink()
    elif folder.exists():
        make_writable(folder)
        shutil.rmtree(folder)


def make_read_only(folder: Path) -> None:
    """Files and folders read-only, so nothing is edited in the cache by mistake (and Python writes no __pycache__)."""
    for path in sorted(folder.rglob("*"), reverse=True):
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod((mode & ~0o222) | (0o555 if path.is_dir() else 0o444 | (mode & 0o111)))
    folder.chmod(0o555)


def _read_url(url: str, context) -> bytes:
    with urllib.request.urlopen(url, timeout=TIMEOUT, context=context) as response:  # noqa: S310 - fixed or test URL
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise LoaderError(f"{url}: Archiv größer als {MAX_DOWNLOAD // 2**20} MB")
    return data


def download(commit: str, env) -> bytes:
    """GitHub's tarball of the commit; retries with the system CA bundle when Python has no certificates of its own."""
    url = env.get("CE_TOOLS_URL", TARBALL_URL).format(commit=commit)
    try:
        return _read_url(url, None)
    except urllib.error.URLError as error:
        if not isinstance(getattr(error, "reason", None), ssl.SSLCertVerificationError):
            raise
        for bundle in CA_BUNDLES:
            if Path(bundle).is_file():
                return _read_url(url, ssl.create_default_context(cafile=bundle))
        raise


def git_archive(commit: str, env) -> bytes:
    """`git fetch` of exactly this commit, then `git archive` of plugins/ce-shared (pax comment = commit, as GitHub's)."""
    git = shutil.which("git")
    if not git:
        raise LoaderError("git fehlt")
    url = env.get("CE_TOOLS_GIT", GIT_URL)
    quiet = dict(env, GIT_TERMINAL_PROMPT="0")
    with tempfile.TemporaryDirectory(prefix="ce-tools-git-") as scratch:
        def run(*args: str) -> bytes:
            done = subprocess.run([git, "-C", scratch, *args], capture_output=True, timeout=TIMEOUT * 2, env=quiet)
            if done.returncode:
                raise LoaderError(f"git {args[0]}: {' '.join(done.stderr.decode(errors='replace').split())[-300:]}")
            return done.stdout
        run("init", "-q")
        run("fetch", "-q", "--depth", "1", url, commit)
        got = run("rev-parse", "FETCH_HEAD^{commit}").decode().strip()
        if got != commit:
            raise LoaderError(f"git lieferte {got[:12]} statt {commit[:12]}")
        return run("archive", "--format=tar", f"--prefix=ce-agent-tools-{commit}/", "FETCH_HEAD", "/".join(SUBTREE))


def extract(data: bytes, target: Path, commit: str) -> None:
    """plugins/ce-shared of the archive into `target`; the archive must name `commit` and hold only files and folders."""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError as error:
        raise LoaderError(f"Archiv unlesbar: {error}") from error
    with tar:
        if tar.pax_headers.get("comment") != commit:
            raise LoaderError(f"Archiv gehört zu {str(tar.pax_headers.get('comment'))[:12]!r}, nicht zu {commit[:12]}")
        for member in tar.getmembers():
            parts = member.name.split("/")
            if tuple(parts[1:3]) != SUBTREE or len(parts) <= 3 or parts[3:] == [""]:
                continue
            sub = parts[3:]
            if member.name.startswith("/") or any(part in ("", ".", "..") for part in sub):
                raise LoaderError(f"Archiv enthält unsicheren Pfad {member.name}")
            path = target.joinpath(*sub)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(tar.extractfile(member).read())
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
            else:
                raise LoaderError(f"Archiv enthält {member.name}, weder Datei noch Ordner")
    if not (target / "tools" / "ce").is_file():
        raise LoaderError(f"Commit {commit[:12]} enthält kein {'/'.join(SUBTREE)}/tools/ce")


def populate(env, version: str | None, commit: str) -> Path:
    """Fetch the commit into the cache (atomically) and return its folder; version None = take it from plugin.json."""
    errors = []
    data = None
    for name, fetch in (("Download", download), ("git", git_archive)):
        try:
            data = fetch(commit, env)
            break
        except (OSError, ValueError, LoaderError, subprocess.SubprocessError) as error:
            errors.append(f"{name}: {getattr(error, 'reason', None) or error}")
    if data is None:
        raise LoaderError("Laden gescheitert (" + "; ".join(errors) + ")")
    root = cache_root(env)
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".tmp-", dir=root))
    try:
        extract(data, staging, commit)
        have = plugin_version(staging / "tools")
        if not have or (version is not None and have != version):
            raise LoaderError(f"Commit {commit[:12]} enthält ce-shared {have}, {CONFIG_NAME} nennt {version}", EXIT_CONFIG)
        files = tree_files(staging)
        (staging / MANIFEST).write_text(json.dumps({"version": have, "commit": commit, "files": files}, indent=1) + "\n",
                                        encoding="utf-8")
        make_read_only(staging)
        final = cache_dir(env, have, commit)
        if final.exists() and cache_problem(final, have, commit) is not None:
            remove_tree(final)
        try:
            staging.rename(final)
        except OSError:
            if cache_problem(final, have, commit) is not None:  # a parallel fetch won only if its result is intact
                raise
        return final
    finally:
        remove_tree(staging)


def resolve(root: Path, config: dict, env, fetch: bool = True) -> tuple[Path, str]:
    """(tools folder, source) for this repo: CE_TOOLS, plugin, cache or download - see the module docstring."""
    if env.get("CE_TOOLS"):
        tools = Path(env["CE_TOOLS"]).expanduser()
        if not (tools / "ce").is_file():
            raise LoaderError(f"CE_TOOLS={tools} ist kein Werkzeugordner (ce fehlt)")
        if "tools_version" in config:
            want, have = parse_pin(config)[0], plugin_version(tools)
            if have != want:
                say(f"CE_TOOLS hat ce-agent-tools {have}, {CONFIG_NAME} nennt {want}")
        return tools, "CE_TOOLS"
    if "tools_version" not in config and config.get("tools_dir"):
        return root / config["tools_dir"], "tools_dir"  # a repo that still has its embedded copy
    version, commit = parse_pin(config)
    tools = installed_plugin(version, commit, env)
    if tools:
        return tools, "plugin"
    folder = cache_dir(env, version, commit)
    problem = cache_problem(folder, version, commit) if folder.exists() else None
    if folder.exists() and problem is None:
        return folder / "tools", "cache"
    where = f"ce-agent-tools {version} ({commit[:12]})"
    if problem:
        say(f"Cache {folder} verändert ({problem}) - wird neu geholt")
    if not fetch:
        raise LoaderError(f"{where} nicht im Cache {folder}")
    try:
        folder = populate(env, version, commit)
    except LoaderError as error:
        if error.code != EXIT_UNAVAILABLE:
            raise
        raise LoaderError(f"{where} nicht verfügbar: kein CE_TOOLS, kein Plugin {PLUGIN_KEY} mit diesem Commit, "
                          f"{'kein gültiger' if problem else 'kein'} Cache in {folder}; {error}. Abhilfe: mit Netz erneut "
                          f"aufrufen oder CE_TOOLS=<ce-agent-tools>/plugins/ce-shared/tools setzen.") from error
    say(f"{where} geladen nach {folder}")
    return folder / "tools", "download"


def fetch_cache(config: dict, env) -> tuple[Path, str]:
    """The cache of tools_version, filled when missing or changed - regardless of CE_TOOLS and the plugin."""
    version, commit = parse_pin(config)
    folder = cache_dir(env, version, commit)
    problem = cache_problem(folder, version, commit) if folder.exists() else "fehlt"
    if problem is None:
        return folder / "tools", "cache"
    if problem != "fehlt":
        say(f"Cache {folder} verändert ({problem}) - wird neu geholt")
    return populate(env, version, commit) / "tools", "download"


def pin(root: Path, config: dict, commit: str, env) -> int:
    """Write tools_version for `commit` (fetched into the cache to learn its version) and that commit's loader as ./ce."""
    if not COMMIT_RE.match(commit):
        raise LoaderError(f"{commit!r} ist kein voller Commit (40 Hex-Zeichen; Tag auflösen: git rev-parse <tag>)", EXIT_CONFIG)
    tools = populate(env, None, commit) / "tools"
    version = plugin_version(tools)
    if not (tools / "ce_loader.py").is_file() or not (tools / "ce_sync.py").is_file():
        raise LoaderError(f"Commit {commit[:12]} (ce-agent-tools {version}) hat keinen Lader - erst ab 0.20", EXIT_CONFIG)
    config = dict(config, tools_version={"version": version, "commit": commit})
    config.pop("tools_dir", None)
    (root / CONFIG_NAME).write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    sys.path.insert(0, str(tools))  # ce_sync of the pinned commit writes its own loader as ./ce
    import ce_sync
    ce_sync.sync(root, config, tools)
    print(f"tools_version: {version} ({commit[:12]}), ./ce = Lader dieses Stands")
    return 0


def run_env(env, source: str) -> dict:
    """Environment for the tools. From the cache, Python keeps its bytecode in a folder next to the checked versions
    (PYTHONPYCACHEPREFIX): the read-only mode does not stop root (cloud sessions), and a __pycache__ inside the cache
    would fail its check on the next call - with the prefix Python neither writes nor reads one there. Always ours: an
    inherited prefix could point into the cache."""
    env = dict(env)
    if source in ("cache", "download"):
        env["PYTHONPYCACHEPREFIX"] = str(cache_root(env) / ".pycache")
    return env


def main(argv: list[str], env=None) -> int:
    env = os.environ if env is None else env
    here = Path(__file__).resolve().parent
    try:
        root = find_root(here if (here / CONFIG_NAME).is_file() else Path.cwd().resolve())
        config = load_config(root)
        if argv[:1] == ["tools"]:
            sub = argv[1] if len(argv) > 1 else ""
            if sub == "pin" and len(argv) == 3:
                return pin(root, config, argv[2], env)
            if sub not in ("where", "fetch"):
                print("ce tools where | fetch | pin <commit>")
                return 2
            tools, source = fetch_cache(config, env) if sub == "fetch" else resolve(root, config, env)
            print(f"{tools} ({source}, ce-agent-tools {plugin_version(tools) or '?'})")
            return 0
        tools, source = resolve(root, config, env)
    except LoaderError as error:
        say(str(error))
        return error.code
    os.chdir(root)
    os.execvpe("sh", ["sh", str(tools / "ce"), *argv], run_env(env, source))
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
