"""Send one harmless Telegram message to verify the local credentials."""

from __future__ import annotations

import os
import sys
from datetime import datetime

from telegram_notify import TelegramNotifier


def required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set.")
    return value


def main() -> None:
    notifier = TelegramNotifier(required("TELEGRAM_BOT_TOKEN"), required("TELEGRAM_CHAT_ID"))
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    notifier.send_message(f"Security camera: Telegram connection test successful · {timestamp}")
    print("Telegram test message sent.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Telegram test failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
