import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from connectivity_history import ConnectivityHistory


BASE = datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc).timestamp()


class ConnectivityHistoryTests(unittest.TestCase):
    def history(self, folder, messages):
        return ConnectivityHistory(
            Path(folder) / "connectivity.json",
            messages.append,
            ["asus", "huawei"],
            internet_threshold_seconds=600,
            router_alert_seconds=3600,
            summary_time="19:00",
            timezone="UTC",
            clock=lambda: BASE,
        )

    def test_short_and_medium_router_outages_only_appear_in_daily_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            messages = []
            history = self.history(folder, messages)
            history.set_internet(True, BASE)
            history.set_router("asus", True, BASE)
            history.set_router("huawei", True, BASE)
            history.set_router("asus", False, BASE + 60)
            history.set_router("asus", True, BASE + 4 * 60)
            history.set_router("huawei", False, BASE + 10 * 60)
            history.set_router("huawei", True, BASE + 40 * 60)
            self.assertEqual(messages, [])

            history.tick(BASE + 3600)
            history.tick(BASE + 3700)

            self.assertEqual(len(messages), 1)
            self.assertIn("asus", messages[0])
            self.assertIn("3m", messages[0])
            self.assertIn("huawei", messages[0])
            self.assertIn("30m", messages[0])

    def test_router_still_down_alerts_once_after_one_hour(self):
        with tempfile.TemporaryDirectory() as folder:
            messages = []
            history = self.history(folder, messages)
            history.set_internet(True, BASE)
            history.set_router("asus", True, BASE)
            history.set_router("asus", False, BASE + 1)
            history.tick(BASE + 3600)
            self.assertEqual(
                [item for item in messages if "SIN CONEXIÓN" in item], [],
            )
            history.tick(BASE + 3601)
            history.tick(BASE + 7200)

            router_alerts = [item for item in messages if "SIN CONEXIÓN" in item]
            self.assertEqual(len(router_alerts), 1)

    def test_short_internet_outage_is_summary_only(self):
        with tempfile.TemporaryDirectory() as folder:
            messages = []
            history = self.history(folder, messages)
            history.set_internet(True, BASE)
            history.set_internet(False, BASE + 60)
            history.set_internet(True, BASE + 9 * 60)
            self.assertEqual(messages, [])

            history.tick(BASE + 3600)
            self.assertEqual(len(messages), 1)
            self.assertIn("Internet", messages[0])
            self.assertIn("8m", messages[0])

    def test_long_internet_alert_survives_restart_and_sends_on_recovery_once(self):
        with tempfile.TemporaryDirectory() as folder:
            messages = []
            history = self.history(folder, messages)
            history.set_internet(True, BASE)
            history.set_internet(False, BASE + 60)

            restored = self.history(folder, messages)
            restored.set_internet(True, BASE + 12 * 60)
            restored.set_internet(True, BASE + 13 * 60)

            alerts = [item for item in messages if "INTERNET RESTABLECIDO" in item]
            self.assertEqual(len(alerts), 1)
            self.assertIn("11m", alerts[0])
            state = json.loads((Path(folder) / "connectivity.json").read_text())
            self.assertEqual(state["pending_notifications"], [])

    def test_summary_due_while_offline_is_sent_after_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            messages = []
            history = self.history(folder, messages)
            history.set_internet(True, BASE)
            history.set_internet(False, BASE + 30 * 60)
            history.tick(BASE + 3600)
            self.assertEqual(messages, [])

            history.set_internet(True, BASE + 70 * 60)

            self.assertTrue(any("INTERNET RESTABLECIDO" in item for item in messages))
            summaries = [item for item in messages if "Resumen diario" in item]
            self.assertEqual(len(summaries), 1)
            self.assertIn("Internet: en línea", summaries[0])

    def test_summary_runs_at_18_argentina_and_includes_alarm_context(self):
        with tempfile.TemporaryDirectory() as folder:
            messages = []
            before = datetime(2026, 1, 1, 20, 59, tzinfo=timezone.utc).timestamp()
            due = datetime(2026, 1, 1, 21, 0, tzinfo=timezone.utc).timestamp()
            history = ConnectivityHistory(
                Path(folder) / "connectivity.json",
                messages.append,
                ["asus", "huawei"],
                summary_time="18:00",
                timezone="America/Argentina/Buenos_Aires",
                summary_context=lambda: "alarma manual y desarmada",
                clock=lambda: before,
            )
            history.set_internet(True, before)
            history.set_router("asus", True, before)
            history.set_router("huawei", True, before)

            history.tick(due - 1)
            self.assertEqual(messages, [])
            history.tick(due)

            self.assertEqual(len(messages), 1)
            self.assertIn("Resumen diario", messages[0])
            self.assertIn("🌐 Estado actual", messages[0])
            self.assertIn("📉 Caídas", messages[0])
            self.assertIn("alarma manual y desarmada", messages[0])


if __name__ == "__main__":
    unittest.main()
