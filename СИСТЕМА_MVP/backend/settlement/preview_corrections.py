"""Append-only point corrections over an immutable confirmed M7 preview."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, router, transaction
from django.db.models import Q
from django.utils import timezone

from .cohorts import _revalidated_member_shift_source
from .control import (
    SettlementControlWriteContext,
    lock_settlement_write_access,
    lock_settlement_write_lease,
)
from .models import (
    AccommodationAnchorBedAssignment,
    AccommodationAnchorCalendarSlot,
    EmployeeBedOccupancy,
    PhysicalBed,
    PhysicalRoom,
    SettlementCohort,
    SettlementCohortMember,
    SettlementPreviewApplication,
    SettlementPreviewCorrection,
    SettlementPreviewPlacement,
    SettlementPreviewRun,
    SettlementPreviewUnresolved,
)
from .residents import (
    build_settlement_resident_lock_plan,
    lock_settlement_residents_after_access,
)
from .resolver import (
    _replaceable_occupancy_kind,
    resolve_settlement_cohort,
    validate_preview_correction_target,
)
from .saved_previews import (
    _assert_stored_result_matches,
    _validate_approved_cohort,
    _validate_resolver_result,
)


def _error(code, message, *, params=None):
    return ValidationError(
        message,
        code=f'settlement.preview_correction.{code}',
        params=params,
    )


def _hash(payload):
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    ).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _preview_is_stale_for_correction(*, run, cohort):
    try:
        _validate_approved_cohort(cohort, stale=True)
        result = resolve_settlement_cohort(
            cohort_id=cohort.pk,
            materialized_preview_run_id=run.pk,
        )
        validated = _validate_resolver_result(cohort=cohort, result=result)
        _assert_stored_result_matches(run=run, result=result, validated=validated)
    except ValidationError:
        return True
    return False


@dataclass(frozen=True, slots=True)
class EffectivePreviewDecision:
    resident_id: int
    work_shift: str
    state: str
    source_placement_id: int | None
    source_unresolved_id: int | None
    calendar_slot_id: int | None
    physical_bed_id: int | None
    effective_correction_id: int | None
    effective_correction_action: str | None
    effective_correction_fingerprint: str


@dataclass(frozen=True, slots=True)
class EffectiveSettlementPreviewPlan:
    preview_run_id: int
    base_result_fingerprint: str
    decisions: tuple[EffectivePreviewDecision, ...]
    correction_ids: tuple[int, ...]
    fingerprint: str

    def normalized_payload(self):
        return {
            'preview_run_id': self.preview_run_id,
            'base_result_fingerprint': self.base_result_fingerprint,
            'decisions': [asdict(item) for item in self.decisions],
            'correction_ids': list(self.correction_ids),
            'fingerprint': self.fingerprint,
        }


def _base_snapshot(source):
    if isinstance(source, SettlementPreviewPlacement):
        return {
            'kind': 'placement',
            'id': source.pk,
            'resident_id': source.resident_id,
            'work_shift': source.work_shift,
            'calendar_slot_id': source.calendar_slot_id,
            'physical_bed_id': source.physical_bed_id,
            'cohort_member_id': source.cohort_member_id_snapshot,
            'shift_source_fingerprint': source.normalized_provenance.get(
                'shift_source_fingerprint',
            ),
        }
    return {
        'kind': 'unresolved',
        'id': source.pk,
        'resident_id': source.resident_id,
        'work_shift': source.work_shift,
        'reason_code': source.reason_code,
        'cohort_member_id': source.cohort_member_id_snapshot,
        'shift_source_fingerprint': source.structured_details.get(
            'shift_source_fingerprint',
        ),
    }


def build_effective_settlement_preview_plan(*, run, placements, unresolved, corrections):
    base = {}
    for row in (*placements, *unresolved):
        key = (row.resident_id, row.work_shift)
        if key in base:
            raise _error('ambiguous_base_decision', 'Для жильца найдено несколько исходных решений.')
        base[key] = row

    chains = {}
    ordered_corrections = sorted(
        corrections,
        key=lambda row: (row.resident_id, row.work_shift, row.created_at, row.pk),
    )
    for correction in ordered_corrections:
        key = (correction.resident_id, correction.work_shift)
        if key not in base:
            raise _error('invalid_correction_chain', 'Исправление не имеет исходного решения.')
        chain = chains.setdefault(key, [])
        expected = chain[-1].pk if chain else None
        if correction.supersedes_id != expected:
            raise _error('invalid_correction_chain', 'Нарушена последовательность исправлений.')
        source = base[key]
        if (
            correction.source_placement_id
            != (source.pk if isinstance(source, SettlementPreviewPlacement) else None)
            or correction.source_unresolved_id
            != (source.pk if isinstance(source, SettlementPreviewUnresolved) else None)
        ):
            raise _error('invalid_correction_chain', 'Исправление ссылается на другую исходную строку.')
        expected_source_snapshot = _base_snapshot(source)
        expected_fingerprint = _hash({
            'preview_run_id': correction.preview_run_id,
            'resident_id': correction.resident_id,
            'work_shift': correction.work_shift,
            'action': correction.action,
            'source': expected_source_snapshot,
            'target_calendar_slot_id': correction.target_calendar_slot_id,
            'target_physical_bed_id': correction.target_physical_bed_id,
            'actor_access_id': correction.actor_access_id,
            'reason': correction.reason,
            'created_at': correction.created_at.isoformat(),
            'supersedes_id': correction.supersedes_id,
        })
        if (
            correction.source_snapshot != expected_source_snapshot
            or correction.fingerprint != expected_fingerprint
        ):
            raise _error(
                'invalid_correction_chain',
                'Неизменяемый снимок исправления не прошёл проверку.',
            )
        chain.append(correction)

    decisions = []
    chain_payload = []
    for key, source in sorted(base.items()):
        chain = chains.get(key, [])
        effective = chain[-1] if chain else None
        if effective is None or effective.action == SettlementPreviewCorrection.Action.RESTORE:
            if isinstance(source, SettlementPreviewPlacement):
                state = 'placement'
                slot_id = source.calendar_slot_id
                bed_id = source.physical_bed_id
            else:
                state = 'unresolved'
                slot_id = bed_id = None
        elif effective.action == SettlementPreviewCorrection.Action.MOVE:
            state = 'placement'
            slot_id = effective.target_calendar_slot_id
            bed_id = effective.target_physical_bed_id
        else:
            state = 'excluded'
            slot_id = bed_id = None
        decisions.append(EffectivePreviewDecision(
            resident_id=key[0],
            work_shift=key[1],
            state=state,
            source_placement_id=(
                source.pk if isinstance(source, SettlementPreviewPlacement) else None
            ),
            source_unresolved_id=(
                source.pk if isinstance(source, SettlementPreviewUnresolved) else None
            ),
            calendar_slot_id=slot_id,
            physical_bed_id=bed_id,
            effective_correction_id=effective.pk if effective else None,
            effective_correction_action=effective.action if effective else None,
            effective_correction_fingerprint=effective.fingerprint if effective else '',
        ))
        chain_payload.extend({
            'id': row.pk,
            'resident_id': row.resident_id,
            'work_shift': row.work_shift,
            'action': row.action,
            'source_placement_id': row.source_placement_id,
            'source_unresolved_id': row.source_unresolved_id,
            'target_calendar_slot_id': row.target_calendar_slot_id,
            'target_physical_bed_id': row.target_physical_bed_id,
            'actor_access_id': row.actor_access_id,
            'reason': row.reason,
            'created_at': row.created_at.isoformat(),
            'supersedes_id': row.supersedes_id,
            'fingerprint': row.fingerprint,
        } for row in chain)
    payload = {
        'preview_run_id': run.pk,
        'base_result_fingerprint': run.result_fingerprint,
        'decisions': [asdict(item) for item in decisions],
        'correction_chain': chain_payload,
    }
    return EffectiveSettlementPreviewPlan(
        preview_run_id=run.pk,
        base_result_fingerprint=run.result_fingerprint,
        decisions=tuple(decisions),
        correction_ids=tuple(row['id'] for row in chain_payload),
        fingerprint=_hash(payload),
    )


def get_effective_settlement_preview_plan(*, run_id, work_shift=None):
    run = SettlementPreviewRun._base_manager.get(pk=run_id)
    placements = list(
        SettlementPreviewPlacement._base_manager.filter(run_id=run_id)
        .filter(**({'work_shift': work_shift} if work_shift else {}))
        .order_by('resident_id', 'pk')
    )
    unresolved = list(
        SettlementPreviewUnresolved._base_manager.filter(run_id=run_id)
        .filter(**({'work_shift': work_shift} if work_shift else {}))
        .order_by('resident_id', 'pk')
    )
    corrections = list(
        SettlementPreviewCorrection._base_manager.filter(preview_run_id=run_id)
        .filter(**({'work_shift': work_shift} if work_shift else {}))
        .order_by('resident_id', 'work_shift', 'created_at', 'pk')
    )
    return build_effective_settlement_preview_plan(
        run=run,
        placements=placements,
        unresolved=unresolved,
        corrections=corrections,
    )


def _create_correction(*, action, run_id, resident_id, reason, control_context,
                       target_calendar_slot_id=None, target_physical_bed_id=None):
    if not str(reason or '').strip():
        raise _error('target_invalid', 'Укажите причину точечного исправления.')
    using = router.db_for_write(SettlementPreviewCorrection)
    run_snapshot = (
        SettlementPreviewRun._base_manager.using(using)
        .filter(pk=run_id).values('cohort_id', 'watch_period_id', 'status').first()
    )
    if run_snapshot is None:
        raise _error('run_invalid', 'Утверждённый план не найден.')
    base_resident_ids = tuple(sorted({
        *SettlementPreviewPlacement._base_manager.using(using)
        .filter(run_id=run_id).values_list('resident_id', flat=True),
        *SettlementPreviewUnresolved._base_manager.using(using)
        .filter(run_id=run_id).values_list('resident_id', flat=True),
    }))
    planned_bed_ids = {
        *SettlementPreviewPlacement._base_manager.using(using)
        .filter(run_id=run_id).values_list('physical_bed_id', flat=True),
        *SettlementPreviewCorrection._base_manager.using(using)
        .filter(preview_run_id=run_id, target_physical_bed_id__isnull=False)
        .values_list('target_physical_bed_id', flat=True),
    }
    if target_physical_bed_id:
        planned_bed_ids.add(target_physical_bed_id)
    planned_room_ids = tuple(
        PhysicalBed.objects.using(using)
        .filter(pk__in=planned_bed_ids)
        .order_by('room_id')
        .values_list('room_id', flat=True)
        .distinct()
    )
    occupancy_resident_ids = tuple(
        EmployeeBedOccupancy._base_manager.using(using)
        .filter(
            Q(resident_id__in=base_resident_ids)
            | Q(physical_bed_id__in=planned_bed_ids)
            | Q(physical_bed__room_id__in=planned_room_ids)
        )
        .order_by('resident_id')
        .values_list('resident_id', flat=True)
        .distinct()
    )
    resident_plan = build_settlement_resident_lock_plan(
        resident_ids=tuple(sorted({*base_resident_ids, *occupancy_resident_ids})),
        require_active=True,
        active_resident_ids=base_resident_ids,
        using=using,
    )

    with transaction.atomic(using=using):
        lease = lock_settlement_write_lease(control_context=control_context, using=using)
        access_rows = lock_settlement_write_access(
            lease=lease,
            control_context=control_context,
            employee_ids=resident_plan.employee_ids,
            using=using,
        )
        actor_access = access_rows.access_by_id(control_context.owner_access_id)
        locked_residents = lock_settlement_residents_after_access(
            resident_plan,
            locked_employees=access_rows.employees,
        )
        residents_by_id = {row.pk: row for row in locked_residents.residents}
        period = (
            run_model_period(using, run_snapshot['watch_period_id'])
        )
        cohort = (
            SettlementCohort._base_manager.using(using)
            .select_for_update(of=('self',)).get(pk=run_snapshot['cohort_id'])
        )
        run = (
            SettlementPreviewRun._base_manager.using(using)
            .select_for_update(of=('self',)).get(pk=run_id)
        )
        placements = list(
            SettlementPreviewPlacement._base_manager.using(using)
            .select_for_update(of=('self',)).filter(run_id=run_id).order_by('pk')
        )
        unresolved = list(
            SettlementPreviewUnresolved._base_manager.using(using)
            .select_for_update(of=('self',)).filter(run_id=run_id).order_by('pk')
        )
        corrections = list(
            SettlementPreviewCorrection._base_manager.using(using)
            .select_for_update(of=('self',)).filter(preview_run_id=run_id)
            .order_by('resident_id', 'work_shift', 'created_at', 'pk')
        )
        if run.status != SettlementPreviewRun.Status.CONFIRMED:
            raise _error('run_not_confirmed', 'Исправлять можно только утверждённый план.')
        source_rows = [
            row for row in (*placements, *unresolved) if row.resident_id == resident_id
        ]
        if not source_rows:
            raise _error('resident_absent', 'Жилец отсутствует в утверждённом плане.')
        if len(source_rows) != 1:
            raise _error('ambiguous_base_decision', 'Для жильца найдено несколько решений.')
        source = source_rows[0]
        member = (
            SettlementCohortMember._base_manager.using(using)
            .select_related(
                'resident__employee',
                'official_equipment_assignment__source_crew_plan_slot__plan',
                'shift_selected_by_access__employee', 'shift_selected_by_access__role',
            )
            .get(pk=source.cohort_member_id_snapshot)
        )
        if member.cohort_id != cohort.pk or member.resident_id != resident_id:
            raise _error('resident_absent', 'Строка состава жильца устарела.')
        _revalidated_member_shift_source(member=member, period=period, for_update=False)
        members_by_resident = {
            row.resident_id: row
            for row in SettlementCohortMember._base_manager.using(using)
            .filter(
                cohort_id=cohort.pk,
                participation_status__in=SettlementCohortMember.ACTIVE_PARTICIPATION_STATUSES,
            )
            .order_by('resident_id', 'pk')
        }
        chain = [
            row for row in corrections
            if row.resident_id == resident_id and row.work_shift == source.work_shift
        ]
        effective = chain[-1] if chain else None
        if action == SettlementPreviewCorrection.Action.RESTORE and (
            effective is None or effective.action == SettlementPreviewCorrection.Action.RESTORE
        ):
            raise _error('restore_unavailable', 'Нет действующего исправления для возврата.')

        all_slot_ids = {
            *(row.calendar_slot_id for row in placements),
            *(row.target_calendar_slot_id for row in corrections if row.target_calendar_slot_id),
        }
        if target_calendar_slot_id:
            all_slot_ids.add(target_calendar_slot_id)
        slots = list(
            AccommodationAnchorCalendarSlot._base_manager.using(using)
            .select_for_update(of=('self',)).filter(pk__in=sorted(all_slot_ids)).order_by('pk')
        )
        anchor_ids = tuple(sorted({row.anchor_id for row in slots}))
        list(
            AccommodationAnchorBedAssignment._base_manager.using(using)
            .select_for_update(of=('self',)).filter(anchor_id__in=anchor_ids).order_by('pk')
        )
        current_plan = build_effective_settlement_preview_plan(
            run=run, placements=placements, unresolved=unresolved, corrections=corrections,
        )
        bed_ids = {
            *(row.physical_bed_id for row in placements),
            *(row.physical_bed_id for row in current_plan.decisions if row.physical_bed_id),
        }
        if target_physical_bed_id:
            bed_ids.add(target_physical_bed_id)
        beds = list(
            PhysicalBed.objects.using(using).select_for_update(of=('self',))
            .select_related('room').filter(pk__in=sorted(bed_ids)).order_by('pk')
        )
        rooms = list(
            PhysicalRoom.objects.using(using).select_for_update(of=('self',))
            .filter(pk__in=sorted({row.room_id for row in beds})).order_by('pk')
        )
        occupancies = list(
            EmployeeBedOccupancy._base_manager.using(using)
            .select_for_update(of=('self',))
            .select_related(
                'resident', 'physical_bed__room', 'source_application__preview_run',
                'source_preview_placement', 'source_preview_correction__source_placement',
                'source_preview_correction__source_unresolved', 'cohort_member',
            )
            .filter(
                Q(resident_id__in=base_resident_ids)
                | Q(physical_bed_id__in=bed_ids)
                | Q(physical_bed__room_id__in=[row.pk for row in rooms])
            ).order_by('pk')
        )
        applications = list(
            SettlementPreviewApplication._base_manager.using(using)
            .select_for_update(of=('self',)).filter(preview_run_id=run.pk).order_by('pk')
        )
        if any(row.legacy_whole_run or row.work_shift == source.work_shift for row in applications):
            raise _error(
                'shift_already_applied',
                'Смена уже применена. Используйте переселение на карте текущей вахты.',
            )
        if _preview_is_stale_for_correction(run=run, cohort=cohort):
            raise _error('run_stale', 'Утверждённый план устарел.')

        target_slot = target_bed = None
        validation_slot_id = target_calendar_slot_id
        validation_bed_id = target_physical_bed_id
        if (
            action == SettlementPreviewCorrection.Action.RESTORE
            and isinstance(source, SettlementPreviewPlacement)
        ):
            validation_slot_id = source.calendar_slot_id
            validation_bed_id = source.physical_bed_id
        if validation_slot_id is not None or validation_bed_id is not None:
            validation_slot = next((row for row in slots if row.pk == validation_slot_id), None)
            validation_bed = next((row for row in beds if row.pk == validation_bed_id), None)
            if validation_slot is None or validation_bed is None:
                raise _error('target_invalid', 'Целевой слот или койка не найдены.')
            validate_preview_correction_target(
                cohort=cohort,
                resident=residents_by_id[resident_id],
                calendar_slot_id=validation_slot.pk,
                physical_bed_id=validation_bed.pk,
            )
            for decision in current_plan.decisions:
                other_member = members_by_resident.get(decision.resident_id)
                intervals_overlap = bool(
                    other_member
                    and member.arrival_at < other_member.departure_at
                    and other_member.arrival_at < member.departure_at
                )
                if (
                    decision.resident_id != resident_id
                    and decision.physical_bed_id == validation_bed.pk
                    and intervals_overlap
                ):
                    raise _error(
                        'target_conflict',
                        'Койка уже используется другим решением плана.',
                    )
            target_external = residents_by_id[resident_id].is_external
            bed_by_id = {row.pk: row for row in beds}
            for decision in current_plan.decisions:
                if decision.resident_id == resident_id or not decision.physical_bed_id:
                    continue
                other_bed = bed_by_id.get(decision.physical_bed_id)
                other_resident = residents_by_id.get(decision.resident_id)
                other_member = members_by_resident.get(decision.resident_id)
                if (
                    other_bed and other_bed.room_id == validation_bed.room_id
                    and other_resident and other_resident.is_external != target_external
                    and other_member
                    and member.arrival_at < other_member.departure_at
                    and other_member.arrival_at < member.departure_at
                ):
                    raise _error('target_conflict', 'Внутренние и внешние жильцы не смешиваются.')
            for occupancy in occupancies:
                overlaps = (
                    occupancy.starts_at < member.departure_at
                    and (occupancy.ends_at is None or occupancy.ends_at > member.arrival_at)
                    and (occupancy.terminated_at is None or occupancy.terminated_at > member.arrival_at)
                )
                if not overlaps:
                    continue
                occupancy_member = members_by_resident.get(occupancy.resident_id)
                replaceable = _replaceable_occupancy_kind(
                    occupancy=occupancy, cohort=cohort, member=occupancy_member,
                )
                if (
                    occupancy.resident_id == resident_id
                    or occupancy.physical_bed_id == validation_bed.pk
                ):
                    if replaceable is None:
                        raise _error('target_conflict', 'Целевое место конфликтует с фактическим проживанием.')
                if (
                    occupancy.physical_bed.room_id == validation_bed.room_id
                    and occupancy.resident.is_external != target_external
                ):
                    raise _error('target_conflict', 'Комната занята жильцами другого типа.')
            if action == SettlementPreviewCorrection.Action.MOVE:
                target_slot = validation_slot
                target_bed = validation_bed

        source_snapshot = _base_snapshot(source)
        created_at = timezone.now()
        fingerprint_payload = {
            'preview_run_id': run.pk,
            'resident_id': resident_id,
            'work_shift': source.work_shift,
            'action': action,
            'source': source_snapshot,
            'target_calendar_slot_id': target_calendar_slot_id,
            'target_physical_bed_id': target_physical_bed_id,
            'actor_access_id': actor_access.pk,
            'reason': str(reason).strip(),
            'created_at': created_at.isoformat(),
            'supersedes_id': effective.pk if effective else None,
        }
        correction = SettlementPreviewCorrection(
            preview_run=run,
            resident_id=resident_id,
            work_shift=source.work_shift,
            action=action,
            source_placement=source if isinstance(source, SettlementPreviewPlacement) else None,
            source_unresolved=source if isinstance(source, SettlementPreviewUnresolved) else None,
            target_calendar_slot=target_slot,
            target_physical_bed=target_bed,
            actor_access=actor_access,
            reason=str(reason).strip(),
            created_at=created_at,
            supersedes=effective,
            source_snapshot=source_snapshot,
            fingerprint=_hash(fingerprint_payload),
        )
        correction._allow_domain_insert = True
        try:
            correction.save(using=using)
        except IntegrityError as error:
            raise _error('invalid_correction_chain', 'Одновременно создана другая ветвь исправления.') from error
        return correction


def run_model_period(using, period_id):
    from shifts.models import WatchPeriod
    return WatchPeriod.objects.using(using).select_for_update(of=('self',)).get(pk=period_id)


def move_settlement_preview_resident(*, run_id, resident_id, target_calendar_slot_id,
                                     target_physical_bed_id, reason, control_context):
    return _create_correction(
        action=SettlementPreviewCorrection.Action.MOVE,
        run_id=run_id, resident_id=resident_id,
        target_calendar_slot_id=target_calendar_slot_id,
        target_physical_bed_id=target_physical_bed_id,
        reason=reason, control_context=control_context,
    )


def exclude_settlement_preview_resident(*, run_id, resident_id, reason, control_context):
    return _create_correction(
        action=SettlementPreviewCorrection.Action.EXCLUDE,
        run_id=run_id, resident_id=resident_id,
        reason=reason, control_context=control_context,
    )


def restore_settlement_preview_resident(*, run_id, resident_id, reason, control_context):
    return _create_correction(
        action=SettlementPreviewCorrection.Action.RESTORE,
        run_id=run_id, resident_id=resident_id,
        reason=reason, control_context=control_context,
    )
