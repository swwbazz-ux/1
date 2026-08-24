from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from shifts.models import EmployeeShift

from .models import Employee, EmployeeAccess


ACTIVE_ROLE_SESSION_KEY = 'active_role_access_id'
ACTIVE_ROLE_GENERATION_SESSION_KEY = 'active_role_login_at'
ACTIVE_ROLE_CODE_SESSION_KEY = 'active_role_code'

SAFE_ROLE_SWITCH_METHODS = {'GET', 'HEAD', 'OPTIONS', 'TRACE'}


def active_access_queryset(employee):
    return (
        EmployeeAccess.objects
        .filter(
            employee=employee,
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
            role__is_active=True,
            employee__is_active=True,
            employee__status=Employee.Status.ACTIVE,
        )
        .select_related('employee', 'role')
    )


def latest_active_role_access(employee):
    return (
        active_access_queryset(employee)
        .exclude(last_login_at__isnull=True)
        .order_by('-last_login_at', '-id')
        .first()
    )


def role_session_state(request, access=None):
    if access is None:
        access_id = request.session.get('employee_access_id')
        if not access_id:
            return {
                'authenticated': False,
                'is_active': True,
                'access': None,
                'active_access': None,
            }
        access = (
            EmployeeAccess.objects
            .select_related('employee', 'role')
            .filter(id=access_id)
            .first()
        )
    if not access:
        return {
            'authenticated': False,
            'is_active': False,
            'access': None,
            'active_access': None,
        }
    if (
        not access.is_active
        or access.status != EmployeeAccess.Status.ACTIVATED
        or not access.role.is_active
        or not access.employee.is_active
        or access.employee.status != Employee.Status.ACTIVE
    ):
        return {
            'authenticated': False,
            'is_active': False,
            'access': access,
            'active_access': None,
            'active_role_code': '',
            'active_role_changed_at': None,
        }

    observer_access = getattr(request, 'observer_access', None)
    if getattr(request, 'observer_mode', False) and observer_access and observer_access.pk == access.pk:
        return {
            'authenticated': True,
            'is_active': True,
            'access': access,
            'active_access': access,
            'active_role_code': access.role.code,
            'active_role_changed_at': access.last_login_at,
            'session_role_code': access.role.code,
            'session_revision': (
                access.last_login_at.isoformat()
                if access.last_login_at
                else ''
            ),
            'observer_mode': True,
        }

    active_access = latest_active_role_access(access.employee)
    is_active = active_access is None or active_access.id == access.id
    session_generation = request.session.get(ACTIVE_ROLE_GENERATION_SESSION_KEY)
    session_role_code = request.session.get(ACTIVE_ROLE_CODE_SESSION_KEY, '')
    if is_active and active_access and session_generation:
        parsed_generation = parse_datetime(str(session_generation))
        is_active = bool(
            parsed_generation
            and active_access.last_login_at
            and parsed_generation == active_access.last_login_at
        )
    if is_active and session_role_code:
        is_active = session_role_code == access.role.code
    return {
        'authenticated': True,
        'is_active': is_active,
        'access': access,
        'active_access': active_access,
        'active_role_code': active_access.role.code if active_access else access.role.code,
        'active_role_changed_at': active_access.last_login_at if active_access else None,
        'session_role_code': session_role_code or access.role.code,
        'session_revision': str(session_generation or ''),
    }


def _shift_workplace_code(shift):
    if shift.workplace_code:
        return shift.workplace_code
    equipment_type_name = (
        getattr(getattr(shift.equipment, 'equipment_type', None), 'name', '') or ''
    ).lower()
    if 'самосвал' in equipment_type_name:
        return 'driver'
    if 'экскаватор' in equipment_type_name:
        return 'excavator_operator'
    return ''


def _missing_end_readings(shift, workplace_code):
    if not shift.equipment_id:
        return []
    if workplace_code == 'driver':
        required_fields = ('end_fuel', 'end_mileage', 'end_engine_hours')
    elif workplace_code == 'excavator_operator':
        required_fields = ('end_fuel', 'end_engine_hours')
    else:
        required_fields = ()
    return [field for field in required_fields if getattr(shift, field) is None]


def _role_switch_blockers(employee, current_access, shifts):
    from downtimes.models import DowntimeEvent
    from trips.models import OPEN_TRIP_STATUSES, Trip

    blockers = []
    for shift in shifts:
        workplace_code = _shift_workplace_code(shift)
        if not workplace_code:
            blockers.append('есть открытая смена неопределенного legacy-контура')
            continue
        if workplace_code != current_access.role.code:
            blockers.append(
                f'есть открытая смена в другом рабочем контуре: {workplace_code}'
            )
            continue
        if not shift.equipment_id:
            continue
        open_trip_exists = Trip.objects.filter(status__in=OPEN_TRIP_STATUSES).filter(
            Q(truck_id=shift.equipment_id)
            | Q(excavator_id=shift.equipment_id)
            | Q(loading_shift=shift)
            | Q(unloading_shift=shift)
        ).exists()
        if open_trip_exists:
            blockers.append('есть открытый рейс')
        if DowntimeEvent.objects.filter(
            equipment_id=shift.equipment_id,
            ended_at__isnull=True,
        ).exists():
            blockers.append('есть открытый простой')
        if _missing_end_readings(shift, workplace_code):
            blockers.append('не заполнены обязательные конечные показания')
    return list(dict.fromkeys(blockers))


def _close_previous_role_shifts(employee, current_access, shifts):
    from trips.models import OPEN_TRIP_STATUSES, Trip

    now = timezone.now()
    closed = []
    for shift in shifts:
        if _shift_workplace_code(shift) != current_access.role.code:
            continue
        shift.closed_at = now
        shift.closed_by = employee
        closed.append(shift)
        if shift.equipment_id:
            Trip.objects.filter(
                Q(truck_id=shift.equipment_id) | Q(loading_shift=shift),
                status__in=OPEN_TRIP_STATUSES,
            ).update(is_carryover=True)
    if closed:
        EmployeeShift.objects.bulk_update(closed, ['closed_at', 'closed_by'])
        from reports.driver_shift_passport_snapshots import (
            enqueue_driver_shift_passport_capture,
        )
        from reports.models import DriverShiftPassportTrigger

        for shift in closed:
            if _shift_workplace_code(shift) != 'driver':
                continue
            enqueue_driver_shift_passport_capture(
                shift=shift,
                trigger=DriverShiftPassportTrigger.ROLE_SWITCH,
                captured_by=employee,
            )
    return closed


def _next_role_timestamp(accesses):
    now = timezone.now()
    latest = max(
        (access.last_login_at for access in accesses if access.last_login_at),
        default=None,
    )
    if latest and now <= latest:
        return latest + timedelta(microseconds=1)
    return now


@transaction.atomic
def activate_role_session(request, access):
    employee = (
        Employee.objects
        .select_for_update()
        .get(pk=access.employee_id)
    )
    accesses = list(
        active_access_queryset(employee)
        .select_for_update(of=('self',))
        .order_by('id')
    )
    target_access = next((item for item in accesses if item.id == access.id), None)
    if target_access is None:
        raise ValidationError('Активированный доступ сотрудника не найден.')

    current_access = max(
        (item for item in accesses if item.last_login_at),
        key=lambda item: (item.last_login_at, item.id),
        default=None,
    )
    if current_access and current_access.id != target_access.id:
        shifts = list(
            EmployeeShift.objects
            .select_for_update(of=('self',))
            .select_related('equipment__equipment_type')
            .filter(employee=employee, closed_at__isnull=True)
            .order_by('id')
        )
        blockers = _role_switch_blockers(employee, current_access, shifts)
        if blockers:
            raise ValidationError(
                ['Переключение роли заблокировано: ' + '; '.join(blockers) + '.']
            )
        _close_previous_role_shifts(employee, current_access, shifts)

    activated_at = _next_role_timestamp(accesses)
    target_access.last_login_at = activated_at
    target_access.save(update_fields=['last_login_at'])

    from core.models import bump_operational_state

    bump_operational_state(
        'EmployeeAccess:active_role_changed',
        event_type='active_role_changed',
        object_type='EmployeeAccess',
        object_id=target_access.id,
        payload={
            'employee_id': employee.id,
            'active_access_id': target_access.id,
            'active_role_code': target_access.role.code,
        },
    )

    request.session['employee_access_id'] = target_access.id
    request.session[ACTIVE_ROLE_SESSION_KEY] = target_access.id
    request.session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = activated_at.isoformat()
    request.session[ACTIVE_ROLE_CODE_SESSION_KEY] = target_access.role.code
    return target_access
