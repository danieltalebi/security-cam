"""DVRIP alarm -> JPEG classification -> Telegram, with RTSP only as fallback."""

from __future__ import annotations

import json
import os
import hashlib
import threading
import time
from argparse import ArgumentParser
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import cv2
import numpy as np
from ultralytics import YOLO

from dvrip_events import DVRIPAlarmListener
from onvif_snapshot import download_snapshot, xmeye_snapshot_uri
from snapshot_loitering import PersonObservation, SnapshotDwellTracker
from telegram_notify import CameraConnectivityAlerts, TelegramNotifier
from timestamp_logging import install_timestamped_logging
from camera_config import credentials, dvrip_endpoint, required_env, rtsp_url
from camera_config import camera_name, expand_host

if TYPE_CHECKING:
    from home_assistant_mqtt import HomeAssistantMQTT


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
                if value and (
                    name.startswith("CAMERA_") or name.startswith("HA_MQTT_")
                    or name in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
                ):
                    os.environ[name] = str(value)
    except OSError:
        # Environment variables supplied by the launcher still work when the
        # registry cannot be read (for example, a restricted service account).
        pass


class SmartAlarm:
    def __init__(
        self, config: dict, stream: str,
        notifier: TelegramNotifier | None = None,
        home_assistant: "HomeAssistantMQTT | None" = None,
        name: str | None = None,
    ):
        self.config, self.stream = config, stream
        self.name = name or camera_name(config)
        self.layout = config["layout"]
        self.width = max(bounds[2] for bounds in self.layout.values())
        self.height = max(bounds[3] for bounds in self.layout.values())
        self.username, self.password = credentials(config, self.name)
        self.telegram = notifier
        self.home_assistant = home_assistant
        self.snapshot_url = expand_host(config["snapshot"]["url"], config, self.name)
        self.snapshot_query_auth = bool(config["snapshot"].get("xmeye_query_auth", False))
        self.model = YOLO("yolov8n.pt")
        self.loitering = config.get("snapshot_loitering", {})
        self.loitering_enabled = bool(self.loitering.get("enabled", True))
        self.interval = float(self.loitering.get("snapshot_interval_seconds", 0.75))
        self.observation_seconds = float(self.loitering.get("observation_seconds", 12))
        self.max_missing_snapshots = int(self.loitering.get("max_missing_snapshots", 3))
        self.review_dir = config["snapshot"].get("review_dir", "events/review")
        self.notification_cooldown = float(config.get("dvrip", {}).get("notification_cooldown_seconds", 40))
        self.last_private_notification_at = float("-inf")
        self.notification_lock = threading.Lock()
        self.event_debounce = float(config.get("dvrip", {}).get("event_debounce_seconds", 3))
        self.last_accepted_event_at = float("-inf")
        self.event_lock = threading.Lock()
        self.session_lock = threading.Lock()

    def detect_people(self, frame: np.ndarray) -> tuple[np.ndarray, list[PersonObservation], list[PersonObservation]]:
        result = self.model.predict(frame, classes=[0], conf=0.5, verbose=False)[0]
        fixed, ptz = tuple(self.layout["fixed"]), tuple(self.layout["ptz"])
        fixed_people: list[PersonObservation] = []
        ptz_people: list[PersonObservation] = []
        for x1, y1, x2, y2, confidence, _class_id in result.boxes.data.tolist():
            box = tuple(map(int, (x1, y1, x2, y2)))
            foot = ((box[0] + box[2]) / 2, box[3])
            observation = PersonObservation(box, foot, max(1, box[3] - box[1]), float(confidence))
            if fixed[0] <= foot[0] <= fixed[2] and fixed[1] <= foot[1] <= fixed[3]:
                fixed_people.append(observation)
                label, color = "fixed immediate", (0, 0, 255)
            elif ptz[0] <= foot[0] <= ptz[2] and ptz[1] <= foot[1] <= ptz[3]:
                local_box = (box[0] - ptz[0], box[1] - ptz[1], box[2] - ptz[0], box[3] - ptz[1])
                local_foot = (foot[0] - ptz[0], foot[1] - ptz[1])
                ptz_people.append(PersonObservation(local_box, local_foot, observation.height, observation.confidence))
                label, color = "ptz tracking", (0, 255, 255)
            else:
                label, color = "outside layout", (128, 128, 128)
            cv2.rectangle(frame, box[:2], box[2:], color, 3)
            cv2.putText(frame, f"person {label} {confidence:.2f}", (box[0], max(25, box[1] - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
        return frame, fixed_people, ptz_people

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

    def notify_alert(self, frame: np.ndarray, source: str, decision: str, count: int) -> None:
        """Send a classified alert with its evidence source."""
        if self.telegram is None:
            return
        with self.notification_lock:
            now = time.monotonic()
            remaining = self.notification_cooldown - (now - self.last_private_notification_at)
            if remaining > 0:
                print(f"Telegram private notification suppressed ({remaining:.1f}s cooldown remaining)")
                return
            self.last_private_notification_at = now
        self.telegram.send_photo(
            self.jpeg(frame),
            f"{self.name} camera · source={source} · decision={decision} · persons={count}",
        )

    def publish_detection(
        self, frame: np.ndarray, source: str, decision: str, count: int,
        occurred_at: datetime,
    ) -> None:
        if self.home_assistant is None:
            return
        try:
            self.home_assistant.publish_detection(
                self.name, self.jpeg(frame), decision, count, source, occurred_at,
            )
        except Exception as error:
            print(f"Home Assistant MQTT publish failed: {type(error).__name__}: {error}")

    def fetch_snapshot(self) -> np.ndarray:
        uri = xmeye_snapshot_uri(self.snapshot_url, self.username, self.password) if self.snapshot_query_auth else self.snapshot_url
        if self.loitering.get("cache_bust_query", True):
            parsed = urlsplit(uri)
            query = parse_qsl(parsed.query, keep_blank_values=True)
            query.append(("security_cam_fresh", str(time.time_ns())))
            uri = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
        data = download_snapshot(uri, self.username, self.password, 6)
        frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise RuntimeError("Snapshot endpoint returned an unreadable image.")
        if frame.shape[1] != self.width or frame.shape[0] != self.height:
            frame = cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_AREA)
        return frame

    def _ptz_crop(self, frame: np.ndarray) -> np.ndarray:
        x1, y1, x2, y2 = map(int, self.layout["ptz"])
        return frame[y1:y2, x1:x2].copy()

    @staticmethod
    def _snapshot_signature(frame: np.ndarray) -> bytes:
        small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
        return hashlib.blake2b(small.tobytes(), digest_size=12).digest()

    def handle_event(self, event: dict) -> None:
        if event.get("type") != "person" or event.get("status") != "Start":
            return
        # DVRIP firmware can emit a burst of identical Start packets for one
        # person. Collapse those packets before starting expensive snapshot and
        # YOLO work; Home Assistant performs the longer incident correlation.
        with self.event_lock:
            now = time.monotonic()
            if now - self.last_accepted_event_at < self.event_debounce:
                return
            self.last_accepted_event_at = now
        if self.session_lock.locked():
            print("DVRIP person event merged into the active snapshot tracking session")
            return
        threading.Thread(target=self._handle_event, args=(datetime.now().astimezone(),), daemon=True).start()

    def _handle_event(self, occurred_at: datetime) -> None:
        if not self.session_lock.acquire(blocking=False):
            return
        try:
            tracker = SnapshotDwellTracker(self.loitering)
            deadline = time.monotonic() + self.observation_seconds
            next_snapshot = time.monotonic()
            missing = 0
            duplicate_signatures = 0
            previous_signature = None
            last_frame = None
            last_count = 0
            samples = 0
            print(
                f"Garage snapshot session started: source=DVRIP person, "
                f"interval={self.interval:.2f}s, window={self.observation_seconds:.1f}s"
            )
            while time.monotonic() < deadline:
                wait = next_snapshot - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                next_snapshot = time.monotonic() + self.interval
                try:
                    frame = self.fetch_snapshot()
                    signature = self._snapshot_signature(frame)
                    if signature == previous_signature:
                        duplicate_signatures += 1
                        print("Garage snapshot ignored: identical image returned by camera")
                        continue
                    previous_signature = signature
                    samples += 1
                    annotated, fixed_people, ptz_people = self.detect_people(frame)
                    last_frame, last_count = annotated, len(fixed_people) + len(ptz_people)
                    print(
                        f"Garage snapshot sample={samples}: fixed={len(fixed_people)}, "
                        f"ptz={len(ptz_people)}"
                    )
                    if fixed_people:
                        source = "DVRIP snapshot · fixed garage lens"
                        self.save_result(annotated, "fixed_snapshot", "private", len(fixed_people), occurred_at)
                        self.publish_detection(annotated, source, "private", len(fixed_people), occurred_at)
                        self.notify_alert(annotated, source, "private", len(fixed_people))
                        print(f"Garage alert: source={source}, persons={len(fixed_people)}")
                        return

                    if not bool(self.loitering.get("enabled", True)):
                        self.save_result(annotated, "ptz_snapshots", "tracking_disabled", len(ptz_people), occurred_at)
                        print("Garage PTZ snapshot tracking is disabled; no alert generated")
                        return

                    if ptz_people:
                        missing = 0
                    else:
                        missing += 1
                    ready, quality = tracker.update(self._ptz_crop(frame), ptz_people, time.monotonic())
                    if quality is not None:
                        print(f"Garage PTZ background stabilization: inlier_ratio={quality:.2f}")
                    elif samples > 1:
                        print("Garage PTZ background stabilization unavailable for this sample")
                    if ready:
                        track = max(ready, key=lambda item: item.duration)
                        source = f"DVRIP snapshots · PTZ stationary {track.duration:.1f}s"
                        cv2.putText(
                            annotated,
                            f"loitering {track.duration:.1f}s radius={track.radius_body_heights:.2f}h",
                            (24, int(self.layout["ptz"][1]) + 48),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 3,
                        )
                        self.save_result(annotated, "ptz_snapshots", "loitering", len(ptz_people), occurred_at)
                        # "presence" remains compatible with the currently deployed HA package;
                        # the source carries the more specific loitering decision.
                        self.publish_detection(annotated, source, "presence", len(ptz_people), occurred_at)
                        self.notify_alert(annotated, source, "loitering", len(ptz_people))
                        print(
                            f"Garage alert: source={source}, detections={track.detections}, "
                            f"radius={track.radius_body_heights:.2f}h, path={track.path_body_heights:.2f}h"
                        )
                        return
                    if missing > self.max_missing_snapshots and tracker.tracks:
                        print(f"Garage PTZ session ended: person disappeared for {missing} snapshots")
                        break
                except Exception as error:
                    print(f"Garage snapshot sample failed: {type(error).__name__}: {error}")

            if last_frame is not None:
                self.save_result(last_frame, "ptz_snapshots", "walking_or_gone", last_count, occurred_at)
            summaries = "; ".join(
                f"track={item.track_id} duration={item.duration:.1f}s detections={item.detections} "
                f"radius={item.radius_body_heights:.2f}h path={item.path_body_heights:.2f}h"
                for item in tracker.tracks
            ) or "no stable tracks"
            print(
                f"Garage PTZ session completed without alert: samples={samples}, "
                f"duplicates={duplicate_signatures}, {summaries}"
            )
        finally:
            self.session_lock.release()


def main():
    install_timestamped_logging()
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
    telegram = config["telegram"]
    notifier = TelegramNotifier(
        required_env(telegram["token_env"]), required_env(telegram["chat_id_env"]),
    ).start_command_listener()
    worker = SmartAlarm(config, args.stream or "", notifier=notifier)
    connectivity = CameraConnectivityAlerts(worker.name, notifier)
    host, port = dvrip_endpoint(config)
    user, password = credentials(config)
    listener = DVRIPAlarmListener(
        host, port, user, password, worker.handle_event,
        lambda text: print("DVRIP " + text), on_connectivity=connectivity.set_online,
    )
    listener.start()
    print("Listening for DVRIP person events. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        listener.stop()
        connectivity.stop()


if __name__ == "__main__":
    main()
