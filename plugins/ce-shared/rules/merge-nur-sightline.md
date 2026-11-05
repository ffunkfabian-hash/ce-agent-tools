---
name: merge-nur-sightline
enabled: true
event: all
action: block
conditions:
  - field: merge_attempt
    operator: equals
    pattern: merge
---

**Merge macht Sightline nach Prüfung – PR-Nummer und Head-SHA ins Postfach** (`./ce session say`). Fachsitzungen mergen
nie selbst: kein `gh pr merge` (auch nicht `--auto`), kein `gh api … PUT …/merge`, keine GitHub-MCP-Werkzeuge
`merge_pull_request` / `enable_pr_auto_merge`. Gemergt wird erst nach Prüfung gegen Git und Ja des Nutzers (Entscheidung
38, 23.10.2026). `git merge origin/main` in den Arbeitsbranch (main einholen) bleibt erlaubt. Sightline ist die Sitzung
mit dem reservierten Namen `Sightline` oder `CE_ROLE=sightline`.

Nach dem Ja des Nutzers darf die Fachsitzung selbst mergen, wenn `merge-check` grün ist: `./ce merge-check <owner/repo>
<PR> --ja "<Wortlaut des Nutzers>"`, dann innerhalb von 30 Minuten aus demselben Ordner `gh pr merge <PR> -R <owner/repo>
--match-head-commit <Head-SHA> --squash`. Nie ohne Ja: Die Quittung hält den Wortlaut fest und beweist ihn nicht.
