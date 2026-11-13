"""Lighting-tolerant visual signatures for temporary false-positive memory."""

from __future__ import annotations

import math

import cv2
import numpy as np


Box = tuple[int, int, int, int]


def appearance_hash(frame: np.ndarray, box: Box) -> str:
    """Return a compact perceptual hash for one detected object crop."""
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = box
    box_height = max(1, y2 - y1)
    box_width = max(1, x2 - x1)
    pad_x = max(2, round(box_width * 0.08))
    pad_y = max(2, round(box_height * 0.05))
    left, top = max(0, x1 - pad_x), max(0, y1 - pad_y)
    right, bottom = min(width, x2 + pad_x), min(height, y2 + pad_y)
    crop = frame[top:bottom, left:right]
    if crop.size == 0:
        raise ValueError("cannot hash an empty detection crop")
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    resized = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    normalized = cv2.equalizeHist(resized)
    coefficients = cv2.dct(np.float32(normalized) / 255.0)[:8, :8].flatten()[1:]
    median = float(np.median(coefficients))
    bits = coefficients > median
    value = 0
    for enabled in bits:
        value = (value << 1) | int(enabled)
    return f"{value:016x}"


def detection_metadata(frame: np.ndarray, boxes: list[Box]) -> list[dict]:
    """Describe detections using normalized coordinates and perceptual hashes."""
    height, width = frame.shape[:2]
    metadata = []
    for raw in boxes:
        x1, y1, x2, y2 = map(int, raw)
        x1, x2 = max(0, x1), min(width, x2)
        y1, y2 = max(0, y1), min(height, y2)
        if x2 <= x1 or y2 <= y1:
            continue
        box = (x1, y1, x2, y2)
        metadata.append({
            "box": [
                round(x1 / width, 6), round(y1 / height, 6),
                round(x2 / width, 6), round(y2 / height, 6),
            ],
            "phash": appearance_hash(frame, box),
        })
    return metadata


def hash_distance(first: str, second: str) -> int:
    try:
        return (int(first, 16) ^ int(second, 16)).bit_count()
    except (TypeError, ValueError):
        return 64


def _intersection_over_union(first: list[float], second: list[float]) -> float:
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right, bottom = min(first[2], second[2]), min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    union = first_area + second_area - intersection
    return intersection / union if union > 0 else 0.0


def detections_match(
    current: dict,
    exemplar: dict,
    *,
    max_hash_distance: int = 10,
    min_iou: float = 0.35,
    max_center_distance: float = 0.07,
) -> bool:
    """Match one object only when both location and appearance agree."""
    first, second = current.get("box"), exemplar.get("box")
    if not (
        isinstance(first, list) and len(first) == 4
        and isinstance(second, list) and len(second) == 4
    ):
        return False
    iou = _intersection_over_union(first, second)
    first_center = ((first[0] + first[2]) / 2, (first[1] + first[3]) / 2)
    second_center = ((second[0] + second[2]) / 2, (second[1] + second[3]) / 2)
    center_distance = math.dist(first_center, second_center)
    first_size = (max(1e-6, first[2] - first[0]), max(1e-6, first[3] - first[1]))
    second_size = (max(1e-6, second[2] - second[0]), max(1e-6, second[3] - second[1]))
    size_similar = all(
        0.55 <= current_size / exemplar_size <= 1.8
        for current_size, exemplar_size in zip(first_size, second_size)
    )
    location_matches = iou >= min_iou or (
        center_distance <= max_center_distance and size_similar
    )
    return location_matches and hash_distance(
        str(current.get("phash", "")), str(exemplar.get("phash", "")),
    ) <= max_hash_distance


def all_detections_match(
    current: list[dict], exemplars: list[dict], **thresholds,
) -> bool:
    """Never suppress an event containing an additional unmatched object."""
    return bool(current and exemplars) and all(
        any(detections_match(item, exemplar, **thresholds) for exemplar in exemplars)
        for item in current
    )
