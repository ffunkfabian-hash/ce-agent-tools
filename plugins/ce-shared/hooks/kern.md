# Der harte Kern – 10 Regeln für alle Agenten in allen Repos

Quelle: Nexogen `templates/kern.md` (seit 1.25 „Bedrock“, Grundlage Prüfung 1.24 „Trimline“, Entscheidung 29 und 7). Nur hier ändern;
`CLAUDE.md` trägt denselben Wortlaut (`check.py` vergleicht), Plugin und Fachrepos verteilen von hier. Das Maß ist der Schaden bei
einem Verstoß, nicht die Trefferzahl.

1. Kein Commit direkt auf `main`.
2. Kein Force-Push, keine überschriebenen Branches oder Tags.
3. Nichts Fremdes anfassen oder löschen: andere Sitzungen, Worktrees, Stash, Haupt-Checkout.
4. Merge nach `main` nur mit Ja des Nutzers, außer Patch bis 100 Zeilen ohne Format, Vertrag, Kernpfad bei grünem `merge-check`; Tag, Release, Repo öffentlich machen immer nur mit Ja.
5. Tests nie löschen oder abschwächen, um grün zu werden.
6. Keine Zugangsdaten, kein fremdes Material, keine großen Binärdaten nach außen (öffentliche Repos, fremde APIs).
7. Formate, Verträge und IDs nur mit Migration; IDs sind ewig.
8. Nichts als fertig melden, was nicht geprüft ist.
9. Cube Escape: Unity nie auf einem Checkout, nur in der Testkopie; Szenen und Prefabs nur über Builder.
10. Cube Escape: Spielerlevel sind nie Szenen, nur validierte Daten; Testläufe in der Sandbox.

Regeln 1–8 gelten in jedem Repo, 9 und 10 nur in Cube Escape. Wer sie heute durchsetzt (Hooks, Git-Netz, Richter, Tests), steht im
Inventar der Prüfung 1.24 (`docs/versions/1.md`), nicht hier.
