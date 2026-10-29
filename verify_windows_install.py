"""Read-only preflight check used by the Windows installer."""

from __future__ import annotations

import argparse
import importlib
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEPENDENCIES = (
    "cv2",
    "numpy",
    "torch",
    "ultralytics",
    "ffmpeg",
    "skimage",
    "sshkeyboard",
    "onvif",
    "dvrip",
    "paho.mqtt.client",
)
APPLICATION_MODULES = (
    "camera_config",
    "dvrip_events",
    "dvrip_smart_telegram",
    "multi_camera_monitor",
    "onvif_snapshot",
    "telegram_notify",
)


def check_imports(names: tuple[str, ...]) -> None:
    for name in names:
        importlib.import_module(name)
        print(f"  OK import {name}")


def check_ffmpeg() -> None:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg was not found in PATH")
    result = subprocess.run(
        [executable, "-version"], capture_output=True, text=True, timeout=10,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"ffmpeg exited with code {result.returncode}")
    first_line = (result.stdout or result.stderr).splitlines()[0]
    print(f"  OK {first_line}")


def check_model() -> None:
    import numpy as np
    from ultralytics import YOLO

    model_path = ROOT / "yolov8n.pt"
    model = YOLO(str(model_path))
    if not model_path.is_file():
        raise RuntimeError(f"YOLO did not create {model_path}")
    result = model.predict(
        np.zeros((64, 64, 3), dtype=np.uint8), imgsz=64, verbose=False,
    )
    if len(result) != 1:
        raise RuntimeError("YOLO smoke inference did not return one result")
    print(f"  OK YOLO model {model_path.name} and smoke inference")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the security camera Windows installation.")
    parser.add_argument(
        "--model", action="store_true",
        help="Load yolov8n.pt, downloading it on a clean installation.",
    )
    args = parser.parse_args()

    print(f"Python {platform.python_version()} ({platform.architecture()[0]})")
    if sys.maxsize <= 2**32:
        raise RuntimeError("64-bit Python is required by PyTorch")
    if sys.version_info[:2] != (3, 11):
        print("  WARNING: the tested Windows installer uses Python 3.11")

    check_imports(DEPENDENCIES)
    check_imports(APPLICATION_MODULES)
    check_ffmpeg()
    if args.model:
        check_model()

    import torch

    accelerator = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
    print(f"  OK PyTorch {torch.__version__}; inference device: {accelerator}")
    print("Windows installation preflight passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"INSTALLATION CHECK FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
