# CE Prism als natives IDE-Panel

Diese kleine lokale Extension öffnet die vorhandene Werkzeugübersicht und den Bildvergleich als native
VSCode-Webview-Panels. Sie verwendet dieselben Python-Werkzeuge wie Browser und CLI: `ce_console.py snapshot --out`
und `ce_imagediff.py --viewer --json`. Regeln, Grenzwerte, Bildmessung und Darstellung bleiben dort.
Keine npm-Abhängigkeiten, kein Server, keine Cloudabfrage und kein automatischer Refresh.

## Start aus dem Quellbaum

```sh
code --new-window --extensionDevelopmentPath="$(pwd)/integrations/vscode" /Pfad/zum/Arbeitsrepo
```

In der Befehlspalette:

- **CE Prism: Ressourcen, Sitzungen und Kontingent** (`cePrism.dashboard`): Locks, erfasste Wartende,
  Sitzungsindizien und lokale Kontingent-/Budgetstände. Der erneute Befehl aktualisiert dasselbe Panel.
- **CE Prism: Bilder vergleichen** (`cePrism.evidence`): Vorher- und Nachher-Bild einzeln wählen, danach optional
  ein bestehendes Referenzprofil (JSON), einen MP4-Prüfclip oder beides. Das Panel zeigt den tastaturbedienbaren
  Schieber, die Heatmap, Originalmesswerte und gegebenenfalls den Clip. Exit 1 bedeutet gemessene Abweichung;
  der Vergleich wird trotzdem angezeigt. Fehler der vorhandenen Validatoren erscheinen als IDE-Meldung.

Der Dashboard-Aufruf übergibt alle lokalen Workspace-Ordner an `--repos`; ohne geöffneten Ordner gilt die bestehende
Repo-Auswahl des Backends. `--mail` wird nie gesetzt. Das Kontingent ist der lokale Rollout-Stand mit vorhandener
Kennzeichnung für unbekannte/veraltete Daten. Sitzungsindizien sind kein Aktivitätsbeweis und erfasste Wartende keine
vollständige oder faire Warteschlange. Die Übersicht bleibt lesend.

## Lokale VSIX-Datei

```sh
python3 integrations/vscode/package_vsix.py --out /tmp/ce-prism-0.19.0.vsix
code --install-extension /tmp/ce-prism-0.19.0.vsix
```

Das Bauskript verwendet nur Pythons ZIP-/XML-Standardbibliothek und verändert keine Installation. Der zweite Befehl
installiert ausdrücklich die lokale Extension; alternativ in der IDE **Extensions: Install from VSIX** verwenden.
Es gibt keine Veröffentlichung in einem Marketplace. Im installierten Paket fehlt der externe Python-Backendbaum:
vor dem ersten Aufruf in den **Benutzereinstellungen** `cePrism.toolsPath` auf den absoluten Ordner
`/Pfad/zu/ce-agent-tools/plugins/ce-shared/tools` setzen. Beim Entwicklungsstart wird dieser Pfad aus dem Quellbaum
ermittelt. `cePrism.pythonPath` ist standardmäßig `python3`; bei Bedarf einen lokalen Interpreterpfad eintragen,
der Pillow und NumPy enthält. Programm und Pfade ohne zusätzliche Shell-Argumente angeben.
Beide Einstellungen haben Maschinengültigkeit und werden nicht aus Workspace-Einstellungen übernommen.

## Sicherheitsgrenzen und Kompatibilität

Backend-Code wird erst in einem vertrauenswürdigen lokalen Workspace ausgeführt, vor dem Start nochmals geprüft.
Remote-/virtuelle Workspaces werden abgelehnt. Nur vertrauenswürdigen lokalen Backend-Code konfigurieren: Workspace
Trust sandboxiert Python nicht. Prozesse verwenden feste Skriptnamen und Argumentlisten ohne Shell, mit 60 Sekunden
Timeout und begrenzter Ausgabepuffergröße. Unter macOS/Linux läuft jeder Aufruf in einer eigenen Prozessgruppe:
beim Timeout wird diese mit SIGTERM beendet, nach einer Sekunde mit SIGKILL eskaliert, auch wenn der Elternprozess
vorher endet. Danach wird höchstens eine weitere Sekunde auf das Schließen des Aufrufs gewartet. Unter Windows gilt
der Abbruch nur für den direkten Backend-Prozess; ohne zusätzliche Betriebssystemintegration ist die Beendigung
seiner Nachkommen nicht zugesichert. Absichtlich in neue Sitzungen ausweichende Prozesse sind auch unter POSIX nicht
erfasst; der konfigurierte Backend-Code bleibt eine Vertrauensgrenze. Die vorhandenen Werkzeuge dürfen ihre bestehenden lesenden Unterprozesse
ausführen; die Extension startet keine Renderer, Blender, Unity, API-Aufträge oder Git-Mutationen.

Die Panels laden ausschließlich vom Backend erzeugtes HTML, nie frei ausgewählte HTML-Dateien. Temporäre Ausgaben
liegen in einem privaten System-Temp-Ordner und werden nach dem Einlesen auch im Fehlerfall entfernt. Webviews erlauben
keine lokalen Ressourcen, Command-URIs, Netzverbindungen oder Frames. Eine CSP mit zufälligen Nonces erlaubt nur
die Backend-Skripte/-Styles und eingebettete Bild-/Videodaten. Es gibt keine Webview-Message-Bridge; gesendete Nachrichten
lösen keine Host-Aktion aus. Die Extension ersetzt keine Backend-Validierung und keine visuelle Abnahme.

Basis ist die stabile VSCode-Extension-API ab 1.85. VSCode-basierte Antigravity-IDE-Versionen können das VSIX nutzen,
wenn sie diese APIs/Engine unterstützen. Eine echte Probe in der jeweiligen IDE bleibt erforderlich; das ist keine
Integration in die eigenständige Antigravity-App. Claude Code innerhalb derselben IDE kann neben den Panels laufen;
eine Claude-Desktop-Panel-API wird damit nicht behauptet.

Die Umsetzung folgt den offiziellen Leitfäden zu [Webviews und CSP](https://code.visualstudio.com/api/extension-guides/webview)
und [Workspace Trust](https://code.visualstudio.com/api/extension-guides/workspace-trust).

## Prüfung

```sh
node --test tests/test_ide_extension.js
python3 -m unittest discover -s tests -p 'test_ide_extension.py'
```

Die Node-Tests prüfen Argumenttransport/Exitcodes, Aufräumen und CSP sowie unter POSIX mit echten Prozessen den
Timeout eines Elternprozesses mit SIGTERM ignorierendem Kind und das anschließende Aufräumen. Ein kleiner VSCode-Hostmock prüft Trust,
Remote-Ablehnung, Dialogabbruch und Panel-Lebenszyklus. Sie sind keine native IDE-Probe und ersetzen keine echte
CLI-/Backend-Probe. Der Python-Wrapper verwendet `node` im PATH oder den gebündelten lokalen Codex-Runtime-Node;
wenn beides fehlt, meldet er den fehlenden Node explizit als übersprungenen Test.

Native Probe: Entwicklungsfenster starten, Workspace vertrauen, Dashboard öffnen und erneut aufrufen; zwei bekannte
Testbilder vergleichen, Schieber per Tastatur bewegen und optional MP4 abspielen; in einem nicht vertrauenswürdigen
Fenster muss der Befehl ohne Backend-Aufruf eine Trust-Meldung zeigen. Die dabei sichtbaren Quellen/Standzeiten sowie
die Grenzen lokaler Kontingentdaten prüfen. Kein Test benötigt eine Cloudabfrage.
