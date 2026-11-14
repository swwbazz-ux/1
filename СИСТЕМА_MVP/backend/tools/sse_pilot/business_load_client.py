from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from .disposable_db import validate_disposable_database
from .sse_load_harness import SSEParser, _open_sse, require_loopback_url


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[index], 3)


def distribution(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "min": round(min(values), 3) if values else None,
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": round(max(values), 3) if values else None,
        "mean": round(statistics.fmean(values), 3) if values else None,
    }


@dataclass
class ClientResult:
    client_id: int
    recipient: str
    role_app_code: str
    connections: int = 0
    all_reconnects: int = 0
    reconnects: int = 0
    all_heartbeats: int = 0
    heartbeats: int = 0
    all_timeouts: int = 0
    timeouts: int = 0
    errors: list[str] = field(default_factory=list)
    event_counts: Counter = field(default_factory=Counter)
    received_utc: dict[int, str] = field(default_factory=dict)
    receive_to_apply_ms: dict[int, float] = field(default_factory=dict)
    semantic_key_extraction_ms: dict[int, float] = field(default_factory=dict)
    voice_operation_keys: dict[int, str] = field(default_factory=dict)
    heartbeat_transport_ms: list[float] = field(default_factory=list)
    heartbeat_arrival_interval_ms: list[float] = field(default_factory=list)
    heartbeat_server_interval_ms: list[float] = field(default_factory=list)
    heartbeat_cadence_overrun_ms: list[float] = field(default_factory=list)
    heartbeat_samples: list[dict[str, object]] = field(default_factory=list)
    _last_heartbeat_arrival: float | None = None
    _last_heartbeat_server_time: datetime | None = None
    last_applied: int = 0
    last_received: int = 0


class ReadyTracker:
    def __init__(self, total: int, path: Path, *, warmup_seconds: float, measurement_seconds: float):
        self.total = total
        self.path = path
        self.warmup_seconds = warmup_seconds
        self.measurement_seconds = measurement_seconds
        self.connected_ids: set[int] = set()
        self.lock = asyncio.Lock()
        self.measurement_start: float | None = None
        self.measurement_end: float | None = None
        self.measurement_started_utc: datetime | None = None
        self.measurement_finished_utc: datetime | None = None

    async def connected(self, client_id: int) -> None:
        async with self.lock:
            self.connected_ids.add(client_id)
            if len(self.connected_ids) == self.total and not self.path.exists():
                ready_clock = time.monotonic()
                ready_utc = datetime.now(timezone.utc)
                self.measurement_start = ready_clock + self.warmup_seconds
                self.measurement_end = self.measurement_start + self.measurement_seconds
                self.measurement_started_utc = ready_utc + timedelta(seconds=self.warmup_seconds)
                self.measurement_finished_utc = self.measurement_started_utc + timedelta(
                    seconds=self.measurement_seconds
                )
                self.path.write_text(
                    json.dumps({
                        "connected_clients": self.total,
                        "warmup_seconds": self.warmup_seconds,
                        "measurement_seconds": self.measurement_seconds,
                        "measurement_started_utc": self.measurement_started_utc.isoformat(),
                        "measurement_finished_utc": self.measurement_finished_utc.isoformat(),
                    }),
                    encoding="utf-8",
                )

    def in_measurement(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return bool(
            self.measurement_start is not None
            and self.measurement_end is not None
            and self.measurement_start <= now < self.measurement_end
        )


def _url_for_role(base_url: str, role_app_code: str) -> str:
    parsed = urlsplit(base_url)
    query = urlencode({"role_app_code": role_app_code})
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _voice_operation(role_app_code: str, event: dict) -> str | None:
    event_type = event.get("type")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    action = payload.get("action")
    object_id = event.get("object_id")
    if event_type == "assignment_changed" and action == "assignment_applied":
        return f"assign:{object_id}"
    if (
        role_app_code == "driver"
        and event_type == "trip_changed"
        and action in {"truck_loaded", "driver_manual_loaded"}
    ):
        return f"dump:{object_id}:id-{payload.get('dump_point_id')}"
    return None


async def run_client(
    client_id: int,
    context: dict,
    args,
    ready: ReadyTracker,
) -> ClientResult:
    result = ClientResult(
        client_id=client_id,
        recipient=context["name"],
        role_app_code=context["role_app_code"],
        last_applied=args.after,
    )
    url = _url_for_role(args.url, context["role_app_code"])
    readiness_deadline = time.monotonic() + args.readiness_timeout
    while (
        (ready.measurement_end is None and time.monotonic() < readiness_deadline)
        or (
            ready.measurement_end is not None
            and time.monotonic() < ready.measurement_end
        )
    ):
        writer = None
        try:
            reader, writer = await _open_sse(
                url,
                after=result.last_applied,
                last_received=result.last_received,
                cookie=context["cookie"],
            )
            result.connections += 1
            if result.connections > 1:
                result.all_reconnects += 1
                if ready.in_measurement():
                    result.reconnects += 1
            await ready.connected(client_id)
            parser = SSEParser()
            while (
                ready.measurement_end is None
                or time.monotonic() < ready.measurement_end
            ):
                remaining = (
                    args.stall_timeout
                    if ready.measurement_end is None
                    else max(0.01, ready.measurement_end - time.monotonic())
                )
                timeout = min(args.stall_timeout, remaining)
                boundary_limited = (
                    ready.measurement_end is not None
                    and remaining <= args.stall_timeout
                )
                try:
                    chunk = await asyncio.wait_for(reader.read(8192), timeout=timeout)
                except asyncio.TimeoutError:
                    # The final wait is intentionally clipped to the fixed
                    # measurement boundary. Reaching that boundary is a clean
                    # stop, not a stalled-connection timeout.
                    if boundary_limited:
                        break
                    result.all_timeouts += 1
                    if ready.in_measurement():
                        result.timeouts += 1
                    break
                if not chunk:
                    break
                for frame in parser.feed(chunk):
                    if frame.event == "heartbeat":
                        arrival_clock = time.monotonic()
                        result.all_heartbeats += 1
                        if not ready.in_measurement(arrival_clock):
                            continue
                        result.heartbeats += 1
                        arrival_utc = datetime.now(timezone.utc)
                        arrival_interval_ms = None
                        if result._last_heartbeat_arrival is not None:
                            arrival_interval_ms = (
                                arrival_clock - result._last_heartbeat_arrival
                            ) * 1000
                            result.heartbeat_arrival_interval_ms.append(arrival_interval_ms)
                        result._last_heartbeat_arrival = arrival_clock
                        try:
                            heartbeat_payload = json.loads(frame.data)
                            server_time = datetime.fromisoformat(
                                heartbeat_payload["server_time"]
                            )
                            if server_time.tzinfo is None:
                                server_time = server_time.replace(tzinfo=timezone.utc)
                            server_time = server_time.astimezone(timezone.utc)
                            transport_ms = (
                                arrival_utc - server_time
                            ).total_seconds() * 1000
                            result.heartbeat_transport_ms.append(transport_ms)
                            server_interval_ms = None
                            cadence_overrun_ms = None
                            if result._last_heartbeat_server_time is not None:
                                server_interval_ms = (
                                    server_time - result._last_heartbeat_server_time
                                ).total_seconds() * 1000
                                cadence_overrun_ms = max(
                                    0.0,
                                    server_interval_ms - args.heartbeat_seconds * 1000,
                                )
                                result.heartbeat_server_interval_ms.append(
                                    server_interval_ms
                                )
                                result.heartbeat_cadence_overrun_ms.append(
                                    cadence_overrun_ms
                                )
                            result._last_heartbeat_server_time = server_time
                            result.heartbeat_samples.append({
                                "received_utc": arrival_utc.isoformat(),
                                "server_utc": server_time.isoformat(),
                                "transport_ms": transport_ms,
                                "arrival_interval_ms": arrival_interval_ms,
                                "server_interval_ms": server_interval_ms,
                                "cadence_overrun_ms": cadence_overrun_ms,
                            })
                        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                            result.errors.append(f"invalid heartbeat payload: {error}")
                    elif frame.event not in {"operational", "operational-state"}:
                        continue
                    else:
                        received_at = datetime.now(timezone.utc)
                        received_clock = time.perf_counter()
                        try:
                            payload = json.loads(frame.data)
                        except json.JSONDecodeError as error:
                            result.errors.append(f"invalid JSON: {error}")
                            continue
                        for event in payload.get("events", []):
                            try:
                                version = int(event["version"])
                            except (KeyError, TypeError, ValueError):
                                result.errors.append("business event has no numeric version")
                                continue
                            result.event_counts[version] += 1
                            result.received_utc.setdefault(version, received_at.isoformat())
                            apply_started = time.perf_counter()
                            if not isinstance(event.get("payload", {}), dict):
                                result.errors.append(f"event {version} payload is not an object")
                            result.receive_to_apply_ms.setdefault(
                                version,
                                max(0.0, (time.perf_counter() - received_clock) * 1000),
                            )
                            operation_key = _voice_operation(context["role_app_code"], event)
                            if operation_key:
                                result.voice_operation_keys.setdefault(version, operation_key)
                                result.semantic_key_extraction_ms.setdefault(
                                    version,
                                    max(0.0, (time.perf_counter() - apply_started) * 1000),
                                )
                        if frame.event_id is not None:
                            try:
                                envelope_id = int(frame.event_id)
                                result.last_received = max(result.last_received, envelope_id)
                                result.last_applied = max(result.last_applied, envelope_id)
                            except ValueError:
                                result.errors.append(f"invalid envelope id {frame.event_id!r}")

        except (OSError, asyncio.IncompleteReadError, ConnectionError) as error:
            result.errors.append(f"{type(error).__name__}: {str(error)[:200]}")
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
        if ready.measurement_end is None or time.monotonic() < ready.measurement_end:
            await asyncio.sleep(0.05)
    return result


def validate_client(
    result: ClientResult,
    expected: dict,
    *,
    min_heartbeats: int = 1,
    max_reconnects: int = 0,
    max_timeouts: int = 0,
    max_heartbeat_cadence_overrun_ms: float = 8000,
) -> dict[str, object]:
    contract = expected["recipients"][result.recipient]
    expected_ids = set(contract["expected_event_ids"])
    foreign_ids = set(contract["foreign_event_ids"])
    actual_ids = set(result.event_counts)
    duplicates = sorted(
        event_id for event_id, count in result.event_counts.items() if count != 1
    )
    missing = sorted(expected_ids - actual_ids)
    foreign = sorted(actual_ids & foreign_ids)
    unexpected = sorted(actual_ids - expected_ids)
    expected_voice = {
        int(key): value for key, value in contract["voice_operations"].items()
    }
    voice_mismatch = {
        str(event_id): {
            "expected": operation_key,
            "actual": result.voice_operation_keys.get(event_id),
        }
        for event_id, operation_key in expected_voice.items()
        if result.voice_operation_keys.get(event_id) != operation_key
    }
    delivery_failures = []
    if missing:
        delivery_failures.append(f"missing expected events: {missing}")
    if duplicates:
        delivery_failures.append(f"duplicate events: {duplicates}")
    if foreign:
        delivery_failures.append(f"foreign events delivered: {foreign}")
    if unexpected:
        delivery_failures.append(f"unexpected events delivered: {unexpected}")
    if voice_mismatch:
        delivery_failures.append(f"semantic key mismatch: {voice_mismatch}")
    delivery_failures.extend(result.errors)

    liveness_failures = []
    if result.connections == 0:
        liveness_failures.append("never connected")
    if result.heartbeats < min_heartbeats:
        liveness_failures.append(
            f"measurement heartbeats below threshold: {result.heartbeats} < {min_heartbeats}"
        )
    if result.reconnects > max_reconnects:
        liveness_failures.append(
            f"measurement reconnects above threshold: {result.reconnects} > {max_reconnects}"
        )
    if result.timeouts > max_timeouts:
        liveness_failures.append(
            f"measurement timeouts above threshold: {result.timeouts} > {max_timeouts}"
        )
    max_overrun = max(result.heartbeat_cadence_overrun_ms, default=0.0)
    if max_overrun > max_heartbeat_cadence_overrun_ms:
        liveness_failures.append(
            "heartbeat cadence overrun above threshold: "
            f"{max_overrun:.3f} > {max_heartbeat_cadence_overrun_ms:.3f} ms"
        )
    liveness_failures.extend(result.errors)
    failures = delivery_failures + [
        failure for failure in liveness_failures if failure not in delivery_failures
    ]
    return {
        "client_id": result.client_id,
        "recipient": result.recipient,
        "role_app_code": result.role_app_code,
        "connections": result.connections,
        "all_reconnects": result.all_reconnects,
        "reconnects": result.reconnects,
        "all_heartbeats": result.all_heartbeats,
        "heartbeats": result.heartbeats,
        "all_timeouts": result.all_timeouts,
        "timeouts": result.timeouts,
        "expected_event_ids": sorted(expected_ids),
        "applied_event_ids": sorted(actual_ids),
        "missing_event_ids": missing,
        "duplicate_event_ids": duplicates,
        "foreign_event_ids_delivered": foreign,
        "unexpected_event_ids": unexpected,
        "voice_operation_keys": {
            str(key): value for key, value in result.voice_operation_keys.items()
        },
        "delivery_status": "pass" if not delivery_failures else "fail",
        "liveness_status": "pass" if not liveness_failures else "fail",
        "delivery_failures": delivery_failures,
        "liveness_failures": liveness_failures,
        "failures": failures,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Validate relevant business SSE delivery.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--clients", type=int, default=96)
    parser.add_argument("--warmup-seconds", type=float, default=3)
    parser.add_argument("--measurement-seconds", type=float, default=12)
    parser.add_argument("--readiness-timeout", type=float, default=30)
    parser.add_argument("--stall-timeout", type=float, default=10)
    parser.add_argument("--heartbeat-seconds", type=float, default=2)
    parser.add_argument("--min-heartbeats", type=int, default=2)
    parser.add_argument("--max-reconnects", type=int, default=0)
    parser.add_argument("--max-timeouts", type=int, default=0)
    parser.add_argument("--max-heartbeat-cadence-overrun-ms", type=float, default=8000)
    parser.add_argument("--after", type=int, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--disposable-root", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--samples-output",
        type=Path,
        help="Raw anonymized timing samples (default: <output stem>-samples.json)",
    )
    return parser.parse_args(argv)


def build_anonymized_timing_samples(
    results: list[ClientResult],
    expected: dict,
) -> dict[str, object]:
    """Build independently recalculable samples without raw client/event IDs."""
    client_aliases = {
        id(result): f"client-{index:04d}"
        for index, result in enumerate(results, start=1)
    }
    event_ids = sorted({
        event_id
        for result in results
        for event_id in result.received_utc
    })
    event_aliases = {
        event_id: f"event-{index:04d}"
        for index, event_id in enumerate(event_ids, start=1)
    }

    commit_to_receive = []
    heartbeat = []
    for result in results:
        client_alias = client_aliases[id(result)]
        for event_id, received in sorted(result.received_utc.items()):
            committed = expected["commit_utc_by_event"].get(str(event_id))
            if not committed:
                continue
            value_ms = (
                datetime.fromisoformat(received) - datetime.fromisoformat(committed)
            ).total_seconds() * 1000
            commit_to_receive.append({
                "client": client_alias,
                "event": event_aliases[event_id],
                "committed_utc": committed,
                "received_utc": received,
                "value_ms": value_ms,
            })
        for sequence, sample in enumerate(result.heartbeat_samples, start=1):
            heartbeat.append({
                "client": client_alias,
                "sequence": sequence,
                **sample,
            })

    return {
        "schema": 1,
        "identity_contract": (
            "Run-local ordinal aliases preserve grouping without raw client, "
            "recipient, equipment or event identifiers."
        ),
        "commit_to_receive": commit_to_receive,
        "heartbeat": heartbeat,
    }


def distributions_from_timing_samples(samples: dict[str, object]) -> dict[str, object]:
    commit_values = [
        float(item["value_ms"])
        for item in samples["commit_to_receive"]
    ]
    heartbeat_values = {
        name: [
            float(item[name])
            for item in samples["heartbeat"]
            if item.get(name) is not None
        ]
        for name in (
            "transport_ms",
            "arrival_interval_ms",
            "server_interval_ms",
            "cadence_overrun_ms",
        )
    }
    return {
        "commit_to_receive_ms": distribution(commit_values),
        "heartbeat_transport_ms": distribution(heartbeat_values["transport_ms"]),
        "heartbeat_arrival_interval_ms": distribution(
            heartbeat_values["arrival_interval_ms"]
        ),
        "heartbeat_server_interval_ms": distribution(
            heartbeat_values["server_interval_ms"]
        ),
        "heartbeat_cadence_overrun_ms": distribution(
            heartbeat_values["cadence_overrun_ms"]
        ),
    }


async def async_main(args) -> int:
    require_loopback_url(args.url)
    database, root = validate_disposable_database(
        args.database,
        args.disposable_root,
        require_database_missing=False,
    )
    if not database.is_file():
        raise ValueError("disposable database is missing")
    for path in (args.fixture, args.expected, args.ready):
        if path.resolve().parent != root:
            raise ValueError("coordination files must stay inside disposable root")
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    contexts = fixture["contexts"]
    ready = ReadyTracker(
        args.clients,
        args.ready,
        warmup_seconds=args.warmup_seconds,
        measurement_seconds=args.measurement_seconds,
    )
    started = time.monotonic()
    results = await asyncio.gather(*(
        run_client(index, contexts[index % len(contexts)], args, ready)
        for index in range(args.clients)
    ))
    elapsed = time.monotonic() - started
    if not args.expected.exists():
        raise RuntimeError("business event producer did not publish expected manifest")
    expected = json.loads(args.expected.read_text(encoding="utf-8"))
    clients = [
        validate_client(
            result,
            expected,
            min_heartbeats=args.min_heartbeats,
            max_reconnects=args.max_reconnects,
            max_timeouts=args.max_timeouts,
            max_heartbeat_cadence_overrun_ms=(
                args.max_heartbeat_cadence_overrun_ms
            ),
        )
        for result in results
    ]

    receive_to_apply = []
    semantic_key_extraction = []
    for result in results:
        receive_to_apply.extend(result.receive_to_apply_ms.values())
        semantic_key_extraction.extend(result.semantic_key_extraction_ms.values())

    timing_samples = build_anonymized_timing_samples(results, expected)
    timing_distributions = distributions_from_timing_samples(timing_samples)
    timing_samples["distributions"] = timing_distributions
    samples_output = args.samples_output or args.output.with_name(
        f"{args.output.stem}-samples.json"
    )
    samples_output.parent.mkdir(parents=True, exist_ok=True)
    samples_bytes = json.dumps(
        timing_samples,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    samples_output.write_bytes(samples_bytes)
    samples_sha256 = hashlib.sha256(samples_bytes).hexdigest()

    delivery_failed = [item for item in clients if item["delivery_failures"]]
    liveness_failed = [item for item in clients if item["liveness_failures"]]
    failed = [item for item in clients if item["failures"]]
    payload = {
        "schema": 1,
        "scope": (
            "loopback-only disposable SQLite; Redis disabled; synthetic identities; "
            "server business delivery, not Android audible playback"
        ),
        "parameters": {
            "clients": args.clients,
            "recipient_contexts": len(contexts),
            "warmup_seconds": args.warmup_seconds,
            "measurement_seconds": args.measurement_seconds,
            "readiness_timeout_seconds": args.readiness_timeout,
            "stall_timeout_seconds": args.stall_timeout,
            "configured_heartbeat_seconds": args.heartbeat_seconds,
            "initial_cursor": args.after,
        },
        "acceptance_thresholds": {
            "min_heartbeats_per_client": args.min_heartbeats,
            "max_reconnects_per_client": args.max_reconnects,
            "max_timeouts_per_client": args.max_timeouts,
            "max_heartbeat_cadence_overrun_ms_per_client": (
                args.max_heartbeat_cadence_overrun_ms
            ),
        },
        "measurement_window": {
            "started_utc": (
                ready.measurement_started_utc.isoformat()
                if ready.measurement_started_utc else None
            ),
            "finished_utc": (
                ready.measurement_finished_utc.isoformat()
                if ready.measurement_finished_utc else None
            ),
            "fixed": True,
        },
        "elapsed_seconds": round(elapsed, 3),
        "summary": {
            "clients_connected": sum(item.connections > 0 for item in results),
            "delivery_status": "pass" if not delivery_failed else "fail",
            "liveness_status": "pass" if not liveness_failed else "fail",
            "clients_delivery_passed": len(clients) - len(delivery_failed),
            "clients_delivery_failed": len(delivery_failed),
            "clients_liveness_passed": len(clients) - len(liveness_failed),
            "clients_liveness_failed": len(liveness_failed),
            "clients_with_heartbeat": sum(item.heartbeats > 0 for item in results),
            "connections": sum(item.connections for item in results),
            "all_reconnects": sum(item.all_reconnects for item in results),
            "reconnects": sum(item.reconnects for item in results),
            "all_timeouts": sum(item.all_timeouts for item in results),
            "timeouts": sum(item.timeouts for item in results),
            "applied_business_event_instances": sum(
                len(item.event_counts) for item in results
            ),
            "duplicate_business_event_instances": sum(
                sum(max(0, count - 1) for count in item.event_counts.values())
                for item in results
            ),
            "foreign_business_event_instances": sum(
                len(item["foreign_event_ids_delivered"]) for item in clients
            ),
            "commit_to_receive_ms": timing_distributions["commit_to_receive_ms"],
            "receive_to_apply_ms": distribution(receive_to_apply),
            "semantic_key_extraction_ms": distribution(semantic_key_extraction),
            "heartbeat_transport_ms": timing_distributions["heartbeat_transport_ms"],
            "heartbeat_arrival_interval_ms": timing_distributions[
                "heartbeat_arrival_interval_ms"
            ],
            "heartbeat_server_interval_ms": timing_distributions[
                "heartbeat_server_interval_ms"
            ],
            "heartbeat_cadence_overrun_ms": timing_distributions[
                "heartbeat_cadence_overrun_ms"
            ],
        },
        "timing_samples": {
            "schema": timing_samples["schema"],
            "file": samples_output.name,
            "sha256": samples_sha256,
            "commit_to_receive_count": len(timing_samples["commit_to_receive"]),
            "heartbeat_count": len(timing_samples["heartbeat"]),
            "identity_contract": timing_samples["identity_contract"],
        },
        "metric_notes": {
            "commit_to_receive_ms": (
                "Earliest local transaction on_commit timestamp to client receipt on the same host."
            ),
            "receive_to_apply_ms": (
                "Client parser/validation boundary; it does not render a real WebView."
            ),
            "semantic_key_extraction_ms": (
                "In-memory extraction and validation of the expected semantic operation key; not durable handoff, Android queue or audible latency."
            ),
            "heartbeat_transport_ms": (
                "Same-host receive wall clock minus heartbeat server_time; includes server, socket and client scheduling."
            ),
            "heartbeat_cadence_overrun_ms": (
                "Excess of consecutive server_time intervals over configured cadence; an event-loop/ORM scheduling proxy."
            ),
        },
        "expected": expected,
        "delivery_failed_clients": delivery_failed,
        "liveness_failed_clients": liveness_failed,
        "failed_clients": failed,
        "per_client": clients,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    return 0 if not failed else 2


def main(argv=None) -> int:
    args = parse_args(argv)
    if (
        args.clients <= 0
        or args.warmup_seconds < 0
        or args.measurement_seconds <= 0
        or args.readiness_timeout <= 0
        or args.stall_timeout <= 0
        or args.min_heartbeats <= 0
    ):
        raise ValueError("client and fixed-window parameters are invalid")
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
