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
        alarm.loitering = {}
        alarm.interval = 0
        alarm.observation_seconds = 1
        alarm.max_missing_snapshots = 2
        alarm.session_lock = threading.Lock()
        alarm.save_result = MagicMock()
        alarm.publish_detection = MagicMock()
        alarm.notify_alert = MagicMock()
        return alarm

    def test_fixed_lens_person_alerts_immediately_with_source(self):
        alarm = self.alarm()
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        fixed = PersonObservation((10, 5, 30, 45), (20, 45), 40, 0.9)
        alarm.fetch_snapshot = MagicMock(return_value=frame)
        alarm.detect_people = MagicMock(return_value=(frame, [fixed], []))

        alarm._handle_event(MagicMock())

        source = alarm.publish_detection.call_args.args[1]
        self.assertIn("fixed garage lens", source)
        self.assertEqual(alarm.publish_detection.call_args.args[2], "private")
        alarm.notify_alert.assert_called_once()

    def test_ptz_dwell_alert_publishes_compatible_presence_with_source(self):
        alarm = self.alarm()
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        ptz = PersonObservation((10, 5, 30, 45), (20, 45), 40, 0.9)
        alarm.fetch_snapshot = MagicMock(return_value=frame)
        alarm.detect_people = MagicMock(return_value=(frame, [], [ptz]))
        track = DwellTrack(1, 0, 8)
        track.positions = [(20, 45)] * 6
        track.heights = [40] * 6
        track.detections = 6
        tracker = MagicMock()
        tracker.update.return_value = ([track], 0.9)

        with patch("dvrip_smart_telegram.SnapshotDwellTracker", return_value=tracker):
            alarm._handle_event(MagicMock())

        source = alarm.publish_detection.call_args.args[1]
        self.assertIn("PTZ stationary", source)
        self.assertEqual(alarm.publish_detection.call_args.args[2], "presence")
        alarm.notify_alert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
