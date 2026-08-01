"""Run DVRIP human-event monitoring for multiple independent cameras."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from argparse import ArgumentParser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import cv2
import numpy as np

from dvrip_events import DVRIPAlarmListener
from dvrip_smart_telegram import SmartAlarm, load_windows_user_environment
from onvif_snapshot import download_snapshot, xmeye_snapshot_uri
from telegram_notify import TelegramNotifier


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set.")
    return value


def camera_credentials(config: dict) -> tuple[str, int, str, str]:
    onvif = config["onvif"]
    dvrip = config["dvrip"]
    endpoint = urlparse(onvif["device_service"])
    host = dvrip.get("host", endpoint.hostname)
    if not host:
        raise RuntimeError("Camera host is missing.")
    onvif_user = required_env(onvif.get("username_env", "CAMERA_ONVIF_USER"))
    onvif_password = required_env(onvif.get("password_env", "CAMERA_ONVIF_PASSWORD"))
    user = os.environ.get(dvrip.get("username_env", "CAMERA_DVRIP_USER")) or onvif_user
    password = os.environ.get(dvrip.get("password_env", "CAMERA_DVRIP_PASSWORD")) or onvif_password
    return host, int(dvrip.get("port", 34567)), user, password


class PresenceAlarm:
    """Backyard-style camera: a human event is itself a positive detection."""

    def __init__(self, name: str, config: dict):
        self.name, self.config = name, config
        onvif = config["onvif"]
        self.user = required_env(onvif.get("username_env", "CAMERA_ONVIF_USER"))
        self.password = required_env(onvif.get("password_env", "CAMERA_ONVIF_PASSWORD"))
        snapshot = config["snapshot"]
        self.snapshot_url = snapshot["url"]
        self.query_auth = bool(snapshot.get("xmeye_query_auth", False))
        self.review_dir = Path(snapshot.get("review_dir", f"events/review/{name}"))
        telegram = config["telegram"]
        self.notifier = TelegramNotifier(required_env(telegram["token_env"]), required_env(telegram["chat_id_env"]))
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
            self.notifier.send_photo(data, f"{self.name} camera · human detected")
            print(f"{self.name}: human event saved and sent")
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

    for entry in multi.get("cameras", []):
        name = entry["name"]
        mode = entry["mode"]
        config_path = (multi_path.parent / entry["config"]).resolve()
        config = load_config(config_path)
        host, port, user, password = camera_credentials(config)
        if mode == "property":
            stream = os.environ.get(entry.get("stream_env", "CAMERA_GARAGE_RTSP_URL"))
            if not stream:
                raise RuntimeError(f"{name}: RTSP environment variable is not set.")
            handler = SmartAlarm(config, stream).handle_event
        elif mode == "presence":
            handler = PresenceAlarm(name, config).handle_event
        else:
            raise RuntimeError(f"{name}: unsupported mode {mode!r}.")
        listener = DVRIPAlarmListener(host, port, user, password, handler, lambda message, camera=name: print(f"{camera}: DVRIP {message}"))
        listener.start()
        listeners.append(listener)
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


if __name__ == "__main__":
    main()
