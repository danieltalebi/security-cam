"""Background-stabilized person dwell tracking for intermittent JPEG snapshots."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np


Box = tuple[int, int, int, int]
Point = tuple[float, float]


@dataclass
class PersonObservation:
    box: Box
    foot: Point
    height: float
    confidence: float


@dataclass
class DwellTrack:
    track_id: int
    first_seen: float
    last_seen: float
    positions: list[Point] = field(default_factory=list)
    heights: list[float] = field(default_factory=list)
    detections: int = 0
    emitted: bool = False

    def add(self, point: Point, height: float, now: float) -> None:
        self.positions.append(point)
        self.heights.append(max(1.0, height))
        self.last_seen = now
        self.detections += 1

    @property
    def duration(self) -> float:
        return self.last_seen - self.first_seen

    @property
    def median_height(self) -> float:
        return float(np.median(self.heights)) if self.heights else 1.0

    @property
    def radius_body_heights(self) -> float:
        if not self.positions:
            return float("inf")
        points = np.asarray(self.positions, dtype=np.float32)
        center = np.median(points, axis=0)
        radius = np.linalg.norm(points - center, axis=1).max(initial=0.0)
        return float(radius / self.median_height)

    @property
    def path_body_heights(self) -> float:
        if len(self.positions) < 2:
            return 0.0
        points = np.asarray(self.positions, dtype=np.float32)
        return float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum() / self.median_height)


class BackgroundStabilizer:
    """Estimate current-to-previous camera motion using background features."""

    def __init__(self, max_width: int = 800, max_features: int = 1200, min_matches: int = 18, min_inlier_ratio: float = 0.35):
        self.max_width = max_width
        self.max_features = max_features
        self.min_matches = min_matches
        self.min_inlier_ratio = min_inlier_ratio
        self.orb = cv2.ORB_create(nfeatures=max_features, fastThreshold=12)

    @staticmethod
    def _mask(shape: tuple[int, int], boxes: list[Box], scale: float) -> np.ndarray:
        mask = np.full(shape, 255, dtype=np.uint8)
        for x1, y1, x2, y2 in boxes:
            padding = max(6, int((y2 - y1) * 0.12))
            left = max(0, int((x1 - padding) * scale))
            top = max(0, int((y1 - padding) * scale))
            right = min(shape[1] - 1, int((x2 + padding) * scale))
            bottom = min(shape[0] - 1, int((y2 + padding) * scale))
            cv2.rectangle(mask, (left, top), (right, bottom), 0, -1)
        return mask

    def estimate(self, previous: np.ndarray, current: np.ndarray, previous_boxes: list[Box], current_boxes: list[Box]) -> tuple[np.ndarray, float] | None:
        scale = min(1.0, self.max_width / max(previous.shape[1], current.shape[1]))
        size = (max(1, int(current.shape[1] * scale)), max(1, int(current.shape[0] * scale)))
        previous_gray = cv2.cvtColor(cv2.resize(previous, size), cv2.COLOR_BGR2GRAY)
        current_gray = cv2.cvtColor(cv2.resize(current, size), cv2.COLOR_BGR2GRAY)
        previous_keypoints, previous_descriptors = self.orb.detectAndCompute(previous_gray, self._mask(previous_gray.shape, previous_boxes, scale))
        current_keypoints, current_descriptors = self.orb.detectAndCompute(current_gray, self._mask(current_gray.shape, current_boxes, scale))
        if previous_descriptors is None or current_descriptors is None:
            return None
        matches = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(current_descriptors, previous_descriptors, k=2)
        good = [pair[0] for pair in matches if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance]
        if len(good) < self.min_matches:
            return None
        current_points = np.float32([current_keypoints[item.queryIdx].pt for item in good]).reshape(-1, 1, 2)
        previous_points = np.float32([previous_keypoints[item.trainIdx].pt for item in good]).reshape(-1, 1, 2)
        matrix, inliers = cv2.findHomography(current_points, previous_points, cv2.RANSAC, 3.0)
        if matrix is None or inliers is None or not np.isfinite(matrix).all():
            return None
        inlier_ratio = float(inliers.mean())
        if inlier_ratio < self.min_inlier_ratio:
            return None
        scale_matrix = np.diag([scale, scale, 1.0])
        full_matrix = np.linalg.inv(scale_matrix) @ matrix @ scale_matrix
        return full_matrix, inlier_ratio


class SnapshotDwellTracker:
    """Track PTZ people in coordinates stabilized against background motion."""

    def __init__(self, config: dict):
        self.dwell_seconds = float(config.get("dwell_seconds", 8.0))
        self.min_detections = int(config.get("min_detections", 6))
        self.stationary_radius = float(config.get("stationary_radius_body_heights", 0.55))
        self.match_distance = float(config.get("match_distance_body_heights", 1.5))
        self.track_timeout = float(config.get("track_timeout_seconds", 2.5))
        self.stabilizer = BackgroundStabilizer(
            max_width=int(config.get("background_max_width", 800)),
            max_features=int(config.get("background_max_features", 1200)),
            min_matches=int(config.get("background_min_matches", 18)),
            min_inlier_ratio=float(config.get("background_min_inlier_ratio", 0.35)),
        )
        self.previous_frame: np.ndarray | None = None
        self.previous_boxes: list[Box] = []
        self.to_reference = np.eye(3, dtype=np.float64)
        self.tracks: list[DwellTrack] = []
        self.next_track_id = 1
        self.reliable_transforms = 0

    @staticmethod
    def _transform_point(matrix: np.ndarray, point: Point) -> Point:
        source = np.float32([[point]])
        transformed = cv2.perspectiveTransform(source, matrix)[0, 0]
        return float(transformed[0]), float(transformed[1])

    def _transformed_height(self, matrix: np.ndarray, observation: PersonObservation) -> float:
        top = (observation.foot[0], observation.foot[1] - observation.height)
        foot = self._transform_point(matrix, observation.foot)
        transformed_top = self._transform_point(matrix, top)
        return max(1.0, math.dist(foot, transformed_top))

    def update(self, frame: np.ndarray, observations: list[PersonObservation], now: float) -> tuple[list[DwellTrack], float | None]:
        boxes = [item.box for item in observations]
        transform_quality: float | None = None
        if self.previous_frame is None:
            current_to_reference = np.eye(3, dtype=np.float64)
        else:
            estimate = self.stabilizer.estimate(self.previous_frame, frame, self.previous_boxes, boxes)
            if estimate is None:
                return [], None
            current_to_previous, transform_quality = estimate
            current_to_reference = self.to_reference @ current_to_previous
            self.reliable_transforms += 1

        candidates = []
        for observation in observations:
            point = self._transform_point(current_to_reference, observation.foot)
            height = self._transformed_height(current_to_reference, observation)
            candidates.append((observation, point, height))

        available = set(range(len(self.tracks)))
        for _observation, point, height in candidates:
            compatible = []
            for index in available:
                track = self.tracks[index]
                if now - track.last_seen > self.track_timeout or not track.positions:
                    continue
                normalized = math.dist(track.positions[-1], point) / max(height, track.heights[-1], 1.0)
                if normalized <= self.match_distance:
                    compatible.append((normalized, index))
            if compatible:
                _, index = min(compatible)
                available.remove(index)
                track = self.tracks[index]
            else:
                track = DwellTrack(self.next_track_id, now, now)
                self.next_track_id += 1
                self.tracks.append(track)
            track.add(point, height, now)

        self.tracks = [track for track in self.tracks if now - track.last_seen <= self.track_timeout]
        self.previous_frame = frame.copy()
        self.previous_boxes = boxes
        self.to_reference = current_to_reference

        ready = []
        for track in self.tracks:
            if track.emitted or track.duration < self.dwell_seconds or track.detections < self.min_detections:
                continue
            if track.radius_body_heights <= self.stationary_radius:
                track.emitted = True
                ready.append(track)
        return ready, transform_quality
