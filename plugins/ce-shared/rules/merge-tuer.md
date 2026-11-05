---
name: merge-tuer
enabled: true
event: all
action: block
conditions:
  - field: merge_attempt
    operator: equals
    pattern: merge
---

**Merge nach Regel 4 nur durch die Merge-Tür:** erst `./ce merge-check <owner/repo> <PR>`, dann innerhalb von 30 Minuten aus
demselben Ordner `gh pr merge <PR> -R <owner/repo> --match-head-commit <Head-SHA> --squash`. Zu bleiben `--auto`, `--admin`,
`gh api … PUT …/merge` und die GitHub-MCP-Werkzeuge `merge_pull_request` / `enable_pr_auto_merge`. `git merge origin/main` in
den Arbeitsbranch (main einholen) bleibt erlaubt.

1. **Stufe 1, ohne Ja:** Patch bis 100 Zeilen ohne Format, Vertrag und Kernpfad, `core_paths` in der Basis. Der grüne Lauf meldet
   `ohne_ja: true` und schreibt die Quittung der Art `patch` selbst. Wer zweifelt, nimmt Stufe 2.
2. **Stufe 2, nach Ja:** alles andere. Der Nutzer sagt Ja in der Sitzung, dann `./ce merge-check <owner/repo> <PR> --ja
   "<Wortlaut des Nutzers>"`. Nie ohne Ja: Die Quittung hält den Wortlaut fest und beweist ihn nicht.
3. **Stufe 3, Sightline:** Cloud-Sitzungen und jede Sitzung, die nicht selbst mergen darf, schicken eine Merge-Bitte mit
   PR-Nummer und Head-SHA ins Postfach (`./ce session say`). Sightline ist die Sitzung mit dem reservierten Namen `Sightline`
   oder `CE_ROLE=sightline`.
