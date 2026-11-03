import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('away_install', SOURCE / 'apps/away-mode/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def test_installed_guide_cli_survives_source_removal_without_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source checkout"
            names = (*installer.FILES, "apps/away-mode/install.py")
            for name in names:
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(SOURCE / name, target)
            expected_guide = (source / "apps/away-mode/guide.md").read_text(encoding="utf-8")
            home, state = root / "home", root / "state"
            env = {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_STATE_HOME": str(state),
                "TMPDIR": str(root),
                "PATH": os.defpath,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            install_command = [
                sys.executable,
                str(source / "apps/away-mode/install.py"),
                "--share-dir", str(root / "share"),
                "--bin-dir", str(root / "bin"),
            ]
            installed = subprocess.run(install_command, cwd=root, env=env,
                                       capture_output=True, text=True, timeout=20)
            self.assertEqual(
                installed.returncode, 0,
                f"installer CLI failed: {install_command!r}\n"
                f"stdout:\n{installed.stdout}\nstderr:\n{installed.stderr}",
            )
            launcher = root / "bin/away-mode"
            self.assertTrue(launcher.is_file())
            shutil.rmtree(source)

            guide_command = [str(launcher), "guide", "--print"]
            guide = subprocess.run(guide_command, cwd=root, env=env,
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(
                guide.returncode, 0,
                f"installed guide CLI failed: {guide_command!r}\n"
                f"stdout:\n{guide.stdout}\nstderr:\n{guide.stderr}",
            )
            self.assertEqual(guide.stdout.rstrip("\n"), expected_guide.rstrip("\n"))
            self.assertIn("no personal-name defaults", guide.stdout)
            self.assertFalse(state.exists())
            self.assertFalse(home.exists())

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
