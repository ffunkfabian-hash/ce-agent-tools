"""Negative probes for shared contract guards; fixtures describe protocol shapes, not a model build."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1] / 'plugins/ce-shared/tools'
sys.path.insert(0, str(TOOLS))
import ce_contract


def arms():
    node = {'name': 'Root', 'parent': None,
            'blender': {'location': [0, 0, 0], 'rotation_wxyz': [1, 0, 0, 0], 'scale': [1, 1, 1]},
            'unity': {'localPosition': [0, 0, 0], 'localRotation': [0, 0, 0, 1], 'localScale': [1, 1, 1]}}
    side = {'fingers': [['Root']], 'thumb': ['Root'], 'trigger_finger': 'Root'}
    return {'format': 5, 'kind': 'ego_arms', 'generator': 'toolgen', 'version': '0.19.0 Kinetic',
            'asset': 'Arms', 'model': 'Arms_Game', 'root': 'Root', 'units': 'meter', 'scale': 1,
            'axes': {'blender': {'up': '+Z', 'forward': '-Y'}, 'unity': {'up': '+Y', 'forward': '+Z'},
                     'fbx': {'axis_forward': '-Z', 'axis_up': 'Y', 'bake_space_transform': False}},
            'rig': {'id': 'ego_arms', 'version': 2, 'hash': 'a' * 64}, 'nodes': [node],
            'chains': {'R': ['Root'], 'L': ['Root']}, 'fingers': {'R': side, 'L': copy.deepcopy(side)},
            'renderers': [{'name': 'Mesh', 'node': 'Root', 'parts': ['Forearm'], 'materials': ['M_Metal'], 'triangles': 12, 'renderer': 'MeshRenderer'}],
            'materials': {'M_Metal': {'color_srgb': [.5, .5, .5], 'metallic': 0, 'roughness': .5, 'emission': 0}},
            'files': {kind: {'path': 'arms.' + kind, 'sha256': 'b' * 64} for kind in ['fbx', 'glb']}}


class ContractAuditTests(unittest.TestCase):
    def check(self, document, content=None, schema_name=None):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'unity_contract_arms.json'
            path.write_text(content if content is not None else json.dumps(document))
            return ce_contract.check_file(path, schema_name)[1]

    def test_realistic_arm_shape_and_additive_metadata_remain_valid(self):
        good = arms(); good['future_note'] = {'source': 'additional metadata'}
        good['renderers'][0]['parts'] = []  # a finger mesh is attached to its own node, without source part names
        self.assertEqual(self.check(good), [])

    def test_wrong_arm_types_and_file_references_are_rejected(self):
        for field, bad in [('version', False), ('asset', None), ('model', 5), ('nodes', [42]),
                           ('chains', {'R': False, 'L': None}), ('fingers', {'R': 1, 'L': False}),
                           ('renderers', [None]), ('materials', {'M_Metal': False}),
                           ('files', {'fbx': False, 'glb': True})]:
            with self.subTest(field=field):
                doc = arms(); doc[field] = bad
                self.assertTrue(self.check(doc), field)

    def test_axes_and_transforms_are_typed(self):
        for mutate in [lambda d: d['axes'].update(blender=None),
                       lambda d: d['axes'].update(unity=False),
                       lambda d: d['nodes'][0]['unity'].update(localRotation=[0, 0]),
                       lambda d: d['fingers']['R'].update(thumb=[42])]:
            doc = arms(); mutate(doc); self.assertTrue(self.check(doc))

    def test_axis_objects_in_the_device_contract_cannot_be_null_or_boolean(self):
        schema = ce_contract.load_schema('unity_contract.v4')
        axes = schema['properties']['axes']
        for value in [{'blender': None, 'unity': False, 'fbx': {'export_yaw_degrees': 180}},
                      {'blender': {}, 'unity': {}, 'fbx': {'export_yaw_degrees': 180}}]:
            self.assertTrue(ce_contract.validate(value, axes, schema))

    def test_arm_links_must_resolve_and_node_names_are_unique(self):
        for mutate in [lambda d: d['chains']['R'].append('Missing'),
                       lambda d: d['renderers'][0]['materials'].append('Missing'),
                       lambda d: d['nodes'].append(copy.deepcopy(d['nodes'][0])),
                       lambda d: d['nodes'][0].update(parent='Root'),
                       lambda d: d['nodes'][0].update(parent='Missing')]:
            doc = arms(); mutate(doc); self.assertTrue(self.check(doc))

    def test_explicit_schema_filename_keeps_reference_checks(self):
        doc = arms(); doc['chains']['R'].append('Missing')
        self.assertTrue(self.check(doc, schema_name='unity_contract_arms.v5.json'))

    def test_nonfinite_numbers_are_never_valid_schema_numbers(self):
        schema = {'type': 'number', 'minimum': 0}
        for value in [float('nan'), float('inf'), -float('inf')]:
            with self.subTest(value=value):
                self.assertTrue(ce_contract.validate(value, schema, schema))

    def test_invalid_json_numbers_duplicate_keys_and_root_type_are_rejected(self):
        for content in ['{"format":5,"format":4}', '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e400}', '[]', 'null']:
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.check(None, content)

    def test_boolean_does_not_match_numeric_enum_or_const(self):
        for schema in [{'enum': [0, 1]}, {'const': 1}]:
            self.assertTrue(ce_contract.validate(True, schema, schema))
        self.assertEqual(ce_contract.validate(1.0, {'const': 1}, {}), [])


if __name__ == '__main__':
    unittest.main()
