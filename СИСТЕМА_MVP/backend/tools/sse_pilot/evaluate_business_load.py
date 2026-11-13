from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[index], 3)


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _read_resource_rows(path: Path, started: datetime, finished: datetime) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [
        row
        for row in rows
        if started <= _parse_utc(row["utc"]) <= finished
    ]


def evaluate(
    payload: dict,
    resource_rows: list[dict],
    *,
    expected_clients: int,
    max_commit_to_receive_p95_ms: float,
    max_heartbeat_overrun_p95_ms: float,
    max_cpu_percent_one_core: float,
    max_private_bytes: int,
    max_threads: int,
    min_resource_samples: int,
    min_resource_coverage_fraction: float,
) -> dict:
    summary = payload["summary"]
    window = payload["measurement_window"]
    started = _parse_utc(window["started_utc"])
    finished = _parse_utc(window["finished_utc"])
    window_seconds = (finished - started).total_seconds()

    cpu_percent = None
    resource_coverage_seconds = 0.0
    if len(resource_rows) >= 2:
        first = resource_rows[0]
        last = resource_rows[-1]
        resource_coverage_seconds = (
            _parse_utc(last["utc"]) - _parse_utc(first["utc"])
        ).total_seconds()
        wall = max(resource_coverage_seconds, 0.000001)
        cpu_percent = round(
            100.0
            * (float(last["cpu_total_seconds"]) - float(first["cpu_total_seconds"]))
            / wall,
            3,
        )

    max_established = max(
        (int(row["established_tcp"]) for row in resource_rows),
        default=0,
    )
    max_private = max(
        (int(row["private_bytes"]) for row in resource_rows),
        default=0,
    )
    max_thread_count = max(
        (int(row["thread_count"]) for row in resource_rows),
        default=0,
    )
    commit_p95 = summary["commit_to_receive_ms"]["p95"]
    heartbeat_overrun_p95 = summary["heartbeat_cadence_overrun_ms"]["p95"]
    coverage_fraction = (
        resource_coverage_seconds / window_seconds if window_seconds > 0 else 0.0
    )

    checks = {
        "delivery": {
            "actual": summary["delivery_status"],
            "threshold": "pass",
            "status": "pass" if summary["delivery_status"] == "pass" else "fail",
        },
        "liveness": {
            "actual": summary["liveness_status"],
            "threshold": "pass",
            "status": "pass" if summary["liveness_status"] == "pass" else "fail",
        },
        "clients_connected": {
            "actual": summary["clients_connected"],
            "threshold": expected_clients,
            "status": (
                "pass" if summary["clients_connected"] == expected_clients else "fail"
            ),
        },
        "commit_to_receive_p95_ms": {
            "actual": commit_p95,
            "threshold_max": max_commit_to_receive_p95_ms,
            "status": (
                "pass"
                if commit_p95 is not None and commit_p95 <= max_commit_to_receive_p95_ms
                else "fail"
            ),
        },
        "heartbeat_cadence_overrun_p95_ms": {
            "actual": heartbeat_overrun_p95,
            "threshold_max": max_heartbeat_overrun_p95_ms,
            "status": (
                "pass"
                if heartbeat_overrun_p95 is not None
                and heartbeat_overrun_p95 <= max_heartbeat_overrun_p95_ms
                else "fail"
            ),
        },
        "resource_samples": {
            "actual": len(resource_rows),
            "threshold_min": min_resource_samples,
            "status": "pass" if len(resource_rows) >= min_resource_samples else "fail",
        },
        "resource_window_coverage_fraction": {
            "actual": round(coverage_fraction, 4),
            "threshold_min": min_resource_coverage_fraction,
            "status": "pass" if coverage_fraction >= min_resource_coverage_fraction else "fail",
        },
        "established_tcp": {
            "actual_max": max_established,
            "threshold_min": expected_clients,
            "status": "pass" if max_established >= expected_clients else "fail",
        },
        "cpu_percent_one_core": {
            "actual": cpu_percent,
            "threshold_max": max_cpu_percent_one_core,
            "status": (
                "pass"
                if cpu_percent is not None and cpu_percent <= max_cpu_percent_one_core
                else "fail"
            ),
        },
        "private_bytes": {
            "actual_max": max_private,
            "threshold_max": max_private_bytes,
            "status": "pass" if max_private <= max_private_bytes else "fail",
        },
        "thread_count": {
            "actual_max": max_thread_count,
            "threshold_max": max_threads,
            "status": "pass" if max_thread_count <= max_threads else "fail",
        },
    }
    failed = [name for name, check in checks.items() if check["status"] != "pass"]
    return {
        "schema": 1,
        "status": "pass" if not failed else "fail",
        "failed_checks": failed,
        "measurement_window": {
            "started_utc": started.isoformat(),
            "finished_utc": finished.isoformat(),
            "seconds": window_seconds,
            "fixed": bool(window.get("fixed")),
        },
        "resource_window": {
            "samples": len(resource_rows),
            "coverage_seconds": round(resource_coverage_seconds, 3),
            "coverage_fraction": round(coverage_fraction, 4),
        },
        "checks": checks,
        "notes": {
            "cpu_percent_one_core": (
                "Process CPU seconds divided by wall seconds; 100% is one fully used logical core."
            ),
            "scope": (
                "Local loopback disposable SQLite observation only; not PostgreSQL, Redis, QA, production, Android, or battery proof."
            ),
        },
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Apply explicit business-load acceptance gates.")
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clients", type=int, required=True)
    parser.add_argument("--max-commit-p95-ms", type=float, default=5000)
    parser.add_argument("--max-heartbeat-overrun-p95-ms", type=float, default=8000)
    parser.add_argument("--max-cpu-percent-one-core", type=float, default=800)
    parser.add_argument("--max-private-bytes", type=int, default=805306368)
    parser.add_argument("--max-threads", type=int, default=128)
    parser.add_argument("--min-resource-samples", type=int, default=8)
    parser.add_argument("--min-resource-coverage-fraction", type=float, default=0.8)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    payload = json.loads(args.result.read_text(encoding="utf-8"))
    window = payload["measurement_window"]
    rows = _read_resource_rows(
        args.resources,
        _parse_utc(window["started_utc"]),
        _parse_utc(window["finished_utc"]),
    )
    result = evaluate(
        payload,
        rows,
        expected_clients=args.clients,
        max_commit_to_receive_p95_ms=args.max_commit_p95_ms,
        max_heartbeat_overrun_p95_ms=args.max_heartbeat_overrun_p95_ms,
        max_cpu_percent_one_core=args.max_cpu_percent_one_core,
        max_private_bytes=args.max_private_bytes,
        max_threads=args.max_threads,
        min_resource_samples=args.min_resource_samples,
        min_resource_coverage_fraction=args.min_resource_coverage_fraction,
    )
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
