"""DVRIP alarm -> JPEG classification -> Telegram, with RTSP only as fallback."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from argparse import ArgumentParser
from datetime import datetime

import cv2
import numpy as np
from ultralytics import YOLO

from dvrip_events import DVRIPAlarmListener
from onvif_snapshot import download_snapshot, xmeye_snapshot_uri
from public_area import PublicAreaClassifier
from telegram_notify import TelegramNotifier
from camera_config import credentials, dvrip_endpoint, required_env, rtsp_url
from camera_config import camera_name


def load_windows_user_environment() -> None:
    """Refresh secrets saved by the Windows setup assistant.

    `setx` affects future processes only. Reading the user environment from the
    registry prevents a launcher started by an older Explorer/terminal process
    from inheriting stale Telegram or camera values.
    """
    if os.name != "nt":
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            index = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, index)
                except FileNotFoundError:
                    break
                except OSError:
                    break
                index += 1
                if value and (name.startswith("CAMERA_") or name in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}):
                    os.environ[name] = str(value)
    except OSError:
        # Environment variables supplied by the launcher still work when the
        # registry cannot be read (for example, a restricted service account).
        pass


class SmartAlarm:
    def __init__(self, config: dict, stream: str):
        self.config, self.stream = config, stream
        self.name = camera_name(config)
        self.layout = config["layout"]
        self.width = max(bounds[2] for bounds in self.layout.values())
        self.height = max(bounds[3] for bounds in self.layout.values())
        self.username, self.password = credentials(config)
        telegram = config["telegram"]
        self.telegram = TelegramNotifier(required_env(telegram["token_env"]), required_env(telegram["chat_id_env"])).start_command_listener()
        self.snapshot_url = config["snapshot"]["url"]
        self.snapshot_query_auth = bool(config["snapshot"].get("xmeye_query_auth", False))
        policy = config["public_area_classifier"]
        self.public_threshold = float(policy["public_threshold"])
        self.private_threshold = float(policy["private_threshold"])
        self.classifier = PublicAreaClassifier(policy["model_path"], tuple(self.layout["ptz"]), policy.get("device", "auto"))
        self.model = YOLO("yolov8n.pt")
        self.video_seconds = float(config["snapshot"].get("video_fallback_seconds", 8))
        self.interval = float(config["snapshot"].get("video_sample_seconds", 0.8))
        self.review_dir = config["snapshot"].get("review_dir", "events/review")
        self.notification_cooldown = float(config.get("dvrip", {}).get("notification_cooldown_seconds", 40))
        self.last_private_notification_at = float("-inf")
        self.notification_lock = threading.Lock()

    def analyze(self, frame: np.ndarray) -> tuple[str, np.ndarray, int]:
        result = self.model.predict(frame, classes=[0], conf=0.5, verbose=False)[0]
        probability = self.classifier.predict(frame)
        fixed, ptz = tuple(self.layout["fixed"]), tuple(self.layout["ptz"])
        decisions = []
        for x1, y1, x2, y2, confidence, _class_id in result.boxes.data.tolist():
            box = tuple(map(int, (x1, y1, x2, y2)))
            foot = ((box[0] + box[2]) / 2, box[3])
            if fixed[0] <= foot[0] <= fixed[2] and fixed[1] <= foot[1] <= fixed[3]:
                decision, color = "private", (0, 0, 255)
            elif ptz[0] <= foot[0] <= ptz[2] and ptz[1] <= foot[1] <= ptz[3]:
                value = self.classifier.probability_at(foot)
                if value is not None and value >= self.public_threshold:
                    decision, color = "public", (0, 165, 255)
                elif value is not None and value <= self.private_threshold:
                    decision, color = "private", (0, 0, 255)
                else:
                    decision, color = "uncertain", (0, 255, 255)
            else:
                decision, color = "uncertain", (0, 255, 255)
            decisions.append(decision)
            cv2.rectangle(frame, box[:2], box[2:], color, 3)
            cv2.putText(frame, f"person {decision} {confidence:.2f}", (box[0], max(25, box[1] - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
        if "private" in decisions:
            return "private", frame, len(decisions)
        if decisions and all(item == "public" for item in decisions):
            return "public", frame, len(decisions)
        return "uncertain", frame, len(decisions)

    @staticmethod
    def jpeg(frame: np.ndarray) -> bytes:
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            raise RuntimeError("Could not encode notification JPEG.")
        return encoded.tobytes()

    def save_result(self, frame: np.ndarray, source: str, decision: str, count: int, occurred_at: datetime) -> str:
        """Persist every reviewed result; folders make later false-positive audits easy."""
        folder = os.path.join(self.review_dir, decision)
        os.makedirs(folder, exist_ok=True)
        stamp = occurred_at.strftime("%Y-%m-%d_%H-%M-%S_%f")
        filename = f"{stamp}_{source}_{decision}_persons-{count}.jpg"
        path = os.path.join(folder, filename)
        if not cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, 90]):
            raise RuntimeError("Could not save review JPEG.")
        return path

    def notify_private(self, frame: np.ndarray, source: str, count: int) -> None:
        """Notify only confirmed property detections, while still saving every event."""
        with self.notification_lock:
            now = time.monotonic()
            remaining = self.notification_cooldown - (now - self.last_private_notification_at)
            if remaining > 0:
                print(f"Telegram private notification suppressed ({remaining:.1f}s cooldown remaining)")
                return
            self.last_private_notification_at = now
        self.telegram.send_photo(self.jpeg(frame), f"{self.name} camera · {source} · private · persons={count}")

    def fetch_snapshot(self) -> np.ndarray:
        uri = xmeye_snapshot_uri(self.snapshot_url, self.username, self.password) if self.snapshot_query_auth else self.snapshot_url
        data = download_snapshot(uri, self.username, self.password, 6)
        frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise RuntimeError("Snapshot endpoint returned an unreadable image.")
        if frame.shape[1] != self.width or frame.shape[0] != self.height:
            frame = cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_AREA)
        return frame

    def _video_frames(self):
        command = ["ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", self.stream,
                   "-an", "-sn", "-dn", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        size = self.width * self.height * 3
        deadline = time.monotonic() + self.video_seconds
        try:
            while time.monotonic() < deadline:
                raw = process.stdout.read(size)
                if len(raw) != size:
                    break
                yield np.frombuffer(raw, np.uint8).reshape((self.height, self.width, 3)).copy()
        finally:
            if process.poll() is None:
                process.kill()

    def fallback(self, occurred_at: datetime) -> None:
        last = None
        last_count = 0
        next_sample = 0.0
        for frame in self._video_frames():
            if time.monotonic() < next_sample:
                continue
            next_sample = time.monotonic() + self.interval
            decision, annotated, count = self.analyze(frame)
            last, last_count = annotated, count
            if decision != "uncertain":
                self.save_result(annotated, "video", decision, count, occurred_at)
                if decision == "private":
                    self.notify_private(annotated, "video fallback", count)
                print(f"DVRIP video decision: {decision} ({count} persons)")
                return
        if last is not None:
            self.save_result(last, "video", "uncertain", last_count, occurred_at)
            print(f"DVRIP video decision: uncertain ({last_count} persons)")

    def handle_event(self, event: dict) -> None:
        if event.get("type") != "person" or event.get("status") != "Start":
            return
        # Analyze every camera event. Only confirmed private notifications are
        # rate-limited, so the review folders preserve all audit evidence.
        threading.Thread(target=self._handle_event, args=(datetime.now().astimezone(),), daemon=True).start()

    def _handle_event(self, occurred_at: datetime) -> None:
        try:
            frame = self.fetch_snapshot()
            decision, annotated, count = self.analyze(frame)
            self.save_result(annotated, "image", decision, count, occurred_at)
            print(f"DVRIP image decision: {decision} ({count} persons)")
            if decision == "uncertain":
                self.fallback(occurred_at)
            elif decision == "private":
                self.notify_private(annotated, "image", count)
        except Exception as error:
            print(f"DVRIP snapshot analysis failed: {type(error).__name__}: {error}")
            try:
                self.fallback(occurred_at)
            except Exception as fallback_error:
                print(f"DVRIP video fallback failed: {type(fallback_error).__name__}: {fallback_error}")


def main():
    parser = ArgumentParser(description="Send classified Telegram images for DVRIP human alarms.")
    parser.add_argument("--config", default="smart-monitor.json")
    parser.add_argument("--stream", default=None)
    args = parser.parse_args()
    load_windows_user_environment()
    with open(args.config, encoding="utf-8") as source:
        config = json.load(source)
    # The value may have been restored from the registry after argparse read
    # its default, so use the refreshed environment when --stream was omitted.
    if args.stream is None:
        args.stream = rtsp_url(config)
    if not args.stream:
        parser.error("provide --stream or set this camera's CAMERA_<NAME>_RTSP_URL")
    worker = SmartAlarm(config, args.stream)
    host, port = dvrip_endpoint(config)
    user, password = credentials(config)
    listener = DVRIPAlarmListener(host, port, user, password, worker.handle_event, lambda text: print("DVRIP " + text))
    listener.start()
    print("Listening for DVRIP person events. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        listener.stop()


if __name__ == "__main__":
    main()
