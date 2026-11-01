---
name: heavy-unlocked
enabled: true
event: bash
action: warn
conditions:
  - field: command
    operator: regex_match
    pattern: (verify\.sh\s+run\s+--full|testplan\.py\s+run\b.*--full|generate_tools\.py\s+.*--tier\s+(standard|full)|check_output\.sh|generate_robot\.py\s+.*--tier\s+full)
  - field: command
    operator: not_contains
    pattern: heavy_lock
  - field: command
    operator: not_contains
    pattern: CE_HEAVY_LOCK=0
---

**Schwerer Lauf ohne sichtbare Sperre:** Die Werkzeuge nehmen die Sperre selbst (`testplan.py`, `generate_tools.py`, `check_output.sh`),
aber nur mit der richtigen Ressource (der Lader `./ce` liefert den gepinnten Stand, `./ce tools where`). Vorher `heavy_lock.py status`; ist die Ressource
belegt, erst alles Nicht-Schwere erledigen (globale Regel › Schwere Läufe und Sperren).
