# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

Basic run (motion detection + YOLO):
```bash
python3 yolo-rtsp-security-cam.py --stream rtsp://ip:port/stream-name --yolo person,dog,cat
```

With live monitor window:
```bash
python3 yolo-rtsp-security-cam.py --stream rtsp://ip:port/stream-name --yolo person,dog,cat --monitor
```

Motion-only mode (no YOLO, very lightweight — suitable for Raspberry Pi):
```bash
python3 yolo-rtsp-security-cam.py --stream rtsp://ip:port/stream-name
```

Testing/threshold-tuning mode (disables recording, prints motion values):
```bash
python3 yolo-rtsp-security-cam.py --stream rtsp://ip:port/stream-name --testing
```

Frame-by-frame debug on a video file:
```bash
python3 yolo-rtsp-security-cam.py --stream /path/to/video.mp4 --frame_click
```

Test camera RTSP URL variations:
```bash
python3 test_camera.py
```

Install dependencies:
```bash
pip3 install -r requirements.txt
```

## Architecture

The entire app lives in `yolo-rtsp-security-cam.py` as a single-file script with a global main loop and four background threads.

**Thread model:**
- `receive_thread` — reads frames from the RTSP stream via OpenCV into a `queue.Queue`. On disconnect, stops any active recording, then polls every 5 seconds to reconnect. Uses `suppress_stdout_stderr` context manager to silence ffmpeg's C-level reconnect errors.
- `keyboard_thread` — listens for `q` keypress via `sshkeyboard` to set `loop = False` and trigger a clean shutdown.
- `timer_thread` — wakes every 2 seconds; prints the current day and date once at midnight (between 00:00:00 and 00:00:02).
- `ffmpeg_thread` (spawned on demand) — runs `python-ffmpeg`'s `FFmpeg.execute()` to copy the raw RTSP stream to a `.mkv` file without re-encoding.

**Main loop logic:**
1. Pull frame from queue; resize to a small resolution (256×144 or 216×162 depending on aspect ratio) and compute a Gaussian-blurred grayscale.
2. Diff against the previous frame → binary threshold → MSE (`ssim_val`) vs a blank frame as the motion score.
3. **Not recording:** accumulate `activity_count` when `ssim_val > thresh`. Once `activity_count >= start_frames`, optionally run `process_yolo()`. Start recording (via a new `ffmpeg_thread`) only when YOLO confirms a target object (or YOLO is off).
4. **Recording:** count frames without motion/object as `activity_count`. Stop ffmpeg when `activity_count >= tail_length`. Optionally auto-delete the file if its total frame count ≈ `tail_length` (false-positive heuristic).

**YOLO integration:** `ultralytics.YOLO` is imported lazily only when `--yolo` is provided. Detection runs on the full-resolution `img` (not the downscaled motion frame). Bounding boxes are drawn onto `img` for the monitor window but the raw stream is what gets recorded by ffmpeg.

**Recording storage:** recordings are saved as `YYYY-MM-DD/HH-MM-SS.mkv`, with the date folder created automatically each day.

**Key globals:** `loop` (shutdown flag), `recording`, `ffmpeg_copy` (the `FFmpeg` object or `0`), `img` (current frame shared between main loop and `process_yolo`), `activity_count`, `yolo_count`.

## Key CLI Arguments

| Argument | Default | Purpose |
|---|---|---|
| `--stream` | required | RTSP URL or video file path |
| `--yolo` | off | Comma-separated COCO object names to detect |
| `--model` | `yolov8n` | YOLOv8 model size (`yolov8n`, `yolov8s`, `yolov8m`, …) |
| `--monitor` | off | Show live OpenCV window |
| `--threshold` | 350 | Motion sensitivity (higher = less sensitive) |
| `--start_frames` | 3 | Consecutive motion frames before triggering |
| `--tail_length` | 8 | Seconds of inactivity before stopping recording |
| `--auto_delete` | off | Delete recordings that are only `tail_length` long |
| `--testing` | off | Print motion values, disable actual recording |
| `--frame_click` | off | Advance frames manually (implies `--testing --monitor`) |

Valid YOLO object names are listed in `coco.names`. The `yolov8n.pt` model file is included in the repo; other model weights are downloaded by `ultralytics` on first use.
