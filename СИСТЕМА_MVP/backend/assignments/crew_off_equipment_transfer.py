"""Publish a single removal from equipment and its replacement duty together."""

from django.core.exceptions import ValidationError
from django.db import transaction

from shifts.models import EmployeeShift
from users.models import Employee

from .models import CrewPlanSlot, CrewPlanStatus
from .off_equipment import off_equipment_on, publish_off_equipment
from .services import (
    _crew_plan_instance,
    _crew_plan_role,
    _locked_deputy_publish_access,
    _validate_current_crew_plan,
    get_or_create_crew_draft,
    publish_crew_plan,
)


OCCUPANT_FIELDS = ('employee', 'secondary_employee', 'reported_employee')


def _occupants(slot, *, baseline=False):
    prefix = 'baseline_' if baseline else ''
    return tuple(getattr(slot, f'{prefix}{field}_id') for field in OCCUPANT_FIELDS)


def _without_employee(people, employee_id):
    return tuple(None if person == employee_id else person for person in people)


def _set_occupants(slots, occupants_by_slot):
    changed = []
    for slot in slots:
        occupants = occupants_by_slot.get((slot.equipment_id, slot.shift_type))
        if occupants is None or occupants == _occupants(slot):
            continue
        for field, employee_id in zip(OCCUPANT_FIELDS, occupants):
            setattr(slot, f'{field}_id', employee_id)
        changed.append(slot)
    if changed:
        # Clear changed rows before restoring occupants so existing swaps still
        # work with the unique crew-position constraints on PostgreSQL.
        CrewPlanSlot.objects.filter(pk__in=[slot.pk for slot in changed]).update(
            employee=None, secondary_employee=None, reported_employee=None,
        )
        CrewPlanSlot.objects.bulk_update(changed, OCCUPANT_FIELDS)
    return bool(changed)


@transaction.atomic
def transfer_off_equipment(
    *, plan_id, expected_plan_version, employee_id, work_date, status, shift_type, location,
    actor_access, validate_employee,
):
    actor, actor_access = _locked_deputy_publish_access(actor_access=actor_access, actor=actor_access.employee)
    plan = _crew_plan_instance(plan_id, for_update=True)
    _crew_plan_role(plan.role)
    _validate_current_crew_plan(plan)
    if plan.status != CrewPlanStatus.DRAFT or plan.version != expected_plan_version or plan.work_date != work_date:
        raise ValidationError('Расстановка изменилась. Обновите данные и откройте карточку заново.', code='stale_version')

    slots = list(plan.slots.select_for_update().order_by('equipment_id', 'shift_type'))
    edited_draft = {
        (slot.equipment_id, slot.shift_type): _without_employee(_occupants(slot), employee_id)
        for slot in slots
    }
    was_published = any(employee_id in _occupants(slot, baseline=True) for slot in slots)
    lock_ids = {employee_id}
    if was_published:
        lock_ids.update(person for slot in slots for person in _occupants(slot, baseline=True) if person)
    locked_people = {
        employee.pk: employee for employee in Employee.objects.select_for_update().filter(pk__in=lock_ids).order_by('pk')
    }
    if employee_id not in locked_people:
        raise ValidationError('Сотрудник не найден.')
    belongs_to_crew = was_published or any(employee_id in _occupants(slot) for slot in slots)
    validate_employee(locked_people[employee_id], belongs_to_crew)
    if EmployeeShift.objects.filter(
        employee_id=employee_id, closed_at__isnull=True, equipment__isnull=False,
    ).exists():
        raise ValidationError('У сотрудника открыта смена на технике. Сначала завершите её.')

    if was_published:
        # Publish the previous baseline with exactly this employee removed.
        # Hand-edited occupants are restored to a new draft, never published here.
        isolated = {
            (slot.equipment_id, slot.shift_type): _without_employee(_occupants(slot, baseline=True), employee_id)
            for slot in slots
        }
        _set_occupants(slots, isolated)
        publish_crew_plan(plan=plan, expected_version=expected_plan_version, actor=actor, actor_access=actor_access)
        plan, _ = get_or_create_crew_draft(role=plan.role, work_date=work_date, actor=actor)
        slots = list(plan.slots.select_for_update().order_by('equipment_id', 'shift_type'))

    if _set_occupants(slots, edited_draft) or not was_published:
        plan.version += 1
        plan.updated_by = actor
        plan.save(update_fields=['version', 'updated_by', 'updated_at'])
    previous = off_equipment_on(work_date, employee_ids=[employee_id]).first()
    publish_off_equipment(
        employee_id=employee_id, work_date=work_date, status=status, shift_type=shift_type, location=location,
        actor=actor, validate_employee=lambda employee: validate_employee(employee, belongs_to_crew),
        expected_version=previous.updated_at.isoformat() if previous else '',
    )
    return plan
