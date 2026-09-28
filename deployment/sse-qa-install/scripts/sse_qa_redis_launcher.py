#!/usr/bin/python3
"""Launch Redis with an ACL held only in an inherited anonymous memfd."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path


REDIS_SERVER = "/usr/bin/redis-server"
REDIS_CONFIG = "/etc/sse-qa/redis.conf"
PASSWORD_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")


def read_systemd_credential(name: str) -> str:
    directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if not directory:
        raise RuntimeError("systemd credential directory is unavailable")
    path = Path(directory) / name
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > 4096:
            raise RuntimeError("invalid systemd credential file")
        raw = os.read(descriptor, 4097)
        if os.read(descriptor, 1):
            raise RuntimeError("systemd credential is too large")
    finally:
        os.close(descriptor)
    try:
        value = raw.decode("utf-8")
    except UnicodeError as exc:
        raise RuntimeError("systemd credential is not UTF-8") from exc
    if not PASSWORD_PATTERN.fullmatch(value):
        raise RuntimeError("invalid Redis credential")
    return value


def build_acl(password: str) -> bytes:
    return (
        "user default off\n"
        f"user sseqa on >{password} "
        "~accounting-mvp:sse:qa:* &accounting-mvp:sse:qa:* "
        "+ping +client +subscribe +unsubscribe +psubscribe +punsubscribe +publish\n"
    ).encode("utf-8")


def main() -> int:
    if not hasattr(os, "memfd_create"):
        raise RuntimeError("memfd_create is unavailable")
    password = read_systemd_credential("redis_password")
    descriptor = os.memfd_create("sse-qa-redis-acl", flags=0)
    os.fchmod(descriptor, 0o400)
    os.set_inheritable(descriptor, True)
    payload = memoryview(build_acl(password))
    while payload:
        written = os.write(descriptor, payload)
        if written <= 0:
            raise OSError("short Redis ACL memfd write")
        payload = payload[written:]
    os.lseek(descriptor, 0, os.SEEK_SET)
    acl_path = f"/proc/self/fd/{descriptor}"
    os.execv(
        REDIS_SERVER,
        [
            REDIS_SERVER,
            REDIS_CONFIG,
            "--supervised", "systemd",
            "--aclfile", acl_path,
        ],
    )
    raise RuntimeError("Redis exec unexpectedly returned")


if __name__ == "__main__":
    raise SystemExit(main())
