---
name: ai-relay
description: Kleine, lesende Aufgaben an kostenlose KI-Anbieter (Ollama, Gemini, Groq, OpenRouter) abgeben, um Kontingent zu sparen oder eine zweite Meinung zu holen - Diff auf Fehler prüfen, langes Log oder Testergebnis zusammenfassen, Dateien einstufen, Doku gegen Code abgleichen. Laden bei "zweite Meinung", "kostenlose KI", "Gemini/Groq/Ollama fragen", "Log zusammenfassen", oder wenn eine große Ausgabe nur gelesen und verdichtet werden soll.
---

# ai-relay – Aufgaben an kostenlose KIs abgeben

Skript: `${CLAUDE_PLUGIN_ROOT}/scripts/relay.py` (nur Standardbibliothek). Einmalig `relay.py init`, danach Schlüssel in
`~/.config/ai-relay/keys.env` und `"cloud_consent": true` in `config.json`. **Ohne Zustimmung läuft nur Ollama** – nichts
verlässt den Rechner.

## Wann abgeben

| Gut geeignet | Nicht abgeben |
|---|---|
| Diff auf offensichtliche Fehler prüfen (zweite Meinung) | Code schreiben oder ändern |
| langes Log / Testausgabe verdichten, bevor es im Kontext landet | Entscheidungen, Architektur, Spielgefühl |
| Dateien oder Einträge einstufen (Labels) | alles mit Geheimnissen, Spielständen, privaten Daten |
| Doku gegen Code/Fakten auf Widersprüche abgleichen | Aufgaben, die mehr als die übergebene Eingabe brauchen |

## Aufruf

```bash
git diff main...HEAD -- src | python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay.py" ask --task review
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay.py" ask --task summarize --file build.log
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay.py" ask --task classify --prompt "Labels: bug, doc, chore" --file titles.txt
```

Aufgaben: `review`, `summarize`, `classify`, `docs`, `ask` (die letzten drei brauchen `--prompt`). Weitere Schalter:
`--provider X` (erzwingen), `--max-lines N`, `--json`, `--dry-run` (Reihenfolge und Ersetzungen zeigen, nichts senden).
`relay.py providers` zeigt Schlüssel, Limits, Pausen; `relay.py stats` den Verbrauch.

## Regeln

1. **Antworten sind ungeprüft.** Befunde selbst im Code verifizieren, bevor etwas geändert wird; „Keine Befunde“ von
   einem kleinen Modell ist kein Freispruch (bei großen oder heiklen Diffs ein stärkeres Modell zusätzlich).
2. **Eingabe klein und gezielt:** Diff statt Repo, gefilterte Logs. Generierte Dateien weglassen. Grenze `max_input_chars`.
3. **Datenschutz:** Cloud nur mit `cloud_consent`; Geheimnisse (Schlüssel, Tokens, `password=…`) werden vor dem Senden
   ersetzt, ersetzen aber keine Sorgfalt. Private Repos in `local_only_paths` eintragen oder `AI_RELAY_LOCAL_ONLY=1`
   setzen. Gratis-Dienste können Eingaben fürs Training nutzen.
4. **DeepSeek** ist kostenpflichtig (sehr günstig), Server in China: nur mit `--provider deepseek` und nur für
   unkritische Eingaben.
5. **Ausweichen:** Bei Limit (429) pausiert der Anbieter automatisch, der nächste in der Reihenfolge übernimmt; Exit 3
   heißt, keiner konnte antworten – dann selbst lesen.
