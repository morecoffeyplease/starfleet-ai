import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts import affected, verify


class AffectedVerificationTests(unittest.TestCase):
    def test_dependency_map_covers_all_maintained_commands(self):
        self.assertEqual({label for checks in affected.COMPONENT_CHECKS.values() for label in checks},
                         {label for label, _ in verify.PYTHON_CHECKS})
        self.assertTrue(set(affected.DEPENDENTS) <= set(affected.COMPONENT_CHECKS))
        self.assertTrue(all(dependents <= set(affected.COMPONENT_CHECKS)
                            for dependents in affected.DEPENDENTS.values()))

    def test_docs_have_explicit_unaffected_runtime_verdict(self):
        self.assertEqual(affected.select_components(['README.md', 'apps/replicator/guide.md']),
                         (set(), 'runtime checks unaffected'))

    def test_component_tests_include_transitive_dependents(self):
        selected, _ = affected.select_components(['apps/opencode-adapter/tests/test_adapter.py'])
        self.assertTrue({'opencode-adapter', 'model-preferences', 'away-mode',
                         'hailing-frequencies', 'discord-watch', 'warp-drive',
                         'pattern-buffer', 'context-injector', 'replicator',
                         'starfleet-policy-guard', 'starfleet'} <= selected)
        self.assertNotIn('starfleet-markup', selected)

    def test_shared_and_unclassified_paths_keep_broad_proof(self):
        for path in ['scripts/verify.py', '.github/workflows/verify.yml', '.gitleaksignore',
                     'apps/future-tool/runtime.py', 'apps/pattern-buffer/hooks.py',
                     'apps/replicator/replicator.py', 'apps/warp-drive/warp_drive.py']:
            with self.subTest(path=path):
                self.assertEqual(affected.select_components([path])[0], set(affected.COMPONENT_CHECKS))

    def test_markup_change_does_not_select_unrelated_runtime(self):
        self.assertEqual(affected.select_components(['apps/starfleet-markup/src/server.ts'])[0],
                         {'starfleet-markup', 'starfleet'})

    def test_empty_python_matrix_requires_explicit_unaffected_result(self):
        self.assertEqual(verify.python_component_matrix(set()), [])
        self.assertTrue(verify.python_verification_succeeded('skipped', 'success', required=False))
        for result in ['failure', 'cancelled', 'success']:
            self.assertFalse(verify.python_verification_succeeded(result, 'success', required=False))
        self.assertFalse(verify.python_verification_succeeded('skipped', 'failure', required=False))

    def test_real_cli_docs_only_and_broken_link_without_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts/tests').mkdir(parents=True)
            for name in ['verify.py', 'affected.py', 'check_docs.py']:
                shutil.copyfile(verify.ROOT / 'scripts' / name, root / 'scripts' / name)
            (root / 'scripts/tests/test_verify.py').write_text(
                'import unittest\nclass FixtureTest(unittest.TestCase):\n'
                '    def test_fixture(self): self.assertTrue(True)\n')
            (root / '.gitignore').write_text('__pycache__/\n')
            (root / 'README.md').write_text('# Fixture\n')
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.STDOUT)
            git('init', '-q')
            git('add', '.')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                'commit', '-qm', 'fixture baseline')
            base = git('rev-parse', 'HEAD').decode().strip()
            (root / 'README.md').write_text('# Fixture\n\nDocumentation changed.\n')
            command = [sys.executable, 'scripts/verify.py', '--base', base]
            plan = subprocess.run([*command, '--plan'], cwd=root, capture_output=True, text=True)
            self.assertEqual(plan.returncode, 0, plan.stderr)
            self.assertEqual(json.loads(plan.stdout)['python_checks'], [])
            result = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('runtime checks unaffected', result.stdout)
            (root / 'README.md').write_text('[Missing](missing.md)\n')
            broken = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertEqual(broken.returncode, 1)
            self.assertIn('missing local link', broken.stdout)
            (root / 'README.md').write_text('/Users/' + 'private-owner/Documents/fixture' + '\n')
            private = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertEqual(private.returncode, 1)
            self.assertIn('nonportable path', private.stdout)
            (root / 'unclassified.py').write_text('')
            full = subprocess.run([*command, '--plan'], cwd=root, capture_output=True, text=True)
            self.assertEqual(set(json.loads(full.stdout)['components']), set(affected.COMPONENT_CHECKS))


if __name__ == '__main__':
    unittest.main()
