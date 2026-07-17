import secrets

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    CrewPlan,
    CrewPlanSlot,
    CrewPlanStatus,
    EquipmentAssignment,
)
from assignments.services import clear_active_equipment_assignment, production_work_date
from core.models import bump_operational_state
from shifts.models import EmployeeShift, ShiftType
from trips.models import OPEN_TRIP_STATUSES, Trip

from .models import AdminActionLog, Employee, EmployeeAccess, Role
from .work_profiles import PRODUCTION_APP_ROLE_CODES, employee_has_effective_access_role
from .oup_undo import (
    OUP_ACTION_ACCESS_DEACTIVATED,
    OUP_ACTION_ACCESS_ISSUED,
    OUP_ACTION_ACCESS_REISSUED,
    OUP_ACTION_EMPLOYEE_DISMISSED,
    OUP_ACTION_PERIOD_FINISHED,
    OUP_ACTION_PERIOD_STARTED,
    access_undo_state,
    assignment_undo_state,
    dismissal_undo_payload,
    employee_status_undo_state,
)


OUP_ROLE_CODE = 'oup'
PROTECTED_OUP_ROLE_CODE = 'admin'
OUP_AUDIT_FIELDS = (
    ('full_name', 'ФИО'),
    ('birth_date', 'Дата рождения'),
    ('phone', 'Телефон'),
    ('personnel_position', 'Кадровая должность'),
    ('base_specialization', 'Производственная специализация'),
    ('department', 'Подразделение'),
    ('hired_at', 'Дата приема'),
    ('rotation', 'Вахта / график'),
    ('comment', 'Комментарий'),
)


def lock_current_crew_drafts():
    draft_plans = list(
        CrewPlan.objects.select_for_update()
        .filter(
            status=CrewPlanStatus.DRAFT,
            work_date__gte=production_work_date(),
        )
        .order_by('id')
    )
    draft_plan_ids = [plan.id for plan in draft_plans]
    if draft_plan_ids:
        list(
            CrewPlanSlot.objects.select_for_update()
            .filter(plan_id__in=draft_plan_ids)
            .order_by('plan_id', 'id')
        )
    return draft_plan_ids


def employee_work_category_blockers(employee):
    blockers = []
    if EmployeeShift.objects.filter(employee=employee, closed_at__isnull=True).exists():
        blockers.append('есть открытая рабочая смена')
    if EquipmentAssignment.objects.filter(
        employee=employee,
        status__in=[AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED],
        ended_at__isnull=True,
    ).exists():
        blockers.append('есть действующее назначение на технику')
    if CrewPlanSlot.objects.filter(
        employee=employee,
        plan__status=CrewPlanStatus.DRAFT,
        plan__work_date__gte=production_work_date(),
    ).exists():
        blockers.append('сотрудник включен в текущий или будущий черновик расстановки')
    return blockers


def log_oup_action(
    actor,
    action,
    obj=None,
    *,
    action_code='',
    old_value='',
    new_value='',
    comment='',
    object_repr=None,
    undo_payload=None,
):
    return AdminActionLog.objects.create(
        actor=actor,
        action=f'ОУП: {action}',
        action_code=action_code,
        object_type=obj.__class__.__name__ if obj else '',
        object_id=str(obj.pk) if obj and obj.pk else '',
        object_repr=(str(obj) if obj else '') if object_repr is None else object_repr,
        old_value=old_value,
        new_value=new_value,
        comment=comment,
        undo_payload=undo_payload or {},
    )


def emit_employee_changed(employee, action):
    bump_operational_state(
        f'Employee:{action}',
        event_type='personnel_changed',
        object_type='Employee',
        object_id=employee.id,
        payload={
            'action': action,
            'employee_ids': [employee.id],
            'work_category': employee.work_category,
            'personnel_position_id': employee.personnel_position_id,
            'base_specialization_id': employee.base_specialization_id,
            'status': employee.status,
            'is_active': employee.is_active,
        },
    )


def employee_audit_snapshot(employee):
    snapshot = {}
    for field_name, label in OUP_AUDIT_FIELDS:
        value = getattr(employee, field_name, '')
        if hasattr(value, 'strftime'):
            value = value.strftime('%d.%m.%Y')
        snapshot[field_name] = (label, str(value or '—'))
    snapshot['photo'] = ('Фото', 'Добавлено' if employee.photo else 'Нет')
    return snapshot


def format_employee_changes(before, after):
    changes = []
    for field_name, (label, old_value) in before.items():
        new_value = after.get(field_name, (label, '—'))[1]
        if old_value != new_value:
            changes.append(f'{label}: {old_value} → {new_value}')
    return '; '.join(changes) or 'Данные без изменений'


def get_active_oup_period():
    return (
        EmployeeShift.objects
        .filter(workplace_code=OUP_ROLE_CODE, closed_at__isnull=True)
        .select_related('employee')
        .order_by('id')
        .first()
    )


def generate_unique_access_code():
    while True:
        code = ''.join(str(secrets.randbelow(10)) for _ in range(6))
        if not EmployeeAccess.objects.filter(access_code=code).exists():
            return code


def issue_employee_access(*, employee, role, actor):
    if role.code == PROTECTED_OUP_ROLE_CODE:
        raise ValidationError(
            'Специалист ОУП не может выдавать или изменять роль администратора.',
            code='admin_role_forbidden',
        )
    if not employee.is_active or employee.status in {
        Employee.Status.DISMISSED,
        Employee.Status.ARCHIVED,
        Employee.Status.DELETED,
    }:
        raise ValidationError('Нельзя выдать доступ неактивному или уволенному сотруднику.')
    if (
        role.code in PRODUCTION_APP_ROLE_CODES
        and employee.personnel_position_id
        and not employee_has_effective_access_role(employee, role.code)
    ):
        raise ValidationError(
            'Это приложение не соответствует действующей производственной специализации сотрудника.',
            code='invalid_work_specialization',
        )
    previous_access = (
        EmployeeAccess.objects.select_for_update()
        .filter(employee=employee, role=role)
        .order_by('id')
        .first()
    )
    previous_state = access_undo_state(previous_access) if previous_access else None
    code = generate_unique_access_code()
    employee_access, created = EmployeeAccess.objects.update_or_create(
        employee=employee,
        role=role,
        defaults={
            'access_code': code,
            'status': EmployeeAccess.Status.NOT_ACTIVATED,
            'is_active': True,
            'primary_code_issued_at': timezone.now(),
            'activated_at': None,
            'deactivated_at': None,
            'blocked_at': None,
            'block_reason': '',
        },
    )
    log_oup_action(
        actor,
        'выдан первичный PIN' if created else 'перевыпущен первичный PIN',
        employee_access,
        action_code=OUP_ACTION_ACCESS_ISSUED if created else OUP_ACTION_ACCESS_REISSUED,
        new_value=f'Сотрудник: {employee.full_name}; роль: {role.name}; первичный PIN: {code}',
        object_repr=f'{employee.full_name} — {role.name}',
        undo_payload={
            'version': 1,
            'before': previous_state,
            'after': access_undo_state(employee_access),
        },
    )
    return employee_access, code, created


def deactivate_employee_access(*, employee_access, actor):
    if employee_access.role.code == PROTECTED_OUP_ROLE_CODE:
        raise ValidationError(
            'Специалист ОУП не может изменять роль администратора.',
            code='admin_role_forbidden',
        )
    if EmployeeShift.objects.filter(employee=employee_access.employee, closed_at__isnull=True).exists():
        raise ValidationError('Сначала завершите открытую рабочую смену сотрудника.')
    previous_state = access_undo_state(employee_access)
    employee_access.status = EmployeeAccess.Status.DEACTIVATED
    employee_access.is_active = False
    employee_access.deactivated_at = timezone.now()
    employee_access.save(update_fields=['status', 'is_active', 'deactivated_at'])
    log_oup_action(
        actor,
        'отключён доступ сотрудника',
        employee_access,
        action_code=OUP_ACTION_ACCESS_DEACTIVATED,
        old_value=f'Роль: {employee_access.role.name}',
        new_value='Доступ отключён',
        object_repr=f'{employee_access.employee.full_name} — {employee_access.role.name}',
        undo_payload={
            'version': 1,
            'before': previous_state,
            'after': access_undo_state(employee_access),
        },
    )
    return employee_access


def get_open_oup_shift(employee):
    return (
        EmployeeShift.objects
        .filter(employee=employee, workplace_code=OUP_ROLE_CODE, closed_at__isnull=True)
        .order_by('-opened_at')
        .first()
    )


def lock_open_oup_shift(*, employee):
    shift = (
        EmployeeShift.objects.select_for_update(of=('self',))
        .filter(employee=employee, workplace_code=OUP_ROLE_CODE, closed_at__isnull=True)
        .order_by('id')
        .first()
    )
    if not shift:
        raise ValidationError(
            'Сначала начните рабочий период ОУП.',
            code='oup_shift_required',
        )
    return shift


def _lock_oup_actor(*, employee):
    try:
        oup_role = Role.objects.select_for_update().get(code=OUP_ROLE_CODE, is_active=True)
    except Role.DoesNotExist as error:
        raise ValidationError('Роль ОУП не настроена.', code='oup_role_unavailable') from error

    employee = Employee.objects.select_for_update().get(pk=employee.pk)
    if not employee.is_active or employee.status != Employee.Status.ACTIVE:
        raise ValidationError('Сотрудник ОУП неактивен или уволен.')

    active_access = (
        EmployeeAccess.objects.select_for_update()
        .filter(
            employee=employee,
            role=oup_role,
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        .order_by('id')
        .first()
    )
    if not active_access:
        raise ValidationError('Активированный доступ ОУП не найден.')
    return employee


def lock_oup_write_context(*, employee):
    employee = _lock_oup_actor(employee=employee)
    shift = lock_open_oup_shift(employee=employee)
    return employee, shift


@transaction.atomic
def open_oup_shift(*, employee):
    employee = _lock_oup_actor(employee=employee)
    current = get_open_oup_shift(employee)
    if current:
        return current, False

    other_workplace_shift = (
        EmployeeShift.objects.select_for_update()
        .filter(employee=employee, closed_at__isnull=True)
        .exclude(workplace_code=OUP_ROLE_CODE)
        .first()
    )
    if other_workplace_shift:
        raise ValidationError('Сначала завершите открытую смену в другом рабочем контуре.')

    other_shift = (
        EmployeeShift.objects.select_for_update(of=('self',))
        .filter(
            closed_at__isnull=True,
            workplace_code=OUP_ROLE_CODE,
        )
        .exclude(employee=employee)
        .select_related('employee')
        .order_by('id')
        .first()
    )
    if other_shift:
        opened_at = timezone.localtime(other_shift.opened_at)
        raise ValidationError(
            'Рабочий период ОУП уже занят: '
            f'{other_shift.employee.full_name}, с {opened_at:%d.%m.%Y %H:%M}.'
        )

    now = timezone.now()
    shift = EmployeeShift.objects.create(
        employee=employee,
        shift_type=ShiftType.DAY,
        workplace_code=OUP_ROLE_CODE,
        equipment=None,
        opened_at=now,
        opened_by=employee,
    )
    log_oup_action(
        employee,
        'начат рабочий период',
        shift,
        action_code=OUP_ACTION_PERIOD_STARTED,
        new_value=f'Начало: {timezone.localtime(now):%d.%m.%Y %H:%M}',
        object_repr='Рабочий период ОУП',
    )
    return shift, True


@transaction.atomic
def close_oup_shift(*, employee):
    employee, shift = lock_oup_write_context(employee=employee)
    now = timezone.now()
    shift.closed_at = now
    shift.closed_by = employee
    shift.save(update_fields=['closed_at', 'closed_by'])
    log_oup_action(
        employee,
        'завершён рабочий период',
        shift,
        action_code=OUP_ACTION_PERIOD_FINISHED,
        old_value=f'Начало: {timezone.localtime(shift.opened_at):%d.%m.%Y %H:%M}',
        new_value=f'Завершение: {timezone.localtime(now):%d.%m.%Y %H:%M}',
        object_repr='Рабочий период ОУП',
    )
    return shift


def employee_dismissal_blockers(employee):
    blockers = []
    open_shift = (
        EmployeeShift.objects
        .filter(employee=employee, closed_at__isnull=True)
        .select_related('equipment')
        .first()
    )
    if open_shift:
        blockers.append({
            'code': 'open_shift',
            'title': 'Открыта рабочая смена',
            'detail': (
                f'{open_shift.get_shift_type_display()}, открыта {timezone.localtime(open_shift.opened_at):%d.%m.%Y %H:%M}. '
                'Сначала выполните служебное закрытие смены.'
            ),
        })

    open_trip = (
        Trip.objects
        .filter(
            Q(driver=employee)
            | Q(excavator_operator=employee)
            | Q(loading_shift__employee=employee)
            | Q(unloading_shift__employee=employee),
            status__in=OPEN_TRIP_STATUSES,
        )
        .select_related('truck', 'excavator')
        .first()
    )
    if open_trip:
        blockers.append({
            'code': 'open_trip',
            'title': 'Есть незавершенный рейс',
            'detail': f'{open_trip.truck} → {open_trip.dump_point}. Сначала завершите или отмените рейс.',
        })
    return blockers


@transaction.atomic
def dismiss_employee(*, employee, actor, dismissed_at, reason=''):
    actor, _shift = lock_oup_write_context(employee=actor)
    draft_plan_ids = lock_current_crew_drafts()

    employee = Employee.objects.select_for_update().get(pk=employee.pk)
    if employee.pk == actor.pk:
        raise ValidationError('Специалист ОУП не может уволить самого себя.')
    if employee.status in {
        Employee.Status.DISMISSED,
        Employee.Status.ARCHIVED,
        Employee.Status.DELETED,
    }:
        raise ValidationError('Сотрудник уже выведен из рабочих списков.')
    if dismissed_at > timezone.localdate():
        raise ValidationError('Будущее увольнение в этой версии не поддерживается.')
    if employee.hired_at and dismissed_at < employee.hired_at:
        raise ValidationError('Дата увольнения не может быть раньше даты приема.')

    blockers = employee_dismissal_blockers(employee)
    if blockers:
        raise ValidationError([item['detail'] for item in blockers])

    before = employee_audit_snapshot(employee)
    employee_before_state = employee_status_undo_state(employee)
    employee_accesses = list(
        EmployeeAccess.objects.select_for_update()
        .filter(employee=employee)
        .order_by('id')
    )
    access_states = {
        item.id: access_undo_state(item)
        for item in employee_accesses
    }
    active_assignments = list(
        EquipmentAssignment.objects.select_for_update()
        .filter(
            employee=employee,
            status__in=[AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED],
            ended_at__isnull=True,
            shift__isnull=True,
            role__isnull=False,
            shift_type__isnull=False,
        )
        .order_by('id')
    )
    assignment_states = {
        item.id: assignment_undo_state(item)
        for item in active_assignments
    }
    clear_active_equipment_assignment(employee=employee, assigned_by=actor)
    deactivated_at = timezone.now()
    for employee_access in employee_accesses:
        employee_access.status = EmployeeAccess.Status.DEACTIVATED
        employee_access.is_active = False
        employee_access.deactivated_at = deactivated_at
    if employee_accesses:
        EmployeeAccess.objects.bulk_update(
            employee_accesses,
            ['status', 'is_active', 'deactivated_at'],
        )

    affected_plan_ids = list(
        CrewPlanSlot.objects.filter(plan_id__in=draft_plan_ids)
        .filter(Q(employee=employee) | Q(baseline_employee=employee))
        .values_list('plan_id', flat=True)
        .distinct()
    )
    if affected_plan_ids:
        CrewPlanSlot.objects.filter(plan_id__in=affected_plan_ids, employee=employee).update(employee=None)
        CrewPlanSlot.objects.filter(
            plan_id__in=affected_plan_ids,
            baseline_employee=employee,
        ).update(baseline_employee=None)
        CrewPlan.objects.filter(id__in=affected_plan_ids).update(
            version=F('version') + 1,
            updated_by=actor,
        )

    employee.status = Employee.Status.DISMISSED
    employee.is_active = False
    employee.dismissed_at = dismissed_at
    employee.save(update_fields=['status', 'is_active', 'dismissed_at', 'updated_at'])
    for employee_access in employee_accesses:
        employee_access.refresh_from_db()
    for assignment in active_assignments:
        assignment.refresh_from_db()
    after = employee_audit_snapshot(employee)
    log_oup_action(
        actor,
        'уволен сотрудник',
        employee,
        action_code=OUP_ACTION_EMPLOYEE_DISMISSED,
        old_value=f'Статус: Работает; {format_employee_changes(before, after)}',
        new_value=f'Уволен с {dismissed_at:%d.%m.%Y}; исключен из рабочих списков',
        comment=reason,
        undo_payload=dismissal_undo_payload(
            employee_before=employee_before_state,
            employee_after=employee_status_undo_state(employee),
            accesses=[
                {
                    'id': item.id,
                    'before': access_states[item.id],
                    'after': access_undo_state(item),
                }
                for item in employee_accesses
            ],
            assignments=[
                {
                    'id': item.id,
                    'before': assignment_states[item.id],
                    'after': assignment_undo_state(item),
                }
                for item in active_assignments
            ],
        ),
    )
    emit_employee_changed(employee, 'dismissed')
    return employee
