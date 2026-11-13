import unittest

import cv2
import numpy as np

from false_positive_memory import (
    all_detections_match,
    detection_metadata,
    detections_match,
    hash_distance,
)


class FalsePositiveMemoryTests(unittest.TestCase):
    @staticmethod
    def scene() -> np.ndarray:
        y, x = np.mgrid[0:240, 0:320]
        base = ((x * 0.45 + y * 0.25) % 180 + 25).astype(np.uint8)
        frame = cv2.merge((base, base, base))
        cv2.rectangle(frame, (50, 40), (120, 190), (25, 25, 25), -1)
        cv2.circle(frame, (85, 68), 22, (220, 220, 220), -1)
        cv2.line(frame, (55, 175), (115, 80), (180, 180, 180), 8)
        return frame

    def test_hash_tolerates_global_brightness_change(self):
        frame = self.scene()
        darker = cv2.convertScaleAbs(frame, alpha=0.68, beta=18)
        first = detection_metadata(frame, [(50, 40, 120, 190)])[0]
        second = detection_metadata(darker, [(50, 40, 120, 190)])[0]

        self.assertLessEqual(hash_distance(first["phash"], second["phash"]), 10)
        self.assertTrue(detections_match(first, second))

    def test_same_appearance_in_another_area_is_not_suppressed(self):
        exemplar = {"box": [0.1, 0.1, 0.3, 0.6], "phash": "0123456789abcdef"}
        elsewhere = {"box": [0.6, 0.1, 0.8, 0.6], "phash": "0123456789abcdef"}
        self.assertFalse(detections_match(elsewhere, exemplar))

    def test_additional_person_keeps_event_alertable(self):
        exemplar = {"box": [0.1, 0.1, 0.3, 0.6], "phash": "0123456789abcdef"}
        additional = {"box": [0.6, 0.1, 0.8, 0.6], "phash": "fedcba9876543210"}
        self.assertFalse(all_detections_match([exemplar, additional], [exemplar]))


if __name__ == "__main__":
    unittest.main()
