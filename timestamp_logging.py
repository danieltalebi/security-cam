"""Prefix process stdout and stderr lines with a local timestamp."""

from __future__ import annotations

import sys
import threading
from datetime import datetime
from typing import TextIO


class TimestampedStream:
    def __init__(self, stream: TextIO):
        self.stream = stream
        self.at_line_start = True
        self.lock = threading.Lock()

    def write(self, text: str) -> int:
        if not text:
            return 0
        with self.lock:
            for piece in text.splitlines(keepends=True):
                if self.at_line_start and piece not in {"\n", "\r\n"}:
                    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
                    self.stream.write(f"[{stamp}] ")
                self.stream.write(piece)
                self.at_line_start = piece.endswith(("\n", "\r"))
            self.stream.flush()
        return len(text)

    def flush(self) -> None:
        self.stream.flush()

    def __getattr__(self, name: str):
        return getattr(self.stream, name)


def install_timestamped_logging() -> None:
    if not isinstance(sys.stdout, TimestampedStream):
        sys.stdout = TimestampedStream(sys.stdout)
    if not isinstance(sys.stderr, TimestampedStream):
        sys.stderr = TimestampedStream(sys.stderr)
