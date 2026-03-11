"""Abstract base class for all rate-limiting algorithms."""

from abc import ABC, abstractmethod
from typing import Any, Dict


class RateLimiter(ABC):
    """Common interface every rate-limiter strategy must implement."""

    @abstractmethod
    def is_allowed(self, client_id: str) -> bool:
        """Return True if the request from *client_id* should be allowed."""

    @abstractmethod
    def get_client_stats(self) -> Dict[str, Any]:
        """Return a snapshot of per-client internal state (for debugging)."""
