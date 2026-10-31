'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { spawn, execFileSync } = require('node:child_process');
const { EventEmitter } = require('node:events');
const { test } = require('node:test');
const { execute, protectHtml, render, escapeHtml } = require('../integrations/vscode/backend');
const { createExtension } = require('../integrations/vscode/extension');

const source = '<!doctype html><html lang="de"><meta charset="utf-8"><style>p{color:red}</style><p>&lt;script&gt;</p><script>void 0;</script></html>';

function fakeChild(code = 0, stdout = '', stderr = '', error, signal) {
  const child = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdout.setEncoding = child.stderr.setEncoding = () => {};
  setImmediate(() => {
    if (error) child.emit('error', error);
    else {
      child.stdout.emit('data', stdout); child.stderr.emit('data', stderr);
      child.emit('close', code, signal);
    }
  });
  return child;
}

test('process arguments preserve metacharacters without a shell', async () => {
  let invocation;
  const runner = (...args) => {
    invocation = args.slice(0, 3);
    return fakeChild(0, 'ok');
  };
  const input = '/tmp/a $(touch surprise) " ü.png';
  assert.equal(await execute('/my python', '/tools/diff.py', [input], '/tools', [0], runner), 'ok');
  assert.equal(invocation[0], '/my python');
  assert.deepEqual(invocation[1], ['/tools/diff.py', input]);
  assert.equal(invocation[2].shell, false);
  assert.deepEqual(invocation[2].stdio, ['ignore', 'pipe', 'pipe']);
  assert.equal(invocation[2].detached, process.platform !== 'win32');
});

test('difference exit 1 is accepted but validator errors and timeouts fail', async () => {
  const runner = code => () => fakeChild(code, '', 'bad');
  await execute('python', 'diff', [], '/', [0, 1], runner(1));
  await assert.rejects(execute('python', 'diff', [], '/', [0, 1], runner(2)), /bad/);
  await assert.rejects(execute('python', 'diff', [], '/', [0, 1], () => fakeChild(0, '', '', new Error('ENOENT'))), /ENOENT/);
  await assert.rejects(execute('python', 'diff', [], '/', [0, 1],
    () => fakeChild(null, '', '', undefined, 'SIGTERM')), /SIGTERM/);
});

test('real spawn preserves Unicode and accepted difference status, and bounds output', async () => {
  const text = await execute(process.execPath, '-e', ["process.stdout.write('Änderung ✓');process.exitCode=1"],
    os.tmpdir(), [0, 1]);
  assert.equal(text, 'Änderung ✓');
  await assert.rejects(execute(process.execPath, '-e',
    ["process.stdout.write('x'.repeat(2*1024*1024+1));setInterval(()=>{},1000)"],
    os.tmpdir(), [0], spawn, { timeoutMs: 2000, killGraceMs: 50, closeWaitMs: 1000 }), /Ausgabepuffer/);
});

test('CSP precedes content, nonces trusted inline code and denies host/network resources', () => {
  const html = protectHtml(source);
  assert.match(html, /default-src &#39;none&#39;/);
  assert.match(html, /connect-src &#39;none&#39;/);
  assert.match(html, /frame-src &#39;none&#39;/);
  assert.match(html, /img-src data:/);
  assert.match(html, /media-src data:/);
  assert.ok(html.indexOf('Content-Security-Policy') < html.indexOf('<style'));
  const nonce = html.match(/<script nonce="([^"]+)"/)[1];
  assert.ok(nonce.length >= 32);
  assert.ok(html.includes(`<style nonce="${nonce}">`));
  assert.notEqual(protectHtml(source), html);
  assert.throws(() => protectHtml('<html>arbitrary</html>'), /Dokumentformat/);
  assert.equal(escapeHtml('<&"\''), '&lt;&amp;&quot;&#39;');
});

test('dashboard delegates to existing snapshot CLI without online mail and cleans output', async () => {
  let temp;
  const html = await render({ python: 'python3', tools: '/backend', repos: ['/repo one', '/repo;two'] },
    async (python, script, args, cwd) => {
      assert.equal(script, path.join('/backend', 'ce_console.py')); // (0.31) native separator
      assert.equal(cwd, '/backend');
      assert.deepEqual(args.slice(3), ['--repos', '/repo one', '/repo;two']);
      assert.equal(args.includes('--mail'), false);
      temp = path.dirname(args[2]);
      await fs.writeFile(args[2], source);
    });
  assert.match(html, /Content-Security-Policy/);
  await assert.rejects(fs.stat(temp), { code: 'ENOENT' });
});

test('evidence delegates reference and MP4 directly to the image validator', async () => {
  await render({ python: 'python3', tools: '/backend', repos: [], left: '/a.png', right: '/b.png',
    reference: '/ref.json', clip: '/clip.mp4' }, async (python, script, args, cwd, codes) => {
    assert.equal(script, path.join('/backend', 'ce_imagediff.py'));
    assert.deepEqual(args.slice(0, 3), ['/a.png', '/b.png', '--viewer']);
    assert.deepEqual(args.slice(4), ['--json', '--reference', '/ref.json', '--clip', '/clip.mp4']);
    assert.deepEqual(codes, [0, 1]);
    await fs.writeFile(args[3], source);
  });
});

test('temporary directory is removed when the backend fails', async () => {
  let temp;
  await assert.rejects(render({ python: 'python3', tools: '/backend', repos: [] }, async (_, __, args) => {
    temp = path.dirname(args[2]);
    throw new Error('backend refused');
  }), /backend refused/);
  await assert.rejects(fs.stat(temp), { code: 'ENOENT' });
});

test('windows timeout ends the whole tree of the own child, never a group signal', async () => {
  // (0.31) mocked, runs everywhere: no process groups and no TERM on Windows -> taskkill /T for our own pid
  const killed = [];
  const child = new EventEmitter();
  child.pid = 4242; child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdout.setEncoding = child.stderr.setEncoding = () => {};
  child.kill = () => assert.fail('the parent alone must not be ended');
  let options;
  await assert.rejects(execute('python', 'diff', [], '/', [0], (python, argv, opts) => { options = opts; return child; },
    { timeoutMs: 20, killGraceMs: 10, closeWaitMs: 10, platform: 'win32', killTree: pid => killed.push(pid) }), /Timeout/);
  assert.equal(options.detached, false);
  assert.deepEqual(killed, [4242, 4242]);
});

test('windows never ends a tree after the own child has exited (its pid may be reused)', async () => {
  const killed = [];
  const child = new EventEmitter();
  child.pid = 4243; child.exitCode = null; child.signalCode = null;
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdout.setEncoding = child.stderr.setEncoding = () => {};
  setTimeout(() => { child.exitCode = 3; child.emit('close', 3, null); }, 5);
  await assert.rejects(execute('python', 'diff', [], '/', [0], () => child,
    { timeoutMs: 1000, killGraceMs: 10, closeWaitMs: 10, platform: 'win32', killTree: pid => killed.push(pid) }),
  /fehlgeschlagen/);
  assert.deepEqual(killed, [], 'exit with an error code: nothing left to end, no taskkill on a stale pid');
});

test('real timeout kills the owned group including a TERM-ignoring child and removes render output',
  { timeout: 8000 }, async () => {
    const tools = await fs.mkdtemp(path.join(os.tmpdir(), 'ce-prism-job-test-'));
    const pidFile = path.join(tools, 'pids.json');
    const script = path.join(tools, 'ce_console.py');
    let temp, pids;
    function isRunning(pid) {
      if (process.platform === 'win32') { // (0.31) no ps; signal 0 only asks there
        try { process.kill(pid, 0); return true; } catch { return false; }
      }
      try {
        process.kill(pid, 0);
        // Orphans may briefly remain as reaped-by-init zombies on Linux containers.
        const state = execFileSync('ps', ['-o', 'stat=', '-p', String(pid)], { encoding: 'utf8' }).trim();
        return state !== '' && !state.startsWith('Z');
      } catch { return false; }
    }
    try {
      await fs.writeFile(script, `
const fs = require('node:fs');
if (process.argv[2] === '--child') {
  process.on('SIGTERM', () => {});
  fs.writeFileSync(${JSON.stringify(pidFile)}, JSON.stringify({parent: process.ppid, child: process.pid}));
} else {
  require('node:child_process').spawn(process.execPath, [__filename, '--child'], {stdio: 'ignore'});
}
setInterval(() => {}, 1000);
`);
      await assert.rejects(render({ python: process.execPath, tools, repos: [] },
        (python, file, args, cwd, codes) => {
          temp = path.dirname(args[2]);
          return execute(python, file, args, cwd, codes, spawn,
            { timeoutMs: 1000, killGraceMs: 150, closeWaitMs: 1000 });
        }), /Timeout/);
      pids = JSON.parse(await fs.readFile(pidFile, 'utf8'));
      assert.equal(isRunning(pids.parent), false, 'backend parent must exit');
      // SIGKILL delivery precedes the scheduling of process exit by a few ticks.
      const until = Date.now() + 1500;
      while (isRunning(pids.child) && Date.now() < until) {
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      assert.equal(isRunning(pids.child), false, 'TERM-ignoring child must exit');
      await assert.rejects(fs.stat(temp), { code: 'ENOENT' });
    } finally {
      if (!pids) {
        try { pids = JSON.parse(await fs.readFile(pidFile, 'utf8')); } catch {}
      }
      if (pids) {
        if (process.platform === 'win32') {
          for (const pid of [pids.child, pids.parent]) { try { process.kill(pid, 'SIGKILL'); } catch {} }
        } else {
          try { process.kill(-pids.parent, 'SIGKILL'); } catch {}
        }
      }
      await fs.rm(tools, { recursive: true, force: true });
    }
  });

function host() {
  const commands = new Map(), errors = [], panels = [], dialogs = [], choices = [];
  const vscode = {
    workspace: {
      isTrusted: true, workspaceFolders: [{ uri: { scheme: 'file', fsPath: '/repo' } }],
      getConfiguration: () => ({ get: (_, fallback) => fallback })
    },
    env: {}, ProgressLocation: { Notification: 15 }, ViewColumn: { Active: -1 },
    commands: { registerCommand: (name, command) => { commands.set(name, command); return { dispose() {} }; } },
    window: {
      showErrorMessage: async value => errors.push(value),
      showOpenDialog: async () => dialogs.shift(), showQuickPick: async () => choices.shift(),
      withProgress: (_, action) => action(),
      createWebviewPanel: (type, title, column, options) => {
        let onDispose;
        const panel = { type, title, options, webview: {}, reveal() {},
          onDidDispose(fn) { onDispose = fn; }, dispose() { onDispose(); } };
        panels.push(panel);
        return panel;
      }
    }
  };
  return { vscode, commands, errors, panels, dialogs, choices };
}

test('untrusted and remote workspaces never invoke backend', async () => {
  for (const state of ['untrusted', 'remote', 'virtual']) {
    const h = host(); let calls = 0;
    if (state === 'untrusted') h.vscode.workspace.isTrusted = false;
    if (state === 'remote') h.vscode.env.remoteName = 'ssh-remote';
    if (state === 'virtual') h.vscode.workspace.workspaceFolders[0].uri.scheme = 'vscode-vfs';
    createExtension(h.vscode, async () => { calls++; }).activate({ subscriptions: [] });
    await h.commands.get('cePrism.dashboard')();
    assert.equal(calls, 0);
    assert.equal(h.panels.length, 0);
    assert.equal(h.errors.length, 1);
  }
});

test('native panel is reused, isolated and recreated after disposal', async () => {
  const h = host(); const context = { subscriptions: [] }; let calls = 0;
  createExtension(h.vscode, async opts => { calls++; assert.deepEqual(opts.repos, ['/repo']); return source; }).activate(context);
  await h.commands.get('cePrism.dashboard')();
  await h.commands.get('cePrism.dashboard')();
  assert.equal(calls, 2);
  assert.equal(h.panels.length, 1);
  assert.deepEqual(h.panels[0].options.localResourceRoots, []);
  assert.equal(h.panels[0].options.enableCommandUris, false);
  assert.equal(h.panels[0].webview.onDidReceiveMessage, undefined);
  h.panels[0].dispose();
  await h.commands.get('cePrism.dashboard')();
  assert.equal(h.panels.length, 2);
  context.subscriptions.at(-1).dispose();
});

test('cancelled evidence dialog and revoked trust do not start backend', async () => {
  for (const revoked of [false, true]) {
    const h = host(); let calls = 0;
    if (revoked) {
      h.dialogs.push([{ scheme: 'file', fsPath: '/a.png' }], [{ scheme: 'file', fsPath: '/b.png' }]);
      h.vscode.window.showQuickPick = async () => {
        h.vscode.workspace.isTrusted = false;
        return { reference: false, clip: false };
      };
    }
    createExtension(h.vscode, async () => { calls++; }).activate({ subscriptions: [] });
    await h.commands.get('cePrism.evidence')();
    assert.equal(calls, 0);
    assert.equal(h.panels.length, 0);
    assert.equal(h.errors.length, revoked ? 1 : 0);
  }
});

test('evidence forwards explicitly selected files and ignores command-supplied paths', async () => {
  const h = host(); let received;
  for (const name of ['a.png', 'b.png', 'ref.json', 'clip.mp4']) {
    h.dialogs.push([{ scheme: 'file', fsPath: '/picked/' + name }]);
  }
  h.choices.push({ reference: true, clip: true });
  createExtension(h.vscode, async opts => { received = opts; return source; }).activate({ subscriptions: [] });
  await h.commands.get('cePrism.evidence')({ left: '/malicious', clip: '/unselected' });
  assert.equal(received.left, '/picked/a.png');
  assert.equal(received.right, '/picked/b.png');
  assert.equal(received.reference, '/picked/ref.json');
  assert.equal(received.clip, '/picked/clip.mp4');
  assert.equal(h.errors.length, 0);
});

test('duplicate pending command does not start another backend', async () => {
  const h = host(); let resolve, calls = 0;
  const started = new Promise(ready => {
    createExtension(h.vscode, () => { calls++; ready(); return new Promise(done => { resolve = done; }); })
      .activate({ subscriptions: [] });
  });
  const first = h.commands.get('cePrism.dashboard')();
  await started;
  await h.commands.get('cePrism.dashboard')();
  assert.equal(calls, 1);
  resolve(source);
  await first;
});
