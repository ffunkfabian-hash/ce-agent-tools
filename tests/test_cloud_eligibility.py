"""Fail-closed cloud declarations; no test contacts a cloud account."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('eligibility_cloud', Path(__file__).resolve().parents[1] / 'plugins/ce-shared/tools/ce_codex_cloud.py')
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)


def manifest_for(prompt):
    return {'manifest_version': 1, 'kind': 'pure_logic',
            'prompt_sha256': hashlib.sha256(prompt.read_bytes()).hexdigest(),
            'required_capabilities': {'unity': False, 'blender': False, 'visuals': False, 'viewer': False},
            'scopes': [{'path': 'logic.py', 'kind': 'logic_code'}]}


class CloudEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        prompt = self.root / 'prompt.txt'; prompt.write_text('Fix the arithmetic function.')
        self.path = self.root / 'eligibility.json'
        self.manifest = manifest_for(prompt)
        self.save()
        self.args = SimpleNamespace(repo=self.root, prompt_file=prompt, eligibility_file=self.path,
            branch='codex/logic', model='gpt-6-luna', effort='low', reason='Logik', env='environment',
            push_approved=True, dry_run=True)

    def save(self):
        self.path.write_text(json.dumps(self.manifest))

    def check(self):
        self.save()
        return cloud.eligibility(self.path, self.root, self.args.prompt_file.read_bytes())

    def test_valid_declaration_dry_run_shows_decision_without_prompt_or_execution(self):
        output = io.StringIO()
        with patch.object(cloud, 'run') as run, contextlib.redirect_stdout(output):
            cloud.submit(self.args)
        run.assert_not_called()
        self.assertIn('"eligible": true', output.getvalue())
        self.assertIn('Umgebung/Login/Zielrepo im Dry-Run ungeprüft', output.getvalue())
        self.assertNotIn('Fix the arithmetic function.', output.getvalue())

    def test_missing_manifest_cannot_submit_or_dry_run(self):
        for dry_run in (True, False):
            self.args.eligibility_file = None; self.args.dry_run = dry_run
            with patch.object(cloud, 'run') as run, self.assertRaisesRegex(ValueError, 'Eignungsnachweis fehlt'):
                cloud.submit(self.args)
            run.assert_not_called()

    def test_unknown_classification_version_and_fields_are_rejected(self):
        for field, value in [('kind', 'unity'), ('kind', 'unknown'), ('manifest_version', 2),
                             ('manifest_version', True), ('new_option', False), ('scopes', []),
                             ('required_capabilities', {})]:
            with self.subTest(field=field, value=value):
                original = manifest_for(self.args.prompt_file)
                self.manifest = {**original, field: value}
                with self.assertRaises(ValueError): self.check()

    def test_every_capability_must_be_explicit_boolean_false(self):
        for capability in cloud.CAPABILITIES:
            for bad in (True, 0, None, 'false'):
                self.manifest = manifest_for(self.args.prompt_file)
                self.manifest['required_capabilities'][capability] = bad
                with self.assertRaises(ValueError): self.check()
        self.manifest = manifest_for(self.args.prompt_file)
        self.manifest['required_capabilities']['network'] = False
        with self.assertRaises(ValueError): self.check()

    def test_prompt_hash_binds_the_unmodified_file_bytes(self):
        self.args.prompt_file.write_text('Changed task.\n')
        with self.assertRaisesRegex(ValueError, 'prompt_sha256'): self.check()

    def test_unsafe_paths_unknown_filetypes_and_engine_areas_fail_closed(self):
        bad_paths = ['../logic.py', '/tmp/logic.py', 'a/../logic.py', 'a//logic.py', './logic.py',
                     'a\\logic.py', 'a/*.py', 'a/?.py', 'a/[x].py', 'a/{x}.py', 'C:/logic.py',
                     '.git/config.py', '.GiT/config.py', '.github/workflows/build.yaml', 'image.png', 'scene.unity',
                     'model.blend', 'viewer.html', 'ProjectSettings/config.json', 'Assets/Art/model.json',
                     'Library/cache.py', 'logic.py\n', 'logic.py\x00', '.', '']
        for name in bad_paths:
            with self.subTest(name=name):
                self.manifest['scopes'] = [{'path': name, 'kind': 'logic_code'}]
                with self.assertRaises(ValueError): self.check()

    def test_known_scope_types_allow_existing_and_new_files(self):
        for kind, name in [('logic_code', 'src/new.py'), ('logic_test', 'Assets/Tests/EditMode/LogicTests.cs'),
                           ('logic_data', 'data/rules.json'), ('documentation', 'docs/LOGIC.md')]:
            self.manifest['scopes'] = [{'path': name, 'kind': kind}]
            self.assertEqual(self.check()['scopes'][0]['path'], name)
        (self.root / 'logic.py').write_text('pass')
        self.manifest['scopes'] = [{'path': 'logic.py', 'kind': 'logic_code'}]
        self.assertTrue(self.check())

    def test_directory_file_parent_symlink_escape_and_in_repo_alias_are_rejected(self):
        (self.root / 'directory.py').mkdir()
        (self.root / 'file.py').write_text('pass')
        (self.root / 'inside').symlink_to(self.root, target_is_directory=True)
        (self.root / 'escape').symlink_to(self.root.parent, target_is_directory=True)
        (self.root / 'alias.py').symlink_to(self.root / 'file.py')
        (self.root / 'broken.py').symlink_to(self.root / 'missing')
        for name in ('directory.py', 'file.py/child.py', 'inside/file.py', 'escape/out.py', 'alias.py', 'broken.py'):
            self.manifest['scopes'] = [{'path': name, 'kind': 'logic_code'}]
            with self.assertRaises(ValueError): self.check()

    def test_scopes_cannot_be_unknown_untyped_or_duplicated(self):
        for scopes in ([{'path': 'logic.py', 'kind': 'engine'}], [{'path': 'logic.py', 'kind': 'logic_code', 'read_only': True}],
                       [{'path': 7, 'kind': 'logic_code'}], [{'path': 'logic.py', 'kind': []}],
                       [{'path': 'logic.py', 'kind': 'logic_code'}] * 2, [None], {}, None):
            self.manifest['scopes'] = scopes
            with self.assertRaises(ValueError): self.check()

    def test_agent_configuration_ci_and_nested_unity_assets_are_not_logic_scopes(self):
        for path in ('.claude/settings.json', '.codex/config.toml', '.gemini/config.json', '.ce-repo.json',
                     '.gitlab-ci.yml', '.circleci/config.yml', 'sub/.circleci/config.yml',
                     'Unity/Packages/manifest.json', 'Unity/Packages/packages-lock.json',
                     'Unity/Assets/Art/model.json', 'azure-pipelines.yml', 'bitbucket-pipelines.yml'):
            with self.subTest(path=path):
                self.manifest['scopes'] = [{'path': path, 'kind': 'logic_data'}]
                with self.assertRaises(ValueError): self.check()

    def test_nested_unity_project_is_detected_by_project_settings(self):
        (self.root / 'nested/Game/ProjectSettings').mkdir(parents=True)
        for path in ('nested/Game/Packages/rules.json', 'nested/Game/Library/cache.json',
                     'nested/Game/Temp/log.py', 'nested/Game/ProjectSettings/settings.json'):
            with self.subTest(path=path):
                self.manifest['scopes'] = [{'path': path, 'kind': 'logic_data' if path.endswith('.json') else 'logic_code'}]
                with self.assertRaises(ValueError): self.check()
        # General source packages stay available outside Unity projects.
        self.manifest['scopes'] = [{'path': 'src/packages/math.py', 'kind': 'logic_code'}]
        self.assertTrue(self.check())

    def test_ambiguous_json_and_nonfinite_numbers_fail(self):
        for content in ('[]', 'null', '{"kind":"pure_logic","kind":"other"}', '{"x":NaN}'):
            self.path.write_text(content)
            with self.assertRaises(ValueError): cloud.eligibility(self.path, self.root, b'')

    def submit_responses(self):
        return [SimpleNamespace(stdout=''), SimpleNamespace(stdout='abc'), SimpleNamespace(stdout=''),
                SimpleNamespace(stdout='abc refs/heads/codex/logic'), SimpleNamespace(stdout='')]

    def test_missing_environment_stops_before_cloud_exec(self):
        self.args.dry_run = False
        responses = self.submit_responses() + [subprocess.CalledProcessError(1, 'codex', stderr='no cloud environments are available for this workspace')]
        with patch.object(cloud, 'run', side_effect=responses) as run:
            with self.assertRaisesRegex(ValueError, 'keine Umgebung'): cloud.submit(self.args)
        self.assertFalse(any(call.args[0][:3] == ['codex', 'cloud', 'exec'] for call in run.call_args_list))

    def test_target_environment_access_failure_stops_before_cloud_exec(self):
        self.args.dry_run = False
        probe = subprocess.CalledProcessError(1, 'codex', stderr="environment 'ce-doctor-environment-probe' not found; run `codex cloud` to list available environments")
        responses = self.submit_responses() + [probe, subprocess.CalledProcessError(1, 'codex', stderr='HTTP 403')]
        with patch.object(cloud, 'run', side_effect=responses) as run:
            with self.assertRaises(subprocess.CalledProcessError): cloud.submit(self.args)
        self.assertFalse(any(call.args[0][:3] == ['codex', 'cloud', 'exec'] for call in run.call_args_list))

    def test_submit_failure_does_not_echo_the_private_prompt(self):
        self.args.dry_run = False
        responses = self.submit_responses() + [SimpleNamespace(stdout=''), SimpleNamespace(stdout=''),
            subprocess.CalledProcessError(1, ['codex', 'cloud', 'exec', self.args.prompt_file.read_text()])]
        with patch.object(cloud, 'run', side_effect=responses):
            with self.assertRaisesRegex(ValueError, 'kein automatischer Neuversuch') as caught:
                cloud.submit(self.args)
        self.assertNotIn(self.args.prompt_file.read_text(), str(caught.exception))


if __name__ == '__main__':
    unittest.main()
