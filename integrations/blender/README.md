# Blender-Seitenleiste

Optionale lokale Oberfläche für **dieselben** `ce_blender_preflight.py`- und `ce_turnaround.py`-Kerne wie `./ce evidence`.
Kein eigener Rigvertrag, Renderer oder Profilformat. Die Oberfläche liegt unter **3D-Ansicht → N → CE Tools**.
Blender 4.2 oder neuer, externer Python-3-Interpreter und das bestehende Werkzeug-Checkout sind erforderlich.

## Lokal laden

Im eigenen Blender-Testprozess das Verzeichnis `integrations/blender` zum Python-Suchpfad hinzufügen und
`import ce_blender_tools; ce_blender_tools.register()` ausführen. Für eine ausdrücklich gewünschte dauerhafte Installation
kann der Ordner `ce_blender_tools` als ZIP gepackt und über Blenders Add-on-Installation geladen werden.
Die Änderung installiert nichts automatisch und speichert keine Blender-Einstellungen.

Unter **Lokale Werkzeuge** den Ordner `plugins/ce-shared/tools`, einen **externen Python-3-Interpreter**
(nicht Blenders ausführbare Datei) und eine externe Ausgabeablage wählen. Bei verschobenem/installiertem Add-on den
Werkzeugpfad neu setzen. Rig-/Preflight-Profil und Turnaround-Profil werden ausdrücklich getrennt ausgewählt.
**Profil prüfen** nutzt jeweils den vorhandenen Profilvalidator; **Datei prüfen** startet den bestehenden Preflight;
**Vorschau rendern** startet Turnaround und den vorhandenen Medienexport. MP4 ist ausdrücklich zuschaltbar.

**Aktuelle gespeicherte Datei** ist nur bei gespeicherter, unveränderter Szene zulässig. Bei neuer oder geänderter Szene
selbst speichern oder ausdrücklich **Datei vom Datenträger** auswählen; dieser Modus prüft den gespeicherten Stand,
auch wenn im geöffneten Blender andere Änderungen vorliegen. Die Oberfläche verändert/speichert die Benutzerszene nie.
Eigene Headless-Prozesse öffnen `.blend` wie die CLI mit `--disable-autoexec`.

Jeder Lauf erhält einen neuen Ausgabeordner mit `request.json`, `process.log`, `result.json` und gegebenenfalls
`preflight.json` beziehungsweise `preview/`. Bestehende Ausgaben werden nicht wiederverwendet. CLI-Pfadregeln
(externe Ausgabe, außerhalb Assetbaum/Repo) bleiben wirksam. Das Panel zeigt Status und bis zu zwölf Befunde;
**Ausgabeordner öffnen** führt zum vollständigen JSON-Bericht/Protokoll. Die Anzeige wird per Blender-Timer aktualisiert,
CLI und Blender laufen in einer separaten Prozessgruppe ohne Shell. **Abbrechen** beziehungsweise Abmelden des Add-ons
fordert auf macOS/Linux das Ende des Auftrags an. Der bestehende `heavy_lock`-Supervisor hält die Ressource bis zum Ende seiner ganzen Workergruppe. Nach fünf Sekunden wird ausschließlich die mit frischer Laufkennung und Supervisor-ID belegte Workergruppe beendet; der Supervisor wird danach abgeholt. Bis dahin bleiben Auftrag und Protokoll offen, neue Starts gesperrt; Teilausgaben bleiben als solche erkennbar erhalten.
Wartende Assetläufe nutzen die vorhandene `blender`-Ressourcensperre. Der persistente Überwachungstimer bleibt bei Dateiwechseln aktiv und betreut das Aufräumen auch nach Abmeldung des Add-ons.

## Größenunabhängiges Vorschau-Licht

Die vorhandene Bounds-Kamera bleibt orthografisch. Lichtposition und Scheibengröße skalieren wie bisher mit dem
Bounds-Radius; die Leistung skaliert nun quadratisch: **1400 W bei Radius 1 Blender-Einheit**.
Damit bleibt die Bestrahlungsstärke beim gleichförmigen Skalieren geometrisch gleicher Assets konstant.
Kameranahgrenze und Ferngrenze skalieren ebenfalls mit dem Radius; die frühere Radius-Untergrenze von 0,1 entfällt
(bis auf einen Degenerationsschutz von 0,000001). Vorhandene zusätzliche Szenenlichter/Materialien bleiben Teil der
Eingabedatei und können weiter das Ergebnis beeinflussen.

## Prüfung

Ohne Blender: `python3 -m unittest discover -s tests -p 'test_blender_addon*'` und `test_turnaround.py`.
Echter Smoke ausschließlich in eigenem Headless-Blender mit Ressourcensperre, neuer externer Ausgabe:

```sh
python3 plugins/ce-shared/tools/heavy_lock.py run --resource blender --what "Blender Add-on Smoke" -- \
  /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup --python-exit-code 1 \
  --python integrations/blender/tests/smoke.py -- --repo "$PWD" \
  --out /tmp/ce-blender-addon-smoke-new --python /Pfad/zu/python3
```

Der Smoke registriert echte RNA-Klassen, startet eine Profilprüfung über den UI-Operator/Worker, meldet erneut an/ab
und erzeugt zwei eigene `.blend`-Fixtures (Größenfaktor 1000). Beide laufen durch den vorhandenen Renderer und
Preflight; unveränderte Quelldateihashes, Bilder und Helligkeitsvergleich stehen in `smoke.json`.
Die Helligkeitsabweichung muss kleiner als 3 % sein. Eine echte GUI-Probe des geöffneten N-Panels bleibt zusätzlich
nötig: sichtbare Felder/Buttons, Dateiwahl, Status während eines Auftrags und Berichtsausgabe ansehen.
