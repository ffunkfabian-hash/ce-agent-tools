---
name: commit-on-main
enabled: true
event: bash
action: block
conditions:
  - field: command
    operator: regex_match
    pattern: \bgit\s+(-C\s+("[^"]*"|'[^']*'|\S+)\s+)?(checkout|switch)\s+(-b\s+)?(main|master)\b.*&&.*\bgit\s+commit\b|\bgit\s+commit\b.*--allow-empty-message
---

**Kein Commit direkt auf `main`:** jede Arbeit läuft auf einem Branch `claude/<repo>-<version>-<name>` (Reservierung per
`session_name.py --reserve`) oder `claude/<repo>-patch-<kurzname>`; nach `main` kommt nur ein PR mit dem Ja des Nutzers.
