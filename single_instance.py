"""Cross-platform process singleton locks released automatically on process exit."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


class SingleInstanceLock:
    def __init__(self, name: str):
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "app"
        self.name = safe_name
        self._handle = None
        self._file = None

    def acquire(self) -> bool:
        if self._handle is not None or self._file is not None:
            return True
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            handle = kernel32.CreateMutexW(None, False, f"Local\\{self.name}")
            if not handle:
                raise ctypes.WinError()
            if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                return False
            self._handle = handle
            return True

        import fcntl

        path = Path(tempfile.gettempdir()) / f"{self.name}.lock"
        lock_file = path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            return False
        self._file = lock_file
        return True

    def release(self) -> None:
        if os.name == "nt" and self._handle is not None:
            import ctypes

            ctypes.windll.kernel32.ReleaseMutex(self._handle)
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None
        elif self._file is not None:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"Another {self.name} instance is already running")
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.release()
