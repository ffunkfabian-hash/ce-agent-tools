import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.request import Request, urlopen
from urllib.error import HTTPError

TOOLS = Path(__file__).resolve().parents[1]/'plugins/ce-shared/tools'
sys.path.insert(0, str(TOOLS))
import ce_check
import ce_console
import ce_lock_status
import heavy_lock


def started_as(pid, launcher):
    """launcher when pid is it - or, on Windows, its direct child: a venv's python.exe only redirects to the real
    interpreter, which then runs (and holds or waits) under its own pid (0.31)."""
    if pid == launcher or (heavy_lock.WINDOWS and heavy_lock.process_table().get(pid, (None,))[0] == launcher):
        return launcher
    return pid


class ConsoleTests(unittest.TestCase):
    def test_missing_stale_and_zero_quota_are_distinct(self):
        now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
        with mock.patch.object(ce_console, 'run_json', side_effect=ValueError()):
            self.assertFalse(ce_console.quota(now)['known'])
        with mock.patch.object(ce_console, 'run_json', return_value={'as_of':'2026-10-22T10:00:00Z', 'weekly':{'used':100}}):
            item = ce_console.quota(now)
        self.assertTrue(item['known']); self.assertTrue(item['stale'])
        self.assertEqual(item['windows']['weekly']['remaining_percent'], 0)
        for value in (True, -1, 101, float('nan')):
            with mock.patch.object(ce_console, 'run_json', return_value={'as_of':now.isoformat(),'weekly':{'used':value}}):
                self.assertFalse(ce_console.quota(now)['known'])

    def test_budget_read_does_not_rewrite_or_create_ledger_or_authenticate(self):
        import ce_api
        now = datetime(2026,10,22,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp)/'budget.json'
            ledger.write_text('{"period":"2026-10","spent":"1.25","reservations":{}}')
            before = ledger.read_bytes(), ledger.stat().st_mtime_ns
            with mock.patch.object(ce_api,'LEDGER',ledger), mock.patch.object(ce_api,'load_policy',return_value={'monthly_usd':'20','request_usd':'0.25'}), mock.patch.object(ce_api,'access_token',side_effect=AssertionError('OAuth forbidden')):
                result = ce_console.api_budget(now)
            self.assertTrue(result['known']); self.assertEqual(result['spent_usd'],'1.25')
            self.assertEqual(before,(ledger.read_bytes(),ledger.stat().st_mtime_ns))
            self.assertFalse(Path(str(ledger)+'.lock').exists())
            ledger.unlink()
            with mock.patch.object(ce_api,'LEDGER',ledger):
                self.assertFalse(ce_console.api_budget(now)['known'])
            self.assertFalse(ledger.exists())

    def test_untrusted_names_cannot_escape_dashboard_script(self):
        data = dict(observed_at='2026-10-22T12:00:00Z',locks={},tracked_waiters=[],worktrees=[],excluded=[],codex={},api={},mailbox={'title':'</script><script>alert(1)</script>'},queue_note='',session_note='')
        rendered = ce_console.dashboard(data)
        self.assertNotIn('</script><script>alert(1)',rendered)
        self.assertIn('\\u003c/script>',rendered)
        self.assertIn('textContent',rendered)

    def test_hold_is_excluded_and_default_has_no_remote_calls(self):
        with mock.patch.object(ce_console.ce_common,'repo_hold',return_value='hold'), mock.patch.object(ce_console.ce_worktrees,'collect',return_value=[]) as collect, mock.patch.object(ce_console,'quota',return_value={}), mock.patch.object(ce_console,'api_budget',return_value={}), mock.patch.object(ce_console,'run_json',side_effect=AssertionError('network forbidden')):
            state=ce_console.snapshot([Path('/guarded')])
        self.assertEqual(collect.call_args.args[0],[])
        self.assertEqual(state['excluded'][0]['reason'],'hold')
        self.assertFalse(state['mailbox']['known'])

    def test_server_has_only_fixed_read_routes_and_checks_host(self):
        data={'observed_at':'2026-10-22T12:00:00Z'}
        with mock.patch.object(ce_console,'snapshot',return_value=data):
            server=ce_console.server(0,[])
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                base='http://127.0.0.1:'+str(server.server_port)
                with urlopen(base+'/api/state') as response:
                    self.assertEqual(json.load(response),data)
                for request, code in [(Request(base+'/api/state',headers={'Host':'untrusted.example'}),403), (Request(base+'/../../.codex/config.toml'),404), (Request(base+'/api/state',data=b'x',method='POST'),501)]:
                    with self.assertRaises(HTTPError) as error:urlopen(request)
                    self.assertEqual(error.exception.code,code)
            finally:
                server.shutdown();thread.join();server.server_close()

    def test_named_checks_run_native_commands_and_reject_missing_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'.ce-repo.json').write_text(json.dumps({'name':'fixture','check':{'quick':[sys.executable,'-c','raise SystemExit(99)'],'logic':[sys.executable,'-c','print("NATIVE_LOGIC")']}}))
            with mock.patch.object(ce_check.ce_common,'open_repo',return_value=(root,json.loads((root/'.ce-repo.json').read_text()))):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(ce_check.main(['quick','--profile','logic']),0)
                self.assertIn('logic: GREEN',out.getvalue())
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(ce_check.main(['quick','--profile','missing']),2)
                    self.assertEqual(ce_check.main(['full','--profile','logic']),2)


class WaiterTests(unittest.TestCase):
    def test_live_waiter_appears_then_is_removed_after_acquisition(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock=Path(tmp)/'heavy-run.lock'
            env=dict(os.environ,CE_HEAVY_LOCK_FILE=str(lock))
            for key in ('CE_HEAVY_LOCK','CE_HEAVY_HELD','CE_HEAVY_RESOURCE'):env.pop(key,None)
            prefix='import sys;sys.path.insert(0,'+repr(str(TOOLS))+');import heavy_lock as h;'
            holder=subprocess.Popen([sys.executable,'-u','-c',prefix+'l=h.HeavyLock("holder",resource="unity").acquire();print("READY");input();l.release()'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env=env)
            waiter=None
            try:
                self.assertEqual(holder.stdout.readline().strip(),'READY')
                waiter=subprocess.Popen([sys.executable,'-u','-c',prefix+'l=h.HeavyLock("waiter",resource="unity",poll=0.02).acquire();print("ACQUIRED");l.release()'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env=env)
                self.assertIn('waiting',waiter.stdout.readline())
                rows=ce_lock_status.waiting(lock)
                self.assertEqual([(started_as(r['pid'],waiter.pid),r['resource']) for r in rows],[(waiter.pid,'unity')])
                holder.communicate(input='release\n',timeout=5)
                output=waiter.communicate(timeout=5)[0]
                self.assertIn('ACQUIRED',output)
                self.assertEqual(ce_lock_status.waiting(lock),[])
            finally:
                for child in (holder,waiter):
                    if child and child.poll() is None:child.terminate();child.communicate(timeout=5)

    def test_reused_pid_and_malformed_records_are_not_active_waiters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=ce_lock_status.folder(Path(tmp)/'lock');root.mkdir()
            (root/'bad.json').write_text('{')
            (root/'reused.json').write_text(json.dumps({'version':1,'pid':os.getpid(),'process_start':'another process','resource':'unity','since':'2026-10-22T10:00:00Z'}))
            self.assertEqual(ce_lock_status.waiting(Path(tmp)/'lock'),[])

    def test_telemetry_failure_does_not_change_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'.ce-lock-waiters';root.write_text('not a directory')
            self.assertIsNone(ce_lock_status.announce(Path(tmp)/'lock','unity','x','/x','barrier'))


if __name__=='__main__':unittest.main()
