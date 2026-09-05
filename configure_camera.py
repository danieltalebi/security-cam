"""Add a named camera to the multi-camera monitor without saving secrets in Git."""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

from camera_config import slug


ROOT = Path(__file__).resolve().parent


def prompt(label: str, default: str | None = None, secret: bool = False, required: bool = True) -> str:
    reader = getpass.getpass if secret else input
    suffix = f" [{default}]" if default else ""
    while True:
        value = reader(f"{label}{suffix}: ").strip() or (default or "")
        if not value and not required:
            return ""
        if not value:
            print("A value is required.")
            continue
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            print("The value contains a control character. In Command Prompt, paste with right-click and try again.")
            continue
        return value


def save_environment(values: dict[str, str]) -> None:
    if os.name != "nt":
        print("\nOn macOS/Linux, add these variables to your shell profile (values are not written by this setup):")
        for name in values:
            print(f"  export {name}=<value>")
        return
    for name, value in values.items():
        completed = subprocess.run(["setx", name, value], capture_output=True, text=True, check=False)
        if completed.returncode:
            raise RuntimeError(f"Could not save {name} as a Windows user variable.")


def load_json(path: Path, fallback: dict) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else fallback


def main() -> None:
    print("\nAdd security camera\n")
    name = prompt("Camera name (for example garage or back patio)")
    key = slug(name)
    filename = f"{name.strip().lower().replace(' ', '-')}-monitor.json"
    mode = prompt("Mode: property (private/public classifier) or presence (any person alerts)", "presence").lower()
    if mode not in {"property", "presence"}:
        raise RuntimeError("Mode must be property or presence.")
    host = prompt("Camera IP address")
    username = prompt("Camera username")
    password = prompt("Camera password", secret=True)
    rtsp = prompt("Complete RTSP URL", required=mode == "property")
    snapshot = prompt("JPEG snapshot URL", f"http://{host}/webcapture.jpg?command=snap&channel=0")
    bot_token = prompt("Telegram bot token (leave blank to keep existing)", secret=True, required=False)
    chat_id = prompt("Telegram chat ID (leave blank to keep existing)", required=False)

    template_name = "smart-monitor.example.json" if mode == "property" else "backyard-monitor.example.json"
    config = load_json(ROOT / template_name, {})
    prefix = f"CAMERA_{key}"
    config["camera"] = {"name": name, "credential_prefix": prefix}
    if mode == "property":
        config["camera"]["rtsp_env"] = f"{prefix}_RTSP_URL"
    config["onvif"]["device_service"] = f"http://{host}:8899/onvif/device_service"
    config["dvrip"]["host"] = host
    config["snapshot"]["url"] = snapshot
    config["snapshot"]["review_dir"] = f"events/review/{name.strip().lower().replace(' ', '-') }"
    target = ROOT / filename
    target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    multi_path = ROOT / "multi-camera.json"
    multi = load_json(multi_path, {"cameras": []})
    entry = {"name": name, "mode": mode, "config": filename}
    cameras = [item for item in multi.get("cameras", []) if item.get("name", "").lower() != name.lower()]
    cameras.append(entry)
    multi["cameras"] = cameras
    multi_path.write_text(json.dumps(multi, indent=2) + "\n", encoding="utf-8")

    values = {f"{prefix}_USER": username, f"{prefix}_PASSWORD": password}
    if rtsp:
        values[f"{prefix}_RTSP_URL"] = rtsp
    if bot_token:
        values["TELEGRAM_BOT_TOKEN"] = bot_token
    if chat_id:
        values["TELEGRAM_CHAT_ID"] = chat_id
    save_environment(values)
    print(f"\nAdded {name!r} to multi-camera.json using {prefix}_USER and {prefix}_PASSWORD.")
    if os.name == "nt":
        print("Close this window and open a new Command Prompt before running the monitor.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nSetup cancelled.")
        sys.exit(1)
    except Exception as error:
        print(f"\nSetup failed: {type(error).__name__}: {error}")
        sys.exit(1)
