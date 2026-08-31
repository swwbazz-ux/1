"""Общий вход: одна ссылка на всех.

Раньше администратору приходилось знать должность человека и давать ему ссылку
именно на его приложение. Здесь человек вводит только номер, а система сама
находит его в базе и показывает, какое приложение ему ставить.

Регистрацию и пинкод страница намеренно не трогает: приложение можно установить
только с его собственного адреса, поэтому переход туда всё равно нужен, а
заводить пинкод на одном адресе и входить на другом — лишний круг.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache

from .access_auth import find_unactivated_accesses_by_phone, format_phone_for_display
from .forms import normalize_phone
from .app_catalog import APP_CATALOG_ROLE_CODES, role_app_public_url
from .native_handoff import build_native_handoff_url
from .role_apps import get_role_app
from .models import EmployeeAccess
from .work_profiles import employee_has_effective_access_role


# Единственный реестр Android-сборок для универсального входа. При выпуске
# новой версии меняются только URL и подпись версии здесь; шаблон о конкретных
# ролях и именах APK ничего не знает.
ANDROID_APK_BY_ROLE = {
    'excavator_operator': {
        'path': 'apk/excavator-15.apk',
        'version': '0.1.12',
    },
    'driver': {
        'path': 'apk/driver-10.apk',
        'version': '0.1.8',
    },
}


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


def is_android_request(request):
    return 'android' in request.META.get('HTTP_USER_AGENT', '').lower()


def is_ios_request(request):
    user_agent = request.META.get('HTTP_USER_AGENT', '').lower()
    return 'iphone' in user_agent or 'ipad' in user_agent or 'ipod' in user_agent


def android_apk_for_role(role_code):
    release = ANDROID_APK_BY_ROLE.get(role_code)
    if release is None:
        return None
    relative_path = Path(release['path'])
    if not (Path(settings.MEDIA_ROOT) / relative_path).is_file():
        return None
    media_url = f"/{settings.MEDIA_URL.strip('/')}"
    return {
        'url': f"{media_url}/{relative_path.as_posix()}",
        'version': release['version'],
    }


def _render_universal_start(request, template_name, context):
    """Не кешировать результат, содержащий телефон и одноразовый App Link."""

    response = render(request, template_name, context)
    # Android Chrome can submit a regular HTTPS form with ``Origin: null``
    # when the document forbids every referrer. Django then correctly rejects
    # the POST before this view sees it. Keep same-origin CSRF evidence while
    # still hiding the source page from role subdomains and other origins.
    response['Referrer-Policy'] = 'same-origin'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


@never_cache
def universal_start_view(request):
    if request.method != 'POST':
        return _render_universal_start(request, 'users/universal_start.html', {})

    phone = with_country_code(request.POST.get('phone', ''))
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
        apk = android_apk_for_role(code) if show_android_apk else None
        native_handoff_url = ''
        if apk and phone:
            native_handoff_url = build_native_handoff_url(
                request,
                phone=phone,
                role_code=code,
            )
        apps.append({
            'app': app,
            'url': app_url,
            'apk': apk,
            'native_handoff_url': native_handoff_url,
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
    )
