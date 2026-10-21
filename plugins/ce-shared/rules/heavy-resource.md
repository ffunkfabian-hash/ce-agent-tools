---
name: heavy-resource
enabled: true
event: bash
action: block
conditions:
  - field: command
    operator: regex_match
    pattern: \bheavy_lock\.py\s+run\b
  - field: command
    operator: not_contains
    pattern: --resource
---

**`heavy_lock.py run` ohne `--resource unity|blender|gpu`** landet auf der alten gemeinsamen Sperre und wartet hinter allem
(ce-agent-tools 0.5.0, globale Regel › Schwere Läufe und Sperren). Ressource angeben: Unity-Regression/Messung `unity`,
Blender-Stufen/Bakes `blender`, Metal-Renderings `gpu`.
