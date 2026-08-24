from collections import defaultdict

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from shifts.models import EmployeeShift
from shifts.services import equipment_is_truck

from .active_role import latest_active_role_access
from .live_monitor import (
    ONLINE_WINDOW,
    build_observer_url,
    force_close_employee_shift,
    force_end_access_sessions,
    recent_application_sessions,
    touch_application_session,
)
from .models import AdminActionLog, EmployeeAccess
from .role_apps import ROLE_APPS, get_role_app
from .views import require_admin_access


SHIFT_TRACKED_APP_CODES = frozenset({
    'driver',
    'excavator_operator',
    'mining_master',
    'dispatcher',
    'oup',
})


@require_POST
def application_session_heartbeat_view(request):
    if getattr(request, 'observer_mode', False):
        return HttpResponse(status=403)
    if not touch_application_session(
        request,
        reported_path=request.POST.get('path', ''),
    ):
        return HttpResponse(status=401)
    response = HttpResponse(status=204)
    response['Cache-Control'] = 'private, no-store'
    return response


def _legacy_shift_app_code(shift, accesses_by_employee):
    if shift.workplace_code and get_role_app(shift.workplace_code):
        return shift.workplace_code
    equipment_type = (
        getattr(getattr(shift.equipment, 'equipment_type', None), 'name', '') or ''
    ).lower()
    if 'самосвал' in equipment_type:
        return 'driver'
    if 'экскаватор' in equipment_type:
        return 'excavator_operator'
    access = accesses_by_employee.get(shift.employee_id)
    return access.role.code if access and get_role_app(access.role.code) else ''


def _active_accesses_for_employees(employee_ids):
    result = {}
    accesses = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(
            employee_id__in=employee_ids,
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
            role__is_active=True,
        )
        .order_by('employee_id', '-last_login_at', '-pk')
    )
    for access in accesses:
        if access.employee_id not in result and get_role_app(access.role.code):
            result[access.employee_id] = access
    return result


def build_live_monitor_context(request, access):
    now = timezone.now()
    sessions = list(recent_application_sessions(now=now))
    open_shifts = list(
        EmployeeShift.objects
        .select_related('employee', 'equipment', 'equipment__equipment_type', 'equipment__model')
        .filter(closed_at__isnull=True)
        .order_by('opened_at', 'employee__full_name')
    )
    employee_ids = {shift.employee_id for shift in open_shifts}
    employee_ids.update(session.access.employee_id for session in sessions)
    accesses_by_employee = _active_accesses_for_employees(employee_ids)

    sessions_by_app = defaultdict(list)
    sessions_by_access = defaultdict(list)
    for session in sessions:
        sessions_by_app[session.app_code].append(session)
        sessions_by_access[session.access_id].append(session)

    shifts_by_app = defaultdict(list)
    for shift in open_shifts:
        app_code = _legacy_shift_app_code(shift, accesses_by_employee)
        if app_code:
            shifts_by_app[app_code].append(shift)

    app_cards = []
    online_access_ids = set()
    for app in ROLE_APPS:
        row_map = {}
        for shift in shifts_by_app.get(app.role_code, []):
            target_access = accesses_by_employee.get(shift.employee_id)
            if target_access and target_access.role.code != app.role_code:
                target_access = (
                    EmployeeAccess.objects
                    .select_related('employee', 'role')
                    .filter(
                        employee_id=shift.employee_id,
                        role__code=app.role_code,
                        is_active=True,
                        status=EmployeeAccess.Status.ACTIVATED,
                    )
                    .first()
                )
            key = target_access.pk if target_access else f'employee:{shift.employee_id}'
            row_map[key] = {
                'employee': shift.employee,
                'access': target_access,
                'shift': shift,
                'sessions': [],
                'last_seen_at': None,
                'current_path': '',
                'is_online': False,
                'is_recent': False,
                'is_truck': bool(shift.equipment_id and equipment_is_truck(shift.equipment)),
                'needs_readings': bool(shift.equipment_id),
            }
        for session in sessions_by_app.get(app.role_code, []):
            key = session.access_id
            row = row_map.setdefault(
                key,
                {
                    'employee': session.access.employee,
                    'access': session.access,
                    'shift': None,
                    'sessions': [],
                    'last_seen_at': None,
                    'current_path': '',
                    'is_online': False,
                    'is_recent': False,
                    'is_truck': False,
                    'needs_readings': False,
                },
            )
            row['sessions'].append(session)
            if row['last_seen_at'] is None or session.last_seen_at > row['last_seen_at']:
                row['last_seen_at'] = session.last_seen_at
                row['current_path'] = session.path
            row['is_recent'] = True
            row['is_online'] = session.last_seen_at >= now - ONLINE_WINDOW
            if row['is_online']:
                online_access_ids.add(session.access_id)

        rows = []
        for row in row_map.values():
            target_access = row['access']
            if target_access:
                row['observe_url'] = build_observer_url(
                    request=request,
                    actor_access=access,
                    target_access=target_access,
                    path=row['current_path'],
                )
                row['can_eject'] = target_access.pk != access.pk
            else:
                row['observe_url'] = ''
                row['can_eject'] = False
            rows.append(row)
        rows.sort(
            key=lambda row: (
                not bool(row['shift']),
                not row['is_online'],
                row['employee'].full_name,
            )
        )
        app_cards.append({
            'app': app,
            'rows': rows,
            'open_shift_count': len(shifts_by_app.get(app.role_code, [])),
            'online_count': len({
                session.access_id
                for session in sessions_by_app.get(app.role_code, [])
                if session.last_seen_at >= now - ONLINE_WINDOW
            }),
            'tracks_shifts': app.role_code in SHIFT_TRACKED_APP_CODES,
        })

    return {
        'access': access,
        'app_cards': app_cards,
        'open_shift_total': len(open_shifts),
        'online_employee_total': len(online_access_ids),
        'application_total': len(ROLE_APPS),
        'monitor_generated_at': now,
    }


def system_admin_live_monitor_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')
    context = build_live_monitor_context(request, access)
    if request.GET.get('fragment') == '1':
        response = render(request, 'users/includes/system_admin_live_monitor_grid.html', context)
    else:
        response = render(request, 'users/system_admin_live_monitor.html', context)
    response['Cache-Control'] = 'private, no-store'
    return response


@require_POST
@transaction.atomic
def system_admin_force_close_shift_view(request, shift_id):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')
    reason = request.POST.get('reason', '').strip()
    if not reason:
        messages.error(request, 'Укажите причину принудительного завершения смены.')
        return redirect('system_admin_live_monitor')
    try:
        shift = force_close_employee_shift(
            shift_id=shift_id,
            actor_access=access,
            end_fuel=request.POST.get('end_fuel'),
            end_mileage=request.POST.get('end_mileage'),
            end_engine_hours=request.POST.get('end_engine_hours'),
        )
    except ValidationError as error:
        messages.error(request, '; '.join(error.messages))
        return redirect('system_admin_live_monitor')

    ended_sessions = 0
    target_access = latest_active_role_access(shift.employee)
    if request.POST.get('eject_after_close') == '1' and target_access and target_access.pk != access.pk:
        ended_sessions = force_end_access_sessions(access=target_access)
    AdminActionLog.objects.create(
        actor=access.employee,
        action='Принудительно завершена смена сотрудника',
        action_code='admin_shift_force_close',
        object_type='EmployeeShift',
        object_id=str(shift.pk),
        object_repr=f'{shift.employee} / {shift.equipment or "без техники"}',
        new_value=f'Смена закрыта служебно; завершено сессий: {ended_sessions}',
        comment=reason,
    )
    messages.success(
        request,
        f'Смена сотрудника {shift.employee} завершена. '
        f'Активных сеансов закрыто: {ended_sessions}.',
    )
    return redirect('system_admin_live_monitor')


@require_POST
@transaction.atomic
def system_admin_force_end_sessions_view(request, access_id):
    actor_access = require_admin_access(request)
    if not actor_access:
        return redirect('role_home')
    reason = request.POST.get('reason', '').strip()
    if not reason:
        messages.error(request, 'Укажите причину завершения сеансов сотрудника.')
        return redirect('system_admin_live_monitor')
    target_access = (
        EmployeeAccess.objects
        .select_for_update()
        .select_related('employee', 'role')
        .filter(pk=access_id, is_active=True)
        .first()
    )
    if not target_access:
        messages.error(request, 'Активный доступ сотрудника не найден.')
        return redirect('system_admin_live_monitor')
    if target_access.pk == actor_access.pk:
        messages.error(request, 'Нельзя завершить текущую административную сессию этим действием.')
        return redirect('system_admin_live_monitor')
    ended_sessions = force_end_access_sessions(access=target_access)
    AdminActionLog.objects.create(
        actor=actor_access.employee,
        action='Принудительно завершены сеансы сотрудника',
        action_code='admin_sessions_force_end',
        object_type='EmployeeAccess',
        object_id=str(target_access.pk),
        object_repr=str(target_access),
        new_value=f'Завершено сессий: {ended_sessions}',
        comment=reason,
    )
    messages.success(
        request,
        f'Сотрудник {target_access.employee} отключён от приложения. '
        f'Завершено сеансов: {ended_sessions}.',
    )
    return redirect('system_admin_live_monitor')
