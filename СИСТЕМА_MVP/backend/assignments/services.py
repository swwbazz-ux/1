from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core.models import bump_operational_state

from .models import AssignmentStatus, HaulAssignment, HaulAssignmentAction


HAUL_ASSIGNMENT_DELAY = timedelta(minutes=5)


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
