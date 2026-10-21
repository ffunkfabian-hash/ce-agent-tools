# ce-agent-tools

Gemeinsame Agenten-Werkzeuge für **Cube Escape**, **Cubegen** und den **Roboter-Generator** – ein Claude-Code-Marketplace
mit einem Plugin, `ce-shared`. Was alle drei Repos gleich brauchen, steht hier einmal statt dreimal; Repo-eigene
Regeln bleiben als hookify-Dateien in `.claude/` des jeweiligen Repos.

## Inhalt

| Teil | Wirkung |
|---|---|
| `plugins/ce-shared/rules/*.md` | gemeinsame Regeln im hookify-Format (siehe unten), ausgewertet bei jedem Bash-/Datei-Werkzeug |
| `plugins/ce-shared/hooks/guard.py` | der eine PreToolUse-Hook: gemeinsame Regeln + Werkzeug-Profile der Subagents |
| `plugins/ce-shared/hooks/precommit.py` | PreToolUse vor `git commit`: bei geänderten Docs/Skills `ce_lint.py` + repo-eigenes `check_docs.py`, nur Warnung |
| `plugins/ce-shared/tools/` | gemeinsame Werkzeuge (siehe „Werkzeuge“), lesen `.ce-repo.json` des Repos |
| `tests/test_guard.py` | jede Regel mit treffenden und nicht treffenden Befehlen, Gleichstand mit den ersetzten hookify-Regeln, Profile |

### Gemeinsame Regeln

| Regel | Art | Inhalt |
|---|---|---|
| `git-kein-force-push` | blockiert | `git push --force/-f/--mirror/+ref` (nur innerhalb eines Befehls – kein Fehlalarm mehr bei `[ -f … ]` nach einem Push) |
| `git-stash-geteilt` | blockiert | nacktes `git stash` / `pop` / `clear` (Stash ist mit allen Worktrees geteilt) |
| `teleport-nur-frisch` | blockiert | Teleport einer Cloud-Sitzung aus einer laufenden Sitzung heraus (verwarf zweimal offene Arbeit) |
| `unity-nie-auf-checkout` | blockiert | Unity oder die Unity-CLI mit einem Cube-Escape-Checkout als Projekt (`-projectPath`, `--project-path`, `unity open/run/build`) |
| `unity-cli-ziel-nennen` | blockiert | `unity mcp/command/list/job/eval/pipeline install` ohne `--project-path` (die CLI nähme sonst irgendeinen Editor) |
| `actions-sparsam` | warnt | Bearbeiten eines GitHub-Workflows: globale Regel „GitHub Actions sparsam“ |

Format wie hookify (`event: bash|file`, `conditions` mit `field` command/file_path/content/new_text/old_text, Operatoren
`regex_match` (ohne Groß/klein), `contains`, `not_contains`, `equals`, `starts_with`, `ends_with`; `action: warn|block`).
Eine neue Regel braucht Fälle in `SharedRuleTests.CASES`.

### Subagent-Profile

Läuft ein Werkzeug in einem Subagent, nennt der Hook-Eingang ihn (`agent_type`). Hat das Repo für ihn ein Profil in
`.claude/agent-guard.json`, gilt:

```json
{"agents": {"ce-test-runner": {
  "readonly_shell": true,
  "bash": ["[\"']?\\.agents/skills/ce-testing/scripts/verify\\.sh[\"']?\\s+(plan|run|test)\\b"],
  "edit": [],
  "deny_edit": []}}}
```

- **bash:** Jeder Teilbefehl (getrennt an `;`, `&`, `|`, `&&`, `||`, Zeilenende außerhalb von Anführungszeichen) muss
  am Anfang einem Muster entsprechen; `readonly_shell` erlaubt zusätzlich lesende Befehle (`cat`, `grep`, `ls`, `find`
  ohne `-delete/-exec`, `sed -n`, `jq`, lesendes `git` …). Absolute Pfade ins Repo werden vorher relativ. Umleitungen in
  Dateien, Befehlsersetzung und Heredocs sind gesperrt (`2>&1` und `>/dev/null` gehen).
- **edit / deny_edit:** Globs relativ zum Repo (Symlinks aufgelöst, `.claude/skills` → `.agents/skills`).
- Ohne Profil (Hauptunterhaltung, fremde Agenten) entscheidet der Wächter nichts.

Die `tools`-Zeile der Agenten-Datei legt fest, *welche* Werkzeuge ein Subagent hat; das Profil, *was* er damit tun darf.

## Werkzeuge

Alle mit `--repo <Pfad>` (Standard: aktuelles Verzeichnis, gesucht wird `.ce-repo.json` aufwärts). Die Repos rufen sie über
dünne Wrapper (`CE_TOOLS` oder `~/.claude/plugins/marketplaces/ce-agent-tools/plugins/ce-shared/tools`).

| Aufruf | Wirkung |
|---|---|
| `ce` | ein Einstieg für alles unten (`./ce check`, `./ce reserve <Name>`, `./ce search <q>`, `./ce judge`, `./ce diff A B`, `./ce clip <frames> --out x.mp4`, `./ce lock unity -- <cmd>`) |
| `ce_report.py [transcript]` | misst eine Sitzung aus dem Transcript (Modelle, Prüfläufe, Suchen, Ausgaben, Wartezeit); der Stop-Hook hängt die Zeile an `report.md` |
| `ce_imagediff.py A B [--out D] [--kind ui\|world\|bake]` | SSIM/PSNR/Pixelanteil/Bereich und Diffbild – Zahlen für den Richter statt zwei Bilder |
| `ce_media.py sheet\|clip\|still` | Kontaktbogen, MP4 (480p, ≤ 8 s), Standbild (≤ 1280 px) – die Belege der Regel › Belege |
| `ce_judge.py [--model luna\|sol] [--opus]` | Codex-Richter mit der Checkliste `pruefung.md`; Modell nach Diffgröße; `--opus` erzeugt den Prompt für einen Opus-Unteragenten |
| `ce_search.py index\|query\|serve\|status` | lokale Suche (FTS5, optional Embeddings) über Docs/Lehren/Skills/Berichte/Code-Kommentare aller Repos; MCP-Server `ce-search` |
| `ce_contract.py validate <json…> \| list` | Verträge zwischen Repos gegen `contracts/<name>.v<format>.json` prüfen (Generator beim Export, Abnehmer beim Import) |
| `ce_interfaces.py` / `./ce contract audit` | Lesender Abgleich über native Formgen-/Cubegen-/Motiongen-Prüfungen, optional Toolgen-Provenance und Robogen-Abnehmerprofil; explizite Repo-Pfade und Cubegen-Vertragsversion |
| `ce_docs.py [--json R]` | Markdown-Links/-Anker, SKILL-Front-Matter, JSON, Shell-Syntax nach `.ce-repo.json › docs` – der eine Doku-Prüfer |
| `ce_nightly.py [--all\|--repo P] [install\|status]` | ein Nachtlauf für alle Repos (`check.nightly` je Repo), Bericht, LaunchAgent |
| `ce_session_name.py [--name N] [--reserve] [--topics]` | Sitzungsname und Branch; `--reserve` pusht den Branch (= Reservierung) |
| `ce_await_mail.py --name N [--issue …]` | wartet auf eine Nachricht an diese Sitzung im Postfach-Issue (cubeescape-nexogen#16) |
| `ce_codex_quota.py`, `ce_codex_cloud.py` | Codex-Kontingent lesen, Codex-Cloud-Aufträge |
| `heavy_lock.py run --resource unity\|blender\|gpu [--what X] [--checkout P] -- <Befehl>` / `status` | eine Sperre **je Ressource** für schwere Läufe (`~/Library/Caches/CubeEscape/heavy-<resource>.lock`; ohne `--resource` die alte gemeinsame Datei `heavy-run.lock`), hält bis zum letzten Enkelprozess; Unity-Regression und Blender-Bake laufen nebeneinander, zwei Läufe derselben Ressource nacheinander. Aufnahmen, Screenshots, Kontaktbögen, gezielte Tests und `quick`-Stufen sind nicht schwer und nehmen keine Sperre. Als Modul: `held(..., resource=)`, `HeavyLock`, `holder_pids()`, `Groups`, `run_group`/`run_forwarding` |
| `ce_check.py quick\|full\|nightly [--verbose] [--no-targeted]` | Befehle aus `.ce-repo.json › check`; Ergebnis zuerst; `full` wiederholt nach Fehlern einmal `check.rerun_failed`; `nightly` = gebündelte Bildstufen einmal täglich auf `main` (0.5.0) |
| `ce_lint.py [--strict]` | Längen (`limits`), AGENTS.md-Abschnitte, keine Notiz-Dateien im Hauptordner, Versionsbericht/CHANGELOG |
| `ce_release.py [--execute] [--no-push] [--remote R]` | Trockenlauf Standard: prüft Version, CHANGELOG, sauberen Baum, Tag `<tag_prefix><Version>`, Branch `<branch_prefix><Version>`, Remote; mit `--execute` Tag, Branch, Push (nie `--force`) |
| `ce_sync.py [--repo P\|--all] [--check]` | schreibt die Werkzeuge als eingebettete Kopie nach `<repo>/<tools_dir>` (Standard `scripts/ce_tools`, Kopfzeile + Prüfsumme); `--check` meldet abweichende/editierte Kopien (auch in `ce_lint.py`). Die Repos rufen nur diese Kopien, nie das Plugin |
| `ce_worktrees.py [--repos P…] [--active-minutes N] [--list\|--json]` | Tabelle (Standard) der Worktrees der vier Repos (`CE_REPOS` überschreibt): Branch, sauber, ahead/behind, Sitzung (`~/.claude/projects`, laufende Prozesse), „NICHT LÖSCHEN“ |

## Einbinden

In `.claude/settings.json` jedes Repos:

```json
{
  "extraKnownMarketplaces": {
    "ce-agent-tools": {"source": {"source": "github", "repo": "ffunkfabian-hash/ce-agent-tools"}}
  },
  "enabledPlugins": {"ce-shared@ce-agent-tools": true}
}
```

Claude Code bietet beim Vertrauen des Ordners an, den Marketplace hinzuzufügen und das Plugin zu installieren; von Hand:
`claude plugin marketplace add ffunkfabian-hash/ce-agent-tools` und `claude plugin install ce-shared@ce-agent-tools`.
Aktualisieren: `claude plugin marketplace update ce-agent-tools`. Ohne Plugin fehlen die gemeinsamen Regeln – Codex und
andere Agenten befolgen sie aus dem Text der `AGENTS.md`.

## Entwickeln

```bash
python3 -m unittest discover -s tests
```

Lokal ausprobieren ohne Installation: `claude --plugin-dir plugins/ce-shared`. Version in `plugin.json` und
`marketplace.json` gemeinsam anheben (der Test prüft es), Eintrag in `CHANGELOG.md`.

## Zuständigkeit

Änderungen an den gemeinsamen Werkzeugen und Regeln (dieses Plugin; die globalen Regeln im Repo `~/Claude/cubeescape-nexogen`,
auf das `~/.claude/CLAUDE.md` und `~/.claude/templates/` verlinken) macht
die Übersichts-Sitzung des Nutzers oder eine Sitzung mit ausdrücklichem Auftrag dafür; Repo-Sitzungen melden
Änderungswünsche, statt selbst zu ändern. Nach jeder Änderung im Plugin: `python3 plugins/ce-shared/tools/ce_sync.py --all`
und `--check`, damit die eingebetteten Kopien der Repos nachziehen.

## Fortsetzung „Crosscurrent“ (21.10.2026)

Der gemeinsame CLI-Einstieg zeigt acht Gruppen: `check`, `session`, `codex`, `evidence`, `search`, `lock`, `docs`, `report`. Alte direkte Unterbefehle funktionieren weiter. Beispiel: `./ce search query "rig"`, `./ce evidence diff A.png B.png`, `./ce report --since 7d`. Der Bericht unterstützt Claude-Transcripts; er beweist aus gleichen Befehlen keine identischen Eingaben.

`ce_nightly --report-issues` meldet rote Läufe als `eingang`-Issue (keine Duplikate bei offenem Issue). Der Lauf braucht saubere main-Checkouts. Der Trockenlauf führt keine Tests aus, ändert keinen Index und sendet keine Meldungen. Installieren erst aus dauerhaftem Stand und einen vorhandenen Scheduler vorher abgleichen. `numpy`, `Pillow`, ImageMagick und FFmpeg werden nur für Bild-/Medienprüfungen gebraucht, nicht vom Lock.

Sperrvertrag im gemischten Rollout: alte Gesamtsperre exklusiv gegen alle benannten Sperren; Unity/Blender untereinander teilbar, `gpu` exklusiv gegen beide. Ein alter Gesamtschritt darf nicht innerhalb einer benannten Sperre hochstufen: außen die Gesamtsperre oder `gpu` nehmen. Das bewahrt gemischte Robogen-/Cubegen-Abnahmen. Parallelleistung ist damit möglich, aber kein gemessener Geschwindigkeitsnachweis.

`docs.agent_sections` benennt bei Bedarf die bestehende Repo-Gliederung statt der Standardüberschriften; Formgens getrennte Agentenregeln behalten ihre Bullet-Struktur (begründetes leeres Array). Boolesche Richtlinien wie `limits.no_media` sind keine Zeilengrenzen.

Koordination und Repo-Verzeichnis: [Nexogen](https://github.com/ffunkfabian-hash/cubeescape-nexogen). Die gemeinsamen Werkzeuge lesen dessen `repos.json`, soweit lokal vorhanden; ältere Installationen behalten ihre Ausweichpfade. `ce-agent-tools` bleibt die Quelle für ausführbare Werkzeuge, Nexogen für Regeln und Verweise.

## Erfahrungen

- Schemaerfolg belegt keine Bild-/Rig-Abnahme; Originalverträge wiederverwenden und die tatsächlich geprüfte Tiefe nennen ([Pactguard](docs/versions/0.6.md)).
- Nichtendliche JSON-Zahlen und doppelte Schlüssel vor dem Schema ablehnen; die Freigabe eines Richters kommt ausschließlich aus seiner Modellantwort auf stdout ([Pactguard](docs/versions/0.6.md)).
- Alte Gesamtsperren und neue Ressourcen-Sperren teilen eine Barriere; die Umstellung darf laufende ältere Clients nicht überholen.
- Eine geschriebene launchd-Datei ist noch keine Aktivierung: `bootstrap` und anschließendes `print` prüfen.
- GitHub-Umbenennungen erzeugen Weiterleitungen, keine zweiten Repos; IDs und lokale Arbeitsstände getrennt prüfen.
