#!/usr/bin/env python3
"""Delegate a coding task to Mistral Vibe (headless) in a throwaway worktree and hand back a patch.

    ./ce vibe run --prompt-file auftrag.txt [--model ALIAS] [--max-turns 40] [--max-price 1.00] [--time-limit 1200] [--json]
    ./ce vibe status
    ./ce vibe clean [--days 14] [--keep 10] [--apply]   # old runs to the Trash, dry run without --apply

The calling agent (Claude Code, Codex, Antigravity) stays responsible: Vibe edits a detached worktree of the current checkout
(with the caller's uncommitted changes and new, not ignored files carried over), the tool saves the result as `changes.patch` and removes the
worktree. The caller reads the patch, applies it with `git apply` (`--3way` only on a clean index) and runs the targeted
tests itself.
Exit codes: 0 finished, 3 a Vibe limit stopped the run (patch is still saved), 1 refused or failed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile

from ce_common import repo_hold

STATE = Path(os.environ.get('CE_VIBE_STATE', Path.home() / '.local/state/nexogen/vibe'))
DEFAULTS = {'max_turns': 40, 'max_price': '1.00', 'time_limit': 1200, 'kill_grace': 120}
# Plain patches whatever the user's git config says (external diff, colour, prefixes, rename detection).
DIFF = ('diff', '--no-ext-diff', '--no-color', '--no-renames', '--src-prefix=a/', '--dst-prefix=b/')
PREAMBLE = """Du arbeitest als Coding-Assistent für einen anderen Agenten (Claude Code oder Codex), der dein Ergebnis prüft.
Regeln für diesen Lauf:
- Arbeite nur in diesem Verzeichnis (einem Wegwerf-Worktree). Keine Dateien außerhalb ändern.
- Kein git commit, kein git push, keine Branches, kein gh; der Aufrufer übernimmt deine Änderungen als Patch.
- Keine Pakete installieren, keine Unity-/Blender-/GPU-Läufe, keine Netzwerkzugriffe außer deinem Modell.
- Halte dich an AGENTS.md des Repos. Ändere nur, was der Auftrag verlangt; keine Tests löschen oder abschwächen.
- Führe die passenden schnellen Tests aus, wenn es welche gibt.
- Schluss: kurze Antwort mit geänderten Dateien, ausgeführten Tests samt Ergebnis und offenen Punkten.

Auftrag:
"""


class VibeError(Exception):
    pass


def vibe_bin():
    found = os.environ.get('CE_VIBE_BIN') or shutil.which('vibe')
    if not found:
        raise VibeError('vibe nicht gefunden (Mistral Vibe CLI installieren oder CE_VIBE_BIN setzen)')
    return found


def git(cwd, *args, check=True, binary=False):
    done = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=not binary)
    if check and done.returncode:
        error = done.stderr if not binary else done.stderr.decode(errors='replace')
        raise VibeError(f'git {" ".join(args[:2])}: {error.strip()}')
    return done.stdout if binary else done.stdout.strip()


def github_slug(root):
    url = git(root, 'remote', 'get-url', 'origin', check=False)
    match = re.search(r'github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$', url or '')
    return match.group(1) if match else None


def is_private(slug):
    """True/False from GitHub, None when unknown (no origin, gh missing or offline)."""
    if not slug:
        return None
    try:
        done = subprocess.run(['gh', 'api', f'repos/{slug}', '--jq', '.private'], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {'true': True, 'false': False}.get(done.stdout.strip()) if done.returncode == 0 else None


def check_repo(root, allow_private):
    hold = repo_hold(root)
    if hold:
        raise VibeError(f'Repo gesperrt (hold): {hold}')
    if allow_private:
        return
    private = is_private(github_slug(root))
    if private is None:
        raise VibeError('Sichtbarkeit des Repos unbekannt (kein GitHub-origin oder gh nicht erreichbar); '
                        'Code geht an Mistral – nur mit --allow-private nach Ja des Nutzers')
    if private:
        raise VibeError('privates Repo: Code geht an Mistral – nur mit --allow-private nach Ja des Nutzers')


def run_dir_for(prompt):
    first = next((line for line in prompt.splitlines() if line.strip()), 'auftrag')
    slug = re.sub(r'[^a-z0-9]+', '-', first.lower())[:40].strip('-') or 'auftrag'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    path = STATE / 'runs' / f'{stamp}-{slug}'
    suffix = 1
    while path.exists():
        suffix += 1
        path = STATE / 'runs' / f'{stamp}-{slug}-{suffix}'
    path.mkdir(parents=True)
    return path


def worker_env(gh_dir):
    """Hurdles, not a sandbox: pushes to origin fail and gh has no login inside the run."""
    env = {k: v for k, v in os.environ.items() if k not in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN')}
    count = int(env.get('GIT_CONFIG_COUNT', '0') or 0)
    env[f'GIT_CONFIG_KEY_{count}'] = 'remote.origin.pushurl'
    env[f'GIT_CONFIG_VALUE_{count}'] = 'ce-vibe-no-push://blocked'
    env['GIT_CONFIG_COUNT'] = str(count + 1)
    env['GH_CONFIG_DIR'] = str(gh_dir)
    env['GIT_TERMINAL_PROMPT'] = '0'
    return env


def snapshot_tree(worktree):
    git(worktree, 'add', '-A')
    return git(worktree, 'write-tree')


def run_worker(command, worktree, env, timeout):
    """Vibe in its own process group, so a timeout also ends the shell commands it started."""
    proc = subprocess.Popen(command, cwd=worktree, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
        end_group(proc.pid)  # background jobs Vibe left behind must not outlive the worktree
        return proc.returncode, out.decode(errors='replace'), err.decode(errors='replace'), False
    except subprocess.TimeoutExpired:
        end_group(proc.pid)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # a detached grandchild still holds the pipes
            proc.kill()
            out, err = b'', b''
        return None, out.decode(errors='replace'), err.decode(errors='replace') + '\nce vibe: harter Abbruch\n', True


def end_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def read_export(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def run(args):
    binary = vibe_bin()
    try:
        prompt = args.prompt_file.read_text(encoding='utf-8').strip()
    except UnicodeDecodeError as error:
        raise VibeError(f'Auftrag ist kein UTF-8: {error}') from error
    if not prompt:
        raise VibeError('Auftrag ist leer')
    root = Path(git(args.repo, 'rev-parse', '--show-toplevel'))
    check_repo(root, args.allow_private)
    base = git(root, 'rev-parse', '--verify', f'{args.base}^{{commit}}')
    own = not args.clean_base and args.base == 'HEAD'
    carry = git(root, *DIFF, '--binary', 'HEAD', binary=True) if own else b''
    untracked = [name for name in git(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0') if name] if own else []

    run_dir = run_dir_for(prompt)
    worktree = run_dir / 'worktree'
    try:
        git(root, 'worktree', 'add', '--detach', str(worktree), base)
    except VibeError:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    warnings = []
    refs_before = git(root, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads', 'refs/tags')
    try:
        if carry:
            done = subprocess.run(['git', '-C', str(worktree), 'apply', '--binary', '-'], input=carry, capture_output=True)
            if done.returncode:
                raise VibeError(f'eigene Änderungen ließen sich nicht übertragen: {done.stderr.decode(errors="replace").strip()}')
        for name in untracked:
            if (root / name).is_dir() and not (root / name).is_symlink():
                warnings.append(f'eingebettetes Repo nicht übertragen: {name}')
                continue
            (worktree / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, worktree / name, follow_symlinks=False)
        base_tree = snapshot_tree(worktree)
        (run_dir / 'prompt.txt').write_text(PREAMBLE + prompt + '\n', encoding='utf-8')
        command = [binary, '--prompt-file', str(run_dir / 'prompt.txt'), '--workdir', str(worktree), '--trust',
                   '--agent', 'auto-approve', '--output', 'text', '--output-dir', str(run_dir / 'export'),
                   '--max-turns', str(args.max_turns), '--max-price', str(args.max_price),
                   '--time-limit', str(args.time_limit)]
        if args.model:
            command += ['--model', args.model]
        with tempfile.TemporaryDirectory(prefix='ce-vibe-gh-') as gh_dir:
            exit_code, answer, log, killed = run_worker(command, worktree, worker_env(gh_dir),
                                                        args.time_limit + DEFAULTS['kill_grace'])
        if killed:
            warnings.append('Vibe hing über das Zeitlimit hinaus und wurde samt Kindprozessen abgebrochen')
        (run_dir / 'answer.txt').write_text(answer, encoding='utf-8')
        (run_dir / 'vibe.log').write_text(log, encoding='utf-8')

        if git(worktree, 'rev-parse', 'HEAD') != base:
            warnings.append('Vibe hat im Worktree committet (gegen die Regeln); der Patch enthält den Stand trotzdem')
        final_tree = snapshot_tree(worktree)
        patch = git(worktree, *DIFF, '--binary', base_tree, final_tree, binary=True)
        (run_dir / 'changes.patch').write_bytes(patch)
        numstat = git(worktree, *DIFF, '--numstat', '-z', base_tree, final_tree)
        if git(root, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads', 'refs/tags') != refs_before:
            warnings.append('Vibe hat Branches oder Tags im Repo verändert – git for-each-ref prüfen')
    finally:
        if not args.keep:
            # The worktree is a throwaway copy and its result is saved as changes.patch above.
            removed_ok = subprocess.run(['git', '-C', str(root), 'worktree', 'remove', '--force', str(worktree)],
                                        capture_output=True).returncode == 0
            git(root, 'worktree', 'prune', check=False)
            if worktree.exists() or not removed_ok:
                warnings.append(f'Worktree ließ sich nicht entfernen: {worktree}')
                print(f'ce vibe: Worktree ließ sich nicht entfernen: {worktree}', file=sys.stderr)

    files, added, removed = [], 0, 0
    for record in filter(None, numstat.split('\0')):
        plus, minus, name = record.split('\t', 2)
        files.append(name)
        added += int(plus) if plus.isdigit() else 0
        removed += int(minus) if minus.isdigit() else 0
    export = read_export(run_dir / 'export' / 'export.json')
    outcome = export.get('outcome') or ('error' if exit_code not in (0, 3) else 'unknown')
    summary = {
        'outcome': outcome, 'stop_reason': export.get('stop_reason'), 'error': export.get('error'),
        'exit_code': exit_code, 'cost_usd': export.get('cost_usd'), 'usage': export.get('usage'),
        'steps': export.get('steps'), 'vibe_version': export.get('vibe_version'),
        'model': args.model or (export.get('config') or {}).get('active_model'),
        'repo': str(root), 'base': base, 'carried_changes': bool(carry or untracked),
        'files': files, 'insertions': added, 'deletions': removed,
        'patch': str(run_dir / 'changes.patch'), 'run_dir': str(run_dir),
        'worktree': str(worktree) if args.keep or worktree.exists() else None, 'warnings': warnings, 'answer': answer.strip(),
    }
    (run_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return summary


def print_summary(summary):
    cost = summary['cost_usd']
    print(summary['answer'] or '(keine Antwort von Vibe)')
    print()
    print(f"Vibe: {summary['outcome']} · {summary['model'] or '?'} · {summary['steps'] or '?'} Schritte · "
          f"{f'{cost:.4f} USD (Listenpreis)' if isinstance(cost, (int, float)) else 'Kosten unbekannt'}")
    if summary['files']:
        print(f"Patch: {len(summary['files'])} Dateien, +{summary['insertions']}/-{summary['deletions']} → {summary['patch']}")
        for name in summary['files']:
            print(f'  {name}')
        print(f"Prüfen, dann übernehmen: git apply {summary['patch']}  (bei Konflikt und sauberem Index: --3way)")
    else:
        print('Patch: keine Änderungen')
    for warning in summary['warnings']:
        print(f'Warnung: {warning}')
    if summary['worktree']:
        print(f"Worktree behalten: {summary['worktree']} (danach: git worktree remove --force …)")


def status():
    binary = vibe_bin()
    version = subprocess.run([binary, '--version'], capture_output=True, text=True).stdout.strip()
    runs = sorted((STATE / 'runs').glob('*')) if (STATE / 'runs').is_dir() else []
    print(json.dumps({'vibe': binary, 'version': version, 'state': str(STATE), 'runs': len(runs),
                      'last_run': str(runs[-1]) if runs else None}, ensure_ascii=False))


def stale_runs(days, keep):
    """Runs older than `days` beyond the newest `keep`; runs with a kept worktree stay (the caller still uses them)."""
    runs = sorted((STATE / 'runs').iterdir(), key=lambda path: path.name) if (STATE / 'runs').is_dir() else []
    limit = datetime.now().timestamp() - timedelta(days=days).total_seconds()
    old = runs[:-keep] if keep else runs
    return [run for run in old if run.is_dir() and not (run / 'worktree').exists() and run.stat().st_mtime < limit]


def clean(days, keep, apply):
    """Moves stale runs to the Trash (CE_TRASH or ~/.Trash), never rm; another volume is refused."""
    trash = Path(os.environ.get('CE_TRASH') or Path.home() / '.Trash')
    stale = stale_runs(days, keep)
    for run in stale:
        if not apply:
            print(f'würde verschieben: {run}')
            continue
        if not trash.is_dir() or os.stat(run).st_dev != os.stat(trash).st_dev:
            raise VibeError(f'Papierkorb fehlt oder liegt auf anderem Volume: {trash}')
        target = trash / f'ce-vibe-{run.name}'
        if target.exists():
            target = trash / f'ce-vibe-{run.name} {datetime.now():%H.%M.%S.%f}'
        os.rename(run, target)
        print(f'im Papierkorb: {target.name}')
    if not stale:
        print('nichts aufzuräumen')
    elif not apply:
        print(f'{len(stale)} Läufe; mit --apply in den Papierkorb')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    tidy = sub.add_parser('clean', help='alte Läufe in den Papierkorb (Probelauf ohne --apply)')
    tidy.add_argument('--days', type=int, default=14, help='nur Läufe älter als so viele Tage')
    tidy.add_argument('--keep', type=int, default=10, help='die jüngsten so vielen Läufe immer behalten')
    tidy.add_argument('--apply', action='store_true')
    call = sub.add_parser('run')
    call.add_argument('--prompt-file', type=Path, required=True, help='Auftrag: Ziel, Pfade, Grenzen, erwartete Tests')
    call.add_argument('--repo', type=Path, default=Path('.'))
    call.add_argument('--base', default='HEAD', help='Startstand (Standard HEAD plus eigene ungesicherte Änderungen)')
    call.add_argument('--clean-base', action='store_true', help='eigene ungesicherte Änderungen nicht mitgeben')
    call.add_argument('--model', help='Vibe-Modellalias (Standard: active_model aus ~/.vibe/config.toml)')
    call.add_argument('--max-turns', type=int, default=DEFAULTS['max_turns'])
    call.add_argument('--max-price', default=DEFAULTS['max_price'], help='USD je Lauf (Vibe-Listenpreis)')
    call.add_argument('--time-limit', type=int, default=DEFAULTS['time_limit'], help='Sekunden')
    call.add_argument('--keep', action='store_true', help='Worktree nach dem Lauf behalten')
    call.add_argument('--allow-private', action='store_true', help='auch private/unbekannte Repos (nur nach Ja des Nutzers)')
    call.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'status':
            status()
            return 0
        if args.command == 'clean':
            clean(args.days, args.keep, args.apply)
            return 0
        summary = run(args)
    except (VibeError, OSError, UnicodeError) as error:
        print(f'ce vibe: {error}', file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print_summary(summary)
    if summary['outcome'] == 'finished' and summary['exit_code'] == 0:
        return 0
    return 3 if summary['exit_code'] == 3 else 1


if __name__ == '__main__':
    sys.exit(main())
