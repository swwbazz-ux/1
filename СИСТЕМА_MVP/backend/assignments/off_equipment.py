"""Off-equipment statuses share the employee lock with crew publication."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone

from core.models import bump_operational_state
from shifts.models import EmployeeShift
from users.models import AdminActionLog, Employee

from .models import AssignmentStatus, CrewOffEquipment, CrewPlanSlot, CrewPlanStatus, EquipmentAssignment, OffEquipmentStatus


BLOCKING_STATUSES = (OffEquipmentStatus.OTHER_WORK, OffEquipmentStatus.SUSPENDED)


def off_equipment_on(work_date, *, employee_ids=None):
    latest = CrewOffEquipment.objects.filter(
        employee_id=OuterRef('employee_id'), work_date__lte=work_date,
    ).order_by('-work_date').values('pk')[:1]
    entries = CrewOffEquipment.objects.filter(pk=Subquery(latest)).filter(
        Q(work_date=work_date) | Q(status=OffEquipmentStatus.SUSPENDED),
    ).exclude(status=OffEquipmentStatus.CLEARED)
    if employee_ids is not None:
        entries = entries.filter(employee_id__in=employee_ids)
    return entries


def blocked_off_equipment_ids(work_date):
    return set(off_equipment_on(work_date).filter(status__in=BLOCKING_STATUSES).values_list('employee_id', flat=True))


def validate_off_equipment_assignment(work_date, employee_ids):
    blocked = (
        off_equipment_on(work_date, employee_ids=employee_ids)
        .filter(status__in=BLOCKING_STATUSES).select_related('employee').order_by('employee_id').first()
    )
    if blocked:
        raise ValidationError(
            f'{blocked.employee.full_name}: {blocked.get_status_display()}. '
            'Снимите отметку во вкладке «Без техники» перед назначением.',
            code='crew_off_equipment',
        )


def booked_on_equipment_ids(work_date, *, include_drafts=False, employee_id=None):
    slot_statuses = [CrewPlanStatus.PUBLISHED]
    if include_drafts:
        slot_statuses.append(CrewPlanStatus.DRAFT)
    slots = CrewPlanSlot.objects.filter(plan__work_date=work_date, plan__status__in=slot_statuses)
    assignments = EquipmentAssignment.objects.filter(
        status__in=[AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED], ended_at__isnull=True,
    )
    shifts = EmployeeShift.objects.filter(closed_at__isnull=True, equipment__isnull=False)
    if employee_id is not None:
        slots = slots.filter(Q(employee_id=employee_id) | Q(secondary_employee_id=employee_id) | Q(reported_employee_id=employee_id))
        return {employee_id} if (
            slots.exists() or assignments.filter(employee_id=employee_id).exists() or shifts.filter(employee_id=employee_id).exists()
        ) else set()
    booked = {person for slot in slots.values_list('employee_id', 'secondary_employee_id', 'reported_employee_id')
              for person in slot if person is not None}
    booked.update(assignments.values_list('employee_id', flat=True))
    booked.update(shifts.values_list('employee_id', flat=True))
    return booked


@transaction.atomic
def publish_off_equipment(*, employee_id, work_date, status, shift_type, location, actor, validate_employee, expected_version):
    employee = Employee.objects.select_for_update().get(pk=employee_id)
    validate_employee(employee)
    previous = off_equipment_on(work_date, employee_ids=[employee.pk]).first()
    if expected_version != (previous.updated_at.isoformat() if previous else ''):
        raise ValidationError('Статус уже изменился. Обновите список и повторите действие.')
    if previous and previous.status == OffEquipmentStatus.SUSPENDED and status not in {
        OffEquipmentStatus.SUSPENDED, OffEquipmentStatus.CLEARED,
    }:
        raise ValidationError('Сначала подтвердите возврат сотрудника к управлению техникой.')
    if status != OffEquipmentStatus.CLEARED and booked_on_equipment_ids(
        work_date, include_drafts=True, employee_id=employee.pk,
    ):
        raise ValidationError('Сначала снимите сотрудника с техники и опубликуйте расстановку.')
    if status == OffEquipmentStatus.CLEARED and not previous:
        return
    if previous and previous.work_date == work_date and (
        previous.status, previous.shift_type, previous.location
    ) == (status, shift_type, location):
        return
    entry, _ = CrewOffEquipment.objects.update_or_create(
        work_date=work_date, employee=employee,
        defaults={'status': status, 'shift_type': shift_type, 'location': location, 'updated_by': actor},
    )
    AdminActionLog.objects.create(
        actor=actor, action='Заместитель начальника: обновлён статус без техники',
        action_code='crew_off_equipment_published', object_type='CrewOffEquipment', object_id=str(entry.pk),
        object_repr=str(employee), old_value=previous.get_status_display() if previous else '',
        new_value=entry.get_status_display(),
    )
    bump_operational_state(
        'CrewOffEquipment:published', event_type='personnel_assignment_changed',
        object_type='CrewOffEquipment', object_id=entry.pk, payload={'employee_ids': [employee.pk]},
    )


def clear_assigned_reserves(work_date, employee_ids, actor):
    CrewOffEquipment.objects.filter(
        work_date=work_date, employee_id__in=employee_ids, status=OffEquipmentStatus.RESERVE,
    ).update(status=OffEquipmentStatus.CLEARED, shift_type='', location='', updated_by=actor, updated_at=timezone.now())
