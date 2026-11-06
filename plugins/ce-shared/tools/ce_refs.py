#!/usr/bin/env python3
"""ce_refs.py who|uses|missing|stats - asset references of a Unity project from its text files (0.41 Backlink).

    ./ce refs who PlayerController            # which prefabs, scenes and assets use it, with the field that holds it
    ./ce refs who PlayerController --depth 3  # also what uses those, e.g. the scene around the prefab
    ./ce refs uses Assets/Prefabs/Bot.prefab  # what this asset points at
    ./ce refs missing                         # references whose target no longer exists; exit 1 if there are any
    ./ce refs stats

Code tools see the C# and not the wiring: a serialized field is connected in a prefab or scene, by GUID. This reads that side.
It needs force-text serialization and nothing else: no Unity, no Library, no index and no cache, so every answer comes from the
files of this checkout and cannot be stale. Works on any Unity project (the root has `Assets/` and `ProjectSettings/`), read only.

An asset is named by its GUID, its path or its file name (`Bot`, `Bot.prefab`); a name with several matches lists them and exits 2.
Sources are all `%YAML` files, the `.meta` files (importer remaps, script defaults) and the text formats in JSON_LIKE.
Not covered, and `who` says so: references made by code (`Resources.Load`, addressable keys) and GUIDs in other data files. An empty
`who` therefore never proves an asset unused.

`missing` needs to know the packages. Their GUIDs live in `Library/PackageCache`, which a worktree does not have, so the cache of
the main checkout is used (read only), and in local `file:` packages of `Packages/manifest.json`. Exit 1 only for what is certainly
gone; undecided, with exit 0, is: everything without a cache, everything when the worktree's `packages-lock.json` differs from the
one of the cache, and scripts inside a DLL of the Unity installation (`m_Script` with another fileID than a `.cs` file has; old
projects hold UnityEngine.UI that way). Only the `{fileID, guid}` form counts here, a bare `guid:` may be a string of a data asset.
Files that cannot be read are skipped, counted in `stats` and named on stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

BUILTIN = {b"0" * 16 + c + b"0" * 15 for c in (b"d", b"e", b"f")}  # Unity's built-in resources, no file behind them
SCAN_ROOTS = ("Assets", "Packages", "ProjectSettings")
JSON_LIKE = {".asmdef", ".asmref", ".shadergraph", ".shadersubgraph", ".vfx", ".uxml", ".uss", ".tss"}
META_GUID = re.compile(rb"^guid: ([0-9a-f]{32})", re.M)
YAML_GUID = re.compile(rb"guid:[ \t]*([0-9a-f]{32})")
YAML_LEAD = re.compile(rb"^[ \t]*(?:-[ \t]+)?(?:([A-Za-z_][\w.]*):[ \t]*)?(?:\{fileID:[ \t]*(-?\d+),)?[ \t]*$")  # what stands before guid:
SCRIPT_FILE_ID = b"11500000"  # a MonoScript from a .cs file; a class inside a DLL has a hash here
JSON_REF = re.compile(rb"guid[\"':= \t]*([0-9a-f]{32})", re.I)
HEX32 = re.compile(r"[0-9a-f]{32}")


class Index:
    """All assets and references of one project: path by GUID, and for each source its targets with count and fields."""

    def __init__(self):
        self.path = {}          # guid -> project-relative path
        self.packages = set()   # guids of the package cache: they exist, the project just does not hold them
        self.edges = {}         # source guid -> {target guid: {"count": n, "fields": Counter, "strict": bool, "dll": bool}}
        self.scanned = Counter()
        self.binary = 0         # serialized files that are not text: their references stay invisible
        self.unreadable = []
        self.package_cache = None
        self.foreign_lock = False  # the cache belongs to other package versions than this checkout
        self.incoming = None
        self.has_cache = False

    def who(self, guid):
        if self.incoming is None:
            self.incoming = {}
            for source, edge in self.edges.items():
                for target, entry in edge.items():
                    self.incoming.setdefault(target, {})[source] = entry
        return self.incoming.get(guid, {})

    def uses(self, guid):
        return self.edges.get(guid, {})


def walk(root):
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.endswith("~")]  # the folders Unity itself ignores
        for name in files:
            yield Path(folder) / name


def read(index, path, size=-1):
    try:
        with open(path, "rb") as handle:
            return handle.read(size)
    except OSError:
        index.unreadable.append(str(path))
        return None


def yaml_refs(data):
    """(field, file id or None, target) per guid. Searching guid: first keeps this linear; Unity may wrap the line after `{fileID: n,`."""
    for match in YAML_GUID.finditer(data):
        start = data.rfind(b"\n", 0, match.start()) + 1
        lead = data[start:match.start()]
        if lead and lead[-1:] not in b" \t":
            continue  # part of a longer key such as my_guid:
        if not lead.strip() and start:
            above = data[data.rfind(b"\n", 0, start - 1) + 1:start - 1].rstrip(b"\r")
            lead = above if above.endswith(b",") else lead
        found = YAML_LEAD.match(lead)
        yield (found.group(1), found.group(2), match.group(1)) if found else (None, b"" if b"{fileID:" in lead else None, match.group(1))


def json_refs(data):
    return ((None, b"", match.group(1)) for match in JSON_REF.finditer(data))


def add_refs(index, source, refs):
    edge = index.edges.setdefault(source, {})
    for field, file_id, target in refs:
        if target == source or target in BUILTIN:
            continue
        entry = edge.setdefault(target, {"count": 0, "fields": Counter(), "strict": False, "dll": False})
        entry["count"] += 1
        entry["strict"] = entry["strict"] or file_id is not None
        entry["dll"] = entry["dll"] or (field == b"m_Script" and file_id not in (None, b"", SCRIPT_FILE_ID))
        if field:
            entry["fields"][field.decode()] += 1


def package_cache(project):
    """Library/PackageCache of this project, else of the main checkout this worktree belongs to."""
    own = project / "Library" / "PackageCache"
    if own.is_dir():
        return own
    try:
        common = subprocess.run(["git", "-C", str(project), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=20).stdout.strip()
        prefix = subprocess.run(["git", "-C", str(project), "rev-parse", "--show-prefix"],
                                capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    cache = Path(common).parent / prefix / "Library" / "PackageCache" if common else None
    return cache if cache and cache.is_dir() else None


def local_packages(project):
    """Folders of the `file:` packages in Packages/manifest.json; they lie outside the project and outside the cache."""
    try:
        dependencies = json.loads((project / "Packages" / "manifest.json").read_text(encoding="utf-8")).get("dependencies", {})
    except (OSError, ValueError, AttributeError):
        return []
    folders = [project / "Packages" / value[5:] for value in dependencies.values() if isinstance(value, str) and value.startswith("file:")]
    return [folder for folder in folders if folder.is_dir()]


def lock_differs(project, cache):
    theirs = Path(cache).parent.parent / "Packages" / "packages-lock.json"
    ours = project / "Packages" / "packages-lock.json"
    try:
        return theirs.resolve() != ours.resolve() and theirs.read_bytes() != ours.read_bytes()
    except OSError:
        return False


def build(project, cache=None):
    index = Index()
    project = Path(project)
    metas, others = [], []
    for root in SCAN_ROOTS:
        for path in walk(project / root):
            (metas if path.suffix == ".meta" else others).append(path)
    for meta in metas:
        data = read(index, meta)
        found = META_GUID.search(data) if data is not None else None
        if found:
            guid = found.group(1)
            index.path[guid] = meta.relative_to(project).as_posix()[:-5]
            add_refs(index, guid, yaml_refs(data))
            index.scanned[".meta"] += 1
    by_path = {path: guid for guid, path in index.path.items()}
    for path in others:
        relative = path.relative_to(project).as_posix()
        suffix = path.suffix.lower()
        head = read(index, path, 5)
        if head is None:
            continue
        if head != b"%YAML" and suffix not in JSON_LIKE:
            index.binary += suffix in (".prefab", ".unity", ".asset", ".mat", ".anim", ".controller")
            continue
        guid = by_path.get(relative) or relative.encode()  # ProjectSettings has no .meta: the path is the source then
        index.path.setdefault(guid, relative)
        data = read(index, path)
        if data is not None:
            add_refs(index, guid, yaml_refs(data) if head == b"%YAML" else json_refs(data))
            index.scanned[suffix] += 1
    index.package_cache = Path(cache) if cache else package_cache(project)
    has_cache = bool(index.package_cache and index.package_cache.is_dir())
    index.foreign_lock = has_cache and not cache and lock_differs(project, index.package_cache)
    for folder in ([index.package_cache] if has_cache else []) + local_packages(project):
        for path in walk(folder):
            if path.suffix == ".meta":
                found = META_GUID.search(read(index, path, 400) or b"")
                if found:
                    index.packages.add(found.group(1))
    index.has_cache = has_cache
    return index


def resolve(index, query, project="."):
    """GUID, path or file name -> list of guids; more than one entry means the name is ambiguous."""
    if HEX32.fullmatch(query):
        return [query.encode()]
    plain = query.replace("\\", "/").removeprefix("./")
    try:
        texts = [Path(plain).resolve().relative_to(Path(project).resolve()).as_posix(), plain]  # first as a path from the shell
    except (OSError, ValueError):
        texts = [plain]
    for text in texts:
        wanted = text.lower()
        hits = ([g for g, p in index.path.items() if p.lower() == wanted]
                or [g for g, p in index.path.items() if wanted in (Path(p).name.lower(), Path(p).stem.lower())]
                or [g for g, p in index.path.items() if p.lower().endswith("/" + wanted)])
        if hits:
            return hits
    return []


def label(index, guid):
    return index.path.get(guid) or guid.decode()


def row(index, guid, entry, depth=1):
    fields = [name for name, _ in entry["fields"].most_common(4)]
    return {"asset": label(index, guid), "guid": guid.decode(), "depth": depth, "count": entry["count"], "fields": fields}


def trace(index, start, step, depth):
    """Breadth-first over `step` (who or uses); every asset once, at its smallest distance."""
    seen, level, rows = {start}, [start], []
    for distance in range(1, depth + 1):
        following = []
        for guid in level:
            for other, entry in sorted(step(guid).items(), key=lambda item: label(index, item[0])):
                if other not in seen:
                    seen.add(other)
                    following.append(other)
                    rows.append(row(index, other, entry, distance))
        level = following
    return rows


def missing(index):
    """Returns (gone, undecided): {target: [sources]} for strict references to nothing the project holds."""
    gone, undecided = {}, {}
    unsure = not index.has_cache or index.foreign_lock
    for source, edge in index.edges.items():
        for target, entry in edge.items():
            if entry["strict"] and target not in index.path and target not in index.packages:
                (undecided if unsure or entry["dll"] else gone).setdefault(target, []).append(source)
    return gone, undecided


def print_rows(rows):
    for item in rows:
        via = f"  über {', '.join(item['fields'])}" if item["fields"] else ""
        print(f"{'  ' * item['depth']}{item['asset']}{' ×' + str(item['count']) if item['count'] > 1 else ''}{via}")


def report_missing(index, as_json):
    gone, undecided = missing(index)
    if as_json:
        as_rows = lambda d: {t.decode(): sorted(label(index, s) for s in v) for t, v in d.items()}  # noqa: E731
        print(json.dumps({"missing": as_rows(gone), "undecided": as_rows(undecided)}, indent=1, ensure_ascii=False))
    else:
        for target, sources in sorted(gone.items(), key=lambda item: -len(item[1])):
            print(f"{target.decode()}  fehlt, genutzt von {len(sources)}:")
            for source in sorted(label(index, s) for s in sources)[:5]:
                print(f"  {source}")
        if undecided:
            why = ("kein Library/PackageCache gefunden (--package-cache)" if not index.has_cache else
                   "packages-lock.json weicht vom Paketcache ab" if index.foreign_lock else "Skripte in einer DLL der Unity-Installation")
            print(f"{len(undecided)} GUIDs nicht entscheidbar: {why}.")
        elif not gone:
            print("Keine fehlenden Referenzen.")
    return 1 if gone else 0


def report_stats(index, as_json):
    gone, undecided = missing(index)
    data = {"assets": len(index.path), "scanned": dict(index.scanned.most_common()),
            "references": sum(len(edge) for edge in index.edges.values()), "binary_skipped": index.binary, "unreadable": len(index.unreadable),
            "package_guids": len(index.packages), "package_cache": str(index.package_cache or ""),
            "missing": len(gone), "undecided": len(undecided)}
    print(json.dumps(data, indent=1, ensure_ascii=False) if as_json else "\n".join(f"{k}: {v}" for k, v in data.items()))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="./ce refs", description="Asset-Referenzen eines Unity-Projekts aus seinen Textdateien.")
    parser.add_argument("command", choices=["who", "uses", "missing", "stats"])
    parser.add_argument("asset", nargs="?", help="GUID, Pfad oder Dateiname (who, uses)")
    parser.add_argument("--project", default=".", help="Projektwurzel mit Assets/ und ProjectSettings/")
    parser.add_argument("--depth", type=int, default=1, help="so viele Stufen weiter verfolgen (who, uses)")
    parser.add_argument("--package-cache", help="statt Library/PackageCache dieses Projekts oder des Haupt-Checkouts")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    project = Path(args.project)
    if not (project / "Assets").is_dir() or not (project / "ProjectSettings").is_dir():
        print(f"{project.resolve()} ist kein Unity-Projekt (Assets/ und ProjectSettings/ fehlen); --project setzen.", file=sys.stderr)
        return 2
    if args.command in ("who", "uses") and not args.asset:
        parser.error(f"{args.command} braucht ein Asset")
    index = build(project, args.package_cache)
    for path in index.unreadable[:5]:
        print(f"nicht lesbar, übersprungen: {path}", file=sys.stderr)
    if args.command == "missing":
        return report_missing(index, args.json)
    if args.command == "stats":
        return report_stats(index, args.json)
    hits = resolve(index, args.asset, project)
    if len(hits) != 1:
        print(f"{len(hits)} Treffer für {args.asset}" + (", bitte genauer:" if hits else "."), file=sys.stderr)
        for guid in hits[:20]:
            print(f"  {label(index, guid)}", file=sys.stderr)
        return 2
    step = index.who if args.command == "who" else index.uses
    rows = trace(index, hits[0], step, max(args.depth, 1))
    if args.json:
        print(json.dumps({"asset": label(index, hits[0]), "guid": hits[0].decode(), args.command: rows}, indent=1, ensure_ascii=False))
        return 0
    print(label(index, hits[0]))
    print_rows(rows)
    if args.command == "who":
        print(f"{len(rows)} Treffer in Dateien. Code-Zugriffe (Resources.Load, Addressables) sind nicht erfasst.")
    return 0


if __name__ == "__main__":
    if os.name == "nt":  # a console in cp1252 must not end the run on a Japanese asset name
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    sys.exit(main())
