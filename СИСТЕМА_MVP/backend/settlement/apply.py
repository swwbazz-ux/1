"""Atomic M8 Apply of a confirmed M7 preview to resident-based occupancy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, router, transaction
from django.db.models import Q
from django.utils import timezone
from shifts.models import WatchPeriod

from .control import (
    SettlementControlWriteContext,
    lock_settlement_write_access,
    lock_settlement_write_lease,
)
from .models import (
    AccommodationAnchorBedAssignment,
    AccommodationAnchorCalendarSlot,
    EmployeeBedOccupancy,
    EmployeeAccommodationBinding,
    PhysicalBed,
    PhysicalRoom,
    SettlementCohort,
    SettlementCohortMember,
    SettlementPreviewApplication,
    SettlementPreviewApplicationItem,
    SettlementPreviewCorrection,
    SettlementPreviewPlacement,
    SettlementPreviewRun,
    SettlementPreviewUnresolved,
)
from .bed_availability import active_prepared_bed_ids, active_reserved_bed_ids
from .intervals import occupancy_overlaps_period
from .personnel_categories import (
    MISSING_CATEGORY,
    MISSING_CATEGORY_MESSAGE,
    ROOM_CATEGORY_CONFLICT,
    ROOM_CATEGORY_CONFLICT_MESSAGE,
    employee_settlement_category,
    room_allows_personnel_category,
)
from .preview_corrections import build_effective_settlement_preview_plan
from .residents import (
    build_settlement_resident_lock_plan,
    lock_settlement_residents_after_access,
)
from .saved_previews import (
    _assert_stored_result_matches,
    _validate_approved_cohort,
    _validate_resolver_result,
)
from .resolver import (
    _occupancy_overlaps_member,
    _replaceable_occupancy_kind,
    resolve_settlement_cohort,
    validate_preview_correction_target,
)
from .services import (
    _validate_occupancy_conflicts,
    _validate_resident_and_destination,
    validate_placement_input_contract,
)


def _apply_error(code: str, message: str, *, params=None) -> ValidationError:
    return ValidationError(
        message,
        code=f'settlement.apply.{code}',
        params=params,
    )


def _overlaps_q(*, starts_at, ends_at):
    return (
        Q(starts_at__lt=ends_at)
        & (Q(ends_at__isnull=True) | Q(ends_at__gt=starts_at))
        & (Q(terminated_at__isnull=True) | Q(terminated_at__gt=starts_at))
        & Q(replaced_by_application__isnull=True)
        & Q(replaced_by_occupancy__isnull=True)
    )


def _member_snapshot(rows):
    return tuple(
        (
            row.pk,
            row.resident_id,
            row.arrival_at,
            row.departure_at,
            row.participation_status,
            row.source_revision_id,
            row.work_shift,
            row.shift_source_kind,
            row.official_equipment_assignment_id,
            row.shift_selected_by_access_id,
            row.shift_selected_at,
            row.shift_selection_basis,
            row.shift_source_fingerprint,
        )
        for row in rows
    )


def _placement_snapshot(rows):
    return tuple(
        (
            row.pk,
            row.resident_id,
            row.calendar_slot_id,
            row.physical_bed_id,
            row.action,
            row.source_kind,
            row.work_shift,
            row.cohort_member_id_snapshot,
            row.physical_room_id_snapshot,
            row.binding_id_snapshot,
            row.equipment_assignment_id_snapshot,
            row.anchor_id_snapshot,
            row.anchor_bed_assignment_id_snapshot,
            row.normalized_provenance,
        )
        for row in rows
    )


def _unresolved_snapshot(rows):
    return tuple(
        (
            row.pk,
            row.resident_id,
            row.reason_code,
            row.reason_codes,
            row.work_shift,
            row.cohort_member_id_snapshot,
            row.structured_details,
        )
        for row in rows
    )


def _occupancy_snapshot(rows):
    return tuple(
        (
            row.pk,
            row.resident_id,
            row.physical_bed_id,
            row.assignment_type,
            row.source_kind,
            row.work_shift,
            row.shift_source_kind,
            row.shift_source_fingerprint,
            row.shift_official_assignment_id,
            row.shift_selected_by_access_id,
            row.shift_selected_at,
            row.shift_selection_basis,
            row.starts_at,
            row.ends_at,
            row.terminated_at,
            row.ended_at,
            row.source_application_id,
            row.source_preview_placement_id,
            row.source_preview_correction_id,
            row.replaced_by_application_id,
            row.replaced_by_occupancy_id,
            row.watch_period_id,
            row.cohort_member_id,
        )
        for row in rows
    )


@dataclass(frozen=True, slots=True)
class _ApplyLockPlan:
    using: str
    run_id: int
    work_shift: str
    run_snapshot: tuple
    cohort_id: int
    cohort_snapshot: tuple
    watch_period_id: int
    period_snapshot: tuple
    resident_plan: object
    active_member_resident_ids: tuple[int, ...]
    member_snapshot: tuple
    placement_snapshot: tuple
    unresolved_snapshot: tuple
    correction_snapshot: tuple
    calendar_slot_snapshot: tuple
    bed_snapshot: tuple
    room_snapshot: tuple
    occupancy_snapshot: tuple
    application_snapshot: tuple


@dataclass(frozen=True, slots=True)
class _EffectiveApplyPlacement:
    resident_id: int
    work_shift: str
    calendar_slot_id: int
    physical_bed_id: int
    cohort_member_id_snapshot: int
    source_placement: SettlementPreviewPlacement | None
    source_unresolved: SettlementPreviewUnresolved | None
    effective_correction: SettlementPreviewCorrection | None


def _effective_rows(locked):
    placements_by_id = {row.pk: row for row in locked['placements']}
    unresolved_by_id = {row.pk: row for row in locked['unresolved']}
    corrections_by_id = {row.pk: row for row in locked['corrections']}
    placements = []
    excluded = []
    unresolved = []
    for decision in locked['effective_plan'].decisions:
        source_placement = placements_by_id.get(decision.source_placement_id)
        source_unresolved = unresolved_by_id.get(decision.source_unresolved_id)
        source = source_placement or source_unresolved
        if source is None:
            raise _apply_error('incomplete_preview', 'Effective plan потерял исходную строку.')
        correction = corrections_by_id.get(decision.effective_correction_id)
        row = {
            'decision': decision,
            'source_placement': source_placement,
            'source_unresolved': source_unresolved,
            'effective_correction': correction,
            'cohort_member_id_snapshot': source.cohort_member_id_snapshot,
        }
        if decision.state == 'placement':
            placements.append(_EffectiveApplyPlacement(
                resident_id=decision.resident_id,
                work_shift=decision.work_shift,
                calendar_slot_id=decision.calendar_slot_id,
                physical_bed_id=decision.physical_bed_id,
                cohort_member_id_snapshot=source.cohort_member_id_snapshot,
                source_placement=source_placement,
                source_unresolved=source_unresolved,
                effective_correction=correction,
            ))
        elif decision.state == 'excluded':
            excluded.append(row)
        elif decision.state == 'unresolved':
            unresolved.append(row)
        else:
            raise _apply_error('incomplete_preview', 'Effective plan содержит неизвестное состояние.')
    return placements, excluded, unresolved


def _source_decision_snapshot(placement):
    if placement.source_placement is not None:
        row = placement.source_placement
        return {
            'state': 'placement',
            'placement_id': row.pk,
            'resident_id': row.resident_id,
            'work_shift': row.work_shift,
            'calendar_slot_id': row.calendar_slot_id,
            'physical_bed_id': row.physical_bed_id,
        }
    row = placement.source_unresolved
    return {
        'state': 'unresolved',
        'unresolved_id': row.pk,
        'resident_id': row.resident_id,
        'work_shift': row.work_shift,
        'reason_code': row.reason_code,
    }


def _effective_decision_snapshot(placement):
    correction = placement.effective_correction
    return {
        'state': 'placement',
        'resident_id': placement.resident_id,
        'work_shift': placement.work_shift,
        'calendar_slot_id': placement.calendar_slot_id,
        'physical_bed_id': placement.physical_bed_id,
        'correction_id': correction.pk if correction else None,
        'correction_action': correction.action if correction else None,
        'correction_fingerprint': correction.fingerprint if correction else '',
    }


def _base_snapshot_for_item(source_placement, source_unresolved):
    if source_placement is not None:
        return {
            'state': 'placement',
            'placement_id': source_placement.pk,
            'resident_id': source_placement.resident_id,
            'work_shift': source_placement.work_shift,
            'calendar_slot_id': source_placement.calendar_slot_id,
            'physical_bed_id': source_placement.physical_bed_id,
        }
    if source_unresolved is not None:
        return {
            'state': 'unresolved',
            'unresolved_id': source_unresolved.pk,
            'resident_id': source_unresolved.resident_id,
            'work_shift': source_unresolved.work_shift,
            'reason_code': source_unresolved.reason_code,
        }
    return {}


def _effective_snapshot_for_item(
    *, effective_placement, effective_excluded, effective_unresolved=None,
):
    if effective_placement is not None:
        return _effective_decision_snapshot(effective_placement)
    row = effective_excluded or effective_unresolved
    if row is None:
        return {}
    decision = row['decision']
    correction = row['effective_correction']
    return {
        'state': decision.state,
        'resident_id': decision.resident_id,
        'work_shift': decision.work_shift,
        'correction_id': correction.pk if correction else None,
        'correction_action': correction.action if correction else None,
        'correction_fingerprint': correction.fingerprint if correction else '',
    }


def _correction_snapshot(rows):
    return tuple(
        (
            row.pk, row.preview_run_id, row.resident_id, row.work_shift,
            row.action, row.source_placement_id, row.source_unresolved_id,
            row.target_calendar_slot_id, row.target_physical_bed_id,
            row.actor_access_id, row.reason, row.created_at,
            row.supersedes_id, row.source_snapshot, row.fingerprint,
        )
        for row in rows
    )


def _build_apply_lock_plan(*, run_id: int, work_shift: str) -> _ApplyLockPlan:
    using = router.db_for_write(SettlementPreviewApplication)
    run_row = (
        SettlementPreviewRun._base_manager.using(using)
        .filter(pk=run_id)
        .values(
            'pk', 'cohort_id', 'watch_period_id', 'watch_composition_id',
            'status', 'resolver_fingerprint', 'result_fingerprint', 'revision',
            'requires_shift_split',
        )
        .first()
    )
    if run_row is None:
        raise SettlementPreviewRun.DoesNotExist

    cohort_row = (
        SettlementCohort._base_manager.using(using)
        .filter(pk=run_row['cohort_id'])
        .values(
            'pk', 'watch_period_id', 'watch_composition_id', 'status',
            'version', 'source_revision_id', 'input_fingerprint',
        )
        .first()
    )
    period_row = (
        WatchPeriod.objects.using(using)
        .filter(pk=run_row['watch_period_id'])
        .values('pk', 'watch_composition_id', 'starts_on', 'ends_on', 'is_active')
        .first()
    )
    if cohort_row is None or period_row is None:
        raise _apply_error('stale_preview', 'Календарное основание preview отсутствует.')

    members = list(
        SettlementCohortMember._base_manager.using(using)
        .filter(
            cohort_id=cohort_row['pk'],
            participation_status__in=SettlementCohortMember.ACTIVE_PARTICIPATION_STATUSES,
            work_shift=work_shift,
        )
        .order_by('resident_id', 'pk')
    )
    placements = list(
        SettlementPreviewPlacement._base_manager.using(using)
        .filter(run_id=run_id, work_shift=work_shift)
        .order_by('pk')
    )
    unresolved = list(
        SettlementPreviewUnresolved._base_manager.using(using)
        .filter(run_id=run_id, work_shift=work_shift)
        .order_by('pk')
    )
    corrections = list(
        SettlementPreviewCorrection._base_manager.using(using)
        .filter(preview_run_id=run_id, work_shift=work_shift)
        .order_by('resident_id', 'work_shift', 'created_at', 'pk')
    )
    run_for_effective = SettlementPreviewRun._base_manager.using(using).get(pk=run_id)
    effective_plan = build_effective_settlement_preview_plan(
        run=run_for_effective,
        placements=placements,
        unresolved=unresolved,
        corrections=corrections,
    )
    target_bed_ids = tuple(sorted({
        row.physical_bed_id for row in effective_plan.decisions if row.physical_bed_id
    }))
    # T-036 backend-14: пол комнаты считается по всем её жильцам, поэтому в
    # снапшот попадают занятия всех коек целевых комнат, а не только целевых.
    target_room_ids = tuple(sorted(set(
        PhysicalBed.objects.using(using)
        .filter(pk__in=target_bed_ids)
        .values_list('room_id', flat=True)
    )))
    calendar_slot_ids = tuple(sorted({
        row.calendar_slot_id for row in effective_plan.decisions if row.calendar_slot_id
    }))
    calendar_slots = tuple(
        AccommodationAnchorCalendarSlot._base_manager.using(using)
        .filter(pk__in=calendar_slot_ids).order_by('pk')
        .values_list('pk', 'anchor_id', 'watch_period_id', 'status', 'valid_from', 'valid_to')
    )
    active_resident_ids = tuple(sorted({row.resident_id for row in members}))
    if members:
        scope_start = min(row.arrival_at for row in members)
        scope_end = max(row.departure_at for row in members)
        interval_q = _overlaps_q(starts_at=scope_start, ends_at=scope_end)
    else:
        interval_q = Q(pk__in=())

    occupancies = list(
        EmployeeBedOccupancy._base_manager.using(using)
        .filter(
            interval_q
            & (
                Q(resident_id__in=active_resident_ids)
                | Q(physical_bed_id__in=target_bed_ids)
                | Q(physical_bed__room_id__in=target_room_ids)
                | Q(source_application__watch_period_id=period_row['pk'])
            )
            & Q(replaced_by_application__isnull=True)
            & Q(replaced_by_occupancy__isnull=True)
        )
        .select_related(
            'source_application__preview_run',
            'source_preview_placement',
            'source_preview_correction__source_placement',
            'source_preview_correction__source_unresolved',
            'cohort_member',
        )
        .order_by('pk')
    )
    planned_resident_ids = tuple(sorted({
        *active_resident_ids,
        *(row.resident_id for row in occupancies),
    }))
    if not planned_resident_ids:
        raise _apply_error(
            'incomplete_preview',
            'Apply lock plan не содержит ни одного resident.',
        )
    resident_plan = build_settlement_resident_lock_plan(
        resident_ids=planned_resident_ids,
        require_active=True,
        active_resident_ids=active_resident_ids,
        using=using,
    )

    bed_ids = tuple(sorted({
        *target_bed_ids,
        *(row.physical_bed_id for row in occupancies),
    }))
    beds = list(
        PhysicalBed.objects.using(using)
        .filter(pk__in=bed_ids)
        .order_by('pk')
        .values_list('pk', 'room_id', 'stable_id', 'block', 'position')
    )
    room_ids = tuple(sorted({row[1] for row in beds}))
    rooms = tuple(
        PhysicalRoom.objects.using(using)
        .filter(pk__in=room_ids)
        .order_by('pk')
        .values_list(
            'pk', 'transfer_status', 'room_type',
            'dormitory_id', 'floor', 'number',
        )
    )
    application_snapshot = tuple(
        SettlementPreviewApplication._base_manager.using(using)
        .filter(preview_run_id=run_id)
        .filter(Q(work_shift=work_shift) | Q(legacy_whole_run=True))
        .order_by('pk')
        .values_list('pk', 'work_shift', 'legacy_whole_run')
    )
    return _ApplyLockPlan(
        using=using,
        run_id=run_id,
        work_shift=work_shift,
        run_snapshot=tuple(run_row.values()),
        cohort_id=cohort_row['pk'],
        cohort_snapshot=tuple(cohort_row.values()),
        watch_period_id=period_row['pk'],
        period_snapshot=tuple(period_row.values()),
        resident_plan=resident_plan,
        active_member_resident_ids=active_resident_ids,
        member_snapshot=_member_snapshot(members),
        placement_snapshot=_placement_snapshot(placements),
        unresolved_snapshot=_unresolved_snapshot(unresolved),
        correction_snapshot=_correction_snapshot(corrections),
        calendar_slot_snapshot=calendar_slots,
        bed_snapshot=tuple(beds),
        room_snapshot=rooms,
        occupancy_snapshot=_occupancy_snapshot(occupancies),
        application_snapshot=application_snapshot,
    )


def _lock_and_revalidate_plan(
    *,
    plan: _ApplyLockPlan,
    control_context: SettlementControlWriteContext,
):
    using = plan.using
    lease = lock_settlement_write_lease(control_context=control_context, using=using)
    locked_access_rows = lock_settlement_write_access(
        lease=lease,
        control_context=control_context,
        employee_ids=plan.resident_plan.employee_ids,
        using=using,
    )
    actor_access = locked_access_rows.access_by_id(control_context.owner_access_id)
    actor_employee = locked_access_rows.employee_by_id(actor_access.employee_id)
    resident_rows = lock_settlement_residents_after_access(
        plan.resident_plan,
        locked_employees=locked_access_rows.employees,
    )

    period = (
        WatchPeriod.objects.using(using)
        .select_for_update(of=('self',))
        .get(pk=plan.watch_period_id)
    )
    cohort = (
        SettlementCohort._base_manager.using(using)
        .select_for_update(of=('self',))
        .select_related('watch_period', 'watch_composition')
        .get(pk=plan.cohort_id)
    )
    run = (
        SettlementPreviewRun._base_manager.using(using)
        .select_for_update(of=('self',))
        .get(pk=plan.run_id)
    )
    placements = list(
        SettlementPreviewPlacement._base_manager.using(using)
        .select_for_update(of=('self',))
        .select_related('calendar_slot__watch_period')
        .filter(run_id=run.pk, work_shift=plan.work_shift)
        .order_by('pk')
    )
    unresolved = list(
        SettlementPreviewUnresolved._base_manager.using(using)
        .select_for_update(of=('self',))
        .filter(run_id=run.pk, work_shift=plan.work_shift)
        .order_by('pk')
    )
    corrections = list(
        SettlementPreviewCorrection._base_manager.using(using)
        .select_for_update(of=('self',))
        .filter(preview_run_id=run.pk, work_shift=plan.work_shift)
        .order_by('resident_id', 'work_shift', 'created_at', 'pk')
    )
    calendar_slot_ids = tuple(row[0] for row in plan.calendar_slot_snapshot)
    calendar_slots = list(
        AccommodationAnchorCalendarSlot._base_manager.using(using)
        .select_for_update(of=('self',))
        .select_related('watch_period', 'anchor', 'anchor__created_revision', 'source_revision')
        .filter(pk__in=calendar_slot_ids).order_by('pk')
    )
    list(
        AccommodationAnchorBedAssignment._base_manager.using(using)
        .select_for_update(of=('self',))
        .filter(anchor_id__in=sorted({row.anchor_id for row in calendar_slots}))
        .order_by('pk')
    )
    bed_ids = tuple(row[0] for row in plan.bed_snapshot)
    beds = list(
        PhysicalBed.objects.using(using)
        .select_for_update(of=('self',))
        .select_related('room', 'room__dormitory')
        .filter(pk__in=bed_ids)
        .order_by('pk')
    )
    room_ids = tuple(row[0] for row in plan.room_snapshot)
    rooms = list(
        PhysicalRoom.objects.using(using)
        .select_for_update(of=('self',))
        .filter(pk__in=room_ids)
        .order_by('pk')
    )
    occupancy_ids = tuple(row[0] for row in plan.occupancy_snapshot)
    occupancies = list(
        EmployeeBedOccupancy._base_manager.using(using)
        .select_for_update(of=('self',))
        .select_related(
            'physical_bed',
            'source_application__preview_run',
            'source_preview_placement',
            'source_preview_correction__source_placement',
            'source_preview_correction__source_unresolved',
            'cohort_member',
        )
        .filter(pk__in=occupancy_ids)
        .order_by('pk')
    )
    application_ids = tuple(row[0] for row in plan.application_snapshot)
    applications = list(
        SettlementPreviewApplication._base_manager.using(using)
        .select_for_update(of=('self',))
        .filter(pk__in=application_ids)
        .order_by('pk')
    )
    actual_application_snapshot = tuple(
        (row.pk, row.work_shift, row.legacy_whole_run)
        for row in applications
    )
    application = next(
        (
            row for row in applications
            if not row.legacy_whole_run and row.work_shift == plan.work_shift
        ),
        None,
    )
    legacy_application = next(
        (row for row in applications if row.legacy_whole_run),
        None,
    )

    actual_run_snapshot = (
        run.pk, run.cohort_id, run.watch_period_id, run.watch_composition_id,
        run.status, run.resolver_fingerprint, run.result_fingerprint, run.revision,
        run.requires_shift_split,
    )
    actual_cohort_snapshot = (
        cohort.pk, cohort.watch_period_id, cohort.watch_composition_id,
        cohort.status, cohort.version, cohort.source_revision_id,
        cohort.input_fingerprint,
    )
    actual_period_snapshot = (
        period.pk, period.watch_composition_id, period.starts_on,
        period.ends_on, period.is_active,
    )
    members = list(
        SettlementCohortMember._base_manager.using(using)
        .filter(
            cohort_id=cohort.pk,
            participation_status__in=SettlementCohortMember.ACTIVE_PARTICIPATION_STATUSES,
            work_shift=plan.work_shift,
        )
        .order_by('resident_id', 'pk')
    )
    actual_bed_snapshot = tuple(
        (row.pk, row.room_id, row.stable_id, row.block, row.position)
        for row in beds
    )
    actual_room_snapshot = tuple(
        (
            row.pk, row.transfer_status, row.room_type,
            row.dormitory_id, row.floor, row.number,
        )
        for row in rooms
    )
    actual_calendar_slot_snapshot = tuple(
        (row.pk, row.anchor_id, row.watch_period_id, row.status, row.valid_from, row.valid_to)
        for row in calendar_slots
    )
    if (
        actual_run_snapshot != plan.run_snapshot
        or actual_cohort_snapshot != plan.cohort_snapshot
        or actual_period_snapshot != plan.period_snapshot
        or _member_snapshot(members) != plan.member_snapshot
        or _placement_snapshot(placements) != plan.placement_snapshot
        or _unresolved_snapshot(unresolved) != plan.unresolved_snapshot
        or _correction_snapshot(corrections) != plan.correction_snapshot
        or actual_calendar_slot_snapshot != plan.calendar_slot_snapshot
        or actual_bed_snapshot != plan.bed_snapshot
        or actual_room_snapshot != plan.room_snapshot
        or _occupancy_snapshot(occupancies) != plan.occupancy_snapshot
        or actual_application_snapshot != plan.application_snapshot
    ):
        raise _apply_error('stale_preview', 'Apply lock plan устарел до записи.')

    return {
        'actor_access': actor_access,
        'actor_employee': actor_employee,
        'resident_rows': resident_rows,
        'period': period,
        'cohort': cohort,
        'run': run,
        'work_shift': plan.work_shift,
        'members': members,
        'placements': placements,
        'unresolved': unresolved,
        'corrections': corrections,
        'calendar_slots': calendar_slots,
        'effective_plan': build_effective_settlement_preview_plan(
            run=run,
            placements=placements,
            unresolved=unresolved,
            corrections=corrections,
        ),
        'beds': beds,
        'rooms': rooms,
        'occupancies': occupancies,
        'application': application,
        'legacy_application': legacy_application,
    }


def _validate_locked_preview(locked, *, using=None):
    run = locked['run']
    cohort = locked['cohort']
    period = locked['period']
    if run.status != SettlementPreviewRun.Status.CONFIRMED:
        raise _apply_error('invalid_state', 'Apply разрешён только для CONFIRMED preview.')
    current_ids = tuple(
        SettlementPreviewRun._base_manager.filter(
            watch_period_id=period.pk,
            status=SettlementPreviewRun.Status.CONFIRMED,
        )
        .order_by('pk')
        .values_list('pk', flat=True)
    )
    if current_ids != (run.pk,):
        raise _apply_error('invalid_state', 'Preview больше не является текущим CONFIRMED run.')
    if (
        run.cohort_id != cohort.pk
        or run.watch_period_id != period.pk
        or run.watch_composition_id != cohort.watch_composition_id
        or period.watch_composition_id != cohort.watch_composition_id
    ):
        raise _apply_error('stale_preview', 'Preview имеет stale календарное основание.')
    try:
        _validate_approved_cohort(cohort, stale=True)
        result = resolve_settlement_cohort(
            cohort_id=cohort.pk,
            materialized_preview_run_id=run.pk,
        )
        validated = _validate_resolver_result(cohort=cohort, result=result)
    except ValidationError as error:
        raise _apply_error(
            'stale_preview',
            'Текущие источники не воспроизводят подтверждённый preview.',
        ) from error
    source_changed = False
    try:
        _assert_stored_result_matches(run=run, result=result, validated=validated)
    except ValidationError:
        # Строки плана могли измениться точечно (подготовка/резерв койки,
        # пол, посторонняя занятость). Это больше не отменяет всю смену:
        # строки разделяются на применимые и пропущенные в _classify_row_conflicts.
        source_changed = True
    return result, source_changed


def _allowed_apply_date(*, period, work_shift):
    if work_shift == SettlementCohortMember.WorkShift.DAY:
        return period.starts_on
    return period.starts_on - timedelta(days=1)


def _validate_apply_date(*, period, work_shift, now):
    effective_now = timezone.now() if now is None else now
    try:
        local_date = timezone.localdate(effective_now)
    except (AttributeError, TypeError, ValueError) as error:
        raise _apply_error('invalid_state', 'Серверное время Apply указано некорректно.') from error
    allowed_date = _allowed_apply_date(period=period, work_shift=work_shift)
    if local_date < allowed_date:
        raise _apply_error(
            'too_early',
            f'Применение этой смены разрешено не раньше {allowed_date:%d.%m.%Y}.',
            params={'allowed_date': allowed_date.isoformat()},
        )


def _validate_shift_scope(locked):
    period = locked['period']
    work_shift = locked['work_shift']
    members_by_id = {row.pk: row for row in locked['members']}
    for member in locked['members']:
        if member.work_shift != work_shift:
            raise _apply_error('incomplete_preview', 'Membership относится к другой смене.')
    for row in (*locked['placements'], *locked['unresolved']):
        member = members_by_id.get(row.cohort_member_id_snapshot)
        if (
            row.work_shift != work_shift
            or member is None
            or member.resident_id != row.resident_id
        ):
            raise _apply_error('incomplete_preview', 'Строка результата содержит stale смену.')
    effective_placements, excluded, effective_unresolved = _effective_rows(locked)
    slots_by_id = {row.pk: row for row in locked['calendar_slots']}
    for placement in effective_placements:
        member = members_by_id.get(placement.cohort_member_id_snapshot)
        slot = slots_by_id.get(placement.calendar_slot_id)
        if member is None or member.resident_id != placement.resident_id or slot is None:
            raise _apply_error('incomplete_preview', 'Effective placement содержит stale ссылки.')
        arrival_date = timezone.localdate(member.arrival_at)
        early_night = (
            work_shift == SettlementCohortMember.WorkShift.NIGHT
            and arrival_date == period.starts_on - timedelta(days=1)
        )
        if (
            slot.watch_period_id != period.pk
            or slot.status != AccommodationAnchorCalendarSlot.Status.CONFIRMED
            or slot.calendar_relation_is_stale
        ):
            raise _apply_error('stale_preview', 'CalendarSlot смены устарел.')
        if arrival_date < slot.valid_from and not early_night:
            raise _apply_error(
                'stale_preview',
                'Начало размещения находится раньше допустимой границы CalendarSlot.',
            )
        if placement.effective_correction is not None:
            resident = locked['resident_rows'].resident_by_id(placement.resident_id)
            try:
                validate_preview_correction_target(
                    cohort=locked['cohort'],
                    resident=resident,
                    calendar_slot_id=placement.calendar_slot_id,
                    physical_bed_id=placement.physical_bed_id,
                )
            except ValidationError as error:
                raise _apply_error(
                    'stale_preview',
                    'Целевое место точечного исправления устарело.',
                ) from error
    locked['effective_placements'] = effective_placements
    locked['effective_excluded'] = excluded
    locked['effective_unresolved'] = effective_unresolved


def _category_conflict(resident, *, room, roommate_categories):
    """Причина отказа по категории «рабочий/ИТР» или ``None``.

    Решение владельца 22.11.2026: рабочие и ИТР не живут в одной комнате даже в
    разных блоках; ИТР-комната принимает только ИТР; пустая категория кадровой
    должности не назначается автоматически.
    """

    if resident.is_external:
        return None
    category = employee_settlement_category(resident.employee)
    if category is None:
        return (
            MISSING_CATEGORY,
            ('SET-R102',),
            MISSING_CATEGORY_MESSAGE,
        )
    if not room_allows_personnel_category(
        room.room_type, category, roommate_categories,
    ):
        return (
            ROOM_CATEGORY_CONFLICT,
            ('SET-R102',),
            ROOM_CATEGORY_CONFLICT_MESSAGE,
        )
    return None


def _sex_conflict(resident, *, occupant_sexes, planned_sexes):
    """Причина отказа по полу или ``None`` (та же логика, что в resolver).

    T-036 backend-14: пол комнаты вычисляется по эффективным жильцам и по уже
    поставленным в этом плане жильцам той же комнаты; атрибут комнаты не
    используется.
    """

    from .room_sex import (
        REASON_OCCUPANT_SEX_UNKNOWN,
        REASON_RESIDENT_SEX_UNKNOWN,
        REASON_ROOM_SEX_MISMATCH,
        REASON_ROOM_SEX_MIXED,
        classify_sexes,
        rejection_reason,
        resident_sex,
    )

    sex = resident_sex(resident)
    state, room_sex_value = classify_sexes(
        {*occupant_sexes, *planned_sexes}
    )
    reason = rejection_reason(
        sex=sex,
        room_state=state,
        room_sex=room_sex_value,
    )
    if reason is None:
        return None
    if reason == REASON_RESIDENT_SEX_UNKNOWN:
        return (
            'resident_sex_unknown',
            ('SET-R081', 'SET-R042'),
            'Пол жильца не указан.',
        )
    if reason == REASON_OCCUPANT_SEX_UNKNOWN:
        return (
            'room_occupant_sex_unknown',
            ('SET-R081', 'SET-R043'),
            'В комнате живёт жилец с неуказанным полом.',
        )
    if reason == REASON_ROOM_SEX_MIXED:
        return (
            'room_sex_mixed',
            ('SET-R042', 'SET-R043'),
            'В комнате на этот период живут и мужчины, и женщины.',
        )
    return (
        'room_sex_mismatch',
        ('SET-R042', 'SET-R043'),
        'Пол жильца не совпадает с полом комнаты.',
    )


def _permanent_chain_problem(*, resident, slot, bed, period):
    """Проверить цепочку employee → permanent anchor → bed (domain-3 §2).

    Возвращает ``(kind, message)``: ``None`` — цепочка подтверждена;
    ``('invalid', …)`` — карта противоречива (привязка есть, но не та);
    ``('missing', …)`` — подтверждённой привязки нет вовсе (место может быть
    временным, но не постоянным). Технический якорь не является исключением:
    посуточное назначение техники постоянным правом не считается (Q-D3-3).
    """

    def invalid(message):
        return 'invalid', message

    if not resident.employee_id:
        return invalid('Жилец не связан с сотрудником.')
    employee = resident.employee
    if not employee.is_active or employee.status != employee.Status.ACTIVE:
        return invalid('Сотрудник неактивен.')
    if slot.status != AccommodationAnchorCalendarSlot.Status.CONFIRMED:
        return invalid('Календарный слот не подтверждён.')
    if slot.calendar_relation_is_stale:
        return invalid('Календарный слот устарел.')
    if slot.source_revision.status != 'confirmed':
        return invalid('Ревизия слота не подтверждена.')
    anchor = slot.anchor
    if anchor.status != anchor.Status.ACTIVE:
        return invalid('Якорь не действует.')
    if anchor.created_revision.status != 'confirmed':
        return invalid('Ревизия создания якоря не подтверждена.')

    bindings = list(
        EmployeeAccommodationBinding.objects.filter(
            resident=resident,
            anchor_calendar_slot=slot,
        ).order_by('pk'),
    )
    valid_bindings = [
        binding for binding in bindings
        if (
            binding.status == EmployeeAccommodationBinding.Status.CONFIRMED
            and binding.approved_by_id is not None
            and binding.approved_at is not None
            and binding.valid_from <= period.starts_on
            and binding.valid_to >= period.ends_on
            and binding.source_revision.status == 'confirmed'
        )
    ]
    if not valid_bindings:
        if bindings:
            return invalid(
                'Привязка жильца к слоту не подтверждена, просрочена или '
                'имеет неподтверждённую ревизию.'
            )
        other_binding = EmployeeAccommodationBinding.objects.filter(
            anchor_calendar_slot=slot,
            status__in=(
                EmployeeAccommodationBinding.Status.CONFIRMED,
                EmployeeAccommodationBinding.Status.CLOSED,
            ),
        ).exclude(resident=resident).exists()
        if other_binding:
            return invalid('Слот привязан к другому жильцу.')
        return 'missing', 'Нет подтверждённой привязки жильца к слоту.'
    period_start = timezone.make_aware(
        datetime.combine(period.starts_on, time.min),
        timezone.get_current_timezone(),
    )
    assignment_exists = (
        AccommodationAnchorBedAssignment.objects.filter(
            anchor=anchor,
            physical_bed=bed,
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
            started_revision__status='confirmed',
            valid_from__lte=period_start,
        )
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=period_start))
        .exists()
    )
    if not assignment_exists:
        return invalid('Нет подтверждённого закрепления якоря за целевой койкой.')
    return None


def _placement_place_kind(*, placement, slot):
    anchor = slot.anchor
    return (
        'pool'
        if anchor.personnel_position_id is None and anchor.equipment_id is None
        else 'positional'
    )


def _skip_row(
    *,
    resident,
    bed,
    reason_codes,
    rule_ids,
    message,
    apply_code,
    line=None,
    source_placement_id=None,
    in_plan=False,
):
    return {
        'resident_id': resident.pk,
        'resident_name': resident.display_name,
        'physical_bed_id': bed.pk if bed is not None else None,
        'bed_stable_id': bed.stable_id if bed is not None else '',
        'reason_codes': list(reason_codes),
        'rule_ids': list(rule_ids),
        'message': message,
        'apply_code': apply_code,
        'line': line,
        'source_placement_id': source_placement_id,
        'in_plan': bool(in_plan),
    }


def _classify_row_conflicts(*, locked, changes, result, source_changed, using):
    """Дополнить конфликты строками, которые нельзя применить сейчас."""

    members_by_resident = changes['members_by_resident']
    beds_by_id = {row.pk: row for row in locked['beds']}
    residents_by_id = {
        row.pk: row for row in locked['resident_rows'].residents
    }
    target_bed_ids = {
        row.physical_bed_id for row in changes['create_placements']
    }
    prepared = active_prepared_bed_ids(
        bed_ids=target_bed_ids,
        using=using,
        for_update=True,
    ) if target_bed_ids else set()
    reserved = active_reserved_bed_ids(
        bed_ids=target_bed_ids,
        using=using,
        for_update=True,
    ) if target_bed_ids else set()
    effective_rows = [
        row for row in locked['occupancies']
        if row not in changes['replace_rows']
    ]
    # T-036 backend-14: пол комнаты — по фактическим жильцам на период и по
    # жильцам, которых этот же план ставит в ту же комнату.
    from .room_sex import occupancy_sex, resident_sex

    occupancies_by_room: dict[int, list] = {}
    for row in effective_rows:
        occupancies_by_room.setdefault(
            row.physical_bed.room_id, [],
        ).append(row)
    planned_sexes_by_room: dict[int, list[tuple[int, str]]] = {}
    for row in changes['create_placements']:
        bed = beds_by_id.get(row.physical_bed_id)
        resident = residents_by_id.get(row.resident_id)
        if bed is None or resident is None:
            continue
        planned_sexes_by_room.setdefault(bed.room_id, []).append(
            (row.resident_id, resident_sex(resident)),
        )
    planned_categories_by_room: dict[int, list[tuple[int, str | None]]] = {}
    for row in changes['create_placements']:
        bed = beds_by_id.get(row.physical_bed_id)
        resident = residents_by_id.get(row.resident_id)
        if bed is None or resident is None or resident.is_external:
            continue
        planned_categories_by_room.setdefault(bed.room_id, []).append(
            (row.resident_id, employee_settlement_category(resident.employee)),
        )
    slots_by_id = {row.pk: row for row in locked['calendar_slots']}
    place_kinds: dict[int, str] = {}
    placement_types: dict[int, str] = {}
    current_placements = {}
    current_unresolved = {}
    if source_changed and result is not None:
        current_placements = {
            row.resident_id: row for row in result.placements
        }
        current_unresolved = {
            row.resident_id: row for row in result.unresolved
        }

    def add(resident_id, *, reason_codes, rule_ids, message, apply_code):
        if resident_id in conflicts:
            return
        resident = residents_by_id[resident_id]
        placement = next(
            (
                row for row in locked['effective_placements']
                if row.resident_id == resident_id
            ),
            None,
        )
        bed = (
            beds_by_id.get(placement.physical_bed_id)
            if placement is not None else None
        )
        conflicts[resident_id] = {
            'reason_codes': list(reason_codes),
            'rule_ids': list(rule_ids),
            'message': message,
            'apply_code': apply_code,
            'resident': resident,
            'bed': bed,
            'in_plan': placement is not None,
            'source_placement_id': (
                placement.source_placement.pk
                if placement is not None and placement.source_placement is not None
                else None
            ),
        }

    conflicts = dict(changes['conflicts'])
    for placement in list(changes['create_placements']):
        resident_id = placement.resident_id
        if resident_id in conflicts:
            continue
        member = members_by_resident[resident_id]
        resident = residents_by_id[resident_id]
        bed = beds_by_id[placement.physical_bed_id]
        room = bed.room
        slot = slots_by_id[placement.calendar_slot_id]
        place_kind = _placement_place_kind(placement=placement, slot=slot)
        place_kinds[resident_id] = place_kind
        if (
            resident.status != resident.Status.ACTIVE
            or (
                resident.employee_id
                and (
                    not resident.employee.is_active
                    or resident.employee.status != resident.employee.Status.ACTIVE
                )
            )
        ):
            add(
                resident_id,
                reason_codes=['resident_inactive'],
                rule_ids=['SET-R003'],
                message='Карточка жильца или сотрудник неактивны.',
                apply_code='hard_conflict',
            )
            continue
        if (
            slot.calendar_relation_is_stale
            or slot.source_revision.status != 'confirmed'
        ):
            add(
                resident_id,
                reason_codes=['stale_calendar_relation'],
                rule_ids=['SET-R010'],
                message='Календарное основание места устарело.',
                apply_code='stale_preview',
            )
            continue
        if place_kind == 'pool':
            placement_types[resident_id] = 'temporary'
        else:
            chain = _permanent_chain_problem(
                resident=resident,
                slot=slot,
                bed=bed,
                period=locked['period'],
            )
            if chain is None:
                placement_types[resident_id] = 'permanent'
            else:
                kind, message = chain
                if kind == 'invalid':
                    add(
                        resident_id,
                        reason_codes=['permanent_chain_missing'],
                        rule_ids=['SET-R013', 'SET-R024', 'SET-R065'],
                        message=(
                            'Место с постоянным правом имеет противоречивую '
                            f'привязку; исправьте карту. {message}'
                        ),
                        apply_code='permanent_chain_missing',
                    )
                    continue
                # Подтверждённой привязки нет вовсе: это допустимое временное
                # фактическое размещение (SET-R065), не отказ.
                placement_types[resident_id] = 'temporary'
        if placement_types[resident_id] == 'temporary' and (
            member.arrival_at is None
            or member.departure_at is None
            or member.departure_at <= member.arrival_at
        ):
            add(
                resident_id,
                reason_codes=['temporary_requires_end'],
                rule_ids=['SET-R066'],
                message=(
                    'Для временного размещения нужны начало и окончание '
                    'в пределах вахты.'
                ),
                apply_code='temporary_requires_end',
            )
            continue
        if bed.pk in prepared:
            # Единственный барьер: глобальный guard удалён, койку проверяет
            # только эта построчная классификация (review-5 note 4). Не удалять.
            add(
                resident_id,
                reason_codes=['bed_prepared_hold'],
                rule_ids=['SET-R095'],
                message='Койка придержана подготовкой за другим жильцом.',
                apply_code='bed_unavailable_for_plan',
            )
            continue
        if bed.pk in reserved:
            add(
                resident_id,
                reason_codes=['bed_reserve_hold'],
                rule_ids=['SET-R096'],
                message='Койка находится в резерве для срочного размещения.',
                apply_code='bed_unavailable_for_plan',
            )
            continue
        if room.transfer_status != PhysicalRoom.TransferStatus.TRANSFERRED:
            add(
                resident_id,
                reason_codes=['hard_rule_conflict'],
                rule_ids=['SET-R032'],
                message='Комната не передана для расселения.',
                apply_code='hard_conflict',
            )
            continue
        occupant_sexes = {
            occupancy_sex(row)
            for row in occupancies_by_room.get(room.pk, ())
            if row.resident_id != resident.pk
            and occupancy_overlaps_period(
                occupancy=row,
                starts_at=member.arrival_at,
                ends_at=member.departure_at,
            )
        }
        planned_sexes = {
            value
            for other_resident_id, value in planned_sexes_by_room.get(room.pk, ())
            if other_resident_id != resident.pk
        }
        sex_conflict = _sex_conflict(
            resident,
            occupant_sexes=occupant_sexes,
            planned_sexes=planned_sexes,
        )
        if sex_conflict is not None:
            reason, rule_ids, message = sex_conflict
            add(
                resident_id,
                reason_codes=[reason],
                rule_ids=rule_ids,
                message=message,
                apply_code='hard_conflict',
            )
            continue
        # Решение владельца 22.11.2026: категории «рабочий/ИТР» не смешиваются.
        occupant_categories = {
            employee_settlement_category(row.resident.employee)
            for row in occupancies_by_room.get(room.pk, ())
            if row.resident_id != resident.pk
            and not row.resident.is_external
            and occupancy_overlaps_period(
                occupancy=row,
                starts_at=member.arrival_at,
                ends_at=member.departure_at,
            )
        }
        planned_categories = {
            value
            for other_resident_id, value in planned_categories_by_room.get(
                room.pk, (),
            )
            if other_resident_id != resident.pk
        }
        category_conflict = _category_conflict(
            resident,
            room=room,
            roommate_categories=(
                *occupant_categories,
                *planned_categories,
            ),
        )
        if category_conflict is not None:
            reason, rule_ids, message = category_conflict
            add(
                resident_id,
                reason_codes=[reason],
                rule_ids=rule_ids,
                message=message,
                apply_code='hard_conflict',
            )
            continue
        try:
            _validate_occupancy_conflicts(
                resident_id=resident.pk,
                bed=bed,
                starts_at=member.arrival_at,
                ends_at=member.departure_at,
                persisted_occupancies=effective_rows,
            )
        except ValidationError as error:
            codes = [
                item.code for item in error.error_list
            ] if hasattr(error, 'error_list') else [error.code]
            rule_ids = []
            if 'settlement.bed.interval_overlap' in codes:
                rule_ids.append('SET-R033')
            if 'settlement.employee.interval_overlap' in codes:
                rule_ids.append('SET-R034')
            if not rule_ids:
                rule_ids = ['SET-R033', 'SET-R034']
            add(
                resident_id,
                reason_codes=['hard_rule_conflict'],
                rule_ids=rule_ids,
                message='; '.join(str(item) for item in error.messages),
                apply_code='hard_conflict',
            )
            continue
        if (
            source_changed
            and placement.effective_correction is None
        ):
            current = current_placements.get(resident_id)
            if (
                current is None
                or current.physical_bed_id != placement.physical_bed_id
                or current.calendar_slot_id != placement.calendar_slot_id
            ):
                unresolved = current_unresolved.get(resident_id)
                if unresolved is not None:
                    add(
                        resident_id,
                        reason_codes=unresolved.reason_codes,
                        rule_ids=unresolved.rule_ids,
                        message='Строка плана больше не воспроизводится из источников.',
                        apply_code='stale_preview',
                    )
                else:
                    add(
                        resident_id,
                        reason_codes=['stale_calendar_relation'],
                        rule_ids=['SET-R010'],
                        message='Строка плана устарела относительно источников.',
                        apply_code='stale_preview',
                    )

    blocked = set(conflicts)
    changes['create_placements'] = [
        row for row in changes['create_placements']
        if row.resident_id not in blocked
    ]
    changes['replace_rows'] = [
        row for row in changes['replace_rows']
        if row.resident_id not in blocked
    ]
    changes['replaced_with_new'] = sum(
        1 for row in changes['replace_rows']
        if row.resident_id in {
            item.resident_id for item in locked['effective_placements']
        }
    )
    changes['conflicts'] = conflicts
    changes['place_kinds'] = place_kinds
    changes['placement_types'] = placement_types
    return changes


def _classify_changes(*, locked, confirm_replace_manual):
    members_by_id = {row.pk: row for row in locked['members']}
    members_by_resident = {row.resident_id: row for row in locked['members']}
    placements_by_resident = {
        row.resident_id: row for row in locked['effective_placements']
    }
    conflicts: dict[int, dict] = {}
    if len(placements_by_resident) != len(locked['effective_placements']):
        raise _apply_error('incomplete_preview', 'Effective plan содержит duplicate resident.')
    if (
        len({row.physical_bed_id for row in locked['effective_placements']})
        != len(locked['effective_placements'])
    ):
        raise _apply_error('incomplete_preview', 'Effective plan содержит duplicate PhysicalBed.')
    all_result_residents = {
        *(row.resident_id for row in locked['placements']),
        *(row.resident_id for row in locked['unresolved']),
    }
    if all_result_residents != set(members_by_resident):
        raise _apply_error('incomplete_preview', 'Каждый member должен иметь ровно одну строку.')
    for placement in locked['effective_placements']:
        member = members_by_id.get(placement.cohort_member_id_snapshot)
        if member is None or member.resident_id != placement.resident_id:
            raise _apply_error('incomplete_preview', 'Effective placement содержит stale membership.')

    target_scope = []
    for occupancy in locked['occupancies']:
        member = members_by_resident.get(occupancy.resident_id)
        if _replaceable_occupancy_kind(
            occupancy=occupancy,
            cohort=locked['cohort'],
            member=member,
        ) is not None:
            target_scope.append(occupancy)

    reusable_by_resident = {}
    for occupancy in target_scope:
        placement = placements_by_resident.get(occupancy.resident_id)
        member = members_by_resident.get(occupancy.resident_id)
        if (
            occupancy.source_kind == EmployeeBedOccupancy.SourceKind.AUTO
            and placement is not None
            and member is not None
            and occupancy.physical_bed_id == placement.physical_bed_id
            and occupancy.starts_at == member.arrival_at
            and occupancy.ends_at == member.departure_at
            and occupancy.terminated_at is None
        ):
            reusable_by_resident[occupancy.resident_id] = occupancy

    manual_rows = [
        row for row in target_scope
        if row.source_kind == EmployeeBedOccupancy.SourceKind.MANUAL
    ]
    if manual_rows and not confirm_replace_manual:
        ids = tuple(sorted(row.pk for row in manual_rows))
        raise _apply_error(
            'manual_replacement_confirmation_required',
            'Повторный Apply затронет ручные корректировки.',
            params={'manual_occupancy_count': len(ids), 'manual_occupancy_ids': ids},
        )

    replace_rows = [
        row for row in target_scope
        if reusable_by_resident.get(row.resident_id) is not row
    ]
    replace_ids = {row.pk for row in replace_rows}
    for occupancy in locked['occupancies']:
        if occupancy.pk in replace_ids or occupancy in reusable_by_resident.values():
            continue
        member = members_by_resident.get(occupancy.resident_id)
        if member is not None:
            overlaps_member = occupancy_overlaps_period(
                occupancy=occupancy,
                starts_at=member.arrival_at,
                ends_at=member.departure_at,
            )
            if overlaps_member:
                conflicts[occupancy.resident_id] = {
                    'reason_codes': ['hard_rule_conflict'],
                    'rule_ids': ['SET-R033', 'SET-R034'],
                    'message': (
                        'У жильца есть посторонняя занятость на период вахты.'
                    ),
                    'apply_code': 'hard_conflict',
                }
        for placement in locked['effective_placements']:
            member = members_by_resident[placement.resident_id]
            overlaps = occupancy_overlaps_period(
                occupancy=occupancy,
                starts_at=member.arrival_at,
                ends_at=member.departure_at,
            )
            if not overlaps:
                continue
            if (
                occupancy.resident_id == placement.resident_id
                or occupancy.physical_bed_id == placement.physical_bed_id
            ):
                conflicts[placement.resident_id] = {
                    'reason_codes': ['hard_rule_conflict'],
                    'rule_ids': ['SET-R033', 'SET-R034'],
                    'message': (
                        'Целевой жилец или койка заняты посторонней occupancy.'
                    ),
                    'apply_code': 'hard_conflict',
                }

    blocked_residents = set(conflicts)
    replace_rows = [
        row for row in replace_rows if row.resident_id not in blocked_residents
    ]
    create_placements = [
        row for row in locked['effective_placements']
        if row.resident_id not in reusable_by_resident
        and row.resident_id not in blocked_residents
    ]
    replaced_with_new = sum(
        1 for row in replace_rows if row.resident_id in placements_by_resident
    )
    return {
        'members_by_resident': members_by_resident,
        'reusable_by_resident': reusable_by_resident,
        'replace_rows': replace_rows,
        'manual_rows': manual_rows,
        'create_placements': create_placements,
        'excluded': locked['effective_excluded'],
        'replaced_with_new': replaced_with_new,
        'conflicts': conflicts,
    }


def _conflict_payloads(*, conflicts, locked):
    beds_by_id = {row.pk: row for row in locked['beds']}
    residents_by_id = {
        row.pk: row for row in locked['resident_rows'].residents
    }
    placements_by_resident = {
        row.resident_id: row for row in locked['effective_placements']
    }
    payloads = []
    for resident_id in sorted(conflicts):
        data = conflicts[resident_id]
        resident = data.get('resident') or residents_by_id[resident_id]
        # Признак плановой строки — authoritative: он всегда выводится из
        # effective placements, а не из dict конфликта. Любое явное значение
        # `in_plan`/`source_placement_id` в конфликте игнорируется, иначе
        # устаревший или ошибочный False вернул бы дефект QA-7.
        placement = placements_by_resident.get(resident_id)
        bed = data.get('bed')
        if bed is None and placement is not None:
            bed = beds_by_id.get(placement.physical_bed_id)
        in_plan = placement is not None
        source_placement_id = (
            placement.source_placement.pk
            if placement is not None and placement.source_placement is not None
            else None
        )
        payloads.append(_skip_row(
            resident=resident,
            bed=bed,
            reason_codes=data['reason_codes'],
            rule_ids=data['rule_ids'],
            message=data['message'],
            apply_code=data['apply_code'],
            source_placement_id=source_placement_id,
            in_plan=in_plan,
        ))
    return payloads


def apply_confirmed_settlement_preview(
    *,
    run_id: int,
    work_shift: str,
    control_context: SettlementControlWriteContext,
    confirm_replace_manual: bool = False,
    now=None,
) -> SettlementPreviewApplication:
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        raise _apply_error('invalid_state', 'Требуется корректный PK preview run.')
    if not isinstance(confirm_replace_manual, bool):
        raise _apply_error('invalid_state', 'Флаг подтверждения должен быть boolean.')
    if work_shift not in {
        SettlementCohortMember.WorkShift.DAY,
        SettlementCohortMember.WorkShift.NIGHT,
    }:
        raise _apply_error('invalid_state', 'Требуется дневная или ночная смена.')
    plan = _build_apply_lock_plan(run_id=run_id, work_shift=work_shift)

    with transaction.atomic(using=plan.using):
        locked = _lock_and_revalidate_plan(
            plan=plan,
            control_context=control_context,
        )
        if locked['application'] is not None:
            return locked['application']
        if locked['legacy_application'] is not None:
            return locked['legacy_application']
        _validate_apply_date(
            period=locked['period'],
            work_shift=work_shift,
            now=now,
        )
        result, source_changed = _validate_locked_preview(
            locked,
            using=plan.using,
        )
        if not locked['run'].requires_shift_split:
            raise _apply_error(
                'shift_split_required',
                'План необходимо применить отдельно для ночной и дневной смены.',
            )
        _validate_shift_scope(locked)

        changes = _classify_changes(
            locked=locked,
            confirm_replace_manual=confirm_replace_manual,
        )
        changes = _classify_row_conflicts(
            locked=locked,
            changes=changes,
            result=result,
            source_changed=source_changed,
            using=plan.using,
        )
        skipped_rows = _conflict_payloads(
            conflicts=changes['conflicts'],
            locked=locked,
        )
        if (
            skipped_rows
            and not changes['create_placements']
            and not changes['reusable_by_resident']
            and not changes['replace_rows']
        ):
            first = skipped_rows[0]
            raise _apply_error(
                first['apply_code'],
                first['message'],
                params={'skipped_rows': skipped_rows},
            )
        run = locked['run']
        actor_access = locked['actor_access']
        actor_employee = locked['actor_employee']
        replace_rows = changes['replace_rows']
        manual_ids = tuple(sorted(row.pk for row in changes['manual_rows']))
        auto_ids = tuple(sorted(
            row.pk for row in replace_rows
            if row.source_kind == EmployeeBedOccupancy.SourceKind.AUTO
        ))
        reused_ids = tuple(sorted(
            row.pk for row in changes['reusable_by_resident'].values()
        ))
        snapshot = {
            'preview_run_id': run.pk,
            'work_shift': work_shift,
            'base_result_fingerprint': run.result_fingerprint,
            'effective_result_fingerprint': locked['effective_plan'].fingerprint,
            'placement_ids': [row.pk for row in locked['placements']],
            'unresolved_ids': [row.pk for row in locked['unresolved']],
            'correction_ids': list(locked['effective_plan'].correction_ids),
            'effective_decisions': [
                {
                    'resident_id': row.resident_id,
                    'work_shift': row.work_shift,
                    'state': row.state,
                    'source_placement_id': row.source_placement_id,
                    'source_unresolved_id': row.source_unresolved_id,
                    'calendar_slot_id': row.calendar_slot_id,
                    'physical_bed_id': row.physical_bed_id,
                    'effective_correction_id': row.effective_correction_id,
                    'effective_correction_action': row.effective_correction_action,
                    'effective_correction_fingerprint': (
                        row.effective_correction_fingerprint
                    ),
                }
                for row in locked['effective_plan'].decisions
            ],
            'create_resident_ids': [
                row.resident_id for row in changes['create_placements']
            ],
            'excluded_resident_ids': [
                row['decision'].resident_id for row in changes['excluded']
            ],
            'reused_occupancy_ids': list(reused_ids),
            'replaced_auto_occupancy_ids': list(auto_ids),
            'replaced_manual_occupancy_ids': list(manual_ids),
            'skipped_rows': skipped_rows,
            'skipped_count': len(skipped_rows),
            'applied_count': len(changes['create_placements']) + len(reused_ids),
        }
        application = SettlementPreviewApplication(
            preview_run=run,
            work_shift=work_shift,
            legacy_whole_run=False,
            watch_period=locked['period'],
            cohort=locked['cohort'],
            applied_by_access=actor_access,
            resolver_fingerprint=run.resolver_fingerprint,
            normalized_fingerprint=locked['effective_plan'].fingerprint,
            confirm_replace_manual=confirm_replace_manual,
            created_occupancy_count=len(changes['create_placements']),
            closed_occupancy_count=len(replace_rows),
            replaced_occupancy_count=changes['replaced_with_new'],
            result_snapshot=snapshot,
        )
        try:
            with transaction.atomic(using=plan.using):
                application.save()
        except IntegrityError as error:
            winner = (
                SettlementPreviewApplication._base_manager.using(plan.using)
                .filter(
                    preview_run_id=run.pk,
                    work_shift=work_shift,
                    legacy_whole_run=False,
                )
                .first()
            )
            if winner is not None:
                return winner
            raise _apply_error(
                'hard_conflict',
                'Concurrent Apply не удалось сериализовать.',
            ) from error

        for occupancy in replace_rows:
            occupancy.replaced_by_application = application
            occupancy.save(update_fields=['replaced_by_application'])
            action = (
                SettlementPreviewApplicationItem.Action.REPLACED_MANUAL
                if occupancy.source_kind == EmployeeBedOccupancy.SourceKind.MANUAL
                else SettlementPreviewApplicationItem.Action.REPLACED_AUTO
            )
            effective_placement = next(
                (
                    row for row in locked['effective_placements']
                    if row.resident_id == occupancy.resident_id
                ),
                None,
            )
            effective_excluded = next(
                (
                    row for row in changes['excluded']
                    if row['decision'].resident_id == occupancy.resident_id
                ),
                None,
            )
            effective_unresolved = next(
                (
                    row for row in locked['effective_unresolved']
                    if row['decision'].resident_id == occupancy.resident_id
                ),
                None,
            )
            correction = (
                effective_placement.effective_correction
                if effective_placement else effective_excluded['effective_correction']
                if effective_excluded else effective_unresolved['effective_correction']
                if effective_unresolved else None
            )
            source_placement = (
                effective_placement.source_placement
                if effective_placement else effective_excluded['source_placement']
                if effective_excluded else effective_unresolved['source_placement']
                if effective_unresolved else None
            )
            source_unresolved = (
                effective_placement.source_unresolved
                if effective_placement else effective_excluded['source_unresolved']
                if effective_excluded else effective_unresolved['source_unresolved']
                if effective_unresolved else None
            )
            SettlementPreviewApplicationItem(
                application=application,
                occupancy=occupancy,
                preview_placement=source_placement,
                preview_unresolved=source_unresolved,
                effective_correction=correction,
                correction_fingerprint=correction.fingerprint if correction else '',
                source_decision_snapshot=(
                    _base_snapshot_for_item(source_placement, source_unresolved)
                ),
                effective_decision_snapshot=(
                    _effective_snapshot_for_item(
                        effective_placement=effective_placement,
                        effective_excluded=effective_excluded,
                        effective_unresolved=effective_unresolved,
                    )
                ),
                action=action,
            ).save()

        for resident_id, occupancy in changes['reusable_by_resident'].items():
            placement = next(
                row for row in locked['effective_placements']
                if row.resident_id == resident_id
            )
            SettlementPreviewApplicationItem(
                application=application,
                occupancy=occupancy,
                preview_placement=placement.source_placement,
                preview_unresolved=placement.source_unresolved,
                effective_correction=placement.effective_correction,
                correction_fingerprint=(
                    placement.effective_correction.fingerprint
                    if placement.effective_correction else ''
                ),
                source_decision_snapshot=_source_decision_snapshot(placement),
                effective_decision_snapshot=_effective_decision_snapshot(placement),
                action=SettlementPreviewApplicationItem.Action.REUSED,
            ).save()

        for excluded in changes['excluded']:
            correction = excluded['effective_correction']
            decision = excluded['decision']
            SettlementPreviewApplicationItem(
                application=application,
                occupancy=None,
                preview_placement=excluded['source_placement'],
                preview_unresolved=excluded['source_unresolved'],
                effective_correction=correction,
                correction_fingerprint=correction.fingerprint,
                source_decision_snapshot=_base_snapshot_for_item(
                    excluded['source_placement'], excluded['source_unresolved'],
                ),
                effective_decision_snapshot={
                    'state': 'excluded',
                    'resident_id': decision.resident_id,
                    'work_shift': decision.work_shift,
                    'correction_id': correction.pk,
                    'correction_action': correction.action,
                    'correction_fingerprint': correction.fingerprint,
                },
                action=SettlementPreviewApplicationItem.Action.EXCLUDED,
            ).save()

        effective_rows = [
            row for row in locked['occupancies']
            if row not in replace_rows
        ]
        beds_by_id = {row.pk: row for row in locked['beds']}
        residents_by_id = {
            row.pk: row for row in locked['resident_rows'].residents
        }
        # SET-R098/R099: защищённый, служебный и резервный фонд авто не трогает
        # даже устаревшим preview.
        from .models import AccommodationAnchor as _Anchor
        from .special_fund import room_anchor_types

        target_room_ids = sorted({
            bed.room_id for bed in beds_by_id.values()
        })
        special_room_types: dict[int, set[str]] = {}
        for room_id in target_room_ids:
            room_obj = next(
                (room for room in locked['rooms'] if room.pk == room_id),
                None,
            )
            if room_obj is None:
                continue
            types = room_anchor_types(room=room_obj, using=plan.using)
            if types:
                special_room_types[room_id] = set(types)
        forbidden_types = {
            _Anchor.AnchorType.PROTECTED,
            _Anchor.AnchorType.SERVICE,
            _Anchor.AnchorType.RESERVE,
        }
        # Решение владельца 22.11.2026: плановые привязки жильцов текущего
        # состава (обе смены) не блокируют друг друга — их однородность
        # проверяет резолвер, классификация плана и жёсткий guard ниже.
        # Блокируют только привязки жильцов вне состава (другие вахты).
        planned_resident_ids = tuple(
            SettlementCohortMember._base_manager.using(plan.using)
            .filter(cohort_id=plan.cohort_id)
            .values_list('resident_id', flat=True)
        )
        for placement in changes['create_placements']:
            member = changes['members_by_resident'][placement.resident_id]
            resident = residents_by_id[placement.resident_id]
            bed = beds_by_id[placement.physical_bed_id]
            placement_type = changes['placement_types'].get(
                placement.resident_id,
                'temporary',
            )
            assignment_type = (
                EmployeeBedOccupancy.AssignmentType.TEMPORARY
                if placement_type == 'temporary'
                else EmployeeBedOccupancy.AssignmentType.PERMANENT
            )
            # SET-R097: двухместная комната не может попасть в авто-план даже
            # устаревшим preview.
            from .special_fund import is_manual_only_room, room_capacity_reason

            if is_manual_only_room(bed.room):
                code, message = room_capacity_reason()
                raise _apply_error(code, message)
            if special_room_types.get(bed.room_id, set()) & forbidden_types:
                raise _apply_error(
                    'protected_room_forbidden',
                    'Защищённый или резервный фонд не используется '
                    'авторасселением.',
                )
            _validate_resident_and_destination(
                resident=resident,
                room=bed.room,
                starts_at=member.arrival_at,
                ends_at=member.departure_at,
                using=plan.using,
                exclude_occupancy_ids=tuple(
                    row.pk for row in replace_rows
                ),
                exclude_resident_ids=planned_resident_ids,
            )
            # Жёсткий guard перед вставкой (решение владельца 22.11.2026):
            # повторяем проверку категории по строкам, которые реально остаются
            # в комнате на пересечении с member.
            if not resident.is_external:
                roommate_categories = [
                    employee_settlement_category(
                        residents_by_id[row.resident_id].employee,
                    )
                    for row in effective_rows
                    if row.resident_id != resident.pk
                    and row.physical_bed.room_id == bed.room_id
                    and _occupancy_overlaps_member(row, member)
                ]
                if not room_allows_personnel_category(
                    bed.room.room_type,
                    employee_settlement_category(resident.employee),
                    roommate_categories,
                ):
                    raise _apply_error(
                        'hard_conflict',
                        ROOM_CATEGORY_CONFLICT_MESSAGE,
                    )
            validate_placement_input_contract(
                resident=resident,
                bed_stable_id=bed.stable_id,
                assignment_type=assignment_type,
                starts_at=member.arrival_at,
                ends_at=member.departure_at,
            )
            try:
                _validate_occupancy_conflicts(
                    resident_id=resident.pk,
                    bed=bed,
                    starts_at=member.arrival_at,
                    ends_at=member.departure_at,
                    persisted_occupancies=effective_rows,
                )
            except ValidationError as error:
                raise _apply_error(
                    'hard_conflict',
                    'Целевой resident или PhysicalBed имеет пересекающуюся occupancy.',
                ) from error
            try:
                occupancy = EmployeeBedOccupancy(
                    resident=resident,
                    physical_bed=bed,
                    assignment_type=assignment_type,
                    source_kind=EmployeeBedOccupancy.SourceKind.AUTO,
                    work_shift=work_shift,
                    shift_source_kind=EmployeeBedOccupancy.ShiftSourceKind.AUTO_PREVIEW,
                    shift_source_fingerprint=member.shift_source_fingerprint,
                    source_application=application,
                    source_preview_placement=placement.source_placement,
                    source_preview_correction=placement.effective_correction,
                    watch_period=locked['period'],
                    cohort_member=member,
                    settled_at=member.arrival_at,
                    starts_at=member.arrival_at,
                    ends_at=member.departure_at,
                    terminated_at=None,
                    settled_by=actor_employee,
                    ended_at=None,
                )
                occupancy.save(using=plan.using)
            except IntegrityError as error:
                raise _apply_error(
                    'hard_conflict',
                    'Создать AUTO occupancy не удалось из-за конкурентного конфликта.',
                ) from error
            SettlementPreviewApplicationItem(
                application=application,
                occupancy=occupancy,
                preview_placement=placement.source_placement,
                preview_unresolved=placement.source_unresolved,
                effective_correction=placement.effective_correction,
                correction_fingerprint=(
                    placement.effective_correction.fingerprint
                    if placement.effective_correction else ''
                ),
                source_decision_snapshot=_source_decision_snapshot(placement),
                effective_decision_snapshot=_effective_decision_snapshot(placement),
                action=SettlementPreviewApplicationItem.Action.CREATED,
            ).save()
            effective_rows.append(occupancy)

        return application
