---
name: unity-nie-auf-checkout
enabled: true
event: bash
action: block
conditions:
  - field: command
    operator: regex_match
    pattern: (-projectPath|--project-path|\bunity\s+(open|run|build))\s+["']?(\S*[/\\]Claude[/\\]Cube( |\\ )Escape|\S*\.codex[/\\]worktrees)
---

**Unity nie auf einem Cube-Escape-Checkout starten oder steuern** (Cube-Escape-AGENTS.md › Arbeitsumgebung). Der Nutzer
hat den Editor meist auf dem Haupt-Checkout offen; Worktrees sind ebenfalls Checkouts. Unity läuft nur in Testkopien
unter `~/Library/Caches/CubeEscape/verify/` (Windows: `%LOCALAPPDATA%\CubeEscape\verify\`): `verify.sh run` /
`verify.sh test …` (Skill `ce-testing`), ein ruhender Editor für Claude nur über `unity_mcp.sh start` (eigene MCP-Kopie).
