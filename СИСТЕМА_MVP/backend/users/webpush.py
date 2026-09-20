"""Отправка push-уведомлений на телефоны сотрудников.

Уведомление отправляется без содержимого: сервер лишь «будит» телефон, а текст
приложение забирает отдельным запросом. Так не нужен пакет для шифрования
полезной нагрузки — хватает cryptography и PyJWT, которые уже стоят, — и текст
уведомления не проходит через чужой push-сервис.
"""

from __future__ import annotations

import base64
import logging
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings
from django.utils import timezone


logger = logging.getLogger(__name__)

# Токен подписи живёт 12 часов: спецификация разрешает не больше 24.
VAPID_TOKEN_TTL_SECONDS = 12 * 60 * 60
# Сколько push-сервис хранит уведомление, если телефон офлайн.
PUSH_TTL_SECONDS = 6 * 60 * 60
PUSH_REQUEST_TIMEOUT_SECONDS = 10
# Фоновое уведомление Диспетчера не должно надолго задерживать
# боевое действие, если push-провайдер временно недоступен.
ROLE_PUSH_REQUEST_TIMEOUT_SECONDS = 3
# Столько неудач подряд — и подписка считается мёртвой.
MAX_SUBSCRIPTION_FAILURES = 5


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _load_private_key():
    raw = (getattr(settings, 'WEBPUSH_VAPID_PRIVATE_KEY', '') or '').strip()
    if not raw:
        return None
    padding = '=' * (-len(raw) % 4)
    try:
        secret = base64.urlsafe_b64decode(raw + padding)
    except (ValueError, TypeError):
        logger.warning('WEBPUSH_VAPID_PRIVATE_KEY не удалось декодировать.')
        return None
    try:
        return ec.derive_private_key(
            int.from_bytes(secret, 'big'),
            ec.SECP256R1(),
        )
    except ValueError:
        logger.warning('WEBPUSH_VAPID_PRIVATE_KEY не является ключом P-256.')
        return None


def public_key_for_browser() -> str:
    """Открытый ключ в том виде, в каком его ждёт телефон при подписке."""
    configured = (getattr(settings, 'WEBPUSH_VAPID_PUBLIC_KEY', '') or '').strip()
    if configured:
        return configured
    private_key = _load_private_key()
    if not private_key:
        return ''
    return _b64(
        private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
    )


def push_is_configured() -> bool:
    return bool(_load_private_key() and public_key_for_browser())


def _authorization_header(endpoint: str) -> str | None:
    private_key = _load_private_key()
    if not private_key:
        return None
    parsed = urlparse(endpoint)
    audience = f'{parsed.scheme}://{parsed.netloc}'
    contact = (
        getattr(settings, 'WEBPUSH_CONTACT', '') or 'mailto:admin@driverform.ru'
    )
    token = jwt.encode(
        {
            'aud': audience,
            'exp': int(time.time()) + VAPID_TOKEN_TTL_SECONDS,
            'sub': contact,
        },
        private_key,
        algorithm='ES256',
    )
    return f'vapid t={token},k={public_key_for_browser()}'


def _deliver(
    endpoint: str,
    *,
    timeout_seconds: int = PUSH_REQUEST_TIMEOUT_SECONDS,
) -> tuple[bool, int]:
    """Возвращает (доставлено, http-код). Код 404/410 означает мёртвую подписку."""
    authorization = _authorization_header(endpoint)
    if not authorization:
        return False, 0
    request = urllib.request.Request(
        endpoint,
        data=b'',
        method='POST',
        headers={
            'Authorization': authorization,
            'TTL': str(PUSH_TTL_SECONDS),
            'Content-Length': '0',
            'Urgency': 'high',
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout_seconds
        ) as response:
            return 200 <= response.status < 300, response.status
    except urllib.error.HTTPError as error:
        return False, error.code
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        logger.warning('Push не отправлен на %s: %s', urlparse(endpoint).netloc, error)
        return False, 0


def notify_employee(employee, *, title, body, url='', tag='', kind='') -> int:
    """Кладёт уведомление в очередь сотрудника и будит его телефоны.

    Возвращает число телефонов, до которых удалось достучаться. Ошибки отправки
    не пробрасываются: уведомление не должно ломать рабочее действие.
    """
    from .models import PushNotification, WebPushSubscription

    PushNotification.objects.create(
        employee=employee,
        title=title,
        body=body,
        url=url,
        tag=tag or kind,
        kind=kind,
    )
    native_delivered = 0
    try:
        from core.models import OperationalStateVersion
        from .native_push import notify_employee_devices

        state_version = (
            OperationalStateVersion.objects
            .filter(key='production')
            .values_list('version', flat=True)
            .first()
        )
        native_delivered = notify_employee_devices(
            employee,
            kind=kind,
            state_version=str(state_version or ''),
        )
    except Exception:
        # Push — вспомогательный канал. Его сбой никогда не должен
        # откатывать погрузку, назначение или другое рабочее действие.
        logger.exception('Native push не отправлен для сотрудника %s.', employee.pk)
    if not push_is_configured():
        return native_delivered

    delivered = 0
    subscriptions = list(
        WebPushSubscription.objects.filter(employee=employee, is_active=True)
    )
    for subscription in subscriptions:
        ok, status = _deliver(subscription.endpoint)
        if ok:
            delivered += 1
            subscription.failure_count = 0
            subscription.last_success_at = timezone.now()
            subscription.save(update_fields=['failure_count', 'last_success_at'])
            continue
        # Push-сервис прямо говорит, что подписки больше нет.
        if status in (404, 410):
            subscription.is_active = False
            subscription.save(update_fields=['is_active'])
            continue
        subscription.failure_count += 1
        if subscription.failure_count >= MAX_SUBSCRIPTION_FAILURES:
            subscription.is_active = False
        subscription.save(update_fields=['failure_count', 'is_active'])
    logger.info(
        'Push %s для сотрудника %s: доставлено %s из %s',
        kind or 'notification',
        employee.pk,
        delivered,
        len(subscriptions),
    )
    return delivered + native_delivered


def notify_role_web_subscribers(
    role_code: str,
    *,
    title: str,
    body: str,
    url: str = '',
    tag: str = '',
    kind: str = '',
) -> int:
    """Будит только Web Push-подписки указанной роли.

    Этот путь намеренно не трогает native-устройства полевых ролей.
    Текст кладётся в защищённую очередь каждого подписанного сотрудника,
    а внешний push-сервис получает только пустой wake-up.
    """
    from .models import PushNotification, WebPushSubscription

    role_code = str(role_code or '').strip()
    if not role_code:
        return 0

    subscriptions = list(
        WebPushSubscription.objects
        .filter(role_code=role_code, is_active=True)
        .select_related('employee')
        .order_by('id')
    )
    if not subscriptions:
        return 0

    employee_ids = {subscription.employee_id for subscription in subscriptions}
    PushNotification.objects.bulk_create([
        PushNotification(
            employee_id=employee_id,
            title=title,
            body=body,
            url=url,
            tag=tag or kind,
            kind=kind,
        )
        for employee_id in employee_ids
    ])
    if not push_is_configured():
        return 0

    def deliver(subscription):
        ok, status = _deliver(
            subscription.endpoint,
            timeout_seconds=ROLE_PUSH_REQUEST_TIMEOUT_SECONDS,
        )
        return subscription, ok, status

    worker_count = min(4, len(subscriptions))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = list(executor.map(deliver, subscriptions))

    delivered = 0
    now = timezone.now()
    for subscription, ok, status in results:
        if ok:
            delivered += 1
            subscription.failure_count = 0
            subscription.last_success_at = now
            subscription.save(update_fields=['failure_count', 'last_success_at'])
            continue
        if status in (404, 410):
            subscription.is_active = False
            subscription.save(update_fields=['is_active'])
            continue
        subscription.failure_count += 1
        if subscription.failure_count >= MAX_SUBSCRIPTION_FAILURES:
            subscription.is_active = False
        subscription.save(update_fields=['failure_count', 'is_active'])

    logger.info(
        'Web Push %s для роли %s: доставлено %s из %s',
        kind or 'notification',
        role_code,
        delivered,
        len(subscriptions),
    )
    return delivered
