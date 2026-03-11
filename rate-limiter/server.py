"""Rate Limiter HTTP Server

Endpoints
---------
POST /request?client_id=<id>[&algorithm=<algo>]
    Check whether a request is allowed.
    Response: {timestamp, client_id, algorithm, result: ALLOWED|RATE_LIMITED}

GET  /stats
    Cumulative totals since server start.

GET  /config
    Current active configuration.

POST /config   (JSON body, all fields optional)
    Hot-update configuration without restart.
    Accepted keys: max_requests, window_seconds, algorithm, per_client_limits

Usage
-----
    python server.py [--config config.json] [--max-requests N]
                     [--window-seconds T] [--algorithm ALGO]
                     [--host HOST] [--port PORT]
"""

import argparse
import json
import logging
import os
import threading
import time

from flask import Flask, jsonify, request

from algorithms import ALGORITHM_MAP

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)
# Keep Werkzeug quieter – we handle our own request logging
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)

# ---------------------------------------------------------------------------
# Shared state (all protected by their respective locks)
# ---------------------------------------------------------------------------
_config_lock = threading.Lock()
_config: dict = {}

_limiters_lock = threading.Lock()
_limiters: dict = {}  # algorithm_name -> RateLimiter instance

_stats_lock = threading.Lock()
_stats: dict = {
    "total": 0,
    "allowed": 0,
    "rejected": 0,
    "per_client": {},
}

# Graceful degradation: cap concurrent in-flight requests to avoid
# unbounded thread piling under extreme load.
_MAX_CONCURRENT = 500
_semaphore = threading.BoundedSemaphore(_MAX_CONCURRENT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_limiter(algorithm: str):
    """Construct a fresh limiter for *algorithm* using the current config."""
    with _config_lock:
        cfg = dict(_config)
    cls = ALGORITHM_MAP.get(algorithm)
    if cls is None:
        raise ValueError(
            f"Unknown algorithm '{algorithm}'. Valid: {list(ALGORITHM_MAP)}"
        )
    return cls(
        max_requests=cfg["max_requests"],
        window_seconds=cfg["window_seconds"],
        per_client_limits=cfg.get("per_client_limits", {}),
    )


def _get_limiter(algorithm: str):
    """Return the cached limiter for *algorithm*, creating it if necessary."""
    with _limiters_lock:
        if algorithm not in _limiters:
            _limiters[algorithm] = _build_limiter(algorithm)
        return _limiters[algorithm]


def _record_result(client_id: str, allowed: bool) -> None:
    with _stats_lock:
        _stats["total"] += 1
        if allowed:
            _stats["allowed"] += 1
        else:
            _stats["rejected"] += 1
        pc = _stats["per_client"].setdefault(
            client_id, {"allowed": 0, "rejected": 0}
        )
        if allowed:
            pc["allowed"] += 1
        else:
            pc["rejected"] += 1


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/request", methods=["POST"])
def handle_request():
    """Evaluate a rate-limit decision for a given client."""
    # Graceful degradation: fail fast when the server is saturated
    if not _semaphore.acquire(blocking=False):
        return jsonify({"error": "server overloaded", "result": "RATE_LIMITED"}), 503

    try:
        client_id = request.args.get("client_id", "").strip()
        if not client_id:
            return jsonify({"error": "client_id query parameter is required"}), 400

        with _config_lock:
            default_algo = _config.get("algorithm", "fixed_window")
        algorithm = request.args.get("algorithm", default_algo)

        try:
            limiter = _get_limiter(algorithm)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        allowed = limiter.is_allowed(client_id)
        result = "ALLOWED" if allowed else "RATE_LIMITED"
        ts = time.time()

        _record_result(client_id, allowed)
        log.info(
            "[%.3f]  client=%-14s  algo=%-14s  %s",
            ts, client_id, algorithm, result,
        )

        return jsonify(
            {
                "timestamp": ts,
                "client_id": client_id,
                "algorithm": algorithm,
                "result": result,
            }
        )
    finally:
        _semaphore.release()


@app.route("/stats", methods=["GET"])
def get_stats():
    """Return cumulative request statistics."""
    with _stats_lock:
        # Return a deep copy so the snapshot is consistent
        snapshot = json.loads(json.dumps(_stats))
    return jsonify(snapshot)


@app.route("/config", methods=["GET"])
def get_config():
    """Return the current active configuration."""
    with _config_lock:
        return jsonify(dict(_config))


@app.route("/config", methods=["POST"])
def update_config():
    """Hot-update configuration without restarting the server.

    Accepts a partial JSON body; only supplied keys are updated.
    Existing limiter instances are discarded so the next request picks up
    the new parameters.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON body is required"}), 400

    valid_keys = {"max_requests", "window_seconds", "algorithm", "per_client_limits"}
    unknown = set(data) - valid_keys
    if unknown:
        return jsonify({"error": f"Unknown configuration keys: {sorted(unknown)}"}), 400

    with _config_lock:
        if "max_requests" in data:
            if not isinstance(data["max_requests"], int) or data["max_requests"] < 1:
                return jsonify({"error": "max_requests must be a positive integer"}), 400
            _config["max_requests"] = data["max_requests"]

        if "window_seconds" in data:
            if not isinstance(data["window_seconds"], (int, float)) or data["window_seconds"] <= 0:
                return jsonify({"error": "window_seconds must be a positive number"}), 400
            _config["window_seconds"] = data["window_seconds"]

        if "algorithm" in data:
            if data["algorithm"] not in ALGORITHM_MAP:
                return jsonify({"error": f"Unknown algorithm: {data['algorithm']}"}), 400
            _config["algorithm"] = data["algorithm"]

        if "per_client_limits" in data:
            if not isinstance(data["per_client_limits"], dict):
                return jsonify({"error": "per_client_limits must be an object"}), 400
            _config.setdefault("per_client_limits", {}).update(
                data["per_client_limits"]
            )

        cfg_snapshot = dict(_config)

    # Discard stale limiters; they will be recreated on next request
    with _limiters_lock:
        _limiters.clear()

    log.info("Config updated: %s", cfg_snapshot)
    return jsonify({"status": "ok", "config": cfg_snapshot})


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _load_config(path: str, cli_overrides: dict) -> None:
    """Merge defaults < config file < CLI flags into _config."""
    defaults = {
        "max_requests": 10,
        "window_seconds": 60,
        "algorithm": "fixed_window",
        "per_client_limits": {},
    }
    file_cfg: dict = {}
    if path and os.path.isfile(path):
        with open(path) as fh:
            file_cfg = json.load(fh)
        log.info("Loaded config from %s", path)

    # CLI wins over file; file wins over defaults
    merged = {
        **defaults,
        **file_cfg,
        **{k: v for k, v in cli_overrides.items() if v is not None},
    }
    with _config_lock:
        _config.update(merged)
    log.info("Active config: %s", {k: v for k, v in merged.items() if k != "per_client_limits"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Concurrent Rate Limiter HTTP Server")
    parser.add_argument("--config", default="config.json", help="Path to JSON config file")
    parser.add_argument("--max-requests", type=int, default=None, metavar="N")
    parser.add_argument("--window-seconds", type=int, default=None, metavar="T")
    parser.add_argument(
        "--algorithm",
        default=None,
        choices=list(ALGORITHM_MAP),
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    _load_config(
        args.config,
        {
            "max_requests": args.max_requests,
            "window_seconds": args.window_seconds,
            "algorithm": args.algorithm,
        },
    )

    log.info("Starting server on %s:%d  (Ctrl+C to stop)", args.host, args.port)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
