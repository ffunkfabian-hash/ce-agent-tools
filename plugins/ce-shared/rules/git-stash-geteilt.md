---
name: git-stash-geteilt
enabled: true
event: bash
action: block
conditions:
  - field: command
    operator: regex_match
    pattern: \bgit\s+(-C\s+("[^"]*"|'[^']*'|\S+)\s+)?stash(\s+(pop|clear|-u|--include-untracked))?\s*($|[;&|)])
---

**Kein nacktes `git stash` / `git stash pop`:** Der Stash-Stapel ist mit dem Haupt-Checkout und allen Worktrees
geteilt – man könnte die Änderungen einer anderen Sitzung erwischen. Besser ein temporärer WIP-Commit. Wenn es sein
muss: `git stash push -u -m "<eindeutiger-tag>"`, SHA per `git stash list --format='%H %gs'` merken und mit
`git stash apply <sha>` zurückholen.
