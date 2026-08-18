"""Immutable M7 saved previews built from the read-only M6 resolver."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from django.core.exceptions import ValidationError
from django.db import IntegrityError, router, transaction
from django.utils import timezone
from shifts.models import WatchPeriod

from .control import (
    SettlementControlWriteContext,
    lock_settlement_write_access,
    lock_settlement_write_lease,
)
from .models import (
    SettlementCohort,
    SettlementCohortMember,
    SettlementPreviewPlacement,
    SettlementPreviewRun,
    SettlementPreviewUnresolved,
)
from .resolver import SettlementResolverResult, resolve_settlement_cohort


def _preview_error(code: str, message: str) -> ValidationError:
    return ValidationError(message, code=f'settlement.preview.{code}')


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    )


def _json_value(value):
    return json.loads(_canonical_json(value))


def _canonical_hash(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()


def _result_rows_payload(result: SettlementResolverResult) -> dict:
    return _json_value({
        'placements': [asdict(item) for item in result.placements],
        'unresolved': [asdict(item) for item in result.unresolved],
    })


def _source_snapshot(result: SettlementResolverResult) -> dict:
    return _json_value({'resolver_result': result.normalized_payload()})


def _validate_resolver_result(
    *,
    cohort: SettlementCohort,
    result: SettlementResolverResult,
) -> dict:
    if (
        result.cohort_id != cohort.pk
        or result.cohort_stable_id != str(cohort.stable_id)
        or result.watch_period_id != cohort.watch_period_id
    ):
        raise _preview_error(
            'incomplete_result',
            'Resolver вернул результат для другого cohort или WatchPeriod.',
        )

    active_members = list(
        SettlementCohortMember.objects.filter(
            cohort_id=cohort.pk,
            participation_status__in=SettlementCohortMember.ACTIVE_PARTICIPATION_STATUSES,
        )
        .order_by('resident_id', 'pk')
        .values(
            'pk', 'resident_id', 'work_shift',
            'shift_source_kind', 'shift_source_fingerprint',
        )
    )
    expected_by_resident = {
        item['resident_id']: item
        for item in active_members
    }
    placement_resident_ids = [item.resident_id for item in result.placements]
    unresolved_resident_ids = [item.resident_id for item in result.unresolved]
    all_result_ids = placement_resident_ids + unresolved_resident_ids
    if (
        len(set(placement_resident_ids)) != len(placement_resident_ids)
        or len(set(unresolved_resident_ids)) != len(unresolved_resident_ids)
        or set(placement_resident_ids).intersection(unresolved_resident_ids)
        or set(all_result_ids) != set(expected_by_resident)
    ):
        raise _preview_error(
            'incomplete_result',
            'Каждый действующий resident должен присутствовать ровно в одной строке preview.',
        )
    for item in (*result.placements, *result.unresolved):
        expected = expected_by_resident.get(item.resident_id)
        if (
            expected is None
            or expected['pk'] != item.member_id
            or expected['work_shift'] != item.work_shift
            or expected['shift_source_kind'] != item.shift_source_kind
            or expected['shift_source_fingerprint'] != item.shift_source_fingerprint
            or item.work_shift not in SettlementCohortMember.WorkShift.values
        ):
            raise _preview_error(
                'incomplete_result',
                'Resolver вернул stale membership или непроверенную смену.',
            )
    bed_ids = [item.physical_bed_id for item in result.placements]
    slot_ids = [item.calendar_slot_id for item in result.placements]
    if len(set(bed_ids)) != len(bed_ids) or len(set(slot_ids)) != len(slot_ids):
        raise _preview_error(
            'incomplete_result',
            'Resolver вернул повторяющийся физический или календарный ресурс.',
        )
    if len(result.input_fingerprint) != 64:
        raise _preview_error(
            'incomplete_result',
            'Resolver вернул недопустимый fingerprint.',
        )
    rows_payload = _result_rows_payload(result)
    return {
        'rows': rows_payload,
        'result_fingerprint': _canonical_hash(result.normalized_payload()),
        'source_snapshot': _source_snapshot(result),
    }


def _lock_preview_scope(
    *,
    cohort_id: int,
    control_context: SettlementControlWriteContext,
):
    using = router.db_for_write(SettlementPreviewRun)
    cohort_snapshot = (
        SettlementCohort._base_manager.using(using)
        .filter(pk=cohort_id)
        .values('watch_period_id', 'watch_composition_id')
        .first()
    )
    if cohort_snapshot is None:
        raise SettlementCohort.DoesNotExist

    lease = lock_settlement_write_lease(
        control_context=control_context,
        using=using,
    )
    locked_access_rows = lock_settlement_write_access(
        lease=lease,
        control_context=control_context,
        employee_ids=(),
        using=using,
    )
    actor_access = locked_access_rows.access_by_id(control_context.owner_access_id)

    period = (
        WatchPeriod.objects.using(using)
        .select_for_update(of=('self',))
        .get(pk=cohort_snapshot['watch_period_id'])
    )
    cohort = (
        SettlementCohort._base_manager.using(using)
        .select_for_update(of=('self',))
        .select_related('watch_period', 'watch_composition')
        .get(pk=cohort_id)
    )
    if (
        cohort.watch_period_id != cohort_snapshot['watch_period_id']
        or cohort.watch_composition_id != cohort_snapshot['watch_composition_id']
        or period.pk != cohort.watch_period_id
    ):
        raise _preview_error(
            'stale_source',
            'Cohort изменился после построения lock plan.',
        )
    runs = list(
        SettlementPreviewRun._base_manager.using(using)
        .select_for_update(of=('self',))
        .filter(watch_period_id=period.pk)
        .order_by('pk')
    )
    return using, actor_access, period, cohort, runs


def _validate_approved_cohort(cohort: SettlementCohort, *, stale: bool = False):
    if cohort.status != SettlementCohort.Status.APPROVED:
        code = 'stale_source' if stale else 'not_approved'
        raise _preview_error(code, 'Для preview требуется актуальный APPROVED cohort.')
    if (
        cohort.watch_period.watch_composition_id != cohort.watch_composition_id
        or not cohort.watch_period.is_active
        or not cohort.watch_composition.is_active
    ):
        raise _preview_error(
            'stale_source' if stale else 'not_approved',
            'Календарное основание cohort неактуально.',
        )


def _current_confirmed_run(runs, *, exclude_id=None):
    confirmed = [
        item for item in runs
        if item.status == SettlementPreviewRun.Status.CONFIRMED
        and item.pk != exclude_id
    ]
    if len(confirmed) > 1:
        raise _preview_error(
            'incomplete_result',
            'Обнаружено несколько подтверждённых preview одного WatchPeriod.',
        )
    return confirmed[0] if confirmed else None


def _save_result_rows(*, run, result):
    for item in result.placements:
        provenance = _json_value(asdict(item))
        SettlementPreviewPlacement(
            run=run,
            resident_id=item.resident_id,
            calendar_slot_id=item.calendar_slot_id,
            physical_bed_id=item.physical_bed_id,
            action=item.action,
            source_kind=item.source_kind,
            work_shift=item.work_shift,
            cohort_member_id_snapshot=item.member_id,
            physical_room_id_snapshot=item.physical_room_id,
            binding_id_snapshot=item.binding_id,
            equipment_assignment_id_snapshot=item.equipment_assignment_id,
            anchor_id_snapshot=item.anchor_id,
            anchor_bed_assignment_id_snapshot=item.anchor_bed_assignment_id,
            normalized_provenance=provenance,
        ).save()
    for item in result.unresolved:
        details = _json_value(asdict(item))
        SettlementPreviewUnresolved(
            run=run,
            resident_id=item.resident_id,
            reason_code=item.reason_codes[0],
            reason_codes=list(item.reason_codes),
            work_shift=item.work_shift,
            cohort_member_id_snapshot=item.member_id,
            structured_details=details,
        ).save()


def _stored_rows_payload(run: SettlementPreviewRun) -> dict:
    placements = list(
        SettlementPreviewPlacement._base_manager.filter(run_id=run.pk)
        .order_by('resident__stable_id', 'pk')
        .values_list('normalized_provenance', flat=True)
    )
    unresolved = list(
        SettlementPreviewUnresolved._base_manager.filter(run_id=run.pk)
        .order_by('resident__stable_id', 'pk')
        .values_list('structured_details', flat=True)
    )
    return _json_value({'placements': placements, 'unresolved': unresolved})


def _assert_stored_result_matches(
    *,
    run: SettlementPreviewRun,
    result: SettlementResolverResult,
    validated: dict,
):
    stored_rows = _stored_rows_payload(run)
    if (
        run.resolver_fingerprint != result.input_fingerprint
        or run.result_fingerprint != validated['result_fingerprint']
        or run.source_snapshot != validated['source_snapshot']
        or stored_rows != validated['rows']
    ):
        raise _preview_error(
            'stale_source',
            'Сохранённый preview больше не совпадает с текущим resolver result.',
        )


@transaction.atomic
def create_settlement_preview_run(
    *,
    cohort_id: int,
    control_context: SettlementControlWriteContext,
) -> SettlementPreviewRun:
    using, actor_access, _period, cohort, runs = _lock_preview_scope(
        cohort_id=cohort_id,
        control_context=control_context,
    )
    _validate_approved_cohort(cohort)
    result = resolve_settlement_cohort(cohort_id=cohort.pk)
    validated = _validate_resolver_result(cohort=cohort, result=result)
    base_confirmed = _current_confirmed_run(runs)
    version = max((item.version for item in runs), default=0) + 1
    run = SettlementPreviewRun(
        cohort=cohort,
        watch_period_id=cohort.watch_period_id,
        watch_composition_id=cohort.watch_composition_id,
        version=version,
        resolver_fingerprint=result.input_fingerprint,
        result_fingerprint=validated['result_fingerprint'],
        requires_shift_split=True,
        source_snapshot=validated['source_snapshot'],
        base_confirmed_run=base_confirmed,
        created_by_access=actor_access,
    )
    try:
        with transaction.atomic(using=using):
            run.save()
            _save_result_rows(run=run, result=result)
    except IntegrityError as error:
        raise _preview_error(
            'incomplete_result',
            'Сохранить полную непротиворечивую версию preview не удалось.',
        ) from error
    if _stored_rows_payload(run) != validated['rows']:
        raise _preview_error(
            'incomplete_result',
            'Сохранённые строки preview не совпадают с resolver result.',
        )
    return run


@transaction.atomic
def confirm_settlement_preview_run(
    *,
    run_id: int,
    control_context: SettlementControlWriteContext,
) -> SettlementPreviewRun:
    using = router.db_for_write(SettlementPreviewRun)
    run_snapshot = (
        SettlementPreviewRun._base_manager.using(using)
        .filter(pk=run_id)
        .values('cohort_id', 'watch_period_id')
        .first()
    )
    if run_snapshot is None:
        raise SettlementPreviewRun.DoesNotExist
    using, actor_access, _period, cohort, runs = _lock_preview_scope(
        cohort_id=run_snapshot['cohort_id'],
        control_context=control_context,
    )
    run_by_id = {item.pk: item for item in runs}
    run = run_by_id.get(run_id)
    if run is None or run.watch_period_id != run_snapshot['watch_period_id']:
        raise _preview_error('stale_source', 'Preview изменился после построения lock plan.')

    list(
        SettlementPreviewPlacement._base_manager.using(using)
        .select_for_update(of=('self',))
        .filter(run_id=run.pk)
        .order_by('pk')
    )
    list(
        SettlementPreviewUnresolved._base_manager.using(using)
        .select_for_update(of=('self',))
        .filter(run_id=run.pk)
        .order_by('pk')
    )

    if run.status == SettlementPreviewRun.Status.CONFIRMED:
        return run
    if run.status != SettlementPreviewRun.Status.DRAFT:
        raise _preview_error('invalid_state', 'Подтвердить можно только DRAFT preview.')

    current_confirmed = _current_confirmed_run(runs, exclude_id=run.pk)
    current_confirmed_id = current_confirmed.pk if current_confirmed else None
    if current_confirmed_id != run.base_confirmed_run_id:
        raise _preview_error(
            'concurrent_confirmation',
            'После создания DRAFT уже подтверждён другой preview.',
        )
    _validate_approved_cohort(cohort, stale=True)
    result = resolve_settlement_cohort(cohort_id=cohort.pk)
    try:
        validated = _validate_resolver_result(cohort=cohort, result=result)
    except ValidationError as error:
        raise _preview_error(
            'stale_source',
            'Источник preview больше не воспроизводит сохранённый результат.',
        ) from error
    _assert_stored_result_matches(run=run, result=result, validated=validated)

    confirmation_time = timezone.now()
    try:
        with transaction.atomic(using=using):
            if current_confirmed is not None:
                current_confirmed.status = SettlementPreviewRun.Status.SUPERSEDED
                current_confirmed.superseded_at = confirmation_time
                current_confirmed.revision += 1
                current_confirmed.save(
                    update_fields=['status', 'superseded_at', 'revision'],
                )
            run.status = SettlementPreviewRun.Status.CONFIRMED
            run.supersedes = current_confirmed
            run.confirmed_by_access = actor_access
            run.confirmed_at = confirmation_time
            run.revision += 1
            run.save(
                update_fields=[
                    'status', 'supersedes', 'confirmed_by_access',
                    'confirmed_at', 'revision',
                ],
            )
    except IntegrityError as error:
        raise _preview_error(
            'concurrent_confirmation',
            'Одновременно был подтверждён другой preview.',
        ) from error
    return run


def settlement_preview_is_stale(*, run_id: int) -> bool:
    run = (
        SettlementPreviewRun._base_manager
        .select_related('cohort__watch_period', 'cohort__watch_composition')
        .get(pk=run_id)
    )
    if run.status != SettlementPreviewRun.Status.CONFIRMED:
        raise _preview_error(
            'invalid_state',
            'Актуальность определяется только для CONFIRMED preview.',
        )
    cohort = run.cohort
    try:
        _validate_approved_cohort(cohort, stale=True)
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        validated = _validate_resolver_result(cohort=cohort, result=result)
        _assert_stored_result_matches(run=run, result=result, validated=validated)
    except ValidationError:
        return True
    return False
