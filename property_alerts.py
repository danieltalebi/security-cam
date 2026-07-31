"""Turn YOLO person boxes plus public-area probabilities into property alerts."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable


Box = tuple[int, int, int, int]


@dataclass
class UncertainTrack:
    point: tuple[float, float]
    first_seen: float
    last_seen: float


class PropertyAlertEngine:
    def __init__(self, config: dict, emit: Callable[[dict], None]):
        layout = config["layout"]
        self.fixed = tuple(layout["fixed"])
        self.ptz = tuple(layout["ptz"])
        policy = config.get("public_area_classifier", {})
        self.public_threshold = float(policy.get("public_threshold", 0.65))
        self.private_threshold = float(policy.get("private_threshold", 0.35))
        self.uncertain_dwell = float(policy.get("uncertain_dwell_seconds", 8))
        tracking = config.get("tracking", {})
        self.cooldown = float(tracking.get("alert_cooldown_seconds", 30))
        self.match_distance = float(tracking.get("match_distance_pixels", 180))
        self.timeout = float(tracking.get("track_timeout_seconds", 2))
        self.emit = emit
        self.last_alert: dict[str, float] = {}
        self.uncertain_tracks: list[UncertainTrack] = []

    @staticmethod
    def _contains(bounds: tuple[int, int, int, int], point: tuple[float, float]) -> bool:
        x1, y1, x2, y2 = bounds
        return x1 <= point[0] <= x2 and y1 <= point[1] <= y2

    def _alert(self, kind: str, box: Box, now: float, public_probability: float | None = None) -> dict | None:
        if now - self.last_alert.get(kind, float("-inf")) < self.cooldown:
            return None
        self.last_alert[kind] = now
        event = {
            "type": "person_detected", "decision": kind, "box": list(box),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        }
        if public_probability is not None:
            event["public_probability"] = round(public_probability, 3)
        self.emit(event)
        return event

    def process(self, boxes: list[Box], probability_at: Callable[[tuple[float, float]], float | None], now: float) -> list[dict]:
        events = []
        self.uncertain_tracks = [track for track in self.uncertain_tracks if now - track.last_seen <= self.timeout]
        for box in boxes:
            point = ((box[0] + box[2]) / 2, box[3])
            if self._contains(self.fixed, point):
                event = self._alert("fixed_property", box, now)
            elif self._contains(self.ptz, point):
                probability = probability_at(point)
                if probability is not None and probability >= self.public_threshold:
                    continue
                if probability is not None and probability <= self.private_threshold:
                    event = self._alert("ptz_not_public", box, now, probability)
                else:
                    track = min(self.uncertain_tracks, key=lambda item: math.dist(item.point, point), default=None)
                    if track is None or math.dist(track.point, point) > self.match_distance:
                        track = UncertainTrack(point, now, now)
                        self.uncertain_tracks.append(track)
                    else:
                        track.point, track.last_seen = point, now
                    event = self._alert("ptz_uncertain_dwell", box, now, probability) if now - track.first_seen >= self.uncertain_dwell else None
            else:
                event = None
            if event:
                events.append(event)
        return events
