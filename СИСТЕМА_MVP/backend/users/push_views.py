"""Обработчики подписки на уведомления и выдачи их содержимого."""

from __future__ import annotations

import json

from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .active_role import role_session_state
from .models import PushNotification, WebPushSubscription
from .webpush import notify_employee, public_key_for_browser, push_is_configured


# Сколько уведомлений отдаём за раз: телефон всё равно покажет последние.
PENDING_LIMIT = 5
# По этой пометке экран настройки отличает проверку от рабочих событий.
TEST_NOTIFICATION_KIND = 'setup_test'


def _current_access(request):
    state = role_session_state(request)
    return state.get('access') if state.get('authenticated') else None


def _payload(request):
    if request.content_type and 'application/json' in request.content_type:
        try:
            return json.loads(request.body.decode('utf-8') or '{}')
        except (ValueError, UnicodeDecodeError):
            return {}
    return request.POST


@require_GET
def push_public_key_view(request):
    """Ключ, с которым телефон оформляет подписку."""
    return JsonResponse({
        'ok': True,
        'configured': push_is_configured(),
        'public_key': public_key_for_browser(),
    })


@require_POST
def push_subscribe_view(request):
    access = _current_access(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нужно войти в приложение.'}, status=401)

    payload = _payload(request)
    endpoint = str(payload.get('endpoint') or '').strip()
    if not endpoint.startswith('https://'):
        return JsonResponse({'ok': False, 'error': 'Некорректный адрес подписки.'}, status=400)

    keys = payload.get('keys') or {}
    if not isinstance(keys, dict):
        keys = {}
    # Один и тот же телефон может быть переиспользован другим сотрудником:
    # адрес подписки уникален, поэтому просто переназначаем владельца.
    WebPushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            'employee': access.employee,
            'p256dh': str(keys.get('p256dh') or '')[:200],
            'auth': str(keys.get('auth') or '')[:100],
            'role_code': access.role.code if access.role_id else '',
            'user_agent': request.headers.get('User-Agent', '')[:300],
            'is_active': True,
            'failure_count': 0,
        },
    )
    return JsonResponse({'ok': True})


@require_POST
def push_unsubscribe_view(request):
    payload = _payload(request)
    endpoint = str(payload.get('endpoint') or '').strip()
    if endpoint:
        WebPushSubscription.objects.filter(endpoint=endpoint).update(is_active=False)
    return JsonResponse({'ok': True})


@require_GET
def push_pending_view(request):
    """Содержимое уведомлений, которые телефон ещё не показал.

    Сам push приходит пустым, поэтому текст приложение забирает отсюда — уже
    по своему сеансу, без участия чужого push-сервиса.
    """
    access = _current_access(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нужно войти в приложение.'}, status=401)

    pending = list(
        PushNotification.objects
        .filter(employee=access.employee, shown_at__isnull=True)
        .order_by('-created_at')[:PENDING_LIMIT]
    )
    unread_total = PushNotification.objects.filter(
        employee=access.employee,
        shown_at__isnull=True,
    ).count()
    response = JsonResponse({
        'ok': True,
        'badge': unread_total,
        # Фоновому модулю негде взять CSRF-токен: страницы у него нет. Без него
        # отметка о показе отбивалась, очередь не пустела, и телефон при каждом
        # событии заново показывал всё старое.
        'csrf_token': get_token(request),
        'notifications': [
            {
                'id': item.id,
                'title': item.title,
                'body': item.body,
                'url': item.url,
                'tag': item.tag or f'notification-{item.id}',
                'kind': item.kind,
            }
            for item in pending
        ],
    })
    response['Cache-Control'] = 'private, no-store'
    return response


@require_POST
def push_mark_shown_view(request):
    access = _current_access(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нужно войти в приложение.'}, status=401)

    payload = _payload(request)
    raw_ids = payload.get('ids') or []
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    ids = []
    for value in raw_ids:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue

    queryset = PushNotification.objects.filter(
        employee=access.employee,
        shown_at__isnull=True,
    )
    if ids:
        queryset = queryset.filter(id__in=ids)
    queryset.update(shown_at=timezone.now())

    unread_total = PushNotification.objects.filter(
        employee=access.employee,
        shown_at__isnull=True,
    ).count()
    return JsonResponse({'ok': True, 'badge': unread_total})


@require_POST
def push_test_view(request):
    """Отправляет сотруднику проверочное уведомление.

    Нужно на экране первичной настройки: человек должен своими глазами увидеть,
    что уведомление доходит, а не узнать об этом в первый рабочий день. Идёт
    тем же путём, что и боевые — сервер, push-сервис, телефон, — поэтому
    проверяет всю цепочку целиком, а не только разрешение в браузере.
    """
    access = _current_access(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нужно войти в приложение.'}, status=401)

    if not push_is_configured():
        return JsonResponse({
            'ok': False,
            'configured': False,
            'error': 'Уведомления не настроены на сервере.',
        }, status=503)

    # Прошлые непоказанные проверки только мешали бы: приложение ждёт,
    # когда очередь опустеет, и старая запись держала бы её вечно.
    PushNotification.objects.filter(
        employee=access.employee,
        kind=TEST_NOTIFICATION_KIND,
        shown_at__isnull=True,
    ).update(shown_at=timezone.now())

    delivered = notify_employee(
        access.employee,
        title='Проверка уведомлений',
        body='Так будет выглядеть сообщение о работе. Настройка завершена.',
        kind=TEST_NOTIFICATION_KIND,
        tag=TEST_NOTIFICATION_KIND,
    )
    if not delivered:
        return JsonResponse({
            'ok': False,
            'configured': True,
            'delivered': 0,
            'error': 'Телефон не подписан на уведомления.',
        }, status=409)

    return JsonResponse({'ok': True, 'configured': True, 'delivered': delivered})
