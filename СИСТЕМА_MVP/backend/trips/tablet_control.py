"""Права и собственная смена мастера в планшетном диспетчерском пульте.

Маркер устанавливается только серверными URL-обёртками. Ни профиль APK,
ни User-Agent, ни присланное клиентом название роли прав не предоставляют.
"""

from django.urls import reverse

from users.active_role import role_session_state
from users.models import EmployeeAccess

from .dispatcher_header import get_active_dispatcher_shift


def is_tablet_control_request(request):
    return bool(request and getattr(request, 'mining_master_tablet_control', False))


def is_tablet_master_request(request, access=None):
    if not is_tablet_control_request(request):
        return False
    access_id = request.session.get('employee_access_id')
    if access is not None and access.id != access_id:
        return False
    # Callers may have fetched access before acquiring the employee lock.
    # Re-read revocation and generation instead of trusting that old object.
    fresh_access = (
        EmployeeAccess.objects.select_related('employee', 'role', 'employee__contractor_organization')
        .filter(pk=access_id)
        .first()
    )
    if not fresh_access or fresh_access.role.code != 'mining_master':
        return False
    if access is not None and fresh_access.employee_id != access.employee_id:
        return False
    state = role_session_state(request, fresh_access)
    return bool(state['authenticated'] and state['is_active'])


def control_access_allowed(request, access, *, role_codes):
    if is_tablet_control_request(request):
        return is_tablet_master_request(request, access)
    return bool(access and access.role.code in role_codes)


def get_control_shift(access, request=None, *, for_update=False):
    if is_tablet_control_request(request):
        if not is_tablet_master_request(request, access):
            return None
        # Reuse the master's legacy-shift compatibility rules without making
        # the dispatcher header depend on assignments.views at import time.
        from assignments.views import mining_master_shift_queryset

        shifts = mining_master_shift_queryset().filter(employee_id=access.employee_id)
        if for_update:
            shifts = shifts.select_for_update(of=('self',))
        return shifts.order_by('-opened_at', '-id').first()
    if access and access.role.code == 'mining_master':
        return None
    return get_active_dispatcher_shift(access)


def tablet_action_url(request, route_name, **kwargs):
    if is_tablet_control_request(request):
        route_name = (
            'mining_master_tablet'
            if route_name == 'dispatcher_control'
            else f'mining_master_tablet_{route_name}'
        )
    return reverse(route_name, kwargs=kwargs or None)


def control_shift_closed_error(request):
    if is_tablet_control_request(request):
        return 'Смена горного мастера закрыта. Сначала начните свою смену.'
    return 'Смена горного диспетчера закрыта. Изменения на пульте недоступны.'
