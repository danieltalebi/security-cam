import unittest

from smart_monitor import SmartMonitor


class CapturingSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def config():
    return {
        "layout": {"fixed": [0, 0, 100, 50], "ptz": [0, 50, 100, 100]},
        "tracking": {
            "match_distance_pixels": 30,
            "stationary_tolerance_pixels": 10,
            "track_timeout_seconds": 2,
            "alert_cooldown_seconds": 0,
        },
        "zones": [
            {
                "name": "fixed_home",
                "region": "fixed",
                "rule": "immediate",
                "polygon": [[0, 0], [100, 0], [100, 50], [0, 50]],
            },
            {
                "name": "sidewalk",
                "region": "ptz",
                "rule": "dwell",
                "dwell_seconds": 5,
                "polygon": [[0, 50], [100, 50], [100, 100], [0, 100]],
            },
        ],
    }


class SmartMonitorTests(unittest.TestCase):
    def setUp(self):
        self.sink = CapturingSink()
        self.monitor = SmartMonitor(config(), self.sink)

    def test_fixed_person_alerts_immediately(self):
        events = self.monitor.process([(10, 5, 30, 40)], 100)
        self.assertEqual(events[0]["zone"], "fixed_home")

    def test_ptz_person_must_reach_dwell_time(self):
        self.assertEqual(self.monitor.process([(10, 55, 30, 90)], 100), [])
        self.assertEqual(self.monitor.process([(12, 55, 32, 90)], 104.9), [])
        events = self.monitor.process([(13, 55, 33, 90)], 105.1)
        self.assertEqual(events[0]["type"], "person_loitering")

    def test_short_sidewalk_crossing_does_not_alert(self):
        self.monitor.process([(10, 55, 30, 90)], 100)
        self.monitor.process([], 101)
        self.monitor.process([], 103)
        self.assertEqual(self.sink.events, [])

    def test_moving_person_does_not_satisfy_dwell_rule(self):
        self.monitor.process([(10, 55, 30, 90)], 100)
        self.monitor.process([(25, 55, 45, 90)], 103)
        self.monitor.process([(40, 55, 60, 90)], 106)
        self.assertEqual(self.sink.events, [])


if __name__ == "__main__":
    unittest.main()
