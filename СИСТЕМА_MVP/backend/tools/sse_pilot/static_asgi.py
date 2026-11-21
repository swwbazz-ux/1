"""Local-only ASGI wrapper: Django application plus ``/static/**``.

This is a T-016 stand tool, not product code. ``config/settings.py`` keeps
``DEBUG=False`` on the stand and the project has neither WhiteNoise nor an
nginx in front, so a plain ``uvicorn config.asgi:application`` cannot deliver
CSS/JS and the browser never reaches ``new EventSource(...)``.

Usage (T-origin topology, one origin, port 8006)::

    python -m uvicorn tools.sse_pilot.static_asgi:application \
        --host 127.0.0.1 --port 8006 --env-file .env

Only ``GET``/``HEAD`` requests below ``/static/`` are served here; everything
else is delegated unchanged to ``config.asgi:application``. No CORS headers
are added: the browser stays same-origin, exactly as production expects.
"""

import mimetypes
from pathlib import Path

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.staticfiles import finders

from config.asgi import application as django_application


_STATIC_PREFIX = '/static/'


def _resolve(relative_path):
    if not settings.DEBUG and not settings.STATICFILES_DIRS:
        return None
    try:
        return finders.find(relative_path)
    except Exception:
        return None


def _not_found(send):
    body = b'{"ok":false,"error":"static_not_found"}'
    return send({
        'type': 'http.response.start',
        'status': 404,
        'headers': [
            (b'content-type', b'application/json'),
            (b'content-length', str(len(body)).encode('ascii')),
        ],
    })


async def _serve_static(scope, send):
    path = scope.get('path', '')
    if not path.startswith(_STATIC_PREFIX):
        return False
    relative_path = path[len(_STATIC_PREFIX):]
    method = scope.get('method', 'GET').upper()
    if method not in {'GET', 'HEAD'}:
        await send({
            'type': 'http.response.start',
            'status': 405,
            'headers': [
                (b'content-type', b'application/json'),
                (b'allow', b'GET, HEAD'),
            ],
        })
        await send({'type': 'http.response.body', 'body': b''})
        return True

    resolved = await sync_to_async(_resolve, thread_sensitive=True)(relative_path)
    if not resolved:
        await _not_found(send)
        await send({'type': 'http.response.body', 'body': b'{"ok":false,"error":"static_not_found"}'})
        return True

    absolute = Path(resolved).resolve()
    try:
        payload = await sync_to_async(absolute.read_bytes, thread_sensitive=False)()
    except OSError:
        await _not_found(send)
        await send({'type': 'http.response.body', 'body': b'{"ok":false,"error":"static_not_found"}'})
        return True

    content_type = mimetypes.guess_type(str(absolute))[0] or 'application/octet-stream'
    await send({
        'type': 'http.response.start',
        'status': 200,
        'headers': [
            (b'content-type', content_type.encode('latin-1')),
            (b'content-length', str(len(payload)).encode('ascii')),
            (b'cache-control', b'no-store'),
            (b'x-sse-stand-static', b'local-tool'),
        ],
    })
    if method == 'HEAD':
        await send({'type': 'http.response.body', 'body': b''})
    else:
        await send({'type': 'http.response.body', 'body': payload})
    return True


async def application(scope, receive, send):
    if scope.get('type') == 'http':
        served = await _serve_static(scope, send)
        if served:
            return
    await django_application(scope, receive, send)
