---
name: actions-sparsam
enabled: true
event: file
action: warn
conditions:
  - field: file_path
    operator: regex_match
    pattern: \.github/workflows/[^/]+\.ya?ml$
---

**GitHub Actions sparsam** (globale Regel in `~/.claude/CLAUDE.md`; 2.000 min/Monat, Budget 0 $ – leer heißt: keine
Läufe in keinem Repo bis zum Monatsanfang): `push` nur auf `main`, sonst über den PR; `concurrency` mit
`cancel-in-progress`; `paths-ignore` für Doku; wenige volle Jobs statt einer Job-Matrix je Testgruppe; große Matrix nur
auf `main` und auf Wunsch; `timeout-minutes` an jedem Job; nur Linux-Runner. Neuer Job/Workflow: Minuten je Lauf ×
Läufe im Monat im Bericht schätzen.
