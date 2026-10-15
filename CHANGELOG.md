# Changelog

## 0.3.0 „Roundtable“ – 2026-10-15

* **`ask --panel N`** fragt bis zu N Anbieter parallel und zeigt jede Antwort einzeln (ausgefallene Anbieter erscheinen auf
  stderr); ohne `--panel` bleibt es beim Ausweichen nacheinander.

## 0.2.0 „Backchannel“ – 2026-10-15

Zweites Plugin im Marketplace: **`ai-relay`** (global nutzbar, nicht nur für die drei Repos).

* `scripts/relay.py` (nur Standardbibliothek): `ask` (review, summarize, classify, docs, ask), `providers`, `stats`, `init`.
  Anbieter Ollama (lokal), Gemini, Groq, OpenRouter; DeepSeek (kostenpflichtig, nur mit `--provider deepseek`).
* **Datenschutz zuerst:** ohne `cloud_consent` läuft nur Ollama; Geheimnisse werden vor dem Cloud-Versand ersetzt;
  `local_only_paths` und `AI_RELAY_LOCAL_ONLY` sperren die Cloud je Ordner.
* **Sparsam und robust:** Reihenfolge mit Ausweichen, Pause nach Limit (429), Tageslimits je Anbieter, Nutzungsprotokoll
  ohne Inhalte, Antwort höchstens 15 Zeilen und als „ungeprüft“ gekennzeichnet.
* Skill `ai-relay` (wann abgeben, wann nicht) und Befehl `/relay`; Tests mit Fake-Server in `tests/test_relay.py`.

## 0.1.0 „Toolbox“ – 2026-10-14

Erste Version (mit Cube Escape 0.56 „Pit Crew“, Cubegen 0.23.0 „Wingman“, Roboter-Generator 1.18.0 „Ground Crew“).

* **Gemeinsame Regeln** statt dreifach gepflegter hookify-Dateien: `git-kein-force-push` und `git-stash-geteilt` (vorher in
  allen drei Repos), `unity-nie-auf-checkout` (vorher Cube Escape und Cubegen; jetzt auch `--project-path` und
  `unity open/run/build`), `teleport-nur-frisch` (vorher nur Cube Escape), `actions-sparsam` (vorher nur Cubegen),
  neu `unity-cli-ziel-nennen` (Unity-CLI nur mit `--project-path`, für das Unity-MCP der Testkopie).
* **Force-Push ohne Fehlalarm:** Die alte Regel traf `-f` irgendwo nach dem Wort „push“ – auch `[ -f … ]` in einer
  Schleife über Regeldateien. Jetzt nur innerhalb eines Befehls.
* **Werkzeug-Profile der Subagents** (`.claude/agent-guard.json` im Repo): Bash-Erlaubnislisten und Schreibpfade je
  Agent, durchgesetzt über `agent_type` im Hook-Eingang.
