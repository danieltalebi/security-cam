"""Local polygon labeler for property-vs-public-area training data.

The tool never uploads video. It saves the selected source frames, a PNG mask,
and editable polygon annotations on the local disk.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import cv2
import numpy as np


PUBLIC_AREA = 1
CLASS_NAME = "public_area"
CLASS_COLOR = (0, 165, 255)  # BGR


def rasterize_mask(shape: tuple[int, int], annotations: list[dict]) -> np.ndarray:
    """Build a public-area mask; 0 is non-public and 1 is public."""
    mask = np.zeros(shape, dtype=np.uint8)
    for annotation in annotations:
        points = np.array(annotation["points"], dtype=np.int32)
        if len(points) >= 3:
            cv2.fillPoly(mask, [points], PUBLIC_AREA)
    return mask


class PropertyVideoLabeler:
    def __init__(self, video_path: Path, output_dir: Path, step_seconds: float, crop: tuple[int, int, int, int] | None):
        self.video_path = video_path
        self.output_dir = output_dir
        self.capture = cv2.VideoCapture(str(video_path))
        if not self.capture.isOpened():
            raise RuntimeError(f"Could not open video: {video_path}")

        self.fps = self.capture.get(cv2.CAP_PROP_FPS) or 25.0
        self.frame_count = int(self.capture.get(cv2.CAP_PROP_FRAME_COUNT))
        self.step_frames = max(1, round(step_seconds * self.fps))
        self.crop = crop
        self.frame_index = 0
        self.frame: np.ndarray | None = None
        self.annotations: list[dict] = []
        self.active_points: list[list[int]] = []
        self.window_name = "Property video labeler"

        for folder in ("images", "masks", "annotations"):
            (output_dir / folder).mkdir(parents=True, exist_ok=True)

    def load_frame(self, index: int) -> bool:
        index = max(0, min(max(0, self.frame_count - 1), index))
        self.capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = self.capture.read()
        if not ok:
            return False
        self.frame_index = index
        if self.crop:
            x1, y1, x2, y2 = self.crop
            height, width = frame.shape[:2]
            if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
                raise RuntimeError(
                    f"Crop {self.crop} is outside this video's {width}x{height} frame."
                )
            frame = frame[y1:y2, x1:x2]
        self.frame = frame
        # Annotations are a working template. They deliberately remain in
        # place while browsing a stable PTZ view, and only change when the
        # user edits or clears them.
        self.active_points = []
        return True

    def add_polygon(self) -> None:
        if len(self.active_points) < 3:
            print("A polygon needs at least three points.")
            return
        self.annotations.append({"class_id": PUBLIC_AREA, "points": self.active_points[:]})
        self.active_points = []

    def save(self) -> None:
        if self.frame is None:
            return
        if self.active_points:
            self.add_polygon()
        if not self.annotations:
            print("Nothing saved: draw at least one public-area polygon first.")
            return

        # Include the source name so frame 0 from a second video cannot
        # overwrite frame 0 from an earlier labeling session.
        source_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", self.video_path.stem).strip("_") or "video"
        stem = f"{source_stem}_frame_{self.frame_index:08d}"
        image_path = self.output_dir / "images" / f"{stem}.jpg"
        mask_path = self.output_dir / "masks" / f"{stem}.png"
        annotation_path = self.output_dir / "annotations" / f"{stem}.json"
        mask = rasterize_mask(self.frame.shape[:2], self.annotations)

        if not cv2.imwrite(str(image_path), self.frame, [cv2.IMWRITE_JPEG_QUALITY, 95]):
            raise RuntimeError(f"Could not write {image_path}")
        if not cv2.imwrite(str(mask_path), mask):
            raise RuntimeError(f"Could not write {mask_path}")
        annotation = {
            "source_video": self.video_path.name,
            "frame_index": self.frame_index,
            "timestamp_seconds": round(self.frame_index / self.fps, 3),
            "resolution": [int(self.frame.shape[1]), int(self.frame.shape[0])],
            "crop_in_source": list(self.crop) if self.crop else None,
            "class_mapping": {"0": "not_public", "1": "public_area"},
            "annotations": self.annotations,
        }
        annotation_path.write_text(json.dumps(annotation, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved {image_path.name}: {len(self.annotations)} polygons")

    def on_mouse(self, event: int, x: int, y: int, _flags: int, _userdata) -> None:
        if event == cv2.EVENT_LBUTTONDOWN:
            self.active_points.append([x, y])
        elif event == cv2.EVENT_RBUTTONDOWN and self.active_points:
            self.active_points.pop()

    def render(self) -> np.ndarray:
        assert self.frame is not None
        canvas = self.frame.copy()
        for index, annotation in enumerate(self.annotations, start=1):
            points = np.array(annotation["points"], dtype=np.int32)
            color = CLASS_COLOR
            cv2.polylines(canvas, [points], True, color, 2)
            cv2.putText(canvas, f"{index}: {CLASS_NAME}", tuple(points[0]), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
        if self.active_points:
            points = np.array(self.active_points, dtype=np.int32)
            cv2.polylines(canvas, [points], False, CLASS_COLOR, 2)
            for point in self.active_points:
                cv2.circle(canvas, tuple(point), 4, CLASS_COLOR, -1)

        total = max(1, self.frame_count - 1)
        header = f"frame {self.frame_index}/{total}  t={self.frame_index / self.fps:.1f}s  mark public area only"
        footer = "click public-area points | Enter commit | right-click undo | A/D frame | J/L jump | S save | U clear | Q quit"
        cv2.rectangle(canvas, (0, 0), (canvas.shape[1], 58), (0, 0, 0), -1)
        cv2.putText(canvas, header, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
        cv2.putText(canvas, footer, (12, 49), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        return canvas

    def run(self) -> None:
        if not self.load_frame(0):
            raise RuntimeError("The video has no readable frames.")
        cv2.namedWindow(self.window_name, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
        cv2.setMouseCallback(self.window_name, self.on_mouse)
        print("Labeler ready. Press H in the video window for keyboard help.")

        while True:
            cv2.imshow(self.window_name, self.render())
            key = cv2.waitKey(20) & 0xFF
            if key in (ord("q"), 27):
                break
            if key in (13, 10):
                self.add_polygon()
            elif key == ord("s"):
                self.save()
            elif key == ord("u"):
                self.annotations = []
                self.active_points = []
            elif key in (ord("a"), 81):
                self.load_frame(self.frame_index - 1)
            elif key in (ord("d"), 83):
                self.load_frame(self.frame_index + 1)
            elif key == ord("j"):
                self.load_frame(self.frame_index - self.step_frames)
            elif key == ord("l"):
                self.load_frame(self.frame_index + self.step_frames)
            elif key == ord("h"):
                print("left-click=public-area point, Enter=commit polygon, right-click=undo point, U=clear frame, A/D=one frame, J/L=jump, S=save, Q=quit")

        self.capture.release()
        cv2.destroyAllWindows()


def parse_crop(value: str) -> tuple[int, int, int, int]:
    try:
        crop = tuple(int(part.strip()) for part in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError("Crop must be x1,y1,x2,y2") from error
    if len(crop) != 4 or crop[0] >= crop[2] or crop[1] >= crop[3]:
        raise argparse.ArgumentTypeError("Crop must be x1,y1,x2,y2 with x2>x1 and y2>y1")
    return crop


def main() -> None:
    parser = argparse.ArgumentParser(description="Label public-area polygons in a local video.")
    parser.add_argument("--video", required=True, help="Path to a local video file.")
    parser.add_argument("--output_dir", default="dataset/labels", help="Where images, masks, and editable annotations are saved.")
    parser.add_argument("--step_seconds", type=float, default=2.0, help="Jump interval for the J/L keys.")
    parser.add_argument("--crop", type=parse_crop, default=None, help="Optional source crop x1,y1,x2,y2. Use this to label only one lens in a combined stream.")
    args = parser.parse_args()
    if args.step_seconds <= 0:
        parser.error("--step_seconds must be greater than zero")
    PropertyVideoLabeler(Path(args.video), Path(args.output_dir), args.step_seconds, args.crop).run()


if __name__ == "__main__":
    main()
