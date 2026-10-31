"""Discoverable runner for the dependency-free native IDE host/security tests."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / 'integrations/vscode'


class IdeExtensionTests(unittest.TestCase):
    def test_node_host_and_security(self):
        node = shutil.which('node')
        bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
        if not node and bundled.is_file():
            node = str(bundled)
        if not node:
            self.skipTest('Node fehlt; native IDE-Hosttests nicht ausgeführt')
        result = subprocess.run([node, '--test', str(ROOT / 'tests/test_ide_extension.js')],
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_vsix_contains_only_extension_and_valid_manifests(self):
        spec = importlib.util.spec_from_file_location('prism_package', EXTENSION / 'package_vsix.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            output = module.package(Path(temp) / 'prism.vsix')
            with ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), {
                    '[Content_Types].xml', 'extension.vsixmanifest', 'extension/package.json',
                    'extension/extension.js', 'extension/backend.js', 'extension/README.md'})
                manifest = ET.fromstring(archive.read('extension.vsixmanifest'))
                ns = {'v': 'http://schemas.microsoft.com/developer/vsx-schema/2011'}
                identity = manifest.find('v:Metadata/v:Identity', ns)
                package = json.loads(archive.read('extension/package.json'))
                self.assertEqual(identity.attrib['Version'], package['version'])
                self.assertEqual(identity.attrib['Id'], package['name'])
                self.assertNotIn('dependencies', package)
                self.assertEqual(package['capabilities']['untrustedWorkspaces']['supported'], 'limited')
                for setting in package['contributes']['configuration']['properties'].values():
                    self.assertEqual(setting['scope'], 'machine')


if __name__ == '__main__':
    unittest.main()
