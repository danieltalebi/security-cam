"""Publish confirmed camera detections to Home Assistant over MQTT."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime
from typing import Callable

import paho.mqtt.client as mqtt


ALARM_MODES = {"disarmed", "armed_home", "armed_away"}


def mqtt_slug(value: str) -> str:
    """Return a stable MQTT/Home Assistant identifier."""
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    if not slug:
        raise ValueError("Camera name must contain a letter or number.")
    return slug


def _configured_value(config: dict, key: str, default_env: str, default=None):
    env_name = config.get(f"{key}_env", default_env)
    environment_value = os.environ.get(env_name) if env_name else None
    return environment_value if environment_value is not None else config.get(key, default)


class HomeAssistantMQTT:
    """One MQTT connection shared by every camera in the monitor process."""

    def __init__(self, config: dict):
        host = _configured_value(config, "host", "HA_MQTT_HOST")
        if not host:
            raise RuntimeError("Set HA_MQTT_HOST (a DNS name such as homeassistant.local is recommended).")
        port_value = _configured_value(config, "port", "HA_MQTT_PORT", 1883)
        self.host = str(host)
        self.port = int(port_value or 1883)
        self.topic_prefix = str(config.get("topic_prefix", "security_cam")).strip("/")
        self.discovery_prefix = str(config.get("discovery_prefix", "homeassistant")).strip("/")
        self.qos = int(config.get("qos", 1))
        self._connected = threading.Event()
        self._registered: dict[str, str] = {}
        self._camera_status: dict[str, bool] = {}
        self._presence_state: tuple[str, dict] | None = None
        self._local_alarm_state: dict | None = None
        self._alarm_mode: str | None = None
        self._alarm_mode_lock = threading.Lock()
        self._command_handler: Callable[[str], str | None] | None = None
        self._detection_handler: Callable[[dict], str | None] | None = None

        client_id = str(config.get("client_id") or f"security-cam-{uuid.uuid4().hex[:8]}")
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        username = _configured_value(config, "username", "HA_MQTT_USER")
        password = _configured_value(config, "password", "HA_MQTT_PASSWORD")
        if username:
            self.client.username_pw_set(str(username), str(password or ""))
        if config.get("tls", False):
            self.client.tls_set(ca_certs=config.get("ca_certs"))
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.will_set(self._topic("app", "status"), "offline", qos=self.qos, retain=True)

    def _topic(self, *parts: str) -> str:
        return "/".join((self.topic_prefix, *parts))

    def start(self, timeout: float = 10.0) -> "HomeAssistantMQTT":
        self.client.connect(self.host, self.port, keepalive=45)
        self.client.loop_start()
        if not self._connected.wait(timeout):
            self.client.loop_stop()
            self.client.disconnect()
            raise TimeoutError(f"MQTT broker {self.host}:{self.port} did not accept the connection.")
        return self

    def stop(self) -> None:
        if self._connected.is_set():
            self.client.publish(self._topic("app", "status"), "offline", qos=self.qos, retain=True).wait_for_publish(5)
        self.client.disconnect()
        self.client.loop_stop()
        self._connected.clear()

    def _on_connect(self, _client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code != 0:
            print(f"MQTT connection rejected: {reason_code}")
            return
        self._connected.set()
        self.client.subscribe(self._topic("alarm", "mode"), qos=self.qos)
        self.client.subscribe(self._topic("alarm", "command"), qos=self.qos)
        self.client.subscribe(self._topic("interior", "event"), qos=self.qos)
        self.client.publish(self._topic("app", "status"), "online", qos=self.qos, retain=True)
        for name in list(self._registered.values()):
            self._publish_discovery(name)
        for slug, online in list(self._camera_status.items()):
            self.client.publish(
                self._topic(slug, "status"), "online" if online else "offline",
                qos=self.qos, retain=True,
            )
        self._publish_presence_discovery()
        self._publish_alarm_discovery()
        if self._presence_state:
            self.publish_presence(*self._presence_state)
        if self._local_alarm_state:
            self.publish_alarm_state(self._local_alarm_state)

    def _on_disconnect(self, _client, _userdata, _disconnect_flags, _reason_code, _properties) -> None:
        self._connected.clear()

    def _on_message(self, _client, _userdata, message) -> None:
        try:
            payload = message.payload.decode("utf-8").strip()
        except (AttributeError, UnicodeDecodeError):
            return
        if message.topic == self._topic("alarm", "command"):
            if not self._command_handler:
                print("Ignoring Home Assistant alarm command: no local handler is registered")
                return
            result = self._command_handler(payload)
            if result is None:
                print(f"Ignoring unsupported Home Assistant alarm command: {payload!r}")
            else:
                print(f"Home Assistant alarm command applied locally: {result}")
            return
        if message.topic == self._topic("interior", "event"):
            try:
                event = json.loads(payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                print("Ignoring invalid Home Assistant interior event payload")
                return
            if not isinstance(event, dict) or event.get("event_type") != "presence_detected":
                print("Ignoring unsupported Home Assistant interior event")
                return
            if not self._detection_handler:
                print("Ignoring Home Assistant interior event: no local handler is registered")
                return
            result = self._detection_handler(event)
            if result:
                print(f"Home Assistant interior event handled locally: {result}")
            return
        if message.topic == self._topic("alarm", "mode"):
            if payload not in ALARM_MODES:
                print(f"Ignoring unsupported Home Assistant alarm mode: {payload!r}")
                return
            with self._alarm_mode_lock:
                self._alarm_mode = payload
            print(f"MQTT alarm mode mirror received: {payload}")

    def set_command_handler(
        self, handler: Callable[[str], str | None],
    ) -> "HomeAssistantMQTT":
        """Route explicit HA operator commands into the local controller."""
        self._command_handler = handler
        return self

    def set_detection_handler(
        self, handler: Callable[[dict], str | None],
    ) -> "HomeAssistantMQTT":
        """Route raw HA sensor events into the local alarm policy."""
        self._detection_handler = handler
        return self

    @property
    def alarm_mode(self) -> str | None:
        with self._alarm_mode_lock:
            return self._alarm_mode

    def should_verify_camera_events(self) -> bool:
        """Compatibility shim: evidence capture is never gated by Home Assistant."""
        return True

    def _publish_checked(
        self, topic: str, payload, *, retain: bool = False, timeout: float = 5.0,
    ) -> None:
        """Publish a live event without filling Paho's offline queue."""
        if not self._connected.is_set():
            raise ConnectionError("MQTT is disconnected; live event was not queued.")
        result = self.client.publish(
            topic, payload, qos=self.qos, retain=retain,
        )
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError(
                f"MQTT rejected the live event: {mqtt.error_string(result.rc)}"
            )
        try:
            result.wait_for_publish(timeout)
        except ValueError as error:
            raise ConnectionError("MQTT could not queue the live event.") from error
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError(
                f"MQTT did not publish the live event: {mqtt.error_string(result.rc)}"
            )

    def publish_alarm_state(self, state: dict) -> None:
        """Mirror the authoritative local alarm state into Home Assistant."""
        mode = str(state.get("mode", "disarmed"))
        if mode not in ALARM_MODES:
            raise ValueError(f"Unsupported local alarm mode: {mode!r}")
        payload = {
            "mode": mode,
            "control": state.get("control", "automatic"),
            "presence": state.get("presence", "unavailable"),
            "presence_devices": state.get("presence_devices", {}),
            "notifications_paused": bool(state.get("notifications_paused", False)),
            "active_incidents": [
                {
                    "id": item.get("id"),
                    "camera": item.get("camera"),
                    "classification": item.get("classification"),
                    "acknowledged": bool(item.get("acknowledged")),
                }
                for item in state.get("active_incidents", [])
            ],
            "updated_at": state.get("updated_at"),
        }
        self._local_alarm_state = payload
        if not self._connected.is_set():
            return
        self.client.publish(self._topic("alarm", "mode"), mode, qos=self.qos, retain=True)
        self.client.publish(
            self._topic("alarm", "state"),
            json.dumps(payload, separators=(",", ":")), qos=self.qos, retain=True,
        )

    def publish_incident(self, incident: dict, event_type: str) -> None:
        """Publish local incident lifecycle events without influencing policy."""
        if not self._connected.is_set():
            return
        payload = dict(incident)
        payload["event_type"] = event_type
        self.client.publish(
            self._topic("alarm", "incident"),
            json.dumps(payload, separators=(",", ":")), qos=self.qos, retain=False,
        )

    def _publish_alarm_discovery(self) -> None:
        if not self._connected.is_set():
            return
        device = {
            "identifiers": ["security_camera_local_alarm"],
            "name": "Security camera local alarm",
            "manufacturer": "security-cam",
            "model": "Python alarm policy",
        }
        sensor = {
            "name": "Alarm mode",
            "unique_id": "security_camera_local_alarm_mode",
            "default_entity_id": "sensor.security_camera_alarm_mode",
            "state_topic": self._topic("alarm", "mode"),
            "json_attributes_topic": self._topic("alarm", "state"),
            "availability_topic": self._topic("app", "status"),
            "icon": "mdi:shield-home",
            "device": device,
        }
        event = {
            "name": "Incident lifecycle",
            "unique_id": "security_camera_local_incident",
            "default_entity_id": "event.security_camera_incident",
            "state_topic": self._topic("alarm", "incident"),
            "event_types": ["incident_notified"],
            "availability_topic": self._topic("app", "status"),
            "device": device,
        }
        self.client.publish(
            f"{self.discovery_prefix}/sensor/security_camera_local_alarm/mode/config",
            json.dumps(sensor), qos=self.qos, retain=True,
        )
        self.client.publish(
            f"{self.discovery_prefix}/event/security_camera_local_alarm/incident/config",
            json.dumps(event), qos=self.qos, retain=True,
        )
        command_topic = self._topic("alarm", "command")
        buttons = {
            "automatic": ("Automatic control", "automatic", "mdi:shield-sync", "/auto"),
            "disarm": ("Disarm", "disarm", "mdi:shield-off-outline", "/disarm"),
            "arm_home": ("Arm at home", "arm_at_home", "mdi:shield-home-outline", "/home"),
            "arm_away": ("Arm away", "arm_away", "mdi:shield-lock-outline", "/away"),
            "pause_1h": ("Pause alerts 1 hour", "pause_alerts_1_hour", "mdi:timer-pause", "1h"),
            "resume": ("Resume alerts", "resume_alerts", "mdi:bell-ring-outline", "/resume"),
            "acknowledge": ("Acknowledge incidents", "acknowledge_incidents", "mdi:check-decagram", "OK"),
        }
        for slug, (name, entity_slug, icon, payload_press) in buttons.items():
            button = {
                "name": name,
                "unique_id": f"security_camera_local_alarm_{slug}",
                "default_entity_id": f"button.security_camera_{entity_slug}",
                "command_topic": command_topic,
                "payload_press": payload_press,
                "availability_topic": self._topic("app", "status"),
                "icon": icon,
                "device": device,
            }
            self.client.publish(
                f"{self.discovery_prefix}/button/security_camera_local_alarm/{slug}/config",
                json.dumps(button), qos=self.qos, retain=True,
            )

    def register_camera(self, name: str) -> None:
        slug = mqtt_slug(name)
        self._registered[slug] = name
        if self._connected.is_set():
            self._publish_discovery(name)

    def set_camera_online(self, name: str, online: bool) -> None:
        slug = mqtt_slug(name)
        self._camera_status[slug] = online
        if self._connected.is_set():
            self.client.publish(
                self._topic(slug, "status"), "online" if online else "offline",
                qos=self.qos, retain=True,
            )

    def publish_detection(
        self, name: str, image: bytes, classification: str, persons: int,
        source: str, occurred_at: datetime,
    ) -> str:
        """Publish the JPEG first, then its event so HA snapshots the right frame."""
        slug = mqtt_slug(name)
        if slug not in self._registered:
            self.register_camera(name)
        event_id = uuid.uuid4().hex
        self._publish_checked(self._topic(slug, "snapshot"), image)
        payload = {
            "event_type": "person_detected",
            "event_id": event_id,
            "camera": name,
            "camera_slug": slug,
            "camera_entity_id": f"camera.security_cam_{slug}",
            "classification": classification,
            "persons": int(persons),
            "source": source,
            "occurred_at": occurred_at.isoformat(),
        }
        self._publish_checked(
            self._topic(slug, "event"),
            json.dumps(payload, separators=(",", ":")),
        )
        return event_id

    def publish_verification_failure(
        self, name: str, reason: str, occurred_at: datetime,
    ) -> str:
        """Report that an armed camera trigger could not be visually checked."""
        slug = mqtt_slug(name)
        if slug not in self._registered:
            self.register_camera(name)
        event_id = uuid.uuid4().hex
        payload = {
            "event_type": "verification_failed",
            "event_id": event_id,
            "camera": name,
            "camera_slug": slug,
            "reason": reason,
            "occurred_at": occurred_at.isoformat(),
        }
        self._publish_checked(
            self._topic(slug, "event"),
            json.dumps(payload, separators=(",", ":")),
        )
        return event_id

    def publish_presence(self, state: str, attributes: dict) -> None:
        """Publish fail-safe combined router presence and diagnostics."""
        if state not in {"home", "away", "unavailable"}:
            raise ValueError(f"Unsupported presence state: {state}")
        self._presence_state = (state, attributes)
        if not self._connected.is_set():
            return
        self._publish_presence_discovery()
        self.client.publish(
            self._topic("presence", "attributes"),
            json.dumps(attributes, separators=(",", ":")),
            qos=self.qos,
            retain=True,
        )
        if state == "unavailable":
            self.client.publish(
                self._topic("presence", "availability"), "offline",
                qos=self.qos, retain=True,
            )
            return
        self.client.publish(
            self._topic("presence", "state"),
            "ON" if state == "home" else "OFF",
            qos=self.qos,
            retain=True,
        )
        self.client.publish(
            self._topic("presence", "availability"), "online",
            qos=self.qos, retain=True,
        )

    def _publish_presence_discovery(self) -> None:
        if not self._connected.is_set():
            return
        payload = {
            "name": "Phone Wi-Fi presence",
            "unique_id": "security_camera_phone_wifi_presence",
            "default_entity_id": "binary_sensor.security_camera_phone_wifi_presence",
            "device_class": "presence",
            "state_topic": self._topic("presence", "state"),
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability_topic": self._topic("presence", "availability"),
            "json_attributes_topic": self._topic("presence", "attributes"),
            "device": {
                "identifiers": ["security_camera_presence"],
                "name": "Security camera presence",
                "manufacturer": "security-cam",
                "model": "Router presence bridge",
            },
        }
        topic = f"{self.discovery_prefix}/binary_sensor/security_camera_presence/phone/config"
        self.client.publish(topic, json.dumps(payload), qos=self.qos, retain=True)

    def _publish_discovery(self, name: str) -> None:
        slug = mqtt_slug(name)
        identifier = f"security_cam_{slug}"
        device = {
            "identifiers": [identifier],
            "name": f"Security camera {name}",
            "manufacturer": "security-cam",
            "model": "DVRIP event bridge",
        }
        availability = [
            {"topic": self._topic("app", "status")},
            {"topic": self._topic(slug, "status")},
        ]
        camera_config = {
            "name": "Last detection",
            "unique_id": f"{identifier}_snapshot",
            "default_entity_id": f"camera.{identifier}",
            "topic": self._topic(slug, "snapshot"),
            "availability": availability,
            "availability_mode": "all",
            "device": device,
        }
        event_config = {
            "name": "Camera event",
            "unique_id": f"{identifier}_person",
            "default_entity_id": f"event.{identifier}_person",
            "state_topic": self._topic(slug, "event"),
            "event_types": ["person_detected", "verification_failed"],
            "availability": availability,
            "availability_mode": "all",
            "device": device,
        }
        for component, suffix, payload in (
            ("camera", "snapshot", camera_config),
            ("event", "person", event_config),
        ):
            topic = f"{self.discovery_prefix}/{component}/{identifier}/{suffix}/config"
            self.client.publish(topic, json.dumps(payload), qos=self.qos, retain=True)
