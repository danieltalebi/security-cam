"""Print DVRIP camera alarm pushes without changing any camera configuration."""

from __future__ import annotations

import json
import os
import time
from argparse import ArgumentParser
from urllib.parse import urlparse

from dvrip_events import DVRIPAlarmListener


def main() -> None:
    parser = ArgumentParser(description="Listen for ICSee/XMeye DVRIP alarm events (read-only).")
    parser.add_argument("--config", default="smart-monitor.json")
    parser.add_argument("--duration", type=float, default=120, help="Seconds to listen; 0 means indefinitely.")
    args = parser.parse_args()
    config = json.loads(open(args.config, encoding="utf-8").read())
    onvif = config["onvif"]
    dvrip = config.get("dvrip", {})
    endpoint = urlparse(onvif["device_service"])
    host = dvrip.get("host", endpoint.hostname)
    port = int(dvrip.get("port", 34567))
    username = os.environ.get(dvrip.get("username_env", "CAMERA_DVRIP_USER")) or os.environ.get(onvif.get("username_env", "CAMERA_ONVIF_USER"))
    password = os.environ.get(dvrip.get("password_env", "CAMERA_DVRIP_PASSWORD")) or os.environ.get(onvif.get("password_env", "CAMERA_ONVIF_PASSWORD"))
    if not host or not username or not password:
        parser.error("Set the DVRIP credential variables named in smart-monitor.json first.")

    listener = DVRIPAlarmListener(
        host, port, username, password,
        on_event=lambda event: print("ALARM " + json.dumps(event, ensure_ascii=False)),
        on_status=lambda status: print("STATUS " + status),
    )
    listener.start()
    print("Listening for camera events. Trigger a person detection now; Ctrl+C stops it.")
    try:
        if args.duration > 0:
            time.sleep(args.duration)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()


if __name__ == "__main__":
    main()
