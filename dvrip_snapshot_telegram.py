"""Send one current camera image to Telegram without waiting for an alarm.

DVRIP is used only to verify the camera connection.  Xiongmai DVRIP alarm
messages do not carry JPEG data, so the actual image is captured from RTSP.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from argparse import ArgumentParser
from datetime import datetime
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import cv2
import numpy as np

from dvrip_events import DVRIPConnection


def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Set {name} before running this script.")
    return value


def capture_rtsp_jpeg(stream_url: str, width: int, height: int, timeout: float) -> bytes:
    """Decode one raw frame, then JPEG-encode it locally.

    Writing MJPEG directly from FFmpeg proved unreliable on some XMeye
    combined-lens streams. Raw BGR output is the exact mechanism used by the
    live monitor, with local OpenCV encoding only after a complete frame has
    arrived.
    """
    command = [
        "ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp",
        "-i", stream_url, "-an", "-sn", "-dn", "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
    ]
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 5)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Timed out while requesting a frame from RTSP.") from error
    expected_bytes = width * height * 3
    if completed.returncode or len(completed.stdout) < expected_bytes:
        # FFmpeg normally repeats the credential-bearing input URL in errors.
        # Keep a short useful diagnostic without ever echoing that secret.
        diagnostic = completed.stderr.decode("utf-8", errors="replace")
        diagnostic = re.sub(r"(?:rtsp|rtsps)://\S+", "RTSP_URL", diagnostic)
        diagnostic = " ".join(line.strip() for line in diagnostic.splitlines() if line.strip())[-500:]
        suffix = f" FFmpeg: {diagnostic}" if diagnostic else ""
        raise RuntimeError(f"Could not decode a full RTSP frame (received {len(completed.stdout):,} of {expected_bytes:,} bytes).{suffix}")
    frame = np.frombuffer(completed.stdout[:expected_bytes], dtype=np.uint8).reshape((height, width, 3))
    success, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not success:
        raise RuntimeError("OpenCV could not JPEG-encode the RTSP frame.")
    return jpeg.tobytes()


def send_telegram_photo(token: str, chat_id: str, image: bytes, caption: str) -> None:
    boundary = "----securitycam" + uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"camera.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode(),
        image,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    request = Request(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data=b"".join(parts), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError("Telegram did not accept the photo.")


def main() -> None:
    parser = ArgumentParser(description="Capture one current RTSP frame and send it to Telegram.")
    parser.add_argument("--config", default="smart-monitor.json")
    parser.add_argument("--stream", default=os.environ.get("CAMERA_GARAGE_RTSP_URL"))
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    if not args.stream:
        parser.error("provide --stream or set CAMERA_GARAGE_RTSP_URL")

    with open(args.config, encoding="utf-8") as source:
        config = json.load(source)
    layout = config.get("layout", {})
    width = max((int(bounds[2]) for bounds in layout.values()), default=0)
    height = max((int(bounds[3]) for bounds in layout.values()), default=0)
    if not width or not height:
        raise RuntimeError("smart-monitor.json needs layout bounds to decode one RTSP frame.")
    dvrip = config.get("dvrip", {})
    endpoint = urlparse(config["onvif"]["device_service"])
    host = dvrip.get("host", endpoint.hostname)
    if not host:
        raise RuntimeError("No camera host was found in smart-monitor.json.")
    username = os.environ.get(dvrip.get("username_env", "CAMERA_DVRIP_USER")) or get_env(config["onvif"].get("username_env", "CAMERA_GARAGE_ONVIF_USER"))
    password = os.environ.get(dvrip.get("password_env", "CAMERA_DVRIP_PASSWORD")) or get_env(config["onvif"].get("password_env", "CAMERA_GARAGE_ONVIF_PASSWORD"))

    connection = DVRIPConnection(host, int(dvrip.get("port", 34567)), username, password, timeout=args.timeout)
    try:
        connection.connect()
        connection.login()
    finally:
        connection.close()
    print("DVRIP login confirmed; capturing current RTSP frame...")

    image = capture_rtsp_jpeg(args.stream, width, height, args.timeout)
    print(f"JPEG captured ({len(image):,} bytes); sending to Telegram...")
    send_telegram_photo(
        get_env("TELEGRAM_BOT_TOKEN"), get_env("TELEGRAM_CHAT_ID"), image,
        "Manual garage-camera snapshot · " + datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
    )
    print("Telegram photo sent.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Snapshot failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
