import json
import tempfile
import unittest
from pathlib import Path

from portable_config import (
    BackupError,
    _safe_path,
    create_backup,
    read_backup,
    restore_backup,
)


class PortableConfigTests(unittest.TestCase):
    def deployment(self, root: Path) -> None:
        (root / "multi-camera.json").write_text(json.dumps({
            "home_assistant": {"mqtt": {"password_env": "HA_MQTT_PASSWORD"}},
            "cameras": [{"name": "garage", "config": "garage-monitor.json"}],
        }), encoding="utf-8")
        (root / "garage-monitor.json").write_text(json.dumps({
            "camera": {
                "credential_prefix": "CAMERA_GARAGE",
                "rtsp_env": "CAMERA_GARAGE_RTSP_URL",
            },
            "telegram": {
                "token_env": "TELEGRAM_BOT_TOKEN",
                "chat_id_env": "TELEGRAM_CHAT_ID",
            },
        }), encoding="utf-8")
        (root / ".security-cam-state.json").write_text(
            '{"mode":"disarmed"}', encoding="utf-8",
        )
        (root / "home-assistant").mkdir()
        (root / "home-assistant" / "security_cam_package.yaml").write_text(
            "automation: []\n", encoding="utf-8",
        )
        (root / "garage-monitor.example.json").write_text("{}", encoding="utf-8")
        (root / "garage-monitor_ old.json").write_text("{}", encoding="utf-8")

    def test_encrypted_round_trip_hides_and_restores_files_and_secrets(self):
        with tempfile.TemporaryDirectory() as source_folder, tempfile.TemporaryDirectory() as target_folder:
            source = Path(source_folder)
            target = Path(target_folder)
            self.deployment(source)
            backup = source / "backup.scbackup"
            environment = {
                "TELEGRAM_BOT_TOKEN": "123456:very-secret-token",
                "HA_MQTT_PASSWORD": "mqtt-secret",
            }

            result = create_backup(
                source, backup, "correct horse battery staple", environment,
            )

            raw = backup.read_bytes()
            self.assertNotIn(b"very-secret-token", raw)
            self.assertNotIn(b"mqtt-secret", raw)
            self.assertNotIn("garage-monitor.example.json", result["files"])
            self.assertNotIn("garage-monitor_ old.json", result["files"])

            manifest, files, restored_environment = read_backup(
                backup, "correct horse battery staple",
            )
            self.assertEqual(manifest["format"], 1)
            self.assertEqual(restored_environment, environment)
            self.assertIn("garage-monitor.json", files)

            restored = restore_backup(
                target, backup, "correct horse battery staple",
                apply=True, files_only=True,
            )
            self.assertTrue(restored["applied"])
            self.assertEqual(
                (target / "garage-monitor.json").read_bytes(),
                (source / "garage-monitor.json").read_bytes(),
            )
            self.assertEqual(restored["environment_restored"], [])

    def test_wrong_password_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.deployment(root)
            backup = root / "backup.scbackup"
            create_backup(root, backup, "correct horse battery staple", {})
            with self.assertRaisesRegex(BackupError, "Incorrect password"):
                read_backup(backup, "definitely the wrong password")

    def test_restore_refuses_different_existing_file_without_force(self):
        with tempfile.TemporaryDirectory() as source_folder, tempfile.TemporaryDirectory() as target_folder:
            source = Path(source_folder)
            target = Path(target_folder)
            self.deployment(source)
            backup = source / "backup.scbackup"
            create_backup(source, backup, "correct horse battery staple", {})
            (target / "multi-camera.json").write_text("different", encoding="utf-8")

            with self.assertRaisesRegex(BackupError, "Existing files differ"):
                restore_backup(
                    target, backup, "correct horse battery staple",
                    apply=True, files_only=True,
                )

    def test_archive_path_validation_rejects_traversal(self):
        with self.assertRaises(BackupError):
            _safe_path("../outside.json")
        with self.assertRaises(BackupError):
            _safe_path("unexpected.txt")


if __name__ == "__main__":
    unittest.main()
