"""Device references use the producer's split object graph, not material names."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/ce-shared/tools'))
import ce_contract
import ce_interfaces

EXPORT_FIXTURES = Path(__file__).resolve().parent / 'fixtures/contract_exports'


def device():
    return {'format': 4, 'generator': 'toolgen', 'version': '0.20.0 Nightshift',
            'tool': 'pistol', 'variant': 'world', 'asset': 'Device', 'model': 'Device_Game',
            'units': 'meter', 'scale': 1,
            'axes': {'blender': {'up': '+Z', 'forward': '-Y'},
                     'unity': {'up': '+Y', 'forward': '+Z'}, 'fbx': {'export_yaw_degrees': 180}},
            'root': {'name': 'Device', 'properties': {}},
            'nodes': [{'name': 'Device', 'parent': None}, {'name': 'Bolt', 'parent': 'Device'}],
            'anchors': [{'name': 'Socket_Hand', 'parent': 'Bolt'}],
            'renderers': [{'name': 'Mesh', 'parent': 'Device', 'lod': 0, 'triangles': 12, 'materials': ['MT_Metal']}],
            'materials': {'MT_Metal': {'color_srgb': [.5, .5, .5], 'metallic': 0, 'roughness': .5, 'emission': 0}},
            'colliders': [{'type': 'box', 'part': 'Bolt', 'center': [0, 0, 0], 'size': [1, 1, 1]}],
            'hold': {'hand_socket': 'Socket_Hand', 'support_socket': None},
            'files': {'glb': {'path': 'device.glb', 'sha256': 'a' * 64}}}


class DeviceGraphTests(unittest.TestCase):
    def check(self, document, schema=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'unity_contract.json'
            path.write_text(json.dumps(document))
            return ce_contract.check_file(path, schema)[1]

    def test_split_graph_and_additive_metadata_are_valid_in_formats_four_and_five(self):
        for version in (4, 5):
            doc = device(); doc['format'] = version; doc['future_note'] = {'anything': True}
            self.assertEqual(self.check(doc), [])

    def test_declared_root_is_known_without_node_duplicate(self):
        doc = device(); doc['nodes'].pop(0)
        self.assertEqual(self.check(doc), [])

    def test_material_namespace_cannot_supply_graph_parents(self):
        for field in ('nodes', 'anchors', 'renderers'):
            doc = device(); doc[field][-1]['parent'] = 'MT_Metal'
            self.assertTrue(self.check(doc), field)

    def test_unknown_anchor_parent_is_rejected_with_precise_location(self):
        doc = device(); doc['anchors'][0]['parent'] = 'Missing'
        self.assertIn('$.anchors[0].parent: unbekannter Knoten Missing', self.check(doc))
        self.assertTrue(self.check(doc, 'unity_contract.v4.json'))

    def test_root_and_materials_are_not_magic_names(self):
        for name in ('Root', 'Materials'):
            doc = device(); doc['nodes'][1]['parent'] = name
            self.assertTrue(self.check(doc))
            doc['nodes'].append({'name': name, 'parent': 'Device'})
            self.assertEqual(self.check(doc), [])

    def test_object_duplicates_and_cross_collection_conflicts_are_rejected(self):
        for collection in ('nodes', 'anchors', 'renderers'):
            doc = device(); doc[collection].append(copy.deepcopy(doc[collection][-1]))
            self.assertTrue(self.check(doc), collection)
        doc = device(); doc['anchors'][0]['name'] = 'Bolt'
        self.assertTrue(self.check(doc))
        doc = device(); doc['renderers'][0]['name'] = 'Socket_Hand'
        self.assertTrue(self.check(doc))

    def test_parent_cycles_include_anchors_and_renderer_objects(self):
        for mutate in (lambda d: d['nodes'][1].update(parent='Bolt'),
                       lambda d: d['nodes'][1].update(parent='Socket_Hand'),
                       lambda d: d['nodes'][1].update(parent='Mesh'),):
            doc = device(); mutate(doc); doc['renderers'][0]['parent'] = 'Bolt'
            self.assertTrue(self.check(doc))

    def test_renderer_reference_alias_and_old_optional_parent_remain_supported(self):
        doc = device(); doc['renderers'][0]['name'] = 'Bolt'; doc['renderers'][0]['parent'] = 'Device'
        self.assertEqual(self.check(doc), [])
        del doc['renderers'][0]['parent']
        self.assertEqual(self.check(doc), [])

    def test_renderer_materials_hold_sockets_and_colliders_must_resolve(self):
        mutations = (lambda d: d['renderers'][0]['materials'].append('MT_Missing'),
                     lambda d: d['colliders'][0].update(part='Missing'),
                     lambda d: d['hold'].update(eye_socket='Missing'),
                     lambda d: d['hold'].update(hand_socket='Missing'))
        for mutate in mutations:
            doc = device(); mutate(doc); self.assertTrue(self.check(doc))

    def test_additive_untyped_reference_values_report_errors_instead_of_crashing(self):
        for bad in (False, 42, [], {}):
            doc = device(); doc['renderers'][0]['parent'] = bad
            self.assertTrue(self.check(doc))
            doc = device(); doc['hold']['eye_socket'] = bad
            self.assertTrue(self.check(doc))
            doc = device(); doc['colliders'][0]['part'] = bad
            self.assertTrue(ce_contract.device_references(doc))

    def test_reticle_socket_resolves_in_existing_graph_for_both_device_formats(self):
        for version in (4, 5):
            for target in (None, 'Socket_Hand', 'Bolt', 'Missing', False, 42, [], {}):
                with self.subTest(version=version, target=target):
                    doc = device()
                    doc['format'] = version
                    doc['hold']['reticle_socket'] = target
                    errors = self.check(doc)
                    if target is None or target in ('Socket_Hand', 'Bolt'):
                        self.assertEqual(errors, [])
                    else:
                        self.assertTrue(any('$.hold.reticle_socket' in error for error in errors), errors)

    def test_long_graph_does_not_depend_on_python_recursion_depth(self):
        doc = device()
        doc['nodes'].extend({'name': f'N{i}', 'parent': 'Device' if i == 0 else f'N{i - 1}'} for i in range(1500))
        self.assertEqual(self.check(doc), [])

    def assembly(self):
        doc = device(); doc['format'] = 5
        doc['assembly'] = [{'name': 'Rig', 'parent': None}, {'name': 'Prop_R', 'parent': 'Rig'},
                           {'name': 'Device', 'parent': 'Prop_R'}, {'name': 'Bolt', 'parent': 'Device'},
                           {'name': 'Socket_Hand', 'parent': 'Bolt'},
                           {'name': 'LeftRoot', 'parent': 'Rig'}, {'name': 'HandMag_L', 'parent': 'LeftRoot'}]
        doc['renderers'][0]['parent'] = 'HandMag_L'
        return doc

    def test_format_five_left_hand_objects_resolve_through_declared_assembly(self):
        self.assertEqual(self.check(self.assembly()), [])
        doc = device(); doc['format'] = 5; doc['renderers'][0]['parent'] = 'HandMag_L'
        self.assertTrue(self.check(doc))  # never a magic known name

    def test_assembly_graph_is_typed_and_checks_duplicate_names_parents_cycles_and_conflicts(self):
        mutations = (lambda d: d['assembly'].append(copy.deepcopy(d['assembly'][0])),
                     lambda d: d['assembly'][0].update(parent='HandMag_L'),
                     lambda d: d['assembly'][1].update(parent='Missing'),
                     lambda d: d['assembly'][3].update(parent='Rig'),
                     lambda d: d['assembly'][1].update(parent=[]),
                     lambda d: d['assembly'].append(None),
                     lambda d: d.update(assembly={}))
        for mutate in mutations:
            doc = self.assembly(); mutate(doc); self.assertTrue(self.check(doc))


class ExistingExportTests(unittest.TestCase):
    def test_original_toolgen_world_fps_robot_variant_and_v5_assembly_graphs_are_valid(self):
        for group, filename in [('toolgen_world', 'unity_contract.json'),
                                ('toolgen_fps', 'unity_contract_fps.json'),
                                ('toolgen_robot_variant', 'unity_contract_robot.json'),
                                ('toolgen_assembly_v5', 'unity_contract_fps.json')]:
            with self.subTest(export=group):
                path = EXPORT_FIXTURES / group / filename
                document = ce_contract.read_json(path)
                self.assertEqual(document['generator'], 'toolgen')
                self.assertRegex(document['_fixture_source']['sha256'], r'^[0-9a-f]{64}$')
                self.assertEqual(ce_contract.check_file(path, None), ('unity_contract.v4', []))
                # Mutation remains observable for every real producer shape.
                document['anchors'][0]['parent'] = 'UndeclaredParent'
                self.assertTrue(ce_contract.device_references(document))

    def test_real_assembly_declares_left_hand_magazine_outside_device_nodes(self):
        document = ce_contract.read_json(EXPORT_FIXTURES / 'toolgen_assembly_v5/unity_contract_fps.json')
        self.assertNotIn('HandMag_L', {entry['name'] for entry in document['nodes']})
        self.assertIn('HandMag_L', {entry['name'] for entry in document['assembly']})
        self.assertTrue(any(entry.get('parent') == 'HandMag_L' for entry in document['renderers']))
        del document['assembly']
        self.assertTrue(ce_contract.device_references(document))

    def test_robogen_export_uses_its_existing_consumer_validator_not_toolgen_schema(self):
        path = EXPORT_FIXTURES / 'robogen/robot_config.json'
        document = ce_contract.read_json(path)
        self.assertEqual(document['generator'], '1.21.0')
        self.assertEqual(ce_interfaces.check_robot_config(document), [])
        # _robot suffixes in Toolgen filenames are equipment variants, not Robogen.
        with self.assertRaises(FileNotFoundError):
            ce_contract.schema_for(document, path)
        schema = ce_contract.load_schema('unity_contract.v4')
        self.assertTrue(ce_contract.validate(document, schema, schema))
        del document['clips']['Walk']
        self.assertIn('clips: missing CE clip Walk', ce_interfaces.check_robot_config(document))


if __name__ == '__main__':
    unittest.main()
