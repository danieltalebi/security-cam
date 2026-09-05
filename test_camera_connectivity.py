import unittest
from unittest.mock import patch

from telegram_notify import CameraConnectivityAlerts


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send_notification_message(self, text):
        self.messages.append(text)
        return True


class CameraConnectivityTests(unittest.TestCase):
    def setUp(self):
        self.notifier = FakeNotifier()
        self.alerts = CameraConnectivityAlerts(
            "garage", self.notifier, thresholds=(300, 1800, 3600), start_thread=False,
        )

    @patch("telegram_notify.time.time", return_value=1000)
    def test_three_offline_alerts_then_recovery(self, _time):
        self.alerts.set_online(True)
        self.alerts.set_online(False)
        self.alerts.check(1299)
        self.assertEqual(self.notifier.messages, [])
        self.alerts.check(1300)
        self.alerts.check(2800)
        self.alerts.check(4600)
        self.alerts.check(9000)
        self.assertEqual(len(self.notifier.messages), 3)

        with patch("telegram_notify.time.time", return_value=4700):
            self.alerts.set_online(True)
        self.assertEqual(len(self.notifier.messages), 4)
        self.assertIn("back online", self.notifier.messages[-1])

    @patch("telegram_notify.time.time", return_value=1000)
    def test_short_outage_does_not_notify(self, _time):
        self.alerts.set_online(True)
        self.alerts.set_online(False)
        with patch("telegram_notify.time.time", return_value=1100):
            self.alerts.set_online(True)
        self.assertEqual(self.notifier.messages, [])

    @patch("telegram_notify.time.time", return_value=1000)
    def test_repeated_disconnect_does_not_reset_timer(self, _time):
        self.alerts.set_online(False)
        with patch("telegram_notify.time.time", return_value=1200):
            self.alerts.set_online(False)
        self.alerts.check(1300)
        self.assertEqual(len(self.notifier.messages), 1)


if __name__ == "__main__":
    unittest.main()
