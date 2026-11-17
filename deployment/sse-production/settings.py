"""Shared WSGI and ASGI settings for the production event stream."""

import os

from .settings import *  # noqa: F401,F403
from .settings import DATABASES as BASE_DATABASES, _env_bool


SSE_PILOT_ENABLED = _env_bool("SSE_PILOT_ENABLED")
SSE_STREAM_SERVING_ENABLED = _env_bool("SSE_ASGI_PROCESS")
SSE_REDIS_URL = os.getenv("SSE_REDIS_URL", "").strip()
SSE_REDIS_NAMESPACE = "accounting-mvp:sse:production"

if SSE_STREAM_SERVING_ENABLED:
    DATABASES = {
        alias: {**database, "CONN_MAX_AGE": 0}
        for alias, database in BASE_DATABASES.items()
    }
