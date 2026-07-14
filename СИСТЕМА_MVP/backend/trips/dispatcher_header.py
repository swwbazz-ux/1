from django.db import transaction
from django.utils import timezone

from shifts.models import EmployeeShift, ShiftType
from shifts.services import lock_active_employee_for_shift
from users.models import EmployeeAccess
from users.session_device import get_session_device_kind


def get_dispatcher_shift_type_for_now(now):
    local_now = timezone.localtime(now)
    return ShiftType.DAY if 7 <= local_now.hour < 19 else ShiftType.NIGHT


def get_active_dispatcher_shift(access=None):
    shifts = (
        EmployeeShift.objects
        .filter(
            closed_at__isnull=True,
            employee__accesses__role__code='dispatcher',
            employee__accesses__is_active=True,
        )
        .select_related('employee')
        .distinct()
        .order_by('-opened_at')
    )
    if access and access.role.code == 'dispatcher':
        own_shift = shifts.filter(employee=access.employee).first()
        if own_shift:
            return own_shift
    return shifts.first()


def build_dispatcher_header_context(access, request=None):
    active_shift = get_active_dispatcher_shift(access)
    own_shift = (
        EmployeeShift.objects
        .filter(employee=access.employee, closed_at__isnull=True)
        .select_related('employee')
        .order_by('-opened_at')
        .first()
        if access and access.role.code in {'dispatcher', 'admin'}
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
    return {
        'active_shift': active_shift,
        'own_shift': own_shift,
        'active_dispatcher': dispatcher,
        'active_dispatcher_name': dispatcher.full_name if dispatcher else '',
        'active_dispatcher_photo': dispatcher_photo,
        'active_dispatcher_initials': dispatcher_initials or 'Д',
        'active_shift_date': active_shift_date,
        'active_shift_opened_at': active_shift_opened_at,
        'can_toggle_shift': bool(own_shift or can_start_shift),
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


@transaction.atomic
def open_dispatcher_shift(access):
    employee = lock_active_employee_for_shift(access.employee, role_code='dispatcher')
    if get_active_dispatcher_shift(access):
        return None
    now = timezone.now()
    return EmployeeShift.objects.create(
        employee=employee,
        shift_type=get_dispatcher_shift_type_for_now(now),
        opened_at=now,
        opened_by=employee,
    )


def close_dispatcher_shift(access):
    shifts = list(
        EmployeeShift.objects
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
