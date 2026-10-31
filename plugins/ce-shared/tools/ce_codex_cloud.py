#!/usr/bin/env python3
"""Claude Cloud -> Codex Cloud; no push, merge, or automatic patch application.

submit requires --eligibility-file: a trusted JSON job classification, version 1:
{ "manifest_version": 1, "kind": "pure_logic", "prompt_sha256": "<file hash>",
  "required_capabilities": {"unity": false, "blender": false, "visuals": false, "viewer": false},
  "scopes": [{"path": "tools/example.py", "kind": "logic_code"}] }
Scope kinds: logic_code, logic_test, logic_data, documentation; paths are individual
repo-relative files. This declaration is not proof of a prompt's semantics or a
remote sandbox. Unknown classifications fail closed, including in --dry-run.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile

WINDOWS = os.name == "nt"  # (0.31)
MODELS = ("gpt-6-luna", "gpt-6.1-sol")
CAPABILITIES = {"unity", "blender", "visuals", "viewer"}
CODE_SUFFIXES = {".py", ".cs", ".js", ".ts", ".mjs", ".cjs", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".hpp", ".sh"}
SCOPE_SUFFIXES = {"logic_code": CODE_SUFFIXES, "logic_test": CODE_SUFFIXES,
                  "logic_data": {".json", ".csv", ".tsv", ".yaml", ".yml", ".toml"},
                  "documentation": {".md", ".rst", ".txt"}}
BOUNDARY = """Cloud-Auftrag: Lies die AGENTS.md im Zielrepo und halte deren Regeln ein.
Nur die unten genannten Dateien bearbeiten. Keine Releases, Merges, Pushes oder
GitHub-Actions-Aenderungen. Keine Render-/Bake-/Bild-/Golden-/Hash-Pruefungen,
Viewer-Builds oder Unity-Laeufe. Braucht der Auftrag diese Faehigkeiten, abbrechen
und den Mac-Pruefweg nennen. Keine weiteren Agenten starten. Keine Wartemeldungen:
mit Ergebnis, Fehler oder notwendiger Entscheidung abschliessen, max. 15 Zeilen.
Die erste Modellzeile ist eine Anforderung; melde, falls sie nicht erfuellbar ist.
"""


def run(argv, cwd=None, capture=False):
    if WINDOWS:
        return run_windows(argv, cwd, capture)
    if argv[0] == "codex":
        # Cloud CLI 0.162.1 writes error.log in cwd, even for read-only listing.
        # Explicit --env/--branch select the target; no project cwd is needed.
        with tempfile.TemporaryDirectory(prefix="ce-codex-cloud-") as scratch:
            return subprocess.run(argv, cwd=scratch, text=True, check=True,
                                  capture_output=capture)
    return subprocess.run(argv, cwd=cwd, text=True, check=True,
                          capture_output=capture)


def run_windows(argv, cwd=None, capture=False):
    """(0.31) The same on Windows: npm's codex.cmd is started by its full name (CreateProcess finds only .exe by a
    bare name), output is read as UTF-8 instead of cp1252."""
    codex = argv[0] == "codex"
    command = [shutil.which(argv[0]) or argv[0], *argv[1:]]
    with tempfile.TemporaryDirectory(prefix="ce-codex-cloud-") as scratch:
        return subprocess.run(command, cwd=scratch if codex else cwd, text=True, check=True, capture_output=capture,
                              encoding="utf-8", errors="replace")


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


def eligibility(path, repo, prompt_bytes):
    """Validate a trusted declaration; never infer task suitability from keywords."""
    if path is None:
        raise ValueError("Cloud-Eignungsnachweis fehlt (--eligibility-file).")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Cloud-Eignungsnachweis: doppelter JSON-Schlüssel {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Cloud-Eignungsnachweis: ungültige JSON-Zahl")

    manifest = json.loads(Path(path).read_text(encoding="utf-8"),
                          object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
    fields = {"manifest_version", "kind", "prompt_sha256", "required_capabilities", "scopes"}
    if not isinstance(manifest, dict) or set(manifest) != fields:
        raise ValueError("Cloud-Eignungsnachweis: unbekannte oder fehlende Felder.")
    if type(manifest["manifest_version"]) is not int or manifest["manifest_version"] != 1:
        raise ValueError("Cloud-Eignungsnachweis: unbekannte Manifestversion.")
    if manifest["kind"] != "pure_logic":
        raise ValueError("Cloud-Eignungsnachweis: nur pure_logic ist zulässig.")
    caps = manifest["required_capabilities"]
    if not isinstance(caps, dict) or set(caps) != CAPABILITIES or any(value is not False for value in caps.values()):
        raise ValueError("Cloud-Eignungsnachweis: Unity/Blender/Bild/Viewer müssen explizit false sein.")
    if manifest["prompt_sha256"] != hashlib.sha256(prompt_bytes).hexdigest():
        raise ValueError("Cloud-Eignungsnachweis gehört nicht zu dieser Auftragsdatei (prompt_sha256).")
    root = Path(repo).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Cloud-Eignungsnachweis: Repo ist kein Verzeichnis.")
    scopes = manifest["scopes"]
    if not isinstance(scopes, list) or not scopes:
        raise ValueError("Cloud-Eignungsnachweis: mindestens ein Dateiscope erforderlich.")
    seen = set()
    for scope in scopes:
        if not isinstance(scope, dict) or set(scope) != {"path", "kind"}:
            raise ValueError("Cloud-Eignungsnachweis: unbekannte Scopefelder.")
        kind, name = scope["kind"], scope["path"]
        if not isinstance(kind, str) or kind not in SCOPE_SUFFIXES or not isinstance(name, str):
            raise ValueError("Cloud-Eignungsnachweis: unbekannte Scopeart oder ungültiger Pfad.")
        parts = PurePosixPath(name).parts
        if (not parts or PurePosixPath(name).is_absolute() or str(PurePosixPath(name)) != name
                or any(part.startswith('.') for part in parts)
                or any(char in name for char in "\\*?[]{}:\x00\r\n")):
            raise ValueError(f"Cloud-Eignungsnachweis: unsicherer Dateiscope {name!r}.")
        engine_area = False
        for index, part in enumerate(parts):
            directory = part.casefold()
            prefix = root.joinpath(*parts[:index])
            unity_root = (prefix / 'ProjectSettings').is_dir()
            if (directory == 'assets' and (len(parts) <= index+2 or parts[index+1].casefold() not in {'scripts', 'tests'})):
                engine_area = True
            if directory in {'projectsettings', 'packages', 'library', 'usersettings', 'temp', 'logs'}:
                if index == 0 or unity_root:
                    engine_area = True
                if directory == 'packages' and index+1 < len(parts) and parts[index+1].casefold() in {'manifest.json', 'packages-lock.json'}:
                    engine_area = True
        if engine_area or parts[-1].casefold() in {'azure-pipelines.yml', 'bitbucket-pipelines.yml', 'appveyor.yml'}:
            raise ValueError(f"Cloud-Eignungsnachweis: Engine-/Assetbereich nicht zulässig: {name}.")
        if PurePosixPath(name).suffix.lower() not in SCOPE_SUFFIXES[kind]:
            raise ValueError(f"Cloud-Eignungsnachweis: Dateityp für {kind} nicht zulässig: {name}.")
        if name in seen:
            raise ValueError(f"Cloud-Eignungsnachweis: doppelter Dateiscope {name}.")
        seen.add(name)
        candidate = root
        for part in parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise ValueError(f"Cloud-Eignungsnachweis: Symlink im Dateiscope {name}.")
            if candidate != root / name and candidate.exists() and not candidate.is_dir():
                raise ValueError(f"Cloud-Eignungsnachweis: ungültiger Elternpfad {name}.")
        if not candidate.resolve().is_relative_to(root) or (candidate.exists() and not candidate.is_file()):
            raise ValueError(f"Cloud-Eignungsnachweis: kein sicherer einzelner Dateipfad {name}.")
    return manifest


def submit(args):
    prompt_bytes = args.prompt_file.read_bytes()
    prompt = prompt_bytes.decode("utf-8").strip()
    if not prompt:
        raise ValueError("Auftragsdatei ist leer.")
    if args.branch in ("main", "master") or args.branch.startswith("-"):
        raise ValueError("Expliziten Arbeitsbranch angeben, nicht main/master.")
    if not args.reason.strip():
        raise ValueError("Modellgrund fehlt.")
    if not args.dry_run and not args.push_approved:
        raise ValueError("Nutzerfreigabe fuer diesen Push-Branch fehlt (--push-approved).")
    manifest = eligibility(getattr(args, "eligibility_file", None), args.repo, prompt_bytes)
    ownership = "\n".join(scope["path"] for scope in manifest["scopes"])
    query = (f"{args.model}, `{args.effort}`, {args.reason}.\n\n" + BOUNDARY
             + "\nAlleiniger Dateibesitz (verbindlich):\n" + ownership + "\n\nAuftrag:\n" + prompt)
    command = ["codex", "cloud", "exec", "--env", args.env, "--branch",
               args.branch, "--attempts", "1", query]
    if args.dry_run:
        print("Cloud-Eignung: " + json.dumps({"eligible": True, **manifest}, ensure_ascii=False, sort_keys=True))
        print(f"Vorschau: env={args.env}, branch={args.branch}, attempts=1")
        print("Modell/Aufwand nur im Prompt angefordert; Cloud-Auswahl separat pruefen.")
        print("Vertrauensmanifest geprüft; Auftragssemantik und Remote-Dateibesitz werden nicht technisch bewiesen.")
        print("Umgebung/Login/Zielrepo im Dry-Run ungeprüft; doctor prüft den Zugriff lesend.")
        return
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
    check_environments()
    run(["codex", "cloud", "list", "--env", args.env, "--json", "--limit", "1"], capture=True)
    try:
        run(command, repo)
    except subprocess.CalledProcessError as error:
        # CalledProcessError.__str__ includes argv, hence the private prompt.
        raise ValueError(f"Cloud-Auftrag fehlgeschlagen (Exit {error.returncode}); kein automatischer Neuversuch.") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="CLI, Login und Cloud-Zugriff lesend pruefen")
    send = sub.add_parser("submit", help="Einen Auftrag absenden, ohne git push")
    send.add_argument("--env", required=True)
    send.add_argument("--repo", type=Path, required=True)
    send.add_argument("--branch", required=True)
    send.add_argument("--prompt-file", type=Path, required=True)
    send.add_argument("--eligibility-file", type=Path,
                      help="Pflichtnachweis als JSON v1: pure_logic, Prompt-SHA256, false-Capabilities, einzelne Dateiscopes")
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
    try:  # (0.31) Windows: a cp1252 console or pipe cannot take the tool's „“ → ·
        import ce_common as _console
        _console.utf8_console()
    except ImportError:
        pass
    sys.exit(main())
