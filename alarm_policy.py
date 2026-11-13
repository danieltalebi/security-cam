"""Local alarm policy, incident correlation, and persistent operator controls.

This module deliberately has no Home Assistant, Telegram, OpenCV, or platform
dependencies.  Windows, macOS, Linux, chat commands, and desktop UIs can all
use the same controller and the same atomic state file.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from false_positive_memory import all_detections_match


ALARM_MODES = {"disarmed", "armed_home", "armed_away"}
CONTROL_MODES = {"automatic", "manual"}
MODE_LABELS = {
    "disarmed": "Desarmada",
    "armed_home": "Armada en casa",
    "armed_away": "Armada fuera de casa",
}
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([smhd])", re.IGNORECASE)


def _parse_clock(value: str, fallback: str) -> clock_time:
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except (TypeError, ValueError):
        return datetime.strptime(fallback, "%H:%M").time()


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch).astimezone().isoformat()


def _duration(text: str) -> float | None:
    value = text.strip().lower()
    if value.startswith("/pause"):
        value = value[6:].strip()
    elif value.startswith("pause"):
        value = value[5:].strip()
    if not value:
        return None
    position, seconds = 0, 0.0
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    for match in _DURATION_PART.finditer(value):
        if value[position:match.start()].strip():
            return None
        seconds += float(match.group(1)) * multipliers[match.group(2).lower()]
        position = match.end()
    return seconds if seconds > 0 and not value[position:].strip() else None


def _format_duration(seconds: float) -> str:
    remaining = max(0, int(seconds + 0.999))
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, seconds = divmod(remaining, 60)
    return " ".join(
        f"{amount}{unit}" for amount, unit in (
            (days, "d"), (hours, "h"), (minutes, "m"), (seconds, "s"),
        ) if amount
    ) or "0s"


class AlarmPolicyController:
    """Own the alarm's local source of truth and persist it atomically."""

    def __init__(
        self,
        config: dict | None = None,
        state_file: str | Path | None = None,
        now: Callable[[], float] = time.time,
    ):
        config = config or {}
        automatic = config.get("automatic", config)
        incidents = config.get("incidents", config)
        self.day_start = _parse_clock(automatic.get("day_start", "08:30"), "08:30")
        self.night_start = _parse_clock(automatic.get("night_start", "23:00"), "23:00")
        timezone_name = automatic.get("timezone")
        if timezone_name:
            try:
                self.timezone = ZoneInfo(str(timezone_name))
            except ZoneInfoNotFoundError as error:
                raise ValueError(f"Unknown alarm policy timezone: {timezone_name!r}") from error
        else:
            self.timezone = datetime.now().astimezone().tzinfo
        self.away_debounce = max(0.0, float(automatic.get("away_debounce_seconds", 300)))
        self.incident_close = max(1.0, float(incidents.get("close_after_seconds", 60)))
        self.false_positive_ttl = max(
            60.0, float(incidents.get("false_positive_suppression_seconds", 4 * 3600)),
        )
        self.false_positive_hash_distance = max(
            0, int(incidents.get("false_positive_hash_distance", 10)),
        )
        backoff = incidents.get("notification_backoff_seconds", [0, 60, 180, 300, 600])
        self.notification_backoff = tuple(max(0.0, float(value)) for value in backoff) or (0.0,)
        configured_path = state_file or config.get("state_file", ".security-cam-state.json")
        self.state_file = Path(configured_path).expanduser().resolve()
        self._now = now
        self._lock = threading.RLock()
        self._state = self._default_state()
        self._state_mtime_ns: int | None = None
        self._load()

    @staticmethod
    def _default_state() -> dict:
        return {
            "version": 1,
            "control": "automatic",
            "mode": "disarmed",
            "presence": "unavailable",
            "presence_devices": {},
            "router_sources": {},
            "away_since": None,
            "notifications_paused_until": 0.0,
            "incidents": {},
            "false_positives": [],
            "updated_at": None,
        }

    def _normalize(self, raw: dict) -> dict:
        state = self._default_state()
        if isinstance(raw, dict):
            for key in state:
                if key in raw:
                    state[key] = raw[key]
        if state["mode"] not in ALARM_MODES:
            state["mode"] = "disarmed"
        if state["control"] not in CONTROL_MODES:
            state["control"] = "automatic"
        if state["presence"] not in {"home", "away", "unavailable"}:
            state["presence"] = "unavailable"
        if not isinstance(state["presence_devices"], dict):
            state["presence_devices"] = {}
        if not isinstance(state["router_sources"], dict):
            state["router_sources"] = {}
        if not isinstance(state["incidents"], dict):
            state["incidents"] = {}
        if not isinstance(state["false_positives"], list):
            state["false_positives"] = []
        try:
            state["notifications_paused_until"] = float(state["notifications_paused_until"] or 0)
        except (TypeError, ValueError):
            state["notifications_paused_until"] = 0.0
        try:
            state["away_since"] = float(state["away_since"]) if state["away_since"] is not None else None
        except (TypeError, ValueError):
            state["away_since"] = None
        return state

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            self._state = self._normalize(raw)
            self._state_mtime_ns = self.state_file.stat().st_mtime_ns
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            self._state = self._default_state()

    def _reload_if_changed(self) -> None:
        try:
            mtime = self.state_file.stat().st_mtime_ns
        except OSError:
            return
        if self._state_mtime_ns is None or mtime != self._state_mtime_ns:
            self._load()

    def _save(self, now: float | None = None) -> None:
        now = self._now() if now is None else now
        self._state["updated_at"] = _iso(now)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_name(
            f"{self.state_file.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(json.dumps(self._state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temporary, self.state_file)
            self._state_mtime_ns = self.state_file.stat().st_mtime_ns
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _scheduled_home_mode(self, moment: datetime) -> str:
        current = moment.timetz().replace(tzinfo=None)
        if self.day_start <= current < self.night_start:
            return "disarmed"
        return "armed_home"

    def _apply_automatic(self, now: float) -> bool:
        if self._state["control"] != "automatic":
            return False
        previous = self._state["mode"]
        if self._state["presence"] == "home":
            self._state["mode"] = self._scheduled_home_mode(datetime.fromtimestamp(now, self.timezone))
        elif self._state["presence"] == "away":
            away_since = self._state.get("away_since")
            if away_since is not None and now - float(away_since) >= self.away_debounce:
                self._state["mode"] = "armed_away"
        # unavailable is intentionally fail-safe: retain the last known mode.
        return previous != self._state["mode"]

    def observe_presence(self, state: str, attributes: dict | None = None, now: float | None = None) -> str:
        if state not in {"home", "away", "unavailable"}:
            raise ValueError(f"Unsupported presence state: {state!r}")
        now = self._now() if now is None else now
        attributes = attributes or {}
        with self._lock:
            self._reload_if_changed()
            self._state["presence"] = state
            self._state["presence_devices"] = deepcopy(attributes.get("devices", {}))
            self._state["router_sources"] = deepcopy(attributes.get("sources", {}))
            if state == "away":
                if self._state.get("away_since") is None:
                    self._state["away_since"] = now
            elif state == "home":
                self._state["away_since"] = None
            else:
                # Unknown router state breaks the proof of continuous absence.
                self._state["away_since"] = None
            self._apply_automatic(now)
            self._save(now)
            return self._state["mode"]

    def set_automatic(self, now: float | None = None) -> str:
        now = self._now() if now is None else now
        with self._lock:
            self._reload_if_changed()
            self._state["control"] = "automatic"
            self._apply_automatic(now)
            self._save(now)
            return self._state["mode"]

    def set_manual(self, mode: str | None = None, now: float | None = None) -> str:
        if mode is not None and mode not in ALARM_MODES:
            raise ValueError(f"Unsupported alarm mode: {mode!r}")
        now = self._now() if now is None else now
        with self._lock:
            self._reload_if_changed()
            self._state["control"] = "manual"
            if mode is not None:
                self._state["mode"] = mode
            self._save(now)
            return self._state["mode"]

    def pause_for(self, seconds: float, now: float | None = None) -> None:
        if seconds <= 0:
            raise ValueError("Pause duration must be positive")
        now = self._now() if now is None else now
        with self._lock:
            self._reload_if_changed()
            self._state["notifications_paused_until"] = now + seconds
            self._save(now)

    def pause_until_8(self, moment: datetime | None = None) -> float:
        moment = moment or datetime.now().astimezone()
        target = moment.replace(hour=8, minute=0, second=0, microsecond=0)
        if target <= moment:
            target += timedelta(days=1)
        seconds = target.timestamp() - moment.timestamp()
        self.pause_for(seconds, moment.timestamp())
        return seconds

    def resume_notifications(self, now: float | None = None) -> None:
        now = self._now() if now is None else now
        with self._lock:
            self._reload_if_changed()
            self._state["notifications_paused_until"] = 0.0
            self._save(now)

    def notifications_paused(self, now: float | None = None) -> tuple[bool, float]:
        now = self._now() if now is None else now
        with self._lock:
            self._reload_if_changed()
            remaining = float(self._state["notifications_paused_until"]) - now
            return remaining > 0, max(0.0, remaining)

    def camera_may_alert(self, role: str, classification: str) -> bool:
        with self._lock:
            self._reload_if_changed()
            if classification == "public":
                return False
            # A PTZ visitor is someone who remained at the Garage access long
            # enough to be confirmed by snapshot tracking.  It acts as a
            # doorbell and is therefore independent of the alarm mode.
            if classification == "visitor":
                return True
            mode = self._state["mode"]
            if mode == "disarmed":
                return False
            if mode == "armed_home" and role == "interior":
                return False
            return mode in {"armed_home", "armed_away"}

    def record_detection(
        self,
        camera: str,
        role: str,
        classification: str,
        evidence_path: str | Path | None,
        occurred_at: datetime | None = None,
        detections: list[dict] | None = None,
    ) -> dict | None:
        now = (occurred_at or datetime.now().astimezone()).timestamp()
        with self._lock:
            self._reload_if_changed()
            if not self.camera_may_alert(role, classification):
                return None
            feedback = self._matching_false_positive(
                camera, classification, detections or [], now,
            )
            if feedback is not None:
                feedback["suppressed_count"] = int(feedback.get("suppressed_count", 0)) + 1
                feedback["last_suppressed_at"] = now
                self._save(now)
                print(
                    f"{camera}: detection suppressed by temporary false-positive memory "
                    f"{str(feedback.get('incident_id', ''))[:8]}"
                )
                return None
            incidents = self._state["incidents"]
            incident = incidents.get(camera)
            if not incident or not incident.get("active") or now - float(incident.get("last_activity", 0)) >= self.incident_close:
                incident = {
                    "id": uuid.uuid4().hex,
                    "camera": camera,
                    "role": role,
                    "classification": classification,
                    "active": True,
                    "acknowledged": False,
                    "opened_at": now,
                    "last_activity": now,
                    "notification_count": 0,
                    "next_notification_at": now,
                    "evidence_path": str(evidence_path) if evidence_path else None,
                    "detections": deepcopy(detections or []),
                }
                incidents[camera] = incident
            else:
                incident["last_activity"] = now
                incident["classification"] = classification
                incident["role"] = role
                if evidence_path:
                    incident["evidence_path"] = str(evidence_path)
                if detections:
                    incident["detections"] = deepcopy(detections)
            self._save(now)
            return deepcopy(incident)

    def _matching_false_positive(
        self,
        camera: str,
        classification: str,
        detections: list[dict],
        now: float,
    ) -> dict | None:
        for feedback in reversed(self._state["false_positives"]):
            if feedback.get("camera") != camera:
                continue
            if feedback.get("classification") != classification:
                continue
            if float(feedback.get("expires_at", 0)) <= now:
                continue
            exemplars = feedback.get("detections")
            if not isinstance(exemplars, list):
                continue
            if all_detections_match(
                detections, exemplars,
                max_hash_distance=self.false_positive_hash_distance,
            ):
                return feedback
        return None

    def due_notifications(self, now: float | None = None) -> list[dict]:
        now = self._now() if now is None else now
        with self._lock:
            self._reload_if_changed()
            if float(self._state["notifications_paused_until"]) > now:
                return []
            due = []
            for incident in self._state["incidents"].values():
                if not incident.get("active") or incident.get("acknowledged"):
                    continue
                if not self.camera_may_alert(str(incident.get("role", "exterior")), str(incident.get("classification", "uncertain"))):
                    continue
                if float(incident.get("next_notification_at", now)) <= now:
                    due.append(deepcopy(incident))
            return due

    def mark_notified(self, camera: str, now: float | None = None) -> None:
        now = self._now() if now is None else now
        with self._lock:
            self._reload_if_changed()
            incident = self._state["incidents"].get(camera)
            if not incident or not incident.get("active"):
                return
            count = int(incident.get("notification_count", 0)) + 1
            incident["notification_count"] = count
            interval = self.notification_backoff[min(count, len(self.notification_backoff) - 1)]
            incident["next_notification_at"] = now + interval
            self._save(now)

    def acknowledge(self, camera: str | None = None, now: float | None = None) -> int:
        now = self._now() if now is None else now
        count = 0
        with self._lock:
            self._reload_if_changed()
            for name, incident in self._state["incidents"].items():
                if incident.get("active") and not incident.get("acknowledged") and (camera is None or name.lower() == camera.lower()):
                    incident["acknowledged"] = True
                    count += 1
            if count:
                self._save(now)
            return count

    def mark_false_positive(
        self, camera: str | None = None, now: float | None = None,
    ) -> dict | None:
        """Dismiss the latest matching incident and retain its evidence as feedback."""
        now = self._now() if now is None else now
        with self._lock:
            self._reload_if_changed()
            candidates = [
                incident for name, incident in self._state["incidents"].items()
                if camera is None or name.lower() == camera.lower()
            ]
            if not candidates:
                return None
            incident = max(
                candidates,
                key=lambda item: float(item.get("last_activity", item.get("opened_at", 0))),
            )
            # Without an explicit camera, avoid accidentally labeling stale
            # evidence if IGNORAR is sent long after an alert.
            latest = float(incident.get("last_activity", incident.get("opened_at", 0)))
            if camera is None and now - latest > 24 * 3600:
                return None
            incident["acknowledged"] = True
            incident["false_positive"] = True
            incident["false_positive_at"] = now
            feedback = {
                "incident_id": incident.get("id"),
                "camera": incident.get("camera"),
                "classification": incident.get("classification"),
                "evidence_path": incident.get("evidence_path"),
                "opened_at": incident.get("opened_at"),
                "marked_at": now,
                "expires_at": now + self.false_positive_ttl,
                "detections": deepcopy(incident.get("detections", [])),
                "suppressed_count": 0,
            }
            stored = self._state["false_positives"]
            existing = next((
                item for item in stored
                if item.get("incident_id") == feedback["incident_id"]
            ), None)
            if existing is not None:
                existing.update(feedback)
                feedback = existing
            else:
                stored.append(feedback)
                del stored[:-500]
            self._save(now)
            return deepcopy(feedback)

    def tick(self, now: float | None = None) -> bool:
        """Apply time-based transitions and close inactive incidents."""
        now = self._now() if now is None else now
        changed = False
        with self._lock:
            self._reload_if_changed()
            changed |= self._apply_automatic(now)
            if 0 < float(self._state["notifications_paused_until"]) <= now:
                self._state["notifications_paused_until"] = 0.0
                changed = True
            for incident in self._state["incidents"].values():
                if incident.get("active") and now - float(incident.get("last_activity", now)) >= self.incident_close:
                    incident["active"] = False
                    incident["closed_at"] = now
                    changed = True
            if changed:
                self._save(now)
        return changed

    def snapshot(self) -> dict:
        with self._lock:
            self._reload_if_changed()
            result = deepcopy(self._state)
            now = self._now()
            result["notifications_paused"] = float(result["notifications_paused_until"]) > now
            result["notifications_pause_remaining"] = max(0.0, float(result["notifications_paused_until"]) - now)
            result["active_incidents"] = [
                deepcopy(item) for item in result["incidents"].values() if item.get("active")
            ]
            return result

    def handle_command(self, command: str) -> str | None:
        """Handle the shared Telegram command vocabulary; return None if unknown."""
        normalized = command.strip().lower()
        normalized = re.sub(r"^/", "", normalized)
        if normalized in {"help", "ayuda", "commands", "comandos"}:
            return (
                "🤖 Comandos de Security Cam\n\n"
                "🛡️ Alarma\n"
                "/auto · /manual · /away · /home · /disarm · /status\n\n"
                "📷 Cámaras\n"
                "/photo · /video\n\n"
                "🔕 Notificaciones\n"
                "10m · 1h · 1h30m · /pause 2h · /until8 · /resume\n\n"
                "✅ OK reconoce un incidente\n"
                "🚫 IGNORAR lo marca como falso positivo temporal"
            )
        if normalized in {"auto", "automatico", "automático"}:
            mode = self.set_automatic()
            return (
                "✅ CONTROL AUTOMÁTICO ACTIVADO\n"
                f"🛡️ Alarma: {MODE_LABELS.get(mode, mode)}"
            )
        if normalized == "manual":
            mode = self.set_manual()
            return (
                "⚠️ CONTROL MANUAL ACTIVADO\n"
                f"🛡️ Alarma: {MODE_LABELS.get(mode, mode)}\n"
                "ℹ️ Enviá /auto para volver al control por presencia y horario."
            )
        manual_modes = {
            "away": "armed_away", "fuera": "armed_away",
            "home": "armed_home", "casa": "armed_home",
            "disarm": "disarmed", "desarmar": "disarmed",
        }
        if normalized in manual_modes:
            mode = self.set_manual(manual_modes[normalized])
            icon = "🔓" if mode == "disarmed" else "🔒"
            warning = (
                "\n🚨 La alarma permanecerá desarmada hasta /auto o un armado manual."
                if mode == "disarmed" else ""
            )
            return (
                "⚠️ CONTROL MANUAL\n"
                f"{icon} Alarma: {MODE_LABELS.get(mode, mode)}{warning}"
            )
        if normalized in {"resume", "unmute", "reanudar"}:
            self.resume_notifications()
            return "🔔 NOTIFICACIONES REANUDADAS\n✅ Las alertas vuelven a estar activas."
        if normalized in {"until8", "hasta8"}:
            seconds = self.pause_until_8()
            return (
                "🔕 NOTIFICACIONES PAUSADAS\n"
                f"🌅 Hasta las 08:00 ({_format_duration(seconds)})"
            )
        if normalized.startswith("ok"):
            target = normalized[2:].strip() or None
            count = self.acknowledge(target)
            return (
                f"✅ {count} incidente(s) reconocido(s)."
                if count else "ℹ️ No hay incidentes activos para reconocer."
            )
        false_positive_prefixes = ("ignore", "ignorar", "falsepositive", "falso positivo")
        for prefix in false_positive_prefixes:
            if normalized == prefix or normalized.startswith(prefix + " "):
                target = normalized[len(prefix):].strip() or None
                feedback = self.mark_false_positive(target)
                if feedback:
                    if not feedback.get("detections"):
                        return (
                            "🚫 FALSO POSITIVO REGISTRADO\n"
                            f"📷 Cámara: {feedback['camera']}\n"
                            "💾 Evidencia conservada. Este incidente era anterior a la memoria "
                            "visual; las próximas alertas sí guardarán coordenadas para comparar."
                        )
                    return (
                        "🚫 FALSO POSITIVO REGISTRADO\n"
                        f"📷 Cámara: {feedback['camera']}\n"
                        f"⏱️ Memoria temporal: {_format_duration(self.false_positive_ttl)}\n"
                        "🎯 Sólo se ignorarán coincidencias visuales en esa misma zona."
                    )
                return "ℹ️ No encontré un incidente reciente para marcar como falso positivo."
        if normalized == "status":
            state = self.snapshot()
            control_labels = {"automatic": "automático", "manual": "manual"}
            pause = (
                f"pausadas por {_format_duration(state['notifications_pause_remaining'])}"
                if state["notifications_paused"] else "activas"
            )
            devices = ", ".join(
                f"{name}={'presente' if value is True else 'ausente' if value is False else 'desconocido'}"
                for name, value in state["presence_devices"].items()
            ) or state["presence"]
            routers = ", ".join(
                f"{name}={'en línea' if value.get('available') is True else 'sin conexión'}"
                for name, value in state["router_sources"].items()
                if isinstance(value, dict)
            ) or "sin datos"
            incidents = state["active_incidents"]
            incident_text = ", ".join(
                f"{item['camera']}{' (OK)' if item.get('acknowledged') else ''}" for item in incidents
            ) or "ninguno"
            mode_icon = "🔓" if state["mode"] == "disarmed" else "🔒"
            control_icon = "✅" if state["control"] == "automatic" else "⚠️"
            return (
                "🛡️ Estado de Security Cam\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"{mode_icon} Alarma: {MODE_LABELS.get(state['mode'], state['mode'])}\n"
                f"{control_icon} Control: {control_labels.get(state['control'], state['control'])}\n"
                f"🏠 Presencia: {devices}\n"
                f"📡 Routers: {routers}\n"
                f"🔔 Notificaciones: {pause}\n"
                f"🚨 Incidentes: {incident_text}"
            )
        seconds = _duration(normalized)
        if seconds is not None:
            if seconds > 30 * 86400:
                return "⚠️ La pausa máxima es 30d."
            self.pause_for(seconds)
            return (
                "🔕 NOTIFICACIONES PAUSADAS\n"
                f"⏱️ Duración: {_format_duration(seconds)}\n"
                "🔔 Enviá /resume para reanudarlas antes."
            )
        return None
