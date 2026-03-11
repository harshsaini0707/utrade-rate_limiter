"""Algorithm registry – import from here to get all rate-limiter classes."""

from .fixed_window import FixedWindowLimiter
from .sliding_window import SlidingWindowLogLimiter
from .token_bucket import TokenBucketLimiter

ALGORITHM_MAP = {
    "fixed_window": FixedWindowLimiter,
    "sliding_window": SlidingWindowLogLimiter,
    "token_bucket": TokenBucketLimiter,
}

__all__ = [
    "FixedWindowLimiter",
    "SlidingWindowLogLimiter",
    "TokenBucketLimiter",
    "ALGORITHM_MAP",
]
