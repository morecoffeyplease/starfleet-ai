import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('away_install', SOURCE / 'apps/away-mode/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def test_installed_guide_survives_source_removal_without_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            for name in installer.FILES:
                (source / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(SOURCE / name, source / name)
            result = installer.install(source, root / 'share', root / 'bin')
            again = installer.install(source, root / 'share', root / 'bin')
            self.assertEqual(result, again)
            shutil.rmtree(source)
            env = dict(os.environ, HOME=str(root / 'home'), XDG_STATE_HOME=str(root / 'state'),
                       PYTHONDONTWRITEBYTECODE='1')
            run = subprocess.run([result['launcher'], 'guide', '--print'], cwd=root,
                                 env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn('no personal-name defaults', run.stdout)
            self.assertFalse((root / 'state').exists())
            self.assertFalse((root / 'home').exists())

    def test_unowned_launcher_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'bin').mkdir()
            launcher = root / 'bin/away-mode'; launcher.write_text('unowned')
            with self.assertRaises(RuntimeError):
                installer.install(SOURCE, root / 'share', root / 'bin')
            self.assertEqual(launcher.read_text(), 'unowned')

if __name__ == '__main__': unittest.main()
