"""Telegram notifications plus chat commands for temporarily muting alerts."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen


_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([smhd])", re.IGNORECASE)


def parse_duration(text: str) -> float | None:
    """Parse values such as ``10m``, ``1h``, or ``1h 30m`` into seconds."""
    value = text.strip().lower()
    if value.startswith("/pause"):
        value = value[6:].strip()
    elif value.startswith("pause"):
        value = value[5:].strip()
    if not value:
        return None
    position = 0
    seconds = 0.0
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    for match in _DURATION_PART.finditer(value):
        if value[position:match.start()].strip():
            return None
        seconds += float(match.group(1)) * multipliers[match.group(2).lower()]
        position = match.end()
    if value[position:].strip() or seconds <= 0:
        return None
    return seconds


def format_duration(seconds: float) -> str:
    remaining = max(0, int(seconds + 0.999))
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, seconds = divmod(remaining, 60)
    parts = []
    for amount, unit in ((days, "d"), (hours, "h"), (minutes, "m"), (seconds, "s")):
        if amount:
            parts.append(f"{amount}{unit}")
    return " ".join(parts) or "0s"


class TelegramNotifier:
    _controllers: dict[tuple[str, str, str], "TelegramNotifier"] = {}
    _controllers_lock = threading.Lock()

    def __init__(self, token: str, chat_id: str, state_file: str = ".telegram-mute.json"):
        if any(ord(character) < 32 or ord(character) == 127 for character in token):
            raise ValueError("TELEGRAM_BOT_TOKEN contains a control character; re-enter it with the Windows setup assistant.")
        self.token, self.chat_id = token, str(chat_id)
        self.state_file = Path(state_file).resolve()
        self._state_lock = threading.Lock()
        self._listener_started = False
        self._muted_until = 0.0
        self._update_offset: int | None = None
        self._load_state()

    def _load_state(self) -> None:
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            self._muted_until = float(state.get("muted_until", 0))
            offset = state.get("update_offset")
            self._update_offset = int(offset) if offset is not None else None
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    def _save_state(self) -> None:
        state = {"muted_until": self._muted_until, "update_offset": self._update_offset}
        temporary = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_file)

    def is_muted(self) -> tuple[bool, float]:
        with self._state_lock:
            remaining = self._muted_until - time.time()
        return remaining > 0, max(0.0, remaining)

    def mute_for(self, seconds: float) -> None:
        with self._state_lock:
            self._muted_until = time.time() + seconds
            self._save_state()

    def resume(self) -> None:
        with self._state_lock:
            self._muted_until = 0.0
            self._save_state()

    def send_photo(self, image: bytes, caption: str) -> bool:
        muted, remaining = self.is_muted()
        if muted:
            print(f"Telegram notification muted ({format_duration(remaining)} remaining)")
            return False
        boundary = "----securitycam" + uuid.uuid4().hex
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{self.chat_id}\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"camera.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode(),
            image, f"\r\n--{boundary}--\r\n".encode(),
        ]
        request = Request(
            f"https://api.telegram.org/bot{self.token}/sendPhoto", data=b"".join(parts), method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError("Telegram did not accept the photo.")
        return True

    def send_message(self, text: str) -> None:
        payload = json.dumps({"chat_id": self.chat_id, "text": text}).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage", data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError("Telegram did not accept the message.")

    def _get_updates(self, timeout: int, offset: int | None = None) -> list[dict]:
        payload: dict[str, object] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        request = Request(
            f"https://api.telegram.org/bot{self.token}/getUpdates",
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=timeout + 10) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError("Telegram did not return bot updates.")
        return result.get("result", [])

    def _handle_command(self, update: dict) -> None:
        message = update.get("message", {})
        incoming_chat = str(message.get("chat", {}).get("id", ""))
        if incoming_chat != self.chat_id:
            return
        text = str(message.get("text", "")).strip()
        command = re.sub(r"^(/[a-z]+)@[a-z0-9_]+", r"\1", text.lower())
        if command in {"resume", "/resume", "unmute", "/unmute"}:
            self.resume()
            self.send_message("Notifications resumed.")
            return
        if command in {"status", "/status"}:
            muted, remaining = self.is_muted()
            self.send_message(f"Notifications are muted for {format_duration(remaining)} more." if muted else "Notifications are active.")
            return
        seconds = parse_duration(command)
        if seconds is not None:
            if seconds > 30 * 86400:
                self.send_message("The maximum pause is 30d.")
                return
            self.mute_for(seconds)
            self.send_message(f"Notifications paused for {format_duration(seconds)}. Send resume to enable them early.")
            return
        self.send_message("Send a duration such as 10m, 1h, or 1h30m. Send status or resume at any time.")

    def _listen(self) -> None:
        while True:
            try:
                if self._update_offset is None:
                    previous = self._get_updates(0, -1)
                    self._update_offset = int(previous[-1]["update_id"]) + 1 if previous else 0
                    with self._state_lock:
                        self._save_state()
                for update in self._get_updates(25, self._update_offset):
                    self._update_offset = int(update["update_id"]) + 1
                    with self._state_lock:
                        self._save_state()
                    self._handle_command(update)
            except Exception as error:
                print(f"Telegram command listener failed: {type(error).__name__}: {error}")
                time.sleep(5)

    def start_command_listener(self) -> "TelegramNotifier":
        """Start exactly one command listener for this bot, chat, and state file."""
        key = (self.token, self.chat_id, str(self.state_file))
        with self._controllers_lock:
            controller = self._controllers.get(key)
            if controller is None:
                controller = self
                self._controllers[key] = controller
            if not controller._listener_started:
                controller._listener_started = True
                threading.Thread(target=controller._listen, name="telegram-commands", daemon=True).start()
        return controller
