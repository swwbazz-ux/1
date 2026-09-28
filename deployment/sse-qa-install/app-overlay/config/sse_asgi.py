"""Restricted ASGI entrypoint with an isolated QA event-loop sampler."""

import os

from django.core.asgi import get_asgi_application

from .sse_event_loop_lag import ensure_sampler_started


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django_application = get_asgi_application()


async def application(scope, receive, send):
    if scope.get("type") != "http" or scope.get("path") != "/realtime/stream/":
        await send({
            "type": "http.response.start",
            "status": 404,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({
            "type": "http.response.body",
            "body": b'{"ok":false,"error":"sse_endpoint_only"}',
        })
        return
    ensure_sampler_started()
    await django_application(scope, receive, send)

