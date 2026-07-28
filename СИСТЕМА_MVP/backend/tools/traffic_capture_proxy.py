#!/usr/bin/env python
"""Local reverse proxy that records traffic sizes without storing payloads."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import http.client
import json
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-port", type=int, default=8001)
    parser.add_argument("--target-port", type=int, default=8000)
    parser.add_argument("--log", type=Path, required=True)
    return parser.parse_args()


class TrafficLog:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def append(self, row: dict) -> None:
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        with self.lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    traffic_log: TrafficLog
    target_port: int

    def log_message(self, format: str, *args) -> None:
        return

    def do_GET(self) -> None:
        self._proxy()

    def do_HEAD(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()

    def do_PATCH(self) -> None:
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def _proxy(self) -> None:
        started = time.perf_counter()
        request_length = int(self.headers.get("Content-Length") or 0)
        request_body = self.rfile.read(request_length) if request_length else b""
        request_header_bytes = len(
            f"{self.command} {self.path} {self.request_version}\r\n".encode(
                "utf-8"
            )
        )
        request_header_bytes += sum(
            len(f"{key}: {value}\r\n".encode("utf-8"))
            for key, value in self.headers.items()
        )
        request_header_bytes += 2
        incoming_host = self.headers.get("Host", "")
        host_name = incoming_host.rsplit(":", 1)[0]
        forward_headers = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                continue
            if key.lower() in {"origin", "referer"}:
                value = value.replace(
                    f":{self.server.server_port}",
                    f":{self.target_port}",
                )
            forward_headers[key] = value
        forward_headers["Host"] = f"{host_name}:{self.target_port}"
        if request_body:
            forward_headers["Content-Length"] = str(len(request_body))

        status = 502
        reason = "Bad Gateway"
        response_headers: list[tuple[str, str]] = []
        response_body = b""
        error = ""
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.target_port,
            timeout=30,
        )
        try:
            connection.request(
                self.command,
                self.path,
                body=request_body or None,
                headers=forward_headers,
            )
            response = connection.getresponse()
            status = int(response.status)
            reason = response.reason
            response_headers = response.getheaders()
            response_body = response.read()
        except Exception as exc:  # noqa: BLE001 - capture failures in evidence.
            error = f"{type(exc).__name__}:{exc}"
            response_body = error.encode("utf-8")
        finally:
            connection.close()

        self.send_response(status, reason)
        for key, value in response_headers:
            lower = key.lower()
            if lower in HOP_BY_HOP or lower == "content-length":
                continue
            if lower == "location":
                value = value.replace(
                    f":{self.target_port}/",
                    f":{self.server.server_port}/",
                )
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response_body)

        content_type = next(
            (
                value
                for key, value in response_headers
                if key.lower() == "content-type"
            ),
            "",
        )
        response_gzip_bytes = (
            len(gzip.compress(response_body, compresslevel=6, mtime=0))
            if response_body
            else 0
        )
        response_header_bytes = len(
            f"{self.protocol_version} {status} {reason}\r\n".encode("utf-8")
        )
        response_header_bytes += sum(
            len(f"{key}: {value}\r\n".encode("utf-8"))
            for key, value in response_headers
            if key.lower() not in HOP_BY_HOP
            and key.lower() != "content-length"
        )
        response_header_bytes += len(
            f"Content-Length: {len(response_body)}\r\n\r\n".encode("ascii")
        )
        self.traffic_log.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "host": host_name,
                "method": self.command,
                "path": self.path,
                "status": status,
                "request_body_bytes": len(request_body),
                "request_header_bytes": request_header_bytes,
                "response_body_bytes": len(response_body),
                "response_gzip_estimate_bytes": response_gzip_bytes,
                "response_header_bytes": response_header_bytes,
                "content_type": content_type,
                "elapsed_ms": round(
                    (time.perf_counter() - started) * 1000,
                    3,
                ),
                "response_sha256": hashlib.sha256(response_body).hexdigest(),
                "error": error,
            }
        )


def main() -> int:
    args = parse_args()
    if args.listen_port != 8001 or args.target_port != 8000:
        raise RuntimeError("Only local 8001 -> 8000 capture is allowed.")
    traffic_log = TrafficLog(args.log)
    ProxyHandler.traffic_log = traffic_log
    ProxyHandler.target_port = args.target_port
    server = ThreadingHTTPServer(("127.0.0.1", args.listen_port), ProxyHandler)
    print(
        f"Traffic capture proxy: 127.0.0.1:{args.listen_port}"
        f" -> 127.0.0.1:{args.target_port}"
    )
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
