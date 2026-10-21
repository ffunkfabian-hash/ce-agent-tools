# Changelog

## 0.5.0 „Split Rail“ – 2026-10-21

* **`heavy_lock.py`: eine Sperre je Ressource** statt einer für alles (`--resource unity|blender|gpu`, Datei
  `heavy-<resource>.lock`; Modul `held(..., resource=)`, `CE_HEAVY_RESOURCE` für Aufrufer ohne Argument). Eine Unity-Regression
  und ein Blender-Bake laufen nebeneinander; zwei Läufe derselben Ressource warten aufeinander. Aufrufer ohne Ressource nutzen weiter
  die gemeinsame Datei `heavy-run.lock` und warten nur aufeinander. `CE_HEAVY_HELD` trägt jetzt die Ressource des Halters: ein
  Kind auf derselben Ressource wartet nicht, eines auf einer anderen reiht sich dort ein. `status` zeigt alle vier Zeilen,
  `holder_pids()` alle Halter (für `pause_sessions.py`). Grund: in den Versionsberichten vom 18.–20.10. standen 20 min bis 2 h
  Wartezeit je Sitzung an der einen Sperre (CE 0.103: 2 h von 3 h 20; CE 0.99: 80 min; CE 0.94: 57 min von 2 h).
* **`ce_check.py nightly`:** dritte Stufe aus `.ce-repo.json` › `check.nightly` – die gebündelten Bildstufen eines Repos
  (volle Stufen, Bakes, Golden-Hashes, Determinismus, volle Regression ohne Anlass) laufen einmal täglich auf `main`, nicht vor
  jedem Commit; Repos ohne den Schlüssel melden das (Exit 2).
* **Nicht mehr schwer:** Aufnahmen (MP4), Screenshots, Kontaktbögen, gezielte Tests und `quick`-Stufen nehmen keine Sperre
  (Regel; die Repos passen ihre Aufrufe an).

## 0.4.0 „Common Rail“ – 2026-10-15

(0.2.0 und 0.3.0 waren das zurückgenommene Plugin ai-relay; die Nummern bleiben unbenutzt.)

* **Vor-Commit-Hook ruft auch `.agents/skills/check_skills.py`** (falls im Repo vorhanden), damit Commit und Stopp-Hook dieselben Prüfungen abdecken; nur Warnung wie bisher.
* **Gemeinsame Werkzeuge** unter `plugins/ce-shared/tools/`, alle lesen `.ce-repo.json` des Repos (`--repo`, Standard
  aktuelles Verzeichnis): `ce_check.py quick|full`, `ce_lint.py`, `ce_release.py` (Trockenlauf, `--execute` nötig),
  `ce_worktrees.py` (alle Worktrees der vier Repos, Markierung „NICHT LÖSCHEN“).
* **`heavy_lock.py`:** die eine Sperre statt dreier Kopien (Cube Escape, Roboter-Generator, Toolgen), Vereinigung:
  Cube-Escape-Fassung (`Groups`, `Interrupted`, `holder_pid`, Befehl `run`/`status`) plus `HeavyLock`-Klasse,
  `run_forwarding` und robuste Signalbehandlung des Roboter-Generators; `held()` liefert das `HeavyLock`-Objekt (liest
  sich auch wie das Halter-Dict). Tests aller drei Repos übernommen (`tests/test_heavy_lock.py`).
* **`ce_release.py --execute`** an einem Wegwerf-Repo mit lokalem Bare-Remote getestet (annotierter Tag, `release/<Version>`, Push, Fehlerfälle Tag/schmutziger Baum/Push scheitert); kein Fehler gefunden.
* **`ce_check.py`:** `quick` ist standardmäßig gezielt (`check.targeted`, `{files}` = geänderte Dateien; `--no-targeted`). `full` wiederholt zuerst das Fehlgeschlagene des letzten Laufs (`rerun_failed`; schlägt es noch fehl, startet der volle Lauf nicht) und weist auf einen zweiten vollen Lauf binnen 30 min ohne Codeänderung hin (Hash von HEAD + Änderungen, Zustand in `~/Library/Caches/CubeEscape/ce_check/`); blockiert nie.
* **`ce_worktrees.py`:** Standardausgabe ist eine Tabelle (Repo | Worktree | Branch | Sitzung mit Titel | sauber? | ahead/behind | Hinweis "NICHT LÖSCHEN"); `--list` die lange Form, `--json` Daten.
* **`ce_sync.py`:** die Repos hängen zur Laufzeit nicht vom Plugin ab. Das Skript schreibt die Werkzeuge als eingebettete
  Kopie nach `<repo>/<tools_dir>` (`.ce-repo.json`, Standard `scripts/ce_tools`; nur Standardbibliothek, Imports relativ)
  mit Kopfzeile „GENERATED from ce-agent-tools <Version> - do not edit“ und SHA-256. `--repo P`, `--all`, `--check`
  (Exit 1 bei fehlender, von Hand geänderter oder gegenüber dem Plugin veralteter Kopie). `ce_lint.py` prüft das mit:
  fehlend/editiert = Fehler, veraltet = Warnung.
* **Hook `precommit.py`:** bei `git commit` mit geänderten `*.md`/Skills läuft `ce_lint.py` und das repo-eigene
  `check_docs.py`; nur Warnung, blockiert nie.

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
