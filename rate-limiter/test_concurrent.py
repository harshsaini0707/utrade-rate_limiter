"""Concurrent Rate Limiter Test Harness

Fires 150+ requests (6 clients × 25 each) in parallel for every algorithm
and validates that no per-client allowed count exceeds the configured limit
(demonstrating thread safety / no race conditions).

Usage
-----
    # server must be running first
    python test_concurrent.py [--url http://localhost:8080]
                              [--max-requests 10] [--window-seconds 10]
"""

import argparse
import sys
import time
import threading
from typing import Dict, List

import requests as http

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
BASE_URL = "http://localhost:8080"
NUM_CLIENTS = 6
REQUESTS_PER_CLIENT = 25   # 6 × 25 = 150 requests per algorithm
ALGORITHMS: List[str] = ["fixed_window", "sliding_window", "token_bucket"]


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def fire_requests(
    client_id: str,
    algorithm: str,
    count: int,
    bucket: dict,      # {"ALLOWED": int, "RATE_LIMITED": int, "ERROR": int}
) -> None:
    """Send *count* POST /request calls as fast as possible (no delay)."""
    for _ in range(count):
        try:
            resp = http.post(
                f"{BASE_URL}/request",
                params={"client_id": client_id, "algorithm": algorithm},
                timeout=5,
            )
            result = resp.json().get("result", "ERROR")
        except Exception:
            result = "ERROR"
        bucket[result] = bucket.get(result, 0) + 1


# ---------------------------------------------------------------------------
# Per-algorithm test runner
# ---------------------------------------------------------------------------

def run_test(algorithm: str, max_requests: int, window_seconds: int) -> bool:
    """Run a full concurrent test for one algorithm.  Returns True if clean."""
    sep = "=" * 64
    print(f"\n{sep}")
    print(f"  Algorithm  : {algorithm}")
    print(f"  Limits     : {max_requests} req / {window_seconds}s window")
    print(f"  Clients    : {NUM_CLIENTS}  ×  {REQUESTS_PER_CLIENT} req  "
          f"= {NUM_CLIENTS * REQUESTS_PER_CLIENT} total")
    print(sep)

    # Reset server limits and discard old limiter state
    http.post(
        f"{BASE_URL}/config",
        json={
            "max_requests": max_requests,
            "window_seconds": window_seconds,
            "algorithm": algorithm,
        },
        timeout=5,
    )
    time.sleep(0.05)  # tiny pause so limiters are fully rebuilt

    # Build result buckets and threads
    buckets: Dict[str, dict] = {
        f"client_{i}": {} for i in range(1, NUM_CLIENTS + 1)
    }
    threads = []
    for cid, bucket in buckets.items():
        t = threading.Thread(
            target=fire_requests,
            args=(cid, algorithm, REQUESTS_PER_CLIENT, bucket),
            daemon=True,
        )
        threads.append(t)

    # Launch all threads simultaneously
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0

    # ---- Results -----------------------------------------------------------
    total_allowed = total_rejected = total_errors = 0
    violations = 0

    print(f"\n  Per-client breakdown:")
    for cid, b in sorted(buckets.items()):
        allowed  = b.get("ALLOWED", 0)
        rejected = b.get("RATE_LIMITED", 0)
        errors   = b.get("ERROR", 0)
        total_allowed  += allowed
        total_rejected += rejected
        total_errors   += errors

        # Thread-safety check:
        # Token Bucket starts clients with a full bucket and refills
        # continuously; allow a small margin for tokens accrued during the
        # test run itself.  For Fixed/Sliding Window the bound is exact.
        tolerance = 2 if algorithm == "token_bucket" else 0
        flag = ""
        if allowed > max_requests + tolerance:
            violations += 1
            flag = "  <-- !! RACE CONDITION"
        print(f"    {cid:<12}  allowed={allowed:>3}  "
              f"rejected={rejected:>3}  errors={errors}{flag}")

    print(
        f"\n  TOTALS : allowed={total_allowed}  rejected={total_rejected}  "
        f"errors={total_errors}  elapsed={elapsed:.2f}s"
    )

    if violations == 0:
        print("  \u2713 No race conditions detected (all per-client counts within limits)")
    else:
        print(f"  !! {violations} RACE CONDITION(S) DETECTED")

    return violations == 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Concurrent test harness for the rate limiter server"
    )
    parser.add_argument("--url", default=BASE_URL, help="Server base URL")
    parser.add_argument("--max-requests", type=int, default=10)
    parser.add_argument("--window-seconds", type=int, default=10)
    args = parser.parse_args()

    global BASE_URL
    BASE_URL = args.url

    # Sanity-check: server must be reachable
    try:
        r = http.get(f"{BASE_URL}/stats", timeout=3)
        r.raise_for_status()
    except Exception as exc:
        print(f"ERROR: Cannot reach server at {BASE_URL}  ({exc})")
        print("Start the server first:  python server.py")
        sys.exit(1)

    all_clean = True
    for algo in ALGORITHMS:
        clean = run_test(algo, args.max_requests, args.window_seconds)
        all_clean = all_clean and clean

    # ---- Final server-side statistics -------------------------------------
    print(f"\n{'=' * 64}")
    print("  Final server-side statistics (cumulative across all tests)")
    print(f"{'=' * 64}")
    stats = http.get(f"{BASE_URL}/stats", timeout=5).json()
    print(f"  Total    : {stats['total']}")
    print(f"  Allowed  : {stats['allowed']}")
    print(f"  Rejected : {stats['rejected']}")
    print("\n  Per-client breakdown (cumulative):")
    for cid, pc in sorted(stats["per_client"].items()):
        print(f"    {cid:<12}  allowed={pc['allowed']:>4}  rejected={pc['rejected']:>4}")

    print()
    if all_clean:
        print("  ALL TESTS PASSED – rate limiter is thread-safe.")
        sys.exit(0)
    else:
        print("  SOME TESTS FAILED – review output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
