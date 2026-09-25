"""Команды собственной смены Горного диспетчера."""

import re

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect

from shifts.services import other_role_shift_flag
from users.access_auth import find_employee_access_by_credentials
from users.active_role import activate_role_session, active_access_for_employee_role
from users.models import EmployeeAccess
from users.session_device import get_session_device_kind, set_session_device_kind

from .dispatcher_guards import get_dispatcher_action_redirect_url
from .dispatcher_header import (
    close_dispatcher_shift,
    get_active_dispatcher_shift,
    open_dispatcher_shift,
)


def authenticate_dispatcher_shared_shift_start(request):
    """Повторно подтвердить диспетчера перед началом смены на общем ПК."""
    phone = request.POST.get('reauth_phone', '').strip()
    access_code = re.sub(r'\D', '', request.POST.get('reauth_access_code', ''))
    device_kind = request.POST.get('device_kind', '').strip()
    if not phone or not access_code:
        return None, (
            'Для начала смены на общем компьютере введите телефон '
            'и код горного диспетчера.'
        )
    if phone and not phone.startswith(('+', '7', '8')):
        phone = f'+7 {phone}'

    access = find_employee_access_by_credentials(
        phone,
        access_code,
        role_code='dispatcher',
    )
    if not access:
        return None, 'Телефон или код горного диспетчера указаны неверно.'

    try:
        access = activate_role_session(request, access)
    except ValidationError as error:
        return None, '; '.join(error.messages)
    set_session_device_kind(request, device_kind)
    return access, ''


def execute_dispatcher_toggle_shift(
    request,
    *,
    lock_mutation_access,
    shared_start_authenticator,
):
    """Открыть или завершить собственную смену Горного диспетчера."""
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(id=access_id, is_active=True)
        .first()
    )
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')
    session_access = access

    redirect_url = get_dispatcher_action_redirect_url(request)
    if request.method != 'POST':
        return redirect(redirect_url)

    action = request.POST.get('shift_action')
    if action == 'start':
        if get_session_device_kind(request) == 'shared':
            reauth_access, reauth_error = shared_start_authenticator(request)
            if reauth_error:
                messages.error(request, reauth_error)
                return redirect(redirect_url)
            access = reauth_access
            # Повторная авторизация обновляет last_login_at и метку активной
            # роли в сессии. Загруженный до неё session_access уже устарел.
            session_access = access
        else:
            access = active_access_for_employee_role(
                access.employee,
                'dispatcher',
            )
            if not access:
                messages.error(
                    request,
                    'Активированный доступ Горного диспетчера не найден.',
                )
                return redirect(redirect_url)
        session_access = lock_mutation_access(request, session_access)
        if not session_access:
            messages.error(request, 'Роль неактивна — доступен только просмотр.')
            return redirect(redirect_url)
        if get_active_dispatcher_shift(access):
            messages.warning(request, 'Смена горного диспетчера уже открыта.')
            return redirect(redirect_url)
        try:
            shift = open_dispatcher_shift(
                access,
                close_other_role_shift=other_role_shift_flag(request.POST),
            )
        except ValidationError as error:
            messages.error(request, '; '.join(error.messages))
            return redirect(redirect_url)
        if not shift:
            messages.warning(request, 'Смена горного диспетчера уже открыта.')
            return redirect(redirect_url)
        messages.success(request, 'Смена горного диспетчера открыта.')
        return redirect(redirect_url)

    if action == 'end':
        dispatcher_access = active_access_for_employee_role(
            access.employee,
            'dispatcher',
        )
        if dispatcher_access:
            access = dispatcher_access
        session_access = lock_mutation_access(request, session_access)
        if not session_access:
            messages.error(request, 'Роль неактивна — доступен только просмотр.')
            return redirect(redirect_url)
        shift = close_dispatcher_shift(access)
        if not shift:
            messages.warning(
                request,
                'Открытая смена горного диспетчера не найдена.',
            )
            return redirect(redirect_url)
        messages.success(request, 'Смена горного диспетчера завершена.')
        return redirect(redirect_url)

    messages.error(request, 'Неизвестное действие со сменой диспетчера.')
    return redirect(redirect_url)
