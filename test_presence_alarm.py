import threading
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from multi_camera_monitor import PresenceAlarm


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

    def test_disarmed_alarm_ignores_start_before_spawning_worker(self):
        alarm = self.bare_alarm()
        alarm.home_assistant.should_verify_camera_events.return_value = False

        with patch("multi_camera_monitor.threading.Thread") as thread:
            alarm.handle_event({"type": "motion", "status": "Start"})

        thread.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
