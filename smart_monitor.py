"""Rules and lightweight person tracking for dual-lens security cameras."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


Box = tuple[int, int, int, int]
Point = tuple[float, float]


def _point_in_polygon(point: Point, polygon: list[Point]) -> bool:
    """Return True when point is inside polygon (ray-casting algorithm)."""
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            crossing_x = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < crossing_x:
                inside = not inside
        j = i
    return inside


@dataclass
class Region:
    name: str
    bounds: Box

    def contains(self, point: Point) -> bool:
        x1, y1, x2, y2 = self.bounds
        return x1 <= point[0] <= x2 and y1 <= point[1] <= y2


@dataclass
class Zone:
    name: str
    region: str
    polygon: list[Point]
    rule: str
    dwell_seconds: float = 0.0

    def contains(self, point: Point) -> bool:
        return _point_in_polygon(point, self.polygon)


@dataclass
class Track:
    track_id: int
    region: str
    center: Point
    first_seen: float
    last_seen: float
    zone_since: dict[str, float] = field(default_factory=dict)
    zone_anchor: dict[str, Point] = field(default_factory=dict)
    emitted_zones: set[str] = field(default_factory=set)


class EventSink:
    """Console + optional JSONL event output, ready to replace with MQTT/webhooks."""

    def __init__(self, jsonl_path: str | None = None):
        self.jsonl_path = Path(jsonl_path) if jsonl_path else None

    def emit(self, event: dict) -> None:
        print("ALERT " + json.dumps(event, ensure_ascii=False, sort_keys=True))
        if self.jsonl_path:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self.jsonl_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(event, ensure_ascii=False) + "\n")


class SmartMonitor:
    def __init__(self, config: dict, event_sink: EventSink | None = None):
        layout = config["layout"]
        self.regions = {
            name: Region(name, tuple(values)) for name, values in layout.items()
        }
        self.zones = [
            Zone(
                name=item["name"],
                region=item["region"],
                polygon=[tuple(point) for point in item["polygon"]],
                rule=item["rule"],
                dwell_seconds=float(item.get("dwell_seconds", 0)),
            )
            for item in config.get("zones", [])
        ]
        tracking = config.get("tracking", {})
        self.match_distance = float(tracking.get("match_distance_pixels", 120))
        self.stationary_tolerance = float(tracking.get("stationary_tolerance_pixels", 40))
        self.track_timeout = float(tracking.get("track_timeout_seconds", 2.0))
        self.alert_cooldown = float(tracking.get("alert_cooldown_seconds", 30.0))
        self.tracks: dict[int, Track] = {}
        self.next_track_id = 1
        self.last_rule_alert: dict[tuple[str, str], float] = {}
        self.event_sink = event_sink or EventSink(config.get("events", {}).get("jsonl_path"))

    @classmethod
    def from_file(cls, filename: str) -> "SmartMonitor":
        with open(filename, encoding="utf-8") as source:
            return cls(json.load(source))

    def has_active_tracks(self, now: float) -> bool:
        return any(now - track.last_seen <= self.track_timeout for track in self.tracks.values())

    def process(self, detections: Iterable[Box], now: float) -> list[dict]:
        candidates = []
        for box in detections:
            x1, y1, x2, y2 = box
            # Feet are a better proxy than box center for ground-plane zones.
            point = ((x1 + x2) / 2, y2)
            region = next((r.name for r in self.regions.values() if r.contains(point)), None)
            if region:
                candidates.append((box, point, region))

        unmatched_tracks = set(self.tracks)
        assignments: list[tuple[Track, Box]] = []
        for box, point, region in candidates:
            compatible = [
                track for track in self.tracks.values()
                if track.track_id in unmatched_tracks and track.region == region
            ]
            track = min(compatible, key=lambda t: math.dist(t.center, point), default=None)
            if track is None or math.dist(track.center, point) > self.match_distance:
                track = Track(self.next_track_id, region, point, now, now)
                self.tracks[track.track_id] = track
                self.next_track_id += 1
            else:
                unmatched_tracks.remove(track.track_id)
                track.center = point
                track.last_seen = now
            assignments.append((track, box))

        expired = [tid for tid, track in self.tracks.items() if now - track.last_seen > self.track_timeout]
        for track_id in expired:
            del self.tracks[track_id]

        events = []
        for track, box in assignments:
            active_zones = {zone.name for zone in self.zones if zone.region == track.region and zone.contains(track.center)}
            for zone_name in list(track.zone_since):
                if zone_name not in active_zones:
                    del track.zone_since[zone_name]
                    track.zone_anchor.pop(zone_name, None)
                    track.emitted_zones.discard(zone_name)

            for zone in self.zones:
                if zone.region != track.region or zone.name not in active_zones:
                    continue
                if zone.name not in track.zone_since:
                    track.zone_since[zone.name] = now
                    track.zone_anchor[zone.name] = track.center
                elif zone.rule == "dwell" and math.dist(track.zone_anchor[zone.name], track.center) > self.stationary_tolerance:
                    # A person continuing to walk through the sidewalk must not
                    # satisfy the dwell rule merely by remaining in the polygon.
                    track.zone_since[zone.name] = now
                    track.zone_anchor[zone.name] = track.center
                    track.emitted_zones.discard(zone.name)
                elapsed = now - track.zone_since[zone.name]
                ready = zone.rule == "immediate" or (zone.rule == "dwell" and elapsed >= zone.dwell_seconds)
                if ready and zone.name not in track.emitted_zones:
                    event = self._event(zone, track, box, elapsed, now)
                    if event:
                        events.append(event)
                    track.emitted_zones.add(zone.name)
        return events

    def _event(self, zone: Zone, track: Track, box: Box, elapsed: float, now: float) -> dict | None:
        cooldown_key = (zone.name, zone.rule)
        if now - self.last_rule_alert.get(cooldown_key, float("-inf")) < self.alert_cooldown:
            return None
        self.last_rule_alert[cooldown_key] = now
        event = {
            "type": "person_detected" if zone.rule == "immediate" else "person_loitering",
            "zone": zone.name,
            "region": zone.region,
            "track_id": track.track_id,
            "dwell_seconds": round(elapsed, 2),
            "box": list(box),
            "timestamp": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        }
        self.event_sink.emit(event)
        return event
