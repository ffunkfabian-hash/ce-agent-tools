# Changelog

## 0.43.2 „Errata“ – Patch richter-kernpfade (08.11.2026)

- **`./ce judge` zählt `core_paths` der Basis:** Die Modellwahl liest `.ce-repo.json` › `core_paths` vom Basis-Ref (wie `merge-check`, nicht vom PR) und gibt einem Diff an einem dieser Pfade, Quellen von Umbenennungen eingeschlossen, `gpt-6.1-sol` mit dem Grund „Kernpfad laut … core_paths der Basis“. Nicht auswertbare `core_paths` stufen vorsichtshalber hoch; die Unity-Erkennung bleibt. Auslöser: In 0.44 bekam ein Diff an `ce_merge_check.py` Luna. Tests in `test_judge_routing.py` (`BaseCorePathsRoutingTests`).

## 0.43.1 „Errata“ – Patch postfach-monat (08.11.2026)

- **Das Postfach ist nicht mehr fest #16:** Aktuell ist das neueste offene Issue in cubeescape-nexogen mit Label `postfach` (Antwort 1 h in `~/.cache/ce-agent-tools/postfach.json`; ohne Treffer oder ohne GitHub der letzte Wert, sonst 16). Geschrieben (`say`, Anmeldung) wird nur dorthin; gelesen (`who`, `mail`, beide Hooks, `ce_console --mail`) werden das aktuelle und das vorige Issue (ohne älteres: #16), `--issue` setzt die Nummern weiter fest. Ausgaben nennen die ermittelte Nummer. Tests in `test_postfach.py` (`MonthlyPostfach`). Bekannter Kompromiss: Beim Monatswechsel schreiben Sitzungen mit altem Cache bis zu 1 h noch ins vorige Issue; gelesen wird es weiter, die Post kommt verspätet an.

## 0.43.0 „Errata“ – unveröffentlicht (07.11.2026)

Die vier offenen niedrigen Befunde aus 0.37 und Trimline 10, Widerspruch 4.

- **`./ce verify` zählt je Befehl und liest pytest:** Ein Befehl, der Tests meldet, aber keinen ausführt oder alle überspringt, ist „nicht prüfbar“, auch wenn ein anderer Befehl Tests hatte. Neben `Ran N tests` liest es die pytest-Zusammenfassung (`3 passed, 1 skipped in 0.12s`, `no tests ran`). Zahlen je Befehl stehen im Ergebnis unter `commands`.
- **Umgebung von `./ce verify`:** Der Schlüssel der geteilten Umgebung deckt mit `-r` und `-c` eingebundene Dateien ab. Wer das Repo selbst installiert (`-e .`, lokaler Pfad, auch in einer eingebundenen Datei), bekommt eine eigene `.venv` in der Kopie statt eines toten Links in der geteilten.
- **`merge-check` liest `previous_filename` überall (Kernpfad):** Umbenennung von Code nach `.md` (`run.py` → `run.md`) ist kein Doku-Patch mehr, Version und CHANGELOG werden geprüft. Eine Testdatei, die ihre Code-Endung verliert (`test_a.py` → `test_a.py.off`), zählt als aus dem Testlauf verschoben.
- **`check.quick` in falscher Form ist keine Sackgasse mehr:** In einem Repo ohne GitHub-CI endet `"quick": "pytest"` in `merge-check` als „nicht prüfbar“ (Exit 2, keine Quittung) mit der erwarteten Form und dem Ausweg. Der PR, der `check.quick` berichtigt, bekommt die Warnung wie bei fehlendem `check.quick` und geht mit dem Ja des Nutzers durch. `./ce verify` nennt die erwartete Form ebenfalls.
- **`AGENTS.md`:** Die Versionsregel folgt der globalen Patch-Regel (bis 300 Zeilen ohne Format, Vertrag, Kernpfad: Patchnummer, sonst Version).
- Tests: neue Fälle in `test_verify` und `test_merge_check`, alle vorher rot. [Version](docs/versions/0.43.md).

## 0.42.2 „Ratchet“ – Patch nachtlauf-stand (07.11.2026)

- **Nachtlauf prüft den Stand:** Schreibt das `nightly.sh` eines Repos eine Zeile `Stand: <sha> (<ref>)` (Cube Escape), gilt ein grüner Lauf nur, wenn Zeile (im Lauf oder im während des Laufs geschriebenen `report_*.md`), Klammer `origin/main` und der sha von `origin/main` vor oder nach dem Lauf übereinstimmen. Sonst `red (stand)`, auch in `--report-issues`. Auslöser: Am 05. und 06.11. testete ein altes Launch-Klon-Skript das lokale `main` vom 24.10. und meldete grün. Repos ohne solche Zeile werden nicht beurteilt. Tests in `test_nightly_stand.py`.

## 0.42.1 „Ratchet“ – Patch post-richter (07.11.2026)

- **Post nur für die Besitzerin:** `postfach_inbox.py` nutzt jetzt `postfach_watch.owns`, die Regel des Stop-Wächters. Eine andere Sitzung im selben Checkout bekommt weder Post noch einen Abruf, eine Reservierung ohne Besitzer geht an die erste Sitzung. Codex übergibt nur den Ordner und behält die alte Regel.
- **`./ce judge --api` liefert ein Urteil oder sagt, warum nicht:** Opus 5.5 denkt immer, und das Nachdenken zählt zu den 2048 Ausgabetokens. Der Richter läuft deshalb mit `effort: low` (`ce_api.ask(effort=…)`, `./ce api ask --effort`) und höchstens sechs Befundzeilen je Abschnitt; das Limit bleibt. Ohne vollständige Antwort nennt die Zeile „kein verwertbares Urteil“ Abschnitt, Ursache und Rückfall. Messung im README › Erfahrungen.
- **Klein:** Der PR-Text von `./ce pin` sagt nur noch, was beim Anlegen stimmt („Nicht fertig, noch ungeprüft“); parallele Läufe von `./ce verify` teilen keine `.tmp`-Datei mehr (niedriger Befund aus 0.37).

## 0.42.0 „Ratchet“ – unveröffentlicht (06.11.2026)

Trimline 22 und Entscheidung 308.

- **`./ce pin <version>` (`ce_pin.py`, Daten `ce_pin_repos.json`):** zieht den Werkzeug-Pin in allen aktiven Repos nach, ohne Sitzung je Repo. Aktive Repos aus Nexogens `repos.json` auf GitHub, Abweichungen je Repo als Daten, `--dry-run` zeigt jeden Diff ohne Schreiben. Je Repo Branch und PR aus einer Wegwerf-Kopie, dann `./ce verify` oder Warten auf CI, dann `./ce merge-check`, eine Tabellenzeile je Repo. `merge-check`, Wächter und Merge-Tür sind unverändert.
- **Vorbedingung:** `version` muss `{file, pattern}` sein. Bis Motiongen PR 72 und Critic PR 32 gemergt sind, stoppen diese beiden Repos und die Runde schafft vier von sechs.
- **Kein Merge im Werkzeug:** Stufe 1 ist nur als Bash-Zeile durch die Merge-Tür sichtbar; das Werkzeug gibt sie aus, Sightline führt sie aus.
- Tests: neue Fälle in `test_pin`, davon Läufe gegen echte Git-Remotes. [Version](docs/versions/0.42.md).

## 0.41.1 „Homing“ – Patch waechter-messung (06.11.2026)

- **Trimline 21:** `heavy_lock_waited_s` zählt jede Wartezeit einmal: nur die Zeile `heavy lock: free after N s` aus Bash-Läufen, nicht „held, waited“ (dieselbe Wartezeit) und nicht aus `cat`/`grep` alter Logs.
- **Entscheidung 311:** `--reserve` schreibt `session_id` (optional) in `ce-session.json`; Stop-Wächter und Erinnerung laufen nur für diese Sitzung. Ohne Besitzer übernimmt der erste Stopp, ein Nachfolger nach 6 h Stille des Besitzers. Die 300.000-Grenze von Sightline gilt nur für den Besitzer.

## 0.41.0 „Backlink“ – unveröffentlicht (06.11.2026)

- **`./ce refs` (`ce_refs.py`):** `who`, `uses`, `missing`, `stats` lesen die GUID-Verweise eines Unity-Projekts aus YAML-, `.meta`- und JSON-artigen Dateien. Der Code-Seite (`csharp-lsp`) fehlte bisher die Verdrahtung in Prefabs und Szenen. Eigenständig, nur Standardbibliothek, für jedes Unity-Projekt; vom Nutzer beauftragt.

## 0.40.0 „Offcut“ – unveröffentlicht (06.11.2026)

Trimline 23, vom Nutzer freigegeben.

- **`./ce codex cloud` entfernt:** `ce_codex_cloud.py` samt `test_codex_cloud.py` und `test_cloud_eligibility.py` (beide prüften nur dieses Werkzeug) gelöscht; Lader, `SYNC_FILES`, Richter-Muster `INFRASTRUCTURE` und README nachgezogen. Vorab geprüft: kein echter Lauf in 828 Transkripten, die Claude-Cloud-Umgebung „Default“ nutzt das Werkzeug nicht. `./ce codex quota` und `./ce judge` unverändert; `./ce codex cloud` meldet die Nutzung. [Version](docs/versions/0.40.md).
  Die Nummer 0.38 kam auf `main` nie vor: der PR blieb offen, bis 0.39 „Sunset“ `merge-check` für gelöschte Tests öffnete, und wurde danach auf 0.40.0 umnummeriert.

## 0.39.0 „Sunset“ – unveröffentlicht (06.11.2026)

Entscheidung 304.

- **`merge-check`: gelöschte Tests, deren Code im selben PR entfällt:** `test_changes` zählt eine Testdatei nicht mehr als Abschwächung, wenn sie ganz gelöscht ist, ihr Patch lesbar ist und ihre entfernten Zeilen eine ebenfalls gelöschte Nicht-Test-Datei nennen (Pfad, Pfadende oder Dateiname als ganzes Wort; Modul nur als das, was eine Importzeile lädt, nach derselben Pfadende-Regel, Python und JS/TS). `.md` zählt nicht als Quelle. Alles andere bleibt rot wie bisher, auch „Inhalt nicht lesbar“, „verschoben“ und „übersprungen“. Neben einer ausgenommenen Datei gleichen sich die übrigen Testdateien nicht mehr aus: Jeder Verlust ist rot, neue Tests zahlen nicht dafür.
- **Nie Stufe 1:** Ein solcher PR braucht das Ja des Nutzers (`ja_noetig_weil`), `warnings` nennt jede ausgenommene Datei mit Testfällen, Prüfungen und entfallener Quelle für den Richter.
- Tests: 14 neue Fälle in `test_merge_check`. [Version](docs/versions/0.39.md).

## 0.37.0 „Waybill“ – unveröffentlicht (06.11.2026)

Entscheidungen 300 und 301.

- **Pin-Ausnahme in `merge-check`** (Entscheidung 300): Ändert ein PR in `.ce-repo.json` nur `tools_version`, sperrt das Stufe 1 nicht mehr; steht `.ce-repo.json` in den `core_paths`, bleibt er auch Patch. Bedingung: ce-agent-tools bestätigt, dass der Commit auf `main` liegt, die genannte Version trägt und der Pin nicht zurückgeht. Sonst, auch bei gescheiterter Abfrage, Stufe 2 wie bisher; alle anderen Bedingungen bleiben.
- **`./ce verify <owner/repo> <PR>`** (Entscheidung 301, neu `ce_verify.py`): sauberer Lauf von `check.quick` der Basis auf dem Head-SHA am Mac, in einer Wegwerf-Kopie im Cache, ohne Nachtlauf. Quittung je Head-SHA, nur bei Grün. Ohne Workflow-Datei in der Basis ist `merge-check` ohne diese Quittung rot, reine `.md`-PRs ausgenommen; eine Basis ohne `check.quick` gibt eine Warnung und keine Stufe 1; wo CI läuft, ändert sich nichts.
- **CI-Erkennung:** `merge-check` liest die Workflow-Dateien der Basis; die Actions-Liste nennt auch Reste geschlossener PRs.
- Tests: `test_merge_check` (`PinTests`, `VerifyReceiptTests`), neu `test_verify` (echtes Git-Repo als Quelle, echte Läufe). [Version](docs/versions/0.37.md).

## 0.36.2 – Patch sightline-grenze-tests (06.11.2026)

- **Testlücken der Sightline-Grenze geschlossen:** Stop-Weg über den Hook (Sightline mit 200.000 ohne `Spitzen-Input` im `ce-session-stats.log`, andere Sitzung mit Treffer), CLI `ce_report.main`, Pulse-Text `(> 300,000)`.
- Klein: Git-Timeout in `ce_report.input_limit` 2 s (unter dem 5-s-Hook-Timeout), Hinweisschlüssel `input>{limit}` im Pulse, Docstring nennt die Sightline-Ausnahme. Bekannte Grenze: die 300.000 hängen am Ordner, nicht an der Sitzung.

## 0.36.1 – Patch sightline-grenze (06.11.2026)

- **Kontextgrenze 300.000 Input-Tokens nur für Sightline** (Entscheidung 295): `ce_report.input_limit` liest den Namen aus `<git-dir>/ce-session.json`; genau `Sightline` → 300.000, sonst (auch fehlende oder kaputte Datei) 150.000. Gilt für den Hinweis in der Sitzung (`session.py` pulse) und im Bericht/Stop-Log (`ce_report.hints`). Grenze von 300 Nachrichten unverändert.
- Tests: vier Fälle in `test_report_usage` (`SessionInputLimitTests`), pulse in `test_tools`.

## 0.36.0 „Capstan“ – unveröffentlicht (06.11.2026)

Trimline 29 und 1b.

- **Harter Kern über SessionStart** (Punkt 29): `hooks/kern.md` ist eine Kopie von Nexogen `templates/kern.md`, `tests/test_kern.py` vergleicht sie am Mac bytegleich mit `origin/main` der Quelle und nennt bei Abweichung den Abgleichbefehl. `session.py` stellt die zehn Regeln an den Anfang des Startkontexts, außer die globale `CLAUDE.md`, die Repo-Dateien und ihre Importe tragen schon jede Regel wortgleich. Im Zweifel erscheint der Kern; fehlt die Kopie, kommt eine Warnung und der übrige Kontext bleibt. Scheitert ein anderer Teil des Startkontexts, kommt der Kern trotzdem.
- **Nachtlauf warnt statt abzubrechen** (Punkt 1b): `run_one` stoppt bei falschem Branch oder geändertem Checkout nicht mehr, wenn `own_main_script` belegt, dass `check.nightly` nur ein `nightly.sh` unter `sh`, `bash` oder `zsh` ist, das origin/main in einem eigenen Worktree testet, dass Einstieg, `.ce-repo.json` und Skript einem Stand von origin/main entsprechen und keine unversionierte `.py` im Wurzelordner liegt. Zustand `green (Warnung)`, Hinweis auch in der Mitteilung, Exitcode unverändert. Skripte, die im Checkout selbst testen (Toolgen, Robogen), und direkte Befehle (Formgen) bleiben `red (checkout)`, ebenso ein `main` mit der alten Skriptfassung (Cube Escape bis zum Nachziehen).
- Tests: `test_kern`, `test_nightly_checkout` (echte Git-Repos, echter `ce_check`-Lauf). [Version](docs/versions/0.36.md).

## 0.35.2 – Patch richter-tests (05.11.2026)

- **Richter-Tests hängen nicht mehr am echten Codex-Kontingent:** Unter 15 % Rest wählte `ce_judge` seit 0.32 auch im Test den API-Weg, vier Tests wurden so am Mac rot. `tests/judge_quota.py` gibt den Stand aus `~/.codex` fest vor, Schwelle und Wegwahl laufen echt. `test_judge_picks_the_model_from_the_diff_and_dry_runs` und `test_judge_stderr_only_cannot_approve` prüfen jetzt beide Wege, nur Tests geändert.

## 0.35.1 – Patch nachtlauf-stillgelegt (05.11.2026)

- **Nachtlauf überspringt stillgelegte Repos:** Die Standardauswahl lässt Repos aus, deren Eintrag in `repos.json` `status: "stillgelegt"` trägt (Bericht: „stillgelegt – übersprungen“, nicht rot). `--repo` und `CE_REPOS` bleiben ungefiltert; ohne Registry oder Status läuft alles wie bisher. Das Feld `status` bekommt damit eine gemeinsame Bedeutung. Plugin am Mac nachziehen.

## 0.35.0 „Turnstile“ – unveröffentlicht (05.11.2026)

Nachzug zu Nexogen 1.26 „Keel“ (`docs/nachzug-agent-tools-keel.md`, Teile A und B).

- **Patch-Tür ohne Ja** (Punkt 13): Ein grüner `merge-check`, der Stufe 1 erfüllt, schreibt eine Quittung der Art `patch` ohne Wortlaut; der Wächter nimmt sie an, gleiche Zeile, gleiche Bindung an Repo, PR, Head und 30 Minuten. Quittungen mit Ja aus 0.34 gelten weiter.
- **Fehlende `core_paths` sperren Stufe 1:** Ohne Liste in der Basis (auch leer oder unbrauchbar) gibt es keine Patch-Quittung, Stufe 2 bleibt möglich.
- **Stufe 1 enger als „grün“ (Richter, über den Auftrag hinaus):** An dieser Tür steht kein Mensch, also verlangt sie zusätzlich: eigener PR (gleiches Repo, Head = dieser Ordner, sonst Fork-PRs ohne Ja), Basis = Standardbranch (die Quittung überlebt sonst `gh pr edit --base main`), keine Cloud-Sitzung (Stufe 3), `.ce-repo.json` außer der Version unverändert, nichts ungeprüft, und ein erfolgreicher Check, wo CI kommen kann (direkt nach dem Push ist der Lauf sonst grün; Repos ohne Workflow und reine `.md`-PRs warten nicht). `core_paths` hier um `ce_merge_check.py`, die Client-Wächter, `.github/workflows/*` und `.ce-repo.json` erweitert.
- **Zwei Grenzen:** `VERSION_LINES = 300` für die Einstufung, `NO_JA_LINES = 100` für die Stufe. Neue Ausgabefelder `ohne_ja`, `ja_noetig_weil`, `code_lines`, `receipt_kind`; die Warnung „nur Patch-Nummer erhöht“ kommt erst über 300 Zeilen.
- **Richter-Hinweis:** Ab 100 Zeilen Code (Diff ohne `.md`) warnt `merge-check`; ob ein Richter lief, sieht es nicht.
- **Regel `merge-nur-sightline` heißt `merge-tuer`** und beschreibt die drei Stufen; `commit-on-main` verweist auf Regel 4. Keine Konfiguration eines Repos nennt den alten Namen.
- **`ce-freigabe` entfällt:** `say --freigabe/--zitat/--zeit` und die Prüfung der Freigabe-Post sind weg. Die alten Optionen enden mit einer klaren Meldung statt als Nachricht; eine Nachricht, die von einer Freigabe spricht, trägt weiter einen Hinweis: Post trägt kein Ja. Alte Nachrichten im Postfach stören nicht.
- **Stop-Hook:** Die Sitzungsstatistik steht in `<git-dir>/ce-session-stats.log`, nicht mehr in `report.md`, dessen Text die PR-Beschreibung wird (gesehen in PR #37).
- Tests: `test_merge_check` (Grenzen, Stufe 1, Quittungen), `test_guard.MergeDoorTests` (Patch-Quittung). Die Tests der Freigabe-Post fallen mit ihrer Funktion weg, Hinweis und alte Nachrichten sind neu abgedeckt. [Version](docs/versions/0.35.md).

## 0.34.1 – Patch merge-check-rest (05.11.2026)

- **`merge-check` liest nur noch REST** (`gh api repos/…`): PR, Head, Mergebarkeit, Dateigrößen (ein `git/trees`-Lesen, `contents` je Datei bei abgeschnittenem Baum) und CI aus `check-runs` plus Commit-Status statt GraphQL, das in Cloud-Sitzungen mit 403 gesperrt ist. Dazu: `self.skipTest(` zählt als übersprungen, mehr als 3000 Dateien im PR sind rot statt still gekürzt, `rules/*.md` steht in `core_paths`.

## 0.34.0 „Gatekeeper“ – unveröffentlicht (04.11.2026)

- **`./ce merge-check <owner/repo> <PR>`** (Trimline 12): `ce_merge_check.py` prüft mechanisch Head-SHA (`--sha`), `main` enthalten, Konflikt, CI, Dateien über 5 MB, `report.md`, gelöschte oder abgeschwächte Tests, Version und CHANGELOG gegen `.ce-repo.json`. Ein Lauf in der Warteschlange ohne Check-Eintrag zählt als laufend, ein Head ganz ohne Checks gibt eine Warnung. Ergebnis: JSON auf stdout, eine Textzeile auf stderr, Exit 0 grün, 1 rot, 2 nicht prüfbar. Einstufung Version ab 101 Zeilen oder bei berührtem Kernpfad, sonst Patch. Ein Kernpfad auf bloßer Patch-Nummer ist rot (außer bei reinen `.md`-PRs, dort nur Warnung), die Größe allein nur eine Warnung, weil die Regel „etwa 100“ sagt.
- **`.ce-repo.json › core_paths`** (Trimline 15, nur die Pfadliste): Glob-Muster, gelesen vom Basisbranch. Hier gesetzt, Vorschläge für alle Repos in [`docs/core-paths.md`](docs/core-paths.md), jedes Muster gegen GitHub geprüft. Fachrepos sind nicht geändert; ohne das Feld warnt `merge-check`.
- **Merge durch die Fachsitzung** (Trimline 14): Ein grüner Lauf mit `--ja "<Wortlaut>"` schreibt `<git-dir>/ce-merge-check.json`; 30 Minuten lässt der Wächter dann aus demselben Ordner genau `gh pr merge <PR> -R <owner/repo> --match-head-commit <SHA>` durch. Jeder neue Lauf löscht die alte Quittung. `--auto`, `--admin`, `gh api`, MCP-Werkzeuge und Shell-Hüllen bleiben gesperrt, Sightline wie bisher. Das Zitat hält das Ja fest, beweist es aber nicht.
- **Pre-Push in öffentlichen Repos** (Trimline 28): `ce_githooks.py` sperrt neue Binärdateien über 5 MB und erkennbare Geheimnisse (neun Token-Formate, private Schlüssel mit Inhalt) in den neuen Commits. Neu heißt neu am Ziel: Tracking-Refs zählen nur, wenn die Fetch-URL das einzige Push-Ziel ist. Nur hinzugefügte Zeilen zählen, Platzhalter gehen durch, ein Fund wird nur mit Art und Ort genannt. Sichtbarkeit kommt von `gh`; keine Antwort lässt den Push mit Meldung durch, andere Hosts werden still übersprungen. Die Wrapper reichen jetzt die Hook-Argumente weiter; alte Wrapper prüfen `origin`, also wirkt die Prüfung ohne Neuinstallation.
- Tests: neu `test_merge_check`, dazu `test_guard.MergeNurSightlineTests` (Quittung) und `test_githooks` (`SecretPatternTests`, `PublicPushTests` mit echten Pushes). [Version](docs/versions/0.34.md).

## 0.33.0 „Earshot“ – unveröffentlicht (04.11.2026)

- **Hinweise erreichen das Modell** (Trimline 3 + 4): In Claude Code sah rund 95 % der Hook-Meldungen nur der Nutzer (`systemMessage`). Probe 04.11.2026 in drei headless-Sitzungen: `hookSpecificOutput.additionalContext` kommt aus PreToolUse, PostToolUse und Stop beim Modell an – bei Stop antwortet das Modell darauf genau einmal mehr (bei jedem Stop wiederholt: Schleife, 10 Stops in der Probe). Deshalb: Warnungen des Wächters (`output-cap`, `heavy-unlocked`, `judge-template`, `actions-sparsam`) als `additionalContext`, einmal je Sitzung und Regel, dann jeder 25. Treffer (`hooks/hint_state.py`, Datei je Sitzung im Cache, `CE_HOOK_STATE`); `vor-commit-doku` als `additionalContext` mit Anweisung (`--amend`); `check-before-stop` als Stop-`additionalContext` einmal je Sitzung und nie bei `stop_hook_active`; Sitzungsgrenze (über 300 Nachrichten, über 150 000 Input-Tokens je Anfrage) neu als PostToolUse-Hinweis alle 20 Aufrufe (Byte-Scan des Transkripts, kein JSON), je Schwelle einmal (`CE_MESSAGE_LIMIT` nur als Probehilfe).
- **Gestrichen in Claude Code:** die Stop-`systemMessage` mit den `ce_report`-Hinweisen (sie stehen weiter in `report.md`), der Text des Commit-Vorabhinweises („Vorabprüfung: möglicher `git commit` … Git-Netz“, 700×), jede Warnungs-`systemMessage` des Wächters. Codex, Antigravity und Vibe bekommen Warnungen und Vorabhinweis unverändert über `client_adapter` (`guard.decide(..., client="adapter")` ist der Standard, Claude Codes `main()` ruft `client="claude"`). `git-nur-auf-wunsch` und die sieben kleineren hookify-Warnungen liegen in den Repos (CE, Cubegen, Robogen, Toolgen), nicht hier – Liste im Versionsbericht.
- **Datentext ohne Fehlalarm – bei Warnungen** (Trimline 5, Nutzerentscheid nach drei Richterrunden: Blocks lesen den vollen Befehl wie vor 0.33, Datentext darf einen Hinweis verschlucken, nie einen Block): Bedingungen auf `command` lesen in Warnregeln `guard.rule_text()` – ohne Heredoc-Inhalte, mehrzeilige Zitate und zitierte Argumente von `echo`/`printf`/`grep`/`rg`/`pgrep`/`pkill`; `not_contains` liest die ganze Zeile; ausführbarer Text bleibt (Heredoc/mehrzeiliges Zitat für eine Shell, `$(…)` in Zitaten, Kommentare als Text, zusammengesetzte Trenner, `<<` in `$((…))` als Shift, Expansionen unzitierter Heredocs, zitierte Programmnamen, Shell hinter Pipe oder Kontrollwort der Heredoc-Zeile, Klammern in Zitaten – Richter-Befunde Runde 1 bis 3). `heavy-resource` nimmt `--help` aus, `output-cap` keine Schreib-Umleitung (`cat > x.json <<EOF`), `judge-template` nennt neun Prüfpunkte. Alle Blocks bleiben und prüfen ungefiltert; die Vorabprüfung von `commit-on-main` las nie Heredoc-Inhalte (ein `bash <<EOF` mit Commit bleibt Hinweis fürs Git-Netz).
- **LESSONS nicht doppelt** (Trimline 6): SessionStart lässt `docs/LESSONS.md` aus, wenn `CLAUDE.md` (auch `CLAUDE.local.md`, `.claude/CLAUDE.md`, oder eine dort importierte Datei wie `AGENTS.md`) dieselbe Datei per `@` importiert (Pfad relativ zur importierenden Datei, Vergleich per Inode – Richter-Befund 6) – CE, Toolgen, Robogen, Formgen, Reference; BALLON und Nexogen bekommen sie weiter über den Hook.
- Tests: `test_guard.RuleTextTests` (13 Datentext-Fälle, Umfeld eines Heredocs, `not_contains`, Schreib-Heredoc, Commit-Block), `ClaudeRenderingTests` (Kontext statt `systemMessage`, Hinweis nur für Adapter, Blocks gleich, Drossel je Sitzung und Regel, Hook-Skript, fail-open), `test_tools` (Vor-Commit als Kontext, Stop ohne `systemMessage`, `check-before-stop` einmal/nie bei `stop_hook_active`, LESSONS-Import in vier Formen, PostToolUse-Puls). `./ce check` grün (103 s). [Version](docs/versions/0.33.md).

## 0.32.0 „Tripwire“ – unveröffentlicht (01.11.2026)

- **Richter wählt den Weg selbst** (Panorama 4): `./ce judge` liest `ce_codex_quota.route()` – unter 15 % Rest in einem Fenster oder Limit erreicht → `--api` (Opus), sonst Codex; Stand unbekannt oder Leser kaputt → Codex (fail-open). Zeile „Weg:“ nennt den Grund, `--codex` erzwingt Codex, `--api`/`--opus`/`--model` bleiben ausdrücklich. `ce_codex_quota.py` ist importierbar (`summary()`, `route()`) und hat erstmals Tests (`test_codex_quota.py`).
- **Schlussrichter-Fixes:** `route()` wertet ein Fenster mit `resets_at` in der Vergangenheit als zurückgesetzt (alter Stand hielt sonst jede Prüfung dauerhaft auf `--api`); `say --frage` mit `--freigabe` ist Exit 2.
- **CI-Fixes (Runner Python 3.12):** `STALE_IGNORE` nutzt `dir/**/*` (ein schließendes `**` trifft vor Python 3.13 nur Ordner, `docs/versions/*.md` wurde gescannt; Test belegt mit 3.12 rot→grün); der Workflow installiert `imagemagick` + `fonts-dejavu-core`. Echter Fehler dahinter: ein ImageMagick 7 ohne Standardschrift (Runner, schlankes Linux) brach `ce_media sheet` mit „unable to read font“ ab – jetzt wählt `ce_media` eine installierte Schrift (`-font`) und fällt bei Fehlschlag des `montage` auf Pillow zurück (`test_media_font`).
- **`--api` ohne abgeschnittene Abschnitte:** Abschnitte 20 KB statt 40 (höchstens 12), jede Anfrage trägt Dateiliste mit +/− und die Doku im Diff; Abschnitt am `max_tokens` bleibt Nachbessern; Test mit großem Diff (keine Zeile geht beim Teilen verloren).
- **Entscheidungsfragen** (Panorama 8): `./ce session say --frage "<A oder B?>" --standard A --frist 60m` – Thema `Entscheidung nötig: …`, Zeilen `Standard:` und `Frist: … bis <UTC>`, Hinweis „ohne Antwort des Nutzers entscheidet niemand“; der Wächter stellt `ENTSCHEIDUNGSFRAGE (Standard, Frist, noch n min / abgelaufen)` voran. Nur Format und Anzeige.
- **Selbstprüfung** (Panorama 7): `.github/workflows/check.yml` (Linux, `unittest discover`, `concurrency`, 20 min, `paths` ohne Doku, `plugins/**` zählt als Code); `ce_lint`-Profil `rules` für Regel-Repos (`.ce-repo.json › lint.profile`), ce-agent-tools selbst lint-grün; `ce_sync` bleibt (Lader und `ce_lint` rufen es auf), die toten `scripts/ce_tools`-Freigaben stehen nur in `~/.claude/settings.json` des Nutzers, nicht in einem Repo.
- **`./ce docs` warnt vor Veraltetem** (Panorama 5): `Agent Tools 0.<n>` unter 0.30, `cubeescape-rules`, `heavy-run.lock`, „eingebettet in jedem Repo“, `Standardbranch` ≠ origin/HEAD – nur Warnung, Exit unverändert.
- **`ce_report` am Stop** (Panorama 2): Hinweis „über 150 Nachrichten, 0 Unteragenten“.
- Keine Regel in `rules/` geändert (nur ein Satz im Text von `heavy-unlocked`), kein Guard-Muster berührt. Tests: neu `test_codex_quota`, `test_decision`, `test_docs_stale` und Ergänzungen in `test_judge_api`, `test_tools`, `test_report_usage`. [Version](docs/versions/0.32.md).

## 0.31.0 „Crossover“ – unveröffentlicht (31.10.2026)

- **Werkzeuge laufen unter Windows, macOS-Zweige unverändert** (Auftrag Sightline 31.10.2026, Nutzer: „Ich will, dass alles auf Mac und Windows läuft“; Grundlage Bestandsliste Postfach 6545955158). `heavy_lock.py` ist die eigenständige Plattformschicht: Sperre unter Windows per `LockFileEx` (geteilt/exklusiv wie `flock`, ein Byte bei 2^62, damit die Halterzeile lesbar bleibt), Prozessgruppe als Job-Objekt (angehalten gestartet, Kill-on-Close, Breakaway erlaubt; Ctrl+C/Ctrl+Break gehen als Ctrl+Break weiter, ein zweites Signal beendet den Job), Freigabe erst bei leerer Gruppe wie bisher; `cache_dir()` (macOS/Linux `~/Library/Caches/CubeEscape`, Windows `%LOCALAPPDATA%\CubeEscape`) für alle Cache-Pfade; `pid_alive`, `process_started`, `process_table` (Win32_Process), `script_argv` (Shell-/Python-Skripte über Git Bash bzw. diese Python, WinError 193), `posix_shell` (nie `System32\bash.exe` = WSL).
- **Wächter war unter Windows komplett aus:** Alle Hooks und der `ce-search`-MCP starteten `python3`, dort der Store-Platzhalter (Exit 49, kein Block). Neu `hooks/ce-python` (Mac: `exec python3` wie bisher; Windows: `py`). `./ce` wählt ebenso und setzt unter Windows `PYTHONUTF8=1`. Wächter, Adapter und Lader lesen und schreiben unter Windows UTF-8 (vorher cp1252: „Á“/„←“ im Befehl ließen den Wächter fail-open durchwinken, Adapter-JSON mit `›` war für Codex ungültig).
- `ce_loader.py`: unter Windows Unterprozess statt `os.execvpe` (endete sofort mit 0), Exitcode wird weitergegeben, kein sh → Exit 3 mit Meldung.
- `guard.py` und Regeln: `commit-on-main` findet Git for Windows nur unter den Programmordnern (nie PATH), feste Systemumgebung; `cd`-Ziele in Laufwerks- und Git-Bash-Form (`G:\x`, `G:/x`, `/g/x`), physischer Weg von Hand (Windows faltet `..` vor Links). `unity-nie-auf-checkout` erkennt jeden Pfad `…/Claude/Cube Escape` auch mit `\`, Laufwerk, `/g/…`, `~` und der Verbindung `C:\Users\…\Claude` (auch am Mac: `~/Claude/Cube Escape` sperrt jetzt), `actions-sparsam` auch mit `\`.
- Git-Netz: Wrapper mit LF (Windows schrieb CRLF), Meldungen in UTF-8.
- Weitere Windows-Fixes: `os.kill(pid, 0)` (= TerminateProcess) in `ce_await_mail`/Postfach-Hooks ersetzt; Ortserkennung „Windows“; Vibe-Abbruch über das Job-Objekt; Nachtlauf mit `SetThreadExecutionState` statt `caffeinate`, launchd-Befehle unter Windows mit Exit 2 und Hinweis; `ce_api_budget` ohne `fcntl`; UTF-8 für Konsole, Git-/gh-Ausgabe und Dateien (`ce_common.utf8_console`); `CE_REPOS`/`CE_TIDY_WORKTREE_ROOTS` an `os.pathsep` (Windows `;`); Windows-Pfadschreibweisen im Worktree-Schutz; Blender- und VS-Code-Abbruch ohne `killpg`.
- Tests: Windows 712 grün (5 übersprungen, vorher 118 rot von 532), Linux (WSL Ubuntu) siehe Bericht; neue Windows-Tests für Sperre, Job-Objekt, Ctrl+Break, Breakaway, Skriptstart, Lader-Exitcode, Regeln mit Windows- und Mac-Befehlen, eingetragenen Hook-Befehl. Mac-Probe offen. [Version](docs/versions/0.31.md).

## 0.30.0 „Second Opinion“ – unveröffentlicht

- `ce_judge.py --api` / `./ce judge --api`: Der Richter läuft ohne Codex und ohne Unteragenten über `ce_api.ask` mit Opus – gleiche Checkliste (`pruefung.md`), gleiche Ausgabe (Tabelle + Schlusszeile, Abschnitt „Richter“ in `report.md`), Exit 0/1/2 wie bisher. Budget, Ledger, Anfrage- und Monatslimit von `./ce api` gelten unverändert (kein zweiter Weg): Weil eine Opus-Anfrage höchstens 0,25 USD kosten darf, wird der Diff je Datei (große Dateien je Hunk) in Abschnitte zu `--api-chunk-kb` (40) geteilt; alle werden vorab kostenlos gezählt, bezahlt wird nur, wenn jeder Abschnitt ins Anfragelimit und alle zusammen in den Monatsrest passen (höchstens `--api-max-sections` 8). Fail-closed: Freigabe nur, wenn jeder Abschnitt mit `end_turn`, Zeilen zu allen acht Punkten und Schlusszeile „Freigabe“ antwortet; abgeschnittene Antwort, fehlende Zeilen oder API-Fehler (ohne Wiederholung) ergeben Nachbessern. Anfragen in 0600-Dateien außerhalb des Repos, danach gelöscht. 10 neue Tests (`tests/test_judge_api.py`, Fake-Transport über das echte `ce_api.ask` und Ledger, vier Gegenproben rot); zwei echte Proben (0,0698 + 0,0599 USD). [Version](docs/versions/0.30.md).

## 0.29.0 „Plumbline“ – unveröffentlicht

- `tests/test_vibe.py`: Der Push-Sperrtest nahm an, dass `GIT_CONFIG_KEY_0` frei ist, und scheiterte, wenn die Umgebung schon `GIT_CONFIG_COUNT` setzt (Cloud-Container mit 3 Einträgen). `worker_env()` hängt die Sperre korrekt hinten an; der Test prüft jetzt die Stelle `GIT_CONFIG_COUNT-1`, der Fake-Vibe zeichnet alle `GIT_CONFIG_KEY_*`/`VALUE_*` auf, und ein neuer Fall mit zwei vorab gesetzten Einträgen belegt Anhängen ohne Überschreiben (Gegenprobe: Sperre an Stelle 0 → rot). Keine Änderung am Werkzeug. Erster echter Auftrag über `./ce vibe run` (Mistral Medium 3.5, 17 Schritte, 0,23 USD Listenpreis); Patch von Sightline geprüft und im Stil der Datei vereinfacht; Vergleichslauf mit Mistral Large 4 (12 Schritte) lieferte dieselbe Lösung ohne Nacharbeit. [Version](docs/versions/0.29.md).

## 0.28.0 „Gangway“ – unveröffentlicht

- `commit-on-main` entscheidet eine zusammengesetzte Form vorab: `cd <fester Pfad> && git … commit …` (einziges Konstrukt `&&`, eingebautes `cd` mit genau einem Pfad ohne Option/Variable/Muster) – Branch im Zielordner relativ zu `cwd`, nur wenn jede Shell dort landet (Ziel vorhanden, jedes `..` hinter einem Ordner, logischer = physischer Pfad), bloße relative Namen nur ohne `CDPATH`. Alles andere Zusammengesetzte bleibt mit Hinweis beim Git-Netz. Anlass: Mistral Vibe 3.0.4 committet so; Probe 30.10.2026 mit installiertem `~/.vibe/hooks.toml` ließ `cd <repo> && git commit` auf `main` mit Hinweis durch. Gilt für alle vier Clients (nur `guard.py` entscheidet). Erweitert die Repo-Regel „Vorabprüfung begrenzt“ (`AGENTS.md`) um genau diese Form. 4 neue Tests (`test_guard.py` › `CommitOnMainTests`, `test_client_hooks.py` › Gleichstand aller vier Clients, `test_githooks.py` › Laufzeitbeleg mit echter bash und zsh). [Version](docs/versions/0.28.md).

## 0.27.0 „Breakwater“ – unveröffentlicht

- Neu `hooks/vibe_guard.py`: derselbe Wächter (`guard.decide`, `rules/*.md`) als Mistral-Vibe-Hook (`~/.vibe/hooks.toml`, `pre_tool` + `post_tool`, auch für Subagenten). Format an Vibe 3.0.4 gemessen: qualifizierte Werkzeugnamen (`file_system.bash`, `process.start`, `mcp_<server>.<tool>`), Dateipfade relativ zu `cwd`. Sperre als `{"decision": "deny", "reason"}` vor dem Freigabe-Prompt (auch bei `auto-approve`), Warnung als `system_message` und nach dem Lauf als `additional_context` für das Modell; Fehler offen mit Meldung. `./ce vibe run` verweigert ohne eingetragenen Wächter (Nutzerentscheid 30.10.2026: kein `auto-approve` ohne Hook), `./ce vibe status` nennt den Eintrag. Echte Vibe-Läufe: Commit auf `main` und Force-Push gesperrt, harmlose Befehle durch – als Projekt-Hook, als User-Hook und über `./ce vibe run`. 13 neue Tests (`tests/test_client_hooks.py` › `VibeAdapterTests`, Gleichstand, Messfixture; `tests/test_vibe.py`). [Version](docs/versions/0.27.md).

## 0.26.0 „Understudy“ – unveröffentlicht

- Neu `ce_vibe.py` / `./ce vibe run|status`: Claude Code, Codex und Antigravity geben eine abgegrenzte Coding-Aufgabe an Mistral Vibe (headless, `auto-approve`) in einen Wegwerf-Worktree des aktuellen Stands (inkl. eigener ungesicherter und neuer Dateien) und erhalten `changes.patch`, Antwort, Kosten und Abbruchgrund; Worktree wird danach entfernt. Lehnt Repos mit `hold` und private/unbekannte Repos ab (`--allow-private` nur nach Ja des Nutzers); Push zu `origin` und `gh` im Lauf erschwert (Hürden, kein Sandkasten); Grenzen für Runden, Preis, Zeit; Exit 3 bei Vibe-Grenze, kein Retry. `./ce vibe clean [--apply]` verschiebt alte Läufe in den Papierkorb. Anlass: Nutzer 30.10.2026 „Vibe soll als Coding-Assistent für Claude Code und Codex dienen“. 23 neue Tests (`tests/test_vibe.py`), Probelauf mit echtem Vibe. [Version](docs/versions/0.26.md).

## 0.25.0 „Spyglass“ – unveröffentlicht

- `ce_api.py ask --image <PNG/JPEG>` (mehrfach) hängt Bilder vor den Text, für Zählung und Antwort gleich. Aus, solange die Richtlinie (Nexogens `.ce-api.json`) kein `max_images` (1–8) mit `max_image_bytes` (≤ 3,75 MB) setzt; Typ (Kopfbytes), Größe und Kantenlänge (≤ 8000 px) werden vor Anmeldung und Netz geprüft. Textanfragen unverändert. Anlass: Kritiker-Schleife im neuen Nebenprojekt `cubeescape-critic` (Nutzer 27.10.2026: „Nur API wie Gemini“). 6 neue Tests (`tests/test_api.py` › `ImageTests`). [Version](docs/versions/0.25.md).

## 0.24.0 „Nametag“ – unveröffentlicht

- `heavy_lock.py status` (`./ce lock status`) sagt, **wer** eine Sperre hält, für `shared`, `unity`, `blender` und `gpu`: Sitzung mit Modell aus der Reservierung des Checkouts (`<git-dir>/ce-session.json`, ältere als 3 Tage markiert), sonst „no reserved session“ mit Branch; Client aus der Kette der Elternprozesse (Claude Code, Codex, Antigravity, Gemini, Cursor, Terminal); Laufzeit; Befehl des Halters und seine Kindprozesse; darunter die Wartenden mit Telemetrie. `shared` heißt bei einem `gpu`-Lauf „barrier, held exclusively by the gpu run“ statt „unknown“. Alles zur Abfragezeit gelesen: auch Halterzeilen älterer Werkzeugstände werden benannt, Schreiber und Sperrvertrag unverändert; Fehler lassen nur den Teil weg. Anlass: `gpu Blender (PID 65733, …/cubeescape-nexogen)` ohne Hinweis auf die Sitzung. 7 neue Tests. [Version](docs/versions/0.24.md).

## 0.23.0 „Modelline“ – unveröffentlicht

- `say` ohne `--model` findet die Modellzeile jetzt auch, wenn die Reservierung älter als 3 Tage ist (die Sitzungsdatei zählt dann nicht mehr als aktuell, ihr Modell galt bisher nicht mehr), und fällt sonst auf die Modellzeile der eigenen Anmeldung im Postfach zurück (Reservierung eines älteren eingebetteten Werkzeugstands speicherte kein Modell). Ohne Fund bleibt der Hinweis „ohne Modellzeile“. `remember` behält Modell und Auftrag, wenn derselbe Name ohne `--model` erneut geschrieben wird. Anlass: Meldungen mit „Modell und Aufwand: nicht angegeben“ trotz Anmeldung mit Modell (CE 0.115, TG 0.26, TG-Patches). 5 neue Tests (`tests/test_postfach_modelline.py`). [Version](docs/versions/0.23.md).

## 0.22.0 „Tollgate“ – unveröffentlicht

- Neue Regel `merge-nur-sightline` (Entscheidung des Nutzers 23.10.2026, Entscheidung 38): Fachsitzungen mergen nie selbst. Blockiert `gh pr merge` (auch `--auto`), `gh api … PUT …/pulls/N/merge`, die GraphQL-Mutationen `mergePullRequest`/`enablePullRequestAutoMerge` und die GitHub-MCP-Werkzeuge `merge_pull_request`/`enable_pr_auto_merge` mit „Merge macht Sightline nach Prüfung – PR-Nummer und Head-SHA ins Postfach.“; Ausnahme nur die Sitzung Sightline (reservierter Name oder `CE_ROLE=sightline`). `git merge origin/main`, `gh pr view/create` und `--help` bleiben frei. Anlass: Formgen „Ungrafted“ mergte cubeescape-formgen#27 selbst.
- Logik nur in `guard.py` (Feld `merge_attempt`, Ereignis `mcp`/`all`); Claude-Matcher in `hooks.json` und Codex-Adapter (`mcp__…` als Name durchgereicht) um die zwei MCP-Werkzeuge erweitert. Antigravity: nur Shell (MCP-Werkzeugnamen dort nicht gemessen). 7 + 1 neue Tests. [Version](docs/versions/0.22.md).

## 0.21.0 „Courier“ – unveröffentlicht

- Postfach während der Arbeit: neuer PostToolUse-Hook `hooks/postfach_inbox.py` (Codex über `codex_guard.py`) gibt Post an eine reservierte Sitzung zwischen zwei Werkzeugaufrufen als `additionalContext` – bei leerem Postfach zwei Dateilesungen, kein Netz, kein Kontext; höchstens alle 3 min ein abgelöster Lesezugriff. Der Stop-Wächter überspringt Zugestelltes; ohne Nachweis im Transkript kommt es beim nächsten Stopp noch einmal.
- Anmeldung: `--reserve` holt vorher `origin/main` (Branch auf dem frischen Stand, wenn der Worktree nur dahinter liegt; eigene Commits bleiben und werden genannt), merkt `--model`/`--about` für `say`, verwirft im wiederverwendeten Worktree den Wächterstand des Vorgängers; `./ce session reserve --help` geht durch. `say` nimmt eine von Hand geschriebene `Modell…`-Zeile als Modellzeile.
- Zustellung: mehrere Empfänger (`--to A --to B`, `→ A, B:` erreicht beide), PR-/Merge-Meldungen gehen zusätzlich an Sightline (`--no-cc`), `ce_await_mail --also-pr` für den Koordinator, `--follow` als Dauerlauf mit gemerkter letzter ID; nicht lesbares Postfach nennt die Abhilfe (Cloud: cubeescape-nexogen mit auswählen).
- Freigabe per Post (Entscheidung des Nutzers 23.10.2026): `say --freigabe commit,push,pr --zitat …` nur mit Absender Sightline; jeder Wächter setzt das Prüfergebnis vor die Nachricht und lehnt andere Absender und nicht erlaubte Schritte ab. Absenderkennung ist eine Vereinbarung, kein Beweis. 45 neue Tests; die ersten 35 (vor den Richterrunden) als Gegenprobe auf 0.20: 30 rot. [Version](docs/versions/0.21.md).

## 0.20.0 „Lodestar“ – unveröffentlicht

- Lader statt eingebetteter Kopie (Entscheidung des Nutzers 23.10.2026): Ein Repo nennt in `.ce-repo.json` `tools_version` (`{"version", "commit"}`), sein `./ce` ist `ce_loader.py` (von `ce_sync.py` mit Prüfsumme geschrieben). Der Lader nimmt `CE_TOOLS`, sonst das installierte Plugin mit genau diesem Commit (`installed_plugins.json`), sonst den Cache `~/.cache/ce-agent-tools/<version>-<commit12>` (jede Datei gegen das Manifest, schreibgeschützt), sonst lädt er `plugins/ce-shared` aus dem öffentlichen Repo (Tarball, Commit im Pax-Kopf geprüft; Rückfall System-Zertifikate, dann `git fetch`). Aus dem Cache laufen die Werkzeuge (und der `ce_sync`-Import von `tools pin`) mit `PYTHONPYCACHEPREFIX` daneben (Cloud-Probe: root schrieb sonst `__pycache__` in den Cache, jeder Aufruf lud neu). Ohne Netz Exit 3 mit Abhilfe. `./ce tools where|fetch|pin <commit>` (`pin` schreibt den Lader des gepinnten Stands, auch aus einem Repo mit Kopie).
- PR-Check: wiederverwendbarer Schritt `.github/actions/ce-tools` holt denselben Commit nach `$RUNNER_TEMP`, prüft Commit und Version, setzt `CE_TOOLS`. `ce_sync`/`ce_lint` verstehen Repos ohne Kopie (fehlender/editierter Lader und ungültiger Pin = Fehler; älterer Lader, andere Plugin-Version, übrige Kopie, tote `.claude/agents`-Links = Warnung), `ce_nightly` ruft dort `./ce check --nightly`.
- `./ce tidy` ohne `--repos` überspringt Standard-Repos, die es auf dem Rechner nicht gibt (Cloud: Exit 1 wegen fester Mac-Pfade); ausdrücklich genannte fehlende Repos bleiben ein Fehler.
- Erstes Repo: Motiongen 0.13 „Lightfoot“. 28 neue Tests (Lader, auch unter simuliertem root, Tidy, CI-Schritt, Umstellung aus einer Kopie, Sync/Lint ohne Kopie). [Version](docs/versions/0.20.md).

## 0.19.0 „Prismwake“ – unveröffentlicht

- Vorbereitete Plugin-Nacharbeiten: semantische Referenzprüfung für `hold.reticle_socket`, Radius²-Flächenlicht und skalierte Clip-Grenzen; bestehende Vertragsformate bleiben erhalten, ungültige `reticle_socket`-Ziele werden nun abgewiesen. Turnaround-Baselines aus ≤0.18 müssen bei Vergleichen mit 0.19 für Radius ≠ 1 neu erzeugt werden.
- Optionale lokale IDE-Panels für Ressourcen und Bildbelege sowie Blender-Sidebar für Preflight und Turnaround; vorhandene Kerne, eigene Prozessgruppen, begrenzte Ausgabe und Abbruchnachhalten. Gewählte Python-venv bleibt erhalten.
- Mac-Nebenkorrektur: Postfachtest löst den Temp-Pfad wie Git auf (`/var` gegenüber `/private/var`). Client-Proben zu 0.15/0.16 und dritte Rollout-Beobachtung für 0.18 dokumentiert. [Version](docs/versions/0.19.md). Kein Rollout oder Sync mit diesem Abschluss.

## 0.18.0 „Sweepline“ – unveröffentlicht

- `./ce tidy` (`ce_tidy.py`) setzt die Regel „Aufräumen nach dem Merge“ um: Probelauf-Tabelle der Reste fertiger Sitzungen aller Repos (Worktrees, repo-eigene Aufräumbefehle, Generator-Ausgaben, Belegablagen), `--apply` räumt über den Papierkorb bzw. `git worktree remove` ohne `--force`, `--json` für Maschinen. Schutz: gemergt, sauber, ohne Sitzung/Prozess, innerhalb der Wurzeln; nie Haupt-Checkouts und deren `Library`, `_rettung`, „NICHT LÖSCHEN“, Repos mit `hold`; aktuelle und vorige Belegversion bleiben.
- `ce_nightly.py` ruft danach `ce_tidy --nightly`: entfernt nur Kategorien mit `"nightly": true` in `.ce-repo.json › tidy`, der Rest steht als Probelauf im Bericht; ein Fehler macht die Nacht nicht rot. Ohne `tidy`-Eintrag nur Worktrees und Belege.
- Anlass: am Mac 41 → 180 GiB frei nach Handaufräumen. 35 neue Tests, jede Schutzregel mit Gegenbeweis (32 Mutationen, alle erkannt). [Version](docs/versions/0.18.md).

## 0.17.0 „Dashline“ – 2026-10-23

- `./ce session say`: Zeilen mit führendem Bindestrich (`---`, `- Punkt`, `-1 °C`) und unbekannte Optionen nach dem Thema brechen nicht mehr mit Argparse-Fehlern (`unrecognized arguments`) ab. Bekannte Optionen (`--to`, `--name`, `--model`, `--agent`, `--repo`, `--dry-run`, `-h`) bleiben vor und nach den Zeilen erlaubt; `--` trennt wie gewohnt. Höchstens 10 Zeilen. [Version](docs/versions/0.17.md).

## 0.16.0 „Tidemark“ – 2026-10-23

- `./ce plugins check|fix` (`ce_plugins.py`): hält die Claude-Code-Installationen von ce-shared auf einer Version. `check` listet die Einträge aus `installed_plugins.json` (Exit 1 bei Abweichung), `fix` setzt veraltete Einträge (jeder Bereich) auf die neueste Installation mit vorhandenen Dateien und entfernt Einträge verschwundener Worktrees – Probelauf, mit `--apply` Sicherung und atomares Schreiben, `--user-only` lässt nur die Nutzer-Installation.
- Der SessionStart-Hook vergleicht das geladene ce-shared mit der neuesten Version auf dem Rechner (Marketplace-Klon, alle Installationen) und warnt sichtbar (`systemMessage`). Einbinden mit `"autoUpdate": true` am Marketplace-Eintrag.
- Anlass: Nach dem Rollout 0.15.1 standen 6 von 9 Installationen am Mac auf 0.11/0.13, `claude plugin update --scope project` erreichte sie nicht. Nebenbei: die Wächter-Tests der 0.15 hingen an einer festen Uhrzeit, jetzt relativ. 445 Tests. [Version](docs/versions/0.16.md).

## 0.15.1 – unveröffentlicht

- Live-Probe 22.10.2026: Beide Postfach-Hooks blieben still, weil sie den reservierten Branch als ausgecheckt verlangten – `--reserve` legt ihn aber nur an, und eine Cloud-Sitzung arbeitet auf ihrem vorgegebenen Branch. Die Reservierung gilt jetzt für den Worktree (Datei im eigenen Git-Ordner), Ablauf nach 3 Tagen (`reserved_at`, sonst Dateizeit); `say` nimmt den Absender ebenfalls aus der Reservierung. 425 Tests, drei neue Gegenbeweise. [Version](docs/versions/0.15.md#patch-0151).

## 0.15.0 „Wakeline“ – unveröffentlicht

- Der Postfach-Wächter startet von selbst: Ein Stop-Hook (`async`, `asyncRewake`, `timeout` 12 h) wartet für eine reservierte Sitzung im Hintergrund auf Nachrichten im Postfach #16 und weckt sie mit Exit 2 und dem Kommentar. Gelesen wird ab der letzten behandelten Nachricht bzw. der Reservierung; behandelt ist eine Nachricht erst mit Nachweis im Transcript, sonst kommt sie genau einmal erneut (gekennzeichnet).
- Rückfall: Läuft kein Wächter, hält ein zweiter Stop-Hook das Anhalten einmal an und nennt den Startbefehl – höchstens alle 2 h, nie ohne `gh`, nicht nach einem GitHub-Fehler. `./ce session mail` braucht keinen Namen mehr, teilt Pid und Stand (atomar) mit dem Hook, Exit 4 = Wächter läuft schon, Exit 5 = kein Name und keine Reservierung.
- Anlass: Formgen-Patch „holo-familie“ startete den Wächter trotz Prompt nicht, zwei Nachrichten kamen nicht an. Live-Probe am Mac offen (`asyncRewake` am Stop-Hook, Laufdauer, Nachweis im Transcript). [Version](docs/versions/0.15.md).

## 0.14.0 „Crosstalk“ – unveröffentlicht

- Sitzungen melden sich selbst an: `--reserve` (und neu `--patch <kurzname>`) schreibt nach dem Branch-Push `<Name> → Sightline: angemeldet` mit Repo, Branch, Basis, Auftrag und Wächterbefehl ins Postfach #16. Eine fehlgeschlagene Anmeldung hebt die Reservierung nicht auf und gibt den Text zum Einfügen aus.
- Sitzungen schreiben einander mit `./ce session say [--to <Sitzung|Rolle|alle>] <Thema> [Zeilen]` (Absender aus dem Branch, Regelformat, höchstens zehn Zeilen); `./ce session who` listet die angemeldeten Sitzungen. Der Wächter weckt bei `→ alle:` jede Sitzung außer der absendenden.
- Gesendet wird nur aus Repos, deren `origin` eine GitHub-URL ist (lokale Origins wie in Tests bleiben stumm, `CE_MAILBOX=0` schaltet ab). Der Absender ist der beim Reservieren gemerkte Name (`<git-dir>/ce-session.json`), sonst aus dem Branch abgeleitet; Schreibweisen (Bindestrich, Umlaut, Groß/klein) zählen gleich. [Version](docs/versions/0.14.md).

## 0.13.0 „Glasshouse“ – 2026-10-22

- Bestehende Prüfwege erweitert: portable Vorher/Nachher-Ansicht, explizite RGB-Referenzgrenzen, lesende Ressourcen-/Sitzungs-/Quotenübersicht, semantische Referenz-/Zyklusprüfung, strenge Cloud-Auftragsdeklaration, native Prüfprofile sowie Blender-Preflight/Turnaround über bestehende Medienexporte.
- Inkompatible Cloud-CLI ab 0.13: jeder Submit benötigt `--eligibility-file`; ältere Aufrufe müssen Manifest und Prompt-Hash ergänzen.
- Kein zweiter Richter, Logikrunner, Runtime-Sentinel oder Unity-MCP-Server. Quoten ohne frische Quelle bleiben unbekannt, API-Budgets stammen ausschließlich aus dem lokalen Ledger; keine bezahlten Abfragen.
- Gemeinsam mit Agent Tools 0.12 Gatehouse: Client-Hooks und Git-Netz aus dessen freigegebenem main-Stand übernommen; Prüfung und globaler Rollout dieses gemeinsamen Stands. [Version](docs/versions/0.13.md).

## 0.12.0 „Gatehouse“ – 2026-10-22

- Gemeinsamer Wächter für Claude, Codex und Antigravity: dünne Client-Adapter, bestehende Regeln und durchlassende Fehlerbehandlung einschließlich fehlender/nicht ladender Module. Native Proben in Codex CLI 0.162.1, Antigravity CLI 1.4.5/IDE 2.5.5 sowie Claude Code 2.1.301; Antigravity zeigt Warn-/Fehlertexte in `allow.reason` nicht sichtbar an.
- `commit-on-main` liest bei einzelnen einfachen Commit-Aufrufen den tatsächlichen Branch mit festem System-Git und sicherer Umgebung. main/master werden exakt unterschieden und bleiben auch mit gleichnamigen Tags geschützt. Komplexe Shell-Abläufe und Präfix-Zuweisungen gehen mit Hinweis an das Git-Netz; 311 Tests, Quick und unabhängiger Richter grün.
- Reversibles Git-Netz (`pre-commit`/`pre-push`) sperrt main/master und echte Nicht-Fast-Forwards, erhält bestehende Hooks und kennt die Nutzerausnahme `CE_ALLOW_MAIN=1`. `--no-verify`, Merge-Commits und erzwungene Fast-Forwards bleiben dokumentierte Grenzen. [Verhalten, Einbau und Prüfbelege](docs/versions/0.12.md).

## 0.11.1 – 2026-10-22

- Vertragsaudit benennt fehlende Toolgen-Konzeptprüfer vor deren Ausführung; ein älterer oder unvollständiger Checkout bleibt mit null geprüften Eingaben fehlgeschlagen. Regression prüft fehlenden Importer und fehlenden Loader.

## 0.11.0 „Wayfinder“ – 2026-10-22

- Suche akzeptiert `formgen`/`motiongen` und deren GitHub-Namen auch bei historischen Indexnamen. Exakte vorhandene Indexnamen haben Vorrang; sonst bleiben Teilfilter erhalten. CLI und MCP verwenden denselben Suchpfad; Rot/Grün-Regression mit getrennten Repos ([Version](docs/versions/0.11.md)).

## 0.10.0 „Indexwake“ – 2026-10-22

- Inkrementelle Suchupdates entfernen FTS5-Begriffe mit den tatsächlichen alten Inhalten, Überschriften und Pfaden. Leere Löschwerte ließen veraltete Postings zurück und beschädigten die Suchkonsistenz nach Änderungen/Löschungen.
- Zwei Rot/Grün-Regressionen decken Ersetzen, Löschen, Wiederanlegen, unveränderte Nachbardateien und externe FTS-Inhaltsintegrität ab. Acht-Repo-Index frisch wiederhergestellt; kein Lockstep- oder Fachrepo-Eingriff. [Version 0.10](docs/versions/0.10.md).

## 0.9.0 „Watchtower“ – 2026-10-21

- **Hold-Schutz & Entkopplung (`ce_sync.py`, `ce_common.py`):** `ce_sync.py` löst seine Standardziele über `DEFAULT_SYNC_NAMES` unabhängig von `ce_worktrees.DEFAULT_REPOS` auf und verweigert das Schreiben in Repos mit aktivem `"hold"` in `repos.json` (`BLOCKED`).
- **8-Repo-Abdeckung & Codex-Sitzungsschutz (`ce_worktrees.py`, `ce_nightly.py`):** `ce_worktrees.py` prüft alle acht Repos aus `repos.json` und schützt neben Claude-Transcripts auch kürzlich aktive Codex-Rollout-Sitzungen (`~/.codex/sessions`); `ce_nightly.py --all` indiziert für `ce_search` alle acht Repos einschließlich `nexogen` und `ce-agent-tools`.
- **Reproduzierbare Testumgebung, Review-Größenwarnung & Cache-Hinweis (`ce_check.py`, `ce_judge.py`, `ce_report.py`):** `ce_check.py` nutzt für nackte `python3`-Aufrufe automatisch `.venv` bzw. `~/.local/share/ce-agent-tools/venv` (`requirements-dev.txt`); `ce_judge.py` gibt ab 1.500 Zeilen / 100 KB eine Größenwarnung mit `--files`-Aufteilungshinweis aus, ohne JSON-/Fixture-Diffs abzuschneiden; `ce_report.hints()` formuliert den Cache-Write-Hinweis nicht-spekulativ. [Version 0.9](docs/versions/0.9.md).

## 0.8.0 „Tokenharbor“ – 2026-10-21

- `./ce api login|status|budget|ask`: lokaler Console-Browser-Login im isolierten Nexogen-Profil; explizite Textanfragen ohne Werkzeuge, automatische Wiederholungen oder Zugangsdaten im Repo.
- Vorabzählung und atomare Kostenreservierung begrenzen Anfrage und UTC-Kalendermonat. Unbekannte Ergebnisse bleiben reserviert; tatsächliche Kosten werden aus vollständiger Usage geschätzt, auch bei Überschreitungen. Parallele Aufrufe teilen denselben lokalen Ledger.
- Stabiles Systempräfix nutzt 5-Minuten-Caching; kalte Cache-Schreibkosten bestimmen die vorsichtige Reservation. Preisstand läuft nach 30 Tagen ab; Konfiguration und Bedienablauf in Nexogen, technische Grenzen in [Version 0.8](docs/versions/0.8.md).

## 0.7.1 – 2026-10-21

- `ce_report.hints()` warnt bei hohem Spitzen-Input (> 150.000 Tokens je Anfrage, auch unter 300 Nachrichten) sowie bei mehrfachem Cache-Neuaufbau (`cache_creation_input_tokens` > 2× Spitzen-Input nach >5-Minuten-Pausen); Tests in `tests/test_report_usage.py` ergänzt.

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
