"""Print DVRIP camera alarm pushes without changing any camera configuration."""

from __future__ import annotations

import json
import time
from argparse import ArgumentParser

from camera_config import credentials, dvrip_endpoint
from dvrip_events import DVRIPAlarmListener


def main() -> None:
    parser = ArgumentParser(description="Listen for ICSee/XMeye DVRIP alarm events (read-only).")
    parser.add_argument("--config", default="smart-monitor.json")
    parser.add_argument("--duration", type=float, default=120, help="Seconds to listen; 0 means indefinitely.")
    args = parser.parse_args()
    config = json.loads(open(args.config, encoding="utf-8").read())
    host, port = dvrip_endpoint(config)
    username, password = credentials(config)

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
