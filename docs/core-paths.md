# `core_paths` – Vorschläge je Repo (0.34 „Gatekeeper“)

`.ce-repo.json › core_paths` listet Format-, Vertrags- und Kernpfade als Glob-Muster. `./ce merge-check` liest die Liste vom Basisbranch, also kann ein PR seine eigenen Dateien nicht herausnehmen. Berührt ein PR einen Pfad, ist er eine Version. Ohne das Feld warnt `merge-check` und stuft nur nach Größe ein.

Muster: `*` bleibt in einem Ordner, `**` geht durch Ordner, ein `/` am Ende meint alles darunter. Lieber eng: Ein Fehltreffer macht einen kleinen Patch zur Version. Versionsdateien gehören nicht hinein, sie steigen bei jedem Patch.

Stand: GitHub `main` am 04.11.2026, jedes Muster trifft dort mindestens eine Datei (geprüft mit `ce_merge_check.glob_regex`). In ce-agent-tools ist die Liste gesetzt. In den Fachrepos sind es nur Vorschläge, das Eintragen ist je Repo ein Patch.

## Listen

### cubeescape

```json
["Assets/Scripts/Core/SaveData.cs", "Assets/Scripts/Core/SaveSystem.cs", "Assets/Scripts/Core/PersistentId.cs",
 "Assets/Scripts/Levels/LevelFormat.cs", "Assets/Scripts/Levels/LevelMigrator.cs", "Assets/Scripts/Levels/LevelSerializer.cs",
 "Assets/Scripts/Items/ToolBelt.cs", "Assets/Scripts/Player/PlayerController.cs", "Assets/Scripts/Player/CameraController.cs",
 "Assets/Scripts/Portals/**/*.cs", "Assets/Scripts/Cubegen/CubegenContract.cs", "Assets/Resources/Cubegen/cubegen_contract.json",
 "Assets/Art/ToolgenShared/EgoArms/unity_contract_arms.json"]
```

Die 41 Verträge `Assets/Art/Toolgen/*/unity_contract*.json` sind importierte Kopien und fehlen bewusst, sonst wäre jeder Waffen-Import eine Version.

### cubeescape-toolgen

```json
["toolgen/tool_contract.py", "toolgen/game_export.py", "toolgen/game_export_rig.py", "toolgen/ego_rig.py", "toolgen/ego_arms.py",
 "data/ego_arms.json", "data/ego_clips.json"]
```

### cubeescape-cubegen

```json
["cubegen/contract.json", "cubegen/contract.py", "cubegen/migrate.py", "cubegen/pipeline.py"]
```

### cubeescape-robogen

```json
["scripts/robogen/export.py", "scripts/robogen/rig.py", "scripts/robogen/clips.py", "scripts/robogen/pipeline.py"]
```

### cubeescape-formgen

```json
["claude/docs/*.schema.json", "claude/generator/export_spec.py", "claude/generator/export_geo.py", "claude/generator/export_sight.py",
 "codex/**/*.schema.json"]
```

### cubeescape-formgenv2

```json
["docs/schemas/*.schema.json", "formgen/wspec.py", "formgen/geo.py"]
```

### cubeescape-ballon

```json
["packages/ego-arms/manifest.json", "packages/ego-arms/arms.fbx", "packages/ego-arms/arms.glb", "basic/arms/robot_arm.py",
 "basic/arms/ego.py", "tools/export_arms.py"]
```

### cubeescape-critic

```json
["critic/schema.json", "web/src/robotgen/data/SOURCE.json", "assets/robot-bodies-three/*/manifest.json"]
```

### cubeescape-nexogen

```json
["repos.json", ".ce-api.json"]
```

### cubeescape-motiongen

Kein Vorschlag: Das Repo hat nur Messdaten und Doku. `[]` setzen, dann entfällt die Warnung.

## Stolpersteine für `merge-check`

- **Cube Escape:** `docs.changelog` zeigt auf `Docs/ROADMAP.md`, im Wurzelordner liegt aber ein `CHANGELOG.md`. Dessen Kopf nennt 0.115.3, `GameVersion.cs` 0.115.2. Bleibt das so, wird `merge-check` dort rot. Vorher klären, welche Datei gilt.
- **BALLON, Motiongen, Critic:** Die Version ist ein Literal in `.ce-repo.json` und in zwei Repos schon veraltet. `merge-check` warnt dann nur. Besser `{"file": "CHANGELOG.md", "pattern": "(?m)^## (\\d+\\.\\d+\\.\\d+)"}`.
- **Formgen v2:** kein `docs.changelog`, obwohl `CHANGELOG.md` existiert. BALLON, Motiongen und Critic brauchen den Eintrag ebenso.
- **Formgen:** Die Version kennt nur die Linie `claude-…`. Ein PR nur in `codex/` wird als „Version nicht erhöht“ rot.
