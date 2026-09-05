"""Shared camera configuration and credential conventions.

Each camera has a human name (for example ``garage`` or ``back patio``).  Its
credentials live in environment variables derived from that name:

    CAMERA_GARAGE_USER, CAMERA_GARAGE_PASSWORD, CAMERA_GARAGE_RTSP_URL

Configuration files only contain endpoints and policy; they never contain
secrets.  The legacy ``username_env``/``password_env`` fields remain supported
so existing installations keep working while they are migrated.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse


def slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_").upper()
    if not value:
        raise ValueError("Camera name must contain a letter or number.")
    return value


def camera_name(config: dict, fallback: str = "camera") -> str:
    return str(config.get("camera", {}).get("name") or fallback)


def credential_prefix(config: dict, fallback: str = "camera") -> str:
    configured = config.get("camera", {}).get("credential_prefix")
    return str(configured or f"CAMERA_{slug(camera_name(config, fallback))}").upper()


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Set {name} before running this script.")
    return value


def credentials(config: dict, fallback: str = "camera") -> tuple[str, str]:
    """Return unified credentials, falling back to old per-protocol fields."""
    prefix = credential_prefix(config, fallback)
    user = os.environ.get(f"{prefix}_USER")
    password = os.environ.get(f"{prefix}_PASSWORD")
    if user and password:
        return user, password

    onvif = config.get("onvif", {})
    user = user or os.environ.get(onvif.get("username_env", ""))
    password = password or os.environ.get(onvif.get("password_env", ""))
    if user and password:
        return user, password
    raise RuntimeError(f"Set {prefix}_USER and {prefix}_PASSWORD before running this script.")


def rtsp_url(config: dict, fallback: str = "camera") -> str | None:
    camera = config.get("camera", {})
    # Pre-named single-camera installations used this variable. Keep direct
    # legacy launches working while the multi-camera launcher passes its name.
    if not camera and fallback == "camera":
        return os.environ.get("CAMERA_GARAGE_RTSP_URL")
    return os.environ.get(camera.get("rtsp_env") or f"{credential_prefix(config, fallback)}_RTSP_URL")


def dvrip_endpoint(config: dict) -> tuple[str, int]:
    onvif = config.get("onvif", {})
    dvrip = config.get("dvrip", {})
    endpoint = urlparse(onvif.get("device_service", ""))
    host = dvrip.get("host") or endpoint.hostname
    if not host:
        raise RuntimeError("Camera host is missing.")
    return str(host), int(dvrip.get("port", 34567))
