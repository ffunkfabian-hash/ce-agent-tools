---
name: output-cap
enabled: true
event: bash
action: warn
conditions:
  - field: command
    operator: regex_match
    pattern: (^|[;&|]\s*)(cat|python3?\s+-m\s+json\.tool|jq\s+\.)\s+[^|;&]*\.(json|log|jsonl|csv)\b(?![^|]*\|\s*(head|tail|grep|wc|jq|cut|sed|awk|python))
---

**Ganze JSON-/Log-Dateien ausgeben kostet Kontingent** (globale Regel › Kontingent sparen): `head`/`tail`/`jq '.feld'`/`grep` davor,
oder in Python nur die gebrauchten Felder drucken. Testausgaben mit `| tail -40`.
