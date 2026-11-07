import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from windows_app import (
    SecurityCamApp, alarm_policy_controller, camera_endpoints, find_project_root,
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

    def test_unexpected_monitor_exit_schedules_restart(self):
        app = SecurityCamApp.__new__(SecurityCamApp)
        app.process = MagicMock(returncode=9)
        app.process.poll.return_value = 9
        log_handle = MagicMock()
        app.log_handle = log_handle
        app.keep_monitor_running = True
        app.restart_pending = False
        app.root = MagicMock()
        app.set_status = MagicMock()
        app.refresh_log = MagicMock()

        app.poll()

        log_handle.close.assert_called_once_with()
        app.set_status.assert_called_once_with(
            "Monitor finalizó · código 9 · reinicio en 5 s"
        )
        self.assertTrue(app.restart_pending)
        app.root.after.assert_any_call(5000, app._restart_monitor)
        app.root.after.assert_any_call(1000, app.poll)

    def test_requested_stop_disables_restart(self):
        app = SecurityCamApp.__new__(SecurityCamApp)
        app.keep_monitor_running = True
        app.restart_pending = True
        app.set_status = MagicMock()

        with patch("windows_app.threading.Thread") as thread:
            app.stop_monitor()

        self.assertFalse(app.keep_monitor_running)
        self.assertFalse(app.restart_pending)
        thread.assert_called_once()


if __name__ == "__main__":
    unittest.main()
