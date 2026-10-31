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
import datetime as dt
import time
import json
import re
import sys
import math
from collections import Counter
from pathlib import Path

BIG_DOC = 60_000
MAX_CONTEXT_TOKENS = 150_000
USAGE_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def _valid_token_count(value):
    """Return a safe nonnegative integer token counter, rejecting bools and nonfinite numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not math.isfinite(value) or value < 0 or int(value) != value:
        return None
    return int(value)


def _synthetic_usage(record: dict, message: dict) -> bool:
    return any(obj.get(key) is True for obj in (record, message) for key in ("synthetic", "isSynthetic", "is_synthetic"))


def _usage_report(requests: dict) -> dict:
    fresh = creation = read = output = 0
    complete_inputs = 0
    output_measured = 0
    for request in requests.values():
        values = request["usage"]
        if all(values.get(field) is not None for field in USAGE_FIELDS[:3]):
            fresh += values["input_tokens"]
            creation += values["cache_creation_input_tokens"]
            read += values["cache_read_input_tokens"]
            complete_inputs += 1
        if values.get("output_tokens") is not None:
            output += values["output_tokens"]
            output_measured += 1
    measured_total = fresh + creation + read
    has_inputs = complete_inputs > 0
    return {
        "requests": len(requests),
        "input_measured_requests": complete_inputs,
        "input_coverage": complete_inputs / len(requests) if requests else None,
        "fresh_input_tokens": fresh if has_inputs else None,
        "cache_creation_input_tokens": creation if has_inputs else None,
        "cache_read_input_tokens": read if has_inputs else None,
        "total_input_tokens": measured_total if has_inputs else None,
        "cache_read_fraction": read / measured_total if has_inputs and measured_total else None,
        "output_measured_requests": output_measured,
        "output_tokens": output if output_measured else None,
        "average_input_tokens_per_request": measured_total / complete_inputs if complete_inputs else None,
        "max_input_tokens_per_request": max((sum(request["usage"][field] for field in USAGE_FIELDS[:3])
                                              for request in requests.values()
                                              if all(request["usage"].get(field) is not None for field in USAGE_FIELDS[:3])), default=None),
    }


def transcript_files(root: Path | None = None) -> list[Path]:
    if root is not None:
        if not root.is_dir():
            return []
        return sorted([*root.glob("*/*.jsonl"), *root.glob("*/.system_generated/logs/transcript.jsonl")], key=lambda p: p.stat().st_mtime)
    claude = Path.home() / ".claude" / "projects"
    agy = Path.home() / ".gemini" / "antigravity" / "brain"
    files = [*(claude.glob("*/*.jsonl") if claude.is_dir() else []),
             *(agy.glob("*/.system_generated/logs/transcript.jsonl") if agy.is_dir() else [])]
    return sorted(files, key=lambda p: p.stat().st_mtime)


def newest_transcript() -> Path | None:
    files = transcript_files()
    return files[-1] if files else None


def _unquote(val) -> str:
    text = str(val or "")
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            return str(json.loads(text))
        except ValueError:
            return text[1:-1]
    return text


def events(path: Path, since=None):
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
                if since is not None:
                    stamp = record.get("timestamp") or record.get("created_at")
                    if not isinstance(stamp, str):
                        continue
                    try:
                        when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        continue
                    if when < since:
                        continue
                yield record
            except ValueError:
                continue


def tool_uses(record: dict):
    message = record.get("message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block.get("name") or "", block.get("input") or {}
    for call in record.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        raw_name = call.get("name") or ""
        args = {k: _unquote(v) for k, v in (call.get("args") or {}).items()} if isinstance(call.get("args"), dict) else {}
        if raw_name == "run_command":
            yield "Bash", {"command": args.get("CommandLine", "")}
        elif raw_name == "read_file":
            yield "Read", {"file_path": args.get("Path", "")}
        elif raw_name in ("edit_file", "write_file"):
            yield ("Edit" if raw_name == "edit_file" else "Write"), {"file_path": args.get("Path", "")}
        elif raw_name == "invoke_subagent":
            yield "Agent", {"model": args.get("Model", "inherit")}
        elif raw_name == "call_mcp_tool" and "search" in args.get("ToolName", ""):
            yield "mcp__ce_search", args
        else:
            yield raw_name, args


def tool_results(record: dict):
    if record.get("type") == "GENERIC" and isinstance(record.get("content"), str):
        yield record["content"]
    message = record.get("message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            inner = block.get("content")
            text = inner if isinstance(inner, str) else "".join(c.get("text", "") for c in inner or [] if isinstance(c, dict))
            yield text


def analyse(path: Path, since=None) -> dict:
    messages = 0
    tools: Counter = Counter()
    agents: Counter = Counter()
    checks, check_signatures, searches, big_reads = 0, Counter(), 0, 0
    largest = (0, "")
    waited = 0.0
    last_command = ""
    usage_requests = {}
    for record in events(path, since):
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        if not _synthetic_usage(record, message):
            request_id = message.get("id")
            model = message.get("model") or record.get("model")
            role = message.get("role")
            claude_assistant = role == "assistant" or (role is None and record.get("type") == "assistant")
            synthetic_model = isinstance(model, str) and model.strip().lower() == "<synthetic>"
            if (isinstance(request_id, str) and request_id
                    and claude_assistant and not synthetic_model):
                request = usage_requests.setdefault(request_id, {"usage": {field: None for field in USAGE_FIELDS}})
                raw_usage = message.get("usage")
                if isinstance(raw_usage, dict):
                    for field in USAGE_FIELDS:
                        count = _valid_token_count(raw_usage.get(field))
                        if count is not None:
                            previous = request["usage"][field]
                            request["usage"][field] = count if previous is None else max(previous, count)
        if record.get("type") in ("user", "assistant", "USER_INPUT", "PLANNER_RESPONSE"):
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
            if len(text.encode("utf-8")) > largest[0]:
                largest = (len(text.encode("utf-8")), last_command)
            for match in re.finditer(r"heavy lock[^\n]*?free after (\d+) s", text):
                waited += int(match[1])
            for match in re.finditer(r"held, waited (\d+) s", text):
                waited += int(match[1])
    repeated = sum(count - 1 for count in check_signatures.values() if count > 1)
    return {"transcript": str(path), "messages": messages, "tools": dict(tools), "agents": dict(agents), "checks": checks,
            "checks_repeated_same_command": repeated, "searches": searches, "big_doc_reads": big_reads,
            "largest_output_bytes": largest[0], "largest_output_command": largest[1], "heavy_lock_waited_s": int(waited),
            "usage": _usage_report(usage_requests)}


def one_line(info: dict) -> str:
    agents = ", ".join(f"{m} {n}" for m, n in sorted(info["agents"].items())) or "keine"
    tools = info["tools"]
    usage = info.get("usage") or {}
    if usage.get("requests", 0) == 0:
        usage_line = "· Modell-Usage nicht verfügbar"
    elif usage.get("total_input_tokens") is None:
        usage_line = f"· Modell-Usage unvollständig ({usage.get('input_measured_requests', 0)}/{usage.get('requests', 0)} Inputanfragen messbar)"
    else:
        cache_fraction = usage.get("cache_read_fraction")
        cache_text = f"{cache_fraction * 100:.1f}%" if cache_fraction is not None else "nicht berechenbar"
        usage_line = (f"· Input gesamt {usage['total_input_tokens']:,} Tokens (frisch {usage['fresh_input_tokens']:,}, "
                      f"Cache-Schreiben {usage['cache_creation_input_tokens']:,}, Cache-Lesen {usage['cache_read_input_tokens']:,}; "
                      f"Cache-Lesequote {cache_text}; Ø {usage['average_input_tokens_per_request']:.0f}, "
                      f"max {usage['max_input_tokens_per_request']:,} je Anfrage; "
                      f"Messabdeckung {usage['input_measured_requests']}/{usage['requests']})")
    if usage.get("output_tokens") is not None:
        usage_line += f" · Output {usage['output_tokens']:,} Tokens"
    return (f"Sitzung: {info['messages']} Nachrichten · Unteragenten {sum(info['agents'].values())} ({agents}) · "
            f"Prüfläufe {info['checks']} ({info['checks_repeated_same_command']} wiederholt mit gleichem Befehl) · "
            f"Suchen {info['searches']} · lange Docs gelesen {info['big_doc_reads']} · größte Ausgabe {info['largest_output_bytes'] // 1024} KB"
            f"{' (' + info['largest_output_command'] + ')' if info['largest_output_command'] else ''} · "
            f"heavy_lock gewartet {info['heavy_lock_waited_s'] // 60} min · Bash {tools.get('Bash', 0)} · Edit {tools.get('Edit', 0) + tools.get('Write', 0)} "
            f"{usage_line}")


def hints(info: dict) -> list[str]:
    out = []
    if info["messages"] > 300:
        out.append("über 300 Nachrichten: neue Sitzung beginnen (› Sitzungen kurz halten)")
    usage = info.get("usage") or {}
    max_input = usage.get("max_input_tokens_per_request")
    if isinstance(max_input, int) and max_input > MAX_CONTEXT_TOKENS:
        out.append(f"Spitzen-Input {max_input:,} Tokens (> {MAX_CONTEXT_TOKENS:,}): neue Sitzung beginnen (› Sitzungen kurz halten)")
    creation = usage.get("cache_creation_input_tokens")
    if isinstance(creation, int) and isinstance(max_input, int) and max_input > 0 and creation > max(100_000, 2 * max_input):
        out.append(f"Cache-Schreiben ({creation:,} Tokens) > 2× Spitzen-Input ({max_input:,}): "
                   "Hinweis auf möglichen wiederholten Cache-Neuaufbau (z.B. Pausen > TTL oder geänderter Präfix); "
                   "schwere Läufe möglichst ans Ende bündeln (› Schwere Läufe und Sperren)")
    if info["agents"].get("inherit"):
        out.append(f"{info['agents']['inherit']} Unteragent(en) ohne model – erben das Hauptmodell (› Unteragenten und Modelle)")
    if info["checks_repeated_same_command"]:
        out.append("Prüflauf mit identischem Befehl wiederholt; der Transcript belegt keine unveränderten Eingaben (› Richter und Selbstkorrektur)")
    if info["big_doc_reads"] and not info["searches"]:
        out.append("lange Docs gelesen, nie gesucht – ce_search/rg zuerst (› Suche und Gedächtnis)")
    if info["largest_output_bytes"] > 40_000:
        out.append("Werkzeugausgabe über 40 KB – head/tail/jq (› Kontingent sparen)")
    return out


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--dashboard":
        import ce_console
        return ce_console.main(argv[1:] or ["snapshot"])
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("transcript", nargs="?", type=Path)
    parser.add_argument("--since", help="aggregate Claude transcripts since e.g. 7d (record timestamps, not file mtime)")
    parser.add_argument("--root", type=Path, help="transcript root (default ~/.claude/projects)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.since:
        match = re.fullmatch(r"(\d+)([dh])", args.since)
        if not match:
            parser.error("--since expects e.g. 7d or 24h")
        cutoff = time.time() - int(match[1]) * (86400 if match[2] == "d" else 3600)
        rows = [analyse(path, cutoff) for path in transcript_files(args.root)]
        rows = [row for row in rows if row["messages"] or row["tools"]]
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            print("Claude-Transcripts | Nachrichten | Prüfläufe | gleicher Befehl | Suchen | Sperrwartezeit (s)")
            for row in rows:
                print(f"{Path(row['transcript']).stem} | {row['messages']} | {row['checks']} | {row['checks_repeated_same_command']} | {row['searches']} | {row['heavy_lock_waited_s']}")
        return 0
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
    try:  # (0.31) Windows: a cp1252 console or pipe cannot take the tool's „“ → ·
        import ce_common as _console
        _console.utf8_console()
    except ImportError:
        pass
    sys.exit(main(sys.argv[1:]))
