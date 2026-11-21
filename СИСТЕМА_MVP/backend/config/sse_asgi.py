"""Restricted ASGI entrypoint for the isolated SSE service.

Run this application in a separate async process and route only
``/realtime/stream/`` to it. Ordinary Django traffic remains on the existing
WSGI service.
"""

import os

from django.core.asgi import get_asgi_application


os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django_application = get_asgi_application()


async def application(scope, receive, send):
    if scope.get('type') != 'http' or scope.get('path') != '/realtime/stream/':
        await send({
            'type': 'http.response.start',
            'status': 404,
            'headers': [(b'content-type', b'application/json')],
        })
        await send({
            'type': 'http.response.body',
            'body': b'{"ok":false,"error":"sse_endpoint_only"}',
        })
        return
    await django_application(scope, receive, send)
