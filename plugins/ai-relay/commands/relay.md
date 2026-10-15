---
description: Aufgabe an kostenlose KI-Anbieter abgeben (review, summarize, classify, docs, ask) oder Anbieter-Status zeigen
argument-hint: <review|summarize|classify|docs|ask|providers|stats> [Optionen]
allowed-tools: Bash
---

Lade den Skill `ai-relay` und führe `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay.py"` passend zu: $ARGUMENTS

- `providers` oder `stats`: Befehl direkt ausführen und das Ergebnis knapp berichten.
- Aufgabe (`review`, `summarize`, …): die Eingabe selbst beschaffen (z. B. `git diff main...HEAD`, Logdatei), klein und
  gezielt halten, dann `relay.py ask --task <Aufgabe>` aufrufen. Antwort als **ungeprüft** weitergeben und Befunde
  selbst verifizieren.
- Ohne Argumente: `providers` ausführen.
