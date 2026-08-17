from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from users.models import Employee

from .models import (
    AccommodationAnchor,
    AccommodationAnchorBedAssignment,
    AccommodationAnchorCalendarSlot,
    EmployeeAccommodationBinding,
    PhysicalBed,
    SettlementRevision,
)


def _confirmed_revision(revision_id):
    revision = SettlementRevision.objects.get(pk=revision_id)
    if revision.status != SettlementRevision.Status.CONFIRMED:
        raise ValidationError('Операция M4 требует подтверждённой ревизии.')
    return revision


def _lock_slot_physical_context(slot):
    AccommodationAnchor.objects.select_for_update().get(pk=slot.anchor_id)
    assignments = list(
        AccommodationAnchorBedAssignment.objects.select_for_update()
        .filter(
            anchor_id=slot.anchor_id,
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
        )
        .order_by('pk')
    )
    list(
        PhysicalBed.objects.select_for_update()
        .filter(pk__in=sorted({assignment.physical_bed_id for assignment in assignments}))
        .order_by('pk')
        .values_list('pk', flat=True)
    )
    list(
        AccommodationAnchorCalendarSlot.objects.select_for_update()
        .filter(valid_from__lte=slot.valid_to, valid_to__gte=slot.valid_from)
        .exclude(pk=slot.pk)
        .order_by('pk')
        .values_list('pk', flat=True)
    )


@transaction.atomic
def create_calendar_slot(*, anchor_id, watch_period_id, source_revision_id):
    from shifts.models import WatchPeriod

    period = WatchPeriod.objects.select_related('watch_composition').get(pk=watch_period_id)
    if period.watch_composition_id is None:
        raise ValidationError('WatchPeriod без WatchComposition нельзя использовать для M4.')
    slot = AccommodationAnchorCalendarSlot(
        anchor_id=anchor_id,
        watch_composition_id=period.watch_composition_id,
        watch_period_id=period.pk,
        valid_from=period.starts_on,
        valid_to=period.ends_on,
        source_revision_id=source_revision_id,
    )
    slot.save()
    return slot


@transaction.atomic
def confirm_calendar_slot(*, slot_id, approved_by_id, approved_at=None):
    slot = (
        AccommodationAnchorCalendarSlot.objects.select_for_update()
        .select_related('watch_period', 'anchor', 'source_revision')
        .get(pk=slot_id)
    )
    if slot.status == slot.Status.CONFIRMED:
        return slot
    if slot.status != slot.Status.DRAFT:
        raise ValidationError('Подтвердить можно только черновик CalendarSlot.')
    _lock_slot_physical_context(slot)
    slot.status = slot.Status.CONFIRMED
    slot.approved_by_id = approved_by_id
    slot.approved_at = approved_at or timezone.now()
    slot.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])
    return slot


@transaction.atomic
def close_calendar_slot(*, slot_id, closed_revision_id, closed_by_id, closed_at=None):
    slot = AccommodationAnchorCalendarSlot.objects.select_for_update().get(pk=slot_id)
    if slot.status == slot.Status.CLOSED:
        return slot
    if slot.status != slot.Status.CONFIRMED:
        raise ValidationError('Закрыть можно только подтверждённый CalendarSlot.')
    if EmployeeAccommodationBinding.objects.filter(
        anchor_calendar_slot_id=slot.pk,
        status=EmployeeAccommodationBinding.Status.CONFIRMED,
    ).exists():
        raise ValidationError('Сначала закройте подтверждённые binding календарного слота.')
    slot.status = slot.Status.CLOSED
    slot.closed_revision = _confirmed_revision(closed_revision_id)
    slot.closed_by_id = closed_by_id
    slot.closed_at = closed_at or timezone.now()
    slot.save(
        update_fields=['status', 'closed_revision', 'closed_by', 'closed_at', 'updated_at'],
    )
    return slot


@transaction.atomic
def create_employee_accommodation_binding(
    *,
    employee_id,
    calendar_slot_id,
    valid_from,
    valid_to,
    basis_type,
    basis_id,
    basis_snapshot,
    source_revision_id,
    supersedes_id=None,
):
    binding = EmployeeAccommodationBinding(
        employee_id=employee_id,
        anchor_calendar_slot_id=calendar_slot_id,
        valid_from=valid_from,
        valid_to=valid_to,
        basis_type=basis_type,
        basis_id=basis_id,
        basis_snapshot=basis_snapshot,
        source_revision_id=source_revision_id,
        supersedes_id=supersedes_id,
    )
    binding.save()
    return binding


@transaction.atomic
def confirm_employee_accommodation_binding(*, binding_id, approved_by_id, approved_at=None):
    snapshot = (
        EmployeeAccommodationBinding.objects.filter(pk=binding_id)
        .values('employee_id', 'anchor_calendar_slot_id')
        .first()
    )
    if snapshot is None:
        raise EmployeeAccommodationBinding.DoesNotExist
    Employee.objects.select_for_update(of=('self',)).get(pk=snapshot['employee_id'])
    AccommodationAnchorCalendarSlot.objects.select_for_update(of=('self',)).get(
        pk=snapshot['anchor_calendar_slot_id'],
    )
    binding = (
        EmployeeAccommodationBinding.objects.select_for_update(of=('self',))
        .select_related(
            'employee',
            'anchor_calendar_slot__watch_period',
            'anchor_calendar_slot__anchor',
            'source_revision',
        )
        .get(pk=binding_id)
    )
    if (
        binding.employee_id != snapshot['employee_id']
        or binding.anchor_calendar_slot_id != snapshot['anchor_calendar_slot_id']
    ):
        raise ValidationError('Binding изменился после построения M4 lock plan.')
    if binding.status == binding.Status.CONFIRMED:
        return binding
    if binding.status != binding.Status.DRAFT:
        raise ValidationError('Подтвердить можно только черновик binding.')
    _lock_slot_physical_context(binding.anchor_calendar_slot)
    list(
        EmployeeAccommodationBinding.objects.select_for_update()
        .filter(valid_from__lte=binding.valid_to, valid_to__gte=binding.valid_from)
        .exclude(pk=binding.pk)
        .order_by('pk')
        .values_list('pk', flat=True)
    )
    binding.status = binding.Status.CONFIRMED
    binding.approved_by_id = approved_by_id
    binding.approved_at = approved_at or timezone.now()
    binding.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])
    return binding


@transaction.atomic
def close_employee_accommodation_binding(
    *,
    binding_id,
    valid_to,
    closed_revision_id,
    closed_by_id,
    closed_at=None,
):
    snapshot = (
        EmployeeAccommodationBinding.objects.filter(pk=binding_id)
        .values('employee_id', 'anchor_calendar_slot_id')
        .first()
    )
    if snapshot is None:
        raise EmployeeAccommodationBinding.DoesNotExist
    Employee.objects.select_for_update(of=('self',)).get(pk=snapshot['employee_id'])
    AccommodationAnchorCalendarSlot.objects.select_for_update(of=('self',)).get(
        pk=snapshot['anchor_calendar_slot_id'],
    )
    binding = EmployeeAccommodationBinding.objects.select_for_update(of=('self',)).get(pk=binding_id)
    if (
        binding.employee_id != snapshot['employee_id']
        or binding.anchor_calendar_slot_id != snapshot['anchor_calendar_slot_id']
    ):
        raise ValidationError('Binding изменился после построения M4 lock plan.')
    if binding.status == binding.Status.CLOSED:
        return binding
    if binding.status != binding.Status.CONFIRMED:
        raise ValidationError('Закрыть можно только подтверждённый binding.')
    binding.status = binding.Status.CLOSED
    binding.valid_to = valid_to
    binding.closed_revision = _confirmed_revision(closed_revision_id)
    binding.closed_by_id = closed_by_id
    binding.closed_at = closed_at or timezone.now()
    binding.save(
        update_fields=[
            'status', 'valid_to', 'closed_revision', 'closed_by', 'closed_at', 'updated_at',
        ],
    )
    return binding


@transaction.atomic
def supersede_employee_accommodation_binding(
    *,
    binding_id,
    replacement_calendar_slot_id,
    replacement_valid_from,
    replacement_valid_to,
    basis_type,
    basis_id,
    basis_snapshot,
    source_revision_id,
    approved_by_id,
    approved_at=None,
):
    snapshot = (
        EmployeeAccommodationBinding.objects.filter(pk=binding_id)
        .values('employee_id', 'anchor_calendar_slot_id')
        .first()
    )
    if snapshot is None:
        raise EmployeeAccommodationBinding.DoesNotExist
    Employee.objects.select_for_update(of=('self',)).get(pk=snapshot['employee_id'])
    AccommodationAnchorCalendarSlot.objects.select_for_update(of=('self',)).get(
        pk=snapshot['anchor_calendar_slot_id'],
    )
    previous = EmployeeAccommodationBinding.objects.select_for_update(of=('self',)).get(pk=binding_id)
    if (
        previous.employee_id != snapshot['employee_id']
        or previous.anchor_calendar_slot_id != snapshot['anchor_calendar_slot_id']
    ):
        raise ValidationError('Binding изменился после построения M4 lock plan.')
    if previous.status != previous.Status.CONFIRMED:
        raise ValidationError('Постоянно скорректировать можно только подтверждённый binding.')
    if replacement_valid_from <= previous.valid_from:
        raise ValidationError('Новая редакция binding должна начинаться позже исходной.')
    correction_time = approved_at or timezone.now()
    close_employee_accommodation_binding(
        binding_id=previous.pk,
        valid_to=replacement_valid_from - timedelta(days=1),
        closed_revision_id=source_revision_id,
        closed_by_id=approved_by_id,
        closed_at=correction_time,
    )
    replacement = create_employee_accommodation_binding(
        employee_id=previous.employee_id,
        calendar_slot_id=replacement_calendar_slot_id,
        valid_from=replacement_valid_from,
        valid_to=replacement_valid_to,
        basis_type=basis_type,
        basis_id=basis_id,
        basis_snapshot=basis_snapshot,
        source_revision_id=source_revision_id,
        supersedes_id=previous.pk,
    )
    return confirm_employee_accommodation_binding(
        binding_id=replacement.pk,
        approved_by_id=approved_by_id,
        approved_at=correction_time,
    )
