"""Run DVRIP human-event monitoring for multiple independent cameras."""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from argparse import ArgumentParser
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np

from dvrip_events import DVRIPAlarmListener
from dvrip_smart_telegram import SmartAlarm, load_windows_user_environment
from onvif_snapshot import download_snapshot, xmeye_snapshot_uri
from telegram_notify import CameraConnectivityAlerts, TelegramNotifier
from camera_config import camera_name, credentials, dvrip_endpoint, expand_host, required_env, rtsp_url

if TYPE_CHECKING:
    from home_assistant_mqtt import HomeAssistantMQTT


def camera_credentials(config: dict, name: str = "camera") -> tuple[str, int, str, str]:
    host, port = dvrip_endpoint(config, name)
    user, password = credentials(config, name)
    return host, port, user, password


class PresenceAlarm:
    """Backyard-style camera: a human event is itself a positive detection."""

    def __init__(
        self, name: str, config: dict,
        notifier: TelegramNotifier | None = None,
        home_assistant: "HomeAssistantMQTT | None" = None,
    ):
        self.name, self.config = name, config
        self.user, self.password = credentials(config, name)
        snapshot = config["snapshot"]
        self.snapshot_url = expand_host(snapshot["url"], config, name)
        self.query_auth = bool(snapshot.get("xmeye_query_auth", False))
        self.review_dir = Path(snapshot.get("review_dir", f"events/review/{name}"))
        self.notifier = notifier
        self.home_assistant = home_assistant
        self.cooldown = float(config["dvrip"].get("notification_cooldown_seconds", 40))
        self.last_notification = float("-inf")
        self.lock = threading.Lock()
        self.event_debounce = float(config["dvrip"].get("event_debounce_seconds", 3))
        self.last_accepted_event_at = float("-inf")
        self.event_lock = threading.Lock()

    def handle_event(self, event: dict) -> None:
        if event.get("type") != "person" or event.get("status") != "Start":
            return
        with self.event_lock:
            now = time.monotonic()
            if now - self.last_accepted_event_at < self.event_debounce:
                return
            self.last_accepted_event_at = now
        threading.Thread(target=self._handle, daemon=True).start()

    def _handle(self) -> None:
        try:
            occurred_at = datetime.now().astimezone()
            uri = xmeye_snapshot_uri(self.snapshot_url, self.user, self.password) if self.query_auth else self.snapshot_url
            data = download_snapshot(uri, self.user, self.password, 8)
            frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise RuntimeError("Snapshot endpoint returned an unreadable image.")
            self.review_dir.mkdir(parents=True, exist_ok=True)
            stamp = occurred_at.strftime("%Y-%m-%d_%H-%M-%S_%f")
            image_path = self.review_dir / f"{stamp}_human_detected.jpg"
            cv2.imwrite(str(image_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            mqtt_published = False
            if self.home_assistant:
                try:
                    self.home_assistant.publish_detection(
                        self.name, data, "presence", 1, "image", occurred_at,
                    )
                    mqtt_published = True
                except Exception as error:
                    print(f"{self.name}: Home Assistant MQTT publish failed: {type(error).__name__}: {error}")
            if self.notifier is None:
                print(f"{self.name}: human event saved" + (" and published" if mqtt_published else ""))
                return
            with self.lock:
                remaining = self.cooldown - (time.monotonic() - self.last_notification)
                if remaining > 0:
                    print(f"{self.name}: Telegram notification suppressed ({remaining:.1f}s cooldown remaining)")
                    return
                self.last_notification = time.monotonic()
            sent = self.notifier.send_photo(data, f"{self.name} camera · human detected")
            if sent:
                suffix = " and sent"
            elif mqtt_published:
                suffix = " and published (direct Telegram muted)"
            else:
                suffix = " (Telegram muted)"
            print(f"{self.name}: human event saved{suffix}")
        except Exception as error:
            print(f"{self.name}: snapshot notification failed: {type(error).__name__}: {error}")


def load_config(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as source:
            return json.load(source)
    except json.JSONDecodeError as error:
        raise SystemExit(
            f"Invalid JSON in {path.resolve()}\n"
            f"Line {error.lineno}, column {error.colno}: {error.msg}.\n"
            "Check commas between entries and double quotes around text values."
        ) from None


def main() -> None:
    parser = ArgumentParser(description="Run smart DVRIP monitoring for multiple cameras.")
    parser.add_argument("--config", default="multi-camera.json")
    args = parser.parse_args()
    load_windows_user_environment()
    multi_path = Path(args.config).resolve()
    multi = load_config(multi_path)
    listeners: list[DVRIPAlarmListener] = []
    connectivity_alerts: list[CameraConnectivityAlerts] = []
    output_mode = str(multi.get("outputs", {}).get("mode", "telegram")).lower()
    if output_mode not in {"telegram", "mqtt", "both"}:
        raise RuntimeError("outputs.mode must be telegram, mqtt, or both.")
    home_assistant = None
    presence_monitor = None
    if output_mode in {"mqtt", "both"}:
        from home_assistant_mqtt import HomeAssistantMQTT

        try:
            home_assistant = HomeAssistantMQTT(multi.get("home_assistant", {}).get("mqtt", {})).start()
        except Exception as error:
            if output_mode == "mqtt":
                raise
            print(f"Home Assistant MQTT unavailable; continuing with Telegram: {type(error).__name__}: {error}")

    presence_config = multi.get("presence", {})
    if home_assistant and presence_config.get("enabled", False):
        from router_presence import RouterPresenceMonitor

        presence_monitor = RouterPresenceMonitor(
            presence_config, home_assistant.publish_presence,
        ).start()

    for entry in multi.get("cameras", []):
        name = entry.get("name")
        mode = entry["mode"]
        config_path = (multi_path.parent / entry["config"]).resolve()
        config = load_config(config_path)
        name = name or camera_name(config)
        host, port, user, password = camera_credentials(config, name)
        notifier = None
        if output_mode in {"telegram", "both"}:
            telegram = config["telegram"]
            notifier = TelegramNotifier(
                required_env(telegram["token_env"]), required_env(telegram["chat_id_env"]),
            ).start_command_listener()
        if home_assistant:
            home_assistant.register_camera(name)
        if mode == "property":
            stream = os.environ.get(entry["stream_env"]) if entry.get("stream_env") else rtsp_url(config, name)
            if not stream:
                raise RuntimeError(f"{name}: its CAMERA_<NAME>_RTSP_URL environment variable is not set.")
            alarm = SmartAlarm(
                config, stream, notifier=notifier,
                home_assistant=home_assistant, name=name,
            )
            handler = alarm.handle_event
        elif mode == "presence":
            alarm = PresenceAlarm(name, config, notifier=notifier, home_assistant=home_assistant)
            handler = alarm.handle_event
        else:
            raise RuntimeError(f"{name}: unsupported mode {mode!r}.")
        connectivity = CameraConnectivityAlerts(name, notifier) if notifier else None

        def set_connectivity(online: bool, camera=name, alerts=connectivity) -> None:
            if alerts:
                alerts.set_online(online)
            if home_assistant:
                home_assistant.set_camera_online(camera, online)

        listener = DVRIPAlarmListener(
            host, port, user, password, handler,
            lambda message, camera=name: print(f"{camera}: DVRIP {message}"),
            on_connectivity=set_connectivity,
        )
        listener.start()
        listeners.append(listener)
        if connectivity:
            connectivity_alerts.append(connectivity)
        print(f"{name}: listener started ({mode})")

    if not listeners:
        raise RuntimeError("No cameras were configured.")
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame) -> None:
        stop_event.set()

    # launchd stops a LaunchAgent with SIGTERM. Handle it as a clean shutdown
    # so MQTT publishes its offline state and camera listener threads close.
    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    print("Monitoring all cameras. Press Ctrl+C to stop.")
    try:
        while not stop_event.wait(1):
            pass
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        for listener in listeners:
            listener.stop()
        for connectivity in connectivity_alerts:
            connectivity.stop()
        if presence_monitor:
            presence_monitor.stop()
        if home_assistant:
            home_assistant.stop()
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    main()
