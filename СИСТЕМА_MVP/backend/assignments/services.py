from datetime import timedelta

from django.db import IntegrityError, transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from core.models import bump_operational_state
from references.models import Equipment
from shifts.models import EmployeeShift

from .models import AssignmentStatus, EquipmentAssignment, HaulAssignment, HaulAssignmentAction, WorkShiftType


HAUL_ASSIGNMENT_DELAY = timedelta(minutes=5)
WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES = {
    'driver': 'Самосвал',
    'excavator_operator': 'Экскаватор',
}


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
    from users.models import EmployeeAccess

    if role.code not in WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES:
        raise ValidationError('Для этой роли назначение техники не поддерживается.')
    if shift_type not in WorkShiftType.values:
        raise ValidationError('Выберите смену 1 или смену 2.')
    has_role_access = EmployeeAccess.objects.filter(
        employee=employee,
        role=role,
        is_active=True,
    ).exclude(status=EmployeeAccess.Status.DEACTIVATED).exists()
    if not has_role_access:
        raise ValidationError('Сначала выдайте сотруднику активный доступ для выбранной роли.')
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
    employee.__class__.objects.select_for_update().get(pk=employee.pk)
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
def schedule_haul_assignment(*, truck, excavator, assigned_by=None, now=None):
    now = now or timezone.now()
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
        HaulAssignment.objects.select_for_update()
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
def apply_pending_haul_assignment(assignment_id, *, now=None):
    now = now or timezone.now()
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

    _emit_assignment_changed(
        action=applied_action, truck_id=pending.truck_id,
        excavator_ids=excavator_ids, assignment_id=pending.id,
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
