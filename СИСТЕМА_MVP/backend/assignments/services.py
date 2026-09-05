from datetime import timedelta

import hashlib
import logging
import os
from pathlib import Path
import threading
import tempfile
import time

from django.core.exceptions import ValidationError
from django.core.files import locks
from django.db import IntegrityError, connection, transaction
from django.db.models import Max, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

from core.models import bump_operational_state
from core.production_time import production_work_date as contract_production_work_date
from references.models import Equipment
from shifts.models import EmployeeShift
from users.models import Employee, EmployeeAccess, Role
from users.work_profiles import employee_has_effective_access_role

from .models import (
    AssignmentStatus,
    CrewPlan,
    CrewPlanSlot,
    CrewPlanStatus,
    EquipmentAssignment,
    HaulAssignment,
    HaulAssignmentAction,
    WorkShiftType,
)


HAUL_ASSIGNMENT_DELAY = timedelta(minutes=5)
WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES = {
    'driver': 'Самосвал',
    'excavator_operator': 'Экскаватор',
}
CREW_PLAN_ROLE_CODES = frozenset(WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES)


def _bulk_create_published_plan_assignments(assignments):
    assignments = list(assignments)
    for assignment in assignments:
        if (
            assignment.source_kind
            != EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN
            or assignment.source_crew_plan_slot_id is None
        ):
            raise ValidationError(
                'Публикация может создавать только назначения с точным официальным источником.',
                code='invalid_published_assignment_provenance',
            )
        assignment.full_clean()
    with transaction.atomic():
        return EquipmentAssignment._base_manager.bulk_create(assignments)


def production_work_date(now=None):
    """Return the date of the 07:00–07:00 production day."""
    return contract_production_work_date(now)


def _crew_plan_role(role):
    if isinstance(role, Role):
        resolved = role
    else:
        try:
            resolved = Role.objects.get(code=str(role or ''))
        except Role.DoesNotExist as error:
            raise ValidationError('Рабочая роль не найдена.', code='invalid_role') from error
    if resolved.code not in CREW_PLAN_ROLE_CODES:
        raise ValidationError('Для этой роли планирование экипажей не поддерживается.', code='invalid_role')
    if not resolved.is_active:
        raise ValidationError('Рабочая роль неактивна.', code='inactive_role')
    return resolved


def _crew_plan_instance(plan, *, for_update=False):
    plan_id = plan.pk if isinstance(plan, CrewPlan) else plan
    queryset = CrewPlan.objects.select_related('role')
    if for_update:
        queryset = queryset.select_for_update()
    try:
        return queryset.get(pk=plan_id)
    except CrewPlan.DoesNotExist as error:
        raise ValidationError('План расстановки не найден.', code='plan_not_found') from error


def _locked_deputy_publish_access(*, actor_access, actor):
    """Lock and verify the exact server-side deputy access before publication."""
    access_id = getattr(actor_access, 'pk', actor_access)
    try:
        access_snapshot = EmployeeAccess.objects.filter(pk=access_id).values(
            'pk', 'employee_id',
        ).get()
    except (EmployeeAccess.DoesNotExist, TypeError, ValueError) as error:
        raise ValidationError(
            'Активный доступ заместителя не найден.',
            code='deputy_access_required',
        ) from error
    locked_employee = Employee.objects.select_for_update().get(
        pk=access_snapshot['employee_id'],
    )
    access = (
        EmployeeAccess.objects.select_related('role')
        .select_for_update(of=('self',))
        .get(pk=access_snapshot['pk'])
    )
    actor_id = getattr(actor, 'pk', actor)
    if (
        access.employee_id != locked_employee.pk
        or actor_id not in (None, locked_employee.pk)
        or not access.is_active
        or access.status in {
            EmployeeAccess.Status.BLOCKED,
            EmployeeAccess.Status.DEACTIVATED,
        }
        or not locked_employee.is_active
        or locked_employee.status != Employee.Status.ACTIVE
        or not access.role.is_active
        or access.role.code != 'deputy_mining_manager'
    ):
        raise ValidationError(
            'Активный доступ заместителя не найден.',
            code='deputy_access_required',
        )
    return locked_employee, access


def _validate_current_crew_plan(plan):
    if plan.work_date != production_work_date():
        raise ValidationError(
            'Производственные сутки этого черновика уже завершены. Обновите страницу.',
            code='plan_work_date_closed',
        )


def _crew_plan_equipment(equipment):
    if isinstance(equipment, Equipment):
        return equipment
    try:
        return Equipment.objects.select_related('equipment_type', 'model').get(pk=equipment)
    except Equipment.DoesNotExist as error:
        raise ValidationError('Техника не найдена.', code='equipment_not_found') from error


def _crew_plan_employee(employee):
    if employee in (None, ''):
        return None
    if isinstance(employee, Employee):
        return employee
    try:
        return Employee.objects.get(pk=employee)
    except Employee.DoesNotExist as error:
        raise ValidationError('Сотрудник не найден.', code='employee_not_found') from error


def employee_matches_work_role(employee, role):
    return employee_has_effective_access_role(employee, role.code)


def _validate_crew_employee(employee, role):
    if not employee.is_active or employee.status != Employee.Status.ACTIVE:
        raise ValidationError('Сотрудник неактивен.', code='inactive_employee')
    if not employee_matches_work_role(employee, role):
        raise ValidationError(
            'Производственная специализация сотрудника не соответствует выбранной роли.',
            code='invalid_work_category',
        )
    has_other_role_assignment = (
        EquipmentAssignment.objects
        .filter(
            employee=employee,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
            shift__isnull=True,
            role__isnull=False,
            shift_type__in=WorkShiftType.values,
        )
        .exclude(role=role)
        .exists()
    )
    if has_other_role_assignment:
        raise ValidationError(
            'Сотрудник уже назначен по другой рабочей роли.',
            code='assignment_conflict',
        )


def _validate_crew_equipment(equipment, role):
    if not equipment_queryset_for_work_role(role.code).filter(pk=equipment.pk).exists():
        raise ValidationError(
            'Техника неактивна или не соответствует выбранной рабочей роли.',
            code='invalid_equipment',
        )


@transaction.atomic
def get_or_create_crew_draft(*, role, work_date=None, actor=None):
    role = _crew_plan_role(role)
    work_date = work_date or production_work_date()
    existing = (
        CrewPlan.objects.select_for_update()
        .filter(work_date=work_date, role=role, status=CrewPlanStatus.DRAFT)
        .order_by('-revision')
        .first()
    )
    if existing:
        return existing, False

    latest_revision = (
        CrewPlan.objects.filter(work_date=work_date, role=role)
        .aggregate(value=Max('revision'))['value']
        or 0
    )
    try:
        with transaction.atomic():
            plan = CrewPlan.objects.create(
                work_date=work_date,
                role=role,
                revision=latest_revision + 1,
                created_by=actor,
                updated_by=actor,
            )
    except IntegrityError:
        concurrent_draft = (
            CrewPlan.objects
            .filter(work_date=work_date, role=role, status=CrewPlanStatus.DRAFT)
            .order_by('-revision')
            .first()
        )
        if concurrent_draft:
            return concurrent_draft, False
        raise
    equipment = list(equipment_queryset_for_work_role(role.code))
    equipment_ids = [item.id for item in equipment]
    active_assignments = (
        EquipmentAssignment.objects
        .filter(
            role=role,
            equipment_id__in=equipment_ids,
            shift_type__in=WorkShiftType.values,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
            shift__isnull=True,
        )
        .order_by('-assigned_at', '-id')
    )
    baseline_by_slot = {}
    for assignment in active_assignments:
        baseline_by_slot.setdefault(
            (assignment.equipment_id, assignment.shift_type),
            assignment.employee_id,
        )
    slots = []
    for item in equipment:
        for shift_type in WorkShiftType.values:
            employee_id = baseline_by_slot.get((item.id, shift_type))
            slots.append(CrewPlanSlot(
                plan=plan,
                equipment=item,
                shift_type=shift_type,
                employee_id=employee_id,
                baseline_employee_id=employee_id,
            ))
    CrewPlanSlot.objects.bulk_create(slots)
    return plan, True


@transaction.atomic
def update_crew_draft_slot(
    *,
    plan,
    equipment,
    shift_type,
    employee,
    expected_version,
    actor=None,
):
    locked_plan = _crew_plan_instance(plan, for_update=True)
    role = _crew_plan_role(locked_plan.role)
    if locked_plan.status != CrewPlanStatus.DRAFT:
        raise ValidationError('Опубликованный план нельзя редактировать.', code='plan_not_draft')
    _validate_current_crew_plan(locked_plan)
    if locked_plan.version != expected_version:
        raise ValidationError(
            'Черновик уже изменен в другом окне. Обновите данные.',
            code='stale_version',
        )
    if shift_type not in WorkShiftType.values:
        raise ValidationError('Выберите смену 1 или смену 2.', code='invalid_shift')

    equipment = _crew_plan_equipment(equipment)
    _validate_crew_equipment(equipment, role)
    try:
        target_slot = (
            CrewPlanSlot.objects.select_for_update(of=('self',))
            .select_related('employee')
            .get(plan=locked_plan, equipment=equipment, shift_type=shift_type)
        )
    except CrewPlanSlot.DoesNotExist as error:
        raise ValidationError('Слот техники не найден в черновике.', code='slot_not_found') from error

    employee = _crew_plan_employee(employee)
    if employee:
        employee = Employee.objects.select_for_update().get(pk=employee.pk)
        _validate_crew_employee(employee, role)
    if target_slot.employee_id == getattr(employee, 'id', None):
        return locked_plan

    source_slot = None
    if employee:
        source_slot = (
            CrewPlanSlot.objects.select_for_update()
            .filter(plan=locked_plan, employee=employee)
            .exclude(pk=target_slot.pk)
            .first()
        )

    if source_slot:
        displaced_employee_id = target_slot.employee_id
        slot_ids = [source_slot.id, target_slot.id]
        # Clearing both rows first keeps swaps portable across SQLite and PostgreSQL.
        CrewPlanSlot.objects.filter(id__in=slot_ids).update(employee=None)
        source_slot.employee_id = displaced_employee_id
        target_slot.employee = employee
        CrewPlanSlot.objects.bulk_update([source_slot, target_slot], ['employee'])
    else:
        target_slot.employee = employee
        target_slot.save(update_fields=['employee'])

    locked_plan.version += 1
    locked_plan.updated_by = actor
    locked_plan.save(update_fields=['version', 'updated_by', 'updated_at'])
    return locked_plan


@transaction.atomic
def _publish_crew_plan_once(*, plan, expected_version, actor=None, actor_access=None):
    if actor_access is not None:
        actor, actor_access = _locked_deputy_publish_access(
            actor_access=actor_access,
            actor=actor,
        )
    locked_plan = _crew_plan_instance(plan, for_update=True)
    role = _crew_plan_role(locked_plan.role)
    if locked_plan.status != CrewPlanStatus.DRAFT:
        raise ValidationError('Этот план уже опубликован.', code='plan_not_draft')
    _validate_current_crew_plan(locked_plan)
    if locked_plan.version != expected_version:
        raise ValidationError(
            'Черновик уже изменен в другом окне. Обновите данные.',
            code='stale_version',
        )

    slots = list(
        CrewPlanSlot.objects.select_for_update(of=('self',))
        .filter(plan=locked_plan)
        .select_related('equipment', 'equipment__equipment_type', 'employee', 'baseline_employee')
        .order_by('equipment_id', 'shift_type')
    )
    active_equipment = list(equipment_queryset_for_work_role(role.code))
    active_equipment_ids = {item.id for item in active_equipment}
    expected_slot_keys = {
        (equipment_id, shift_type)
        for equipment_id in active_equipment_ids
        for shift_type in WorkShiftType.values
    }
    actual_slot_keys = {(slot.equipment_id, slot.shift_type) for slot in slots}
    if actual_slot_keys != expected_slot_keys:
        raise ValidationError(
            'Состав активной техники изменился. Создайте свежий черновик.',
            code='stale_baseline',
        )

    target_employee_ids = set()
    for slot in slots:
        _validate_crew_equipment(slot.equipment, role)
        if slot.employee_id:
            _validate_crew_employee(slot.employee, role)
            if slot.employee_id in target_employee_ids:
                raise ValidationError(
                    'Сотрудник назначен более чем в один слот.',
                    code='duplicate_employee',
                )
            target_employee_ids.add(slot.employee_id)

    if target_employee_ids:
        list(
            Employee.objects.select_for_update()
            .filter(id__in=target_employee_ids)
            .order_by('id')
        )
        refreshed_employees = Employee.objects.in_bulk(target_employee_ids)
        for slot in slots:
            if slot.employee_id:
                slot.employee = refreshed_employees[slot.employee_id]
                _validate_crew_employee(slot.employee, role)

    current_role_assignments = list(
        EquipmentAssignment.objects.select_for_update()
        .filter(
            role=role,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
            shift__isnull=True,
            shift_type__in=WorkShiftType.values,
        )
        .order_by('-assigned_at', '-id')
    )
    current_by_slot = {}
    for assignment in current_role_assignments:
        current_by_slot.setdefault(
            (assignment.equipment_id, assignment.shift_type),
            assignment.employee_id,
        )
    stale_slots = [
        slot
        for slot in slots
        if current_by_slot.get((slot.equipment_id, slot.shift_type)) != slot.baseline_employee_id
    ]
    if stale_slots:
        raise ValidationError(
            'Базовая расстановка изменилась после создания черновика. Обновите данные.',
            code='stale_baseline',
        )

    target_slot_keys = {
        (slot.equipment_id, slot.shift_type)
        for slot in slots
        if slot.employee_id
    }
    other_role_employee_conflict = (
        EquipmentAssignment.objects.select_for_update()
        .filter(
            employee_id__in=target_employee_ids,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
            shift__isnull=True,
            role__isnull=False,
            shift_type__in=WorkShiftType.values,
        )
        .exclude(role=role)
        .first()
    )
    if other_role_employee_conflict:
        raise ValidationError(
            'Один из сотрудников уже назначен по другой рабочей роли.',
            code='assignment_conflict',
        )

    outside_slot_assignments = list(
        EquipmentAssignment.objects.select_for_update()
        .filter(
            equipment_id__in={key[0] for key in target_slot_keys},
            shift_type__in={key[1] for key in target_slot_keys},
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
            shift__isnull=True,
            role__isnull=False,
        )
        .exclude(role=role)
    )
    outside_slot_conflict = next(
        (
            assignment
            for assignment in outside_slot_assignments
            if (assignment.equipment_id, assignment.shift_type) in target_slot_keys
        ),
        None,
    )
    if outside_slot_conflict:
        raise ValidationError(
            'Один из слотов уже занят назначением вне публикуемого плана.',
            code='assignment_conflict',
        )

    current_scope_assignments = list(
        EquipmentAssignment.objects.select_for_update()
        .filter(
            role=role,
            status__in=(AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED),
            ended_at__isnull=True,
            shift__isnull=True,
            shift_type__in=WorkShiftType.values,
        )
    )
    desired_slots = {
        (slot.equipment_id, slot.shift_type, slot.employee_id): slot
        for slot in slots
        if slot.employee_id
    }
    current_semantic_triples = {
        (assignment.equipment_id, assignment.shift_type, assignment.employee_id)
        for assignment in current_scope_assignments
        if assignment.employee_id
    }
    changed_semantic_triples = current_semantic_triples.symmetric_difference(
        desired_slots.keys()
    )
    unchanged_triples = set()
    for assignment in current_scope_assignments:
        assignment_key = (
            assignment.equipment_id,
            assignment.shift_type,
            assignment.employee_id,
        )
        source_slot = desired_slots.get(assignment_key)
        if (
            assignment.status == AssignmentStatus.ACCEPTED
            and source_slot is not None
            and assignment.source_kind
            == EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN
            and assignment.source_crew_plan_slot_id == source_slot.pk
        ):
            unchanged_triples.add(assignment_key)
    assignments_to_close = [
        assignment
        for assignment in current_scope_assignments
        if assignment.status == AssignmentStatus.PENDING
        or (assignment.equipment_id, assignment.shift_type, assignment.employee_id) not in unchanged_triples
    ]

    now = timezone.now()
    for assignment in assignments_to_close:
        if assignment.status == AssignmentStatus.PENDING:
            assignment.status = AssignmentStatus.CANCELLED
        assignment.ended_at = now
        assignment.ended_by = actor
    if assignments_to_close:
        EquipmentAssignment.objects.bulk_update(
            assignments_to_close,
            ['status', 'ended_at', 'ended_by'],
        )

    new_assignments = [
        EquipmentAssignment(
            employee=slot.employee,
            role=role,
            equipment=slot.equipment,
            shift_type=slot.shift_type,
            assigned_by=actor,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=now,
            source_kind=EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN,
            source_crew_plan_slot=slot,
        )
        for slot in slots
        if slot.employee_id
        and (slot.equipment_id, slot.shift_type, slot.employee_id) not in unchanged_triples
    ]
    # Replacing a legacy/manual row with an equivalent published-plan row changes
    # provenance, not the employee's visible assignment. Keep realtime scope tied
    # to the semantic before/after difference so unchanged workers do not refresh.
    changed_employee_ids = {
        employee_id
        for _equipment_id, _shift_type, employee_id in changed_semantic_triples
        if employee_id
    }
    changed_equipment_ids = {
        equipment_id
        for equipment_id, _shift_type, _employee_id in changed_semantic_triples
        if equipment_id
    }
    (
        CrewPlan.objects.select_for_update()
        .filter(
            work_date=locked_plan.work_date,
            role=role,
            status=CrewPlanStatus.PUBLISHED,
        )
        .exclude(pk=locked_plan.pk)
        .update(status=CrewPlanStatus.SUPERSEDED)
    )
    locked_plan.status = CrewPlanStatus.PUBLISHED
    locked_plan.version += 1
    locked_plan.updated_by = actor
    locked_plan.published_by = actor
    locked_plan.published_at = now
    locked_plan.save(update_fields=[
        'status',
        'version',
        'updated_by',
        'published_by',
        'published_at',
        'updated_at',
    ])
    try:
        _bulk_create_published_plan_assignments(new_assignments)
    except IntegrityError as error:
        raise ValidationError(
            'Публикация конфликтует с другим активным назначением. Обновите данные.',
            code='assignment_conflict',
        ) from error
    if actor_access is not None:
        from rotations.arrival_roster_routing import _record_published_crew_plan_routing

        _record_published_crew_plan_routing(
            plan=locked_plan,
            slots=slots,
            actor_access=actor_access,
        )
    bump_operational_state(
        'CrewPlan:published',
        event_type='personnel_assignment_changed',
        object_type='CrewPlan',
        object_id=locked_plan.id,
        payload={
            'action': 'crew_plan_published',
            'plan_id': locked_plan.id,
            'work_date': locked_plan.work_date.isoformat(),
            'role_code': role.code,
            'employee_ids': sorted(changed_employee_ids),
            'equipment_ids': sorted(changed_equipment_ids),
        },
    )
    return locked_plan


def publish_crew_plan(*, plan, expected_version, actor=None, actor_access=None):
    """Publish once; retain a controlled OUP-role review outside a rolled-back draft."""
    try:
        return _publish_crew_plan_once(
            plan=plan,
            expected_version=expected_version,
            actor=actor,
            actor_access=actor_access,
        )
    except ValidationError as error:
        if actor_access is not None and getattr(error, 'code', '') == 'invalid_work_category':
            from rotations.arrival_roster_routing import _record_crew_plan_role_review

            _record_crew_plan_role_review(
                plan=plan,
                actor_access=actor_access,
            )
        raise


def equipment_queryset_for_work_role(role_code):
    equipment_type_name = WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES.get(str(role_code or ''))
    if not equipment_type_name:
        return Equipment.objects.none()
    return (
        Equipment.objects
        .filter(
            is_active=True,
            equipment_type__is_active=True,
            equipment_type__name__iexact=equipment_type_name,
        )
        .select_related('equipment_type', 'model')
        .distinct()
        .order_by('garage_number')
    )


def get_active_equipment_assignment(employee, role_code=None):
    assignments = (
        EquipmentAssignment.objects
        .filter(
            employee=employee,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
            shift__isnull=True,
            role__isnull=False,
            shift_type__isnull=False,
        )
        .select_related('employee', 'role', 'equipment', 'equipment__equipment_type', 'assigned_by')
        .order_by('-assigned_at', '-id')
    )
    if role_code:
        assignments = assignments.filter(role__code=role_code)
    return assignments.first()


def validate_work_assignment(*, employee, role, equipment, shift_type, exclude_assignment=None):
    if role.code not in WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES:
        raise ValidationError('Для этой роли назначение техники не поддерживается.')
    if shift_type not in WorkShiftType.values:
        raise ValidationError('Выберите смену 1 или смену 2.')
    if not employee.is_active or employee.status not in {
        Employee.Status.ACTIVE,
        Employee.Status.NOT_ACTIVATED,
    }:
        raise ValidationError('Сотрудник неактивен.')
    if not employee_matches_work_role(employee, role):
        raise ValidationError('Производственная специализация сотрудника не соответствует выбранной роли.')
    if not equipment_queryset_for_work_role(role.code).filter(id=equipment.id).exists():
        raise ValidationError('Выбранная техника не соответствует рабочей роли или неактивна.')

    conflict = EquipmentAssignment.objects.filter(
        equipment=equipment,
        shift_type=shift_type,
        status=AssignmentStatus.ACCEPTED,
        ended_at__isnull=True,
        shift__isnull=True,
    ).exclude(employee=employee)
    if exclude_assignment:
        conflict = conflict.exclude(id=exclude_assignment.id)
    if conflict.exists():
        raise ValidationError('Эта техника уже назначена другому сотруднику в выбранной смене.')


def work_assignment_state(employee, assignment):
    if not assignment:
        return 'no_active_assignment'
    if not employee.is_active:
        return 'employee_inactive'
    if not employee.accesses.filter(
        role=assignment.role,
        is_active=True,
    ).exclude(status='deactivated').exists():
        return 'access_inactive'
    if not assignment.equipment.is_active:
        return 'equipment_inactive'
    has_conflicting_shift = EmployeeShift.objects.filter(
        equipment=assignment.equipment,
        closed_at__isnull=True,
    ).exclude(employee=employee).exists()
    if has_conflicting_shift:
        return 'assignment_conflict'
    return 'assigned'


def _emit_work_assignment_changed(assignment, action):
    bump_operational_state(
        f'EquipmentAssignment:{action}',
        event_type='personnel_assignment_changed',
        object_type='EquipmentAssignment',
        object_id=assignment.id if assignment else '',
        payload={
            'action': action,
            'employee_ids': [assignment.employee_id] if assignment else [],
            'equipment_ids': [assignment.equipment_id] if assignment else [],
            'shift_type': assignment.shift_type if assignment else '',
            'role_code': assignment.role.code if assignment and assignment.role_id else '',
        },
    )


@transaction.atomic
def set_active_equipment_assignment(*, employee, role, equipment, shift_type, assigned_by=None, now=None):
    now = now or timezone.now()
    employee = employee.__class__.objects.select_for_update().get(pk=employee.pk)
    equipment = Equipment.objects.select_for_update().get(pk=equipment.pk)
    current = (
        EquipmentAssignment.objects.select_for_update()
        .filter(
            employee=employee,
            status__in=(AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED),
            ended_at__isnull=True,
            shift__isnull=True,
            role__isnull=False,
            shift_type__isnull=False,
        )
        .select_related('role', 'equipment')
        .order_by('-assigned_at', '-id')
        .first()
    )
    validate_work_assignment(
        employee=employee,
        role=role,
        equipment=equipment,
        shift_type=shift_type,
        exclude_assignment=current,
    )
    if (
        current
        and current.status == AssignmentStatus.ACCEPTED
        and current.role_id == role.id
        and current.equipment_id == equipment.id
        and current.shift_type == shift_type
    ):
        return current, False

    open_assignments = list(
        EquipmentAssignment.objects.select_for_update()
        .filter(
            employee=employee,
            status__in=(AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED),
            ended_at__isnull=True,
            shift__isnull=True,
            role__isnull=False,
            shift_type__isnull=False,
        )
    )
    for item in open_assignments:
        if item.status == AssignmentStatus.PENDING:
            item.status = AssignmentStatus.CANCELLED
        item.ended_at = now
        item.ended_by = assigned_by
    if open_assignments:
        EquipmentAssignment.objects.bulk_update(open_assignments, ['status', 'ended_at', 'ended_by'])

    try:
        assignment = EquipmentAssignment.objects.create(
            employee=employee,
            role=role,
            equipment=equipment,
            shift_type=shift_type,
            assigned_by=assigned_by,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=now,
        )
    except IntegrityError as error:
        raise ValidationError('Назначение конфликтует с другим активным назначением. Обновите страницу.') from error
    _emit_work_assignment_changed(assignment, 'assigned')
    return assignment, True


@transaction.atomic
def clear_active_equipment_assignment(*, employee, assigned_by=None, now=None, role_code=None):
    now = now or timezone.now()
    employee.__class__.objects.select_for_update().get(pk=employee.pk)
    assignment_queryset = (
        EquipmentAssignment.objects.select_for_update().filter(
            employee=employee,
            status__in=(AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED),
            ended_at__isnull=True,
            shift__isnull=True,
            role__isnull=False,
            shift_type__isnull=False,
        )
        .select_related('role')
    )
    if role_code:
        assignment_queryset = assignment_queryset.filter(role__code=role_code)
    assignments = list(assignment_queryset)
    for assignment in assignments:
        if assignment.status == AssignmentStatus.PENDING:
            assignment.status = AssignmentStatus.CANCELLED
        assignment.ended_at = now
        assignment.ended_by = assigned_by
    if assignments:
        EquipmentAssignment.objects.bulk_update(assignments, ['status', 'ended_at', 'ended_by'])
        _emit_work_assignment_changed(assignments[0], 'cleared')
    return len(assignments)


def _emit_assignment_changed(*, action, truck_id, excavator_ids, assignment_id=None):
    bump_operational_state(
        f'HaulAssignment:{action}',
        event_type='assignment_changed',
        object_type='HaulAssignment',
        object_id=assignment_id or '',
        payload={
            'action': action,
            'truck_ids': [truck_id],
            'excavator_ids': sorted({value for value in excavator_ids if value}),
        },
    )


def _cancel_assignments(assignments, now):
    changed = []
    for assignment in assignments:
        if assignment.status == AssignmentStatus.CANCELLED and assignment.ended_at:
            continue
        assignment.status = AssignmentStatus.CANCELLED
        assignment.ended_at = now
        changed.append(assignment)
    if changed:
        HaulAssignment.objects.bulk_update(changed, ['status', 'ended_at'])
    return changed


@transaction.atomic
# select_for_update требует активную транзакцию. У соседней schedule_haul_release
# декоратор уже стоял, а здесь его не было — молчало, пока действие «Назначить
# самосвал» не сработало на редком стечении данных, и тогда падало 500-й ошибкой
# ровно на той кнопке, которой горный мастер и диспетчер пользуются каждый день.
def schedule_haul_assignment(*, truck, excavator, assigned_by=None, now=None):
    now = now or timezone.now()
    truck = Equipment.objects.select_for_update().get(pk=truck.pk)
    open_assignments = list(
        HaulAssignment.objects.select_for_update()
        .filter(truck=truck, ended_at__isnull=True)
        .exclude(status=AssignmentStatus.CANCELLED)
        .order_by('-assigned_at', '-id')
    )
    accepted = next((item for item in open_assignments if item.status == AssignmentStatus.ACCEPTED), None)
    current_pending = next((item for item in open_assignments if item.status == AssignmentStatus.PENDING), None)
    if (
        current_pending
        and current_pending.action == HaulAssignmentAction.ASSIGN
        and current_pending.excavator_id == excavator.id
    ):
        return current_pending, False
    cancelled = _cancel_assignments(
        [item for item in open_assignments if item.status == AssignmentStatus.PENDING],
        now,
    )
    if accepted and accepted.excavator_id == excavator.id:
        if cancelled:
            excavator_ids = [accepted.excavator_id, *(item.excavator_id for item in cancelled)]
            _emit_assignment_changed(
                action='pending_cancelled', truck_id=truck.id,
                excavator_ids=excavator_ids, assignment_id=accepted.id,
            )
        return accepted, False

    assignment = HaulAssignment.objects.create(
        truck=truck,
        excavator=excavator,
        assigned_by=assigned_by,
        action=HaulAssignmentAction.ASSIGN,
        status=AssignmentStatus.PENDING,
        effective_at=now + HAUL_ASSIGNMENT_DELAY,
    )
    excavator_ids = [excavator.id, getattr(accepted, 'excavator_id', None)]
    _emit_assignment_changed(
        action='assignment_pending', truck_id=truck.id,
        excavator_ids=excavator_ids, assignment_id=assignment.id,
    )
    return assignment, True


@transaction.atomic
def schedule_haul_release(*, truck, assigned_by=None, now=None):
    now = now or timezone.now()
    open_assignments = list(
        HaulAssignment.objects.select_for_update(of=('self',))
        .filter(truck=truck, ended_at__isnull=True)
        .exclude(status=AssignmentStatus.CANCELLED)
        .select_related('excavator')
        .order_by('-assigned_at', '-id')
    )
    accepted = next((item for item in open_assignments if item.status == AssignmentStatus.ACCEPTED), None)
    pending = [item for item in open_assignments if item.status == AssignmentStatus.PENDING]
    current_pending = pending[0] if pending else None
    if current_pending and current_pending.action == HaulAssignmentAction.RELEASE:
        return current_pending, False
    source = accepted or (pending[0] if pending else None)
    _cancel_assignments(pending, now)
    if not source:
        return None, False

    assignment = HaulAssignment.objects.create(
        truck=truck,
        excavator=source.excavator,
        assigned_by=assigned_by,
        action=HaulAssignmentAction.RELEASE,
        status=AssignmentStatus.PENDING,
        effective_at=now + HAUL_ASSIGNMENT_DELAY,
    )
    _emit_assignment_changed(
        action='release_pending', truck_id=truck.id,
        excavator_ids=[source.excavator_id], assignment_id=assignment.id,
    )
    return assignment, True


@transaction.atomic
def notify_excavator_assignment_changed(assignment, *, released, previous_excavator_ids=()):
    """Сообщает машинисту, что ему дали или забрали самосвал.

    Внутри приложения об этом уже говорит звук и сообщение на экране, но при
    свёрнутом приложении человек ничего не узнает и может ждать машину,
    которую у него забрали. Ошибки отправки проглатываются: уведомление не
    должно ломать саму расстановку.
    """
    from shifts.models import EmployeeShift
    from users.webpush import notify_employee

    try:
        truck_number = (
            getattr(assignment.truck, 'garage_number', '') or 'без номера'
        )
        # При снятии назначение уже закрыто, поэтому берём экскаваторы, которые
        # были заняты этим самосвалом до применения.
        excavator_ids = (
            list(previous_excavator_ids)
            if released
            else [assignment.excavator_id]
        )
        for excavator_id in {item for item in excavator_ids if item}:
            shift = (
                EmployeeShift.objects
                .select_related('employee')
                .filter(equipment_id=excavator_id, closed_at__isnull=True)
                .filter(
                    Q(workplace_code='excavator_operator')
                    | Q(workplace_code='', equipment__equipment_type__name='Экскаватор')
                )
                .order_by('-opened_at')
                .first()
            )
            if not shift or not shift.employee_id:
                continue
            if released:
                title = 'Самосвал снят'
                body = f'{truck_number} больше не закреплён за вами.'
            else:
                title = 'Назначен самосвал'
                body = f'{truck_number} закреплён за вашим экскаватором.'
            notify_employee(
                shift.employee,
                title=title,
                body=body,
                url='/excavator/work/',
                tag='excavator-assignment',
                kind='excavator_assignment_released' if released else 'excavator_assignment_added',
            )
    except Exception:
        logger.exception('Не удалось отправить машинисту уведомление о назначении.')


def apply_pending_haul_assignment(assignment_id, *, now=None):
    # select_for_update требует активную транзакцию — без неё Django отказывает
    # запросом, а не блокировкой. Раньше функция вызывалась без transaction.atomic,
    # и это молчало, пока не появилось первое просроченное назначение: тогда
    # опрос состояния (дергает эту функцию каждые 5 секунд) стал падать 500-й
    # ошибкой у всех, кто в приложениях, — не только у того, чьё назначение.
    now = now or timezone.now()
    with transaction.atomic():
        pending = (
            HaulAssignment.objects.select_for_update()
            .filter(id=assignment_id, status=AssignmentStatus.PENDING, ended_at__isnull=True)
            .first()
        )
        if not pending:
            return None

        open_assignments = list(
            HaulAssignment.objects.select_for_update()
            .filter(truck_id=pending.truck_id, ended_at__isnull=True)
            .exclude(status=AssignmentStatus.CANCELLED)
            .order_by('-assigned_at', '-id')
        )
        excavator_ids = [item.excavator_id for item in open_assignments]
        if pending.action == HaulAssignmentAction.RELEASE:
            _cancel_assignments(open_assignments, now)
            applied_action = 'release_applied'
        else:
            _cancel_assignments([item for item in open_assignments if item.id != pending.id], now)
            pending.status = AssignmentStatus.ACCEPTED
            pending.accepted_at = now
            pending.save(update_fields=['status', 'accepted_at'])
            applied_action = 'assignment_applied'

    # Уведомления — за пределами блокировки: сетевой вызов внутри select_for_update
    # держал бы строки взаперти, пока не ответит сервер уведомлений.
    _emit_assignment_changed(
        action=applied_action, truck_id=pending.truck_id,
        excavator_ids=excavator_ids, assignment_id=pending.id,
    )
    notify_excavator_assignment_changed(
        pending,
        released=pending.action == HaulAssignmentAction.RELEASE,
        previous_excavator_ids=excavator_ids,
    )
    return pending


def reconcile_due_haul_assignments(*, truck_id=None, now=None):
    now = now or timezone.now()
    due = HaulAssignment.objects.filter(
        status=AssignmentStatus.PENDING,
        ended_at__isnull=True,
        effective_at__isnull=False,
        effective_at__lte=now,
    )
    if truck_id:
        due = due.filter(truck_id=truck_id)
    applied = 0
    for assignment_id in due.order_by('effective_at', 'id').values_list('id', flat=True):
        if apply_pending_haul_assignment(assignment_id, now=now):
            applied += 1
    return applied


RECONCILE_INTERVAL_SECONDS = 5


def _reconcile_lock_path():
    database = connection.settings_dict
    identity = '|'.join(
        str(database.get(field) or '')
        for field in ('ENGINE', 'HOST', 'PORT', 'NAME')
    )
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f'accounting-mvp-due-reconcile-v1-{digest}.lock'


RECONCILE_LOCK_PATH = _reconcile_lock_path()
RECONCILE_PROCESS_ONLY = object()
_reconcile_gate = threading.Lock()
_reconcile_next_check = 0.0
_reconcile_running = False


def _acquire_reconcile_process_gate():
    global _reconcile_next_check, _reconcile_running

    now = time.monotonic()
    with _reconcile_gate:
        if _reconcile_running or now < _reconcile_next_check:
            return False
        _reconcile_running = True
        _reconcile_next_check = now + RECONCILE_INTERVAL_SECONDS
    return True


def _release_reconcile_process_gate():
    global _reconcile_running

    with _reconcile_gate:
        _reconcile_running = False


def _acquire_reconcile_server_gate():
    try:
        RECONCILE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        lock_file = RECONCILE_LOCK_PATH.open('a+b')
    except OSError:
        return RECONCILE_PROCESS_ONLY
    locked = False
    try:
        if not locks.lock(lock_file, locks.LOCK_EX | locks.LOCK_NB):
            lock_file.close()
            return None
        locked = True
        lock_file.seek(0)
        try:
            next_check = float(lock_file.read().decode('ascii').strip() or 0)
        except (UnicodeDecodeError, ValueError):
            next_check = 0
        if time.time() < next_check:
            locks.unlock(lock_file)
            locked = False
            lock_file.close()
            return None
        return lock_file
    except OSError:
        if locked:
            try:
                locks.unlock(lock_file)
            except OSError:
                pass
        try:
            lock_file.close()
        except OSError:
            pass
        return RECONCILE_PROCESS_ONLY


def _release_reconcile_server_gate(lock_file):
    try:
        try:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(
                f'{time.time() + RECONCILE_INTERVAL_SECONDS:.6f}'.encode('ascii')
            )
            lock_file.flush()
            os.fsync(lock_file.fileno())
        except OSError:
            pass
    finally:
        try:
            locks.unlock(lock_file)
        except OSError:
            pass
        try:
            lock_file.close()
        except OSError:
            pass


def reconcile_due_haul_assignments_throttled():
    if not _acquire_reconcile_process_gate():
        return 0
    try:
        lock_file = _acquire_reconcile_server_gate()
        if lock_file is None:
            return 0
        try:
            return reconcile_due_haul_assignments()
        finally:
            if lock_file is not RECONCILE_PROCESS_ONLY:
                _release_reconcile_server_gate(lock_file)
    finally:
        _release_reconcile_process_gate()
