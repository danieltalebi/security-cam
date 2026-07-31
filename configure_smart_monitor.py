"""Interactive first-time configuration for the Windows smart monitor."""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def prompt(label: str, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    reader = getpass.getpass if secret else input
    while True:
        value = reader(f"{label}{suffix}: ").strip()
        value = value or (default or "")
        if value:
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                print("The value contains a control character. In Command Prompt, paste with right-click instead of Ctrl+V, then try again.")
                continue
            return value
        print("A value is required.")


def set_windows_user_variable(name: str, value: str) -> None:
    """Persist a secret outside the repository for new Windows processes."""
    try:
        completed = subprocess.run(["setx", name, value], capture_output=True, text=True, check=False)
    except FileNotFoundError as error:
        raise RuntimeError("This setup assistant must be run from Windows.") from error
    if completed.returncode:
        raise RuntimeError(f"Could not save {name} as a Windows user variable.")


def main() -> None:
    if os.name != "nt":
        raise SystemExit("This setup assistant is intended for Windows.")

    root = Path(__file__).resolve().parent
    template = root / "smart-monitor.example.json"
    target = root / "smart-monitor.json"
    config = json.loads(template.read_text(encoding="utf-8")) if not target.exists() else json.loads(target.read_text(encoding="utf-8"))

    existing_host = urlparse(config["onvif"]["device_service"]).hostname
    print("\nSmart garage monitor setup\n")
    print("Passwords and Telegram secrets are saved as Windows user environment variables, not in smart-monitor.json.\n")
    host = prompt("Camera IP address", existing_host or "192.168.1.100")
    rtsp_url = prompt("Complete RTSP URL")
    camera_user = prompt("Camera username")
    camera_password = prompt("Camera password", secret=True)
    bot_token = prompt("Telegram bot token", secret=True)
    chat_id = prompt("Telegram chat ID")
    classifier = config.setdefault("public_area_classifier", {})
    model_path = prompt("Property classifier model path", classifier.get("model_path", "models/public-area/model.pt"))

    default_snapshot = f"http://{host}/webcapture.jpg?command=snap&channel=0"
    snapshot_url = prompt("JPEG snapshot URL", default_snapshot)
    config["onvif"]["device_service"] = f"http://{host}:8899/onvif/device_service"
    config.setdefault("snapshot", {})["url"] = snapshot_url
    config["snapshot"]["xmeye_query_auth"] = True
    config.setdefault("telegram", {}).update({
        "token_env": "TELEGRAM_BOT_TOKEN",
        "chat_id_env": "TELEGRAM_CHAT_ID",
    })
    classifier.update({"enabled": True, "model_path": model_path})
    config["onvif"].update({
        "username_env": "CAMERA_GARAGE_ONVIF_USER",
        "password_env": "CAMERA_GARAGE_ONVIF_PASSWORD",
    })
    config["dvrip"].update({
        "username_env": "CAMERA_DVRIP_USER",
        "password_env": "CAMERA_DVRIP_PASSWORD",
    })

    for name, value in {
        "CAMERA_GARAGE_RTSP_URL": rtsp_url,
        "CAMERA_GARAGE_ONVIF_USER": camera_user,
        "CAMERA_GARAGE_ONVIF_PASSWORD": camera_password,
        "CAMERA_DVRIP_USER": camera_user,
        "CAMERA_DVRIP_PASSWORD": camera_password,
        "TELEGRAM_BOT_TOKEN": bot_token,
        "TELEGRAM_CHAT_ID": chat_id,
    }.items():
        set_windows_user_variable(name, value)

    target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print("\nConfiguration saved.")
    print("Close this window, then double-click run_smart_windows.bat to start the monitor.")
    print("Before the first run, confirm smart-monitor.json has the correct combined-stream layout and that the classifier model exists at the selected path.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nSetup cancelled.")
        sys.exit(1)
    except Exception as error:
        print(f"\nSetup failed: {type(error).__name__}: {error}")
        sys.exit(1)
