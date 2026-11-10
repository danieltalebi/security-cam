"""Persistent router and Internet outage history with one daily summary."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, time as datetime_time, timedelta, timezone as fixed_timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram_notify import format_duration


class ConnectivityHistory:
    def __init__(
        self,
        state_file: str | Path,
        notify: Callable[[str], object],
        router_names: list[str],
        *,
        internet_threshold_seconds: float = 600,
        router_alert_seconds: float = 3600,
        summary_time: str = "18:00",
        timezone: str | None = None,
        summary_context: Callable[[], str | None] | None = None,
        clock: Callable[[], float] = time.time,
        start_thread: bool = False,
    ):
        self.state_file = Path(state_file).resolve()
        self.notify = notify
        self.internet_threshold = float(internet_threshold_seconds)
        self.router_alert_seconds = float(router_alert_seconds)
        if self.internet_threshold <= 0 or self.router_alert_seconds <= 0:
            raise ValueError("connectivity alert thresholds must be positive")
        hour_text, minute_text = summary_time.split(":", 1)
        self.summary_clock = datetime_time(int(hour_text), int(minute_text))
        if timezone in {"UTC", "Etc/UTC"}:
            self.timezone = fixed_timezone.utc
        elif timezone:
            try:
                self.timezone = ZoneInfo(timezone)
            except ZoneInfoNotFoundError:
                self.timezone = datetime.now().astimezone().tzinfo
                print(
                    f"Timezone database does not contain {timezone!r}; "
                    "using the Windows local timezone. Install tzdata for named zones."
                )
        else:
            self.timezone = datetime.now().astimezone().tzinfo
        self.summary_context = summary_context
        self.clock = clock
        self.lock = threading.RLock()
        self.delivery_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.state = self._load()
        with self.lock:
            for name in router_names:
                self._connection("router", name)
            self._connection("internet", "Internet")
            self._save()
        if start_thread:
            self.start()

    def _initial_state(self) -> dict:
        now = self.clock()
        local = datetime.fromtimestamp(now, self.timezone)
        last_date = (
            local.date().isoformat()
            if local.time() >= self.summary_clock
            else (local.date() - timedelta(days=1)).isoformat()
        )
        return {
            "version": 1,
            "connections": {},
            "incidents": [],
            "pending_notifications": [],
            "last_summary_at": now,
            "last_summary_date": last_date,
            "pending_summary_date": None,
        }

    def _load(self) -> dict:
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError("state root is not an object")
            initial = self._initial_state()
            for key, value in initial.items():
                state.setdefault(key, value)
            return state
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return self._initial_state()

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_file)

    @staticmethod
    def _key(kind: str, name: str) -> str:
        return f"{kind}:{name}"

    def _connection(self, kind: str, name: str) -> dict:
        key = self._key(kind, name)
        return self.state["connections"].setdefault(key, {
            "kind": kind,
            "name": name,
            "online": None,
            "offline_since": None,
            "incident_id": None,
        })

    def _incident(self, incident_id: str | None) -> dict | None:
        if not incident_id:
            return None
        return next(
            (item for item in self.state["incidents"] if item.get("id") == incident_id),
            None,
        )

    def set_router(self, name: str, online: bool, now: float | None = None) -> None:
        self._set_connection("router", name, online, now)

    def set_internet(self, online: bool, now: float | None = None) -> None:
        self._set_connection("internet", "Internet", online, now)

    def _set_connection(
        self, kind: str, name: str, online: bool, now: float | None,
    ) -> None:
        now = self.clock() if now is None else now
        recovered: dict | None = None
        with self.lock:
            connection = self._connection(kind, name)
            previous = connection.get("online")
            if previous is online:
                return
            if not online:
                if previous is not False:
                    incident = {
                        "id": uuid.uuid4().hex,
                        "kind": kind,
                        "name": name,
                        "started_at": now,
                        "ended_at": None,
                        "router_alert_sent": False,
                        "recovery_alert_sent": False,
                    }
                    self.state["incidents"].append(incident)
                    connection["incident_id"] = incident["id"]
                    connection["offline_since"] = now
                    print(f"{name} outage started at {self._timestamp(now)}")
                connection["online"] = False
            else:
                if previous is False:
                    recovered = self._incident(connection.get("incident_id"))
                    if recovered is not None and recovered.get("ended_at") is None:
                        recovered["ended_at"] = now
                    started_at = float(connection.get("offline_since") or now)
                    print(
                        f"{name} recovered at {self._timestamp(now)} after "
                        f"{format_duration(now - started_at)}"
                    )
                connection["online"] = True
                connection["offline_since"] = None
                connection["incident_id"] = None
                if kind == "internet" and recovered is not None:
                    duration = now - float(recovered["started_at"])
                    if duration >= self.internet_threshold:
                        self._queue_notification(
                            f"internet-recovery:{recovered['id']}",
                            (
                                "✅ Internet volvió. El corte iniciado a las "
                                f"{self._timestamp(float(recovered['started_at']))} duró "
                                f"{format_duration(duration)}."
                            ),
                            recovered["id"], "recovery_alert_sent",
                        )
            self._save()
        if online:
            self._flush_notifications()
            self.tick(now)

    def _queue_notification(
        self, key: str, message: str, incident_id: str, delivered_field: str,
    ) -> None:
        if any(item.get("key") == key for item in self.state["pending_notifications"]):
            return
        self.state["pending_notifications"].append({
            "key": key,
            "message": message,
            "incident_id": incident_id,
            "delivered_field": delivered_field,
        })

    def _flush_notifications(self) -> None:
        with self.delivery_lock:
            while True:
                with self.lock:
                    if self._internet_offline() or not self.state["pending_notifications"]:
                        return
                    pending = dict(self.state["pending_notifications"][0])
                try:
                    self.notify(str(pending["message"]))
                except Exception as error:
                    print(
                        "Telegram connectivity notification failed: "
                        f"{type(error).__name__}: {error}"
                    )
                    return
                with self.lock:
                    self.state["pending_notifications"] = [
                        item for item in self.state["pending_notifications"]
                        if item.get("key") != pending.get("key")
                    ]
                    incident = self._incident(str(pending.get("incident_id", "")))
                    if incident is not None:
                        incident[str(pending["delivered_field"])] = True
                    self._save()

    def _internet_offline(self) -> bool:
        return self._connection("internet", "Internet").get("online") is False

    def tick(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        with self.lock:
            for connection in self.state["connections"].values():
                if connection.get("kind") != "router" or connection.get("online") is not False:
                    continue
                incident = self._incident(connection.get("incident_id"))
                if incident is None or incident.get("router_alert_sent"):
                    continue
                duration = now - float(incident["started_at"])
                if duration >= self.router_alert_seconds:
                    self._queue_notification(
                        f"router-hour:{incident['id']}",
                        (
                            f"⚠️ Router {incident['name']} sigue caído desde "
                            f"{self._timestamp(float(incident['started_at']))} "
                            f"({format_duration(duration)})."
                        ),
                        incident["id"], "router_alert_sent",
                    )

            local = datetime.fromtimestamp(now, self.timezone)
            scheduled = datetime.combine(
                local.date(), self.summary_clock, tzinfo=self.timezone,
            )
            today = local.date().isoformat()
            if now >= scheduled.timestamp() and self.state.get("last_summary_date") != today:
                self.state["pending_summary_date"] = today
            self._save()
        self._flush_notifications()
        self._flush_summary(now)

    def _flush_summary(self, now: float) -> None:
        with self.delivery_lock:
            with self.lock:
                pending_date = self.state.get("pending_summary_date")
                if not pending_date or self._internet_offline():
                    return
                message = self._summary_message(now)
            try:
                self.notify(message)
            except Exception as error:
                print(
                    f"Telegram daily connectivity summary failed: {type(error).__name__}: {error}"
                )
                return
            with self.lock:
                self.state["last_summary_at"] = now
                self.state["last_summary_date"] = pending_date
                self.state["pending_summary_date"] = None
                cutoff = now - 8 * 86400
                self.state["incidents"] = [
                    item for item in self.state["incidents"]
                    if item.get("ended_at") is None or float(item["ended_at"]) >= cutoff
                ]
                self._save()

    def _summary_message(self, now: float) -> str:
        since = float(self.state.get("last_summary_at", now))
        status_lines = []
        connections = sorted(
            self.state["connections"].values(),
            key=lambda item: (item.get("kind") != "internet", str(item.get("name"))),
        )
        for item in connections:
            online = item.get("online")
            if online is True:
                status = "en línea"
            elif online is False:
                duration = now - float(item.get("offline_since") or now)
                status = f"CAÍDA abierta ({format_duration(duration)})"
            else:
                status = "sin datos"
            status_lines.append(f"• {item['name']}: {status}")

        incident_lines = []
        for incident in self.state["incidents"]:
            ended = incident.get("ended_at")
            if (
                ended is not None
                and float(incident["started_at"]) < since
                and float(ended) < since
            ):
                continue
            end = now if ended is None else float(ended)
            suffix = " · abierta" if ended is None else ""
            incident_lines.append(
                f"• {incident['name']}: {self._timestamp(float(incident['started_at']))}, "
                f"{format_duration(end - float(incident['started_at']))}{suffix}"
            )
        incidents = "\n".join(incident_lines) or "• Sin caídas desde el resumen anterior."
        message = (
            "📡 Resumen diario de conectividad\n"
            "Estado actual:\n" + "\n".join(status_lines) +
            "\nCaídas desde el resumen anterior:\n" + incidents
        )
        if self.summary_context is not None:
            try:
                context = self.summary_context()
            except Exception as error:
                print(f"Daily summary context failed: {type(error).__name__}: {error}")
            else:
                if context:
                    message += "\n\n" + str(context)
        return message

    def _timestamp(self, value: float) -> str:
        return datetime.fromtimestamp(value, self.timezone).strftime("%Y-%m-%d %H:%M:%S %Z")

    def start(self) -> "ConnectivityHistory":
        if self.thread is None:
            self.thread = threading.Thread(
                target=self._run, name="connectivity-summary", daemon=True,
            )
            self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            self.tick()
            self.stop_event.wait(30)
