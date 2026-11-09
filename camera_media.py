"""On-demand Telegram photos and short, directly playable camera clips."""

from __future__ import annotations

import subprocess
import tempfile
import threading
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from onvif_snapshot import download_snapshot, xmeye_snapshot_uri


@dataclass(frozen=True)
class CameraMediaSource:
    name: str
    snapshot_url: str
    username: str
    password: str
    query_auth: bool
    stream: str | None
    lens: tuple[int, int, int, int] | None = None
    layout_size: tuple[int, int] | None = None


def _crop_lens(frame: np.ndarray, source: CameraMediaSource) -> np.ndarray:
    if source.lens is None or source.layout_size is None:
        return np.ascontiguousarray(frame)
    x1, y1, x2, y2 = source.lens
    layout_width, layout_height = source.layout_size
    height, width = frame.shape[:2]
    left = round(x1 * width / layout_width)
    top = round(y1 * height / layout_height)
    right = round(x2 * width / layout_width)
    bottom = round(y2 * height / layout_height)
    cropped = frame[top:bottom, left:right]
    if cropped.size == 0:
        raise RuntimeError("the configured camera lens crop is empty")
    return np.ascontiguousarray(cropped)


def normalized_snapshot(source: CameraMediaSource, timeout: float = 8) -> bytes:
    """Decode the vendor JPEG, select one lens, and emit a standard JPEG."""
    uri = (
        xmeye_snapshot_uri(source.snapshot_url, source.username, source.password)
        if source.query_auth else source.snapshot_url
    )
    data = download_snapshot(uri, source.username, source.password, timeout)
    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError("camera returned an unreadable JPEG")
    frame = _crop_lens(frame, source)
    max_width = 1920
    if frame.shape[1] > max_width:
        scale = max_width / frame.shape[1]
        frame = cv2.resize(
            frame,
            (max_width, max(1, round(frame.shape[0] * scale))),
            interpolation=cv2.INTER_AREA,
        )
    ok, encoded = cv2.imencode(
        ".jpg", np.ascontiguousarray(frame),
        [cv2.IMWRITE_JPEG_QUALITY, 90, cv2.IMWRITE_JPEG_OPTIMIZE, 1],
    )
    if not ok:
        raise RuntimeError("could not encode a normalized JPEG")
    return encoded.tobytes()


def _video_filter(source: CameraMediaSource) -> str:
    filters = []
    if source.lens is not None and source.layout_size is not None:
        x1, y1, x2, y2 = source.lens
        layout_width, layout_height = source.layout_size
        filters.append(
            "crop="
            f"iw*{x2 - x1}/{layout_width}:ih*{y2 - y1}/{layout_height}:"
            f"iw*{x1}/{layout_width}:ih*{y1}/{layout_height}"
        )
    filters.extend((
        "scale='min(1920,iw)':'min(1920,ih)':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2",
        "fps=15",
        "setpts=PTS-STARTPTS",
    ))
    return ",".join(filters)


def record_video(
    source: CameraMediaSource,
    output: Path,
    seconds: float = 5,
    start_barrier: threading.Barrier | None = None,
) -> Path:
    if start_barrier is not None:
        start_barrier.wait(timeout=5)
    if not source.stream:
        raise RuntimeError("RTSP stream is not configured")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-rtsp_transport", "tcp", "-timeout", "5000000",
        "-use_wallclock_as_timestamps", "1", "-fflags", "+genpts+discardcorrupt",
        "-i", source.stream, "-t", str(seconds),
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-vf", _video_filter(source),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-pix_fmt", "yuv420p", "-r", "15", "-movflags", "+faststart",
        str(output),
    ]
    completed = subprocess.run(
        command, capture_output=True, timeout=max(20, seconds + 15), check=False,
    )
    if completed.returncode or not output.is_file() or output.stat().st_size == 0:
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = detail[-1] if detail else f"ffmpeg exited with {completed.returncode}"
        raise RuntimeError(reason)
    return output


class CameraMediaCommands:
    """Run camera requests off the Telegram polling and monitor threads."""

    def __init__(self, sources: list[CameraMediaSource], notifier):
        self.sources = sources
        self.notifier = notifier
        self._busy = threading.Lock()

    def handle(self, command: str) -> str | None:
        normalized = command.strip().lower().split("@", 1)[0]
        if normalized not in {"photo", "/photo", "video", "/video"}:
            return None
        if not self._busy.acquire(blocking=False):
            return "Ya hay una captura de cámaras en curso. Intente nuevamente en unos segundos."
        kind = "photo" if "photo" in normalized else "video"
        threading.Thread(
            target=self._run, args=(kind,), name=f"telegram-{kind}", daemon=True,
        ).start()
        return (
            "Tomando fotos de Garage y Backyard…"
            if kind == "photo" else
            "Grabando 5 segundos de Garage y Backyard…"
        )

    def _run(self, kind: str) -> None:
        try:
            if kind == "photo":
                self._photos()
            else:
                self._videos()
        except Exception as error:
            try:
                self.notifier.send_message(
                    f"No se pudo completar /{kind}: {type(error).__name__}: {error}"
                )
            except Exception as notify_error:
                print(
                    f"Telegram /{kind} failure could not be reported: "
                    f"{type(notify_error).__name__}: {notify_error}"
                )
        finally:
            self._busy.release()

    def _photos(self) -> None:
        results: dict[str, bytes] = {}
        failures: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=len(self.sources)) as executor:
            pending = {
                executor.submit(normalized_snapshot, source): source
                for source in self.sources
            }
            for future in as_completed(pending):
                source = pending[future]
                try:
                    results[source.name] = future.result()
                except Exception as error:
                    failures[source.name] = self._safe_error(source, error)
        for source in self.sources:
            image = results.get(source.name)
            if image is not None:
                self.notifier.send_photo(image, f"📷 {source.name}")
        self._report_failures("foto", failures)

    def _videos(self) -> None:
        failures: dict[str, str] = {}
        with tempfile.TemporaryDirectory(prefix="security-cam-telegram-") as folder:
            root = Path(folder)
            barrier = threading.Barrier(len(self.sources))
            with ThreadPoolExecutor(max_workers=len(self.sources)) as executor:
                pending = {
                    executor.submit(
                        record_video, source, root / f"{source.name}.mp4", 5, barrier,
                    ): source
                    for source in self.sources
                }
                completed: dict[str, Path] = {}
                for future in as_completed(pending):
                    source = pending[future]
                    try:
                        completed[source.name] = future.result()
                    except Exception as error:
                        failures[source.name] = self._safe_error(source, error)
            for source in self.sources:
                path = completed.get(source.name)
                if path is not None:
                    self.notifier.send_video(
                        path.read_bytes(), f"🎥 {source.name} · 5 segundos",
                        filename=f"{source.name}.mp4",
                    )
        self._report_failures("video", failures)

    def _report_failures(self, kind: str, failures: dict[str, str]) -> None:
        if not failures:
            return
        details = "; ".join(f"{name}: {reason}" for name, reason in failures.items())
        self.notifier.send_message(
            f"⚠️ Falló la captura de {kind} para: {details}. "
            "Los resultados disponibles de las otras cámaras sí fueron enviados."
        )

    @staticmethod
    def _safe_error(source: CameraMediaSource, error: Exception) -> str:
        text = f"{type(error).__name__}: {error}"
        for secret in (source.username, source.password):
            if secret:
                text = text.replace(secret, "<redacted>")
        return re.sub(r"(?:rtsp|https?)://\S+", "<camera endpoint>", text)


def chain_command_handlers(
    media: CameraMediaCommands,
    fallback: Callable[[str], str | None],
) -> Callable[[str], str | None]:
    def handle(command: str) -> str | None:
        response = media.handle(command)
        return response if response is not None else fallback(command)

    return handle
