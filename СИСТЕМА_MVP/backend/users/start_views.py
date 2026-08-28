"""Общий вход: одна ссылка на всех.

Раньше администратору приходилось знать должность человека и давать ему ссылку
именно на его приложение. Здесь человек вводит только номер, а система сама
находит его в базе и показывает, какое приложение ему ставить.

Регистрацию и пинкод страница намеренно не трогает: приложение можно установить
только с его собственного адреса, поэтому переход туда всё равно нужен, а
заводить пинкод на одном адресе и входить на другом — лишний круг.
"""

from __future__ import annotations

from django.conf import settings
from django.shortcuts import render
from django.urls import reverse

from .access_auth import find_unactivated_accesses_by_phone, format_phone_for_display
from .forms import normalize_phone
from .app_catalog import APP_CATALOG_ROLE_CODES, role_app_public_url
from .role_apps import get_role_app
from .models import EmployeeAccess
from .work_profiles import employee_has_effective_access_role


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


def universal_start_view(request):
    if request.method != 'POST':
        return render(request, 'users/universal_start.html', {})

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
    seen = set()
    for candidate in matches:
        code = candidate.role.code
        if code in seen or code not in APP_CATALOG_ROLE_CODES:
            continue
        app = get_role_app(code)
        if app is None:
            continue
        seen.add(code)
        apps.append({
            'app': app,
            'url': role_app_public_url(request, code),
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
        return render(
            request,
            'users/login_phone_not_found.html',
            {
                'login_role_app': None,
                'submitted_phone': format_phone_for_display(phone),
                'support_chat_url': getattr(settings, 'SUPPORT_CHAT_URL', ''),
                'support_chat_label': getattr(settings, 'SUPPORT_CHAT_LABEL', ''),
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

    return render(
        request,
        'users/universal_start.html',
        {
            'found': True,
            'apps': apps,
            'employee': apps[0]['employee'],
            'submitted_phone': format_phone_for_display(phone),
            'has_working_code': has_working_code,
        },
    )
