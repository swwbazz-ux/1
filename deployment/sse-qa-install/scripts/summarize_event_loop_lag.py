from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise ValueError("no samples")
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--start-unix-ns", type=int, required=True)
    parser.add_argument("--end-unix-ns", type=int, required=True)
    parser.add_argument("--max-age-seconds", type=int, default=120)
    args = parser.parse_args()
    if args.start_unix_ns <= 0 or args.end_unix_ns <= args.start_unix_ns:
        raise ValueError("invalid measurement window")
    path = args.path
    values: list[float] = []
    pids: set[int] = set()
    selected_timestamps: list[int] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        if set(item) != {"schema", "unix_ns", "interval_ms", "lag_ms", "pid"} or item["schema"] != 1:
            raise ValueError(f"invalid sample at line {number}")
        lag = float(item["lag_ms"])
        if not math.isfinite(lag) or lag < 0:
            raise ValueError(f"invalid lag at line {number}")
        timestamp = int(item["unix_ns"])
        if args.start_unix_ns <= timestamp < args.end_unix_ns:
            values.append(lag)
            pids.add(int(item["pid"]))
            selected_timestamps.append(timestamp)
    if not values:
        raise ValueError("no event-loop samples in requested window")
    now_ns = time.time_ns()
    if now_ns - max(selected_timestamps) > args.max_age_seconds * 1_000_000_000:
        raise ValueError("event-loop samples are stale")
    result = {
        "schema": 1,
        "samples": len(values),
        "workers": len(pids),
        "window": {
            "start_unix_ns": args.start_unix_ns,
            "end_unix_ns": args.end_unix_ns,
            "first_sample_unix_ns": min(selected_timestamps),
            "last_sample_unix_ns": max(selected_timestamps),
            "freshness_limit_seconds": args.max_age_seconds,
        },
        "lag_ms": {
            "p50": percentile(values, 0.50),
            "p95": percentile(values, 0.95),
            "p99": percentile(values, 0.99),
            "max": max(values),
            "mean": round(statistics.fmean(values), 3),
        },
    }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
