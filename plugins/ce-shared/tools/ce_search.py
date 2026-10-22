#!/usr/bin/env python3
"""ce_search.py - local retrieval over the docs of all Cube Escape repos (RAG without a cloud).

0.5.0: the index is a SQLite file with FTS5 (BM25) over chunks of Markdown (docs, skills, AGENTS, version reports, LESSONS),
JSON data and the doc comments of code; optional dense vectors when `sentence-transformers` is installed (hybrid rank).
The nightly run rebuilds it; a session asks it instead of reading 12 000-word docs:

    python3 ce_search.py index [--all | --repo PATH ...]          build/refresh (~seconds per repo; content-hashed, incremental)
    python3 ce_search.py query "Z-Fighting Determinismus" [-k 8] [--repo toolgen] [--kind lesson|doc|skill|version|code]
    python3 ce_search.py serve                                     MCP server (stdio): tools ce_search, ce_search_status
    python3 ce_search.py status

Index: ~/Library/Caches/CubeEscape/search/index.sqlite (CE_SEARCH_INDEX moves it). Repos: --repo, CE_REPOS, or the standard
checkouts under ~/Claude. A hit returns repo, path, heading, lines and the chunk text, so the agent cites the source and reads
only that part. Pure standard library; embeddings are optional (CE_SEARCH_EMBED=0 turns them off even when installed).

Claude Code / Codex: add to .mcp.json  {"mcpServers": {"ce-search": {"command": "python3", "args": ["<tools>/ce_search.py", "serve"]}}}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from ce_common import load_config, RepoError

HOME = Path.home()
DEFAULT_INDEX = HOME / "Library" / "Caches" / "CubeEscape" / "search" / "index.sqlite"
DEFAULT_REPOS = ["cubeescape", "cubeescape-toolgen", "cubeescape-robogen", "cubeescape-cubegen", "cubeescape-formgen",
                 "cubeescape-motiongen", "cubeescape-nexogen", "ce-agent-tools"]
SKIP_DIRS = {".git", "Library", "Temp", "Logs", "obj", "bin", "node_modules", "__pycache__", "output", "outputs", "work",
             ".claude/worktrees", "Packages", "ProjectSettings", "UserSettings", "archive"}
MAX_CHUNK = 1800          # characters per chunk (about 400 tokens)
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


# ----------------------------------------------------------------------------------------------------------------- sources
def repos(explicit) -> list[Path]:
    if explicit:
        return [Path(p).expanduser().resolve() for p in explicit]
    env = os.environ.get("CE_REPOS")
    if env:
        return [Path(p).expanduser().resolve() for p in env.split(":") if p]
    base = Path(os.environ.get("CE_HOME", HOME / "Claude"))
    from ce_common import standard_repos
    return standard_repos(DEFAULT_REPOS, HOME)


def kind_of(rel: str) -> str:
    low = rel.lower()
    if low.endswith("lessons.md"):
        return "lesson"
    if "/versions/" in low:
        return "version"
    if low.endswith("skill.md") or "/skills/" in low:
        return "skill"
    if low.endswith((".md",)):
        return "doc"
    if low.endswith((".json",)):
        return "data"
    return "code"


def wanted(path: Path, root: Path) -> bool:
    rel = path.relative_to(root).as_posix()
    if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]) or "/archive/" in rel:
        return False
    if path.suffix in (".md",):
        return True
    if path.suffix == ".json" and path.stat().st_size < 200_000 and ("data/" in rel or rel.startswith("data") or rel.endswith(".ce-repo.json")):
        return True
    if path.suffix in (".py", ".cs", ".sh") and path.stat().st_size < 400_000:
        return True
    return False


def chunks_of(path: Path, text: str):
    """(heading, line_start, line_end, body) - Markdown by headings, code by its docstrings/comments blocks, JSON whole."""
    if path.suffix == ".md":
        heading, start, buffer = "", 1, []
        for number, line in enumerate(text.splitlines(), 1):
            match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
            if match or sum(len(b) + 1 for b in buffer) > MAX_CHUNK:
                if "".join(buffer).strip():
                    yield heading, start, number - 1, "\n".join(buffer)
                buffer, start = [], number
                if match:
                    heading = match[1]
            buffer.append(line)
        if "".join(buffer).strip():
            yield heading, start, start + len(buffer) - 1, "\n".join(buffer)
        return
    if path.suffix == ".json":
        yield path.name, 1, text.count("\n") + 1, text[:MAX_CHUNK * 2]
        return
    # code: module docstring / leading comment block plus each def/class with its docstring or comment
    lines = text.splitlines()
    head = []
    for line in lines[:60]:
        if line.startswith(("#", '"""', "'''", "//", "///", "/*", " *")) or not line.strip() or line.startswith(("import", "from", "using")):
            head.append(line)
        else:
            break
    if "".join(head).strip():
        yield path.name, 1, len(head), "\n".join(head)[:MAX_CHUNK]
    pattern = re.compile(r"^\s*(def |class |public |internal |private |protected |static |function )")
    for number, line in enumerate(lines, 1):
        if pattern.match(line) and ("(" in line or "class " in line):
            block = lines[number - 1:number + 14]
            body = "\n".join(block)
            if re.search(r'"""|///|//|#', body):
                yield line.strip()[:120], number, number + len(block) - 1, body[:MAX_CHUNK]


# ------------------------------------------------------------------------------------------------------------------- index
def open_index(path: Path | None = None) -> sqlite3.Connection:
    index = Path(os.environ.get("CE_SEARCH_INDEX") or path or DEFAULT_INDEX)
    index.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(index))
    db.executescript("""
        CREATE TABLE IF NOT EXISTS files(repo TEXT, path TEXT, sha256 TEXT, mtime REAL, PRIMARY KEY(repo, path));
        CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, repo TEXT, path TEXT, kind TEXT, heading TEXT,
                                          line_start INT, line_end INT, body TEXT, vector BLOB);
        CREATE INDEX IF NOT EXISTS chunks_file ON chunks(repo, path);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(body, heading, path, repo, content='chunks', content_rowid='id',
                                                                  tokenize='unicode61 remove_diacritics 2');
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    """)
    return db


def embedder():
    if os.environ.get("CE_SEARCH_EMBED", "1") == "0":
        return None
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError:
        return None
    try:
        return SentenceTransformer(EMBED_MODEL)
    except Exception:  # model not downloadable offline
        return None


def to_blob(vector) -> bytes:
    import array
    return array.array("f", [float(x) for x in vector]).tobytes()


def from_blob(blob: bytes):
    import array
    vector = array.array("f")
    vector.frombytes(blob)
    return vector


def source_files(root: Path):
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and d != "worktrees" and not (Path(folder) / d).is_symlink())
        for name in sorted(files):
            path = Path(folder) / name
            if not path.is_symlink():
                yield path


def delete_file_chunks(db: sqlite3.Connection, name: str, rel: str):
    # External-content FTS5 needs the original indexed values to remove postings.
    old = db.execute("SELECT id, body, heading, path, repo FROM chunks WHERE repo=? AND path=?", (name, rel)).fetchall()
    db.executemany("INSERT INTO chunks_fts(chunks_fts, rowid, body, heading, path, repo) VALUES('delete', ?, ?, ?, ?, ?)", old)
    db.execute("DELETE FROM chunks WHERE repo=? AND path=?", (name, rel))


def index_repo(db: sqlite3.Connection, root: Path, model, log) -> tuple[int, int]:
    """(files indexed, chunks written); unchanged files (same sha256) are skipped."""
    try:
        name = load_config(root).get("name", root.name)
    except RepoError:  # config-less source repos remain indexable
        name = root.name
    known = {path: sha for path, sha in db.execute("SELECT path, sha256 FROM files WHERE repo=?", (name,))}
    seen, files, written = set(), 0, 0
    for path in source_files(root):
        if not path.is_file() or not wanted(path, root):
            continue
        rel = path.relative_to(root).as_posix()
        seen.add(rel)
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if known.get(rel) == sha:
            continue
        files += 1
        delete_file_chunks(db, name, rel)
        kind = kind_of(rel)
        rows = [(name, rel, kind, heading, start, end, body) for heading, start, end, body in chunks_of(path, text)]
        vectors = [None] * len(rows)
        if model is not None and rows:
            try:
                vectors = [to_blob(v) for v in model.encode([f"{h}\n{b}" for _, _, _, h, _, _, b in rows], normalize_embeddings=True)]
            except Exception:
                vectors = [None] * len(rows)
        for row, vector in zip(rows, vectors):
            cursor = db.execute("INSERT INTO chunks(repo, path, kind, heading, line_start, line_end, body, vector) VALUES(?,?,?,?,?,?,?,?)",
                                (*row, vector))
            db.execute("INSERT INTO chunks_fts(rowid, body, heading, path, repo) VALUES(?,?,?,?,?)",
                       (cursor.lastrowid, row[6], row[3], row[1], row[0]))
            written += 1
        db.execute("INSERT OR REPLACE INTO files(repo, path, sha256, mtime) VALUES(?,?,?,?)", (name, rel, sha, path.stat().st_mtime))
    for rel in set(known) - seen:  # deleted files
        delete_file_chunks(db, name, rel)
        db.execute("DELETE FROM files WHERE repo=? AND path=?", (name, rel))
    db.commit()
    log(f"{name}: {files} Dateien neu, {written} Abschnitte")
    return files, written


def build(roots: list[Path], index: Path | None, log=print) -> dict:
    db = open_index(index)
    model = embedder()
    started = time.time()
    total_files = total_chunks = 0
    for root in roots:
        if root.is_dir():
            f, c = index_repo(db, root, model, log)
            total_files += f
            total_chunks += c
    db.execute("INSERT OR REPLACE INTO meta VALUES('built', ?)", (time.strftime("%Y-%m-%d %H:%M:%S"),))
    db.execute("INSERT OR REPLACE INTO meta VALUES('embeddings', ?)", ("1" if model is not None else "0",))
    db.commit()
    count = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    db.close()
    return {"files": total_files, "chunks": total_chunks, "total_chunks": count, "seconds": round(time.time() - started, 1),
            "embeddings": model is not None}


# ------------------------------------------------------------------------------------------------------------------- query
def fts_query(text: str) -> str:
    """Tokens ANDed, each as a prefix match; quotes and operators stripped (never a syntax error for the user)."""
    tokens = re.findall(r"[\wÀ-ɏ]+", text)
    # unicode61 drops '#' and '+', so C# / C++ are indexed as token "c".
    # Retain this one-character token only for explicit language spellings.
    language_c = bool(re.search(r"(?i)(?<!\w)c(?:#|\+{2})(?!\w)", text))
    terms = [f'"{t}"*' for t in dict.fromkeys(tokens) if len(t) > 1]
    if language_c:
        terms.append('"c"')
    return " ".join(terms) or '""'


def search(query: str, k: int = 8, repo: str | None = None, kind: str | None = None, index: Path | None = None) -> list[dict]:
    db = open_index(index)
    where, args = [], [fts_query(query)]
    if repo:
        where.append("c.repo LIKE ?")
        args.append(f"%{repo}%")
    if kind:
        where.append("c.kind = ?")
        args.append(kind)
    clause = (" AND " + " AND ".join(where)) if where else ""
    sql = f"""SELECT c.id, c.repo, c.path, c.kind, c.heading, c.line_start, c.line_end, c.body, bm25(chunks_fts, 1.0, 2.0, 0.5, 0.5) AS score
              FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ?{clause} ORDER BY score LIMIT ?"""
    rows = db.execute(sql, (*args, max(k * 4, 20))).fetchall()
    if not rows:  # relax: any token
        tokens = [t for t in re.findall(r"[\wÀ-ɏ]+", query) if len(t) > 2]
        if tokens:
            args[0] = " OR ".join(f'"{t}"*' for t in tokens)
            rows = db.execute(sql, (*args, max(k * 4, 20))).fetchall()
    results = [{"id": r[0], "repo": r[1], "path": r[2], "kind": r[3], "heading": r[4], "lines": f"{r[5]}-{r[6]}", "text": r[7],
                "bm25": round(-r[8], 3)} for r in rows]
    # hybrid: re-rank with cosine on the stored vectors when both the index and this process have embeddings
    model = embedder() if db.execute("SELECT value FROM meta WHERE key='embeddings'").fetchone() == ("1",) else None
    if model is not None and results:
        try:
            q = model.encode([query], normalize_embeddings=True)[0]
            for hit in results:
                blob = db.execute("SELECT vector FROM chunks WHERE id=?", (hit["id"],)).fetchone()[0]
                if blob:
                    v = from_blob(blob)
                    hit["cosine"] = round(sum(a * b for a, b in zip(q, v)), 3)
            top = max(h["bm25"] for h in results) or 1.0
            results.sort(key=lambda h: -(0.5 * h["bm25"] / top + 0.5 * h.get("cosine", 0)))
        except Exception:
            pass
    # kind weights: lessons, skills and docs are what an agent wants first; code only when asked (--kind code) or alone
    weight = {"lesson": 1.6, "skill": 1.2, "doc": 1.0, "version": 0.9, "data": 0.8, "code": 0.55}
    results.sort(key=lambda h: -(h["bm25"] * weight.get(h["kind"], 1.0) * (1 + h.get("cosine", 0))))
    db.close()
    return results[:k]


def status(index: Path | None = None) -> dict:
    db = open_index(index)
    meta = dict(db.execute("SELECT key, value FROM meta"))
    per_repo = db.execute("SELECT repo, COUNT(*) FROM files GROUP BY repo").fetchall()
    out = {"index": str(Path(os.environ.get("CE_SEARCH_INDEX") or index or DEFAULT_INDEX)), "built": meta.get("built"),
           "embeddings": meta.get("embeddings") == "1", "files": dict(per_repo),
           "chunks": db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]}
    db.close()
    return out


# -------------------------------------------------------------------------------------------------------------------- MCP
TOOLS = [
    {"name": "ce_search",
     "description": "Search the docs, lessons, skills, version reports and code comments of all Cube Escape repos "
                    "(local index). Returns the best chunks with repo, path, heading and line range - cite them and read "
                    "only that part. Use before reading a long doc or when asking 'was that solved before?'.",
     "inputSchema": {"type": "object", "required": ["query"],
                     "properties": {"query": {"type": "string"}, "k": {"type": "integer", "default": 8},
                                    "repo": {"type": "string", "description": "substring of a repo name, e.g. toolgen"},
                                    "kind": {"type": "string", "enum": ["lesson", "doc", "skill", "version", "data", "code"]}}}},
    {"name": "ce_search_status", "description": "When the index was built, which repos and how many chunks it holds.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def serve() -> int:
    """Minimal MCP server over stdio (JSON-RPC 2.0, newline-delimited)."""
    def reply(ident, result=None, error=None):
        message = {"jsonrpc": "2.0", "id": ident}
        if error:
            message["error"] = error
        else:
            message["result"] = result
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        method, ident, params = request.get("method"), request.get("id"), request.get("params") or {}
        if method == "initialize":
            reply(ident, {"protocolVersion": params.get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}},
                          "serverInfo": {"name": "ce-search", "version": "0.5.0"}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(ident, {"tools": TOOLS})
        elif method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments") or {}
            try:
                if name == "ce_search":
                    hits = search(arguments["query"], int(arguments.get("k", 8)), arguments.get("repo"), arguments.get("kind"))
                    text = "\n\n".join(f"[{i + 1}] {h['repo']}/{h['path']}:{h['lines']} — {h['heading']}\n{h['text'][:900]}"
                                       for i, h in enumerate(hits)) or "keine Treffer"
                elif name == "ce_search_status":
                    text = json.dumps(status(), ensure_ascii=False, indent=1)
                else:
                    raise KeyError(name)
                reply(ident, {"content": [{"type": "text", "text": text}]})
            except Exception as error:  # noqa: BLE001 - every error goes back to the client
                reply(ident, {"content": [{"type": "text", "text": f"ce_search: {error}"}], "isError": True})
        elif ident is not None:
            reply(ident, error={"code": -32601, "message": f"unknown method {method}"})
    return 0


# -------------------------------------------------------------------------------------------------------------------- main
def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    idx = sub.add_parser("index")
    idx.add_argument("--all", action="store_true")
    idx.add_argument("--repo", action="append")
    idx.add_argument("--index", type=Path)
    qry = sub.add_parser("query")
    qry.add_argument("query")
    qry.add_argument("-k", type=int, default=8)
    qry.add_argument("--repo")
    qry.add_argument("--kind", choices=["lesson", "doc", "skill", "version", "data", "code"])
    qry.add_argument("--index", type=Path)
    qry.add_argument("--json", action="store_true")
    sub.add_parser("serve")
    st = sub.add_parser("status")
    st.add_argument("--index", type=Path)
    args = parser.parse_args(argv)
    if args.command == "index":
        roots = repos(args.repo)
        if not roots:
            print("ce_search: keine Repos gefunden (CE_REPOS, --repo oder ~/Claude/<repo>)")
            return 2
        missing = [root for root in roots if not root.is_dir()]
        if missing:
            print("ce_search: ungültige Repo-Wurzel(n): " + ", ".join(map(str, missing)), file=sys.stderr)
            return 2
        result = build(roots, args.index)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == "query":
        hits = search(args.query, args.k, args.repo, args.kind, args.index)
        if args.json:
            print(json.dumps(hits, ensure_ascii=False, indent=1))
        else:
            for i, h in enumerate(hits, 1):
                print(f"[{i}] {h['repo']}/{h['path']}:{h['lines']} — {h['heading']}  (bm25 {h['bm25']}" + (f", cos {h['cosine']}" if "cosine" in h else "") + ")")
                print("    " + h["text"].strip().replace("\n", "\n    ")[:600])
            if not hits:
                print("keine Treffer")
        return 0
    if args.command == "status":
        print(json.dumps(status(args.index), ensure_ascii=False, indent=1))
        return 0
    return serve()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
