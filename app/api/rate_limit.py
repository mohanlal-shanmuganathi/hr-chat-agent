"""Per-employee sliding-window rate limit (in-process).

Good for a single instance; with several replicas this moves to Redis so the window is shared.
"""

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self._limit = limit
        self._window = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True
