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
from .work_profiles import employee_has_effective_access_role


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
        })

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

    return render(
        request,
        'users/universal_start.html',
        {
            'found': True,
            'apps': apps,
            'employee': apps[0]['employee'],
            'submitted_phone': format_phone_for_display(phone),
        },
    )
