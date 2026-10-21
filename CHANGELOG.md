# Changelog

## 0.7.0 „Clearpath“ – 2026-10-21

- Richter wählt nach Risiko und Diffgröße: Spielstand/Einstellungen, Kernpfade aus der CE-Testkarte und Prüf-/Paket-/Sperrinfrastruktur gehen auch bei kleinen Diffs an Sol; Routine bleibt Luna. CLI nennt den Grund und macht manuelle Abweichungen sichtbar.
- Sitzungsbericht trennt frischen Input, Cache-Schreiben/-Lesen und Output; Streaming-Zähler werden pro Nachrichten-ID dedupliziert. Absolute Inputmenge, Mittel/Maximum und Messabdeckung verhindern eine irreführende Bewertung allein nach Trefferquote. Fehlende Usage bleibt unbekannt.
- [Version](docs/versions/0.7.md); keine bezahlten API-Aufrufe und keine Änderungen an Lockstep.

## 0.6.0 „Pactguard“ – 2026-10-21

- Gemini Antigravity in gemeinsamen Werkzeugen sichtbar gemacht: `ce_session_name.py` erkennt `ANTIGRAVITY_*` (`Gemini-Mac`, Branch-Präfix `gemini/`), `ce_worktrees.py` schützt aktive Antigravity-Arbeitsbäume (`language_server`/`antigravity`), und `ce_report.py` wertet neben Claude-Transcripts auch Antigravity-Transcripts (`~/.gemini/antigravity/brain/*/.system_generated/logs/transcript.jsonl`) aus.
- Vertragswächter lehnen doppelte Schlüssel, nichtendliche Zahlen und falsche Arm-/Achsentypen ab; Arm-Referenzen werden aufgelöst. Lesender Interface-Audit verwendet die nativen Fachrepo-Prüfungen und ein Robogen-Abnehmerprofil.
- Richter akzeptiert nur Modellantworten auf stdout; ungültige Indexpfade ändern keinen Index, C# wird gefunden, Kontaktbögen halten Breite und Transparenz ein. Alle bestätigten Fehler mit Gegenproben abgesichert.
- Kopierpakete werden vor der ersten Ausgabe vollständig geprüft; Prüftiefen und verbleibende Bild-/Rig-Abnahmen bleiben ausdrücklich sichtbar. [Version](docs/versions/0.6.md).

## 0.5.0 „Split Rail“ – 2026-10-21

- Mac-Nachprüfung: Arbeitskopie-Diffs im Richter, echte launchd-Aktivierung, konkrete Nachtlauf-Auswahl und Sperrhalter-Anzeige korrigiert. Nexogen-Verzeichnis mit alten Pfad-Aliasen unterstützt; 142 Werkzeugtests grün.

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
* **`ce_docs.py`:** der eine Doku-Prüfer aller Repos (Links, Anker nach GitHub-Regel, Code-Zäune und Inline-Code ausgenommen,
  SKILL.md-Front-Matter, JSON, `bash -n`), Listen in `.ce-repo.json` › `docs` (`check`, `json`, `shell`); Cube Escapes `check_docs.py`
  behält nur das Fehlerregister und ruft ihn, Toolgens ist ein Wrapper. Fand beim ersten Lauf einen toten Anker in CE.
* **`ce_nightly.py`:** ein Nachtlauf für alle Repos (`--all`, `CE_REPOS`), ruft je Repo `ce_check nightly` nacheinander, ein Bericht
  unter `~/Library/Caches/CubeEscape/nightly/`, macOS-Benachrichtigung; `install` legt den LaunchAgent (03:00) an.
* **Werkzeuge aus cubeescape-rules hierher:** `ce_session_name.py` (jetzt mit `--reserve`: der Branch auf `origin` ist die Reservierung,
  `--topics`), `ce_await_mail.py` (liest das Postfach-Issue #16), `ce_codex_quota.py`, `ce_codex_cloud.py` samt Tests; das Regel-Repo
  behält dünne Wrapper unter `templates/`. Alle neuen Dateien in `SYNC_FILES`.
* **Verträge als Schema:** `plugins/ce-shared/contracts/unity_contract.v4.json` (JSON Schema, aus den 41 importierten Toolgen-Verträgen
  abgeleitet und gegen alle grün) und `ce_contract.py validate` (Teilmenge von JSON Schema ohne Abhängigkeit; Formate additiv, ein
  Format-5-Vertrag prüft gegen v4, bis v5 existiert). Toolgen prüft beim Export (`verify_output.py`), Cube Escape beim Import
  (`Tools/tests/test_toolgen_contracts.py`); `ce_sync` kopiert die Schemas nach `<tools_dir>/contracts/`.
* **`ce_search.py` – lokales RAG:** SQLite-FTS5-Index (BM25) über Markdown, Lehren, Skills, Versionsberichte, Datendateien und
  Code-Kommentare aller Repos, in Abschnitte je Überschrift zerlegt (1 700 Dateien / 14 000 Abschnitte in 1,6 s, 20 MB), inkrementell
  nach Inhaltshash; optionale Embeddings (`sentence-transformers`, hybrides Ranking) ohne Pflicht. Lehren und Skills vor Docs, Code zuletzt.
  CLI `index|query|status` und MCP-Server (`serve`, stdio; Plugin `.mcp.json` „ce-search“ mit `ce_search`, `ce_search_status`).
  `ce_nightly` baut den Index nach den Läufen neu. Tests `SearchTests`.
* **Durchsetzung statt Prosa** (Prüfung Teil 2): neue gemeinsame Hooks `heavy-resource` (block: `heavy_lock run` ohne `--resource`),
  `commit-on-main` (block), `heavy-unlocked`, `output-cap`, `judge-template` (warn); **SessionStart-Hook** gibt Werkzeugzeile, Sitzungsnamen-
  Vorschlag und `docs/LESSONS.md` aus; **Stop-Hook** misst die Sitzung mit `ce_report.py` (Modell je Unteragent, wiederholte Prüfläufe,
  Suchen vs. lange Docs, größte Ausgabe, Wartezeit an der Sperre, Nachrichten) in `report.md` und warnt bei Code ohne `./check`.
* **`ce_imagediff.py`** (SSIM/PSNR/Pixelanteil/Bereich + Diffbild, Schwellen je Bildart), **`ce_media.py`** (`sheet|clip|still`, feste Größen
  480p/≤ 8 s/≤ 1280 px), **`ce_judge.py`** (Codex-Richter mit `pruefung.md`, Modell nach Diffgröße/Vertragsdateien, `--opus` für einen
  Opus-Unteragenten), **`ce`** (ein Einstieg: `./ce check|reserve|search|judge|diff|sheet|clip|lock|status|docs|contract|report|sync`).
* **Agent-Vorlagen** `agents/ce-judge.md` (Opus, read-only), `ce-recorder.md` (Sonnet, Belege), `ce-searcher.md` (Haiku, ≤ 10 Zeilen) – `ce_sync`
  kopiert sie nach `<tools_dir>/agents/`; Repos verlinken sie aus `.claude/agents/`.
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

### Gegenprüfung und Fortsetzung auf dem Mac (21.10.2026)

- `./ce` nach Sitzung, Codex und Beleg gruppiert, alte Aufrufe kompatibel; Wochenbericht `report --since 7d`.
- Gesamtsperre koordiniert alte und neue Aufrufer; `gpu` exklusiv, verschachtelte globale Upgrades schlagen verständlich fehl.
- Richter erhält Prompt und Diff über stdin; Prozessfehler und unklare Schlusszeilen gelten nicht als Freigabe.
- Suchlauf spart ausgeschlossene Bäume aus, findet tatsächliche Mac-Pfade; Synchronisierung prüft Schemas und Agenten mit.
- Nachtlauf: kein Index/Meldung im Trockenlauf, sauberes main, kein Grün ohne Tests, `caffeinate`, optionale deduplizierte Fehler-Issues.
- Strukturschema für vorhandenes Arm-Paket (Vertrag 5); semantische Abnahme bleibt beim Produzenten/Abnehmer.
