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
| `plugins/ce-shared/hooks/postfach_watch.py` | zwei Stop-Hooks (0.15): `watch` (async + asyncRewake) startet für eine reservierte Sitzung den Postfach-Wächter und weckt sie bei einer Nachricht; `remind` hält als Rückfall das Anhalten einmal an (höchstens alle 2 h, nie ohne `gh`) |
| `plugins/ce-shared/hooks/postfach_inbox.py` | PostToolUse-Hook (0.21): Post an eine reservierte Sitzung kommt mitten in der Arbeit als `additionalContext` (Codex über `codex_guard.py`). Bei leerem Postfach nur zwei Dateilesungen, kein Netz, kein Kontext; höchstens alle 3 min (`CE_INBOX_EVERY`) ein abgelöster Lesezugriff, dessen Ergebnis der nächste Werkzeugaufruf zustellt; zählt als `pending` wie ein Wecken des Stop-Wächters, der das Zugestellte überspringt. `CE_INBOX=0` schaltet ab, Fehler geben nichts aus |
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
| `commit-on-main` | blockiert | ein einfacher `git commit`, dessen Repo auf `main`/`master` steht (Feld `git_commit`, unten), und `--allow-empty-message`; zusammengesetzte Zeilen nur mit Hinweis, dort entscheidet das Git-Netz |
| `heavy-resource` | blockiert | `heavy_lock.py run` ohne `--resource` |
| `heavy-unlocked`, `output-cap`, `judge-template` | warnt | schwerer Lauf ohne sichtbare Sperre; ganze JSON-/Log-Datei ausgeben; `codex exec` ohne Checkliste |
| `merge-nur-sightline` | blockiert | 0.22: `gh pr merge` (auch `--auto`), `gh api … PUT …/pulls/N/merge`, GraphQL `mergePullRequest`/`enablePullRequestAutoMerge` (auch nach `NAME=wert`, `env`, `bash -c`, `eval`) und die GitHub-MCP-Werkzeuge `merge_pull_request`/`enable_pr_auto_merge` – außer in der Sitzung Sightline (reservierter Name `Sightline` in `<git-dir>/ce-session.json` oder `CE_ROLE=sightline`). `git merge origin/main` (main einholen), `gh pr view/create` und `gh pr merge --help` bleiben frei. Feld `merge_attempt`, Ereignis `all`, Logik nur in `guard.py`; ein Lesefehler lässt durch |
| `actions-sparsam` | warnt | Bearbeiten eines GitHub-Workflows: globale Regel „GitHub Actions sparsam“ |

Format wie hookify (`event: bash|file|mcp|all`, `conditions` mit `field` command/file_path/content/new_text/old_text, Operatoren
`regex_match` (ohne Groß/klein), `contains`, `not_contains`, `equals`, `starts_with`, `ends_with`; `action: warn|block`).
Zusätzlich `merge_attempt` (Bash und `mcp__…`-Werkzeuge, siehe `merge-nur-sightline`) und `git_commit` (nur Bash), die **begrenzte Vorabprüfung** von `commit-on-main`: Beurteilt wird nur eine Zeile,
die als Ganzes ein einziger einfacher Befehl `git [Optionen] commit …` aus festen Wörtern ist. Quotes, Backslashes,
Zeilenfortsetzung und Kommentare werden wie in der Shell gelesen, auch innerhalb eines Worts (`'gi't commit`,
`git 'c'ommit`, `g\it commit` zählen wie `git commit`); kein `$`, kein Backtick, keine Muster-/Klammererweiterung und
keine Präfix-Zuweisung `NAME=wert` (auch nicht `GIT_DIR=…`, `PATH=…`, `CE_ALLOW_MAIN=1`). Der Branch kommt aus dem Ordner
des Hook-Eingangs (`cwd`), `git -C`, `--git-dir`/`--work-tree`/`--bare` und den vom Client geerbten
`GIT_DIR`/`GIT_WORK_TREE`/`GIT_COMMON_DIR`; gelesen vom System-Git (fester Pfad, `/usr/bin/git` zuerst) mit kleiner
fester Umgebung – nie ein `git`, `PATH` oder Loader aus der geprüften Zeile oder der geerbten Umgebung. Auf
`main`/`master` sperrt die Regel (ohne Ausnahme, `CE_ALLOW_MAIN` gilt nur im Git-Netz). Losgelöster HEAD, kein Repository oder fehlender Ordner sperren
nicht. Ist der Branch nicht lesbar (kein System-Git, Zeitlimit, unerwarteter Git-Fehler) oder scheitert die Prüfung
intern, läuft der Befehl mit Hinweis weiter; die übrigen Regeln werden trotzdem geprüft.

**Nicht nachgebildet** wird die Shell: Präfix-Zuweisungen, Operatoren (`&&`, `;`, `|`, `&`, Zeilenumbruch), Subshells, Gruppen, Variablen,
Befehlsersetzungen (auch Claudes übliches `git commit -m "$(cat <<'EOF' …)"`), Heredocs, Umleitungen, Funktionen,
Wrapper (`env`, `command`, `timeout` …), eingebettete Shells (`bash -c`) und nicht ausgewertete Git-Optionen. Erwähnt
eine solche Zeile `git … commit`, geht sie mit dem Hinweis „Vorabprüfung: möglicher `git commit` … das Git-Netz
(pre-commit) prüft den tatsächlichen Branch beim Ausführen“ durch; der Hinweis kann auch bei harmlosen Zeilen erscheinen,
die `git … commit` nur erwähnen; Zeilen ohne erkennbaren Commit bleiben still. Diese Fälle schützt nur das
[Git-Netz](#git-netz) – in Repos ohne installiertes Netz gibt es für sie keine Sperre. Git-Aliase (`git ci`) erkennt die
Vorabprüfung nicht; für den Hinweis bei zusammengesetzten Zeilen dient nur eine Texterkennung von `git … commit`. `--allow-empty-message` (auch als eindeutige Abkürzung) wird in jedem festen `git … commit`-Abschnitt der Zeile
gesperrt, auch in zusammengesetzten Zeilen; als Wert von `-m`/`-F` oder nach `--` nicht.
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
`./ce`: ab 0.20 der Lader (unten, „Repos ohne Kopie“), in noch nicht umgestellten Repos die eingebettete Kopie.

| Aufruf | Wirkung |
|---|---|
| `ce` | ein Einstieg für alles unten (`./ce check`, `./ce reserve <Name>`, `./ce search <q>`, `./ce judge`, `./ce diff A B`, `./ce clip <frames> --out x.mp4`, `./ce lock unity -- <cmd>`) |
| `ce_report.py [transcript]` | misst eine Sitzung aus dem Transcript (Modelle, Prüfläufe, Suchen, Ausgaben, Wartezeit); Claude-Usage zusätzlich dedupliziert als frischen Input, Cache-Schreiben/-Lesen, Output und absolute Inputmenge mit Messabdeckung; der Stop-Hook hängt die Zeile an `report.md` |
| `ce_api.py login\|status\|budget\|ask [--image PNG/JPEG …]` / `./ce api …` | explizite Claude-Anfrage (Text, ab 0.25 optional Bilder, nur mit `max_images` in der Richtlinie) mit isoliertem Browser-Login, lokalem Monats-/Anfragelimit, Vorabzählung und 5-Minuten-Präfixcache; Richtlinie in Nexogens `.ce-api.json`, keine Zugangsdaten im Repo ([Ablauf](https://github.com/ffunkfabian-hash/cubeescape-nexogen#lokale-claude-api)) |
| `ce_imagediff.py A B [--out D] [--kind ui\|world\|bake]` | SSIM/PSNR/Pixelanteil/Bereich und Diffbild – Zahlen für den Richter statt zwei Bilder |
| `ce_media.py sheet\|clip\|still` | Kontaktbogen, MP4 (480p, ≤ 8 s), Standbild (≤ 1280 px) – die Belege der Regel › Belege |
| `ce_judge.py [--model luna\|sol] [--opus]` | Codex-Richter mit der Checkliste `pruefung.md`; Modell nach Diffgröße und Kern-/Vertrags-/Infrastrukturrisiko, Grund im CLI; warnt ab 1.500 Diff-Zeilen / 100 KB zur aufgeteilten Prüfung mit `--files` (ohne pauschales Kürzen von JSON); `--opus` erzeugt den Prompt für einen Opus-Unteragenten |
| `ce_search.py index\|query\|serve\|status` | lokale Suche (FTS5, optional Embeddings) über Docs/Lehren/Skills/Berichte/Code-Kommentare aller Repos; MCP-Server `ce-search` |
| `ce_contract.py validate <json…> \| list` | Verträge zwischen Repos gegen `contracts/<name>.v<format>.json` prüfen (Generator beim Export, Abnehmer beim Import) |
| `ce_interfaces.py` / `./ce contract audit` | Lesender Abgleich über native Formgen-/Cubegen-/Motiongen-Prüfungen, optional Toolgen-Provenance und Robogen-Abnehmerprofil; explizite Repo-Pfade und Cubegen-Vertragsversion. Fehlende Toolgen-Konzeptprüfer melden eine nicht verfügbare Prüfung und lassen den Audit fehlschlagen. |
| `ce_docs.py [--json R]` | Markdown-Links/-Anker, SKILL-Front-Matter, JSON, Shell-Syntax nach `.ce-repo.json › docs` – der eine Doku-Prüfer |
| `ce_nightly.py [--all\|--repo P] [install\|status]` | ein Nachtlauf für alle Repos (`check.nightly` je Repo) inkl. Suchindex über alle acht Standard-Repos, Bericht, LaunchAgent |
| `ce_session_name.py [--name N] [--reserve] [--patch K] [--about A] [--model M] [--no-announce] [--topics]` | Sitzungsname und Branch; `--reserve` pusht den Branch (= Reservierung) und meldet die Sitzung im Postfach an (`<Name> → Sightline: angemeldet`, 0.14); `--patch K` reserviert `…/<repo>-patch-K` ebenso. Eine fehlgeschlagene Anmeldung lässt die Reservierung stehen und gibt den Text zum Einfügen aus. 0.21: holt vorher `origin/<Standardbranch>`; liegt der Worktree nur dahinter, steht der Branch auf dem frischen Stand (eigene Commits bleiben und werden genannt); merkt `--model`/`--about` für spätere Nachrichten; ein neuer Name im wiederverwendeten Worktree verwirft den Wächterstand des Vorgängers |
| `ce_postfach.py say [--to E] [--model M] <Thema> [Zeilen…]` / `who [--days N]` (`./ce session say\|who`) | Nachricht ins Postfach (cubeescape-nexogen#16) im Regelformat, Absender aus dem Branch, Empfänger eine Sitzung, Rolle oder `alle` (höchstens zehn Zeilen, `--dry-run`); `who` zeigt die Anmeldungen der letzten Tage mit der jüngsten Meldung je Sitzung. Sendet nur aus Repos, deren `origin` eine GitHub-URL ist (`CE_MAILBOX=0` schaltet ab); Absender = beim Reservieren gemerkter Name (`<git-dir>/ce-session.json`), sonst aus dem Branch. 0.21: Modellzeile aus der Reservierung; mehrere Empfänger (`--to A --to B` oder `A, B`); PR-/Merge-Meldungen gehen zusätzlich an Sightline (`--no-cc`); `--freigabe commit,push,pr --zitat … [--zeit …]` schreibt Sightlines Freigabe per Post (nur Absender Sightline, siehe „Freigabe per Post“); Postfach nicht lesbar → Meldung mit Abhilfe (Cloud: cubeescape-nexogen mit auswählen) |
| `ce_plugins.py check\|fix [--apply] [--user-only]` (`./ce plugins …`) | Claude-Code-Installationen der ce-agent-tools-Plugins auf einer Version halten (0.16): listet die Einträge in `~/.claude/plugins/installed_plugins.json`, `fix` setzt veraltete Einträge (jeder Bereich) auf die neueste Installation mit vorhandenen Dateien und entfernt Einträge verschwundener Worktrees (Probelauf, mit `--apply` Sicherung und Schreiben; `--user-only` lässt nur die Nutzer-Installation). Lädt nichts herunter; liegt der Marketplace weiter, nennt es die Update-Befehle. Der SessionStart-Hook warnt sichtbar, wenn eine Sitzung ein älteres ce-shared geladen hat |
| `ce_await_mail.py [--name N] [--issue …]` | wartet auf eine Nachricht an diese Sitzung im Postfach-Issue (cubeescape-nexogen#16); `→ alle:` weckt jede Sitzung außer der absendenden (0.14). Ohne `--name` gilt die Reservierung des Worktrees; Pid und behandelte Nachricht liegen in `<git-dir>/ce-watch.*`, geteilt mit dem Stop-Hook `hooks/postfach_watch.py`, der den Wächter ab 0.15 von selbst startet (Rückfall: einmalige Erinnerung); Exit 4 = Wächter läuft schon, Exit 5 = kein Name und keine Reservierung. 0.21: `--follow` läuft weiter, gibt jede Nachricht aus und merkt die letzte ID (eigener Worktree: `ce-watch.json`, sonst `--state` bzw. `~/.local/state/ce-agent-tools/`), nach einem Neustart geht es dort weiter; `--also-pr` weckt zusätzlich für jede PR-/Merge-Meldung anderer Sitzungen (Koordinator); vor einer Freigabe-Nachricht steht ihr Prüfergebnis |
| `ce_codex_quota.py`, `ce_codex_cloud.py` | Codex-Kontingent lesen, Codex-Cloud-Aufträge |
| `heavy_lock.py run --resource unity\|blender\|gpu [--what X] [--checkout P] -- <Befehl>` / `status` | eine Sperre **je Ressource** für schwere Läufe (`~/Library/Caches/CubeEscape/heavy-<resource>.lock`; ohne `--resource` die alte gemeinsame Datei `heavy-run.lock`), hält bis zum letzten Enkelprozess; Unity-Regression und Blender-Bake laufen nebeneinander, zwei Läufe derselben Ressource nacheinander. Aufnahmen, Screenshots, Kontaktbögen, gezielte Tests und `quick`-Stufen sind nicht schwer und nehmen keine Sperre. Als Modul: `held(..., resource=)`, `HeavyLock`, `holder_pids()`, `Groups`, `run_group`/`run_forwarding`. `status` nennt je gehaltener Sperre Sitzung (Reservierung des Checkouts, sonst Branch), Modell, Client, Laufzeit, Befehl und Kindprozesse, darunter die Wartenden; `shared` ist die alte Gesamtsperre, die ein `gpu`-Lauf als Barriere exklusiv hält (0.24) |
| `ce_check.py quick\|full\|nightly [--verbose] [--no-targeted]` | Befehle aus `.ce-repo.json › check` (löst nacktes `python3`/`python` bevorzugt auf `.venv/bin/python` bzw. `~/.local/share/ce-agent-tools/venv/bin/python` auf); Ergebnis zuerst; `full` wiederholt nach Fehlern einmal `check.rerun_failed`; `nightly` = gebündelte Bildstufen einmal täglich auf `main` (0.5.0) |
| `ce_lint.py [--strict]` | Längen (`limits`), AGENTS.md-Abschnitte, keine Notiz-Dateien im Hauptordner, Versionsbericht/CHANGELOG |
| `ce_release.py [--execute] [--no-push] [--remote R]` | Trockenlauf Standard: prüft Version, CHANGELOG, sauberen Baum, Tag `<tag_prefix><Version>`, Branch `<branch_prefix><Version>`, Remote; mit `--execute` Tag, Branch, Push (nie `--force`) |
| `ce_sync.py [--repo P\|--all] [--check]` | schreibt die Werkzeuge als eingebettete Kopie nach `<repo>/<tools_dir>` (Standard `scripts/ce_tools`, Kopfzeile + Prüfsumme); respektiert `hold` in `repos.json` (Schreibsperre ohne `--check`); `--check` meldet abweichende/editierte Kopien (auch in `ce_lint.py`). Repos mit `tools_version` (0.20) bekommen nur den Lader als `./ce`; `--check` meldet dort fehlenden/editierten Lader und ungültige `tools_version` als Fehler, älteren Lader, andere Plugin-Version und übrig gebliebene Kopie als Warnung. Löscht nie |
| `ce_loader.py` = `./ce` eines Repos ohne Kopie (0.20) | lädt genau `.ce-repo.json › tools_version` (`{"version", "commit"}`) und führt `ce <Befehl>` daraus im Repo-Ordner aus: `CE_TOOLS` → installiertes Plugin mit diesem Commit → Cache `~/.cache/ce-agent-tools/<version>-<commit12>` (Manifest geprüft) → Download aus dem öffentlichen Repo (Tarball mit Commit im Pax-Kopf, sonst `git fetch` des Commits). Nur Standardbibliothek, Meldungen auf stderr; ohne Werkzeuge und Netz Exit 3 mit Abhilfe, ungültige Angabe Exit 2. `./ce tools where\|fetch\|pin <commit>` |
| `ce_worktrees.py [--repos P…] [--active-minutes N] [--list\|--json]` | Tabelle (Standard) der Worktrees aller acht Repos (`repos.json`, `CE_REPOS` überschreibt): Branch, sauber, ahead/behind, Sitzung (`~/.claude/projects`, `~/.codex/sessions`, laufende Prozesse), „NICHT LÖSCHEN“ |
| `ce_tidy.py [--apply\|--nightly] [--json] [--category worktree\|befehl\|ausgabe\|beleg]` (`./ce tidy`) | Reste fertiger Sitzungen in allen Repos (Nexogen › Aufräumen nach dem Merge, 0.18): Probelauf-Tabelle Kategorie · Pfad · Größe · Grund · löschbar. Worktrees aus `ce_worktrees.collect` nur gemergt, sauber, ohne Sitzung (24 h) oder Prozess im Ordner und unter einer Worktree-Wurzel (`.claude/worktrees`, `~/.codex/worktrees`, `tidy.worktree_roots`) – Entfernen mit `git worktree remove` ohne `--force` + `prune`; repo-eigene Aufräumskripte (nur Skripte im Haupt-Checkout), Generator-Ausgaben (`keep`, `older_than_days`) und Belegablagen (`~/Library/Caches/*/videos`, aktuelle + vorige Version bleiben) aus `.ce-repo.json › tidy`. `--apply` verschiebt in den Papierkorb, nie `rm`; nie Haupt-Checkouts und deren `Library`, `~/Claude/_rettung`, „NICHT LÖSCHEN“, Repos mit `hold`. Der Nachtlauf räumt nur Einträge mit `"nightly": true` |

## Native Oberflächen (0.19 „Prismwake“)

Die [IDE-Erweiterung](integrations/vscode/README.md) öffnet Ressourcen, Sitzungen und Kontingent sowie den
Bildvergleich als lokale Webview-Panels in VS Code und kompatiblen Hosts, auch neben Claude Code im Editor.
Die [Blender-Seitenleiste](integrations/blender/README.md) startet Profilprüfung, Rig-/Asset-Preflight und
Turnaround mit Status und Abbruch. Beide Adapter nutzen die bestehenden Werkzeuge; Installation und Pfadwahl
stehen in ihren Anleitungen. CLI und Browser bleiben verfügbar.

`hold.reticle_socket` wird wie die übrigen Socket-Referenzen semantisch aufgelöst. Das Vorschau-Licht skaliert
quadratisch mit dem Bounds-Radius (1400 W bei Radius 1), damit geometrisch gleiche Modelle verschiedener Größe
vergleichbar beleuchtet sind. Keine neue Vertragsversion. [Prüfungen und Grenzen](docs/versions/0.19.md).
Die automatische Cloud-Anbindung ist bewusst eingestellt.

## Glasshouse-Erweiterungen (0.13)

Analyse vor dem Bau: Die 13 praktischen Versuche im Cube-Escape-Ökosystem bestätigen fünf vorhandene Wege
(Richter, Logiktests, Importer, Lebenszyklustests, Blender/Medien). Acht Erweiterungen schließen deren Lücken;
vier zusätzliche Implementierungen (Richter, Logikrunner, Runtime-Sentinel, Unity-MCP-Server) werden nicht eingeführt.

| Aufruf | Ergänzung |
|---|---|
| `./ce report --dashboard snapshot --out /tmp/ce.html` | Portable lesende Ansicht aus bestehenden Lock-/Worktree-/Kontingentquellen; `serve --port 8768` auf Loopback mit manuellem Aktualisieren |
| `./ce evidence diff A.png B.png --viewer /tmp/diff.html` | Eingebettete Bilder, tastaturbedienbarer Schieber, Heatmap, optional `--clip video.mp4` (bis 20 MiB) |
| `./ce evidence diff A.png B.png --reference ref.json` | Versioniertes Referenzprofil, RGB-Pixelgrenze zusätzlich zu SSIM, optional Region in Originalkoordinaten |
| `./ce check --profile logic` | Vorhandenen `.ce-repo.json`-Prüfbefehl aufrufen; kein zusätzlicher Runner und kein Unity-Aufruf durch den Dispatcher selbst |
| `./ce evidence preflight --profile rig.json --asset model.blend --out /tmp/preflight.json --blender /Pfad/zu/blender` | Explizite Bones/Parents, zugewiesene Materialien, exakte Bildnamen/Farbräume, Szeneneinheiten (.blend) und optional Objekt-Skalierung |
| `./ce evidence turnaround --example-profile` | Neutrales Profil auf stdout; das gewählte Generatorprofil ist bei jeder Aufnahme ausdrücklich erforderlich |
| `./ce evidence turnaround --input model.fbx --profile render.json --out /tmp/neuer-ordner --frames 4 --resolution 256 --blender /Pfad/zu/blender` | Bounds-Kamera, kleine Frames und Kontaktbogen via `ce_media`; MP4 nur mit ausdrücklichem `--clip --fps 12` |

Die Turnaround-Ausgabe braucht einen neuen/leeren Ordner außerhalb des Eingabeordners und außerhalb von Repos.
Preflight braucht ebenfalls externe, neue Report-/Skriptpfade; ein älterer Bericht wird niemals als aktuelles Ergebnis übernommen.
Szeneneinheiten sind nur in .blend prüfbar; FBX meldet sie ausdrücklich als nicht verfügbar und prüft die tatsächlichen
Objekt-Skalierungen weiter. Beide Blenderadapter koordinieren ihre Läufe über die bestehende Blender-Ressourcensperre.
`.blend` wird mit `--disable-autoexec` geöffnet. Generatorprofile besitzen eigene Namen, Bounds-Marge, Farbmanagement
und Auflösungs-/Frame-Limits; keine vermeintlich gemeinsame Rig-Namenskonvention. Beispiel für Preflight:

```json
{"format":1,"generator":"example","unit_scale":1,"require_unit_object_scale":true,
 "bones":[{"name":"Root","parent":null}],"materials":{"Body":"Body"},
 "images":{"albedo":{"name":"BodyColor","color_space":"sRGB","required":true}}}
```

Referenzprofil für Bildvergleiche (RGB-Grenze und SSIM müssen beide bestehen):

```json
{"version":1,"ssim":0.99,"max_changed_percent":0,"pixel_tolerance":0,"region":[0,0,640,480]}
```

Dashboard: `--mail` liest zusätzlich das vorhandene Postfach #16; ohne diesen Schalter keine GitHub-Abfrage.
Nur neue Lock-Aufrufer melden Wartende, also keine vollständige Warteschlange. Worktree-Branches belegen keine aktive
Sitzung. Codex-Werte stammen aus lokalen Rollout-Dateien und werden nach 15 Minuten als veraltet markiert;
fehlende Werte sind unbekannt. API-Werte stammen aus dem lokalen Budget-Ledger, ohne Login, API-Aufruf oder
Ledgeränderung; sie ersetzen keine Anbieterabrechnung. Der lokale Webserver liefert ausschließlich `/` und
`/api/state` und führt keine Aktionen aus. Portable HTML-Dateien enthalten lokale Pfade und gehören in private Ablagen.

Inkompatible CLI-Ergänzung ab Agent Tools 0.13: Cloud-Submit verlangt nun `--eligibility-file` mit exakt diesen Feldern; `prompt_sha256` ist der SHA-256 der
Prompt-Dateibytes. Nur einzelne relative Dateien mit passender Kategorie sind erlaubt; keine Globs, Verzeichnisse,
Symlinks, Engine-/Bilddateien oder CI-Konfiguration. Doppelte JSON-Schlüssel und nichtendliche Zahlen scheitern.

```json
{"manifest_version":1,"kind":"pure_logic","prompt_sha256":"<64 hex characters>",
 "required_capabilities":{"unity":false,"blender":false,"visuals":false,"viewer":false},
 "scopes":[{"path":"src/rules.py","kind":"logic_code"}]}
```

Weitere Kategorien: `logic_test`, `logic_data`, `documentation`. Dies ist eine geprüfte Auftragsdeklaration,
keine semantische Promptgarantie oder Remote-Sandbox. Dry-run bestätigt keine Cloud-Umgebung. Reales Submit prüft
weiterhin Branchfreigabe, sauberen veröffentlichten HEAD, Login und die ausdrücklich gewählte verfügbare Umgebung.
`ce_contract.py` ergänzt Schema-/Typprüfungen durch gültige Referenzziele und zyklusfreie Elternketten; das ersetzt
keinen semantischen Produzenten-/Abnehmertest. Unity-spezifische Kamera-/MCP-Erweiterungen bleiben im Unity-Fachrepo.

## Einbinden

In `.claude/settings.json` jedes Repos:

```json
{
  "extraKnownMarketplaces": {
    "ce-agent-tools": {"source": {"source": "github", "repo": "ffunkfabian-hash/ce-agent-tools"}, "autoUpdate": true}
  },
  "enabledPlugins": {"ce-shared@ce-agent-tools": true}
}
```

Claude Code bietet beim Vertrauen des Ordners an, den Marketplace hinzuzufügen und das Plugin zu installieren; von Hand:
`claude plugin marketplace add ffunkfabian-hash/ce-agent-tools` und `claude plugin install ce-shared@ce-agent-tools`.
Aktualisieren: `autoUpdate` holt neue Versionen beim Start (0.16; Drittanbieter-Marketplaces sind sonst aus), von Hand
`claude plugin marketplace update ce-agent-tools` und `claude plugin update ce-shared@ce-agent-tools`. Claude Code führt je Ordner
(Checkout, jeder Worktree) einen eigenen Projekt-Eintrag, den diese Befehle nicht sicher erreichen: danach `./ce plugins check`
und bei Abweichung `./ce plugins fix --apply` ([0.16](docs/versions/0.16.md)). Ohne Plugin fehlen die gemeinsamen Regeln – Codex und
Antigravity bekommen denselben Wächter als User-Hook (unten), sonst befolgen sie die Regeln aus dem Text der `AGENTS.md`.

### Repos ohne Kopie (0.20 „Lodestar“)

Ein Repo braucht weder Plugin noch eingebettete Kopie: In `.ce-repo.json` steht der geprüfte Stand,

```json
"tools_version": {"version": "0.20.0", "commit": "<voller Commit von ce-agent-tools>"}
```

und `./ce` ist der Lader (`ce_loader.py`, von `ce_sync.py` mit Kopfzeile und Prüfsumme geschrieben – die einzige Datei aus
ce-agent-tools im Repo). Umstellen: `./ce tools pin <commit>` (holt den Stand, setzt `tools_version`, entfernt `tools_dir` und schreibt dessen Lader
als `./ce`; Commits vor 0.20 lehnt es ab), Kopie mit `git rm -r` entfernen, tote `.claude/agents`-Links mit, `.mcp.json` auf `["ce", "search", "serve"]`. Woher die Werkzeuge kamen: `./ce tools where`.
Der Lader nimmt der Reihe nach `CE_TOOLS` (Cloud-Setup, Entwicklung; andere Version nur gemeldet), das installierte Plugin, wenn
`installed_plugins.json` genau diesen Commit führt, den Cache (jede Datei gegen sein Manifest; ein veränderter Cache wird neu
geholt, nie benutzt) und sonst den Download. Ohne Netz und ohne Treffer endet er mit Exit 3 und nennt die Abhilfe.

PR-Check (`check.yml`) mit dem wiederverwendbaren Schritt – er holt denselben Commit nach `$RUNNER_TEMP`, prüft Commit und
Version und setzt `CE_TOOLS`:

```yaml
- uses: ffunkfabian-hash/ce-agent-tools/.github/actions/ce-tools@<derselbe Commit wie tools_version>
- run: ./ce check
```

Cloud-Sitzungen ohne ausgewähltes ce-agent-tools laden über den Lader aus dem öffentlichen Repo; mit ausgewähltem Checkout gilt
dessen `CE_TOOLS`. Neuer Stand für ein Repo: `./ce tools pin <commit>` mit dessen nächster Version (Sync nach Bedarf), derselbe Commit im `uses:` des PR-Checks.

### Andere Clients (Codex, Antigravity)

Die Adapter übersetzen nur Ein- und Ausgabe; entschieden wird in `guard.decide` mit denselben `rules/*.md` (Gleichstand
prüft `tests/test_client_hooks.py`). Eingetragen werden sie von Nexogens `./install.sh --client-hooks` (`--dry-run`,
Sicherungen, `--remove-hooks`). Der eingetragene Befehl reicht nur eine Adapterantwort mit Exit 0 durch (auch eine Sperre);
fehlt das Skript später oder startet es nicht (Syntax-/Importfehler), antwortet er mit Meldung ohne Block.

| Client | Eintrag | Eingang | Ausgabe |
|---|---|---|---|
| Codex (CLI 0.162.1 gemessen; App ungemessen) | `~/.codex/hooks.json`, `PreToolUse`, Matcher `^(Bash\|apply_patch\|mcp__github__merge_pull_request\|mcp__github__enable_pr_auto_merge)$` (0.22; vorher `^(Bash\|apply_patch)$` reicht für alles außer die MCP-Merge-Werkzeuge); läuft erst nach Vertrauen unter `/hooks` | `tool_input.command` (Shellzeile bzw. Patchtext; je Datei des Patches ein Datei-Ereignis); `mcp__…`-Werkzeuge nur als Name für `merge-nur-sightline`, übrige Funktionswerkzeuge nicht | Block `hookSpecificOutput.permissionDecision: "deny"`, Warnung `additionalContext` + `systemMessage`, sonst keine Ausgabe |
| Antigravity (IDE 2.5.5/CLI 1.4.5 gemessen; App ungemessen) | `~/.gemini/config/hooks.json`, Name `ce-shared-guard`, Matcher `run_command\|write_to_file\|replace_file_content\|multi_replace_file_content` | `toolCall.args.CommandLine` bzw. `TargetFile` (+ Inhalt), Ordner aus `Cwd`/`workspacePaths` | `{"decision": "deny", "reason"}`; sonst `allow` (laut Doku: an Folgehook/normale Freigabe weiterreichen), Warnung als `reason` |

Claude-Subagent-Profile (`.claude/agent-guard.json`) gelten nur in Claude Code. `CE_HOOK_LOG=<Datei>` in der Umgebung des
Clients schreibt jede Ein-/Ausgabe als JSON-Zeile – das ist die Messung eines echten Clientlaufs; Adaptertests mit
nachgebauter Eingabe sind keine. Was in welchem Client tatsächlich feuert, steht im [Versionsbericht 0.12](docs/versions/0.12.md). Antigravity hat in den
gemessenen CLI-/IDE-Proben `allow.reason` bei Warnungen und Fehlern nicht sichtbar an den Agenten weitergegeben;
Sperren über `deny` greifen. Eine sichtbare Warnung ist dort derzeit nicht belegt.

Mehrere Workspace-Ordner: Antigravity CLI 1.4.5 liefert kein `Cwd`; der erste `workspacePaths`-Eintrag ist dann nur ein Kontext-Fallback. Gemessen sind Einordner-Workspaces. Bei mehreren Roots kann die Commit-Vorabprüfung falsch zuordnen; einen einzelnen Workspace oder absolutes `git -C` verwenden. Das Git-Netz arbeitet am tatsächlichen Aufruf ([Gatehouse](docs/versions/0.12.md)).

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

Das Netz ist die Laufzeitschranke für alles, was die Vorabprüfung von `commit-on-main` nicht beurteilt (Variablen,
Funktionen, Pipelines, gescheiterte Branchwechsel, Kindshells …): pre-commit sieht den HEAD, auf dem der Commit
tatsächlich entsteht. `tests/test_githooks.py` › `PrecheckBoundaryTests` führt solche Zeilen mit echter Bash in
Wegwerf-Repos aus – gegen main abgelehnt, auf Arbeitsbranches normal.

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
Änderungswünsche, statt selbst zu ändern. Eingebettete Kopien ziehen nach Bedarf mit der nächsten Version des Repos nach
(`ce_sync.py --repo …`, Nexogen › Werkzeugstopp und Sync nach Bedarf); Repos mit `tools_version` wechseln den Stand mit `./ce tools pin`.

## Fortsetzung „Crosscurrent“ (21.10.2026)

Der gemeinsame CLI-Einstieg zeigt acht Gruppen: `check`, `session`, `codex`, `evidence`, `search`, `lock`, `docs`, `report`. Alte direkte Unterbefehle funktionieren weiter. Beispiel: `./ce search query "rig"`, `./ce evidence diff A.png B.png`, `./ce report --since 7d`. Der Bericht unterstützt Claude-Transcripts; er beweist aus gleichen Befehlen keine identischen Eingaben.

`ce_nightly --report-issues` meldet rote Läufe als `eingang`-Issue (keine Duplikate bei offenem Issue). Der Lauf braucht saubere main-Checkouts. Der Trockenlauf führt keine Tests aus, ändert keinen Index und sendet keine Meldungen. Installieren erst aus dauerhaftem Stand und einen vorhandenen Scheduler vorher abgleichen. `numpy`, `Pillow`, ImageMagick und FFmpeg werden nur für Bild-/Medienprüfungen gebraucht, nicht vom Lock.

Sperrvertrag im gemischten Rollout: alte Gesamtsperre exklusiv gegen alle benannten Sperren; Unity/Blender untereinander teilbar, `gpu` exklusiv gegen beide. Ein alter Gesamtschritt darf nicht innerhalb einer benannten Sperre hochstufen: außen die Gesamtsperre oder `gpu` nehmen. Das bewahrt gemischte Robogen-/Cubegen-Abnahmen. Parallelleistung ist damit möglich, aber kein gemessener Geschwindigkeitsnachweis.

`docs.agent_sections` benennt bei Bedarf die bestehende Repo-Gliederung statt der Standardüberschriften; Formgens getrennte Agentenregeln behalten ihre Bullet-Struktur (begründetes leeres Array). Boolesche Richtlinien wie `limits.no_media` sind keine Zeilengrenzen.

Koordination und Repo-Verzeichnis: [Nexogen](https://github.com/ffunkfabian-hash/cubeescape-nexogen). Die gemeinsamen Werkzeuge lesen dessen `repos.json`, soweit lokal vorhanden; ältere Installationen behalten ihre Ausweichpfade. `ce-agent-tools` bleibt die Quelle für ausführbare Werkzeuge, Nexogen für Regeln und Verweise.

### Freigabe per Post (0.21)

Entscheidung des Nutzers vom 23.10.2026 (Sightline 6370961598): Ein Ja darf per Postfach gelten, **nur** für umkehrbare Schritte
(Commit, Push ohne Force, PR öffnen) und **nur** als Freigabe-Nachricht von Sightline in diesem Format; Merge, Tag, Release,
Force-Push und Löschen weiter nur nach Ja im Chat.

```
Sightline · Cloud → TG 0.25 „Ocular“: Freigabe Commit/PR
Opus 5.5, high
Freigabe per Post (ce-freigabe 1)
Ziel: TG 0.25 „Ocular“
Schritte: commit, push, pr
Zitat des Nutzers: „Per Post“
Zeit (UTC): 2026-10-23T16:05Z
```

`./ce session say --name "Sightline · Cloud" --to "TG 0.25 „Ocular“" --freigabe commit,push,pr --zitat "Per Post" "Freigabe Commit/PR"`
schreibt sie; andere Absender, andere Schritte, fehlende Felder oder ein Ziel, das nicht der einzige Empfänger ist, lehnt `say` ab.
Jeder Wächter (Stop-Hook, `ce_await_mail`, PostToolUse-Hinweis) setzt vor die zugestellte Nachricht das Ergebnis von
`ce_await_mail.release_check`: „gültig … erlaubt nur …“, „UNGÜLTIG (Grund)“ oder bei formlosen Freigaben den Hinweis auf
Leitfaden 1. **Grenze:** Alle Sitzungen posten unter demselben GitHub-Konto; der Absender ist der Text der Kopfzeile – eine
Vereinbarung, kein kryptografischer Beweis.

## Erfahrungen

- **Schreibschutz hält root nicht auf:** Cloud-Sitzungen laufen als root; Python legte trotz schreibgeschütztem Cache `__pycache__` an, die Manifestprüfung verwarf den Cache bei jedem Aufruf. Bytecode mit `PYTHONPYCACHEPREFIX` außerhalb umleiten ([0.20](docs/versions/0.20.md)).
- **Python von python.org hat am Mac keine Zertifikate:** `urllib` scheitert an jedem HTTPS-Abruf (`CERTIFICATE_VERIFY_FAILED`),
  solange niemand „Install Certificates.command“ ausgeführt hat; `curl` und `git` gehen. Der Lader versucht deshalb danach das
  System-Bündel `/etc/ssl/cert.pem` und zuletzt `git fetch`. GitHubs Tarball trägt den Commit im Pax-Kopf (`comment`) – so lässt
  sich der Stand ohne Git prüfen ([0.20](docs/versions/0.20.md)).
- **Sauber und gemergt heißt nicht „Rest“:** Der Nachtlauf-Checkout von Cube Escape (`~/Library/Caches/CubeEscape/nightly/checkout`) ist ein Worktree ohne Branch, sauber, ohne Commit über `main` – nach Git-Zustand allein löschbar. Aufräumen wählt deshalb zusätzlich über Worktree-Wurzeln; und `git worktree remove` ohne `--force` löscht ignorierte Dateien (Unity-`Library`, Builds) mit, der Probelauf nennt sie ([0.18](docs/versions/0.18.md)).
- **Argparse hält Zeilen mit führendem Bindestrich für Optionen:** Positional-Argumente (`nargs="*"`) brechen bei Eingaben wie `---` oder `-b` ab; Optionen vor dem Parsen von den Textzeilen trennen und `--` voranstellen ([0.17](docs/versions/0.17.md)).
- **Ein Plugin-Update erreicht nicht jede Installation:** Claude Code hält je Ordner (Checkout, jeder Worktree, auch Codex-Worktrees) einen Projekt-Eintrag auf der Version seiner Anlage; `claude plugin update` bewegt den Nutzer-Eintrag, `--scope project` traf die Worktree-Einträge nicht (6 von 9 blieben auf 0.11/0.13). Versionen nach jedem Rollout mit `./ce plugins check` prüfen, die Sitzung warnt beim Start selbst ([0.16](docs/versions/0.16.md)).
- **Post nur beim Anhalten reicht nicht:** Der Postfach-Wächter startete erst im Stop-Hook; eine Sitzung, die nach der Reservierung lange arbeitet, hörte nichts (Sightline 6370961598 kam eine Minute nach Couriers Anmeldung). Zustellung zwischen zwei Werkzeugaufrufen über PostToolUse (Codex: PreToolUse), gedrosselt und ohne Kosten bei leerem Postfach ([0.21](docs/versions/0.21.md)).
- **Reservieren heißt erst holen:** `--reserve` setzte den Branch auf das lokale `HEAD`; wiederverwendete Worktrees meldeten alte Basen (Loupe, Prismwake, Ocular). Vorher `origin/main` holen und nur bei eigenen Commits auf `HEAD` bleiben ([0.21](docs/versions/0.21.md)).
- **Tests mit fester Uhrzeit laufen ab:** Die Wächter-Tests der 0.15 nutzten `2026-10-22T18:10Z` als Kommentarzeit und wurden 30 min danach rot, weil der Wächter ab der Reservierung bzw. dem Rückblick liest. Zeiten in Tests relativ zu jetzt setzen ([0.16](docs/versions/0.16.md)).
- **Einen Vorab-Wächter nicht zur Shell ausbauen:** Ein statischer Shell-Leser für `commit-on-main` wuchs auf ~860 Zeilen, und jede unabhängige Prüfung fand neue Gegenbeispiele (Variablen, Funktionsumgebung, parallele Pipelines, gescheiterte Wechsel). Vorabprüfung auf statisch eindeutige Formen begrenzen, den Rest mit Hinweis an eine Laufzeitschranke (Git-Hook) geben und diese mit echten Shellläufen testen ([0.12](docs/versions/0.12.md)).
- **Ein Wächter darf nichts aus der geprüften Zeile ausführen:** Der Branchleser startete `git` mit der Umgebung des Befehls; `PATH=/fremd git commit` hätte schon bei der Prüfung ein fremdes `git` gestartet. Leseabfragen nur mit festem Git-Pfad und eigener kleiner Umgebung ([0.12](docs/versions/0.12.md)).
- **Ein fehlendes Hook-Skript sperrt Codex komplett:** `python3 fehlt.py` endet mit Exit 2, und Exit 2 heißt in Codex „Werkzeugaufruf sperren“. Ein Syntax-/Importfehler endet mit Exit 1 ohne Antwort. Client-Hooks deshalb so eintragen, dass nur eine Antwort mit Exit 0 durchkommt, sonst eine Meldung ohne Block ([0.12](docs/versions/0.12.md)).
- **Antigravitys Web-Doku und installierter Stand können abweichen:** Die im `language_server` eingebettete Hook-Doku nennt eine andere `allow`-Bedeutung und Werkzeugnamen aus Step-Typen; Adapter nach Argumentform statt Namen, Verhalten am echten Client messen. Gemessen: der Hook läuft im Ordner der `hooks.json`, und das CLI sendet kein `Cwd` – den Repo-Ordner aus `workspacePaths` nehmen ([0.12](docs/versions/0.12.md)).
- **FTS5-External-Content löscht mit alten Werten:** Bei inkrementellem Ersetzen/Löschen die bisherigen Spaltenwerte an das FTS-`delete` übergeben; leere Strings hinterlassen alte Postings. Konsistenz zusätzlich gegen die Inhaltstabelle prüfen ([0.10](docs/versions/0.10.md)).

- Schemaerfolg belegt keine Bild-/Rig-Abnahme; Originalverträge wiederverwenden und die tatsächlich geprüfte Tiefe nennen ([Pactguard](docs/versions/0.6.md)).
- Nichtendliche JSON-Zahlen und doppelte Schlüssel vor dem Schema ablehnen; die Freigabe eines Richters kommt ausschließlich aus seiner Modellantwort auf stdout ([Pactguard](docs/versions/0.6.md)).
- Alte Gesamtsperren und neue Ressourcen-Sperren teilen eine Barriere; die Umstellung darf laufende ältere Clients nicht überholen.
- Eine geschriebene launchd-Datei ist noch keine Aktivierung: `bootstrap` und anschließendes `print` prüfen.
- GitHub-Umbenennungen erzeugen Weiterleitungen, keine zweiten Repos; IDs und lokale Arbeitsstände getrennt prüfen.
