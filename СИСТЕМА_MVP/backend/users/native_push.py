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


def _oauth_access_token(service_account: dict) -> str:
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
        with urllib.request.urlopen(request, timeout=FCM_REQUEST_TIMEOUT_SECONDS) as response:
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


def _deliver_fcm(device, *, kind: str, state_version: str) -> tuple[bool, bool]:
    """Возвращает (delivered, token_is_dead)."""
    service_account = _service_account()
    project_id = _project_id(service_account)
    if not service_account or not project_id:
        return False, False
    try:
        access_token = _oauth_access_token(service_account)
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

    try:
        body = json.dumps({
            'message': {
                'token': device.token,
                'data': {
                    'kind': str(kind or 'operational_state'),
                    'version': str(state_version or ''),
                },
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
        with urllib.request.urlopen(request, timeout=FCM_REQUEST_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300, False
    except urllib.error.HTTPError as error:
        raw = error.read()
        code = _fcm_error_code(raw)
        return False, error.code == 404 or code == 'UNREGISTERED'
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError) as error:
        logger.warning('FCM push не отправлен: %s', error.__class__.__name__)
        return False, False


def notify_employee_devices(employee, *, kind: str = '', state_version: str = '') -> int:
    """Разбудить все активные native-устройства сотрудника."""
    from .models import NativePushDevice

    if not native_push_is_configured():
        return 0
    devices = list(NativePushDevice.objects.filter(
        employee=employee,
        provider=NativePushDevice.Provider.FCM,
        is_active=True,
    ))
    delivered = 0
    for device in devices:
        ok, token_is_dead = _deliver_fcm(
            device,
            kind=kind,
            state_version=state_version,
        )
        if ok:
            delivered += 1
            device.failure_count = 0
            device.last_success_at = timezone.now()
            device.save(update_fields=['failure_count', 'last_success_at'])
            continue
        if token_is_dead:
            device.is_active = False
            device.save(update_fields=['is_active'])
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
