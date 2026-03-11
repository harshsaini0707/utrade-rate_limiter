"""Token Bucket rate limiter.

Each client has a bucket that refills at `rate = max_requests / window_seconds`
tokens per second up to a capacity of `max_requests`.
A request consumes one token; if the bucket is empty the request is rejected.

Refills are computed lazily at request time – no background thread needed.

Data structure: dict[client_id -> {tokens: float, last_refill: float}]
Time complexity : O(1) per request
Space complexity: O(C) where C = number of distinct clients
"""

import time
import threading
from typing import Any, Dict, Optional

from .base import RateLimiter


class TokenBucketLimiter(RateLimiter):
    """Thread-safe Token Bucket rate limiter.

    Compared with Fixed/Sliding Window algorithms this one allows **controlled
    bursting**: a client that has been idle accumulates tokens and can send up
    to ``max_requests`` requests at once.  The long-term average rate is still
    capped at ``max_requests / window_seconds`` req/s.
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

        # {client_id: {"tokens": float, "last_refill": float}}
        self._state: Dict[str, Dict] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def _get_rate(self, client_id: str):
        """Return (capacity, tokens_per_second) for *client_id*."""
        override = self.per_client_limits.get(client_id, {})
        max_req = override.get("max_requests", self.default_max_requests)
        win_sec = override.get("window_seconds", self.default_window_seconds)
        return float(max_req), max_req / win_sec

    # ------------------------------------------------------------------

    def is_allowed(self, client_id: str) -> bool:
        now = time.monotonic()
        capacity, rate = self._get_rate(client_id)

        with self._lock:
            if client_id not in self._state:
                # New client starts with a full bucket
                self._state[client_id] = {"tokens": capacity, "last_refill": now}

            state = self._state[client_id]
            elapsed = now - state["last_refill"]

            # Lazy refill: add tokens proportional to elapsed time
            state["tokens"] = min(capacity, state["tokens"] + elapsed * rate)
            state["last_refill"] = now

            if state["tokens"] >= 1.0:
                state["tokens"] -= 1.0
                return True
            return False

    def get_client_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                cid: {"tokens_remaining": round(s["tokens"], 4)}
                for cid, s in self._state.items()
            }
