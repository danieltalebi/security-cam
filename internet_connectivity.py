"""Internet outage detection with a recovery notification through Telegram."""

from __future__ import annotations

import threading
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from telegram_notify import format_duration


def internet_reachable(timeout: float = 5.0) -> bool:
    """Use independent HTTPS endpoints and consider either one sufficient."""
    urls = (
        "https://connectivitycheck.gstatic.com/generate_204",
        "https://www.cloudflare.com/cdn-cgi/trace",
    )
    for url in urls:
        try:
            with urlopen(Request(url, headers={"User-Agent": "security-cam-health/1.0"}), timeout=timeout):
                return True
        except HTTPError:
            # An HTTP response still proves DNS, TLS routing, and Internet access.
            return True
        except (URLError, TimeoutError, OSError):
            continue
    return False


class InternetConnectivityMonitor:
    """Log Internet state changes and notify as soon as a connection returns."""

    def __init__(
        self,
        notify: Callable[[str], object],
        interval_seconds: float = 30,
        timeout_seconds: float = 5,
        failure_threshold: int = 2,
        probe: Callable[[], bool] | None = None,
    ):
        self.notify = notify
        self.interval = max(5.0, float(interval_seconds))
        self.failure_threshold = max(1, int(failure_threshold))
        self.probe = probe or (lambda: internet_reachable(float(timeout_seconds)))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.online: bool | None = None
        self.consecutive_failures = 0
        self.first_failure_at: float | None = None

    def start(self) -> "InternetConnectivityMonitor":
        self.thread = threading.Thread(target=self._run, name="internet-connectivity", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=self.interval + 1)

    def check_once(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        reachable = self.probe()
        if reachable:
            self.consecutive_failures = 0
            if self.online is False and self.first_failure_at is not None:
                duration = now - self.first_failure_at
                message = f"✅ Internet connection restored after {format_duration(duration)} offline."
                print(message)
                try:
                    self.notify(message)
                except Exception as error:
                    print(f"Telegram Internet recovery alert failed: {type(error).__name__}: {error}")
            elif self.online is None:
                print("Internet connectivity initialized: online")
            self.online = True
            self.first_failure_at = None
            return True

        if self.first_failure_at is None:
            self.first_failure_at = now
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold and self.online is not False:
            self.online = False
            print("Internet connection is offline; recovery duration tracking started")
        return False

    def _run(self) -> None:
        while not self.stop_event.is_set():
            self.check_once()
            self.stop_event.wait(self.interval)

