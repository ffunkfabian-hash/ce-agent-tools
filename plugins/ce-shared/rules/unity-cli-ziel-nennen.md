---
name: unity-cli-ziel-nennen
enabled: true
event: bash
action: block
conditions:
  - field: command
    operator: regex_match
    pattern: \bunity\s+(mcp|command|cmd|request|list|job|eval|pipeline\s+(install|upgrade))\b
  - field: command
    operator: not_contains
    pattern: --project-path
---

**Unity-CLI nur mit ausdrücklichem Ziel:** Ohne `--project-path` sucht `unity` den Editor über den aktuellen Ordner oder
nimmt irgendeinen laufenden – das kann ein Checkout oder der offene Editor des Nutzers sein. Immer
`--project-path <Testkopie>` angeben; in Cube Escape `$(.agents/skills/ce-testing/scripts/unity_mcp.sh path)` oder gleich
`unity_mcp.sh` (start, sync, status, stop). Das Pipeline-Paket kommt nur in die MCP-Kopie, nie ins Spielprojekt.
