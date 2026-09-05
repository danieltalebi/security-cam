"""Run DVRIP human-event monitoring for multiple independent cameras."""

from __future__ import annotations

import json
import os
import threading
import time
from argparse import ArgumentParser
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from dvrip_events import DVRIPAlarmListener
from dvrip_smart_telegram import SmartAlarm, load_windows_user_environment
from onvif_snapshot import download_snapshot, xmeye_snapshot_uri
from telegram_notify import CameraConnectivityAlerts, TelegramNotifier
from camera_config import camera_name, credentials, dvrip_endpoint, required_env, rtsp_url


def camera_credentials(config: dict) -> tuple[str, int, str, str]:
    host, port = dvrip_endpoint(config)
    user, password = credentials(config)
    return host, port, user, password


class PresenceAlarm:
    """Backyard-style camera: a human event is itself a positive detection."""

    def __init__(self, name: str, config: dict):
        self.name, self.config = name, config
        self.user, self.password = credentials(config, name)
        snapshot = config["snapshot"]
        self.snapshot_url = snapshot["url"]
        self.query_auth = bool(snapshot.get("xmeye_query_auth", False))
        self.review_dir = Path(snapshot.get("review_dir", f"events/review/{name}"))
        telegram = config["telegram"]
        self.notifier = TelegramNotifier(required_env(telegram["token_env"]), required_env(telegram["chat_id_env"])).start_command_listener()
        self.cooldown = float(config["dvrip"].get("notification_cooldown_seconds", 40))
        self.last_notification = float("-inf")
        self.lock = threading.Lock()

    def handle_event(self, event: dict) -> None:
        if event.get("type") != "person" or event.get("status") != "Start":
            return
        threading.Thread(target=self._handle, daemon=True).start()

    def _handle(self) -> None:
        try:
            uri = xmeye_snapshot_uri(self.snapshot_url, self.user, self.password) if self.query_auth else self.snapshot_url
            data = download_snapshot(uri, self.user, self.password, 8)
            frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise RuntimeError("Snapshot endpoint returned an unreadable image.")
            self.review_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S_%f")
            image_path = self.review_dir / f"{stamp}_human_detected.jpg"
            cv2.imwrite(str(image_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            with self.lock:
                remaining = self.cooldown - (time.monotonic() - self.last_notification)
                if remaining > 0:
                    print(f"{self.name}: Telegram notification suppressed ({remaining:.1f}s cooldown remaining)")
                    return
                self.last_notification = time.monotonic()
            sent = self.notifier.send_photo(data, f"{self.name} camera · human detected")
            print(f"{self.name}: human event saved" + (" and sent" if sent else " (Telegram muted)"))
        except Exception as error:
            print(f"{self.name}: snapshot notification failed: {type(error).__name__}: {error}")


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def main() -> None:
    parser = ArgumentParser(description="Run smart DVRIP monitoring for multiple cameras.")
    parser.add_argument("--config", default="multi-camera.json")
    args = parser.parse_args()
    load_windows_user_environment()
    multi_path = Path(args.config).resolve()
    multi = load_config(multi_path)
    listeners: list[DVRIPAlarmListener] = []
    connectivity_alerts: list[CameraConnectivityAlerts] = []

    for entry in multi.get("cameras", []):
        name = entry.get("name")
        mode = entry["mode"]
        config_path = (multi_path.parent / entry["config"]).resolve()
        config = load_config(config_path)
        name = name or camera_name(config)
        host, port, user, password = camera_credentials(config)
        if mode == "property":
            stream = os.environ.get(entry["stream_env"]) if entry.get("stream_env") else rtsp_url(config, name)
            if not stream:
                raise RuntimeError(f"{name}: its CAMERA_<NAME>_RTSP_URL environment variable is not set.")
            alarm = SmartAlarm(config, stream)
            handler, notifier = alarm.handle_event, alarm.telegram
        elif mode == "presence":
            alarm = PresenceAlarm(name, config)
            handler, notifier = alarm.handle_event, alarm.notifier
        else:
            raise RuntimeError(f"{name}: unsupported mode {mode!r}.")
        connectivity = CameraConnectivityAlerts(name, notifier)
        listener = DVRIPAlarmListener(
            host, port, user, password, handler,
            lambda message, camera=name: print(f"{camera}: DVRIP {message}"),
            on_connectivity=connectivity.set_online,
        )
        listener.start()
        listeners.append(listener)
        connectivity_alerts.append(connectivity)
        print(f"{name}: listener started ({mode})")

    if not listeners:
        raise RuntimeError("No cameras were configured.")
    print("Monitoring all cameras. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        for listener in listeners:
            listener.stop()
        for connectivity in connectivity_alerts:
            connectivity.stop()


if __name__ == "__main__":
    main()
