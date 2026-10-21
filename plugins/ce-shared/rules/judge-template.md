---
name: judge-template
enabled: true
event: bash
action: warn
conditions:
  - field: command
    operator: regex_match
    pattern: \bcodex\s+exec\b
  - field: command
    operator: not_contains
    pattern: pruefung.md
  - field: command
    operator: not_contains
    pattern: Checkliste
---

**Richter ohne Checkliste:** Codex-Prüfungen bekommen immer die Vorlage `~/.claude/templates/pruefung.md` (acht Prüfpunkte,
Tabelle, Schlusszeile) – sonst prüft der Richter, was ihm einfällt (globale Regel › Richter und Selbstkorrektur). `ce_judge.py` kapselt das.
