import unittest
from unittest.mock import MagicMock

import numpy as np

from dvrip_smart_telegram import SmartAlarm


class GarageSnapshotAlarmTests(unittest.TestCase):
    @staticmethod
    def alarm():
        alarm = object.__new__(SmartAlarm)
        alarm.name = "garage"
        alarm.layout = {"fixed": [0, 0, 100, 50], "ptz": [0, 50, 100, 100]}
        alarm.public_threshold = 0.65
        alarm.private_threshold = 0.35
        alarm.classifier = MagicMock()
        alarm.classifier.predict.return_value = np.zeros((50, 100), dtype=np.float32)
        result = MagicMock()
        result.boxes.data.tolist.return_value = []
        alarm.model = MagicMock()
        alarm.model.predict.return_value = [result]
        alarm.save_result = MagicMock()
        alarm.publish_detection = MagicMock()
        alarm.notify_private = MagicMock()
        alarm.fallback = MagicMock()
        return alarm

    def test_ptz_person_uses_ground_probability_for_public_decision(self):
        alarm = self.alarm()
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        alarm.model.predict.return_value[0].boxes.data.tolist.return_value = [
            [10, 55, 30, 95, 0.9, 0],
        ]
        alarm.classifier.probability_at.return_value = 0.9

        decision, _annotated, count = alarm.analyze(frame)

        self.assertEqual((decision, count), ("public", 1))
        alarm.classifier.probability_at.assert_called_once_with((20.0, 95))

    def test_fixed_lens_person_is_private_without_ptz_probability(self):
        alarm = self.alarm()
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        alarm.model.predict.return_value[0].boxes.data.tolist.return_value = [
            [10, 5, 30, 45, 0.9, 0],
        ]

        decision, _annotated, count = alarm.analyze(frame)

        self.assertEqual((decision, count), ("private", 1))
        alarm.classifier.probability_at.assert_not_called()

    def test_single_public_image_publishes_without_notification_or_fallback(self):
        alarm = self.alarm()
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        alarm.fetch_snapshot = MagicMock(return_value=frame)
        alarm.analyze = MagicMock(return_value=("public", frame, 1))

        alarm._handle_event(MagicMock())

        self.assertEqual(alarm.publish_detection.call_args.args[1:4], ("image", "public", 1))
        alarm.notify_private.assert_not_called()
        alarm.fallback.assert_not_called()

    def test_uncertain_single_image_uses_rtsp_fallback(self):
        alarm = self.alarm()
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        alarm.fetch_snapshot = MagicMock(return_value=frame)
        alarm.analyze = MagicMock(return_value=("uncertain", frame, 1))

        occurred_at = MagicMock()
        alarm._handle_event(occurred_at)

        alarm.fallback.assert_called_once_with(occurred_at)
        alarm.publish_detection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
