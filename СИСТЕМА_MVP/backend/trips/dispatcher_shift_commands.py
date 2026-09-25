"""Команды смен на Диспетчерском пульте."""

import re

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.utils import timezone

from references.models import Equipment
from shifts.models import EmployeeShift
from shifts.services import (
    ExcavatorShiftError,
    equipment_is_truck,
    other_role_shift_flag,
    validate_driver_close_readings,
    validate_excavator_shift_readings,
)
from users.access_auth import find_employee_access_by_credentials
from users.active_role import activate_role_session, active_access_for_employee_role
from users.models import Employee, EmployeeAccess
from users.session_device import get_session_device_kind, set_session_device_kind

from .dispatcher_guards import (
    dispatcher_shift_required_redirect,
    get_dispatcher_action_redirect_url,
    get_dispatcher_control_url,
)
from .dispatcher_header import (
    close_dispatcher_shift,
    get_active_dispatcher_shift,
    open_dispatcher_shift,
)
from .models import (
    DispatcherActionType,
    OPEN_TRIP_STATUSES,
    Trip,
)


SERVICE_CLOSE_NEGLECTED = 'neglected'
SERVICE_CLOSE_COORDINATED = 'coordinated'
SERVICE_CLOSE_AUTO_EXPIRED = 'auto_expired'
SERVICE_CLOSE_KIND_LABELS = {
    SERVICE_CLOSE_NEGLECTED: 'Сотрудник не закрыл сам',
    SERVICE_CLOSE_COORDINATED: 'По согласованию с диспетчером',
    SERVICE_CLOSE_AUTO_EXPIRED: 'Автоматически через 13 часов',
}
SERVICE_CLOSE_NEGLECTED_NOTE = 'Сотрудник не закрыл смену сам и не сообщил диспетчеру.'
SERVICE_CLOSE_AUTO_NOTE = (
    'Закрыта автоматически в конце смены: сотрудник не закрыл её сам '
    'и не сообщил диспетчеру.'
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


def normalize_service_close_kind(raw_kind, reason):
    """Вид закрытия из формы; старые формы без поля — по наличию причины."""
    kind = str(raw_kind or '').strip()
    if kind in SERVICE_CLOSE_KIND_LABELS and kind != SERVICE_CLOSE_AUTO_EXPIRED:
        return kind
    return SERVICE_CLOSE_COORDINATED if reason else SERVICE_CLOSE_NEGLECTED


def finish_service_closed_shift(
    shift,
    *,
    closed_by,
    close_kind,
    note,
    reading_fields=(),
    now=None,
):
    """Общий хвост служебного закрытия: смена, процессы, рейсы и паспорт."""
    shift.closed_at = now or timezone.now()
    shift.closed_by = closed_by
    shift.is_service_closed = True
    shift.service_close_kind = close_kind
    shift.service_close_note = str(note or '')[:255]
    shift.save(update_fields=[
        *reading_fields,
        'closed_at',
        'closed_by',
        'is_service_closed',
        'service_close_kind',
        'service_close_note',
    ])
    from trips.free_bucket import cancel_free_bucket_acceptances_for_shift

    cancel_free_bucket_acceptances_for_shift(
        shift,
        cancelled_at=shift.closed_at,
    )
    if not shift.equipment_id:
        return

    # Ожидания рабочего процесса не живут дольше смены; ремонт и прочие
    # состояния техники остаются и передаются сменщику.
    from downtimes.driver_workflow import close_workflow_downtimes

    close_workflow_downtimes(shift.equipment, ended_at=shift.closed_at)
    if equipment_is_truck(shift.equipment):
        Trip.objects.filter(
            truck=shift.equipment,
            status__in=OPEN_TRIP_STATUSES,
        ).update(is_carryover=True)
        from reports.driver_shift_passport_snapshots import (
            enqueue_driver_shift_passport_capture,
        )
        from reports.models import DriverShiftPassportTrigger

        enqueue_driver_shift_passport_capture(
            shift=shift,
            trigger=DriverShiftPassportTrigger.SERVICE_CLOSE,
            captured_by=closed_by,
        )
    else:
        Trip.objects.filter(
            loading_shift=shift,
            status__in=OPEN_TRIP_STATUSES,
        ).update(is_carryover=True)


def execute_dispatcher_service_close_shift(
    request,
    shift_id,
    *,
    lock_mutation_access,
    parse_shift_decimal,
    close_kind_normalizer,
    finish_shift,
    action_logger,
):
    """Служебно закрыть открытую смену другого сотрудника."""
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
    redirect_url = get_dispatcher_control_url(request)

    if request.method != 'POST':
        return redirect(redirect_url)
    reason = request.POST.get('reason', '').strip()
    close_kind = close_kind_normalizer(
        request.POST.get('close_kind'),
        reason,
    )
    if close_kind == SERVICE_CLOSE_COORDINATED and not reason:
        messages.error(
            request,
            'Укажите причину закрытия смены по согласованию с сотрудником.',
        )
        return redirect(redirect_url)
    if not reason:
        reason = SERVICE_CLOSE_NEGLECTED_NOTE

    shift_reference = (
        EmployeeShift.objects
        .filter(id=shift_id, closed_at__isnull=True)
        .values('employee_id', 'equipment_id')
        .first()
    )
    if not shift_reference:
        messages.error(
            request,
            'Открытая смена для служебного закрытия не найдена.',
        )
        return redirect(redirect_url)
    locked_employee_ids = list(
        Employee.objects
        .select_for_update()
        .filter(pk__in={access.employee_id, shift_reference['employee_id']})
        .order_by('pk')
        .values_list('pk', flat=True)
    )
    access = lock_mutation_access(request, access)
    if not access:
        messages.error(request, 'Роль неактивна — доступен только просмотр.')
        return redirect(redirect_url)
    shift = (
        EmployeeShift.objects
        .select_for_update(of=('self',))
        .select_related('employee', 'equipment')
        .filter(id=shift_id, closed_at__isnull=True)
        .first()
    )
    if not shift:
        messages.error(
            request,
            'Открытая смена для служебного закрытия не найдена.',
        )
        return redirect(redirect_url)
    if shift.employee_id not in locked_employee_ids:
        messages.error(
            request,
            'Смена сотрудника изменилась. Повторите служебное закрытие.',
        )
        return redirect(redirect_url)
    if shift.employee_id == access.employee_id:
        messages.error(
            request,
            'Собственную смену нужно завершить штатным действием.',
        )
        return redirect(redirect_url)
    if shift.equipment_id:
        locked_equipment = (
            Equipment.objects.select_for_update(of=('self',))
            .select_related('equipment_type', 'model')
            .get(pk=shift.equipment_id)
        )
        shift.equipment = locked_equipment

    is_blocking_dispatcher_shift = (
        access.role.code == 'dispatcher'
        and EmployeeAccess.objects.filter(
            employee_id=shift.employee_id,
            role__code='dispatcher',
            is_active=True,
        ).exists()
        and get_active_dispatcher_shift(access).id == shift.id
    )
    if not is_blocking_dispatcher_shift:
        shift_error = dispatcher_shift_required_redirect(
            request,
            access,
            redirect_url,
        )
        if shift_error:
            return shift_error

    reading_fields = []
    # Показания необязательны: сотрудник, не закрывший смену, их не сдал, и
    # требовать их с диспетчера нелогично. Введённые проверяем как раньше.
    readings_provided = close_kind == SERVICE_CLOSE_COORDINATED and any(
        str(request.POST.get(key) or '').strip()
        for key in ('end_fuel', 'end_mileage', 'end_engine_hours')
    )
    if shift.equipment_id and not readings_provided:
        shift.end_fuel = None
        shift.end_mileage = None
        shift.end_engine_hours = None
        reading_fields = ['end_fuel', 'end_mileage', 'end_engine_hours']
    elif shift.equipment_id:
        if equipment_is_truck(shift.equipment):
            try:
                readings = {
                    'end_fuel': parse_shift_decimal(
                        request.POST.get('end_fuel'),
                        'Топливо',
                    ),
                    'end_mileage': parse_shift_decimal(
                        request.POST.get('end_mileage'),
                        'Одометр',
                    ),
                    'end_engine_hours': parse_shift_decimal(
                        request.POST.get('end_engine_hours'),
                        'Моточасы',
                    ),
                }
                validate_driver_close_readings(shift, **readings)
            except (ValueError, ValidationError) as error:
                error_messages = getattr(error, 'messages', None) or [str(error)]
                messages.error(request, '; '.join(error_messages))
                return redirect(redirect_url)
            for field, value in readings.items():
                setattr(shift, field, value)
            reading_fields = list(readings)
        else:
            try:
                fuel, engine_hours = validate_excavator_shift_readings(
                    shift.equipment,
                    request.POST.get('end_fuel'),
                    request.POST.get('end_engine_hours'),
                    opening_shift=shift,
                )
            except ExcavatorShiftError as error:
                messages.error(request, error.message)
                return redirect(redirect_url)
            shift.end_fuel = fuel
            shift.end_mileage = None
            shift.end_engine_hours = engine_hours
            reading_fields = ['end_fuel', 'end_mileage', 'end_engine_hours']

    finish_shift(
        shift,
        closed_by=access.employee,
        close_kind=close_kind,
        note=reason,
        reading_fields=reading_fields,
    )
    action_logger(
        actor=access.employee,
        action_type=DispatcherActionType.SERVICE_CLOSE_SHIFT,
        shift=shift,
        target_summary=(
            f'{shift.employee} / {shift.equipment or "-"} / '
            f'{shift.get_shift_type_display()}'
        ),
        reason=reason,
    )
    if close_kind == SERVICE_CLOSE_COORDINATED:
        messages.success(
            request,
            f'Смена сотрудника {shift.employee} закрыта по согласованию с ним.',
        )
    else:
        messages.success(
            request,
            f'Смена сотрудника {shift.employee} закрыта: '
            'сотрудник не закрыл её сам.',
        )
    return redirect(redirect_url)
