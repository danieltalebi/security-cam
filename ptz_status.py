"""Optional ONVIF PTZ status polling with graceful failure handling."""

from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlparse


class PtzStatusPoller:
    def __init__(self, config: dict):
        self.config = config
        self.status = {
            "state": "starting",
            "pan": None,
            "tilt": None,
            "zoom": None,
            "profile": None,
            "poll_count": 0,
            "updated_at": None,
        }
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)

    def snapshot(self) -> dict:
        with self.lock:
            return self.status.copy()

    def _set_status(self, **values):
        with self.lock:
            self.status.update(values)

    def _run(self):
        interval = max(0.2, float(self.config.get("poll_seconds", 1)))
        try:
            from onvif import ONVIFCamera
            endpoint = urlparse(self.config["device_service"])
            username = os.environ.get(self.config.get("username_env", "CAMERA_ONVIF_USER"))
            password = os.environ.get(self.config.get("password_env", "CAMERA_ONVIF_PASSWORD"))
            if not endpoint.hostname or not endpoint.port:
                self._set_status(state="unavailable: invalid ONVIF endpoint")
                return
            if not username or not password:
                self._set_status(state="unavailable: ONVIF credentials not set")
                return

            camera = ONVIFCamera(endpoint.hostname, endpoint.port, username, password)
            media = camera.create_media_service()
            ptz = camera.create_ptz_service()
            profiles = media.GetProfiles()
            desired_name = self.config.get("profile_name")
            profile = next((p for p in profiles if p.Name == desired_name), profiles[0])
            profile_token = profile.token
            self._set_status(state="connected", profile=profile.Name)

            while not self.stop_event.is_set():
                response = ptz.GetStatus({"ProfileToken": profile_token})
                position = getattr(response, "Position", None)
                pan_tilt = getattr(position, "PanTilt", None)
                zoom = getattr(position, "Zoom", None)
                self._set_status(
                    state="connected",
                    pan=getattr(pan_tilt, "x", None),
                    tilt=getattr(pan_tilt, "y", None),
                    zoom=getattr(zoom, "x", None),
                    poll_count=self.snapshot()["poll_count"] + 1,
                    updated_at=time.monotonic(),
                )
                self.stop_event.wait(interval)
        except Exception as error:
            # This is intentionally compact because some ONVIF libraries include
            # request details in exception strings.
            self._set_status(state=f"unavailable: {type(error).__name__}")
