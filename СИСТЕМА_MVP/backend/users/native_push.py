"""Нативный push-канал мобильных приложений.

Сервер отправляет только пустой data-сигнал: телефон просыпается и
запускает уже существующую сверку с сервером. Рабочие данные через
внешнего push-провайдера не передаются.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import jwt
from django.conf import settings
from django.utils import timezone


logger = logging.getLogger(__name__)

FCM_SCOPE = 'https://www.googleapis.com/auth/firebase.messaging'
FCM_DEFAULT_TOKEN_URI = 'https://oauth2.googleapis.com/token'
FCM_REQUEST_TIMEOUT_SECONDS = 10
NATIVE_APP_IDS_BY_ROLE = {
    'driver': frozenset({'ru.copperresources.driver', 'ru.copperresources.driver.qa'}),
    'excavator_operator': frozenset({
        'ru.copperresources.excavator',
        'ru.copperresources.excavator.qa',
    }),
}
MAX_DEVICE_FAILURES = 5
TOKEN_REFRESH_MARGIN_SECONDS = 60

_access_token_lock = threading.Lock()
_access_token = ''
_access_token_expires_at = 0.0


def _service_account() -> dict:
    filename = (getattr(settings, 'FCM_SERVICE_ACCOUNT_FILE', '') or '').strip()
    if not filename:
        return {}
    try:
        payload = json.loads(Path(filename).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError) as error:
        logger.warning('FCM service account не загружен: %s', error.__class__.__name__)
        return {}
    if not isinstance(payload, dict):
        return {}
    if not payload.get('client_email') or not payload.get('private_key'):
        logger.warning('FCM service account не содержит обязательных полей.')
        return {}
    return payload


def _project_id(service_account: dict) -> str:
    configured = (getattr(settings, 'FCM_PROJECT_ID', '') or '').strip()
    return configured or str(service_account.get('project_id') or '').strip()


def native_push_is_configured() -> bool:
    service_account = _service_account()
    return bool(service_account and _project_id(service_account))


def _oauth_access_token(
    service_account: dict,
    *,
    timeout_seconds=FCM_REQUEST_TIMEOUT_SECONDS,
) -> str:
    global _access_token, _access_token_expires_at

    now = time.time()
    if _access_token and now < _access_token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
        return _access_token

    with _access_token_lock:
        now = time.time()
        if _access_token and now < _access_token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
            return _access_token

        token_uri = str(service_account.get('token_uri') or FCM_DEFAULT_TOKEN_URI).strip()
        assertion = jwt.encode(
            {
                'iss': service_account['client_email'],
                'sub': service_account['client_email'],
                'aud': token_uri,
                'scope': FCM_SCOPE,
                'iat': int(now),
                'exp': int(now) + 3600,
            },
            service_account['private_key'],
            algorithm='RS256',
        )
        request = urllib.request.Request(
            token_uri,
            data=urllib.parse.urlencode({
                'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
                'assertion': assertion,
            }).encode('ascii'),
            method='POST',
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response_payload = json.loads(response.read().decode('utf-8'))
        access_token = str(response_payload.get('access_token') or '').strip()
        if not access_token:
            raise ValueError('FCM OAuth response has no access token')
        expires_in = max(120, int(response_payload.get('expires_in') or 3600))
        _access_token = access_token
        _access_token_expires_at = now + expires_in
        return access_token


def _fcm_error_code(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError, TypeError):
        return ''
    error = payload.get('error') if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return ''
    for detail in error.get('details') or []:
        if isinstance(detail, dict) and detail.get('errorCode'):
            return str(detail['errorCode']).upper()
    return str(error.get('status') or '').upper()


def native_app_ids_for_role(role_code):
    return NATIVE_APP_IDS_BY_ROLE.get(str(role_code or ''), frozenset())


def _deliver_fcm(
    device,
    *,
    kind: str,
    state_version: str,
    extra_data=None,
    request_timeout_seconds=FCM_REQUEST_TIMEOUT_SECONDS,
) -> tuple[bool, bool]:
    """Возвращает (delivered, token_is_dead)."""
    service_account = _service_account()
    project_id = _project_id(service_account)
    if not service_account or not project_id:
        return False, False
    try:
        access_token = _oauth_access_token(
            service_account,
            timeout_seconds=request_timeout_seconds,
        )
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        TypeError,
        jwt.PyJWTError,
    ) as error:
        logger.warning('FCM OAuth не выдал токен: %s', error.__class__.__name__)
        return False, False

    data = {
        'kind': str(kind or 'operational_state'),
        'version': str(state_version or ''),
    }
    for key, value in (extra_data or {}).items():
        key = str(key or '').strip()
        value = str(value or '').strip()
        if key and len(key) <= 32 and value and len(value) <= 128:
            data[key] = value
    try:
        body = json.dumps({
            'message': {
                'token': device.token,
                'data': data,
                'android': {'priority': 'high'},
            },
        }, ensure_ascii=True, separators=(',', ':')).encode('utf-8')
        url = (
            'https://fcm.googleapis.com/v1/projects/'
            + urllib.parse.quote(project_id, safe='')
            + '/messages:send'
        )
        request = urllib.request.Request(
            url,
            data=body,
            method='POST',
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json; charset=utf-8',
            },
        )
        with urllib.request.urlopen(request, timeout=request_timeout_seconds) as response:
            return 200 <= response.status < 300, False
    except urllib.error.HTTPError as error:
        raw = error.read()
        code = _fcm_error_code(raw)
        return False, error.code == 404 or code == 'UNREGISTERED'
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError) as error:
        logger.warning('FCM push не отправлен: %s', error.__class__.__name__)
        return False, False


def notify_employee_devices(
    employee,
    *,
    kind: str = '',
    state_version: str = '',
    app_ids=None,
    extra_data=None,
    max_devices=None,
    request_timeout_seconds=FCM_REQUEST_TIMEOUT_SECONDS,
    stop_after_first_success=False,
    penalize_transient_failures=True,
) -> int:
    """Разбудить все активные native-устройства сотрудника."""
    from .models import NativePushDevice

    if not native_push_is_configured():
        return 0
    devices_query = NativePushDevice.objects.filter(
        employee=employee,
        provider=NativePushDevice.Provider.FCM,
        is_active=True,
    )
    if app_ids is not None:
        app_ids = {str(app_id) for app_id in app_ids if app_id}
        if not app_ids:
            return 0
        devices_query = devices_query.filter(app_id__in=app_ids)
    devices_query = devices_query.order_by('-last_success_at', '-created_at')
    if max_devices is not None:
        max_devices = max(1, min(int(max_devices), 10))
        devices_query = devices_query[:max_devices]
    devices = list(devices_query)
    delivered = 0
    for device in devices:
        ok, token_is_dead = _deliver_fcm(
            device,
            kind=kind,
            state_version=state_version,
            extra_data=extra_data,
            request_timeout_seconds=max(
                1,
                min(int(request_timeout_seconds), FCM_REQUEST_TIMEOUT_SECONDS),
            ),
        )
        if ok:
            delivered += 1
            device.failure_count = 0
            device.last_success_at = timezone.now()
            device.save(update_fields=['failure_count', 'last_success_at'])
            if stop_after_first_success:
                break
            continue
        if token_is_dead:
            device.is_active = False
            device.save(update_fields=['is_active'])
            continue
        if not penalize_transient_failures:
            continue
        device.failure_count += 1
        if device.failure_count >= MAX_DEVICE_FAILURES:
            device.is_active = False
        device.save(update_fields=['failure_count', 'is_active'])
    logger.info(
        'Native push %s для сотрудника %s: доставлено %s из %s',
        kind or 'operational_state', employee.pk, delivered, len(devices),
    )
    return delivered
