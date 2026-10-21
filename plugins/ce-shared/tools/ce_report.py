#!/usr/bin/env python3
"""ce_report.py [TRANSCRIPT.jsonl] [--json] - what a session actually did, measured from its Claude Code transcript.

0.5.0: the rules ask for things nobody measured (model per subagent, no repeated full runs, search before reading, output size,
session length). This reads the transcript (the Stop hook passes transcript_path; or the newest under ~/.claude/projects) and
prints one line for report.md plus the details:

    Sitzung: 184 Nachrichten · Unteragenten 3 (opus 1, sonnet 2) · ./check 2 (1 wiederholt ohne Diff) · ce_search 4 · Read .md>60 KB 1 ·
    größte Ausgabe 41 KB (cat logs/toolgen_report.json) · heavy_lock gewartet 12 min · Bash 97 · Edit 23

Every number names the rule it belongs to (› Kontingent sparen, › Suche und Gedächtnis, › Unteragenten und Modelle). Pure standard library.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

BIG_DOC = 60_000


def newest_transcript() -> Path | None:
    root = Path.home() / ".claude" / "projects"
    files = sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime) if root.is_dir() else []
    return files[-1] if files else None


def events(path: Path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def tool_uses(record: dict):
    message = record.get("message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block.get("name") or "", block.get("input") or {}


def tool_results(record: dict):
    message = record.get("message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            inner = block.get("content")
            text = inner if isinstance(inner, str) else "".join(c.get("text", "") for c in inner or [] if isinstance(c, dict))
            yield text


def analyse(path: Path) -> dict:
    messages = 0
    tools: Counter = Counter()
    agents: Counter = Counter()
    checks, check_signatures, searches, big_reads = 0, Counter(), 0, 0
    largest = (0, "")
    waited = 0.0
    last_command = ""
    for record in events(path):
        if record.get("type") in ("user", "assistant"):
            messages += 1
        for name, data in tool_uses(record):
            tools[name] += 1
            if name == "Agent":
                agents[str(data.get("model") or "inherit")] += 1
            if name == "Bash":
                command = str(data.get("command") or "")
                last_command = command[:80]
                if re.search(r"(^|[;&|]\s*)\./check\b|ce_check\.py|verify\.sh\s+run|testplan\.py\s+run|unittest discover", command):
                    checks += 1
                    check_signatures[re.sub(r"\s+", " ", command.strip())[:120]] += 1
                if "ce_search" in command or re.search(r"\brg\b|\bgrep\b", command):
                    searches += 1
            if name == "Read":
                file_path = str(data.get("file_path") or "")
                try:
                    if file_path.endswith(".md") and Path(file_path).stat().st_size > BIG_DOC:
                        big_reads += 1
                except OSError:
                    pass
            if name.startswith("mcp__") and "search" in name:
                searches += 1
        for text in tool_results(record):
            if len(text) > largest[0]:
                largest = (len(text), last_command)
            for match in re.finditer(r"heavy lock[^\n]*?free after (\d+) s", text):
                waited += int(match[1])
            for match in re.finditer(r"held, waited (\d+) s", text):
                waited += int(match[1])
    repeated = sum(count - 1 for count in check_signatures.values() if count > 1)
    return {"transcript": str(path), "messages": messages, "tools": dict(tools), "agents": dict(agents), "checks": checks,
            "checks_repeated_same_command": repeated, "searches": searches, "big_doc_reads": big_reads,
            "largest_output_bytes": largest[0], "largest_output_command": largest[1], "heavy_lock_waited_s": int(waited)}


def one_line(info: dict) -> str:
    agents = ", ".join(f"{m} {n}" for m, n in sorted(info["agents"].items())) or "keine"
    tools = info["tools"]
    return (f"Sitzung: {info['messages']} Nachrichten · Unteragenten {sum(info['agents'].values())} ({agents}) · "
            f"Prüfläufe {info['checks']} ({info['checks_repeated_same_command']} wiederholt mit gleichem Befehl) · "
            f"Suchen {info['searches']} · lange Docs gelesen {info['big_doc_reads']} · größte Ausgabe {info['largest_output_bytes'] // 1024} KB"
            f"{' (' + info['largest_output_command'] + ')' if info['largest_output_command'] else ''} · "
            f"heavy_lock gewartet {info['heavy_lock_waited_s'] // 60} min · Bash {tools.get('Bash', 0)} · Edit {tools.get('Edit', 0) + tools.get('Write', 0)}")


def hints(info: dict) -> list[str]:
    out = []
    if info["messages"] > 300:
        out.append("über 300 Nachrichten: neue Sitzung beginnen (› Sitzungen kurz halten)")
    if info["agents"].get("inherit"):
        out.append(f"{info['agents']['inherit']} Unteragent(en) ohne model – erben das Hauptmodell (› Unteragenten und Modelle)")
    if info["checks_repeated_same_command"]:
        out.append("Prüflauf mit identischem Befehl wiederholt – nach Korrektur nur den gezielten Test (› Richter und Selbstkorrektur)")
    if info["big_doc_reads"] and not info["searches"]:
        out.append("lange Docs gelesen, nie gesucht – ce_search/rg zuerst (› Suche und Gedächtnis)")
    if info["largest_output_bytes"] > 40_000:
        out.append("Werkzeugausgabe über 40 KB – head/tail/jq (› Kontingent sparen)")
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("transcript", nargs="?", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    path = args.transcript or newest_transcript()
    if not path or not path.is_file():
        print("ce_report: kein Transcript gefunden")
        return 2
    info = analyse(path)
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=1))
        return 0
    print(one_line(info))
    for hint in hints(info):
        print("  Hinweis: " + hint)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
