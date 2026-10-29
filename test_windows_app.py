import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from windows_app import find_project_root, monitor_command


class WindowsAppTests(unittest.TestCase):
    def test_finds_explicit_project_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "multi_camera_monitor.py").touch()
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(find_project_root(root), root.resolve())

    def test_monitor_command_uses_project_venv_and_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            python = root / "venv" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.touch()

            command = monitor_command(root)

            self.assertEqual(command[0], str(python))
            self.assertEqual(command[-1], str(root / "multi-camera.json"))


if __name__ == "__main__":
    unittest.main()
