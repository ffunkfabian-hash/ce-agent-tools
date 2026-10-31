'use strict';

const { spawn } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');

// Argument arrays intentionally preserve spaces, shell metacharacters and Unicode.
// (0.31) Windows has neither process groups nor TERM: taskkill /T /F ends our own child and every descendant.
function killTreeWindows(pid) {
  const killer = spawn('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true, shell: false });
  killer.on('error', () => {});
}

function execute(python, script, args, cwd, allowedCodes = [0], runner = spawn,
  { timeoutMs = 60000, killGraceMs = 1000, closeWaitMs = 1000, platform = process.platform,
    killTree = killTreeWindows } = {}) {
  return new Promise((resolve, reject) => {
    const grouped = platform !== 'win32';
    let child, deadline, escalation, closeDeadline, result, abortError, settled = false;
    let stdout = '', stderr = '', bytes = 0;

    function finish() {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      clearTimeout(escalation);
      clearTimeout(closeDeadline);
      if (abortError) reject(abortError);
      else if (result.error) reject(result.error);
      else resolve(result.stdout);
    }

    function signalOwnedJob(signal) {
      // Only the pid returned by our own detached spawn can select a process group.
      if (!child || !Number.isInteger(child.pid) || child.pid <= 0) return;
      try {
        if (grouped) process.kill(-child.pid, signal);
        // Windows has no POSIX process-group signalling: the whole tree at once, while the parent still links it
        // (a TERM stage that ended only the parent would orphan a TERM-ignoring child); KILL repeats it. Never once
        // the parent has ended: its pid may already belong to another program, and taskkill /T would end that tree.
        else if (child.exitCode == null && child.signalCode == null) killTree(child.pid);
      } catch (error) {
        if (error.code !== 'ESRCH') {
          abortError = new Error(`${abortError.message}; Prozessabbruch fehlgeschlagen: ${error.message}`);
        }
      }
    }

    function abort(error) {
      if (settled || abortError) return;
      abortError = error;
      clearTimeout(deadline);
      signalOwnedJob('SIGTERM');
      // Do not cancel escalation when the parent exits: a descendant may ignore TERM
      // and may already have closed the pipes observed by the close event.
      escalation = setTimeout(() => {
        signalOwnedJob('SIGKILL');
        if (result) finish();
        else closeDeadline = setTimeout(finish, closeWaitMs);
      }, killGraceMs);
    }

    child = runner(python, [script, ...args], {
      cwd, shell: false, windowsHide: true, detached: grouped, stdio: ['ignore', 'pipe', 'pipe']
    });
    function collect(chunk, isError) {
      if (settled || abortError) return;
      bytes += Buffer.byteLength(chunk);
      if (bytes > 2 * 1024 * 1024) {
        abort(new Error('Lokaler Backend-Aufruf: Ausgabepuffer überschritten.'));
      } else if (isError) stderr += chunk;
      else stdout += chunk;
    }
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', chunk => collect(chunk, false));
    child.stderr.on('data', chunk => collect(chunk, true));
    child.on('error', error => {
      if (settled || abortError) return;
      result = { error: new Error(`Lokaler Backend-Aufruf fehlgeschlagen: ${error.message}`) };
      finish();
    });
    child.on('close', (code, signal) => {
      if (settled) return;
      result = { stdout };
      if (signal || !allowedCodes.includes(code)) {
        const detail = String(stderr || signal || `Exit ${code}`).slice(0, 2000);
        result.error = new Error(`Lokaler Backend-Aufruf fehlgeschlagen: ${detail}`);
      }
      if (abortError) return; // Keep the group's scheduled KILL even after parent exit.
      if (result.error && child && child.pid) abort(result.error);
      else finish();
    });
    deadline = setTimeout(() => abort(new Error('Lokaler Backend-Aufruf: Timeout nach ' + timeoutMs + ' ms.')), timeoutMs);
  });
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, value => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[value]);
}

// This adapts only the trusted ce_console document format, never arbitrary HTML files.
// No host bridge, command URIs, network, iframe or local resource loading is enabled.
function protectHtml(html, nonce = crypto.randomBytes(24).toString('base64')) {
  const policy = `default-src 'none'; img-src data:; media-src data:; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; connect-src 'none'; font-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'`;
  if (!html.startsWith('<!doctype html><html lang="de"><meta charset="utf-8">')) {
    throw new Error('Unerwartetes Backend-Dokumentformat; ce-shared aktualisieren.');
  }
  return html.replace('<meta charset="utf-8">',
    `<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${escapeHtml(policy)}">`)
    .replaceAll('<style>', `<style nonce="${nonce}">`)
    .replaceAll('<script>', `<script nonce="${nonce}">`);
}

async function render({ python, tools, repos, left, right, reference, clip }, run = execute) {
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'ce-prism-'));
  try {
    const output = path.join(temp, 'panel.html');
    if (left && right) {
      const args = [left, right, '--viewer', output, '--json'];
      if (reference) args.push('--reference', reference);
      if (clip) args.push('--clip', clip);
      // Exit 1 is a measured image difference, not a backend failure.
      await run(python, path.join(tools, 'ce_imagediff.py'), args, tools, [0, 1]);
    } else {
      const args = ['snapshot', '--out', output];
      if (repos.length) args.push('--repos', ...repos);
      await run(python, path.join(tools, 'ce_console.py'), args, tools);
    }
    return protectHtml(await fs.readFile(output, 'utf8'));
  } finally {
    await fs.rm(temp, { recursive: true, force: true });
  }
}

module.exports = { execute, escapeHtml, protectHtml, render };
