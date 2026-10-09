import unittest

from internet_connectivity import InternetConnectivityMonitor


class InternetConnectivityTests(unittest.TestCase):
    def test_recovery_reports_full_outage_period(self):
        results = iter([True, False, False, False, True])
        messages = []
        monitor = InternetConnectivityMonitor(
            messages.append, failure_threshold=2, probe=lambda: next(results),
        )

        monitor.check_once(1000)
        monitor.check_once(1010)
        monitor.check_once(1040)
        monitor.check_once(1070)
        self.assertEqual(messages, [])
        monitor.check_once(1100)

        self.assertEqual(len(messages), 1)
        self.assertIn("1m 30s", messages[0])

    def test_single_failed_probe_does_not_change_online_state(self):
        results = iter([True, False, True])
        messages = []
        monitor = InternetConnectivityMonitor(
            messages.append, failure_threshold=2, probe=lambda: next(results),
        )

        monitor.check_once(1000)
        monitor.check_once(1030)
        monitor.check_once(1060)

        self.assertTrue(monitor.online)
        self.assertEqual(messages, [])


if __name__ == "__main__":
    unittest.main()
