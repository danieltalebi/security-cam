"""Minimal Telegram photo notifier using only the Python standard library."""

from __future__ import annotations

import json
import uuid
from urllib.request import Request, urlopen


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        if any(ord(character) < 32 or ord(character) == 127 for character in token):
            raise ValueError("TELEGRAM_BOT_TOKEN contains a control character; re-enter it with the Windows setup assistant.")
        self.token, self.chat_id = token, chat_id

    def send_photo(self, image: bytes, caption: str) -> None:
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

    def send_message(self, text: str) -> None:
        payload = json.dumps({"chat_id": self.chat_id, "text": text}).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage", data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError("Telegram did not accept the test message.")
