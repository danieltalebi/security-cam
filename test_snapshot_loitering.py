import unittest

import numpy as np

from snapshot_loitering import DwellTrack, PersonObservation, SnapshotDwellTracker


class FakeStabilizer:
    def __init__(self, translations):
        self.translations = iter(translations)

    def estimate(self, _previous, _current, _previous_boxes, _current_boxes):
        translation = next(self.translations)
        matrix = np.array(
            [[1, 0, translation], [0, 1, 0], [0, 0, 1]], dtype=np.float64,
        )
        return matrix, 0.9


def observation(x):
    return PersonObservation(
        (int(x - 20), 20, int(x + 20), 120), (x, 120), 100, 0.9,
    )


class SnapshotDwellTrackerTests(unittest.TestCase):
    def tracker(self):
        tracker = SnapshotDwellTracker({
            "dwell_seconds": 4,
            "min_detections": 5,
            "stationary_radius_body_heights": 0.15,
            "match_distance_body_heights": 1.5,
            "track_timeout_seconds": 2.5,
        })
        tracker.stabilizer = FakeStabilizer([20, 20, 20, 20])
        return tracker

    def test_same_person_does_not_confirm_before_four_seconds(self):
        tracker = self.tracker()
        frame = np.zeros((140, 300, 3), dtype=np.uint8)
        ready = []
        for now, x in zip([0, 1, 2, 3.99], [100, 80, 60, 40]):
            ready, _quality = tracker.update(frame, [observation(x)], now)
        self.assertEqual(ready, [])

    def test_camera_motion_is_compensated_at_four_seconds(self):
        tracker = self.tracker()
        frame = np.zeros((140, 300, 3), dtype=np.uint8)
        ready = []
        for now, x in enumerate([100, 80, 60, 40, 20]):
            ready, _quality = tracker.update(frame, [observation(x)], now)
        self.assertEqual(len(ready), 1)
        self.assertLess(ready[0].radius_body_heights, 0.01)

    def test_missing_snapshot_within_timeout_keeps_same_track(self):
        tracker = self.tracker()
        frame = np.zeros((140, 300, 3), dtype=np.uint8)
        tracker.update(frame, [observation(100)], 0)
        tracker.update(frame, [observation(80)], 1)
        tracker.update(frame, [], 2)
        tracker.update(frame, [observation(40)], 3)
        ready, _quality = tracker.update(frame, [observation(20)], 4)
        self.assertEqual(len(tracker.tracks), 1)
        self.assertEqual(len(ready), 0)

    def test_centered_person_is_moving_when_background_pans(self):
        tracker = self.tracker()
        frame = np.zeros((140, 300, 3), dtype=np.uint8)
        ready = []
        for now in range(5):
            ready, _quality = tracker.update(frame, [observation(100)], now)
        self.assertEqual(ready, [])
        self.assertGreater(tracker.tracks[0].radius_body_heights, 0.15)


if __name__ == "__main__":
    unittest.main()
