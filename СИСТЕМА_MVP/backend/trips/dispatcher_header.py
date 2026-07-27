from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from core.models import lock_production_state
from core.production_time import production_shift_context, production_shift_type
from shifts.models import EmployeeShift, ShiftType
from shifts.services import lock_active_employee_for_shift
from users.models import EmployeeAccess
from users.session_device import get_session_device_kind


def get_dispatcher_shift_type_for_now(now):
    return production_shift_type(now)


WORKPLACE_ROLE_CODES = {
    'driver',
    'excavator_operator',
    'dispatcher',
    'mining_master',
    'oup',
}


def dispatcher_shift_queryset():
    dispatcher_access = EmployeeAccess.objects.filter(
        employee_id=OuterRef('employee_id'),
        role__code='dispatcher',
        role__is_active=True,
        is_active=True,
        status=EmployeeAccess.Status.ACTIVATED,
    )
    other_workplace_access = EmployeeAccess.objects.filter(
        employee_id=OuterRef('employee_id'),
        role__code__in=WORKPLACE_ROLE_CODES - {'dispatcher'},
        role__is_active=True,
        is_active=True,
        status=EmployeeAccess.Status.ACTIVATED,
    )
    return (
        EmployeeShift.objects
        .filter(closed_at__isnull=True)
        .annotate(
            has_dispatcher_access=Exists(dispatcher_access),
            has_other_workplace_access=Exists(other_workplace_access),
        )
        .filter(
            Q(workplace_code='dispatcher')
            | Q(
                workplace_code='',
                equipment__isnull=True,
                has_dispatcher_access=True,
                has_other_workplace_access=False,
            )
        )
        .select_related('employee')
    )


def get_active_dispatcher_shift(access=None):
    shifts = (
        dispatcher_shift_queryset()
        .order_by('-opened_at')
    )
    if access and access.role.code == 'dispatcher':
        own_shift = shifts.filter(employee=access.employee).first()
        if own_shift:
            return own_shift
    return shifts.first()


def build_dispatcher_header_context(access, request=None):
    production_context = production_shift_context()
    active_shift = get_active_dispatcher_shift(access)
    own_shift = (
        dispatcher_shift_queryset()
        .filter(employee=access.employee)
        .order_by('-opened_at')
        .first()
        if access and access.role.code in {'dispatcher', 'admin'}
        else None
    )
    blocking_shift = (
        active_shift
        if active_shift and not own_shift and active_shift.employee_id != access.employee_id
        else None
    )
    session_device_kind = get_session_device_kind(request) if request else 'shared'
    can_start_shift = bool(access and access.role.code == 'dispatcher' and not own_shift and not active_shift)
    dispatcher = active_shift.employee if active_shift else None
    dispatcher_photo = ''
    if dispatcher and getattr(dispatcher, 'photo', None):
        try:
            dispatcher_photo = dispatcher.photo.url
        except ValueError:
            dispatcher_photo = ''
    dispatcher_initials = ''
    if dispatcher:
        dispatcher_initials = ''.join(part[0] for part in (dispatcher.full_name or '').split()[:2]).upper()
    active_shift_date = timezone.localtime(active_shift.opened_at).strftime('%d.%m.%Y') if active_shift else ''
    active_shift_opened_at = timezone.localtime(active_shift.opened_at).strftime('%H:%M') if active_shift else ''
    context = {
        'active_shift': active_shift,
        'own_shift': own_shift,
        'blocking_shift': blocking_shift,
        'active_dispatcher': dispatcher,
        'active_dispatcher_name': dispatcher.full_name if dispatcher else '',
        'active_dispatcher_photo': dispatcher_photo,
        'active_dispatcher_initials': dispatcher_initials or 'Д',
        'active_shift_date': active_shift_date,
        'active_shift_opened_at': active_shift_opened_at,
        'can_toggle_shift': bool(own_shift or can_start_shift),
        'can_service_close_blocking_shift': bool(
            blocking_shift and access and access.role.code in {'dispatcher', 'admin'}
        ),
        'shift_is_open': bool(own_shift),
        'requires_shift_reauth': bool(can_start_shift),
        'session_device_kind': session_device_kind,
        'shift_reauth_title': 'Вход Горного диспетчера',
        'shift_reauth_description': 'Введите телефон и код диспетчера, который начинает смену на этом устройстве.',
        'shift_reauth_code_label': 'Код диспетчера',
        'shift_start_confirm': 'Начать смену горного диспетчера?',
        'shift_end_confirm': 'Завершить смену горного диспетчера?',
        'shift_end_confirm_title': 'Завершение смены',
        'shift_end_confirm_description': 'Вы уверены, что хотите завершить текущую смену? После завершения смены будут сохранены результаты работы.',
        'shift_end_confirm_role': 'Диспетчер',
    }
    effective_shift_type = active_shift.shift_type if active_shift else production_context.shift_type
    context.update({
        'current_time': production_context.local_datetime.strftime('%H:%M'),
        'current_date': production_context.production_date.strftime('%d.%m.%Y'),
        'shift_label': 'Дневная' if effective_shift_type == ShiftType.DAY else 'Ночная',
        'dispatcher_header_time_range': '07:00-19:00' if effective_shift_type == ShiftType.DAY else '19:00-07:00',
    })
    return context


@transaction.atomic
def open_dispatcher_shift(access):
    employee = lock_active_employee_for_shift(access.employee, role_code='dispatcher')
    lock_production_state()
    if get_active_dispatcher_shift(access):
        return None
    now = timezone.now()
    return EmployeeShift.objects.create(
        employee=employee,
        shift_type=get_dispatcher_shift_type_for_now(now),
        workplace_code='dispatcher',
        opened_at=now,
        opened_by=employee,
    )


@transaction.atomic
def close_dispatcher_shift(access):
    shifts = list(
        dispatcher_shift_queryset()
        .select_for_update()
        .filter(employee=access.employee, closed_at__isnull=True)
        .order_by('-opened_at')
    )
    if not shifts:
        return None
    now = timezone.now()
    for shift in shifts:
        shift.closed_at = now
        shift.closed_by = access.employee
    EmployeeShift.objects.bulk_update(shifts, ['closed_at', 'closed_by'])
    shift = shifts[0]
    return shift
