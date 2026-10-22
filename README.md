# ce-agent-tools

Gemeinsame Agenten-Werkzeuge für **Cube Escape**, **Cubegen** und den **Roboter-Generator** – ein Claude-Code-Marketplace
mit einem Plugin, `ce-shared`. Was alle drei Repos gleich brauchen, steht hier einmal statt dreimal; Repo-eigene
Regeln bleiben als hookify-Dateien in `.claude/` des jeweiligen Repos.

## Inhalt

| Teil | Wirkung |
|---|---|
| `plugins/ce-shared/rules/*.md` | gemeinsame Regeln im hookify-Format (siehe unten), ausgewertet bei jedem Bash-/Datei-Werkzeug |
| `plugins/ce-shared/hooks/guard.py` | der eine PreToolUse-Hook: gemeinsame Regeln + Werkzeug-Profile der Subagents |
| `plugins/ce-shared/hooks/codex_guard.py`, `antigravity_guard.py` | derselbe Wächter als Codex-/Antigravity-User-Hook (dünne Adapter über `client_adapter.py`, siehe „Andere Clients“) |
| `plugins/ce-shared/githooks/ce_githooks.py` | Git-Netz pre-commit/pre-push je Repo, umkehrbar (siehe „Git-Netz“) |
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
| `commit-on-main` | blockiert | `git commit`, der tatsächlich auf `main`/`master` landet (Feld `git_commit`, unten), und `--allow-empty-message` |
| `heavy-resource` | blockiert | `heavy_lock.py run` ohne `--resource` |
| `heavy-unlocked`, `output-cap`, `judge-template` | warnt | schwerer Lauf ohne sichtbare Sperre; ganze JSON-/Log-Datei ausgeben; `codex exec` ohne Checkliste |
| `actions-sparsam` | warnt | Bearbeiten eines GitHub-Workflows: globale Regel „GitHub Actions sparsam“ |

Format wie hookify (`event: bash|file`, `conditions` mit `field` command/file_path/content/new_text/old_text, Operatoren
`regex_match` (ohne Groß/klein), `contains`, `not_contains`, `equals`, `starts_with`, `ends_with`; `action: warn|block`).
Zusätzlich `git_commit` (nur Bash): je Branch, auf dem ein `git commit` der Zeile landen kann, eine Zeile
`<Branch> <Befehl>` (`-` ohne Branch). Die Zeile wird wie von der Shell zerlegt (Quotes, Kommentare, Heredocs,
Substitutionen auch in `$((…))`, Listen, `&&`/`||`, Pipelines, Subshells, Gruppen, if/while/for/case, Funktionen) und
über alle möglichen Abläufe verfolgt: Ein Pfad, der nicht laufen muss, oder ein Branchwechsel, der scheitern kann
(`-b`/`-c` auf vorhandenen Branch, unbekanntes Ziel), schließt main nie aus. Der Branch folgt dem Sitzungsordner (`cwd`
des Hook-Eingangs), `cd`/`pushd`, `git -C` (auch `-C<pfad>`), `--git-dir`/`--work-tree`, `GIT_DIR` (Präfix, `export`,
`unset`, `env -u/-i/-C/-S`) und `checkout`/`switch` (Datei-Checkout ändert ihn nicht, `checkout X --`/`switch X --`
schon). Ordner und Variablen bleiben in Subshells, Pipeline-Elementen, Hintergrundjobs, Substitutionen und `sh -c`;
Branchwechsel dort gelten danach weiter (HEAD liegt im Repo). Funktionen wirken erst beim Aufruf; Shellskripte auf
der Standardeingabe (`bash <<EOF`, `bash -`, `<<<`) werden gelesen. Branches liest das System-Git (fester Pfad,
`/usr/bin/git` zuerst) mit kleiner fester Umgebung – nie ein `git`, `PATH` oder Loader aus der geprüften Zeile.
Skriptdateien, Eingabe aus Dateien und erst zur Laufzeit entstehende Werte bleiben offen; dafür gibt es das Git-Netz.
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
| `ce_report.py [transcript]` | misst eine Sitzung aus dem Transcript (Modelle, Prüfläufe, Suchen, Ausgaben, Wartezeit); Claude-Usage zusätzlich dedupliziert als frischen Input, Cache-Schreiben/-Lesen, Output und absolute Inputmenge mit Messabdeckung; der Stop-Hook hängt die Zeile an `report.md` |
| `ce_api.py login\|status\|budget\|ask` / `./ce api …` | explizite Claude-Textanfrage mit isoliertem Browser-Login, lokalem Monats-/Anfragelimit, Vorabzählung und 5-Minuten-Präfixcache; Richtlinie in Nexogens `.ce-api.json`, keine Zugangsdaten im Repo ([Ablauf](https://github.com/ffunkfabian-hash/cubeescape-nexogen#lokale-claude-api)) |
| `ce_imagediff.py A B [--out D] [--kind ui\|world\|bake]` | SSIM/PSNR/Pixelanteil/Bereich und Diffbild – Zahlen für den Richter statt zwei Bilder |
| `ce_media.py sheet\|clip\|still` | Kontaktbogen, MP4 (480p, ≤ 8 s), Standbild (≤ 1280 px) – die Belege der Regel › Belege |
| `ce_judge.py [--model luna\|sol] [--opus]` | Codex-Richter mit der Checkliste `pruefung.md`; Modell nach Diffgröße und Kern-/Vertrags-/Infrastrukturrisiko, Grund im CLI; warnt ab 1.500 Diff-Zeilen / 100 KB zur aufgeteilten Prüfung mit `--files` (ohne pauschales Kürzen von JSON); `--opus` erzeugt den Prompt für einen Opus-Unteragenten |
| `ce_search.py index\|query\|serve\|status` | lokale Suche (FTS5, optional Embeddings) über Docs/Lehren/Skills/Berichte/Code-Kommentare aller Repos; MCP-Server `ce-search` |
| `ce_contract.py validate <json…> \| list` | Verträge zwischen Repos gegen `contracts/<name>.v<format>.json` prüfen (Generator beim Export, Abnehmer beim Import) |
| `ce_interfaces.py` / `./ce contract audit` | Lesender Abgleich über native Formgen-/Cubegen-/Motiongen-Prüfungen, optional Toolgen-Provenance und Robogen-Abnehmerprofil; explizite Repo-Pfade und Cubegen-Vertragsversion. Fehlende Toolgen-Konzeptprüfer melden eine nicht verfügbare Prüfung und lassen den Audit fehlschlagen. |
| `ce_docs.py [--json R]` | Markdown-Links/-Anker, SKILL-Front-Matter, JSON, Shell-Syntax nach `.ce-repo.json › docs` – der eine Doku-Prüfer |
| `ce_nightly.py [--all\|--repo P] [install\|status]` | ein Nachtlauf für alle Repos (`check.nightly` je Repo) inkl. Suchindex über alle acht Standard-Repos, Bericht, LaunchAgent |
| `ce_session_name.py [--name N] [--reserve] [--topics]` | Sitzungsname und Branch; `--reserve` pusht den Branch (= Reservierung) |
| `ce_await_mail.py --name N [--issue …]` | wartet auf eine Nachricht an diese Sitzung im Postfach-Issue (cubeescape-nexogen#16) |
| `ce_codex_quota.py`, `ce_codex_cloud.py` | Codex-Kontingent lesen, Codex-Cloud-Aufträge |
| `heavy_lock.py run --resource unity\|blender\|gpu [--what X] [--checkout P] -- <Befehl>` / `status` | eine Sperre **je Ressource** für schwere Läufe (`~/Library/Caches/CubeEscape/heavy-<resource>.lock`; ohne `--resource` die alte gemeinsame Datei `heavy-run.lock`), hält bis zum letzten Enkelprozess; Unity-Regression und Blender-Bake laufen nebeneinander, zwei Läufe derselben Ressource nacheinander. Aufnahmen, Screenshots, Kontaktbögen, gezielte Tests und `quick`-Stufen sind nicht schwer und nehmen keine Sperre. Als Modul: `held(..., resource=)`, `HeavyLock`, `holder_pids()`, `Groups`, `run_group`/`run_forwarding` |
| `ce_check.py quick\|full\|nightly [--verbose] [--no-targeted]` | Befehle aus `.ce-repo.json › check` (löst nacktes `python3`/`python` bevorzugt auf `.venv/bin/python` bzw. `~/.local/share/ce-agent-tools/venv/bin/python` auf); Ergebnis zuerst; `full` wiederholt nach Fehlern einmal `check.rerun_failed`; `nightly` = gebündelte Bildstufen einmal täglich auf `main` (0.5.0) |
| `ce_lint.py [--strict]` | Längen (`limits`), AGENTS.md-Abschnitte, keine Notiz-Dateien im Hauptordner, Versionsbericht/CHANGELOG |
| `ce_release.py [--execute] [--no-push] [--remote R]` | Trockenlauf Standard: prüft Version, CHANGELOG, sauberen Baum, Tag `<tag_prefix><Version>`, Branch `<branch_prefix><Version>`, Remote; mit `--execute` Tag, Branch, Push (nie `--force`) |
| `ce_sync.py [--repo P\|--all] [--check]` | schreibt die Werkzeuge als eingebettete Kopie nach `<repo>/<tools_dir>` (Standard `scripts/ce_tools`, Kopfzeile + Prüfsumme); respektiert `hold` in `repos.json` (Schreibsperre ohne `--check`); `--check` meldet abweichende/editierte Kopien (auch in `ce_lint.py`). Die Repos rufen nur diese Kopien, nie das Plugin |
| `ce_worktrees.py [--repos P…] [--active-minutes N] [--list\|--json]` | Tabelle (Standard) der Worktrees aller acht Repos (`repos.json`, `CE_REPOS` überschreibt): Branch, sauber, ahead/behind, Sitzung (`~/.claude/projects`, `~/.codex/sessions`, laufende Prozesse), „NICHT LÖSCHEN“ |

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
Antigravity bekommen denselben Wächter als User-Hook (unten), sonst befolgen sie die Regeln aus dem Text der `AGENTS.md`.

### Andere Clients (Codex, Antigravity)

Die Adapter übersetzen nur Ein- und Ausgabe; entschieden wird in `guard.decide` mit denselben `rules/*.md` (Gleichstand
prüft `tests/test_client_hooks.py`). Eingetragen werden sie von Nexogens `./install.sh --client-hooks` (`--dry-run`,
Sicherungen, `--remove-hooks`). Der eingetragene Befehl reicht nur eine Adapterantwort mit Exit 0 durch (auch eine Sperre);
fehlt das Skript später oder startet es nicht (Syntax-/Importfehler), antwortet er mit Meldung ohne Block.

| Client | Eintrag | Eingang | Ausgabe |
|---|---|---|---|
| Codex (CLI 0.162.1 gemessen; App ungemessen) | `~/.codex/hooks.json`, `PreToolUse`, Matcher `^(Bash\|apply_patch)$`; läuft erst nach Vertrauen unter `/hooks` | `tool_input.command` (Shellzeile bzw. Patchtext; je Datei des Patches ein Datei-Ereignis); MCP-/Funktionswerkzeuge nicht | Block `hookSpecificOutput.permissionDecision: "deny"`, Warnung `additionalContext` + `systemMessage`, sonst keine Ausgabe |
| Antigravity (IDE 2.5.5/CLI 1.4.5 gemessen; App ungemessen) | `~/.gemini/config/hooks.json`, Name `ce-shared-guard`, Matcher `run_command\|write_to_file\|replace_file_content\|multi_replace_file_content` | `toolCall.args.CommandLine` bzw. `TargetFile` (+ Inhalt), Ordner aus `Cwd`/`workspacePaths` | `{"decision": "deny", "reason"}`; sonst `allow` (laut Doku: an Folgehook/normale Freigabe weiterreichen), Warnung als `reason` |

Claude-Subagent-Profile (`.claude/agent-guard.json`) gelten nur in Claude Code. `CE_HOOK_LOG=<Datei>` in der Umgebung des
Clients schreibt jede Ein-/Ausgabe als JSON-Zeile – das ist die Messung eines echten Clientlaufs; Adaptertests mit
nachgebauter Eingabe sind keine. Was in welchem Client tatsächlich feuert, steht im [Versionsbericht 0.12](docs/versions/0.12.md). Antigravity hat in den
gemessenen CLI-/IDE-Proben `allow.reason` bei Warnungen und Fehlern nicht sichtbar an den Agenten weitergegeben;
Sperren über `deny` greifen. Eine sichtbare Warnung ist dort derzeit nicht belegt.

### Git-Netz

`python3 plugins/ce-shared/githooks/ce_githooks.py install|status|uninstall [--repo P …] [--dry-run]` (Nexogen:
`./install.sh --git-hooks` für alle lokalen Repos aus `repos.json` außer `hold`). Ein Ordner `<git-common-dir>/ce-hooks`
wird lokaler `core.hooksPath` (gilt für alle Worktrees); der vorherige Hook-Ordner bleibt unberührt und seine Hooks laufen
weiter (pre-commit/pre-push nach der Prüfung mit denselben Argumenten und Eingaben, alle anderen beim Installieren
vorhandenen unverändert). `uninstall` stellt den vorherigen lokalen Wert wieder her.

| Hook | sperrt | Ausnahme |
|---|---|---|
| pre-commit | Commit, wenn HEAD auf `main`/`master` steht (losgelöster HEAD ist kein Branch) | `CE_ALLOW_MAIN=1` |
| pre-push | Push nach `main`/`master` (auch Neuanlage, auch aus losgelöstem HEAD) | `CE_ALLOW_MAIN=1` |
| pre-push | Löschen von `main`/`master`; Überschreiben eines vorhandenen Tags; jedes Update, dessen Remote-Commit nicht im gepushten Stand enthalten ist (fehlt er lokal, ist er es nicht: erst `git fetch`) | keine |

Grenzen des Git-Protokolls: pre-push sieht Refs und Commits, keine Optionen – ein erzwungener Push, der faktisch ein
Fast-Forward ist, sieht aus wie ein normaler und geht durch (die Client-Wächter prüfen die Optionen weiter);
`--no-verify` überspringt beide Hooks; Merge-Commits laufen über pre-merge-commit (nicht abgedeckt). Fehler in der
Prüfung lassen Git weiterlaufen; fehlt das Skript oder startet es nicht (z. B. Syntax-/Importfehler), melden die Wrapper
dies und reichen an die vorherigen Hooks weiter. Deren Exitcode bleibt erhalten. Nur der Exitcode `REJECT` (86) von
`ce_githooks.py check` ist eine bewusste Ablehnung; dann laufen die vorherigen Hooks nicht.

## Entwickeln

```bash
python3 plugins/ce-shared/tools/ce_check.py quick   # nutzt automatisch .venv oder ~/.local/share/ce-agent-tools/venv
python3 -m unittest discover -s tests
```

Optionale Test- und Bildabhängigkeiten (`Pillow`, `numpy`) stehen in `requirements-dev.txt` (`python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`). Lokal ausprobieren ohne Installation: `claude --plugin-dir plugins/ce-shared`. Version in `plugin.json` und
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

- **Ein Wächter darf nichts aus der geprüften Zeile ausführen:** Der Branchleser startete `git` mit der Umgebung des Befehls; `PATH=/fremd git commit` hätte schon bei der Prüfung ein fremdes `git` gestartet. Leseabfragen nur mit festem Git-Pfad und eigener kleiner Umgebung ([0.12](docs/versions/0.12.md)).
- **Ein fehlendes Hook-Skript sperrt Codex komplett:** `python3 fehlt.py` endet mit Exit 2, und Exit 2 heißt in Codex „Werkzeugaufruf sperren“. Ein Syntax-/Importfehler endet mit Exit 1 ohne Antwort. Client-Hooks deshalb so eintragen, dass nur eine Antwort mit Exit 0 durchkommt, sonst eine Meldung ohne Block ([0.12](docs/versions/0.12.md)).
- **Antigravitys Web-Doku und installierter Stand können abweichen:** Die im `language_server` eingebettete Hook-Doku nennt eine andere `allow`-Bedeutung und Werkzeugnamen aus Step-Typen; Adapter nach Argumentform statt Namen, Verhalten am echten Client messen. Gemessen: der Hook läuft im Ordner der `hooks.json`, und das CLI sendet kein `Cwd` – den Repo-Ordner aus `workspacePaths` nehmen ([0.12](docs/versions/0.12.md)).
- **FTS5-External-Content löscht mit alten Werten:** Bei inkrementellem Ersetzen/Löschen die bisherigen Spaltenwerte an das FTS-`delete` übergeben; leere Strings hinterlassen alte Postings. Konsistenz zusätzlich gegen die Inhaltstabelle prüfen ([0.10](docs/versions/0.10.md)).

- Schemaerfolg belegt keine Bild-/Rig-Abnahme; Originalverträge wiederverwenden und die tatsächlich geprüfte Tiefe nennen ([Pactguard](docs/versions/0.6.md)).
- Nichtendliche JSON-Zahlen und doppelte Schlüssel vor dem Schema ablehnen; die Freigabe eines Richters kommt ausschließlich aus seiner Modellantwort auf stdout ([Pactguard](docs/versions/0.6.md)).
- Alte Gesamtsperren und neue Ressourcen-Sperren teilen eine Barriere; die Umstellung darf laufende ältere Clients nicht überholen.
- Eine geschriebene launchd-Datei ist noch keine Aktivierung: `bootstrap` und anschließendes `print` prüfen.
- GitHub-Umbenennungen erzeugen Weiterleitungen, keine zweiten Repos; IDs und lokale Arbeitsstände getrennt prüfen.
