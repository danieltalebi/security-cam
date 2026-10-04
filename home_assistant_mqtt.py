"""Publish confirmed camera detections to Home Assistant over MQTT."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime

import paho.mqtt.client as mqtt


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
        self.client.publish(self._topic("app", "status"), "online", qos=self.qos, retain=True)
        for name in list(self._registered.values()):
            self._publish_discovery(name)
        for slug, online in list(self._camera_status.items()):
            self.client.publish(
                self._topic(slug, "status"), "online" if online else "offline",
                qos=self.qos, retain=True,
            )
        self._publish_presence_discovery()
        if self._presence_state:
            self.publish_presence(*self._presence_state)

    def _on_disconnect(self, _client, _userdata, _disconnect_flags, _reason_code, _properties) -> None:
        self._connected.clear()

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
        image_result = self.client.publish(
            self._topic(slug, "snapshot"), image, qos=self.qos, retain=False,
        )
        image_result.wait_for_publish(5)
        if image_result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError("MQTT did not accept the detection image.")
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
        event_result = self.client.publish(
            self._topic(slug, "event"), json.dumps(payload, separators=(",", ":")),
            qos=self.qos, retain=False,
        )
        event_result.wait_for_publish(5)
        if event_result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError("MQTT did not accept the camera event.")
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
            "name": "Person detection",
            "unique_id": f"{identifier}_person",
            "default_entity_id": f"event.{identifier}_person",
            "state_topic": self._topic(slug, "event"),
            "event_types": ["person_detected"],
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
