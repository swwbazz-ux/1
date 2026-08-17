from dataclasses import dataclass
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from shifts.models import WatchPeriod

from .models import (
    AccommodationAnchor,
    AccommodationAnchorBedAssignment,
    AccommodationAnchorCalendarSlot,
    EmployeeAccommodationBinding,
    PhysicalBed,
    SettlementRevision,
    SettlementResident,
)
from .residents import build_settlement_resident_lock_plan, lock_settlement_resident_plan


@dataclass(frozen=True)
class _CalendarBindingLockPlan:
    resident_plan: object
    watch_period_ids: tuple[int, ...]
    slot_snapshots: tuple[tuple[int, int, int, object, object], ...]
    binding_snapshots: tuple[tuple[int, int, int, object, object, str], ...]
    anchor_ids: tuple[int, ...]
    assignment_snapshots: tuple[tuple[int, int, int], ...]
    bed_ids: tuple[int, ...]


@dataclass(frozen=True)
class _LockedCalendarBindingRows:
    subjects: object
    slots: tuple[AccommodationAnchorCalendarSlot, ...]
    bindings: tuple[EmployeeAccommodationBinding, ...]

    def slot_by_id(self, slot_id):
        for slot in self.slots:
            if slot.pk == slot_id:
                return slot
        raise KeyError(slot_id)

    def binding_by_id(self, binding_id):
        for binding in self.bindings:
            if binding.pk == binding_id:
                return binding
        raise KeyError(binding_id)


def _confirmed_revision(revision_id):
    revision = SettlementRevision.objects.get(pk=revision_id)
    if revision.status != SettlementRevision.Status.CONFIRMED:
        raise ValidationError('Операция M4 требует подтверждённой ревизии.')
    return revision


def _overlap_query(windows):
    query = Q(pk__in=())
    for valid_from, valid_to in windows:
        query |= Q(valid_from__lte=valid_to, valid_to__gte=valid_from)
    return query


def _build_calendar_binding_lock_plan(
    *,
    resident_ids,
    employee_ids=(),
    slot_ids,
    binding_ids=(),
    windows,
    require_active=True,
):
    requested_slot_ids = tuple(sorted(set(slot_ids)))
    requested_binding_ids = tuple(sorted(set(binding_ids)))
    requested_slot_snapshots = tuple(
        AccommodationAnchorCalendarSlot.objects.filter(pk__in=requested_slot_ids)
        .order_by('pk')
        .values_list('pk', 'watch_period_id', 'anchor_id', 'valid_from', 'valid_to')
    )
    if tuple(item[0] for item in requested_slot_snapshots) != requested_slot_ids:
        raise ValidationError('M4 lock plan содержит отсутствующий CalendarSlot.')

    normalized_windows = tuple(sorted(set(windows)))
    slot_snapshots = tuple(
        AccommodationAnchorCalendarSlot.objects.filter(_overlap_query(normalized_windows))
        .order_by('pk')
        .values_list('pk', 'watch_period_id', 'anchor_id', 'valid_from', 'valid_to')
    )
    slot_snapshot_by_id = {item[0]: item for item in slot_snapshots}
    for item in requested_slot_snapshots:
        slot_snapshot_by_id[item[0]] = item
    slot_snapshots = tuple(slot_snapshot_by_id[key] for key in sorted(slot_snapshot_by_id))

    binding_snapshots = tuple(
        EmployeeAccommodationBinding.objects.filter(_overlap_query(normalized_windows))
        .order_by('pk')
        .values_list(
            'pk', 'resident_id', 'anchor_calendar_slot_id',
            'valid_from', 'valid_to', 'status',
        )
    )
    binding_snapshot_by_id = {item[0]: item for item in binding_snapshots}
    if requested_binding_ids:
        requested_binding_snapshots = tuple(
            EmployeeAccommodationBinding.objects.filter(pk__in=requested_binding_ids)
            .order_by('pk')
            .values_list(
                'pk', 'resident_id', 'anchor_calendar_slot_id',
                'valid_from', 'valid_to', 'status',
            )
        )
        if tuple(item[0] for item in requested_binding_snapshots) != requested_binding_ids:
            raise EmployeeAccommodationBinding.DoesNotExist
        for item in requested_binding_snapshots:
            binding_snapshot_by_id[item[0]] = item
    binding_snapshots = tuple(
        binding_snapshot_by_id[key] for key in sorted(binding_snapshot_by_id)
    )

    all_resident_ids = tuple(sorted({
        *resident_ids,
        *(item[1] for item in binding_snapshots),
    }))
    resident_plan = build_settlement_resident_lock_plan(
        resident_ids=all_resident_ids,
        employee_ids=employee_ids,
        require_active=require_active,
        active_resident_ids=tuple(resident_ids),
    )
    anchor_ids = tuple(sorted({item[2] for item in slot_snapshots}))
    assignment_snapshots = tuple(
        AccommodationAnchorBedAssignment.objects.filter(
            anchor_id__in=anchor_ids,
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
        )
        .order_by('pk')
        .values_list('pk', 'anchor_id', 'physical_bed_id')
    )
    return _CalendarBindingLockPlan(
        resident_plan=resident_plan,
        watch_period_ids=tuple(sorted({item[1] for item in slot_snapshots})),
        slot_snapshots=slot_snapshots,
        binding_snapshots=binding_snapshots,
        anchor_ids=anchor_ids,
        assignment_snapshots=assignment_snapshots,
        bed_ids=tuple(sorted({item[2] for item in assignment_snapshots})),
    )


def _lock_calendar_binding_plan(plan):
    subjects = lock_settlement_resident_plan(plan.resident_plan)
    periods = tuple(
        WatchPeriod.objects.select_for_update(of=('self',))
        .filter(pk__in=plan.watch_period_ids)
        .order_by('pk')
    )
    if tuple(period.pk for period in periods) != plan.watch_period_ids:
        raise ValidationError('WatchPeriod исчез после построения M4 lock plan.')

    slots = tuple(
        AccommodationAnchorCalendarSlot.objects.select_for_update(of=('self',))
        .select_related('watch_period', 'anchor', 'source_revision')
        .filter(pk__in=tuple(item[0] for item in plan.slot_snapshots))
        .order_by('pk')
    )
    actual_slot_snapshots = tuple(
        (slot.pk, slot.watch_period_id, slot.anchor_id, slot.valid_from, slot.valid_to)
        for slot in slots
    )
    if actual_slot_snapshots != plan.slot_snapshots:
        raise ValidationError('CalendarSlot изменился после построения M4 lock plan.')

    bindings = tuple(
        EmployeeAccommodationBinding.objects.select_for_update(of=('self',))
        .select_related('resident__employee', 'anchor_calendar_slot', 'source_revision')
        .filter(pk__in=tuple(item[0] for item in plan.binding_snapshots))
        .order_by('pk')
    )
    actual_binding_snapshots = tuple(
        (
            binding.pk, binding.resident_id, binding.anchor_calendar_slot_id,
            binding.valid_from, binding.valid_to, binding.status,
        )
        for binding in bindings
    )
    if actual_binding_snapshots != plan.binding_snapshots:
        raise ValidationError('Binding изменился после построения M4 lock plan.')

    anchors = tuple(
        AccommodationAnchor.objects.select_for_update(of=('self',))
        .filter(pk__in=plan.anchor_ids)
        .order_by('pk')
    )
    if tuple(anchor.pk for anchor in anchors) != plan.anchor_ids:
        raise ValidationError('Anchor исчез после построения M4 lock plan.')
    assignments = tuple(
        AccommodationAnchorBedAssignment.objects.select_for_update(of=('self',))
        .filter(pk__in=tuple(item[0] for item in plan.assignment_snapshots))
        .order_by('pk')
    )
    if tuple(
        (assignment.pk, assignment.anchor_id, assignment.physical_bed_id)
        for assignment in assignments
    ) != plan.assignment_snapshots:
        raise ValidationError('AnchorBedAssignment изменился после построения M4 lock plan.')
    beds = tuple(
        PhysicalBed.objects.select_for_update(of=('self',))
        .filter(pk__in=plan.bed_ids)
        .order_by('pk')
    )
    if tuple(bed.pk for bed in beds) != plan.bed_ids:
        raise ValidationError('PhysicalBed исчез после построения M4 lock plan.')
    return _LockedCalendarBindingRows(subjects=subjects, slots=slots, bindings=bindings)


def _validate_resident_slot(*, resident, slot):
    if (
        resident.resident_type == SettlementResident.ResidentType.EMPLOYEE
        and resident.employee.watch_composition_id != slot.watch_composition_id
    ):
        raise ValidationError({
            'resident': 'Внутренний Employee не принадлежит WatchComposition календарного слота.',
        })


def _lock_slot_confirmation_context(slot):
    list(
        AccommodationAnchorCalendarSlot.objects.select_for_update(of=('self',))
        .filter(valid_from__lte=slot.valid_to, valid_to__gte=slot.valid_from)
        .exclude(pk=slot.pk)
        .order_by('pk')
        .values_list('pk', flat=True)
    )
    AccommodationAnchor.objects.select_for_update(of=('self',)).get(pk=slot.anchor_id)
    assignments = tuple(
        AccommodationAnchorBedAssignment.objects.select_for_update(of=('self',))
        .filter(
            anchor_id=slot.anchor_id,
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
        )
        .order_by('pk')
    )
    list(
        PhysicalBed.objects.select_for_update(of=('self',))
        .filter(pk__in=tuple(sorted({item.physical_bed_id for item in assignments})))
        .order_by('pk')
        .values_list('pk', flat=True)
    )


@transaction.atomic
def create_calendar_slot(*, anchor_id, watch_period_id, source_revision_id):
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
    _lock_slot_confirmation_context(slot)
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
    *, resident_id, calendar_slot_id, valid_from, valid_to, basis_type, basis_id,
    basis_snapshot, source_revision_id, supersedes_id=None,
):
    plan = _build_calendar_binding_lock_plan(
        resident_ids=(resident_id,),
        slot_ids=(calendar_slot_id,),
        binding_ids=(supersedes_id,) if supersedes_id else (),
        windows=((valid_from, valid_to),),
    )
    locked = _lock_calendar_binding_plan(plan)
    resident = locked.subjects.resident_by_id(resident_id)
    slot = locked.slot_by_id(calendar_slot_id)
    _validate_resident_slot(resident=resident, slot=slot)
    if supersedes_id and locked.binding_by_id(supersedes_id).resident_id != resident_id:
        raise ValidationError('Постоянная коррекция должна относиться к тому же жильцу.')
    binding = EmployeeAccommodationBinding(
        resident=resident,
        anchor_calendar_slot=slot,
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
        .values('resident_id', 'anchor_calendar_slot_id', 'valid_from', 'valid_to')
        .first()
    )
    if snapshot is None:
        raise EmployeeAccommodationBinding.DoesNotExist
    plan = _build_calendar_binding_lock_plan(
        resident_ids=(snapshot['resident_id'],),
        employee_ids=(approved_by_id,),
        slot_ids=(snapshot['anchor_calendar_slot_id'],),
        binding_ids=(binding_id,),
        windows=((snapshot['valid_from'], snapshot['valid_to']),),
    )
    locked = _lock_calendar_binding_plan(plan)
    binding = locked.binding_by_id(binding_id)
    resident = locked.subjects.resident_by_id(binding.resident_id)
    slot = locked.slot_by_id(binding.anchor_calendar_slot_id)
    _validate_resident_slot(resident=resident, slot=slot)
    if binding.status == binding.Status.CONFIRMED:
        return binding
    if binding.status != binding.Status.DRAFT:
        raise ValidationError('Подтвердить можно только черновик binding.')
    binding.status = binding.Status.CONFIRMED
    binding.approved_by_id = approved_by_id
    binding.approved_at = approved_at or timezone.now()
    binding.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])
    return binding


@transaction.atomic
def close_employee_accommodation_binding(
    *, binding_id, valid_to, closed_revision_id, closed_by_id, closed_at=None,
):
    snapshot = (
        EmployeeAccommodationBinding.objects.filter(pk=binding_id)
        .values('resident_id', 'anchor_calendar_slot_id', 'valid_from', 'valid_to')
        .first()
    )
    if snapshot is None:
        raise EmployeeAccommodationBinding.DoesNotExist
    plan = _build_calendar_binding_lock_plan(
        resident_ids=(snapshot['resident_id'],),
        employee_ids=(closed_by_id,),
        slot_ids=(snapshot['anchor_calendar_slot_id'],),
        binding_ids=(binding_id,),
        windows=((snapshot['valid_from'], snapshot['valid_to']),),
        require_active=False,
    )
    locked = _lock_calendar_binding_plan(plan)
    binding = locked.binding_by_id(binding_id)
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
    *, binding_id, replacement_calendar_slot_id, replacement_valid_from,
    replacement_valid_to, basis_type, basis_id, basis_snapshot,
    source_revision_id, approved_by_id, approved_at=None,
):
    snapshot = (
        EmployeeAccommodationBinding.objects.filter(pk=binding_id)
        .values('resident_id', 'anchor_calendar_slot_id', 'valid_from', 'valid_to')
        .first()
    )
    if snapshot is None:
        raise EmployeeAccommodationBinding.DoesNotExist
    plan = _build_calendar_binding_lock_plan(
        resident_ids=(snapshot['resident_id'],),
        employee_ids=(approved_by_id,),
        slot_ids=(snapshot['anchor_calendar_slot_id'], replacement_calendar_slot_id),
        binding_ids=(binding_id,),
        windows=(
            (snapshot['valid_from'], snapshot['valid_to']),
            (replacement_valid_from, replacement_valid_to),
        ),
    )
    locked = _lock_calendar_binding_plan(plan)
    previous = locked.binding_by_id(binding_id)
    resident = locked.subjects.resident_by_id(previous.resident_id)
    replacement_slot = locked.slot_by_id(replacement_calendar_slot_id)
    _validate_resident_slot(resident=resident, slot=replacement_slot)
    if previous.status != previous.Status.CONFIRMED:
        raise ValidationError('Постоянно скорректировать можно только подтверждённый binding.')
    if replacement_valid_from <= previous.valid_from:
        raise ValidationError('Новая редакция binding должна начинаться позже исходной.')

    correction_time = approved_at or timezone.now()
    previous.status = previous.Status.CLOSED
    previous.valid_to = replacement_valid_from - timedelta(days=1)
    previous.closed_revision = _confirmed_revision(source_revision_id)
    previous.closed_by_id = approved_by_id
    previous.closed_at = correction_time
    previous.save(
        update_fields=[
            'status', 'valid_to', 'closed_revision', 'closed_by', 'closed_at', 'updated_at',
        ],
    )
    replacement = EmployeeAccommodationBinding(
        resident=resident,
        anchor_calendar_slot=replacement_slot,
        valid_from=replacement_valid_from,
        valid_to=replacement_valid_to,
        basis_type=basis_type,
        basis_id=basis_id,
        basis_snapshot=basis_snapshot,
        source_revision_id=source_revision_id,
        supersedes=previous,
    )
    replacement.save()
    replacement.status = EmployeeAccommodationBinding.Status.CONFIRMED
    replacement.approved_by_id = approved_by_id
    replacement.approved_at = correction_time
    replacement.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])
    return replacement
