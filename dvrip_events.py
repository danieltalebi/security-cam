"""Persistent, read-only DVRIP alarm subscription for Xiongmai/ICSee cameras."""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import threading
import time
from collections.abc import Callable


HEADER_FORMAT = "<BB2xII2xHI"
HEADER_LENGTH = struct.calcsize(HEADER_FORMAT)
LOGIN_REQUEST = 1000
KEEPALIVE_REQUEST = 1006
KEEPALIVE_RESPONSE = 1007
ALARM_SET_REQUEST = 1500
ALARM_SET_RESPONSE = 1501
ALARM_INFO = 1504


def xmeye_hash(password: str) -> str:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    digest = hashlib.md5(password.encode("utf-8")).digest()
    return "".join(alphabet[(left + right) % len(alphabet)] for left, right in zip(digest[::2], digest[1::2]))


class DVRIPConnection:
    def __init__(self, host: str, port: int, username: str, password: str, timeout: float = 8.0):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.timeout = timeout
        self.socket: socket.socket | None = None
        self.session = 0
        self.sequence = 0

    def connect(self) -> None:
        self.socket = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.socket.settimeout(self.timeout)

    def close(self) -> None:
        if self.socket:
            self.socket.close()
            self.socket = None

    def _read_exact(self, length: int) -> bytes:
        if not self.socket:
            raise ConnectionError("DVRIP socket is closed")
        chunks = []
        while length:
            chunk = self.socket.recv(length)
            if not chunk:
                raise ConnectionError("camera closed the DVRIP connection")
            chunks.append(chunk)
            length -= len(chunk)
        return b"".join(chunks)

    def send(self, command: int, payload: dict) -> None:
        if not self.socket:
            raise ConnectionError("DVRIP socket is closed")
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\x0a\x00"
        header = struct.pack(HEADER_FORMAT, 255, 0, self.session, self.sequence, command, len(data))
        self.socket.sendall(header + data)
        self.sequence += 1

    def receive(self) -> tuple[int, dict | None, bytes]:
        header = self._read_exact(HEADER_LENGTH)
        _, _, self.session, _, command, length = struct.unpack(HEADER_FORMAT, header)
        raw = self._read_exact(length) if length else b""
        try:
            payload = json.loads(raw.rstrip(b"\x00\x0a").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        return command, payload, raw

    def login(self) -> dict:
        self.send(LOGIN_REQUEST, {
            "EncryptType": "MD5", "LoginType": "DVRIP-Web",
            "PassWord": xmeye_hash(self.password), "UserName": self.username,
        })
        _, reply, _ = self.receive()
        if not reply or reply.get("Ret") not in (100, 515, "100", "515"):
            raise PermissionError(f"DVRIP login failed: {reply.get('Ret') if reply else 'invalid response'}")
        session_id = reply.get("SessionID")
        if isinstance(session_id, str):
            self.session = int(session_id, 16)
        return reply


def classify_alarm(payload: dict | None) -> str:
    """Best-effort type label. The raw event is retained because OEM schemas vary."""
    text = json.dumps(payload or {}, ensure_ascii=False).lower()
    if any(word in text for word in ("human", "person", "pedestrian", "ped")):
        return "person"
    if "face" in text:
        return "face"
    if "motion" in text:
        return "motion"
    return "unknown"


class DVRIPAlarmListener:
    """Reconnects automatically and forwards read-only AlarmInfo pushes to a callback."""

    def __init__(
        self, host: str, port: int, username: str, password: str,
        on_event: Callable[[dict], None], on_status: Callable[[str], None] | None = None,
        reconnect_seconds: float = 3.0,
    ):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.on_event, self.on_status = on_event, on_status or (lambda _message: None)
        self.reconnect_seconds = reconnect_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dvrip-alarm-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.reconnect_seconds + 2)

    def _run(self) -> None:
        while not self._stop.is_set():
            connection = DVRIPConnection(self.host, self.port, self.username, self.password)
            try:
                connection.connect()
                connection.login()
                connection.send(ALARM_SET_REQUEST, {"Name": "", "SessionID": f"0x{connection.session:08X}"})
                command, response, _ = connection.receive()
                if command != ALARM_SET_RESPONSE or not response or response.get("Ret") not in (100, "100"):
                    self.on_status("DVRIP alarm subscription was not confirmed; listening anyway.")
                else:
                    self.on_status("DVRIP alarm listener connected.")
                if connection.socket:
                    connection.socket.settimeout(1.0)
                last_keepalive = time.monotonic()
                while not self._stop.is_set():
                    if time.monotonic() - last_keepalive >= 15:
                        connection.send(KEEPALIVE_REQUEST, {"Name": "KeepAlive", "SessionID": f"0x{connection.session:08X}"})
                        last_keepalive = time.monotonic()
                    try:
                        command, payload, raw = connection.receive()
                    except socket.timeout:
                        continue
                    if command == KEEPALIVE_RESPONSE:
                        continue
                    if command == ALARM_INFO:
                        alarm_info = (payload or {}).get("AlarmInfo", {})
                        self.on_event({
                            "source": "dvrip", "type": classify_alarm(payload), "payload": payload,
                            "event_name": alarm_info.get("Event"),
                            "status": alarm_info.get("Status"),
                            "channel": alarm_info.get("Channel"),
                            "raw_length": len(raw), "received_at": time.time(),
                        })
                    else:
                        self.on_status(f"DVRIP message {command} received while listening.")
            except (OSError, ConnectionError, PermissionError, struct.error) as error:
                self.on_status(f"DVRIP listener reconnecting: {type(error).__name__}: {error}")
            finally:
                connection.close()
            self._stop.wait(self.reconnect_seconds)
