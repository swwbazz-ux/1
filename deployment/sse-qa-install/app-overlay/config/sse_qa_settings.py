"""Settings overlay that consumes decrypted systemd credentials in memory."""

from urllib.parse import quote

from .settings import *  # noqa: F403
from .sse_qa_credentials import read_credential


SECRET_KEY = read_credential("django_secret_key")
DATABASES["default"]["PASSWORD"] = read_credential("postgres_app_password")  # noqa: F405
SSE_REDIS_URL = (
    "redis://sseqa:"
    + quote(read_credential("redis_password"), safe="")
    + "@127.0.0.1:6381/0"
)
SSE_QA_DRIVER_PIN = read_credential("driver_pin", required=False)
SSE_QA_EXCAVATOR_PIN = read_credential("excavator_pin", required=False)
