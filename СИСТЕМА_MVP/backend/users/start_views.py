"""Общий вход: одна ссылка на всех.

Раньше администратору приходилось знать должность человека и давать ему ссылку
именно на его приложение. Здесь человек вводит только номер, а система сама
находит его в базе и показывает, какое приложение ему ставить.

Регистрацию и пинкод страница намеренно не трогает: приложение можно установить
только с его собственного адреса, поэтому переход туда всё равно нужен, а
заводить пинкод на одном адресе и входить на другом — лишний круг.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .access_auth import find_unactivated_accesses_by_phone, format_phone_for_display
from .forms import normalize_phone
from .app_catalog import APP_CATALOG_ROLE_CODES, role_app_public_url
from .role_apps import get_role_app
from .models import EmployeeAccess
from .work_profiles import employee_has_effective_access_role


# Единственный реестр Android-сборок для универсального входа. Экскаваторщик
# берёт имя APK и отображаемую версию из того же manifest, по которому нативное
# приложение проверяет обновления: второй ручной номер неизбежно отставал.
ANDROID_APK_BY_ROLE = {
    'excavator_operator': {
        'manifest_path': 'apk/excavator-update.json',
        'profile': 'excavator',
    },
    'driver': {
        'path': 'apk/driver-10.apk',
        'version': '0.1.8',
    },
}


START_RESULT_TTL_SECONDS = 30 * 60
_START_RESULT_TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{43}$')
_START_RESULT_CACHE_PREFIX = 'universal-start-result:v1:'
logger = logging.getLogger(__name__)


# Раньше здесь стоял предел на число показанных кнопок: восемь штук подряд
# читались как список, а не как выбор. Со значками в два столбца место
# перестало быть узким местом, и прятать что-то больше не нужно.


def with_country_code(value):
    """Человек набирает десять цифр, а поиск ждёт номер целиком.

    На экране входа код страны подставляет скрипт, здесь его нет — и не должно
    быть: страница обязана работать, даже если скрипты не отработали.
    """
    digits = normalize_phone(value)
    if len(digits) == 10 and digits.startswith('9'):
        return f'7{digits}'
    return digits


def _start_result_cache_key(token):
    digest = hashlib.sha256(token.encode('ascii')).hexdigest()
    return f'{_START_RESULT_CACHE_PREFIX}{digest}'


def _issue_start_result(phone):
    """Сохранить номер для безопасного POST/Redirect/GET без PII в URL."""

    payload = {
        'phone': str(phone),
        'issued_at': time.time(),
    }
    try:
        for _attempt in range(4):
            token = secrets.token_urlsafe(32)
            if cache.add(
                _start_result_cache_key(token),
                payload,
                START_RESULT_TTL_SECONDS,
            ):
                return token
    except Exception:
        logger.exception('Universal start result cache write failed')
        return ''
    logger.warning('Universal start result token allocation failed')
    return ''


def _read_start_result(token):
    if not isinstance(token, str) or not _START_RESULT_TOKEN_RE.fullmatch(token):
        return None
    key = _start_result_cache_key(token)
    try:
        payload = cache.get(key)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    phone = payload.get('phone')
    issued_at = payload.get('issued_at')
    if (
        not isinstance(phone, str)
        or len(phone) > 32
        or (phone and not phone.isdigit())
        or not isinstance(issued_at, (int, float))
    ):
        try:
            cache.delete(key)
        except Exception:
            pass
        return None
    age = time.time() - float(issued_at)
    if age < -60 or age > START_RESULT_TTL_SECONDS:
        try:
            cache.delete(key)
        except Exception:
            pass
        return None
    return phone


def is_android_request(request):
    return 'android' in request.META.get('HTTP_USER_AGENT', '').lower()


def is_ios_request(request):
    user_agent = request.META.get('HTTP_USER_AGENT', '').lower()
    return 'iphone' in user_agent or 'ipad' in user_agent or 'ipod' in user_agent


def android_apk_for_role(role_code):
    release = ANDROID_APK_BY_ROLE.get(role_code)
    if release is None:
        return None

    manifest_relative_path = release.get('manifest_path')
    if manifest_relative_path:
        try:
            manifest_path = Path(settings.MEDIA_ROOT) / manifest_relative_path
            payload = json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError):
            logger.exception('Android update manifest is unavailable for %s', role_code)
            return None
        profile = release.get('profile', '')
        version_code = payload.get('versionCode')
        version_name = payload.get('versionName')
        if (
            payload.get('schemaVersion') != 1
            or payload.get('profile') != profile
            or not isinstance(version_code, int)
            or version_code < 1
            or not isinstance(version_name, str)
            or not version_name.strip()
            or len(version_name) > 64
        ):
            logger.error('Android update manifest is invalid for %s', role_code)
            return None
        relative_path = Path('apk') / f'{profile}-{version_code}.apk'
        version = version_name.strip()
    else:
        relative_path = Path(release['path'])
        version = release['version']

    if not (Path(settings.MEDIA_ROOT) / relative_path).is_file():
        return None
    media_url = f"/{settings.MEDIA_URL.strip('/')}"
    return {
        'url': f"{media_url}/{relative_path.as_posix()}",
        'version': version,
    }


def _render_universal_start(
    request,
    template_name,
    context,
    *,
    referrer_policy='same-origin',
):
    """Не кешировать персональный результат поиска приложения."""

    response = render(request, template_name, context)
    response['Referrer-Policy'] = referrer_policy
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


def _see_other(location):
    response = HttpResponseRedirect(location)
    response.status_code = 303
    response['Cache-Control'] = 'private, no-store, max-age=0'
    response['Pragma'] = 'no-cache'
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


def _render_start_result(request, phone):
    matches = [
        candidate
        for candidate in find_unactivated_accesses_by_phone(phone)
        if employee_has_effective_access_role(
            candidate.employee,
            candidate.role.code,
            allow_pending_access=True,
        )
    ]

    apps = []
    show_android_apk = is_android_request(request)
    seen = set()
    for candidate in matches:
        code = candidate.role.code
        if code in seen or code not in APP_CATALOG_ROLE_CODES:
            continue
        app = get_role_app(code)
        if app is None:
            continue
        seen.add(code)
        # Номер уже введён здесь — набирать его заново на входе в приложение
        # незачем, поэтому несём его дальше в ссылке. Экран установки от
        # этого не пропадает: он размонтируется на login_view только при
        # ошибке входа, а не при простом наличии номера в поле.
        app_url = role_app_public_url(request, code)
        if phone:
            app_url = f'{app_url}?{urlencode({"phone": normalize_phone(phone)})}'
        apps.append({
            'app': app,
            'url': app_url,
            'apk': android_apk_for_role(code) if show_android_apk else None,
            'employee': candidate.employee,
            'last_login_at': candidate.last_login_at,
        })

    # Чем недавно пользовались — то и наверх. У большинства приложение одно и
    # порядок неважен, но у того, кто совмещает роли, список иначе превращается
    # в стену одинаковых кнопок, где своё приходится выискивать глазами.
    apps.sort(key=lambda item: (
        item['last_login_at'] is None,
        -(item['last_login_at'].timestamp() if item['last_login_at'] else 0),
        item['app'].name,
    ))

    if not apps:
        return _render_universal_start(
            request,
            'users/login_phone_not_found.html',
            {
                'login_role_app': None,
                'submitted_phone': format_phone_for_display(phone),
                'back_url': reverse('universal_start'),
            },
            referrer_policy='no-referrer',
        )

    # Пинкод уже заведён — значит придумывать его не надо, и обещать обратное
    # нельзя: человек будет ждать окна, которого не будет.
    has_working_code = any(
        candidate.status == EmployeeAccess.Status.ACTIVATED
        and (candidate.access_code or '').isdigit()
        and len(candidate.access_code) == 6
        for candidate in matches
    )

    return _render_universal_start(
        request,
        'users/universal_start.html',
        {
            'found': True,
            'apps': apps,
            'employee': apps[0]['employee'],
            'submitted_phone': format_phone_for_display(phone),
            'has_working_code': has_working_code,
            'is_ios': is_ios_request(request),
        },
        referrer_policy='no-referrer',
    )


@never_cache
@require_http_methods(['GET', 'POST'])
def universal_start_view(request):
    if request.method == 'POST':
        phone = with_country_code(request.POST.get('phone', ''))
        result_token = _issue_start_result(phone)
        if not result_token:
            # Даже при сбое Redis POST не должен становиться history entry:
            # возврат из установщика иначе снова отправит форму.
            return _see_other(reverse('universal_start'))
        result_url = f'{reverse("universal_start")}?{urlencode({"result": result_token})}'
        return _see_other(result_url)

    result_token = request.GET.get('result', '')
    if result_token:
        phone = _read_start_result(result_token)
        if phone is None:
            return _see_other(reverse('universal_start'))
        return _render_start_result(request, phone)

    # На единственной странице с POST-формой нужен same-origin referrer:
    # Android Chrome иначе отправляет Origin:null, и Django отклоняет CSRF.
    return _render_universal_start(
        request,
        'users/universal_start.html',
        {},
        referrer_policy='same-origin',
    )
