import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from camera_media import (
    CameraMediaCommands,
    CameraMediaSource,
    configured_media_sources,
    normalized_snapshot,
)
from telegram_notify import TelegramNotifier


def source(name, lens=None):
    return CameraMediaSource(
        name=name,
        snapshot_url=f"http://{name}.invalid/snapshot.jpg",
        username="user",
        password="password",
        query_auth=False,
        stream=f"rtsp://{name}.invalid/stream",
        lens=lens,
        layout_size=(100, 100) if lens else None,
    )


class CameraMediaTests(unittest.TestCase):
    def test_garage_photo_selects_ptz_lens_and_reencodes_jpeg(self):
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        frame[:50] = (0, 0, 255)
        frame[50:] = (0, 255, 0)
        ok, vendor_jpeg = cv2.imencode(".jpg", frame)
        self.assertTrue(ok)
        garage = source("Garage", (0, 50, 100, 100))

        with patch("camera_media.download_snapshot", return_value=vendor_jpeg.tobytes()):
            result = normalized_snapshot(garage)

        decoded = cv2.imdecode(np.frombuffer(result, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape[:2], (50, 100))
        self.assertGreater(float(decoded[:, :, 1].mean()), 240)
        self.assertLess(float(decoded[:, :, 2].mean()), 15)

    def test_dual_lens_garage_builds_both_photo_sources_and_keeps_ptz_video(self):
        video, photos = configured_media_sources(
            "garage", "http://garage/snapshot", "user", "password", False,
            "rtsp://garage/stream",
            layout={"fixed": [0, 0, 100, 50], "ptz": [0, 50, 100, 100]},
            telegram_media={
                "photo_lenses": ["fixed", "ptz"],
                "video_lens": "ptz",
            },
        )

        self.assertEqual(video.name, "Garage")
        self.assertEqual(video.lens, (0, 50, 100, 100))
        self.assertEqual(
            [(item.name, item.lens) for item in photos],
            [
                ("Garage · lente fijo", (0, 0, 100, 50)),
                ("Garage · lente móvil", (0, 50, 100, 100)),
            ],
        )

    def test_photo_command_sends_each_garage_lens_and_backyard(self):
        garage_fixed = source("Garage · lente fijo", (0, 0, 100, 50))
        garage_ptz = source("Garage · lente móvil", (0, 50, 100, 100))
        backyard = source("Backyard")
        notifier = MagicMock()
        commands = CameraMediaCommands(
            [garage_ptz, backyard], notifier,
            photo_sources=[garage_fixed, garage_ptz, backyard],
        )

        with patch("camera_media.normalized_snapshot", return_value=b"jpeg"):
            commands._photos()

        self.assertEqual(
            [call.args[1] for call in notifier.send_photo.call_args_list],
            [
                "📷 Garage · lente fijo",
                "📷 Garage · lente móvil",
                "📷 Backyard",
            ],
        )

    def test_photo_sends_working_camera_and_reports_failed_camera(self):
        garage = source("Garage")
        backyard = source("Backyard")
        notifier = MagicMock()
        commands = CameraMediaCommands([garage, backyard], notifier)

        def capture(item):
            if item.name == "Garage":
                raise TimeoutError("offline")
            return b"jpeg"

        with patch("camera_media.normalized_snapshot", side_effect=capture):
            commands._photos()

        notifier.send_photo.assert_called_once_with(b"jpeg", "📷 Backyard")
        failure = notifier.send_message.call_args.args[0]
        self.assertIn("Garage", failure)
        self.assertIn("otras cámaras", failure)

    def test_video_sends_working_camera_and_cleans_temporary_file(self):
        garage = source("Garage")
        backyard = source("Backyard")
        notifier = MagicMock()
        commands = CameraMediaCommands([garage, backyard], notifier)
        created = []

        def capture(item, output, _seconds, _barrier):
            if item.name == "Backyard":
                raise TimeoutError("offline")
            output.write_bytes(b"mp4")
            created.append(output)
            return output

        with patch("camera_media.record_video", side_effect=capture):
            commands._videos()

        notifier.send_video.assert_called_once_with(
            b"mp4", "🎥 Garage · 5 segundos", filename="Garage.mp4",
        )
        self.assertFalse(created[0].exists())
        self.assertIn("Backyard", notifier.send_message.call_args.args[0])

    def test_photo_command_uses_same_authorized_chat_check(self):
        notifier = TelegramNotifier("token", "123")
        handler = MagicMock(return_value="accepted")
        notifier.set_command_handler(handler)
        notifier.send_message = MagicMock()

        notifier._handle_command({
            "message": {"chat": {"id": 999}, "text": "/photo"},
        })
        handler.assert_not_called()

        notifier._handle_command({
            "message": {"chat": {"id": 123}, "text": "/photo"},
        })
        handler.assert_called_once_with("/photo")
        notifier.send_message.assert_called_once_with("accepted")


if __name__ == "__main__":
    unittest.main()
