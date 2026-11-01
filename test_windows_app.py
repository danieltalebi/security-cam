import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from windows_app import (
    alarm_policy_controller, camera_endpoints, find_project_root,
    monitor_command, parse_dvrip_log,
)


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

    def test_camera_endpoints_use_host_environment_override(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "multi-camera.json").write_text(
                '{"cameras":[{"name":"garage","config":"garage.json"}]}',
                encoding="utf-8",
            )
            (root / "garage.json").write_text(
                '{"camera":{"credential_prefix":"CAMERA_GARAGE","host":"{host}"},'
                '"dvrip":{"host":"{host}","port":34567}}',
                encoding="utf-8",
            )
            self.assertEqual(
                camera_endpoints(root, {"CAMERA_GARAGE_HOST": "192.168.100.193"}),
                [("garage", "192.168.100.193", 34567)],
            )

    def test_parse_dvrip_log_uses_latest_state_per_camera(self):
        text = "\n".join((
            "garage: DVRIP alarm listener connected.",
            "backyard: DVRIP listener reconnecting: TimeoutError",
            "backyard: DVRIP alarm listener connected.",
        ))
        self.assertEqual(
            parse_dvrip_log(text, ["garage", "backyard"]),
            {"garage": "online", "backyard": "online"},
        )

    def test_windows_controls_share_the_configured_local_state_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "multi-camera.json").write_text(
                '{"alarm_policy":{"state_file":"runtime-state.json"}}',
                encoding="utf-8",
            )
            first = alarm_policy_controller(root)
            first.set_manual("armed_away")
            second = alarm_policy_controller(root)
            self.assertEqual(second.snapshot()["mode"], "armed_away")
            self.assertEqual(second.state_file, root / "runtime-state.json")


if __name__ == "__main__":
    unittest.main()
