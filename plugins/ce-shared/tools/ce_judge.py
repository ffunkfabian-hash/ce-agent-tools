#!/usr/bin/env python3
"""ce_judge.py [--base REF] [--model luna|sol] [--dry-run] [--files ...] - the second reviewer, always with the same checklist.

0.5.0 (› Richter und Selbstkorrektur): wraps `codex exec` so every review gets templates/pruefung.md (eight points, table,
"Freigabe"/"Nachbessern"), the diff on stdin without generated files, read-only sandbox, the model by size:
`gpt-6-luna` medium below ~300 changed lines and without contract/format files, else `gpt-6.1-sol` high. Prints the
verdict and writes it to report.md (section "Richter"). --dry-run shows the prompt and the diff size only.

    python3 ce_judge.py                      # diff of HEAD against origin/main (or --base)
    python3 ce_judge.py --base main --model sol
    python3 ce_judge.py --opus                # no Codex: print the prompt for an Opus subagent (Agent(model=opus))

Exit 0 Freigabe, 1 Nachbessern, 2 codex missing / nothing to review.
"""
from __future__ import annotations

import argparse
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


def choose_model(diff: str, names: list[str], wanted: str | None) -> str:
    if wanted:
        return wanted
    lines = sum(1 for line in diff.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
    return "sol" if lines >= 300 or any(CONTRACT.search(n) for n in names) else "luna"


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
    model = choose_model(diff, names, args.model)
    head = git("rev-parse", "--short", "HEAD", cwd=cwd).strip()
    prompt = template().replace("<Repo> <Version> „<Name>“ (Basis <Commit>)", f"{cwd.name} @ {head} (Basis {base})")
    prompt = prompt.replace("<Pfade, falls nötig>", ", ".join(names[:20])).replace("<…>", args.known or "keine")
    lines = sum(1 for line in diff.splitlines() if line.startswith(("+", "-")))
    print(f"ce_judge: {len(names)} Dateien, {lines} Diff-Zeilen, Richter {model} ({MODELS[model][0]} {MODELS[model][1]}), Basis {base}")
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
