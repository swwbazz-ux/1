"""Atomic M8 Apply of a confirmed M7 preview to resident-based occupancy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

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
    EmployeeBedOccupancy,
    PhysicalBed,
    PhysicalRoom,
    SettlementCohort,
    SettlementCohortMember,
    SettlementPreviewApplication,
    SettlementPreviewApplicationItem,
    SettlementPreviewPlacement,
    SettlementPreviewRun,
    SettlementPreviewUnresolved,
)
from .residents import (
    build_settlement_resident_lock_plan,
    lock_settlement_residents_after_access,
)
from .saved_previews import (
    _assert_stored_result_matches,
    _validate_approved_cohort,
    _validate_resolver_result,
)
from .resolver import resolve_settlement_cohort
from .services import (
    _validate_occupancy_conflicts,
    _validate_resident_and_destination,
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
    bed_snapshot: tuple
    room_snapshot: tuple
    occupancy_snapshot: tuple
    application_snapshot: tuple


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

    target_bed_ids = tuple(sorted({row.physical_bed_id for row in placements}))
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
                | Q(source_application__watch_period_id=period_row['pk'])
            )
            & Q(replaced_by_application__isnull=True)
            & Q(replaced_by_occupancy__isnull=True)
        )
        .select_related(
            'source_application__preview_run',
            'source_preview_placement',
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
            'pk', 'transfer_status', 'sex_restriction',
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
            row.pk, row.transfer_status, row.sex_restriction,
            row.dormitory_id, row.floor, row.number,
        )
        for row in rooms
    )
    if (
        actual_run_snapshot != plan.run_snapshot
        or actual_cohort_snapshot != plan.cohort_snapshot
        or actual_period_snapshot != plan.period_snapshot
        or _member_snapshot(members) != plan.member_snapshot
        or _placement_snapshot(placements) != plan.placement_snapshot
        or _unresolved_snapshot(unresolved) != plan.unresolved_snapshot
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
        'beds': beds,
        'rooms': rooms,
        'occupancies': occupancies,
        'application': application,
        'legacy_application': legacy_application,
    }


def _validate_locked_preview(locked):
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
        _assert_stored_result_matches(run=run, result=result, validated=validated)
    except ValidationError as error:
        raise _apply_error(
            'stale_preview',
            'Текущие источники не воспроизводят подтверждённый preview.',
        ) from error
    return result


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
    run = locked['run']
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
    for placement in locked['placements']:
        member = members_by_id[placement.cohort_member_id_snapshot]
        slot = placement.calendar_slot
        arrival_date = timezone.localdate(member.arrival_at)
        early_night = (
            work_shift == SettlementCohortMember.WorkShift.NIGHT
            and arrival_date == period.starts_on - timedelta(days=1)
        )
        if (
            placement.run_id != run.pk
            or slot.watch_period_id != period.pk
            or slot.calendar_relation_is_stale
        ):
            raise _apply_error('stale_preview', 'CalendarSlot смены устарел.')
        if arrival_date < slot.valid_from and not early_night:
            raise _apply_error(
                'stale_preview',
                'Начало размещения находится раньше допустимой границы CalendarSlot.',
            )


def _classify_changes(*, locked, confirm_replace_manual):
    run = locked['run']
    period = locked['period']
    work_shift = locked['work_shift']
    members_by_id = {row.pk: row for row in locked['members']}
    members_by_resident = {row.resident_id: row for row in locked['members']}
    placements_by_resident = {row.resident_id: row for row in locked['placements']}
    if len(placements_by_resident) != len(locked['placements']):
        raise _apply_error('incomplete_preview', 'Preview содержит duplicate resident.')
    if len({row.physical_bed_id for row in locked['placements']}) != len(locked['placements']):
        raise _apply_error('incomplete_preview', 'Preview содержит duplicate PhysicalBed.')
    all_result_residents = {
        *(row.resident_id for row in locked['placements']),
        *(row.resident_id for row in locked['unresolved']),
    }
    if all_result_residents != set(members_by_resident):
        raise _apply_error('incomplete_preview', 'Каждый member должен иметь ровно одну строку.')
    for placement in locked['placements']:
        member = members_by_id.get(placement.cohort_member_id_snapshot)
        if member is None or member.resident_id != placement.resident_id:
            raise _apply_error('incomplete_preview', 'Placement содержит stale membership.')

    target_scope = []
    for occupancy in locked['occupancies']:
        is_previous_auto = (
            occupancy.source_kind == EmployeeBedOccupancy.SourceKind.AUTO
            and occupancy.shift_source_kind
            == EmployeeBedOccupancy.ShiftSourceKind.AUTO_PREVIEW
            and occupancy.work_shift == work_shift
            and occupancy.source_application_id is not None
            and not occupancy.source_application.legacy_whole_run
            and occupancy.source_application.work_shift == work_shift
            and occupancy.source_application.watch_period_id == period.pk
            and occupancy.source_preview_placement_id is not None
            and occupancy.source_preview_placement.work_shift == work_shift
            and occupancy.cohort_member_id is not None
            and occupancy.shift_source_fingerprint
            == occupancy.source_preview_placement.normalized_provenance.get(
                'shift_source_fingerprint'
            )
            and occupancy.replaced_by_application_id is None
            and occupancy.replaced_by_occupancy_id is None
            and occupancy.terminated_at is None
            and occupancy.ended_at is None
        )
        is_target_manual = (
            occupancy.source_kind == EmployeeBedOccupancy.SourceKind.MANUAL
            and occupancy.shift_source_kind in {
                EmployeeBedOccupancy.ShiftSourceKind.OFFICIAL_ASSIGNMENT,
                EmployeeBedOccupancy.ShiftSourceKind.CLERK_SELECTED,
            }
            and occupancy.work_shift == work_shift
            and occupancy.watch_period_id == period.pk
            and occupancy.resident_id in members_by_resident
            and occupancy.cohort_member_id == members_by_resident[occupancy.resident_id].pk
            and occupancy.replaced_by_application_id is None
            and occupancy.replaced_by_occupancy_id is None
            and occupancy.terminated_at is None
            and occupancy.ended_at is None
        )
        if is_previous_auto or is_target_manual:
            target_scope.append(occupancy)

    reusable_by_resident = {}
    for occupancy in target_scope:
        placement = placements_by_resident.get(occupancy.resident_id)
        member = members_by_resident.get(occupancy.resident_id)
        if (
            occupancy.source_kind == EmployeeBedOccupancy.SourceKind.AUTO
            and placement is not None
            and member is not None
            and occupancy.source_application.cohort_id == run.cohort_id
            and occupancy.source_application.work_shift == work_shift
            and occupancy.work_shift == work_shift
            and occupancy.source_preview_placement.work_shift == work_shift
            and occupancy.source_preview_placement_id is not None
            and occupancy.source_preview_placement.run_id
            == occupancy.source_application.preview_run_id
            and occupancy.source_preview_placement.resident_id == occupancy.resident_id
            and occupancy.source_preview_placement.physical_bed_id
            == occupancy.physical_bed_id
            and occupancy.source_preview_placement.cohort_member_id_snapshot == member.pk
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
            overlaps_member = (
                occupancy.starts_at < member.departure_at
                and (occupancy.ends_at is None or occupancy.ends_at > member.arrival_at)
                and (
                    occupancy.terminated_at is None
                    or occupancy.terminated_at > member.arrival_at
                )
            )
            if overlaps_member:
                raise _apply_error(
                    'hard_conflict',
                    'Resident выбранной смены имеет unrelated occupancy.',
                )
        for placement in locked['placements']:
            member = members_by_resident[placement.resident_id]
            overlaps = (
                occupancy.starts_at < member.departure_at
                and (occupancy.ends_at is None or occupancy.ends_at > member.arrival_at)
                and (
                    occupancy.terminated_at is None
                    or occupancy.terminated_at > member.arrival_at
                )
            )
            if not overlaps:
                continue
            if (
                occupancy.resident_id == placement.resident_id
                or occupancy.physical_bed_id == placement.physical_bed_id
            ):
                raise _apply_error(
                    'hard_conflict',
                    'Целевой resident или PhysicalBed занят unrelated occupancy.',
                )

    create_placements = [
        row for row in locked['placements']
        if row.resident_id not in reusable_by_resident
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
        'replaced_with_new': replaced_with_new,
    }


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
        _validate_locked_preview(locked)
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
            'placement_ids': [row.pk for row in locked['placements']],
            'unresolved_ids': [row.pk for row in locked['unresolved']],
            'create_placement_ids': [row.pk for row in changes['create_placements']],
            'reused_occupancy_ids': list(reused_ids),
            'replaced_auto_occupancy_ids': list(auto_ids),
            'replaced_manual_occupancy_ids': list(manual_ids),
        }
        application = SettlementPreviewApplication(
            preview_run=run,
            work_shift=work_shift,
            legacy_whole_run=False,
            watch_period=locked['period'],
            cohort=locked['cohort'],
            applied_by_access=actor_access,
            resolver_fingerprint=run.resolver_fingerprint,
            normalized_fingerprint=run.result_fingerprint,
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
            SettlementPreviewApplicationItem(
                application=application,
                occupancy=occupancy,
                preview_placement=next(
                    (
                        row for row in locked['placements']
                        if row.resident_id == occupancy.resident_id
                    ),
                    None,
                ),
                action=action,
            ).save()

        for resident_id, occupancy in changes['reusable_by_resident'].items():
            placement = next(
                row for row in locked['placements']
                if row.resident_id == resident_id
            )
            SettlementPreviewApplicationItem(
                application=application,
                occupancy=occupancy,
                preview_placement=placement,
                action=SettlementPreviewApplicationItem.Action.REUSED,
            ).save()

        effective_rows = [
            row for row in locked['occupancies']
            if row not in replace_rows
        ]
        beds_by_id = {row.pk: row for row in locked['beds']}
        residents_by_id = {
            row.pk: row for row in locked['resident_rows'].residents
        }
        for placement in changes['create_placements']:
            member = changes['members_by_resident'][placement.resident_id]
            resident = residents_by_id[placement.resident_id]
            bed = beds_by_id[placement.physical_bed_id]
            _validate_resident_and_destination(resident=resident, room=bed.room)
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
                    assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
                    source_kind=EmployeeBedOccupancy.SourceKind.AUTO,
                    work_shift=work_shift,
                    shift_source_kind=EmployeeBedOccupancy.ShiftSourceKind.AUTO_PREVIEW,
                    shift_source_fingerprint=member.shift_source_fingerprint,
                    source_application=application,
                    source_preview_placement=placement,
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
                preview_placement=placement,
                action=SettlementPreviewApplicationItem.Action.CREATED,
            ).save()
            effective_rows.append(occupancy)

        return application
