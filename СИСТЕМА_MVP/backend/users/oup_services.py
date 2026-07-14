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


OUP_ROLE_CODE = 'oup'
OUP_AUDIT_FIELDS = (
    ('full_name', 'ФИО'),
    ('birth_date', 'Дата рождения'),
    ('personnel_number', 'Табельный номер'),
    ('phone', 'Телефон'),
    ('position', 'Должность'),
    ('department', 'Подразделение'),
    ('work_category', 'Рабочая категория'),
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
    old_value='',
    new_value='',
    comment='',
    object_repr=None,
):
    return AdminActionLog.objects.create(
        actor=actor,
        action=f'ОУП: {action}',
        object_type=obj.__class__.__name__ if obj else '',
        object_id=str(obj.pk) if obj and obj.pk else '',
        object_repr=(str(obj) if obj else '') if object_repr is None else object_repr,
        old_value=old_value,
        new_value=new_value,
        comment=comment,
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
            'status': employee.status,
            'is_active': employee.is_active,
        },
    )


def employee_audit_snapshot(employee):
    snapshot = {}
    for field_name, label in OUP_AUDIT_FIELDS:
        value = getattr(employee, field_name, '')
        if field_name == 'work_category':
            value = employee.get_work_category_display()
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
    clear_active_equipment_assignment(employee=employee, assigned_by=actor)
    EmployeeAccess.objects.filter(employee=employee).update(
        status=EmployeeAccess.Status.DEACTIVATED,
        is_active=False,
        deactivated_at=timezone.now(),
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
    after = employee_audit_snapshot(employee)
    log_oup_action(
        actor,
        'уволен сотрудник',
        employee,
        old_value=f'Статус: Работает; {format_employee_changes(before, after)}',
        new_value=f'Уволен с {dismissed_at:%d.%m.%Y}; исключен из рабочих списков',
        comment=reason,
    )
    emit_employee_changed(employee, 'dismissed')
    return employee
