"""Read systemd credentials for the isolated SSE-QA runtime."""

from __future__ import annotations

import os
import stat
from pathlib import Path

ALLOWED_CREDENTIALS = {
    "django_secret_key": "DJANGO_SECRET_KEY",
    "postgres_app_password": "POSTGRES_PASSWORD",
    "redis_password": "SSE_REDIS_PASSWORD",
    "driver_pin": "SSE_QA_DRIVER_PIN",
    "excavator_pin": "SSE_QA_EXCAVATOR_PIN",
}


class SseQaCredentialError(RuntimeError):
    pass


def read_credential(name: str, *, required: bool = True) -> str:
    environment_name = ALLOWED_CREDENTIALS.get(name)
    if environment_name is None:
        raise SseQaCredentialError("unknown SSE-QA credential")
    directory = os.getenv("CREDENTIALS_DIRECTORY", "").strip()
    if directory:
        path = Path(directory) / name
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                details = os.fstat(descriptor)
                if not stat.S_ISREG(details.st_mode) or details.st_size > 4096:
                    raise SseQaCredentialError("invalid SSE-QA credential file")
                chunks: list[bytes] = []
                remaining = 4097
                while remaining:
                    chunk = os.read(descriptor, min(remaining, 4097))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
            finally:
                os.close(descriptor)
        except OSError as exc:
            if not required and not path.exists():
                return ""
            raise SseQaCredentialError("SSE-QA credential is unavailable") from exc
        try:
            value = raw.decode("utf-8")
        except UnicodeError as exc:
            raise SseQaCredentialError("SSE-QA credential is not UTF-8") from exc
    else:
        # Used only by the short-lived installer/bootstrap process. Persistent
        # services always receive CREDENTIALS_DIRECTORY from systemd.
        value = os.getenv(environment_name, "")
    if not value:
        if required:
            raise SseQaCredentialError("SSE-QA credential is empty")
        return ""
    if any(character in value for character in "\x00\r\n"):
        raise SseQaCredentialError("SSE-QA credential contains a forbidden character")
    return value
