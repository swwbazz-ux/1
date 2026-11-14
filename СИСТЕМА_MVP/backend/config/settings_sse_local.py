"""Local-only SSE stand settings.

This module is an adaptation for the isolated T-014 working copy. It is not
part of the transferred product settings and must not be copied to production.
It reuses ``config.settings`` and enables the SSE kill switch only for the
local stand, with values taken from the local ``.env``.
"""

import os

from .settings import *  # noqa: F401,F403


def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, '') or default)
    except ValueError:
        return default


SSE_PILOT_ENABLED = _flag('SSE_PILOT_ENABLED', True)
SSE_HEARTBEAT_SECONDS = _int('SSE_HEARTBEAT_SECONDS', 10)
SSE_SAFETY_SCAN_SECONDS = _int('SSE_SAFETY_SCAN_SECONDS', 5)
SSE_REAUTH_SECONDS = _int('SSE_REAUTH_SECONDS', 10)
SSE_STREAM_MAX_SECONDS = _int('SSE_STREAM_MAX_SECONDS', 900)
SSE_BATCH_SIZE = _int('SSE_BATCH_SIZE', 200)
SSE_REDIS_URL = os.getenv('SSE_REDIS_URL', '')
SSE_REDIS_NAMESPACE = os.getenv('SSE_REDIS_NAMESPACE', 'accounting-mvp:sse:local')
SSE_ALLOWED_ROLE_CODES = frozenset({'driver', 'excavator_operator'})
