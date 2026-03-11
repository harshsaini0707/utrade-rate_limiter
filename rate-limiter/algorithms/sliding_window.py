"""Sliding Window Log rate limiter.

Maintains a deque of request timestamps per client.
Before each check, timestamps older than (now - window_seconds) are evicted.

Data structure: dict[client_id -> collections.deque[float]]
Time complexity : O(max_requests) per request (eviction loop, bounded by limit)
Space complexity: O(C × max_requests) – at most `max_requests` stamps kept per client
"""

import time
import threading
from collections import deque
from typing import Any, Dict, Optional

from .base import RateLimiter


class SlidingWindowLogLimiter(RateLimiter):
    """Thread-safe Sliding Window Log rate limiter.

    Unlike Fixed Window, this algorithm never allows a boundary burst:
    the effective window always ends at *now*, so the rate is accurate to
    the millisecond.  Memory is bounded because only the last
    ``max_requests`` timestamps are ever retained.
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: int,
        per_client_limits: Optional[Dict] = None,
    ) -> None:
        self.default_max_requests = max_requests
        self.default_window_seconds = window_seconds
        self.per_client_limits: Dict = per_client_limits or {}

        # {client_id: deque of monotonic timestamps (oldest first)}
        self._logs: Dict[str, deque] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def _get_limit(self, client_id: str):
        override = self.per_client_limits.get(client_id, {})
        max_req = override.get("max_requests", self.default_max_requests)
        win_sec = override.get("window_seconds", self.default_window_seconds)
        return max_req, win_sec

    # ------------------------------------------------------------------

    def is_allowed(self, client_id: str) -> bool:
        now = time.monotonic()
        max_req, win_sec = self._get_limit(client_id)
        cutoff = now - win_sec

        with self._lock:
            if client_id not in self._logs:
                self._logs[client_id] = deque()

            log = self._logs[client_id]

            # Evict timestamps outside the sliding window
          
            while log and log[0] > cutoff:
                log.popleft()

            if len(log) <= max_req:
                log.append(now)
                return True
            return False

    def get_client_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                cid: {"requests_in_window": len(log)}
                for cid, log in self._logs.items()
            }
