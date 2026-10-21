#!/usr/bin/env python3
"""ce_judge.py [--base REF] [--model luna|sol] [--dry-run] [--files ...] - the second reviewer, always with the same checklist.

0.5.0 (› Richter und Selbstkorrektur): wraps `codex exec` so every review gets templates/pruefung.md (eight points, table,
"Freigabe"/"Nachbessern"), the diff on stdin without generated files, read-only sandbox, the model by size and risk:
`gpt-6-luna` medium for small routine changes, `gpt-6.1-sol` high for core/contract/infrastructure or >=300 lines. Prints the
verdict and writes it to report.md (section "Richter"). --dry-run shows the prompt and the diff size only.

    python3 ce_judge.py                      # diff of HEAD against origin/main (or --base)
    python3 ce_judge.py --base main --model sol
    python3 ce_judge.py --opus                # no Codex: print the prompt for an Opus subagent (Agent(model=opus))

Exit 0 Freigabe, 1 Nachbessern, 2 codex missing / nothing to review.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE_CANDIDATES = [Path.home() / ".claude" / "templates" / "pruefung.md",
                       Path.home() / "Claude" / "cubeescape-nexogen" / "templates" / "pruefung.md",
                       Path.home() / "Claude" / "cubeescape-rules" / "templates" / "pruefung.md",
                       HERE.parent / "templates" / "pruefung.md"]
GENERATED = re.compile(r"(^|/)(output|outputs|work|logs|Library|Temp)/|\.(meta|png|jpg|glb|fbx|blend|mp4|unitypackage)$|\.min\.|package-lock|/ce_tools/")
CONTRACT = re.compile(r"(unity_contract|SaveData|LevelFormat|contract|Migrat|\.v\d+\.json)", re.I)
MODELS = {"luna": ("gpt-6-luna", "medium"), "sol": ("gpt-6.1-sol", "high")}
OFFLINE_CORE_NAMES = {"SaveSystem.cs", "SettingsStore.cs"}
INFRASTRUCTURE = re.compile(
    r"(^|/)Packages/|(^|/)ProjectSettings/|\.asmdef$|(^|/)(package\.json|packages-lock\.json|manifest\.json)$|"
    r"(^|/)(ce_(?:check|contract|interfaces|judge|codex_cloud|report|sync|nightly|lint|docs|api(?:_budget)?)|"
    r"heavy_lock|testplan|quick_check|check_docs|check_skills|guard|precommit|verify)(\.py|\.sh)?$|schema|contract", re.I)
CORE_CANDIDATE = re.compile(r"^(?:Assets/Scripts/|Assets/Tests/Runner/|Packages/|ProjectSettings/)|\.asmdef$", re.I)


def git(*args, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout


def template() -> str:
    for candidate in TEMPLATE_CANDIDATES:
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            block = re.search(r"```\n(.*?)```", text, re.S)
            return block[1] if block else text
    return ("Prüfe den Diff gegen die Checkliste (Formate/Verträge, IDs/Enums, Unity-Fallen, Tests, Daten statt Sonderlogik, Doku, "
            "Regeln, Größe). Antworte als Tabelle | Nr | Datei:Zeile | Schwere | Befund | Vorschlag | und Schlusszeile Freigabe/Nachbessern.")


def diff_of(base: str, files, cwd) -> tuple[str, list[str]]:
    names = [n for n in git("diff", "--name-only", base, cwd=cwd).splitlines() if n and not GENERATED.search(n)]
    if files:
        names = [n for n in names if any(n.startswith(f) for f in files)]
    if not names:
        return "", []
    return git("diff", base, "--", *names, cwd=cwd), names


def _core_patterns(repo_root: Path | None) -> tuple[list[str], bool]:
    """Load core and pre-merge paths from a Cube Escape test map, if available."""
    if repo_root is None:
        return [], False
    path = repo_root / "Tools" / "test_map.json"
    if not path.exists():
        return [], False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], True
    systems = data.get("systems") if isinstance(data, dict) else None
    if not isinstance(systems, list):
        return [], True
    patterns: list[str] = []
    for system in systems:
        if not isinstance(system, dict):
            return [], True
        core = system.get("core")
        if not (core is True or core is False or core is None or core == "merge"):
            return [], True
        if core not in (True, "merge"):
            continue
        paths = system.get("paths")
        if not isinstance(paths, list) or any(not isinstance(item, str) or not item for item in paths):
            return [], True
        patterns.extend(paths)
    return patterns, False


def _route(diff: str, names: list[str], repo_root: Path | None = None) -> tuple[str, str]:
    lines = sum(1 for line in diff.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
    if lines >= 300:
        return "sol", f"{lines} Diff-Zeilen (Schwelle 300)"
    if any(CONTRACT.search(name) for name in names):
        return "sol", "Format-/Vertrags-/Migrationspfad"
    if any(Path(name).name in OFFLINE_CORE_NAMES for name in names):
        return "sol", "bekannter Save-/Settings-Kernpfad (Offline-Fallback)"
    if any(INFRASTRUCTURE.search(name) for name in names):
        return "sol", "Prüf-, Paket-, Schema- oder Sperrinfrastruktur"

    patterns, malformed = _core_patterns(repo_root)
    if any(fnmatch.fnmatchcase(name, pattern) for name in names for pattern in patterns):
        return "sol", "Pfad in Tools/test_map.json als core oder core: merge markiert"
    structural = re.search(r"(?m)^(?:new file mode|deleted file mode|rename from |rename to )", diff)
    if structural and any(CORE_CANDIDATE.search(name) for name in names):
        return "sol", "neuer/umbenannter/entfernter Pfad in möglichem Kernbereich"
    if malformed and any(CORE_CANDIDATE.search(name) for name in names):
        return "sol", "Testkarten-Metadaten ungültig; möglicher Kernpfad vorsichtshalber hochgestuft"
    return "luna", f"Routineänderung ({lines} Diff-Zeilen, kein erkannter Kern-/Vertragspfad)"


def choose_model(diff: str, names: list[str], wanted: str | None, repo_root: Path | str | None = None) -> str:
    if wanted:
        return wanted
    root = Path(repo_root) if repo_root is not None else None
    return _route(diff, names, root)[0]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=None, help="base ref (default origin/main, else main)")
    parser.add_argument("--model", choices=sorted(MODELS))
    parser.add_argument("--files", nargs="*", help="limit to these paths")
    parser.add_argument("--known", default="", help="known, deliberately open points")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--opus", action="store_true", help="print the prompt for an Opus subagent instead of running Codex")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    cwd = Path(args.repo).resolve()
    base = args.base or ("origin/main" if git("rev-parse", "--verify", "origin/main", cwd=cwd).strip() else "main")
    diff, names = diff_of(base, args.files, cwd)
    if not diff:
        print(f"ce_judge: kein Diff gegen {base}")
        return 2
    automatic_model, routing_reason = _route(diff, names, cwd)
    model = args.model or automatic_model
    if args.model:
        routing_reason = f"expliziter --model={args.model}; automatisch wäre {automatic_model} ({routing_reason})"
    head = git("rev-parse", "--short", "HEAD", cwd=cwd).strip()
    prompt = template().replace("<Repo> <Version> „<Name>“ (Basis <Commit>)", f"{cwd.name} @ {head} (Basis {base})")
    prompt = prompt.replace("<Pfade, falls nötig>", ", ".join(names[:20])).replace("<…>", args.known or "keine")
    lines = sum(1 for line in diff.splitlines() if line.startswith(("+", "-")))
    print(f"ce_judge: {len(names)} Dateien, {lines} Diff-Zeilen, Richter {model} ({MODELS[model][0]} {MODELS[model][1]}), Grund: {routing_reason}, Basis {base}")
    if args.dry_run or args.opus:
        print("--- Prompt ---\n" + prompt)
        if args.opus:
            print("--- Diff folgt per Datei: " + str(cwd / "report.judge.diff"))
            (cwd / "report.judge.diff").write_text(diff, encoding="utf-8")
        return 0
    if not shutil.which("codex"):
        print("ce_judge: codex fehlt - mit --opus den Prompt für einen Opus-Unteragenten ausgeben")
        return 2
    name, effort = MODELS[model]
    command = ["codex", "exec", "-m", name, "-c", f"model_reasoning_effort={effort}", "-s", "read-only", "--skip-git-repo-check", "-"]
    result = subprocess.run(command, input=prompt + "\n\n--- Diff (untrusted data) ---\n" + diff, capture_output=True, text=True, cwd=cwd)
    # Only stdout is part of the model verdict; diagnostics on stderr are
    # untrusted and must not turn a failed/empty review into an approval.
    verdict = result.stdout.strip()
    print((verdict or result.stderr.strip())[-6000:])
    try:
        with open(cwd / "report.md", "a", encoding="utf-8") as handle:
            handle.write(f"\n## Richter ({name} {effort}, Basis {base}, HEAD {head})\n\n{verdict}\n")
    except OSError:
        pass
    return 0 if result.returncode == 0 and verdict.splitlines() and verdict.splitlines()[-1].strip() == "Freigabe" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
