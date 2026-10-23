'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const backend = require('./backend');

function createExtension(vscode, renderer = backend.render) {
  const panels = new Map();
  const busy = new Set();

  function requireLocalTrust() {
    if (!vscode.workspace.isTrusted) {
      throw new Error('CE Prism benötigt Workspace Trust, bevor lokale Werkzeuge ausgeführt werden.');
    }
    if (vscode.env.remoteName || (vscode.workspace.workspaceFolders || []).some(folder => folder.uri.scheme !== 'file')) {
      throw new Error('CE Prism unterstützt lokale IDE-Fenster; Remote- und virtuelle Workspaces werden nicht ausgeführt.');
    }
  }

  async function options() {
    requireLocalTrust();
    const config = vscode.workspace.getConfiguration('cePrism');
    const configured = config.get('toolsPath', '');
    const tools = configured || path.resolve(__dirname, '../../plugins/ce-shared/tools');
    if (!path.isAbsolute(tools)) throw new Error('cePrism.toolsPath muss ein absoluter lokaler Pfad sein.');
    for (const script of ['ce_console.py', 'ce_imagediff.py']) {
      if (!(await fs.stat(path.join(tools, script))).isFile()) throw new Error(`Backend fehlt: ${script}`);
    }
    const python = config.get('pythonPath', 'python3');
    if (typeof python !== 'string' || !python.trim()) throw new Error('Python-Interpreter fehlt.');
    return { python, tools, repos: (vscode.workspace.workspaceFolders || []).map(folder => folder.uri.fsPath) };
  }

  async function choose(title, filters) {
    const picked = await vscode.window.showOpenDialog({
      title, canSelectFiles: true, canSelectFolders: false, canSelectMany: false, filters
    });
    if (!picked || !picked.length) return undefined;
    if (picked[0].scheme !== 'file') throw new Error('Bitte eine lokale Datei wählen.');
    return picked[0].fsPath;
  }

  async function open(kind) {
    if (busy.has(kind)) return;
    busy.add(kind);
    try {
      const opts = await options();
      if (kind === 'evidence') {
        const filters = { Bilder: ['png', 'jpg', 'jpeg', 'webp', 'bmp', 'tif', 'tiff'] };
        opts.left = await choose('Vorher: Referenzbild wählen', filters);
        if (!opts.left) return;
        opts.right = await choose('Nachher: Prüfbild wählen', filters);
        if (!opts.right) return;
        const extra = await vscode.window.showQuickPick([
          { label: 'Ohne Zusatzdateien', reference: false, clip: false },
          { label: 'Referenzprofil (JSON)', reference: true, clip: false },
          { label: 'Prüfclip (MP4)', reference: false, clip: true },
          { label: 'Referenzprofil und Prüfclip', reference: true, clip: true }
        ], { title: 'Optionale vorhandene Belege' });
        if (!extra) return;
        if (extra.reference) {
          opts.reference = await choose('Vorhandenes Referenzprofil wählen', { JSON: ['json'] });
          if (!opts.reference) return;
        }
        if (extra.clip) {
          opts.clip = await choose('Vorhandenen Prüfclip wählen (höchstens 20 MiB)', { MP4: ['mp4'] });
          if (!opts.clip) return;
        }
      }
      // Dialogs can yield control; recheck immediately before any subprocess starts.
      requireLocalTrust();
      const html = await vscode.window.withProgress({
        location: vscode.ProgressLocation.Notification, title: 'CE Prism: lokale Ansicht erstellen', cancellable: false
      }, () => renderer(opts));
      let panel = panels.get(kind);
      if (!panel) {
        panel = vscode.window.createWebviewPanel(`cePrism.${kind}`,
          kind === 'evidence' ? 'CE Prism · Visual Evidence' : 'CE Prism · Locks / Sessions / Quota',
          vscode.ViewColumn.Active, {
            enableScripts: true, enableCommandUris: false, localResourceRoots: [],
            enableFindWidget: true, retainContextWhenHidden: false
          });
        panels.set(kind, panel);
        panel.onDidDispose(() => panels.delete(kind));
        // No onDidReceiveMessage handler: every webview message is ignored.
      }
      panel.webview.html = html;
      panel.reveal(vscode.ViewColumn.Active);
    } catch (error) {
      await vscode.window.showErrorMessage(`CE Prism: ${error.message}`);
    } finally {
      busy.delete(kind);
    }
  }

  return {
    activate(context) {
      for (const kind of ['dashboard', 'evidence']) {
        context.subscriptions.push(vscode.commands.registerCommand(`cePrism.${kind}`, () => open(kind)));
      }
      context.subscriptions.push({ dispose() {
        for (const panel of panels.values()) panel.dispose();
        panels.clear();
      } });
    }
  };
}

function activate(context) { createExtension(require('vscode')).activate(context); }
module.exports = { activate, createExtension };
