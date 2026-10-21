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

## Erfahrungen

Werkzeug-Fallen und Gründe stehen im [README](README.md), Abschnitt „Erfahrungen“. Schwesterprojekte lesen diese Stelle vor Integrationsänderungen.
