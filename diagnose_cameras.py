"""Read-only health check for every camera in multi-camera.json."""

from __future__ import annotations

import json
import os
from argparse import ArgumentParser
from pathlib import Path

from dvrip_events import DVRIPConnection
from dvrip_smart_telegram import load_windows_user_environment
from multi_camera_monitor import camera_credentials
from onvif_snapshot import download_snapshot, xmeye_snapshot_uri
from telegram_notify import TelegramNotifier


def load(path: Path) -> dict:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def check_camera(name: str, config: dict, send_telegram: bool) -> bool:
    ok = True
    try:
        host, port, user, password = camera_credentials(config)
        connection = DVRIPConnection(host, port, user, password, timeout=8)
        try:
            connection.connect()
            connection.login()
        finally:
            connection.close()
        print(f"{name}: DVRIP OK")
    except Exception as error:
        print(f"{name}: DVRIP FAIL ({type(error).__name__})")
        ok = False

    try:
        onvif = config["onvif"]
        user = os.environ[onvif.get("username_env", "CAMERA_ONVIF_USER")]
        password = os.environ[onvif.get("password_env", "CAMERA_ONVIF_PASSWORD")]
        snapshot = config["snapshot"]
        uri = snapshot["url"]
        if snapshot.get("xmeye_query_auth", False):
            uri = xmeye_snapshot_uri(uri, user, password)
        image = download_snapshot(uri, user, password, 8)
        print(f"{name}: snapshot OK ({len(image):,} bytes)")
    except Exception as error:
        print(f"{name}: snapshot FAIL ({type(error).__name__})")
        ok = False

    if send_telegram:
        try:
            telegram = config["telegram"]
            notifier = TelegramNotifier(os.environ[telegram["token_env"]], os.environ[telegram["chat_id_env"]])
            notifier.send_message(f"Security camera diagnostic: {name} Telegram OK")
            print(f"{name}: Telegram OK")
        except Exception as error:
            print(f"{name}: Telegram FAIL ({type(error).__name__})")
            ok = False
    return ok


def main() -> None:
    parser = ArgumentParser(description="Test DVRIP and JPEG snapshot access for every configured camera.")
    parser.add_argument("--config", default="multi-camera.json")
    parser.add_argument("--telegram", action="store_true", help="Also send one Telegram test message per camera.")
    args = parser.parse_args()
    load_windows_user_environment()
    multi_path = Path(args.config).resolve()
    multi = load(multi_path)
    outcomes = []
    for entry in multi.get("cameras", []):
        config = load((multi_path.parent / entry["config"]).resolve())
        outcomes.append(check_camera(entry["name"], config, args.telegram))
    if not outcomes:
        raise SystemExit("No cameras were configured.")
    raise SystemExit(0 if all(outcomes) else 1)


if __name__ == "__main__":
    main()
