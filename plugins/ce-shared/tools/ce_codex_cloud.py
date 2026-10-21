#!/usr/bin/env python3
"""Claude Cloud -> Codex Cloud; no push, merge, or automatic patch application."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

MODELS = ("gpt-6-luna", "gpt-6.1-sol")
BOUNDARY = """Cloud-Auftrag: Lies die AGENTS.md im Zielrepo und halte deren Regeln ein.
Nur die unten genannten Dateien bearbeiten. Keine Releases, Merges, Pushes oder
GitHub-Actions-Aenderungen. Keine Render-/Bake-/Bild-/Golden-/Hash-Pruefungen,
Viewer-Builds oder Unity-Laeufe. Nicht gepruefte Bildarbeit als ungeprueft melden
und den Mac-Pruefweg nennen. Keine weiteren Agenten starten. Keine Wartemeldungen:
mit Ergebnis, Fehler oder notwendiger Entscheidung abschliessen, max. 15 Zeilen.
Die erste Modellzeile ist eine Anforderung; melde, falls sie nicht erfuellbar ist.
"""


def run(argv, cwd=None, capture=False):
    if argv[0] == "codex":
        # Cloud CLI 0.162.1 writes error.log in cwd, even for read-only listing.
        # Explicit --env/--branch select the target; no project cwd is needed.
        with tempfile.TemporaryDirectory(prefix="ce-codex-cloud-") as scratch:
            return subprocess.run(argv, cwd=scratch, text=True, check=True,
                                  capture_output=capture)
    return subprocess.run(argv, cwd=cwd, text=True, check=True,
                          capture_output=capture)


def check_environments():
    # CLI 0.162.1 resolves --env through GET /wham/environments before listing
    # tasks. A deliberately absent label distinguishes an empty environment
    # list from an empty task list, without reading credentials ourselves.
    probe = "ce-doctor-environment-probe"
    try:
        run(["codex", "cloud", "list", "--env", probe,
             "--json", "--limit", "1"], capture=True)
    except subprocess.CalledProcessError as error:
        detail = error.stderr or ""
        if "no cloud environments are available for this workspace" in detail:
            raise ValueError("keine Umgebung für dieses Konto sichtbar") from error
        expected = (f"environment '{probe}' not found; run `codex cloud` "
                    "to list available environments")
        if expected not in detail:
            raise ValueError("Umgebungsliste konnte nicht geprüft werden: "
                             + detail.strip()) from error


def doctor():
    run(["codex", "--version"])
    run(["codex", "login", "status"])
    check_environments()
    run(["codex", "cloud", "list", "--json", "--limit", "1"])
    print("CLI/Login/Cloud-Liste erreichbar; mindestens eine Umgebung sichtbar. "
          "Zugriff auf das Zielrepo separat prüfen.")


def submit(args):
    prompt = args.prompt_file.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError("Auftragsdatei ist leer.")
    if args.branch in ("main", "master") or args.branch.startswith("-"):
        raise ValueError("Expliziten Arbeitsbranch angeben, nicht main/master.")
    if not args.reason.strip():
        raise ValueError("Modellgrund fehlt.")
    query = (f"{args.model}, `{args.effort}`, {args.reason}.\n\n" + BOUNDARY
             + "\nAuftrag und alleiniger Dateibesitz:\n" + prompt)
    command = ["codex", "cloud", "exec", "--env", args.env, "--branch",
               args.branch, "--attempts", "1", query]
    if args.dry_run:
        print(f"Vorschau: env={args.env}, branch={args.branch}, attempts=1")
        print("Modell/Aufwand nur im Prompt angefordert; Cloud-Auswahl separat pruefen.")
        print(query)
        return
    if not args.push_approved:
        raise ValueError("Nutzerfreigabe fuer diesen Push-Branch fehlt (--push-approved).")
    repo = args.repo.resolve()
    run(["git", "check-ref-format", "--branch", args.branch], repo, True)
    head = run(["git", "rev-parse", "HEAD"], repo, True).stdout.strip()
    dirty = run(["git", "status", "--porcelain"], repo, True).stdout
    if dirty:
        raise ValueError("Checkout ist nicht sauber; die Cloud sieht lokale Aenderungen nicht.")
    remote = run(["git", "ls-remote", "--exit-code", "origin",
                  "refs/heads/" + args.branch], repo, True).stdout.split()
    if not remote or remote[0] != head:
        raise ValueError("Remote-Branch fehlt oder stimmt nicht mit lokalem HEAD ueberein.")
    run(["codex", "login", "status"], repo)
    run(command, repo)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="CLI, Login und Cloud-Zugriff lesend pruefen")
    send = sub.add_parser("submit", help="Einen Auftrag absenden, ohne git push")
    send.add_argument("--env", required=True)
    send.add_argument("--repo", type=Path, required=True)
    send.add_argument("--branch", required=True)
    send.add_argument("--prompt-file", type=Path, required=True)
    send.add_argument("--model", choices=MODELS, required=True)
    send.add_argument("--effort", choices=("low", "medium", "high"), required=True)
    send.add_argument("--reason", required=True)
    send.add_argument("--push-approved", action="store_true",
                      help="Nur setzen, wenn der Nutzer diesen Branch freigegeben hat")
    send.add_argument("--dry-run", action="store_true")
    sub.add_parser("list")
    for action in ("status", "diff"):
        sub.add_parser(action).add_argument("task_id")
    args = parser.parse_args()
    try:
        if args.command == "submit":
            if not args.dry_run and not shutil.which("codex"):
                raise ValueError("Codex fehlt; zuerst claude_cloud_setup.sh ausfuehren.")
            submit(args)
        else:
            if not shutil.which("codex"):
                raise ValueError("Codex fehlt; zuerst claude_cloud_setup.sh ausfuehren.")
            if args.command == "doctor":
                doctor()
            elif args.command == "list":
                run(["codex", "cloud", "list", "--json", "--limit", "5"])
            else:
                if args.task_id.startswith("-"):
                    raise ValueError("Ungueltige Task-ID.")
                run(["codex", "cloud", args.command, args.task_id])
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"FEHLER: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
