"""Fetch one JPEG snapshot through the camera's ONVIF Media service.

This is deliberately read-only.  It discovers the device-provided snapshot
URI instead of guessing a vendor-specific HTTP endpoint.
"""

from __future__ import annotations

import json
import os
from base64 import b64encode
from argparse import ArgumentParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit
from urllib.request import (
    HTTPBasicAuthHandler,
    HTTPDigestAuthHandler,
    HTTPPasswordMgrWithDefaultRealm,
    Request,
    build_opener,
)

from camera_config import credentials


def select_profile(profiles, requested_name: str | None):
    if requested_name:
        profile = next((item for item in profiles if item.Name == requested_name), None)
        if profile is None:
            available = ", ".join(repr(item.Name) for item in profiles)
            raise RuntimeError(f"ONVIF profile {requested_name!r} was not found. Available: {available}")
        return profile
    return profiles[0]


def download_snapshot(uri: str, username: str, password: str, timeout: float) -> bytes:
    password_manager = HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password(None, uri, username, password)
    opener = build_opener(
        HTTPBasicAuthHandler(password_manager),
        HTTPDigestAuthHandler(password_manager),
    )
    # Several Xiongmai firmwares accept Basic auth but do not issue a usable
    # challenge for urllib's retry path. Send it proactively while retaining
    # the Basic/Digest handlers above for standards-compliant cameras.
    basic_token = b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    request = Request(uri, headers={"Accept": "image/jpeg", "Authorization": f"Basic {basic_token}"})
    try:
        with opener.open(request, timeout=timeout) as response:
            image = response.read()
            content_type = response.headers.get_content_type()
    except HTTPError as error:
        if error.code == 401:
            auth_method = error.headers.get("WWW-Authenticate", "not supplied")
            raise RuntimeError(f"Snapshot endpoint rejected the configured credentials (HTTP 401; challenge: {auth_method}).") from error
        raise
    if content_type != "image/jpeg" or not image.startswith(b"\xff\xd8"):
        raise RuntimeError("The ONVIF snapshot URI did not return a JPEG image.")
    return image


def xmeye_snapshot_uri(uri: str, username: str, password: str) -> str:
    """Add XMeye's non-standard snapshot query authentication safely."""
    parsed = urlsplit(uri)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({"user": username, "password": password})
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def main():
    parser = ArgumentParser(description="Save one JPEG snapshot from the ONVIF Media service.")
    parser.add_argument("--config", default="smart-monitor.json")
    parser.add_argument("--output", default="snapshot-test.jpg")
    parser.add_argument("--profile", help="Override the ONVIF media profile name from the config.")
    parser.add_argument("--snapshot_url", help="Test an explicit camera JPEG URL instead of asking ONVIF for one.")
    parser.add_argument("--xmeye_query_auth", action="store_true", help="Append user/password query parameters required by some XMeye snapshot endpoints.")
    parser.add_argument("--timeout", type=float, default=8)
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as source:
        config = json.load(source)
    onvif_config = config["onvif"]
    endpoint = urlparse(onvif_config["device_service"])
    username, password = credentials(config)
    if not endpoint.hostname or not endpoint.port:
        raise SystemExit("Invalid ONVIF device_service in the configuration.")

    if args.snapshot_url:
        uri = args.snapshot_url
        profile_name = "explicit URL"
    else:
        from onvif import ONVIFCamera

        camera = ONVIFCamera(endpoint.hostname, endpoint.port, username, password)
        media = camera.create_media_service()
        profile = select_profile(media.GetProfiles(), args.profile or onvif_config.get("profile_name"))
        snapshot = media.GetSnapshotUri({"ProfileToken": profile.token})
        uri = str(getattr(snapshot, "Uri", snapshot))
        profile_name = profile.Name
    parsed_uri = urlsplit(uri)
    print(f"Requesting ONVIF snapshot from {parsed_uri.scheme}://{parsed_uri.hostname}:{parsed_uri.port or 80}{parsed_uri.path}")
    if args.xmeye_query_auth:
        uri = xmeye_snapshot_uri(uri, username, password)
    image = download_snapshot(uri, username, password, args.timeout)
    output = Path(args.output)
    output.write_bytes(image)
    print(f"Saved JPEG snapshot ({len(image):,} bytes) for profile {profile_name!r}: {output}")


if __name__ == "__main__":
    main()
