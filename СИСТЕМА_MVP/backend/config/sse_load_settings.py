"""Fail-closed settings used only by the disposable local SSE load harness."""

import os

from tools.sse_pilot.disposable_db import validate_disposable_database


_database, _root = validate_disposable_database(
    os.environ.get("SSE_LOAD_DISPOSABLE_DB", ""),
    os.environ.get("SSE_LOAD_DISPOSABLE_ROOT", ""),
    require_database_missing=False,
)

# Prevent values from the normal .env from selecting an external database or
# external delivery services before importing the regular application settings.
os.environ["DJANGO_DB_ENGINE"] = "sqlite"
os.environ["SSE_REDIS_URL"] = ""
os.environ["DJANGO_FCM_SERVICE_ACCOUNT_FILE"] = ""
os.environ["DJANGO_FCM_PROJECT_ID"] = ""
os.environ["DJANGO_WEBPUSH_VAPID_PRIVATE_KEY"] = ""
os.environ["DJANGO_WEBPUSH_VAPID_PUBLIC_KEY"] = ""

from .settings import *  # noqa: E402,F401,F403


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": _database,
        "CONN_MAX_AGE": 0,
    }
}
ALLOWED_HOSTS = ["127.0.0.1", "localhost", ".localhost"]
SSE_PILOT_ENABLED = True
SSE_REDIS_URL = ""
SSE_REDIS_NAMESPACE = "accounting-mvp:sse:disposable-business-load"
