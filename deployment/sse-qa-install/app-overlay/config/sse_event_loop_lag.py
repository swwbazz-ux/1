"""Minimal same-loop lag sampler for the isolated SSE QA worker."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path


_task: asyncio.Task | None = None


def _interval() -> float:
    try:
        value = float(os.getenv("SSE_EVENT_LOOP_LAG_INTERVAL_SECONDS", "1"))
    except ValueError:
        return 1.0
    return min(max(value, 0.1), 60.0)


def _path() -> Path | None:
    raw = os.getenv("SSE_EVENT_LOOP_LAG_PATH", "").strip()
    return Path(raw) if raw else None


def _append_sample(path: Path, payload: dict[str, object]) -> None:
    line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o640)
    try:
        os.write(descriptor, line.encode("utf-8"))
    finally:
        os.close(descriptor)


async def _sample_forever(path: Path, interval: float) -> None:
    loop = asyncio.get_running_loop()
    expected = loop.time() + interval
    while True:
        await asyncio.sleep(max(0.0, expected - loop.time()))
        observed = loop.time()
        _append_sample(path, {
            "schema": 1,
            "unix_ns": time.time_ns(),
            "interval_ms": round(interval * 1000, 3),
            "lag_ms": round(max(0.0, observed - expected) * 1000, 3),
            "pid": os.getpid(),
        })
        expected += interval
        if observed - expected > interval:
            expected = observed + interval


def ensure_sampler_started() -> None:
    global _task
    path = _path()
    if path is None or (_task is not None and not _task.done()):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _task = asyncio.create_task(_sample_forever(path, _interval()))

