#!/usr/bin/env python3
"""Read fixed non-secret Redis QA capacity counters without exposing AUTH."""

from __future__ import annotations

import json
import socket
from pathlib import Path

from sse_qa_ctl import decrypt_systemd_credential


REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6381
REDIS_USERNAME = "sseqa"


def command(sock: socket.socket, *parts: str) -> bytes:
    payload = f"*{len(parts)}\r\n".encode()
    for part in parts:
        encoded = part.encode()
        payload += f"${len(encoded)}\r\n".encode() + encoded + b"\r\n"
    sock.sendall(payload)
    prefix = sock.recv(1)
    line = b""
    while not line.endswith(b"\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise RuntimeError("Redis response ended early")
        line += chunk
    if prefix == b"+":
        return line[:-2]
    if prefix == b"-":
        raise RuntimeError("Redis command rejected")
    if prefix != b"$":
        raise RuntimeError("unexpected Redis response")
    length = int(line[:-2])
    result = b""
    while len(result) < length + 2:
        chunk = sock.recv(length + 2 - len(result))
        if not chunk:
            raise RuntimeError("Redis bulk response ended early")
        result += chunk
    return result[:length]


def main() -> int:
    password = decrypt_systemd_credential("redis_password")
    config: dict[str, int] = {}
    for line in Path("/etc/sse-qa/redis.conf").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] in {"port", "maxclients"}:
            config[fields[0]] = int(fields[1])
        elif len(fields) == 2 and fields[0] == "maxmemory" and fields[1].lower() == "64mb":
            config[fields[0]] = 64 * 1024 * 1024
    if config != {"port": 6381, "maxclients": 32, "maxmemory": 64 * 1024 * 1024}:
        raise RuntimeError("Redis QA limits mismatch")
    with socket.create_connection((REDIS_HOST, REDIS_PORT), timeout=5) as sock:
        if command(sock, "AUTH", REDIS_USERNAME, password) != b"OK":
            raise RuntimeError("Redis authentication response invalid")
        clients = command(sock, "CLIENT", "LIST")
    client_lines = [line for line in clients.splitlines() if line.strip()]
    if not client_lines or any(
        not any(field.startswith(b"id=") for field in line.split())
        for line in client_lines
    ):
        raise RuntimeError("Redis client list response invalid")
    client_count = len(client_lines)
    print(json.dumps({**config, "connected_clients": client_count}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
