"""Failed-login limiter for ``POST /api/v1/auth/login``.

Counts *failed* logins in a sliding window, per e-mail and per client address. Once
either count reaches its limit, further attempts get 429 with ``Retry-After`` before the
password is even checked; a successful login clears the e-mail's count (not the
address's). Settings, read on every call so a restart is not needed to change them:

* ``AIS_LOGIN_MAX_FAILURES`` (default 5) per e-mail,
* ``AIS_LOGIN_MAX_FAILURES_PER_IP`` (default 20) per client address,
* ``AIS_LOGIN_WINDOW_SECONDS`` (default 300); ``0`` for either limit turns it off.

The state is in process memory: each worker counts on its own and a restart forgets.
The client address is the socket peer (``request.client.host``); behind a proxy that is
the proxy, so run uvicorn with ``--proxy-headers`` and a trusted ``--forwarded-allow-ips``
if the per-address limit should see real clients.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque

_MAX_KEYS = 10_000  # sweep expired keys once the table grows past this


def _setting(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


class FailureLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, str], deque[float]] = {}

    @staticmethod
    def _limits() -> dict[str, int]:
        return {
            "email": _setting("AIS_LOGIN_MAX_FAILURES", 5),
            "ip": _setting("AIS_LOGIN_MAX_FAILURES_PER_IP", 20),
        }

    @staticmethod
    def _window() -> int:
        return _setting("AIS_LOGIN_WINDOW_SECONDS", 300)

    def _prune(self, key: tuple[str, str], now: float, window: int) -> deque[float]:
        hits = self._hits.get(key)
        if hits is None:
            return deque()
        while hits and hits[0] <= now - window:
            hits.popleft()
        if not hits:
            del self._hits[key]
        return hits

    def retry_after(self, email: str, ip: str) -> int | None:
        """Seconds until the next attempt is allowed, or None when it is allowed now."""
        now, window, limits = time.monotonic(), self._window(), self._limits()
        wait = 0.0
        with self._lock:
            for kind, value in (("email", email), ("ip", ip)):
                limit = limits[kind]
                if not limit or not window:
                    continue
                hits = self._prune((kind, value), now, window)
                if len(hits) >= limit:
                    wait = max(wait, hits[len(hits) - limit] + window - now)
        return max(1, int(wait + 0.999)) if wait > 0 else None

    def failure(self, email: str, ip: str) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > _MAX_KEYS:
                window = self._window()
                for key in list(self._hits):
                    self._prune(key, now, window)
            for key in (("email", email), ("ip", ip)):
                self._hits.setdefault(key, deque()).append(now)

    def success(self, email: str) -> None:
        with self._lock:
            self._hits.pop(("email", email), None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


login_limiter = FailureLimiter()
