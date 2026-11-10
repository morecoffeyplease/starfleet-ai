import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
SOURCE_NAMES = ("replicator.py", "install.py", "guide.md", "README.md")


class ReplicatorCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="replicator-test-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "copied-source"
        self.source.mkdir()
        for name in SOURCE_NAMES:
            shutil.copyfile(ROOT / name, self.source / name)
        self.home = self.root / "empty-home"
        self.home.mkdir()
        self.bin_dir = self.root / "bin"
        self.runtime_root = self.root / "runtime"
        self.unrelated = self.root / "unrelated-cwd"
        self.unrelated.mkdir()
        self.environment = {
            "HOME": str(self.home),
            "PATH": "",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def run_process(self, command, cwd=None):
        return subprocess.run(
            command,
            cwd=cwd or self.unrelated,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def tree_snapshot(self):
        result = {}
        for path in sorted(self.root.rglob("*")):
            relative = str(path.relative_to(self.root))
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                result[relative] = ("symlink", os.readlink(path), mode)
            elif path.is_dir():
                result[relative] = ("directory", mode)
            else:
                result[relative] = ("file", path.read_bytes(), mode)
        return result

    def install_copy(self):
        result = self.run_process([
            sys.executable,
            str(self.source / "install.py"),
            "--runtime-root",
            str(self.runtime_root),
            "--bin-dir",
            str(self.bin_dir),
        ])
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return json.loads(result.stdout)

    def test_installed_cli_prints_the_packaged_guide_without_writes(self):
        expected_guide = (self.source / "guide.md").read_bytes()
        installation = self.install_copy()
        runtime = Path(installation["runtime"])
        self.assertEqual((runtime / "guide.md").read_bytes(), expected_guide)
        self.assertEqual((runtime / "README.md").read_bytes(), (self.source / "README.md").read_bytes())
        self.assertEqual((stat.S_IMODE((runtime / "guide.md").stat().st_mode) & 0o222), 0)

        second_install = self.install_copy()
        self.assertEqual(second_install["runtime"], installation["runtime"])
        self.assertEqual((runtime / "guide.md").read_bytes(), expected_guide)

        shutil.rmtree(self.source)
        launcher = Path(installation["command"])
        before = self.tree_snapshot()
        help_result = self.run_process([str(launcher), "--help"])
        guide_result = self.run_process([str(launcher), "guide", "--print"])
        after = self.tree_snapshot()

        self.assertEqual(help_result.returncode, 0, help_result.stderr.decode())
        self.assertIn(b"usage: replicator", help_result.stdout)
        self.assertIn(b"guide", help_result.stdout)
        self.assertEqual(guide_result.returncode, 0, guide_result.stderr.decode())
        self.assertEqual(guide_result.stdout, expected_guide)
        self.assertEqual(before, after)

    def test_invalid_arguments_fail_without_filesystem_writes(self):
        installation = self.install_copy()
        before = self.tree_snapshot()

        result = self.run_process([installation["command"], "unknown"])

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.stderr)
        self.assertEqual(before, self.tree_snapshot())

    def test_installer_preserves_an_unowned_launcher(self):
        self.bin_dir.mkdir()
        launcher = self.bin_dir / "replicator"
        launcher.write_bytes(b"#!/bin/sh\necho owned by another tool\n")
        before = self.tree_snapshot()

        result = self.run_process([
            sys.executable,
            str(self.source / "install.py"),
            "--runtime-root",
            str(self.runtime_root),
            "--bin-dir",
            str(self.bin_dir),
        ])

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(launcher.read_bytes(), b"#!/bin/sh\necho owned by another tool\n")
        self.assertFalse(self.runtime_root.exists())
        self.assertEqual(before, self.tree_snapshot())


if __name__ == "__main__":
    unittest.main()
