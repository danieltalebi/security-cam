"""Read-only ICSee/XMeye DVRIP probe tolerant of missing login fields."""

import hashlib
import json
import os
import struct
from argparse import ArgumentParser
from socket import create_connection
from urllib.parse import urlparse


def xmeye_hash(password: str) -> str:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    digest = hashlib.md5(password.encode("utf-8")).digest()
    return "".join(alphabet[(left + right) % len(alphabet)] for left, right in zip(digest[::2], digest[1::2]))


class DVRIPProbe:
    """Small DVRIP client for safe read-only requests.

    Some recent ICSee firmwares omit ``AliveInterval`` from the login reply,
    which breaks older generic DVRIP clients. This parser intentionally accepts
    that optional field.
    """

    def __init__(self, host: str, port: int, username: str, password: str):
        self.sock = create_connection((host, port), timeout=8)
        self.sock.settimeout(8)
        self.username = username
        self.password = password
        self.session = 0
        self.sequence = 0

    def _read_exact(self, length: int) -> bytes:
        chunks = []
        while length:
            chunk = self.sock.recv(length)
            if not chunk:
                raise ConnectionError("camera closed the DVRIP connection")
            chunks.append(chunk)
            length -= len(chunk)
        return b"".join(chunks)

    def request(self, command: int, payload: dict) -> dict:
        self.sequence += 1
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\x0a\x00"
        header = struct.pack("BB2xII2xHI", 255, 0, self.session, self.sequence, command, len(data))
        self.sock.sendall(header + data)
        response = self._read_exact(20)
        _, _, self.session, _, _, length = struct.unpack("BB2xII2xHI", response)
        return json.loads(self._read_exact(length)[:-2])

    def login(self) -> dict:
        reply = self.request(1000, {
            "EncryptType": "MD5",
            "LoginType": "DVRIP-Web",
            "PassWord": xmeye_hash(self.password),
            "UserName": self.username,
        })
        if reply.get("Ret") not in (100, 515):
            raise PermissionError(f"DVRIP login failed with code {reply.get('Ret')}")
        self.session = int(reply["SessionID"], 16)
        return reply

    def get(self, name: str, command: int) -> dict:
        reply = self.request(command, {"Name": name, "SessionID": f"0x{self.session:08X}"})
        return reply.get(name, reply)

    def close(self):
        self.sock.close()


parser = ArgumentParser(description="Inspect the camera's local DVRIP/ICSee service.")
parser.add_argument("--config", default="smart-monitor.json")
args = parser.parse_args()

with open(args.config, encoding="utf-8") as source:
    config = json.load(source)

onvif = config["onvif"]
endpoint = urlparse(onvif["device_service"])
dvrip = config.get("dvrip", {})
host = dvrip.get("host", endpoint.hostname)
port = int(dvrip.get("port", 34567))
username = os.environ.get(dvrip.get("username_env", "CAMERA_DVRIP_USER")) or os.environ.get(onvif.get("username_env", "CAMERA_ONVIF_USER"))
password = os.environ.get(dvrip.get("password_env", "CAMERA_DVRIP_PASSWORD")) or os.environ.get(onvif.get("password_env", "CAMERA_ONVIF_PASSWORD"))
if not host or not username or not password:
    raise SystemExit("Set CAMERA_DVRIP_USER/PASSWORD or CAMERA_ONVIF_USER/PASSWORD first.")

probe = DVRIPProbe(host, port, username, password)
try:
    login = probe.login()
    print(f"DVRIP connected: {host}:{port}")
    print("Login reply fields: " + ", ".join(sorted(login.keys())))
    for name, command in (
        ("SystemInfo", 1020),
        ("SystemFunction", 1360),
        ("Camera", 1042),
        # Proprietary human-detection rules and alarm polygons, when supported.
        ("Detect.HumanDetection.[0]", 1042),
        ("Detect.HumanDetection.[1]", 1042),
        # This is the DVRIP configuration name used by ICSee/XMeye firmware
        # for auto-tracking. It is queried only; no settings are changed.
        ("Detect.DetectTrack", 1042),
    ):
        try:
            print(f"{name}: " + json.dumps(probe.get(name, command), ensure_ascii=False, indent=2, default=str))
        except Exception as error:
            print(f"{name}: unavailable ({type(error).__name__})")
finally:
    probe.close()
