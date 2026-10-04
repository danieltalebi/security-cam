"""Shared camera configuration and credential conventions.

Each camera has a human name (for example ``garage`` or ``back patio``).  Its
credentials live in environment variables derived from that name:

    CAMERA_GARAGE_USER, CAMERA_GARAGE_PASSWORD, CAMERA_GARAGE_RTSP_URL

Configuration files only contain endpoints and policy; they never contain
secrets.  The legacy ``username_env``/``password_env`` fields remain supported
so existing installations keep working while they are migrated.
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
from urllib.parse import urlparse, urlsplit, urlunsplit


def slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_").upper()
    if not value:
        raise ValueError("Camera name must contain a letter or number.")
    return value


def configured_value(name: str) -> str | None:
    """Read a setting from the environment or the current user's Keychain."""
    if not name:
        return None
    value = os.environ.get(name)
    if value:
        return value
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                [
                    "security", "find-generic-password",
                    "-a", getpass.getuser(),
                    "-s", f"security-cam-{name}",
                    "-w",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0 and result.stdout.rstrip("\n"):
                return result.stdout.rstrip("\n")
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def camera_name(config: dict, fallback: str = "camera") -> str:
    return str(config.get("camera", {}).get("name") or fallback)


def credential_prefix(config: dict, fallback: str = "camera") -> str:
    configured = config.get("camera", {}).get("credential_prefix")
    return str(configured or f"CAMERA_{slug(camera_name(config, fallback))}").upper()


def camera_host(config: dict, fallback: str = "camera") -> str:
    """Return the one camera address used by every protocol endpoint."""
    prefix = credential_prefix(config, fallback)
    camera = config.get("camera", {})
    configured = camera.get("host") or config.get("dvrip", {}).get("host")
    host = os.environ.get(f"{prefix}_HOST") or configured
    if not host or host == "{host}":
        endpoint = urlparse(config.get("onvif", {}).get("device_service", ""))
        host = endpoint.hostname
    if not host:
        raise RuntimeError(f"Set {prefix}_HOST or camera.host.")
    return str(host)


def expand_host(value: str, config: dict, fallback: str = "camera") -> str:
    value = str(value)
    host = camera_host(config, fallback)
    if "{host}" in value:
        return value.replace("{host}", host)
    # Old configurations contain the IP in every URL. An explicit HOST
    # environment override upgrades those files without rewriting them.
    prefix = credential_prefix(config, fallback)
    if not os.environ.get(f"{prefix}_HOST"):
        return value
    parsed = urlsplit(value)
    if not parsed.hostname or parsed.hostname == host:
        return value
    position = parsed.netloc.rfind(parsed.hostname)
    if position < 0:
        return value
    netloc = parsed.netloc[:position] + host + parsed.netloc[position + len(parsed.hostname):]
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def required_env(name: str) -> str:
    value = configured_value(name)
    if not value:
        raise RuntimeError(f"Set {name} before running this script.")
    return value


def credentials(config: dict, fallback: str = "camera") -> tuple[str, str]:
    """Return unified credentials, falling back to old per-protocol fields."""
    prefix = credential_prefix(config, fallback)
    user = configured_value(f"{prefix}_USER")
    password = configured_value(f"{prefix}_PASSWORD")
    if user and password:
        return user, password

    onvif = config.get("onvif", {})
    user = user or configured_value(onvif.get("username_env", ""))
    password = password or configured_value(onvif.get("password_env", ""))
    if user and password:
        return user, password
    raise RuntimeError(f"Set {prefix}_USER and {prefix}_PASSWORD before running this script.")


def rtsp_url(config: dict, fallback: str = "camera") -> str | None:
    camera = config.get("camera", {})
    # Pre-named single-camera installations used this variable. Keep direct
    # legacy launches working while the multi-camera launcher passes its name.
    if not camera and fallback == "camera":
        value = configured_value("CAMERA_GARAGE_RTSP_URL")
    else:
        value = configured_value(camera.get("rtsp_env") or f"{credential_prefix(config, fallback)}_RTSP_URL")
    value = value or camera.get("rtsp_url")
    return expand_host(value, config, fallback) if value else None


def dvrip_endpoint(config: dict, fallback: str = "camera") -> tuple[str, int]:
    onvif = config.get("onvif", {})
    dvrip = config.get("dvrip", {})
    endpoint = urlparse(onvif.get("device_service", ""))
    host = os.environ.get(f"{credential_prefix(config, fallback)}_HOST") or dvrip.get("host") or config.get("camera", {}).get("host") or endpoint.hostname
    if host == "{host}":
        host = camera_host(config, fallback)
    if not host:
        raise RuntimeError("Camera host is missing.")
    return str(host), int(dvrip.get("port", 34567))
