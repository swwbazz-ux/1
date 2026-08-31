"""Одноразовый перенос уже введённого телефона в нативное приложение.

Прямая установка APK не передаёт приложению URL скачивания, cookie браузера
или referrer. Поэтому ``/start/`` выдаёт короткоживущий непрозрачный билет, а
Android открывает его через verified HTTPS App Link. Телефон остаётся только в
общем серверном cache; в URL находится случайный токен без персональных данных.

Билет ничего не авторизует: после переноса человек всё равно вводит PIN. Новых
таблиц нет, а одноразовый prefill в существующей Django session удаляется при
первом показе подходящего ролевого экрана входа.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass

from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from .app_catalog import role_app_public_url
from .context_processors import parse_native_app_identity
from .forms import normalize_phone
from .role_apps import get_role_app_for_request


logger = logging.getLogger(__name__)

NATIVE_HANDOFF_TTL_SECONDS = 30 * 60
NATIVE_HANDOFF_REUSE_SECONDS = 60
NATIVE_HANDOFF_TOKEN_BYTES = 32
NATIVE_HANDOFF_TOKEN_PATTERN = re.compile(r'^[A-Za-z0-9_-]{43}$')
NATIVE_HANDOFF_CACHE_PREFIX = 'users:native-handoff:v1:'
NATIVE_HANDOFF_SESSION_KEY = 'native_handoff_phone_prefill_v1'

# Оба APK подписываются одним постоянным release-ключом. Fingerprint является
# публичной частью сертификата и нужен Android для verified App Links.
NATIVE_HANDOFF_CERT_SHA256 = (
    '15:CB:7E:9E:DF:50:02:12:93:91:78:D2:20:AC:26:BD:'
    'CD:F3:9E:80:94:E4:1A:92:03:AB:69:D0:34:30:08:CB'
)


@dataclass(frozen=True)
class NativeHandoffProfile:
    role_code: str
    profile_id: str
    package_name: str
    label: str
    accent_color: str


NATIVE_HANDOFF_PROFILES_BY_ROLE = {
    'driver': NativeHandoffProfile(
        role_code='driver',
        profile_id='driver',
        package_name='ru.copperresources.driver',
        label='Водитель',
        accent_color='#28C7B7',
    ),
    'excavator_operator': NativeHandoffProfile(
        role_code='excavator_operator',
        profile_id='excavator',
        package_name='ru.copperresources.excavator',
        label='Экскаваторщик',
        accent_color='#FFD200',
    ),
}
NATIVE_HANDOFF_PROFILES_BY_ID = {
    profile.profile_id: profile
    for profile in NATIVE_HANDOFF_PROFILES_BY_ROLE.values()
}


@dataclass(frozen=True)
class NativeHandoffConsumeResult:
    status: str
    phone: str = ''


def _cache_digest(token):
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def _data_cache_key(digest):
    return f'{NATIVE_HANDOFF_CACHE_PREFIX}data:{digest}'


def _used_cache_key(digest):
    return f'{NATIVE_HANDOFF_CACHE_PREFIX}used:{digest}'


def _recent_cache_key(profile, phone):
    material = f'{profile.profile_id}:{phone}'.encode('utf-8')
    digest = hashlib.sha256(material).hexdigest()
    return f'{NATIVE_HANDOFF_CACHE_PREFIX}recent:{digest}'


def _normalized_handoff_phone(value):
    phone = normalize_phone(value)
    if len(phone) != 11 or not phone.startswith('7'):
        return ''
    return phone


def build_native_handoff_url(request, *, phone, role_code):
    """Создать одноразовый App Link или вернуть пустую строку fail-closed.

    ``cache.add`` не перезаписывает уже существующий ключ. Коллизия практически
    невозможна при 256 битах случайности, но несколько попыток позволяют не
    превращать теоретическое совпадение в потерянную установку.
    """

    profile = NATIVE_HANDOFF_PROFILES_BY_ROLE.get(role_code)
    normalized_phone = _normalized_handoff_phone(phone)
    if profile is None or not normalized_phone:
        return ''

    payload = {
        'phone': normalized_phone,
        'role_code': profile.role_code,
        'profile_id': profile.profile_id,
    }
    app_root = role_app_public_url(request, profile.role_code).rstrip('/')
    recent_key = _recent_cache_key(profile, normalized_phone)
    try:
        recent_token = str(cache.get(recent_key) or '')
        if NATIVE_HANDOFF_TOKEN_PATTERN.fullmatch(recent_token):
            recent_digest = _cache_digest(recent_token)
            recent_payload = cache.get(_data_cache_key(recent_digest))
            recent_used = cache.get(_used_cache_key(recent_digest))
            if recent_payload == payload and not recent_used:
                return f'{app_root}/native-handoff/open/#token={recent_token}'
    except Exception:
        logger.warning('Native handoff cache is unavailable.', exc_info=True)
        return ''

    for _attempt in range(4):
        token = secrets.token_urlsafe(NATIVE_HANDOFF_TOKEN_BYTES)
        digest = _cache_digest(token)
        try:
            created = cache.add(
                _data_cache_key(digest),
                payload,
                timeout=NATIVE_HANDOFF_TTL_SECONDS,
            )
        except Exception:
            # Не печатаем payload или token: в них находятся персональные данные
            # и одноразовый bearer. Обычная установка APK остаётся доступной.
            logger.warning('Native handoff cache is unavailable.', exc_info=True)
            return ''
        if created:
            try:
                published = cache.add(
                    recent_key,
                    token,
                    timeout=NATIVE_HANDOFF_REUSE_SECONDS,
                )
                if not published:
                    shared_token = str(cache.get(recent_key) or '')
                    shared_payload = None
                    shared_used = True
                    if NATIVE_HANDOFF_TOKEN_PATTERN.fullmatch(shared_token):
                        shared_digest = _cache_digest(shared_token)
                        shared_payload = cache.get(
                            _data_cache_key(shared_digest)
                        )
                        shared_used = cache.get(_used_cache_key(shared_digest))
                    if shared_payload == payload and not shared_used:
                        cache.delete(_data_cache_key(digest))
                        return (
                            f'{app_root}/native-handoff/open/'
                            f'#token={shared_token}'
                        )
                    # Старый marker пережил data-ticket или был повреждён:
                    # заменяем его уже созданным рабочим билетом.
                    cache.set(
                        recent_key,
                        token,
                        timeout=NATIVE_HANDOFF_REUSE_SECONDS,
                    )
            except Exception:
                # Основной data-ticket уже записан и остаётся рабочим. Ошибка
                # короткого dedup-ключа не должна заставлять человека начинать
                # установку заново.
                logger.warning('Native handoff reuse marker write failed.', exc_info=True)
            return f'{app_root}/native-handoff/open/#token={token}'
    logger.warning('Native handoff token allocation exhausted collision retries.')
    return ''


def consume_native_handoff(*, token, role_code):
    """Атомарно погасить билет для роли и вернуть статус с телефоном.

    Общий cache API не имеет переносимого ``GETDEL``. Поэтому после проверки
    payload ставится атомарный used-lock через ``cache.add``: среди двух
    конкурентных запросов ровно один получает право удалить data и вернуть
    телефон.
    """

    token = str(token or '').strip()
    if not NATIVE_HANDOFF_TOKEN_PATTERN.fullmatch(token):
        return NativeHandoffConsumeResult('malformed')
    profile = NATIVE_HANDOFF_PROFILES_BY_ROLE.get(role_code)
    if profile is None:
        return NativeHandoffConsumeResult('wrong_role')

    digest = _cache_digest(token)
    try:
        payload = cache.get(_data_cache_key(digest))
    except Exception:
        logger.warning('Native handoff cache read failed.', exc_info=True)
        return NativeHandoffConsumeResult('unavailable')
    if not isinstance(payload, dict):
        return NativeHandoffConsumeResult('gone')
    if (
        payload.get('role_code') != profile.role_code
        or payload.get('profile_id') != profile.profile_id
    ):
        # Ошибка другого профиля не сжигает корректный билет.
        return NativeHandoffConsumeResult('wrong_role')

    phone = _normalized_handoff_phone(payload.get('phone'))
    if not phone:
        try:
            cache.delete(_data_cache_key(digest))
        except Exception:
            logger.warning('Invalid native handoff cache entry cleanup failed.', exc_info=True)
        return NativeHandoffConsumeResult('gone')

    try:
        claimed = cache.add(
            _used_cache_key(digest),
            True,
            timeout=NATIVE_HANDOFF_TTL_SECONDS,
        )
        if not claimed:
            return NativeHandoffConsumeResult('gone')
        cache.delete(_data_cache_key(digest))
    except Exception:
        logger.warning('Native handoff atomic claim failed.', exc_info=True)
        return NativeHandoffConsumeResult('unavailable')
    try:
        recent_key = _recent_cache_key(profile, phone)
        if cache.get(recent_key) == token:
            cache.delete(recent_key)
    except Exception:
        # Билет уже безопасно погашен. Очистка короткого dedup-маркера —
        # best-effort и не должна превращать успешный перенос в ошибку.
        logger.warning('Native handoff reuse marker cleanup failed.', exc_info=True)
    return NativeHandoffConsumeResult('ok', phone)


def store_native_handoff_session_prefill(request, *, phone, role_code):
    request.session[NATIVE_HANDOFF_SESSION_KEY] = {
        'phone': _normalized_handoff_phone(phone),
        'role_code': role_code,
        'issued_at': int(time.time()),
    }


def consume_native_handoff_session_prefill(request, *, role_code):
    """Разово забрать prefill только из сессии соответствующего role-host."""

    payload = request.session.get(NATIVE_HANDOFF_SESSION_KEY)
    if not isinstance(payload, dict):
        if payload is not None:
            request.session.pop(NATIVE_HANDOFF_SESSION_KEY, None)
        return ''
    if payload.get('role_code') != role_code:
        request.session.pop(NATIVE_HANDOFF_SESSION_KEY, None)
        return ''
    issued_at = payload.get('issued_at')
    now = int(time.time())
    if (
        not isinstance(issued_at, int)
        or issued_at > now + 60
        or now - issued_at > NATIVE_HANDOFF_TTL_SECONDS
    ):
        request.session.pop(NATIVE_HANDOFF_SESSION_KEY, None)
        return ''
    request.session.pop(NATIVE_HANDOFF_SESSION_KEY, None)
    return _normalized_handoff_phone(payload.get('phone'))


def _profile_for_request(request):
    role_app = get_role_app_for_request(request)
    if role_app is None:
        return None
    return NATIVE_HANDOFF_PROFILES_BY_ROLE.get(role_app.role_code)


def _native_profile_matches_request(request, profile):
    identity = parse_native_app_identity(request)
    # Для погашения capability одного cookie недостаточно: его может выставить
    # страница, а профиль приложения виден только в нативной UA-метке. Новые APK
    # не регистрируют PWA service worker, поэтому GET и POST сохраняют эту метку.
    return identity.found and identity.profile_id == profile.profile_id


def _private_response(response):
    response['Cache-Control'] = 'private, no-store, max-age=0'
    response['Pragma'] = 'no-cache'
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


@require_GET
def native_handoff_assetlinks_view(request):
    profile = _profile_for_request(request)
    if profile is None:
        raise Http404
    response = JsonResponse(
        [
            {
                'relation': ['delegate_permission/common.handle_all_urls'],
                'target': {
                    'namespace': 'android_app',
                    'package_name': profile.package_name,
                    'sha256_cert_fingerprints': [NATIVE_HANDOFF_CERT_SHA256],
                },
            }
        ],
        safe=False,
    )
    response['Cache-Control'] = 'public, max-age=3600'
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


@require_GET
def native_handoff_open_view(request):
    profile = _profile_for_request(request)
    if profile is None:
        raise Http404
    return _private_response(
        render(
            request,
            'users/native_handoff.html',
            {
                'native_handoff_profile': profile,
                'native_handoff_can_redeem': _native_profile_matches_request(
                    request,
                    profile,
                ),
            },
        )
    )


@require_POST
def native_handoff_redeem_view(request):
    profile = _profile_for_request(request)
    if profile is None:
        raise Http404
    if not _native_profile_matches_request(request, profile):
        return _private_response(
            JsonResponse({'ok': False, 'code': 'native_app_required'}, status=403)
        )
    if len(request.body) > 2048:
        return _private_response(
            JsonResponse({'ok': False, 'code': 'malformed'}, status=400)
        )
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except (TypeError, ValueError, UnicodeDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return _private_response(
            JsonResponse({'ok': False, 'code': 'malformed'}, status=400)
        )

    result = consume_native_handoff(
        token=payload.get('token'),
        role_code=profile.role_code,
    )
    if result.status == 'malformed':
        status = 400
    elif result.status == 'wrong_role':
        status = 404
    elif result.status == 'unavailable':
        status = 503
    elif result.status != 'ok':
        status = 410
    else:
        store_native_handoff_session_prefill(
            request,
            phone=result.phone,
            role_code=profile.role_code,
        )
        return _private_response(
            JsonResponse({'ok': True, 'redirect_url': '/'})
        )
    return _private_response(
        JsonResponse({'ok': False, 'code': result.status}, status=status)
    )
