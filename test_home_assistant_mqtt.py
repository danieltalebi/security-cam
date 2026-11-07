import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from camera_config import camera_host, dvrip_endpoint, expand_host, rtsp_url
from home_assistant_mqtt import HomeAssistantMQTT, mqtt_slug


class HomeAssistantMQTTTests(unittest.TestCase):
    def setUp(self):
        client_patch = patch("home_assistant_mqtt.mqtt.Client")
        self.addCleanup(client_patch.stop)
        self.client_class = client_patch.start()
        self.client = self.client_class.return_value
        self.published = []

        def publish(topic, payload, **kwargs):
            self.published.append((topic, payload, kwargs))
            result = MagicMock()
            result.rc = 0
            return result

        self.client.publish.side_effect = publish
        self.bridge = HomeAssistantMQTT({"host": "ha.test", "topic_prefix": "security_cam"})
        self.bridge._connected.set()

    def test_discovery_and_detection_order(self):
        self.bridge.register_camera("Back Patio")
        camera_discovery = json.loads(self.published[0][1])
        self.assertEqual(camera_discovery["topic"], "security_cam/back_patio/snapshot")
        self.assertNotIn("image_topic", camera_discovery)
        self.bridge.publish_detection(
            "Back Patio", b"jpeg", "presence", 1, "image",
            datetime(2026, 9, 11, tzinfo=timezone.utc),
        )

        self.assertEqual(mqtt_slug("Back Patio"), "back_patio")
        self.assertEqual(self.published[-2][0], "security_cam/back_patio/snapshot")
        self.assertEqual(self.published[-2][1], b"jpeg")
        self.assertEqual(self.published[-1][0], "security_cam/back_patio/event")
        event = json.loads(self.published[-1][1])
        self.assertEqual(event["classification"], "presence")
        self.assertEqual(event["source"], "image")
        self.assertEqual(event["camera_entity_id"], "camera.security_cam_back_patio")

    def test_camera_status_is_retained(self):
        self.bridge.set_camera_online("garage", False)
        topic, payload, options = self.published[-1]
        self.assertEqual((topic, payload), ("security_cam/garage/status", "offline"))
        self.assertTrue(options["retain"])

    def test_received_alarm_mode_is_mirrored_but_never_gates_verification(self):
        self.bridge._on_connect(None, None, None, 0, None)
        self.client.subscribe.assert_any_call("security_cam/alarm/mode", qos=1)
        self.client.subscribe.assert_any_call("security_cam/alarm/command", qos=1)
        self.client.subscribe.assert_any_call("security_cam/interior/event", qos=1)
        self.assertTrue(self.bridge.should_verify_camera_events())

        message = MagicMock(topic="security_cam/alarm/mode", payload=b"disarmed")
        self.bridge._on_message(None, None, message)
        self.assertEqual(self.bridge.alarm_mode, "disarmed")
        self.assertTrue(self.bridge.should_verify_camera_events())

        message.payload = b"armed_away"
        self.bridge._on_message(None, None, message)
        self.assertTrue(self.bridge.should_verify_camera_events())

    def test_explicit_home_assistant_command_uses_local_policy_handler(self):
        handler = MagicMock(return_value="Control manual. Modo: armed_away.")
        self.assertIs(self.bridge.set_command_handler(handler), self.bridge)

        message = MagicMock(topic="security_cam/alarm/command", payload=b"/away")
        self.bridge._on_message(None, None, message)

        handler.assert_called_once_with("/away")

    def test_interior_event_uses_local_detection_handler(self):
        handler = MagicMock(return_value="interior presence accepted")
        self.assertIs(self.bridge.set_detection_handler(handler), self.bridge)
        message = MagicMock(
            topic="security_cam/interior/event",
            payload=json.dumps({
                "event_type": "presence_detected",
                "entity_id": "binary_sensor.sensor_de_presencia_humana_motion",
                "occurred_at": "2026-11-07T23:30:00+11:00",
            }).encode(),
        )

        self.bridge._on_message(None, None, message)

        handler.assert_called_once()
        self.assertEqual(handler.call_args.args[0]["event_type"], "presence_detected")

    def test_alarm_discovery_exposes_operator_buttons_on_command_topic(self):
        self.bridge._publish_alarm_discovery()
        discovered = {
            topic: json.loads(payload)
            for topic, payload, _options in self.published
            if "/button/security_camera_local_alarm/" in topic
        }
        self.assertEqual(len(discovered), 7)
        self.assertTrue(all(
            payload["command_topic"] == "security_cam/alarm/command"
            for payload in discovered.values()
        ))
        self.assertEqual(
            discovered[
                "homeassistant/button/security_camera_local_alarm/arm_away/config"
            ]["payload_press"],
            "/away",
        )
        self.assertEqual(
            discovered[
                "homeassistant/button/security_camera_local_alarm/pause_1h/config"
            ]["payload_press"],
            "1h",
        )

    def test_verification_failure_is_published_as_camera_event(self):
        self.bridge.register_camera("Backyard")
        self.bridge.publish_verification_failure(
            "Backyard", "snapshot and RTSP unavailable",
            datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        topic, raw_payload, _options = self.published[-1]
        self.assertEqual(topic, "security_cam/backyard/event")
        payload = json.loads(raw_payload)
        self.assertEqual(payload["event_type"], "verification_failed")
        self.assertEqual(payload["camera_slug"], "backyard")

    def test_disconnected_detection_is_not_added_to_the_offline_queue(self):
        self.bridge.register_camera("Backyard")
        before = len(self.published)
        self.bridge._connected.clear()

        with self.assertRaisesRegex(ConnectionError, "not queued"):
            self.bridge.publish_detection(
                "Backyard", b"jpeg", "presence", 1, "image",
                datetime(2026, 9, 11, tzinfo=timezone.utc),
            )

        self.assertEqual(len(self.published), before)

    def test_mqtt_queue_error_is_reported_as_connection_error(self):
        result = MagicMock(rc=15)
        self.client.publish.side_effect = None
        self.client.publish.return_value = result

        with self.assertRaisesRegex(ConnectionError, "rejected"):
            self.bridge.publish_verification_failure(
                "Backyard", "snapshot unavailable",
                datetime(2026, 9, 11, tzinfo=timezone.utc),
            )

        result.wait_for_publish.assert_not_called()

    def test_local_alarm_state_is_published_as_authoritative_mirror(self):
        self.bridge.publish_alarm_state({
            "mode": "armed_home",
            "control": "automatic",
            "presence": "home",
            "presence_devices": {"phone": True},
            "notifications_paused": False,
            "active_incidents": [],
        })
        self.assertEqual(self.published[-2][:2], ("security_cam/alarm/mode", "armed_home"))
        self.assertTrue(self.published[-2][2]["retain"])
        topic, raw, options = self.published[-1]
        self.assertEqual(topic, "security_cam/alarm/state")
        self.assertEqual(json.loads(raw)["control"], "automatic")
        self.assertTrue(options["retain"])

    def test_incident_event_preserves_local_policy_context(self):
        self.bridge.publish_incident({
            "id": "incident-1",
            "camera": "garage",
            "classification": "private",
            "alarm_mode": "armed_away",
            "alarm_control": "automatic",
            "presence": "away",
        }, "incident_notified")

        topic, raw, options = self.published[-1]
        self.assertEqual(topic, "security_cam/alarm/incident")
        payload = json.loads(raw)
        self.assertEqual(payload["event_type"], "incident_notified")
        self.assertEqual(payload["alarm_mode"], "armed_away")
        self.assertEqual(payload["presence"], "away")
        self.assertFalse(options["retain"])


class CameraHostTests(unittest.TestCase):
    def setUp(self):
        configured_patch = patch(
            "camera_config.configured_value", side_effect=lambda name: os.environ.get(name),
        )
        self.addCleanup(configured_patch.stop)
        configured_patch.start()

    def test_one_host_expands_every_endpoint(self):
        config = {
            "camera": {
                "name": "garage", "credential_prefix": "CAMERA_GARAGE",
                "host": "garage-camera.local", "rtsp_url": "rtsp://{host}/main",
            },
            "dvrip": {"host": "{host}", "port": 34567},
            "onvif": {"device_service": "http://{host}:8899/onvif/device_service"},
        }
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(camera_host(config), "garage-camera.local")
            self.assertEqual(dvrip_endpoint(config), ("garage-camera.local", 34567))
            self.assertEqual(rtsp_url(config), "rtsp://garage-camera.local/main")
            self.assertEqual(
                expand_host("http://{host}/snapshot.jpg", config),
                "http://garage-camera.local/snapshot.jpg",
            )

    def test_host_environment_override_moves_camera_once(self):
        config = {
            "camera": {"name": "garage", "credential_prefix": "CAMERA_GARAGE", "host": "old.local"},
            "dvrip": {"host": "{host}", "port": 34567},
        }
        with patch.dict(os.environ, {"CAMERA_GARAGE_HOST": "new.local"}, clear=True):
            self.assertEqual(dvrip_endpoint(config), ("new.local", 34567))

    def test_host_override_migrates_legacy_urls(self):
        config = {
            "dvrip": {"host": "192.168.1.20", "port": 34567},
            "onvif": {"device_service": "http://192.168.1.20:8899/onvif/device_service"},
        }
        with patch.dict(os.environ, {"CAMERA_GARAGE_HOST": "garage.local"}, clear=True):
            self.assertEqual(dvrip_endpoint(config, "garage"), ("garage.local", 34567))
            self.assertEqual(
                expand_host("http://192.168.1.20/snapshot.jpg", config, "garage"),
                "http://garage.local/snapshot.jpg",
            )


if __name__ == "__main__":
    unittest.main()
