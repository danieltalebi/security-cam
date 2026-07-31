"""Inference wrapper for the locally trained public-area segmentation model."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import torch

from train_public_area import TinyUNet


class PublicAreaClassifier:
    def __init__(self, model_path: str, ptz_bounds: tuple[int, int, int, int], device: str = "auto"):
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
        self.device = torch.device(device)
        checkpoint = torch.load(Path(model_path), map_location=self.device)
        self.model = TinyUNet(int(checkpoint.get("base_channels", 16))).to(self.device)
        self.model.load_state_dict(checkpoint["state_dict"])
        self.model.eval()
        self.image_size = int(checkpoint.get("image_size", 384))
        self.ptz_bounds = ptz_bounds
        self.probability: np.ndarray | None = None

    def predict(self, frame: np.ndarray) -> np.ndarray:
        x1, y1, x2, y2 = self.ptz_bounds
        ptz = frame[y1:y2, x1:x2]
        if ptz.size == 0:
            raise RuntimeError("PTZ bounds are outside the received RTSP frame")
        rgb = cv2.cvtColor(ptz, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, (self.image_size, self.image_size), interpolation=cv2.INTER_AREA)
        tensor = torch.from_numpy(resized.transpose(2, 0, 1)).unsqueeze(0).float().to(self.device) / 255.0
        with torch.no_grad():
            probability = torch.sigmoid(self.model(tensor))[0, 0].cpu().numpy()
        self.probability = cv2.resize(probability, (ptz.shape[1], ptz.shape[0]), interpolation=cv2.INTER_LINEAR)
        return self.probability

    def probability_at(self, point: tuple[float, float]) -> float | None:
        if self.probability is None:
            return None
        x1, y1, x2, y2 = self.ptz_bounds
        x, y = round(point[0]) - x1, round(point[1]) - y1
        if not (0 <= x < x2 - x1 and 0 <= y < y2 - y1):
            return None
        return float(self.probability[y, x])
