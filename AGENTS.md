# ce-agent-tools – Regeln für KI-Agenten

Zentraler Repo-Einstieg: [Nexogen](https://github.com/ffunkfabian-hash/cubeescape-nexogen), Verzeichnis `repos.json` und Live-Status `./status`. Globale Regeln dort, Werkzeugquelle hier.

Gemeinsames Plugin für Cube Escape, Cubegen und den Roboter-Generator (`README.md`). Antworten, Doku und
Commit-Messages auf Deutsch; Code, Bezeichner und Kommentare auf Englisch.

- **Nur, was alle drei Repos gleich brauchen.** Repo-eigene Regeln bleiben als `.claude/hookify.*.local.md` im Repo;
  Subagents und ihre Profile (`.claude/agents/`, `.claude/agent-guard.json`) gehören ebenfalls ins Repo.
- **Jede Regel mit Tests:** neue oder geänderte Regel → treffende und nicht treffende Befehle in
  `tests/test_guard.py`; `python3 -m unittest discover -s tests` grün vor jedem Commit.
- **Der Wächter blockiert nie aus Versehen alles:** Fehler im Hook lassen den Aufruf durch (mit Meldung); Muster
  vorsichtig, Fehlalarme sind Fehler.
- **Version:** jede Änderung ist eine neue Version (Minor +1, eigener Name, cool und englisch); `plugin.json` und
  `marketplace.json` gemeinsam, Eintrag in `CHANGELOG.md`. Nutzende Repos ziehen nach mit
  `claude plugin marketplace update ce-agent-tools`; ändert sich eine Regel, auch die Hook-Beschreibung in deren
  `AGENTS.md` › „Plugins und Hooks“.
- Committen, taggen und pushen nur auf Wunsch des Nutzers; nie `--force`. Kein GitHub-Workflow ohne Wunsch (globale
  Regel „GitHub Actions sparsam“).

## Plugins und Hooks

- **Eine Entscheidung, drei Clients:** `hooks/guard.py` (`decide`) mit `rules/*.md` ist die einzige Regel-Logik. Claude Code
  ruft sie über das Plugin auf, Codex über `hooks/codex_guard.py`, Antigravity über `hooks/antigravity_guard.py`; Adapter
  übersetzen nur Ein-/Ausgabe, nie eigene Regeln. Korrekturen gehören in `guard.py` und gelten damit überall.
- **Git-Netz:** `githooks/ce_githooks.py` (pre-commit/pre-push) unabhängig vom Client; Grenzen (Optionen, `--no-verify`)
  stehen im README › Git-Netz.
- **Vorabprüfung begrenzt:** `commit-on-main` beurteilt vor dem Lauf nur einen einzelnen einfachen `git … commit` aus
  festen Wörtern (README › Regeln, Feld `git_commit`); alles Zusammengesetzte geht mit Hinweis an das Git-Netz. Keine
  Shell-Nachbildung in `guard.py` – neue Gegenbeispiele werden Laufzeittests des Netzes (`PrecheckBoundaryTests`).
- **Fail-open:** Adapter antworten immer mit Exit 0 und gültigem Clientformat, auch wenn `guard.py` oder `client_adapter.py` nicht lädt; eingetragene
  Befehle reichen nur eine Antwort mit Exit 0 durch, sonst eine Meldung ohne Block (fehlendes Skript, Syntax-/Importfehler;
  Exit 2 hieße in Codex: alles sperren). Git-Wrapper: nur Exitcode `REJECT` ist eine Ablehnung, alles andere ein Startfehler.
- **Belege:** Adaptertests mit nachgebauter Eingabe sind keine Client-Probe; gemessene Eingaben (`CE_HOOK_LOG`) kommen als
  Fixture nach `tests/fixtures/`, Stand je Client im Versionsbericht.

## Erfahrungen

Werkzeug-Fallen und Gründe stehen im [README](README.md), Abschnitt „Erfahrungen“. Schwesterprojekte lesen diese Stelle vor Integrationsänderungen.
