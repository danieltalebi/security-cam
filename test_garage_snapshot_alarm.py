import threading
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from dvrip_smart_telegram import SmartAlarm
from snapshot_loitering import DwellTrack, PersonObservation


class GarageSnapshotAlarmTests(unittest.TestCase):
    @staticmethod
    def alarm():
        alarm = object.__new__(SmartAlarm)
        alarm.name = "garage"
        alarm.layout = {"fixed": [0, 0, 100, 50], "ptz": [0, 50, 100, 100]}
        alarm.loitering = {"enabled": True, "dwell_seconds": 4}
        alarm.loitering_enabled = True
        alarm.interval = 0
        alarm.observation_seconds = 1
        alarm.max_missing_snapshots = 2
        alarm.session_lock = threading.Lock()
        alarm.event_lock = threading.Lock()
        alarm.last_confirmed_event_at = float("-inf")
        alarm.save_result = MagicMock(return_value="evidence.jpg")
        alarm.publish_detection = MagicMock()
        alarm.register_local_detection = MagicMock()
        alarm.notify_alert = MagicMock()
        alarm.policy = MagicMock()
        return alarm

    def test_confirmed_event_cooldown_prevents_duplicate_tracking_session(self):
        alarm = self.alarm()
        alarm.event_lock = threading.Lock()
        alarm.last_accepted_event_at = 100
        alarm.last_confirmed_event_at = 100
        alarm.notification_cooldown = 40
        alarm.event_debounce = 3

        with patch("dvrip_smart_telegram.time.monotonic", return_value=120), patch(
            "dvrip_smart_telegram.threading.Thread"
        ) as worker:
            alarm.handle_event({"type": "person", "status": "Start"})

        worker.assert_not_called()

    def test_detect_people_splits_fixed_and_ptz_without_area_classifier(self):
        alarm = self.alarm()
        result = MagicMock()
        result.boxes.data.tolist.return_value = [
            [10, 5, 30, 45, 0.9, 0],
            [40, 55, 60, 95, 0.8, 0],
        ]
        alarm.model = MagicMock()
        alarm.model.predict.return_value = [result]

        _annotated, fixed, ptz = alarm.detect_people(
            np.zeros((100, 100, 3), dtype=np.uint8)
        )

        self.assertEqual((len(fixed), len(ptz)), (1, 1))
        self.assertEqual(ptz[0].foot, (50.0, 45))

    def test_fixed_lens_person_alerts_immediately_with_fixed_crop(self):
        alarm = self.alarm()
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        fixed = PersonObservation((10, 5, 30, 45), (20, 45), 40, 0.9)
        alarm.fetch_snapshot = MagicMock(return_value=frame)
        alarm.detect_people = MagicMock(return_value=(frame, [fixed], []))

        alarm._handle_event(MagicMock())

        published = alarm.publish_detection.call_args.args
        self.assertEqual(published[0].shape, (50, 100, 3))
        self.assertIn("fixed garage lens", published[1])
        self.assertEqual(published[2], "private")
        alarm.register_local_detection.assert_called_once()
        alarm.notify_alert.assert_not_called()

    def test_ptz_tracking_alert_uses_ptz_crop_and_presence_decision(self):
        alarm = self.alarm()
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        ptz = PersonObservation((10, 5, 30, 45), (20, 45), 40, 0.9)
        alarm.fetch_snapshot = MagicMock(return_value=frame)
        alarm.detect_people = MagicMock(return_value=(frame, [], [ptz]))
        track = DwellTrack(1, 0, 4)
        track.positions = [(20, 45)] * 5
        track.heights = [40] * 5
        track.detections = 5
        tracker = MagicMock()
        tracker.update.return_value = ([track], 0.9)

        with patch("dvrip_smart_telegram.SnapshotDwellTracker", return_value=tracker):
            alarm._handle_event(MagicMock())

        published = alarm.publish_detection.call_args.args
        self.assertEqual(published[0].shape, (50, 100, 3))
        self.assertIn("PTZ tracked 4.0s", published[1])
        self.assertEqual(published[2], "presence")
        alarm.register_local_detection.assert_called_once()
        self.assertEqual(alarm.register_local_detection.call_args.args[0], "visitor")


if __name__ == "__main__":
    unittest.main()
