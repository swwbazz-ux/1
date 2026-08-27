"""Вход администратора в приложение любого сотрудника.

«Смена онлайн» показывает только тех, кто сейчас работает, и войти к человеку
можно было лишь пока он в системе. Но если в поле никого нет — ни горного
мастера, ни диспетчера, — а комплексы назначать надо, входить оказывается
некуда. Здесь список строится от самих доступов, а не от смен: нужный человек
есть в списке всегда, работает он прямо сейчас или нет.

Наблюдение и управление разведены намеренно. Наблюдение ничего не меняет и
годится, чтобы посмотреть чужими глазами. Управление пишет каждое действие в
журнал на имя администратора — чтобы потом было видно, кто на самом деле нажал
кнопку.
"""

from __future__ import annotations

from django.db.models import Q
from django.shortcuts import redirect, render
from django.utils import timezone

from .live_monitor import (
    OBSERVER_MODE_CONTROL,
    ONLINE_WINDOW,
    build_observer_url,
    recent_application_sessions,
)
from .models import EmployeeAccess
from .role_apps import ROLE_APPS
from .views import require_admin_access

# Показывать разом все доступы незачем: с полусотней людей страница станет
# нечитаемой, а искать всё равно будут по фамилии.
RESULT_LIMIT = 60


def system_admin_enter_employee_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')

    query = (request.GET.get('q') or '').strip()
    role_filter = (request.GET.get('role') or '').strip()

    accesses = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(
            is_active=True,
            employee__is_active=True,
            role__is_active=True,
            role__code__in=[app.role_code for app in ROLE_APPS],
        )
        .exclude(status=EmployeeAccess.Status.DEACTIVATED)
    )
    if role_filter:
        accesses = accesses.filter(role__code=role_filter)
    if query:
        accesses = accesses.filter(
            Q(employee__full_name__icontains=query)
            | Q(employee__phone__icontains=query)
            | Q(employee__personnel_number__icontains=query)
        )

    accesses = list(accesses.order_by('employee__full_name', 'role__name')[:RESULT_LIMIT + 1])
    truncated = len(accesses) > RESULT_LIMIT
    accesses = accesses[:RESULT_LIMIT]

    # «Сейчас в приложении» — подсказка, а не условие: войти можно к любому.
    now = timezone.now()
    online_access_ids = {
        session.access_id
        for session in recent_application_sessions(now=now)
        if session.last_seen_at >= now - ONLINE_WINDOW
    }

    rows = []
    for target in accesses:
        rows.append({
            'access': target,
            'employee': target.employee,
            'role': target.role,
            'is_online': target.pk in online_access_ids,
            'is_self': target.pk == access.pk,
            'activated': target.status == EmployeeAccess.Status.ACTIVATED,
            'observe_url': build_observer_url(
                request=request, actor_access=access, target_access=target,
            ),
            'control_url': build_observer_url(
                request=request, actor_access=access, target_access=target,
                mode=OBSERVER_MODE_CONTROL,
            ),
        })

    # Свои роли — то же самое, но без «от имени»: действия останутся на вас.
    own_by_role = {
        item.role.code: item
        for item in EmployeeAccess.objects
        .select_related('role')
        .filter(
            employee=access.employee,
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
    }
    # Вход в собственную роль тоже идёт через параллельный режим. Обычный
    # вход по пинкоду сделал бы новую роль единственной активной и погасил бы
    # админку: активная роль у сотрудника одна. Здесь настоящая сессия не
    # трогается, поэтому админка остаётся открытой в соседней вкладке.
    own_apps = [
        {
            'app': app,
            'access': own_by_role[app.role_code],
            'url': build_observer_url(
                request=request,
                actor_access=access,
                target_access=own_by_role[app.role_code],
                mode=OBSERVER_MODE_CONTROL,
            ),
        }
        for app in ROLE_APPS
        if app.role_code in own_by_role and own_by_role[app.role_code].pk != access.pk
    ]

    return render(request, 'users/system_admin_enter_employee.html', {
        'access': access,
        'rows': rows,
        'query': query,
        'role_filter': role_filter,
        'truncated': truncated,
        'result_limit': RESULT_LIMIT,
        'role_apps': ROLE_APPS,
        'own_apps': own_apps,
    })
