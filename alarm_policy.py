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


ALARM_MODES = {"disarmed", "armed_home", "armed_away"}
CONTROL_MODES = {"automatic", "manual"}
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
        self.away_debounce = max(0.0, float(automatic.get("away_debounce_seconds", 300)))
        self.incident_close = max(1.0, float(incidents.get("close_after_seconds", 60)))
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
            self._state["mode"] = self._scheduled_home_mode(datetime.fromtimestamp(now).astimezone())
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
    ) -> dict | None:
        now = (occurred_at or datetime.now().astimezone()).timestamp()
        with self._lock:
            self._reload_if_changed()
            if not self.camera_may_alert(role, classification):
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
                }
                incidents[camera] = incident
            else:
                incident["last_activity"] = now
                incident["classification"] = classification
                incident["role"] = role
                if evidence_path:
                    incident["evidence_path"] = str(evidence_path)
            self._save(now)
            return deepcopy(incident)

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
                "Controles: /auto, /manual, /away, /home, /disarm, /status, "
                "/resume, /until8; pausas: 10m, 1h, 1h30m o /pause 2h; "
                "responda OK para reconocer el incidente activo."
            )
        if normalized in {"auto", "automatico", "automático"}:
            mode = self.set_automatic()
            return f"Control automático activado. Modo: {mode}."
        if normalized == "manual":
            mode = self.set_manual()
            return f"Control manual activado. Modo: {mode}."
        manual_modes = {
            "away": "armed_away", "fuera": "armed_away",
            "home": "armed_home", "casa": "armed_home",
            "disarm": "disarmed", "desarmar": "disarmed",
        }
        if normalized in manual_modes:
            mode = self.set_manual(manual_modes[normalized])
            return f"Control manual. Modo: {mode}."
        if normalized in {"resume", "unmute", "reanudar"}:
            self.resume_notifications()
            return "Notificaciones reanudadas."
        if normalized in {"until8", "hasta8"}:
            seconds = self.pause_until_8()
            return f"Notificaciones pausadas hasta las 08:00 ({_format_duration(seconds)})."
        if normalized.startswith("ok"):
            target = normalized[2:].strip() or None
            count = self.acknowledge(target)
            return f"{count} incidente(s) reconocido(s)." if count else "No hay incidentes activos para reconocer."
        if normalized == "status":
            state = self.snapshot()
            pause = (
                f"pausadas por {_format_duration(state['notifications_pause_remaining'])}"
                if state["notifications_paused"] else "activas"
            )
            devices = ", ".join(
                f"{name}={'presente' if value is True else 'ausente' if value is False else 'desconocido'}"
                for name, value in state["presence_devices"].items()
            ) or state["presence"]
            incidents = state["active_incidents"]
            incident_text = ", ".join(
                f"{item['camera']}{' (OK)' if item.get('acknowledged') else ''}" for item in incidents
            ) or "ninguno"
            return (
                f"Modo {state['mode']} ({state['control']}). Presencia: {devices}. "
                f"Notificaciones {pause}. Incidentes: {incident_text}."
            )
        seconds = _duration(normalized)
        if seconds is not None:
            if seconds > 30 * 86400:
                return "La pausa máxima es 30d."
            self.pause_for(seconds)
            return f"Notificaciones pausadas por {_format_duration(seconds)}."
        return None
