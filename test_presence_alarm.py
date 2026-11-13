import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from multi_camera_monitor import (
    PresenceAlarm, daily_alarm_reminder, handle_interior_presence,
    send_incident_notification,
)


class PresenceAlarmTests(unittest.TestCase):
    @staticmethod
    def bare_alarm() -> PresenceAlarm:
        alarm = object.__new__(PresenceAlarm)
        alarm.name = "backyard"
        alarm.home_assistant = MagicMock()
        alarm.notifier = None
        alarm.user = "camera-user"
        alarm.password = "camera-password"
        alarm.stream = None
        alarm.event_debounce = 3
        alarm.last_accepted_event_at = float("-inf")
        alarm.event_lock = threading.Lock()
        alarm.verification_lock = threading.Lock()
        return alarm

    def test_disarmed_home_assistant_state_does_not_prevent_local_evidence_worker(self):
        alarm = self.bare_alarm()
        alarm.home_assistant.should_verify_camera_events.return_value = False

        with patch("multi_camera_monitor.threading.Thread") as thread:
            alarm.handle_event({"type": "motion", "status": "Start"})

        thread.assert_called_once()
        thread.return_value.start.assert_called_once()

    def test_armed_alarm_accepts_any_dvrip_start_type(self):
        alarm = self.bare_alarm()
        alarm.home_assistant.should_verify_camera_events.return_value = True

        with patch("multi_camera_monitor.threading.Thread") as thread:
            alarm.handle_event({"type": "unknown", "status": "Start"})

        thread.assert_called_once()
        thread.return_value.start.assert_called_once()

    def test_snapshot_person_is_published_without_opening_rtsp(self):
        alarm = self.bare_alarm()
        frame = np.zeros((8, 8, 3), dtype=np.uint8)
        alarm.fetch_snapshot = MagicMock(return_value=frame)
        alarm.analyze = MagicMock(return_value=(frame, 1))
        alarm.save_result = MagicMock()
        alarm.publish_confirmed = MagicMock()
        alarm.publish_verification_failure = MagicMock()
        alarm._video_frames = MagicMock(side_effect=AssertionError("RTSP should not open"))

        alarm._handle({"type": "motion", "status": "Start"})

        alarm.publish_confirmed.assert_called_once()
        alarm.publish_verification_failure.assert_not_called()
        alarm._video_frames.assert_not_called()

    def test_backyard_analysis_records_normalized_box_and_visual_hash(self):
        alarm = self.bare_alarm()
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        frame[20:80, 40:100] = 180
        result = MagicMock()
        result.boxes.data.tolist.return_value = [[40, 20, 100, 80, 0.9, 0]]
        result.plot.return_value = frame.copy()
        alarm.model = MagicMock()
        alarm.model.predict.return_value = [result]
        alarm.confidence = 0.5

        _annotated, count, detections = alarm.analyze(frame)

        self.assertEqual(count, 1)
        self.assertEqual(detections[0]["box"], [0.2, 0.2, 0.5, 0.8])
        self.assertEqual(len(detections[0]["phash"]), 16)

    def test_no_snapshot_or_rtsp_publishes_technical_failure(self):
        alarm = self.bare_alarm()
        alarm.fetch_snapshot = MagicMock(side_effect=TimeoutError("camera-password unavailable"))
        alarm.publish_verification_failure = MagicMock()

        alarm._handle({"type": "unknown", "status": "Start"})

        alarm.publish_verification_failure.assert_called_once()
        reason = alarm.publish_verification_failure.call_args.args[0]
        self.assertIn("snapshot", reason)
        self.assertIn("RTSP fallback is not configured", reason)

    def test_rtsp_can_confirm_after_negative_snapshot(self):
        alarm = self.bare_alarm()
        frame = np.zeros((8, 8, 3), dtype=np.uint8)
        alarm.stream = "rtsp://camera/stream"
        alarm.fetch_snapshot = MagicMock(return_value=frame)
        alarm.analyze = MagicMock(side_effect=[(frame, 0), (frame, 1)])
        alarm._video_frames = MagicMock(return_value=iter([frame]))
        alarm.save_result = MagicMock()
        alarm.publish_confirmed = MagicMock()
        alarm.publish_verification_failure = MagicMock()

        alarm._handle({"type": "motion", "status": "Start"})

        self.assertEqual(alarm.analyze.call_count, 2)
        self.assertEqual(alarm.publish_confirmed.call_args.args[1:3], ("video", 1))
        alarm.publish_verification_failure.assert_not_called()

    def test_failure_reason_redacts_camera_credentials(self):
        alarm = self.bare_alarm()

        alarm.publish_verification_failure(
            "request for camera-user:camera-password failed", MagicMock(),
        )

        reason = alarm.home_assistant.publish_verification_failure.call_args.args[1]
        self.assertNotIn("camera-user", reason)
        self.assertNotIn("camera-password", reason)
        self.assertIn("<redacted>", reason)

    def test_local_policy_receives_confirmed_detection_without_home_assistant(self):
        alarm = self.bare_alarm()
        alarm.home_assistant = None
        alarm.policy = MagicMock()
        alarm.role = "interior"
        alarm.notifier = MagicMock()
        occurred_at = MagicMock()

        alarm.publish_confirmed(
            np.zeros((8, 8, 3), dtype=np.uint8), "image", 1,
            occurred_at, "evidence.jpg",
        )

        alarm.policy.record_detection.assert_called_once_with(
            "backyard", "interior", "presence", "evidence.jpg", occurred_at,
        )
        alarm.notifier.send_photo.assert_not_called()

    def test_home_assistant_interior_event_is_fixed_to_interior_policy_role(self):
        policy = MagicMock()
        policy.record_detection.return_value = {"id": "incident"}

        result = handle_interior_presence(policy, {
            "event_type": "presence_detected",
            "camera": "untrusted-camera-name",
            "occurred_at": "2026-11-07T23:30:00+11:00",
        })

        self.assertIn("accepted", result)
        self.assertEqual(policy.record_detection.call_args.args[:4], (
            "interior", "interior", "presence", None,
        ))

    def test_interior_incident_sends_text_without_fake_evidence(self):
        channel = MagicMock()
        channel.send_notification_message.return_value = True

        sent = send_incident_notification(channel, {
            "id": "1234567890abcdef",
            "camera": "interior",
            "classification": "presence",
            "evidence_path": None,
        })

        self.assertTrue(sent)
        channel.send_notification_message.assert_called_once()
        channel.send_photo.assert_not_called()

    def test_camera_incident_captions_identify_each_detection_path(self):
        with tempfile.TemporaryDirectory() as folder:
            evidence = Path(folder) / "evidence.jpg"
            evidence.write_bytes(b"jpeg")
            cases = (
                ("garage", "private", "lente fijo"),
                ("garage", "visitor", "lente móvil PTZ"),
                ("backyard", "presence", "Backyard"),
            )
            for camera, classification, expected in cases:
                with self.subTest(camera=camera, classification=classification):
                    channel = MagicMock()
                    channel.send_photo.return_value = True
                    sent = send_incident_notification(channel, {
                        "id": "1234567890abcdef",
                        "camera": camera,
                        "classification": classification,
                        "evidence_path": str(evidence),
                    })
                    self.assertTrue(sent)
                    self.assertIn(expected, channel.send_photo.call_args.args[1])
                    self.assertIn("IGNORAR", channel.send_photo.call_args.args[1])

    def test_daily_alarm_reminder_reports_control_and_warns_when_disarmed(self):
        policy = MagicMock()
        policy.snapshot.return_value = {"control": "manual", "mode": "disarmed"}
        warning = daily_alarm_reminder(policy)
        self.assertIn("ADVERTENCIA", warning)
        self.assertIn("MANUAL", warning)
        self.assertIn("DESARMADA", warning)

        policy.snapshot.return_value = {"control": "manual", "mode": "armed_home"}
        manual_armed = daily_alarm_reminder(policy)
        self.assertIn("Recordatorio", manual_armed)
        self.assertIn("Manual", manual_armed)
        self.assertIn("Armada en casa", manual_armed)

        policy.snapshot.return_value = {"control": "automatic", "mode": "disarmed"}
        automatic = daily_alarm_reminder(policy)
        self.assertIn("Estado de la alarma", automatic)
        self.assertIn("Automático", automatic)
        self.assertIn("DESARMADA", automatic)


if __name__ == "__main__":
    unittest.main()
