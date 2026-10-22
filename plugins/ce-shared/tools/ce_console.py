#!/usr/bin/env python3
"""Read-only workflow dashboard and portable evidence viewer; reuse the existing CLI and validators.

ce_console.py snapshot [--repos PATH ...] [--mail] [--out dashboard.html]
ce_console.py serve [--port 8768] [--repos PATH ...] [--mail]
The server binds loopback only, has no write routes, and never serves arbitrary files.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
from decimal import Decimal
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import subprocess
import sys
import threading

import ce_common
import ce_lock_status
import ce_worktrees
import heavy_lock

HERE = Path(__file__).resolve().parent


def run_json(argv, timeout=15):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if done.returncode:
        raise ValueError('Quelle nicht verfügbar (Exit ' + str(done.returncode) + ')')
    return json.loads(done.stdout)


def quota(now=None, max_age=900):
    now = now or datetime.now(timezone.utc)
    result = {'source': 'Lokaler Codex-Rollout; keine Serverabfrage', 'known': False}
    try:
        raw = run_json([sys.executable, str(HERE/'ce_codex_quota.py'), '--json'])
        stamp = datetime.fromisoformat(raw['as_of'].replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('Stand ohne Zeitzone')
        age = (now-stamp).total_seconds()
        if age < -60:
            raise ValueError('Stand liegt in der Zukunft')
        windows = {}
        for name, item in raw.items():
            if name in ('plan', 'as_of'):
                continue
            used = item.get('used')
            if type(used) not in (float, int) or not math.isfinite(used) or not 0 <= used <= 100:
                raise ValueError('Ungültiger Kontingentwert')
            windows[name] = {'used_percent': used, 'remaining_percent': round(100-used, 1), 'reset': item.get('resets')}
        if not windows:
            raise ValueError('Keine verfügbaren Zeitfenster')
        result.update(known=True, as_of=stamp.isoformat(), age_seconds=max(0, round(age)),
                      stale=age > max_age, windows=windows)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.TimeoutExpired):
        result['reason'] = 'Kein gültiger lokaler Kontingentstand verfügbar'
    return result


def api_budget(now=None):
    """Use the existing strict policy/ledger reader, without its mutating snapshot transaction or OAuth."""
    import ce_api
    from ce_api_budget import BudgetLedger, BudgetError
    result = {'source': 'Lokales Anthropic-API-Ledger; kein Rechnungsabgleich', 'known': False}
    try:
        if not ce_api.LEDGER.is_file() or ce_api.LEDGER.is_symlink():
            raise ValueError('Kein vorhandenes Ledger')
        policy = ce_api.load_policy(ce_api.DEFAULT_POLICY, require_current=False)
        ledger = BudgetLedger(ce_api.LEDGER, policy['monthly_usd'], policy['request_usd'], clock=lambda: now or datetime.now(timezone.utc))
        data = ledger._load(ledger._now_period())
        reserved = sum((Decimal(item['amount']) for item in data['reservations'].values()), Decimal(0))
        result.update(known=True, period=data['period'], spent_usd=data['spent'], reserved_usd=str(reserved),
                      monthly_limit_usd=str(policy['monthly_usd']),
                      observed_at=(now or datetime.now(timezone.utc)).isoformat())
    except (OSError, ValueError, TypeError, KeyError, BudgetError, ce_api.ApiError):
        result['reason'] = 'Kein gültiger lokaler Budgetstand verfügbar'
    return result


def snapshot(repos=None, mail=False):
    now = datetime.now(timezone.utc)
    roots = repos if repos is not None else ce_worktrees.default_repos()
    allowed, held = [], []
    for root in roots:
        root = Path(root).resolve()
        reason = ce_common.repo_hold(root)
        if reason:
            held.append({'path': str(root), 'reason': reason})
        else:
            allowed.append(root)
    collected = ce_worktrees.collect(allowed, Path.home()/'.claude/projects')
    worktrees = [{'repo': str(repo), 'worktrees': states} for repo, states in collected]
    result = dict(schema_version=1, observed_at=now.isoformat(), locks={
        resource or 'shared': heavy_lock.holder(resource) for resource in (None,)+heavy_lock.RESOURCES},
        tracked_waiters=ce_lock_status.waiting(heavy_lock.lock_path()), worktrees=worktrees, excluded=held,
        codex=quota(now), api=api_budget(now),
        queue_note='Nur Wartende mit Telemetrie dieses Werkzeugstands; kein Fairness- oder Vollständigkeitsversprechen.',
        session_note='Prozesse und jüngste Sitzungen sind Indizien; ein Branch allein bedeutet nicht aktiv.')
    result['mailbox'] = {'known': False, 'reason': 'Onlineabfrage ausgeschaltet; --mail liest nur Postfach #16.'}
    if mail:
        try:
            issue = run_json(['gh', 'api', 'repos/ffunkfabian-hash/cubeescape-nexogen/issues/16'])
            result['mailbox'] = dict(known=True, url=issue['html_url'], title=issue['title'],
                                     comments=issue['comments'], updated_at=issue['updated_at'])
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            result['mailbox']['reason'] = 'Postfachabfrage fehlgeschlagen'
    return result


STYLE = '''body{font:16px system-ui;background:#101923;color:#e4edf4;max-width:1100px;margin:30px auto;padding:0 20px}section{background:#1b2a38;padding:20px;border-radius:12px;margin:18px 0}h1{font-size:30px}h2{font-size:21px}p{line-height:1.55}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px ui-monospace}button,a{color:#8de6ef}button{background:#234257;border:1px solid #83acbf;border-radius:5px;padding:10px;cursor:pointer}img,video{max-width:100%}.compare{position:relative;max-width:min(760px,60vh)}.compare img{display:block;width:100%}.compare .after{position:absolute;inset:0;clip-path:inset(0 50% 0 0)}input{width:760px;max-width:100%}details{margin:10px 0}.muted{color:#b9cbd7}.metric{font-size:26px;font-weight:650;margin:8px 0}.table-scroll{overflow-x:auto}td{overflow-wrap:anywhere;min-width:95px}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:7px;border-bottom:1px solid #4c6071;vertical-align:top}@media(max-width:600px){body{padding:0 10px}pre{font-size:11px}}'''


def document(title, body, script=''):
    return '<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>'+html.escape(title)+'</title><style>'+STYLE+'</style>'+body+'<script>'+script+'</script></html>'


def dashboard(data, live=False):
    # Untrusted names, paths and remote titles enter the DOM only through textContent.
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).replace('<', '\\u003c').replace('&', '\\u0026')
    body = '<h1>Cube Escape Werkzeuge</h1><p id="stamp"></p>' + ('<button id="refresh">Aktualisieren</button>' if live else '<p class="muted">Gespeicherte Momentaufnahme</p>')
    for key, title in [('locks','Ressourcen und erfasste Wartende'),('codex','Codex Kontingent'),('api','Anthropic API Budget'),('worktrees','Sitzungen und Arbeitsstände'),('mailbox','Postfach')]:
        body += '<section><h2>'+title+'</h2><div id="'+key+'"></div></section>'
    script = 'let state='+encoded+';' + "function el(tag,text){const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);return n}\nfunction paragraph(parent,text){parent.appendChild(el('p',text))}\nfunction table(parent,headers,rows){const box=el('div');box.className='table-scroll';const t=el('table');const head=el('tr');for(const h of headers)head.appendChild(el('th',h));t.appendChild(head);for(const values of rows){const row=el('tr');for(const value of values)row.appendChild(el('td',value));t.appendChild(row)}box.appendChild(t);parent.appendChild(box)}\nfunction render(d){\n document.querySelector('#stamp').textContent='Abfrage: '+new Date(d.observed_at).toLocaleString('de-DE',{timeZone:'Europe/Berlin'});\n for(const k of ['locks','worktrees','codex','api','mailbox'])document.getElementById(k).replaceChildren();\n const locks=document.querySelector('#locks');\n table(locks,['Ressource','Halter','Erfasste Wartende'],Object.entries(d.locks).map(([k,v])=>[k,v||'Frei',d.tracked_waiters.filter(w=>w.resource===k).map(w=>w.what+' · PID '+w.pid).join('\\n')||'Keine erfasst']));\n paragraph(locks,d.queue_note);\n const codex=document.querySelector('#codex');\n if(d.codex.known){for(const [name,w] of Object.entries(d.codex.windows)){const metric=el('p',w.remaining_percent+' % frei · '+name);metric.className='metric';codex.appendChild(metric);paragraph(codex,'Verbraucht: '+w.used_percent+' % · Reset: '+(w.reset||'unbekannt'))}paragraph(codex,(d.codex.stale?'VERALTET · ':'')+'Stand: '+d.codex.as_of)}else paragraph(codex,'Unbekannt · '+d.codex.reason);\n paragraph(codex,d.codex.source);\n const api=document.querySelector('#api');if(d.api.known){const metric=el('p',d.api.spent_usd+' USD / '+d.api.monthly_limit_usd+' USD');metric.className='metric';api.appendChild(metric);paragraph(api,'Verbraucht / Monatslimit · '+d.api.period+' · reserviert: '+d.api.reserved_usd+' USD')}else paragraph(api,'Unbekannt · '+d.api.reason);paragraph(api,d.api.source);\n const work=document.querySelector('#worktrees');let rows=[];for(const repo of d.worktrees){for(const w of repo.worktrees){const state=w.processes.length?'Prozess läuft':w.session&&w.session.age_minutes<30?'Kürzlich aktiv':'Keine aktuelle Aktivität erfasst';rows.push([w.branch||'(detached)',state,w.dirty+' offen · '+w.ahead+' voraus / '+w.behind+' zurück',w.path])}}\n table(work,['Branch','Sitzungsindiz','Git','Arbeitskopie'],rows);paragraph(work,d.session_note);for(const x of d.excluded)paragraph(work,'Ausgeschlossen: '+x.path+' · '+x.reason);\n const mail=document.querySelector('#mailbox');if(d.mailbox.known){paragraph(mail,d.mailbox.title);paragraph(mail,d.mailbox.comments+' Kommentare · aktualisiert '+d.mailbox.updated_at)}else paragraph(mail,d.mailbox.reason);\n}\nrender(state);\n"
    if live:
        script += '''document.querySelector('#refresh').onclick=async()=>{const b=document.querySelector('#refresh');b.disabled=true;try{const r=await fetch('/api/state');if(!r.ok)throw Error('Quelle nicht erreichbar');state=await r.json();render(state)}catch(e){document.querySelector('#stamp').textContent='Aktualisierung fehlgeschlagen: '+e.message}finally{b.disabled=false}};'''
    return document('Cube Escape Werkzeugübersicht', body, script)


def image_data(path):
    from PIL import Image
    import io
    with Image.open(path) as source:
        image = source.convert('RGB')
        image.thumbnail((1280, 1280))
        output = io.BytesIO()
        image.save(output, format='PNG')
    return 'data:image/png;base64,'+base64.b64encode(output.getvalue()).decode()


def evidence(result, left, right, destination, clip=None):
    destination = Path(destination)
    sources = [Path(left), Path(right)] + ([Path(clip)] if clip is not None else [])
    if result.get('diff_image'):
        sources.append(Path(result['diff_image']))
    if any(destination.resolve() == source.resolve() or
           (destination.exists() and source.exists() and destination.samefile(source)) for source in sources):
        raise ValueError('Viewer darf keine Eingabe überschreiben')
    if 'error' in result:
        raise ValueError('Fehlerhafte Bildpaare können keine Vergleichsansicht erzeugen')
    body = '<h1>Bildvergleich</h1><p class="muted">Messwerte und Sichtprüfung gemeinsam bewerten. Die Vorschau ist auf höchstens 1280 Pixel verkleinert; Messwerte stammen aus den angegebenen Originalen.</p>'
    body += '<div class="compare"><img alt="Vorher" src="'+image_data(left)+'"><img class="after" id="after" alt="Nachher" src="'+image_data(right)+'"></div><p><label for="split">Nachher-Anteil: <output id="value">50</output> %</label></p><input id="split" type="range" min="0" max="100" value="50">'
    if result.get('diff_image'):
        body += '<section><h2>Heatmap</h2><img alt="Heatmap" src="'+image_data(result['diff_image'])+'"></section>'
    if clip is not None:
        clip = Path(clip)
        if clip.suffix.lower() != '.mp4' or clip.stat().st_size > 20*1024*1024:
            raise ValueError('Clip muss MP4 und höchstens 20 MiB sein')
        body += '<section><h2>Prüfclip</h2><video controls preload="metadata" src="data:video/mp4;base64,'+base64.b64encode(clip.read_bytes()).decode()+'"></video></section>'
    body += '<section><h2>Originalmesswerte</h2><pre>'+html.escape(json.dumps(result, ensure_ascii=False, indent=2))+'</pre></section>'
    script = "const s=document.querySelector('#split');s.oninput=()=>{document.querySelector('#after').style.clipPath=`inset(0 ${100-s.value}% 0 0)`;document.querySelector('#value').value=s.value};"
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document('Cube Escape Bildvergleich', body, script), encoding='utf-8')


def server(port, repos=None, mail=False):
    guard = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # Prevent cross-site pages from querying private loopback state via a hostile Host header.
            if self.headers.get('Host') != '127.0.0.1:'+str(self.server.server_port):
                self.send_error(403); return
            if self.path not in ('/', '/api/state'):
                self.send_error(404); return
            with guard:
                data = snapshot(repos, mail)
            payload = (json.dumps(data, ensure_ascii=False, allow_nan=False) if self.path == '/api/state' else dashboard(data, True)).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8' if self.path == '/api/state' else 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers(); self.wfile.write(payload)
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('snapshot', 'serve'))
    parser.add_argument('--repos', nargs='+', type=Path)
    parser.add_argument('--mail', action='store_true', help='Postfach #16 online lesend abfragen')
    parser.add_argument('--out', type=Path, help='Portable HTML-Momentaufnahme')
    parser.add_argument('--port', type=int, default=8768)
    args = parser.parse_args(argv)
    if args.command == 'snapshot':
        data = snapshot(args.repos, args.mail)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(dashboard(data), encoding='utf-8')
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        with server(args.port, args.repos, args.mail) as instance:
            print('http://127.0.0.1:'+str(instance.server_port)+'/', flush=True)
            instance.serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main())
