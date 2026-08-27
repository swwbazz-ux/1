"""Сбор падений, случившихся на телефоне сотрудника.

Ошибка в приложении у водителя никуда не попадала: человек видел сломанный
экран, а система об этом не знала. При полусотне человек это значит, что
большая часть проблем осталась бы невидимой, и о ходе теста пришлось бы судить
по тишине, которая ничего не значит.

Сюда приходит только то, что нужно для разбора: что упало, где, у кого и на
какой версии. Ни ввода человека, ни содержимого экранов.
"""

from __future__ import annotations

import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .active_role import role_session_state
from .models import ClientErrorReport

# Один и тот же сломанный экран у полусотни людей не должен залить журнал.
MAX_MESSAGE = 500
MAX_SOURCE = 300
MAX_STACK = 2000


def _clip(value, limit):
    return str(value or '').strip()[:limit]


@csrf_exempt
@require_POST
def client_error_report_view(request):
    """Принимает падение с телефона.

    Без проверки CSRF: отчёт уходит из обработчика ошибок, где страница может
    быть уже сломана и токена под рукой нет. Записывается только то, что
    прислали, никаких действий над данными сотрудника это не делает.
    """
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'ok': False, 'error': 'Некорректный отчёт.'}, status=400)

    message = _clip(payload.get('message'), MAX_MESSAGE)
    if not message:
        return JsonResponse({'ok': False, 'error': 'Пустой отчёт.'}, status=400)

    state = role_session_state(request)
    access = state.get('access') if state.get('authenticated') else None

    ClientErrorReport.objects.create(
        employee=access.employee if access else None,
        role_code=(access.role.code if access and access.role_id else '') or _clip(payload.get('role'), 64),
        app_version=_clip(payload.get('appVersion'), 64),
        screen=_clip(payload.get('screen'), 120),
        message=message,
        source=_clip(payload.get('source'), MAX_SOURCE),
        stack=_clip(payload.get('stack'), MAX_STACK),
        user_agent=_clip(request.headers.get('User-Agent'), 300),
    )
    return JsonResponse({'ok': True})
