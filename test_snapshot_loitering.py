import unittest

import numpy as np

from snapshot_loitering import DwellTrack, PersonObservation, SnapshotDwellTracker


class DwellTrackTests(unittest.TestCase):
    def test_stationary_jitter_stays_within_body_relative_radius(self):
        track = DwellTrack(1, 0, 0)
        for second, point in enumerate([(100, 200), (103, 198), (98, 202), (101, 201), (99, 199)], 1):
            track.add(point, 100, second)
        self.assertLess(track.radius_body_heights, 0.06)
        self.assertEqual(track.duration, 5)

    def test_walking_path_is_large_relative_to_person_height(self):
        track = DwellTrack(1, 0, 0)
        for second, x in enumerate([100, 140, 180, 220, 260], 1):
            track.add((x, 200), 100, second)
        self.assertGreater(track.radius_body_heights, 0.7)
        self.assertGreater(track.path_body_heights, 1.5)


class FakeStabilizer:
    def __init__(self, translations):
        self.translations = iter(translations)

    def estimate(self, _previous, _current, _previous_boxes, _current_boxes):
        translation = next(self.translations)
        matrix = np.array([[1, 0, translation], [0, 1, 0], [0, 0, 1]], dtype=np.float64)
        return matrix, 0.9


def observation(x):
    return PersonObservation((int(x - 20), 20, int(x + 20), 120), (x, 120), 100, 0.9)


class SnapshotDwellTrackerTests(unittest.TestCase):
    def tracker(self):
        tracker = SnapshotDwellTracker({
            "dwell_seconds": 3,
            "min_detections": 4,
            "stationary_radius_body_heights": 0.15,
            "match_distance_body_heights": 1.5,
        })
        tracker.stabilizer = FakeStabilizer([20, 20, 20])
        return tracker

    def test_background_motion_keeps_stationary_world_position_stable(self):
        tracker = self.tracker()
        frame = np.zeros((140, 300, 3), dtype=np.uint8)
        ready = []
        for now, x in enumerate([100, 80, 60, 40]):
            ready, _quality = tracker.update(frame, [observation(x)], now)
        self.assertEqual(len(ready), 1)
        self.assertLess(ready[0].radius_body_heights, 0.01)

    def test_centered_person_is_moving_when_background_pans(self):
        tracker = self.tracker()
        frame = np.zeros((140, 300, 3), dtype=np.uint8)
        ready = []
        for now in range(4):
            ready, _quality = tracker.update(frame, [observation(100)], now)
        self.assertEqual(ready, [])
        self.assertGreater(tracker.tracks[0].radius_body_heights, 0.15)


if __name__ == "__main__":
    unittest.main()
