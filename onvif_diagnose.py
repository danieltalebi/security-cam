"""Print safe ONVIF PTZ diagnostics for every media profile."""

import json
import os
import time
from argparse import ArgumentParser
from urllib.parse import urlparse


def position_values(status):
    position = getattr(status, "Position", None)
    pan_tilt = getattr(position, "PanTilt", None)
    zoom = getattr(position, "Zoom", None)
    return (
        getattr(pan_tilt, "x", None),
        getattr(pan_tilt, "y", None),
        getattr(zoom, "x", None),
    )


def report(label, operation):
    try:
        print(f"{label}: {operation()}")
    except Exception as error:
        print(f"{label}: unavailable ({type(error).__name__})")


parser = ArgumentParser(description="Inspect PTZ positions returned by every ONVIF profile.")
parser.add_argument("--config", default="smart-monitor.json")
parser.add_argument("--samples", type=int, default=12, help="One sample per second.")
args = parser.parse_args()

with open(args.config, encoding="utf-8") as source:
    config = json.load(source)["onvif"]

endpoint = urlparse(config["device_service"])
username = os.environ.get(config.get("username_env", "CAMERA_ONVIF_USER"))
password = os.environ.get(config.get("password_env", "CAMERA_ONVIF_PASSWORD"))
if not endpoint.hostname or not endpoint.port:
    raise SystemExit("Invalid ONVIF device_service in the configuration.")
if not username or not password:
    raise SystemExit("Set the ONVIF username and password environment variables first.")

from onvif import ONVIFCamera

camera = ONVIFCamera(endpoint.hostname, endpoint.port, username, password)
media = camera.create_media_service()
ptz = camera.create_ptz_service()
profiles = media.GetProfiles()

print("ONVIF PTZ profile diagnostics (credentials are never printed)")
report("PTZ service capabilities", ptz.GetServiceCapabilities)
report("PTZ nodes", ptz.GetNodes)
for profile in profiles:
    ptz_config = getattr(profile, "PTZConfiguration", None)
    print(f"- profile={profile.Name!r} token={profile.token!r} ptz_config={getattr(ptz_config, 'token', None)!r}")
    if ptz_config:
        report(
            f"  configuration options for {profile.Name!r}",
            lambda config_token=ptz_config.token: ptz.GetConfigurationOptions({"ConfigurationToken": config_token}),
        )
    report(
        f"  presets for {profile.Name!r}",
        lambda profile_token=profile.token: ptz.GetPresets({"ProfileToken": profile_token}),
    )

print("Move the PTZ now; each row is a new GetStatus response.")
for sample in range(args.samples):
    row = []
    for profile in profiles:
        try:
            pan, tilt, zoom = position_values(ptz.GetStatus({"ProfileToken": profile.token}))
            row.append(f"{profile.Name}: pan={pan} tilt={tilt} zoom={zoom}")
        except Exception as error:
            row.append(f"{profile.Name}: {type(error).__name__}")
    print(f"{sample + 1:02d}: " + " | ".join(row))
    time.sleep(1)
