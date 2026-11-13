import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from alarm_policy import AlarmPolicyController


class Clock:
    def __init__(self, value: float):
        self.value = value

    def __call__(self) -> float:
        return self.value


class AlarmPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_file = Path(self.temporary.name) / "state.json"

    @staticmethod
    def local_epoch(hour: int, minute: int = 0) -> float:
        return datetime.now().astimezone().replace(
            hour=hour, minute=minute, second=0, microsecond=0,
        ).timestamp()

    def controller(self, clock: Clock, **overrides) -> AlarmPolicyController:
        config = {
            "automatic": {
                "day_start": "08:30", "night_start": "23:00",
                "away_debounce_seconds": 300,
            },
            "incidents": {
                "close_after_seconds": 60,
                "notification_backoff_seconds": [0, 60, 180],
            },
        }
        for section, values in overrides.items():
            config.setdefault(section, {}).update(values)
        return AlarmPolicyController(config, self.state_file, now=clock)

    def test_present_phone_selects_day_and_night_modes(self):
        clock = Clock(self.local_epoch(10))
        policy = self.controller(clock)
        policy.observe_presence("home", {"devices": {"phone": True}}, clock.value)
        self.assertEqual(policy.snapshot()["mode"], "disarmed")

        clock.value = self.local_epoch(23, 30)
        policy.tick(clock.value)
        self.assertEqual(policy.snapshot()["mode"], "armed_home")

        clock.value = self.local_epoch(8, 30)
        policy.tick(clock.value)
        self.assertEqual(policy.snapshot()["mode"], "disarmed")

    def test_automatic_schedule_uses_configured_argentina_timezone(self):
        argentina_day = datetime(2026, 1, 1, 21, 0, tzinfo=timezone.utc).timestamp()
        clock = Clock(argentina_day)
        policy = self.controller(
            clock, automatic={"timezone": "America/Argentina/Buenos_Aires"},
        )
        policy.observe_presence("home", {"devices": {"iPhone": True}}, clock.value)
        self.assertEqual(policy.snapshot()["mode"], "disarmed")

        clock.value = datetime(2026, 1, 2, 2, 30, tzinfo=timezone.utc).timestamp()
        policy.tick(clock.value)
        self.assertEqual(policy.snapshot()["mode"], "armed_home")

    def test_continuous_absence_arms_after_five_minutes_and_return_is_immediate(self):
        clock = Clock(self.local_epoch(12))
        policy = self.controller(clock)
        policy.observe_presence("home", {"devices": {"phone": True}}, clock.value)
        policy.observe_presence("away", {"devices": {"phone": False}}, clock.value)
        clock.value += 299
        policy.tick(clock.value)
        self.assertEqual(policy.snapshot()["mode"], "disarmed")
        clock.value += 1
        policy.tick(clock.value)
        self.assertEqual(policy.snapshot()["mode"], "armed_away")

        clock.value += 1
        policy.observe_presence("home", {"devices": {"phone": True}}, clock.value)
        self.assertEqual(policy.snapshot()["mode"], "disarmed")

    def test_router_failure_retains_mode_and_breaks_absence_debounce(self):
        clock = Clock(self.local_epoch(12))
        policy = self.controller(clock)
        policy.set_manual("armed_home", clock.value)
        policy.set_automatic(clock.value)
        policy.observe_presence("unavailable", {}, clock.value)
        self.assertEqual(policy.snapshot()["mode"], "armed_home")

        policy.observe_presence("away", {"devices": {"phone": False}}, clock.value)
        clock.value += 200
        policy.observe_presence("unavailable", {}, clock.value)
        clock.value += 200
        policy.observe_presence("away", {"devices": {"phone": False}}, clock.value)
        clock.value += 299
        policy.tick(clock.value)
        self.assertEqual(policy.snapshot()["mode"], "armed_home")

    def test_manual_mode_ignores_presence_until_auto_is_restored(self):
        clock = Clock(self.local_epoch(12))
        policy = self.controller(clock)
        policy.set_manual("armed_home", clock.value)
        policy.observe_presence("home", {"devices": {"phone": True}}, clock.value)
        self.assertEqual(policy.snapshot()["mode"], "armed_home")
        self.assertEqual(policy.snapshot()["control"], "manual")
        policy.set_automatic(clock.value)
        self.assertEqual(policy.snapshot()["mode"], "disarmed")

    def test_camera_policy_never_alerts_public_and_respects_interior_role(self):
        clock = Clock(self.local_epoch(12))
        policy = self.controller(clock)
        policy.set_manual("armed_home", clock.value)
        self.assertTrue(policy.camera_may_alert("exterior", "private"))
        self.assertTrue(policy.camera_may_alert("exterior", "uncertain"))
        self.assertFalse(policy.camera_may_alert("exterior", "public"))
        self.assertFalse(policy.camera_may_alert("interior", "presence"))
        policy.set_manual("armed_away", clock.value)
        self.assertTrue(policy.camera_may_alert("interior", "presence"))
        policy.set_manual("disarmed", clock.value)
        self.assertFalse(policy.camera_may_alert("exterior", "private"))

    def test_tracked_garage_visitor_alerts_while_disarmed_but_respects_pause(self):
        clock = Clock(self.local_epoch(12))
        policy = self.controller(clock)
        policy.set_manual("disarmed", clock.value)
        occurred = datetime.fromtimestamp(clock.value).astimezone()

        self.assertIsNone(
            policy.record_detection("garage", "exterior", "private", "fixed.jpg", occurred)
        )
        visitor = policy.record_detection(
            "garage", "exterior", "visitor", "ptz.jpg", occurred,
        )

        self.assertIsNotNone(visitor)
        self.assertEqual(visitor["classification"], "visitor")
        self.assertEqual(len(policy.due_notifications(clock.value)), 1)
        policy.pause_for(60, clock.value)
        self.assertEqual(policy.due_notifications(clock.value), [])

    def test_status_uses_friendly_alarm_phone_and_router_names(self):
        clock = Clock(self.local_epoch(12))
        policy = self.controller(clock)
        policy.observe_presence("home", {
            "devices": {"iPhone": False, "iPhone 2": True},
            "sources": {
                "asus": {"available": True},
                "huawei": {"available": False},
            },
        }, clock.value)

        status = policy.handle_command("/status")

        self.assertIn("Modo de alarma: desarmada", status)
        self.assertIn("Control: automático", status)
        self.assertIn("iPhone=ausente", status)
        self.assertIn("iPhone 2=presente", status)
        self.assertIn("asus=en línea", status)
        self.assertIn("huawei=sin conexión", status)

    def test_incidents_group_back_off_acknowledge_and_close(self):
        clock = Clock(self.local_epoch(12))
        policy = self.controller(clock)
        policy.set_manual("armed_away", clock.value)
        occurred = datetime.fromtimestamp(clock.value).astimezone()
        first = policy.record_detection("garage", "exterior", "private", "one.jpg", occurred)
        self.assertIsNotNone(first)
        self.assertEqual(len(policy.due_notifications(clock.value)), 1)
        policy.mark_notified("garage", clock.value)
        self.assertEqual(policy.due_notifications(clock.value + 59), [])

        clock.value += 30
        second = policy.record_detection(
            "garage", "exterior", "uncertain", "two.jpg",
            datetime.fromtimestamp(clock.value).astimezone(),
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["evidence_path"], "two.jpg")
        self.assertEqual(len(policy.due_notifications(clock.value + 30)), 1)
        self.assertEqual(policy.acknowledge("garage", clock.value + 30), 1)
        self.assertEqual(policy.due_notifications(clock.value + 300), [])

        clock.value += 61
        policy.tick(clock.value)
        self.assertFalse(policy.snapshot()["incidents"]["garage"]["active"])

    def test_notification_backoff_progresses(self):
        clock = Clock(self.local_epoch(12))
        policy = self.controller(clock)
        policy.set_manual("armed_away", clock.value)
        policy.record_detection(
            "garage", "exterior", "private", "one.jpg",
            datetime.fromtimestamp(clock.value).astimezone(),
        )
        policy.mark_notified("garage", clock.value)
        self.assertEqual(policy.due_notifications(clock.value + 59), [])
        self.assertEqual(len(policy.due_notifications(clock.value + 60)), 1)
        policy.mark_notified("garage", clock.value + 60)
        self.assertEqual(policy.due_notifications(clock.value + 239), [])
        self.assertEqual(len(policy.due_notifications(clock.value + 240)), 1)

    def test_public_detection_never_creates_incident(self):
        clock = Clock(self.local_epoch(12))
        policy = self.controller(clock)
        policy.set_manual("armed_away", clock.value)
        result = policy.record_detection(
            "garage", "exterior", "public", "public.jpg",
            datetime.fromtimestamp(clock.value).astimezone(),
        )
        self.assertIsNone(result)
        self.assertEqual(policy.snapshot()["active_incidents"], [])

    def test_ignore_marks_latest_incident_as_persistent_false_positive(self):
        clock = Clock(self.local_epoch(12))
        policy = self.controller(clock)
        policy.set_manual("armed_away", clock.value)
        policy.record_detection(
            "garage", "exterior", "private", "shadow.jpg",
            datetime.fromtimestamp(clock.value).astimezone(),
        )
        policy.mark_notified("garage", clock.value)

        response = policy.handle_command("IGNORAR")

        self.assertIn("garage", response)
        state = policy.snapshot()
        self.assertTrue(state["incidents"]["garage"]["acknowledged"])
        self.assertTrue(state["incidents"]["garage"]["false_positive"])
        self.assertEqual(state["false_positives"][0]["evidence_path"], "shadow.jpg")
        self.assertEqual(policy.due_notifications(clock.value + 300), [])

        restored = self.controller(clock)
        self.assertEqual(
            restored.snapshot()["false_positives"][0]["incident_id"],
            state["incidents"]["garage"]["id"],
        )

    def test_ignore_can_target_a_camera_after_incident_closed(self):
        clock = Clock(self.local_epoch(12))
        policy = self.controller(clock, incidents={"close_after_seconds": 5})
        policy.set_manual("armed_away", clock.value)
        policy.record_detection(
            "backyard", "exterior", "presence", "shadow.jpg",
            datetime.fromtimestamp(clock.value).astimezone(),
        )
        clock.value += 6
        policy.tick(clock.value)

        response = policy.handle_command("/ignore backyard")

        self.assertIn("backyard", response)
        self.assertTrue(policy.snapshot()["incidents"]["backyard"]["false_positive"])

    def test_false_positive_memory_suppresses_same_object_only_until_expiry(self):
        clock = Clock(self.local_epoch(12))
        policy = self.controller(
            clock, incidents={
                "close_after_seconds": 60,
                "false_positive_suppression_seconds": 120,
            },
        )
        policy.set_manual("armed_away", clock.value)
        same_object = [{"box": [0.1, 0.1, 0.3, 0.5], "phash": "0123456789abcdef"}]
        other_area = [{"box": [0.65, 0.1, 0.85, 0.5], "phash": "0123456789abcdef"}]
        occurred = datetime.fromtimestamp(clock.value).astimezone()
        policy.record_detection(
            "backyard", "exterior", "presence", "shadow.jpg", occurred,
            detections=same_object,
        )
        policy.mark_false_positive("backyard", clock.value)

        clock.value += 61
        policy.tick(clock.value)
        self.assertIsNone(policy.record_detection(
            "backyard", "exterior", "presence", "same-shadow.jpg",
            datetime.fromtimestamp(clock.value).astimezone(), detections=same_object,
        ))
        different = policy.record_detection(
            "backyard", "exterior", "presence", "person-elsewhere.jpg",
            datetime.fromtimestamp(clock.value + 1).astimezone(), detections=other_area,
        )
        self.assertIsNotNone(different)

        clock.value += 61
        policy.tick(clock.value)
        after_expiry = policy.record_detection(
            "backyard", "exterior", "presence", "shadow-later.jpg",
            datetime.fromtimestamp(clock.value).astimezone(), detections=same_object,
        )
        self.assertIsNotNone(after_expiry)
        feedback = policy.snapshot()["false_positives"][0]
        self.assertEqual(feedback["suppressed_count"], 1)

    def test_pause_and_operator_controls_survive_restart(self):
        clock = Clock(self.local_epoch(12))
        policy = self.controller(clock)
        policy.set_manual("armed_away", clock.value)
        policy.pause_for(5400, clock.value)
        restored = self.controller(clock)
        state = restored.snapshot()
        self.assertEqual((state["mode"], state["control"]), ("armed_away", "manual"))
        self.assertTrue(state["notifications_paused"])
        self.assertGreater(state["notifications_pause_remaining"], 5390)
        restored.resume_notifications(clock.value)
        self.assertFalse(self.controller(clock).snapshot()["notifications_paused"])

    def test_telegram_command_vocabulary(self):
        clock = Clock(self.local_epoch(12))
        policy = self.controller(clock)
        self.assertIn("/manual", policy.handle_command("/help"))
        self.assertIn("/photo", policy.handle_command("/help"))
        self.assertIn("/video", policy.handle_command("/help"))
        self.assertIn("armed_away", policy.handle_command("/away"))
        self.assertEqual(policy.snapshot()["control"], "manual")
        self.assertIn("armed_home", policy.handle_command("/home"))
        self.assertIn("disarmed", policy.handle_command("/disarm"))
        self.assertIn("manual", policy.handle_command("/manual"))
        self.assertIn("automático", policy.handle_command("/auto"))
        self.assertIn("pausadas", policy.handle_command("1h30m"))
        self.assertIn("pausadas", policy.handle_command("/pause 10m"))
        self.assertIn("Modo", policy.handle_command("/status"))
        self.assertIn("reanudadas", policy.handle_command("/resume"))
        self.assertIn("08:00", policy.handle_command("/until8"))
        self.assertIn("No hay incidentes", policy.handle_command("OK"))


if __name__ == "__main__":
    unittest.main()
