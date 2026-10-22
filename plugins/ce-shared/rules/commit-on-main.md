---
name: commit-on-main
enabled: true
event: bash
action: block
conditions:
  - field: git_commit
    operator: regex_match
    pattern: (?m)^(?-i:main|master)\s|--allow-empty-message
---

**Kein Commit direkt auf `main`:** jede Arbeit läuft auf einem Branch `claude/<repo>-<version>-<name>` (Reservierung per
`session_name.py --reserve`) oder `claude/<repo>-patch-<kurzname>`; nach `main` kommt nur ein PR mit dem Ja des Nutzers.
