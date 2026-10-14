---
name: teleport-nur-frisch
enabled: true
event: bash
action: block
conditions:
  - field: command
    operator: regex_match
    pattern: \bclaude\s+(.*\s)?--teleport\b
---

**Kein `claude --teleport` aus einer Sitzung heraus:** Das Teleportieren checkt den Branch der Cloud-Sitzung im
aktuellen Ordner aus und hat am 12. Oktober 2026 zweimal den unkommitteten Stand von Cube Escape 0.46 verworfen (einmal
ohne Sicherung). Den Nutzer bitten, es selbst in einem **frischen, leeren Ordner** zu starten (`cd ~` oder ein neuer
Worktree) – nie in einem Worktree mit offenen Änderungen. Ist es doch passiert: `git reflog`, `git stash list`
(„Teleport auto-stash“, per SHA mit `apply` zurückholen) und das Sitzungsprotokoll.
