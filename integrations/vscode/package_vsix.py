#!/usr/bin/env python3
"""Build a dependency-free local VSIX; no install, network or global settings."""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile


def package(destination):
    root = Path(__file__).resolve().parent
    metadata = json.loads((root / 'package.json').read_text())
    namespace = 'http://schemas.microsoft.com/developer/vsx-schema/2011'
    ET.register_namespace('', namespace)
    manifest = ET.Element('PackageManifest', {'Version': '2.0.0', 'xmlns': namespace})
    info = ET.SubElement(manifest, 'Metadata')
    ET.SubElement(info, 'Identity', {'Language': 'de-DE', 'Id': metadata['name'],
                                  'Version': metadata['version'], 'Publisher': metadata['publisher']})
    ET.SubElement(info, 'DisplayName').text = metadata['displayName']
    ET.SubElement(info, 'Description', {'{http://www.w3.org/XML/1998/namespace}space': 'preserve'}).text = metadata['description']
    properties = ET.SubElement(info, 'Properties')
    ET.SubElement(properties, 'Property', {'Id': 'Microsoft.VisualStudio.Code.Engine',
                                         'Value': metadata['engines']['vscode']})
    ET.SubElement(properties, 'Property', {'Id': 'Microsoft.VisualStudio.Code.ExtensionKind', 'Value': 'ui'})
    install = ET.SubElement(manifest, 'Installation')
    ET.SubElement(install, 'InstallationTarget', {'Id': 'Microsoft.VisualStudio.Code'})
    ET.SubElement(manifest, 'Dependencies')
    assets = ET.SubElement(manifest, 'Assets')
    ET.SubElement(assets, 'Asset', {'Type': 'Microsoft.VisualStudio.Code.Manifest',
                                 'Path': 'extension/package.json', 'Addressable': 'true'})
    content = ET.Element('Types', {'xmlns': 'http://schemas.openxmlformats.org/package/2006/content-types'})
    for extension, mime in [('json', 'application/json'), ('js', 'application/javascript'),
                            ('md', 'text/markdown'), ('vsixmanifest', 'text/xml')]:
        ET.SubElement(content, 'Default', {'Extension': extension, 'ContentType': mime})
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('extension.vsixmanifest', ET.tostring(manifest, encoding='utf-8', xml_declaration=True))
        archive.writestr('[Content_Types].xml', ET.tostring(content, encoding='utf-8', xml_declaration=True))
        for filename in ['package.json', 'extension.js', 'backend.js', 'README.md']:
            archive.write(root / filename, 'extension/' + filename)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    print(package(parser.parse_args().out))
