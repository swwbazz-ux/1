from __future__ import annotations

import argparse
import asyncio
import codecs
import contextlib
import ipaddress
import json
import math
import ssl
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}
SCENARIOS = ("clean", "disconnect", "stall", "duplicate", "gap", "out_of_order")


@dataclass(frozen=True)
class SSEEvent:
    event: str
    data: str
    event_id: str | None = None
    retry_ms: int | None = None


class SSEParser:
    """Incremental UTF-8 parser implementing the useful subset of the SSE grammar."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._text = ""
        self._event_name = ""
        self._data_lines: list[str] = []
        self._event_id: str | None = None
        self._retry_ms: int | None = None

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        self._text += self._decoder.decode(chunk)
        events: list[SSEEvent] = []
        while True:
            line, separator, rest = self._next_line(self._text)
            if separator is None:
                break
            self._text = rest
            event = self._consume_line(line)
            if event is not None:
                events.append(event)
        return events

    @staticmethod
    def _next_line(text: str) -> tuple[str, str | None, str]:
        for index, character in enumerate(text):
            if character == "\n":
                line = text[:index]
                if line.endswith("\r"):
                    line = line[:-1]
                return line, "\n", text[index + 1 :]
            if character == "\r":
                if index + 1 == len(text):
                    return text, None, ""
                offset = 2 if text[index + 1] == "\n" else 1
                return text[:index], "\r", text[index + offset :]
        return text, None, ""

    def _consume_line(self, line: str) -> SSEEvent | None:
        if line == "":
            if not self._data_lines:
                self._reset_event()
                return None
            event = SSEEvent(
                event=self._event_name or "message",
                data="\n".join(self._data_lines),
                event_id=self._event_id,
                retry_ms=self._retry_ms,
            )
            self._reset_event()
            return event
        if line.startswith(":"):
            return None
        field_name, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field_name == "event":
            self._event_name = value
        elif field_name == "data":
            self._data_lines.append(value)
        elif field_name == "id" and "\x00" not in value:
            self._event_id = value
        elif field_name == "retry" and value.isdecimal():
            self._retry_ms = int(value)
        return None

    def _reset_event(self) -> None:
        self._event_name = ""
        self._data_lines = []
        self._event_id = None
        self._retry_ms = None


@dataclass
class ClientMetrics:
    client_id: int
    connections: int = 0
    reconnects: int = 0
    data_events: int = 0
    heartbeat_events: int = 0
    duplicate_ids: int = 0
    regressing_ids: int = 0
    id_gaps: int = 0
    malformed_ids: int = 0
    payload_version_mismatches: int = 0
    bytes_received: int = 0
    timeouts: int = 0
    errors: list[str] = field(default_factory=list)
    first_event_ms: float | None = None
    max_silence_ms: float = 0.0
    last_applied: int = 0
    last_received: int = 0
    _seen_ids: set[int] = field(default_factory=set, repr=False)

    def public(self) -> dict[str, object]:
        payload = asdict(self)
        payload.pop("_seen_ids", None)
        return payload


@dataclass(frozen=True)
class LoadOptions:
    url: str
    clients: int
    duration: float
    stall_timeout: float
    initial_after: int = 0
    cookie: str | None = None


def require_loopback_url(raw_url: str) -> None:
    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("only http:// or https:// URLs are supported")
    if parsed.username or parsed.password:
        raise ValueError("credentials in the URL are forbidden")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname in LOOPBACK_NAMES:
        return
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as error:
        raise ValueError("the harness is local-only; use localhost or a loopback IP") from error
    if not address.is_loopback:
        raise ValueError("the harness is local-only; non-loopback targets are forbidden")


def url_with_after(raw_url: str, after: int) -> str:
    parsed = urlsplit(raw_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["after"] = [str(after)]
    encoded = urlencode(query, doseq=True)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, encoded, parsed.fragment))


async def _open_sse(
    raw_url: str,
    *,
    after: int,
    last_received: int,
    cookie: str | None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    target_url = url_with_after(raw_url, after)
    parsed = urlsplit(target_url)
    host = parsed.hostname or "127.0.0.1"
    use_tls = parsed.scheme == "https"
    port = parsed.port or (443 if use_tls else 80)
    ssl_context = ssl.create_default_context() if use_tls else None
    reader, writer = await asyncio.open_connection(
        host,
        port,
        ssl=ssl_context,
        server_hostname=host if use_tls else None,
    )
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    host_header = parsed.netloc
    headers = [
        f"GET {path} HTTP/1.1",
        f"Host: {host_header}",
        "Accept: text/event-stream",
        "Cache-Control: no-cache",
        "Connection: close",
    ]
    if last_received > 0:
        # The application cursor in `after` deliberately wins over this delivery cursor.
        headers.append(f"Last-Event-ID: {last_received}")
    if cookie:
        headers.append(f"Cookie: {cookie}")
    writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
    await writer.drain()
    response_headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    status_line, *raw_headers = response_headers.decode("iso-8859-1").split("\r\n")
    if not status_line.startswith("HTTP/") or " 200 " not in status_line:
        writer.close()
        await writer.wait_closed()
        raise ConnectionError(f"unexpected HTTP response: {status_line[:120]}")
    content_type = ""
    for raw_header in raw_headers:
        name, separator, value = raw_header.partition(":")
        if separator and name.lower() == "content-type":
            content_type = value.strip().lower()
    if not content_type.startswith("text/event-stream"):
        writer.close()
        await writer.wait_closed()
        raise ConnectionError(f"unexpected Content-Type: {content_type or '<missing>'}")
    return reader, writer


def _record_event(metrics: ClientMetrics, event: SSEEvent, started_at: float) -> None:
    if metrics.first_event_ms is None:
        metrics.first_event_ms = (time.monotonic() - started_at) * 1000
    if event.event == "heartbeat":
        metrics.heartbeat_events += 1
        return
    metrics.data_events += 1
    if event.event_id is None:
        metrics.malformed_ids += 1
        return
    try:
        event_id = int(event.event_id)
    except ValueError:
        metrics.malformed_ids += 1
        return
    if event_id in metrics._seen_ids:
        metrics.duplicate_ids += 1
    elif metrics.last_received > 0:
        if event_id < metrics.last_received:
            metrics.regressing_ids += 1
        elif event_id > metrics.last_received + 1:
            metrics.id_gaps += event_id - metrics.last_received - 1
    metrics._seen_ids.add(event_id)
    metrics.last_received = max(metrics.last_received, event_id)
    try:
        payload = json.loads(event.data)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and "version" in payload:
        try:
            payload_version = int(payload["version"])
        except (TypeError, ValueError):
            metrics.payload_version_mismatches += 1
        else:
            if payload_version != event_id:
                metrics.payload_version_mismatches += 1
    # The harness has no DOM/domain operation. Validation itself is its apply boundary.
    metrics.last_applied = max(metrics.last_applied, event_id)


async def run_client(client_id: int, options: LoadOptions) -> ClientMetrics:
    metrics = ClientMetrics(client_id=client_id, last_applied=options.initial_after)
    started_at = time.monotonic()
    deadline = started_at + options.duration
    retry_delay = 0.05
    while time.monotonic() < deadline:
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await _open_sse(
                options.url,
                after=metrics.last_applied,
                last_received=metrics.last_received,
                cookie=options.cookie,
            )
            metrics.connections += 1
            if metrics.connections > 1:
                metrics.reconnects += 1
            parser = SSEParser()
            last_activity = time.monotonic()
            retry_delay = 0.05
            while time.monotonic() < deadline:
                timeout = min(options.stall_timeout, max(0.01, deadline - time.monotonic()))
                try:
                    chunk = await asyncio.wait_for(reader.read(8192), timeout=timeout)
                except asyncio.TimeoutError:
                    if time.monotonic() >= deadline:
                        break
                    metrics.timeouts += 1
                    metrics.max_silence_ms = max(
                        metrics.max_silence_ms,
                        (time.monotonic() - last_activity) * 1000,
                    )
                    break
                if not chunk:
                    break
                metrics.bytes_received += len(chunk)
                now = time.monotonic()
                metrics.max_silence_ms = max(
                    metrics.max_silence_ms,
                    (now - last_activity) * 1000,
                )
                last_activity = now
                for event in parser.feed(chunk):
                    _record_event(metrics, event, started_at)
        except (OSError, asyncio.IncompleteReadError, ConnectionError) as error:
            message = f"{type(error).__name__}: {str(error)[:160]}"
            if not metrics.errors or metrics.errors[-1] != message:
                metrics.errors.append(message)
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(min(retry_delay, max(0, deadline - time.monotonic())))
        retry_delay = min(1.0, retry_delay * 2)
    return metrics


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 3)


def summarize(metrics: list[ClientMetrics], elapsed: float) -> dict[str, object]:
    first_events = [item.first_event_ms for item in metrics if item.first_event_ms is not None]
    totals = {
        field_name: sum(getattr(item, field_name) for item in metrics)
        for field_name in (
            "connections",
            "reconnects",
            "data_events",
            "heartbeat_events",
            "duplicate_ids",
            "regressing_ids",
            "id_gaps",
            "malformed_ids",
            "payload_version_mismatches",
            "bytes_received",
            "timeouts",
        )
    }
    return {
        "schema": 1,
        "clients": len(metrics),
        "elapsed_seconds": round(elapsed, 3),
        "clients_connected": sum(item.connections > 0 for item in metrics),
        "clients_with_data": sum(item.data_events > 0 for item in metrics),
        "clients_with_heartbeat": sum(item.heartbeat_events > 0 for item in metrics),
        "first_event_ms_p50": _percentile(first_events, 0.50),
        "first_event_ms_p95": _percentile(first_events, 0.95),
        "max_silence_ms": round(max((item.max_silence_ms for item in metrics), default=0), 3),
        "totals": totals,
        "errors": [
            {"client_id": item.client_id, "messages": item.errors}
            for item in metrics
            if item.errors
        ],
        "per_client": [item.public() for item in metrics],
    }


async def run_load(options: LoadOptions) -> dict[str, object]:
    require_loopback_url(options.url)
    started_at = time.monotonic()
    metrics = await asyncio.gather(
        *(run_client(client_id, options) for client_id in range(options.clients))
    )
    return summarize(list(metrics), time.monotonic() - started_at)


class MockSSEServer:
    """Loopback-only deterministic SSE source for load and recovery exercises."""

    def __init__(self, scenario: str, event_count: int) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario: {scenario}")
        self.scenario = scenario
        self.event_count = event_count
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self, port: int = 0) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        socket = self.server.sockets[0]
        self.port = int(socket.getsockname()[1])

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        writers = list(self._writers)
        for writer in writers:
            writer.close()
        for writer in writers:
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        self._writers.clear()

    def _sequence(self, after: int) -> list[int]:
        sequence = list(range(after + 1, self.event_count + 1))
        if self.scenario == "gap" and 3 in sequence:
            sequence.remove(3)
        elif self.scenario == "out_of_order" and after == 0 and len(sequence) >= 3:
            sequence[1], sequence[2] = sequence[2], sequence[1]
        return sequence

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
            request_line = request.decode("iso-8859-1").split("\r\n", 1)[0]
            method, target, _protocol = request_line.split(" ", 2)
            if method != "GET":
                writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            query = parse_qs(urlsplit(target).query)
            try:
                after = max(0, int(query.get("after", ["0"])[0]))
            except ValueError:
                after = 0
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream; charset=utf-8\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            sequence = self._sequence(after)
            if self.scenario == "disconnect" and sequence:
                sequence = sequence[:2]
            elif self.scenario == "stall" and after < 2 and sequence:
                sequence = sequence[:2]
            for event_id in sequence:
                await self._send_event(writer, event_id)
                if self.scenario == "duplicate" and event_id == 3:
                    await self._send_event(writer, event_id)
                await asyncio.sleep(0.003)
            if self.scenario == "disconnect" and sequence and sequence[-1] < self.event_count:
                return
            if self.scenario == "stall" and after < 2:
                await asyncio.sleep(10)
                return
            while not writer.is_closing():
                payload = json.dumps(
                    {"version": self.event_count, "kind": "mock-heartbeat"},
                    separators=(",", ":"),
                )
                writer.write(f"event: heartbeat\ndata: {payload}\n\n".encode("utf-8"))
                await writer.drain()
                await asyncio.sleep(0.1)
        except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    @staticmethod
    async def _send_event(writer: asyncio.StreamWriter, event_id: int) -> None:
        payload = json.dumps(
            {"version": event_id, "operation_key": f"mock:{event_id}"},
            separators=(",", ":"),
        )
        frame = f"id: {event_id}\nevent: operational-state\ndata: {payload}\n\n"
        writer.write(frame.encode("utf-8"))
        await writer.drain()


async def exercise_scenario(
    scenario: str,
    *,
    clients: int,
    events: int,
    duration: float,
    stall_timeout: float,
) -> dict[str, object]:
    server = MockSSEServer(scenario, events)
    await server.start()
    try:
        result = await run_load(
            LoadOptions(
                url=f"http://127.0.0.1:{server.port}/realtime/stream/",
                clients=clients,
                duration=duration,
                stall_timeout=stall_timeout,
            )
        )
        result["scenario"] = scenario
        return result
    finally:
        await server.close()


def validate_result(
    result: dict[str, object],
    *,
    min_data_events: int,
    expect_contiguous_ids: bool,
) -> list[str]:
    failures: list[str] = []
    clients = int(result["clients"])
    if int(result["clients_connected"]) != clients:
        failures.append("not all clients established an SSE connection")
    if int(result["clients_with_heartbeat"]) != clients:
        failures.append("not all clients received a data-bearing heartbeat event")
    totals = result["totals"]
    assert isinstance(totals, dict)
    if int(totals["data_events"]) < clients * min_data_events:
        failures.append("the minimum data-event count was not reached")
    if int(totals["malformed_ids"]) > 0:
        failures.append("one or more data events had an invalid or missing id")
    if int(totals["payload_version_mismatches"]) > 0:
        failures.append("event id and payload version did not match")
    if expect_contiguous_ids:
        for metric in ("duplicate_ids", "regressing_ids", "id_gaps"):
            if int(totals[metric]) > 0:
                failures.append(f"contiguous-id contract failed: {metric}={totals[metric]}")
    return failures


def _read_cookie(path: str | None) -> str | None:
    if not path:
        return None
    value = Path(path).read_text(encoding="utf-8").strip()
    if "\n" in value or "\r" in value:
        raise ValueError("cookie file must contain exactly one line")
    return value or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Loopback-only SSE load and fault harness for the isolated pilot."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="open concurrent clients against a local SSE URL")
    load.add_argument("--url", required=True)
    load.add_argument("--clients", type=int, default=96)
    load.add_argument("--duration", type=float, default=60)
    load.add_argument("--stall-timeout", type=float, default=30)
    load.add_argument("--after", type=int, default=0)
    load.add_argument("--cookie-file", help="one-line synthetic local cookie; never logged")
    load.add_argument("--min-data-events", type=int, default=1)
    load.add_argument("--expect-contiguous-ids", action="store_true")
    load.add_argument("--output", type=Path)

    exercise = subparsers.add_parser(
        "exercise", help="run a deterministic in-process local fault scenario"
    )
    exercise.add_argument("--scenario", choices=SCENARIOS, default="clean")
    exercise.add_argument("--clients", type=int, default=96)
    exercise.add_argument("--events", type=int, default=12)
    exercise.add_argument("--duration", type=float, default=2)
    exercise.add_argument("--stall-timeout", type=float, default=0.25)
    exercise.add_argument("--output", type=Path)
    return parser


def _positive(value: int | float, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")


async def async_main(args: argparse.Namespace) -> tuple[dict[str, object], list[str]]:
    _positive(args.clients, "clients")
    _positive(args.duration, "duration")
    _positive(args.stall_timeout, "stall-timeout")
    if args.command == "load":
        _positive(args.min_data_events, "min-data-events")
        cookie = _read_cookie(args.cookie_file)
        result = await run_load(
            LoadOptions(
                url=args.url,
                clients=args.clients,
                duration=args.duration,
                stall_timeout=args.stall_timeout,
                initial_after=max(0, args.after),
                cookie=cookie,
            )
        )
        failures = validate_result(
            result,
            min_data_events=args.min_data_events,
            expect_contiguous_ids=args.expect_contiguous_ids,
        )
        return result, failures
    _positive(args.events, "events")
    result = await exercise_scenario(
        args.scenario,
        clients=args.clients,
        events=args.events,
        duration=args.duration,
        stall_timeout=args.stall_timeout,
    )
    expect_clean = args.scenario in {"clean", "disconnect", "stall"}
    failures = validate_result(
        result,
        min_data_events=args.events if expect_clean else 1,
        expect_contiguous_ids=expect_clean,
    )
    expected_fault_metric = {
        "duplicate": "duplicate_ids",
        "gap": "id_gaps",
        "out_of_order": "regressing_ids",
    }.get(args.scenario)
    if expected_fault_metric:
        totals = result["totals"]
        assert isinstance(totals, dict)
        if int(totals[expected_fault_metric]) == 0:
            failures.append(
                f"injected {args.scenario} fault was not detected as {expected_fault_metric}"
            )
    return result, failures


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result, failures = asyncio.run(async_main(args))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    result["validation_failures"] = failures
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
