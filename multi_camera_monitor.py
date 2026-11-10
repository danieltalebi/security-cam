"""Run DVRIP human-event monitoring for multiple independent cameras."""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from argparse import ArgumentParser
import atexit
import hashlib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np

from dvrip_events import DVRIPAlarmListener
from onvif_snapshot import download_snapshot, xmeye_snapshot_uri
from telegram_notify import CameraConnectivityAlerts, TelegramNotifier
from timestamp_logging import install_timestamped_logging
from single_instance import SingleInstanceLock
from camera_config import camera_name, credentials, dvrip_endpoint, expand_host, required_env, rtsp_url

if TYPE_CHECKING:
    from alarm_policy import AlarmPolicyController
    from home_assistant_mqtt import HomeAssistantMQTT


def camera_credentials(config: dict, name: str = "camera") -> tuple[str, int, str, str]:
    host, port = dvrip_endpoint(config, name)
    user, password = credentials(config, name)
    return host, port, user, password


class PresenceAlarm:
    """Verify armed backyard DVRIP triggers with a snapshot and short RTSP fallback."""

    def __init__(
        self, name: str, config: dict,
        notifier: TelegramNotifier | None = None,
        home_assistant: "HomeAssistantMQTT | None" = None,
        stream: str | None = None,
        policy: "AlarmPolicyController | None" = None,
        role: str = "exterior",
    ):
        self.name, self.config = name, config
        self.user, self.password = credentials(config, name)
        snapshot = config["snapshot"]
        self.snapshot_url = expand_host(snapshot["url"], config, name)
        self.query_auth = bool(snapshot.get("xmeye_query_auth", False))
        self.review_dir = Path(snapshot.get("review_dir", f"events/review/{name}"))
        self.notifier = notifier
        self.home_assistant = home_assistant
        self.stream = stream
        self.policy = policy
        self.role = role
        confirmation = config.get("confirmation", {})
        self.confidence = float(confirmation.get("confidence", 0.5))
        self.video_seconds = float(confirmation.get("video_fallback_seconds", 8))
        self.video_interval = float(confirmation.get("video_sample_seconds", 0.8))
        from ultralytics import YOLO

        self.model = YOLO(str(confirmation.get("model", "yolov8n.pt")))
        self.cooldown = float(config["dvrip"].get("notification_cooldown_seconds", 40))
        self.last_notification = float("-inf")
        self.lock = threading.Lock()
        self.event_debounce = float(config["dvrip"].get("event_debounce_seconds", 3))
        self.last_accepted_event_at = float("-inf")
        self.event_lock = threading.Lock()
        self.verification_lock = threading.Lock()

    def handle_event(self, event: dict) -> None:
        if event.get("status") != "Start":
            return
        with self.event_lock:
            now = time.monotonic()
            if now - self.last_accepted_event_at < self.event_debounce:
                return
            self.last_accepted_event_at = now
        threading.Thread(target=self._handle, args=(event,), daemon=True).start()

    @staticmethod
    def jpeg(frame: np.ndarray) -> bytes:
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            raise RuntimeError("Could not encode verification JPEG.")
        return encoded.tobytes()

    def analyze(self, frame: np.ndarray) -> tuple[np.ndarray, int]:
        result = self.model.predict(
            frame, classes=[0], conf=self.confidence, verbose=False,
        )[0]
        return result.plot(), len(result.boxes)

    def save_result(
        self, frame: np.ndarray, outcome: str, source: str,
        count: int, occurred_at: datetime,
    ) -> Path:
        folder = self.review_dir / outcome
        folder.mkdir(parents=True, exist_ok=True)
        stamp = occurred_at.strftime("%Y-%m-%d_%H-%M-%S_%f")
        path = folder / f"{stamp}_{source}_persons-{count}.jpg"
        if not cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 90]):
            raise RuntimeError("Could not save verification JPEG.")
        return path

    def fetch_snapshot(self) -> np.ndarray:
        uri = xmeye_snapshot_uri(self.snapshot_url, self.user, self.password) if self.query_auth else self.snapshot_url
        data = download_snapshot(uri, self.user, self.password, 8)
        frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise RuntimeError("Snapshot endpoint returned an unreadable image.")
        return frame

    def _video_frames(self):
        if not self.stream:
            return
        capture = cv2.VideoCapture(self.stream)
        deadline = time.monotonic() + self.video_seconds
        next_sample = 0.0
        try:
            while time.monotonic() < deadline:
                available, frame = capture.read()
                if not available:
                    break
                now = time.monotonic()
                if now < next_sample:
                    continue
                next_sample = now + self.video_interval
                yield frame
        finally:
            capture.release()

    def publish_confirmed(
        self, frame: np.ndarray, source: str, count: int, occurred_at: datetime,
        evidence_path: str | Path | None = None,
    ) -> None:
        data = self.jpeg(frame)
        mqtt_published = False
        if self.home_assistant:
            try:
                self.home_assistant.publish_detection(
                    self.name, data, "presence", count, source, occurred_at,
                )
                mqtt_published = True
            except Exception as error:
                print(f"{self.name}: Home Assistant MQTT publish failed: {type(error).__name__}: {error}")
        policy = getattr(self, "policy", None)
        if policy is not None:
            policy.record_detection(
                self.name, getattr(self, "role", "exterior"), "presence",
                evidence_path, occurred_at,
            )
            print(f"{self.name}: person confirmed via {source}" + (" and published" if mqtt_published else ""))
            return
        if self.notifier is None:
            print(f"{self.name}: person confirmed via {source}" + (" and published" if mqtt_published else ""))
            return
        with self.lock:
            remaining = self.cooldown - (time.monotonic() - self.last_notification)
            if remaining > 0:
                print(f"{self.name}: Telegram notification suppressed ({remaining:.1f}s cooldown remaining)")
                return
            self.last_notification = time.monotonic()
        sent = self.notifier.send_photo(data, f"{self.name} camera · person confirmed via {source}")
        suffix = " and sent" if sent else " (Telegram muted)"
        print(f"{self.name}: person confirmed via {source}{suffix}")

    def publish_verification_failure(self, reason: str, occurred_at: datetime) -> None:
        for secret in (self.user, self.password):
            if secret:
                reason = reason.replace(secret, "<redacted>")
        print(f"{self.name}: visual verification failed: {reason}")
        if self.home_assistant:
            try:
                self.home_assistant.publish_verification_failure(self.name, reason, occurred_at)
            except Exception as error:
                print(f"{self.name}: failure event publish failed: {type(error).__name__}: {error}")
        if self.notifier:
            # Verification failures are technical alerts and bypass detection
            # pauses and disarmed modes.
            self.notifier.send_message(
                f"⚠️ {self.name}: DVRIP event received but visual verification failed. {reason}"
            )

    def _handle(self, event: dict) -> None:
        if not self.verification_lock.acquire(blocking=False):
            print(f"{self.name}: DVRIP trigger merged into active visual verification")
            return
        try:
            occurred_at = datetime.now().astimezone()
            errors = []
            evidence_seen = False
            try:
                frame = self.fetch_snapshot()
                evidence_seen = True
                annotated, count = self.analyze(frame)
                if count:
                    evidence_path = self.save_result(annotated, "confirmed", "image", count, occurred_at)
                    self.publish_confirmed(annotated, "image", count, occurred_at, evidence_path)
                    return
                self.save_result(annotated, "rejected", "image", 0, occurred_at)
            except Exception as error:
                errors.append(f"snapshot: {type(error).__name__}: {error}")

            try:
                for frame in self._video_frames() or ():
                    evidence_seen = True
                    annotated, count = self.analyze(frame)
                    if count:
                        evidence_path = self.save_result(annotated, "confirmed", "video", count, occurred_at)
                        self.publish_confirmed(annotated, "video", count, occurred_at, evidence_path)
                        return
                if not self.stream:
                    errors.append("RTSP fallback is not configured")
                elif not evidence_seen:
                    errors.append("RTSP fallback returned no frames")
            except Exception as error:
                errors.append(f"RTSP: {type(error).__name__}: {error}")

            if evidence_seen:
                print(
                    f"{self.name}: DVRIP {event.get('type', 'unknown')} rejected; "
                    "no person was visually confirmed"
                )
            else:
                self.publish_verification_failure("; ".join(errors), occurred_at)
        finally:
            self.verification_lock.release()


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


def technical_notifier(multi: dict, multi_path: Path) -> TelegramNotifier | None:
    """Create an unmuted channel for health alerts even in MQTT-only mode."""
    telegram_configs = []
    for entry in multi.get("cameras", []):
        try:
            config = load_config((multi_path.parent / entry["config"]).resolve())
            telegram = config["telegram"]
            identity = (telegram.get("token_env"), telegram.get("chat_id_env"))
            if identity not in {
                (item.get("token_env"), item.get("chat_id_env"))
                for item in telegram_configs
            }:
                telegram_configs.append(telegram)
        except (KeyError, OSError):
            continue
    last_error: Exception | None = None
    # A login LaunchAgent can start a moment before the user's Keychain is
    # ready. Brief retries avoid silently losing all health notifications.
    for attempt in range(3):
        for telegram in telegram_configs:
            try:
                return TelegramNotifier(
                    required_env(telegram["token_env"]), required_env(telegram["chat_id_env"]),
                )
            except RuntimeError as error:
                last_error = error
        if attempt < 2:
            time.sleep(1)
    if last_error:
        print(f"Technical Telegram notifier unavailable: {type(last_error).__name__}: {last_error}")
    return None


def handle_interior_presence(policy, event: dict) -> str:
    """Apply a raw Home Assistant interior sensor event to local policy."""
    occurred_at = datetime.now().astimezone()
    raw_time = event.get("occurred_at")
    if raw_time:
        try:
            occurred_at = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            if occurred_at.tzinfo is None:
                occurred_at = occurred_at.astimezone()
        except ValueError:
            pass
    incident = policy.record_detection(
        "interior", "interior", "presence", None, occurred_at,
    )
    if incident:
        return "interior presence accepted in armed-away mode"
    return "interior presence ignored by the current local alarm mode"


def send_incident_notification(channel: TelegramNotifier, incident: dict) -> bool:
    """Send image incidents and text-only interior sensor incidents."""
    evidence_path = incident.get("evidence_path")
    if evidence_path:
        camera = str(incident.get("camera", "cámara"))
        classification = str(incident.get("classification", "detection"))
        if camera.lower() == "garage" and classification == "visitor":
            description = "🔔 Garage · Persona esperando en el acceso (lente móvil PTZ)"
        elif camera.lower() == "garage" and classification == "private":
            description = "🚨 Garage · Persona detectada dentro de la propiedad (lente fijo)"
        elif camera.lower() == "backyard":
            description = "🚨 Backyard · Persona detectada"
        else:
            description = f"🚨 {camera} · {classification}"
        caption = (
            f"{description} · incidente {incident['id'][:8]}. "
            "Responda OK para reconocer."
        )
        return channel.send_photo(Path(str(evidence_path)).read_bytes(), caption)
    return channel.send_notification_message(
        "🚨 ALARMA FUERA · Presencia detectada por el sensor interior. "
        f"Incidente {incident['id'][:8]}. Responda OK para reconocer."
    )


def daily_alarm_reminder(policy) -> str | None:
    """Return the reminder appended to the daily summary when action is needed."""
    state = policy.snapshot()
    if state.get("control") == "manual" and state.get("mode") == "disarmed":
        return "⚠️ Recordatorio: la alarma sigue en control manual y desarmada."
    return None


def main() -> None:
    install_timestamped_logging()

    parser = ArgumentParser(description="Run smart DVRIP monitoring for multiple cameras.")
    parser.add_argument("--config", default="multi-camera.json")
    args = parser.parse_args()
    multi_path = Path(args.config).resolve()
    lock_digest = hashlib.sha256(str(multi_path).encode("utf-8")).hexdigest()[:16]
    instance_lock = SingleInstanceLock(f"security-cam-monitor-{lock_digest}")
    if not instance_lock.acquire():
        raise SystemExit(f"Another monitor is already running for {multi_path}")
    atexit.register(instance_lock.release)
    from dvrip_smart_telegram import SmartAlarm, load_windows_user_environment

    load_windows_user_environment()
    multi = load_config(multi_path)
    from alarm_policy import AlarmPolicyController

    policy_config = dict(multi.get("alarm_policy", {}))
    state_path = Path(policy_config.get("state_file", ".security-cam-state.json"))
    if not state_path.is_absolute():
        state_path = multi_path.parent / state_path
    policy = AlarmPolicyController(policy_config, state_path)
    listeners: list[DVRIPAlarmListener] = []
    connectivity_alerts: list[CameraConnectivityAlerts] = []
    output_mode = str(multi.get("outputs", {}).get("mode", "telegram")).lower()
    if output_mode not in {"telegram", "mqtt", "both"}:
        raise RuntimeError("outputs.mode must be telegram, mqtt, or both.")
    home_assistant = None
    presence_monitor = None
    internet_monitor = None
    connectivity_history = None
    system_notifier = technical_notifier(multi, multi_path)
    if system_notifier:
        system_notifier = system_notifier.start_command_listener().set_command_handler(policy.handle_command)
    health_config = multi.get("health", {})
    camera_thresholds = tuple(
        float(value)
        for value in health_config.get("camera_offline_alert_seconds", [300])
    )
    camera_repeat_seconds = health_config.get("camera_offline_repeat_seconds", 21600)
    camera_repeat_seconds = (
        float(camera_repeat_seconds) if camera_repeat_seconds is not None else None
    )
    if not camera_thresholds or any(value <= 0 for value in camera_thresholds):
        raise RuntimeError("health.camera_offline_alert_seconds must contain positive seconds.")
    if camera_repeat_seconds is not None and camera_repeat_seconds <= 0:
        raise RuntimeError("health.camera_offline_repeat_seconds must be positive or null.")
    if system_notifier:
        from connectivity_history import ConnectivityHistory

        connectivity_state = Path(
            health_config.get("connectivity_state_file", ".connectivity-history.json")
        )
        if not connectivity_state.is_absolute():
            connectivity_state = multi_path.parent / connectivity_state
        router_names = [
            str(source.get("name") or source.get("type") or "router")
            for source in multi.get("presence", {}).get("sources", [])
        ]
        connectivity_history = ConnectivityHistory(
            connectivity_state,
            system_notifier.send_message,
            router_names,
            internet_threshold_seconds=float(
                health_config.get("internet_outage_alert_seconds", 600)
            ),
            router_alert_seconds=float(
                health_config.get("router_offline_alert_seconds", 3600)
            ),
            summary_time=str(health_config.get("daily_summary_time", "18:00")),
            timezone=health_config.get("timezone"),
            summary_context=lambda: daily_alarm_reminder(policy),
        ).start()
    if system_notifier and health_config.get("internet_enabled", True):
        from internet_connectivity import InternetConnectivityMonitor

        internet_monitor = InternetConnectivityMonitor(
            system_notifier.send_message,
            interval_seconds=float(health_config.get("internet_poll_seconds", 30)),
            timeout_seconds=float(health_config.get("internet_timeout_seconds", 5)),
            failure_threshold=int(health_config.get("internet_failure_threshold", 2)),
            history=connectivity_history,
        ).start()
    if output_mode in {"mqtt", "both"}:
        try:
            from home_assistant_mqtt import HomeAssistantMQTT
            home_assistant = (
                HomeAssistantMQTT(multi.get("home_assistant", {}).get("mqtt", {}))
                .set_command_handler(policy.handle_command)
                .set_detection_handler(lambda event: handle_interior_presence(policy, event))
                .start()
            )
        except Exception as error:
            print(f"Home Assistant MQTT unavailable; local alarm remains active: {type(error).__name__}: {error}")

    presence_config = multi.get("presence", {})
    if presence_config.get("enabled", False):
        from router_presence import RouterPresenceMonitor
        last_policy_summary = None

        def publish_presence(state: str, attributes: dict) -> None:
            nonlocal last_policy_summary
            mode = policy.observe_presence(state, attributes)
            summary = (mode, policy.snapshot()["control"])
            if summary != last_policy_summary:
                print(f"Local alarm mode: {summary[0]} ({summary[1]})")
                last_policy_summary = summary
            if home_assistant:
                try:
                    home_assistant.publish_presence(state, attributes)
                except Exception as error:
                    print(f"Home Assistant presence publish failed: {type(error).__name__}: {error}")

        presence_monitor = RouterPresenceMonitor(
            presence_config, publish_presence,
            notify=system_notifier.send_message if system_notifier else None,
            history=connectivity_history,
        ).start()

    notification_channels: dict[str, TelegramNotifier] = {}
    media_sources = []
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
            ).start_command_listener().set_command_handler(policy.handle_command)
        notification_channel = notifier or system_notifier
        if notification_channel:
            notification_channels[name] = notification_channel
        if home_assistant:
            home_assistant.register_camera(name)
        role = str(entry.get("role") or ("interior" if "interior" in name.lower() else "exterior")).lower()
        if role not in {"interior", "exterior"}:
            raise RuntimeError(f"{name}: role must be interior or exterior.")
        if mode == "property":
            stream = os.environ.get(entry["stream_env"]) if entry.get("stream_env") else rtsp_url(config, name)
            if not stream:
                raise RuntimeError(f"{name}: its CAMERA_<NAME>_RTSP_URL environment variable is not set.")
            alarm = SmartAlarm(
                config, stream, notifier=notification_channel,
                home_assistant=home_assistant, name=name, policy=policy, role=role,
            )
            handler = alarm.handle_event
        elif mode == "presence":
            stream = os.environ.get(entry["stream_env"]) if entry.get("stream_env") else rtsp_url(config, name)
            alarm = PresenceAlarm(
                name, config, notifier=notification_channel,
                home_assistant=home_assistant, stream=stream, policy=policy, role=role,
            )
            handler = alarm.handle_event
        else:
            raise RuntimeError(f"{name}: unsupported mode {mode!r}.")
        from camera_media import CameraMediaSource

        snapshot = config["snapshot"]
        lens = None
        layout_size = None
        if config.get("layout"):
            media_region = str(config.get("telegram_media", {}).get("lens", "ptz"))
            if media_region not in config["layout"]:
                raise RuntimeError(f"{name}: telegram_media.lens {media_region!r} is not in layout.")
            lens = tuple(map(int, config["layout"][media_region]))
            layout_size = (
                max(int(bounds[2]) for bounds in config["layout"].values()),
                max(int(bounds[3]) for bounds in config["layout"].values()),
            )
        media_sources.append(CameraMediaSource(
            name=name,
            snapshot_url=expand_host(snapshot["url"], config, name),
            username=user,
            password=password,
            query_auth=bool(snapshot.get("xmeye_query_auth", False)),
            stream=stream,
            lens=lens,
            layout_size=layout_size,
        ))
        connectivity = CameraConnectivityAlerts(
            name, system_notifier, thresholds=camera_thresholds,
            repeat_seconds=camera_repeat_seconds, bypass_mute=True,
        ) if system_notifier else None

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
    if system_notifier and media_sources:
        from camera_media import CameraMediaCommands, chain_command_handlers

        media_commands = CameraMediaCommands(media_sources, system_notifier)
        shared_handler = chain_command_handlers(media_commands, policy.handle_command)
        controllers = {id(system_notifier): system_notifier}
        controllers.update({id(channel): channel for channel in notification_channels.values()})
        for controller in controllers.values():
            controller.set_command_handler(shared_handler)
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame) -> None:
        stop_event.set()

    # launchd stops a LaunchAgent with SIGTERM. Handle it as a clean shutdown
    # so MQTT publishes its offline state and camera listener threads close.
    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    last_mqtt_state = None
    print(f"Local alarm initialized: {policy.snapshot()['mode']} ({policy.snapshot()['control']})")
    print("Monitoring all cameras. Press Ctrl+C to stop.")
    try:
        while not stop_event.wait(1):
            policy.tick()
            state = policy.snapshot()
            mqtt_state = json.dumps({
                "mode": state["mode"],
                "control": state["control"],
                "presence": state["presence"],
                "notifications_paused": state["notifications_paused"],
                "active_incidents": [
                    (item.get("id"), item.get("acknowledged"), item.get("notification_count"))
                    for item in state["active_incidents"]
                ],
            }, sort_keys=True)
            if home_assistant and mqtt_state != last_mqtt_state:
                try:
                    home_assistant.publish_alarm_state(state)
                    last_mqtt_state = mqtt_state
                except Exception as error:
                    print(f"Home Assistant alarm state publish failed: {type(error).__name__}: {error}")
            for incident in policy.due_notifications():
                channel = notification_channels.get(str(incident.get("camera"))) or system_notifier
                if not channel:
                    continue
                try:
                    sent = send_incident_notification(channel, incident)
                    if sent:
                        policy.mark_notified(str(incident["camera"]))
                        if home_assistant:
                            home_assistant.publish_incident({
                                **incident,
                                "alarm_mode": state["mode"],
                                "alarm_control": state["control"],
                                "presence": state["presence"],
                            }, "incident_notified")
                except Exception as error:
                    print(f"{incident.get('camera')}: incident notification failed: {type(error).__name__}: {error}")
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        for listener in listeners:
            listener.stop()
        for connectivity in connectivity_alerts:
            connectivity.stop()
        if presence_monitor:
            presence_monitor.stop()
        if internet_monitor:
            internet_monitor.stop()
        if connectivity_history:
            connectivity_history.stop()
        if home_assistant:
            home_assistant.stop()
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    main()
