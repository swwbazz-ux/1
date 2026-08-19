"""Atomic, server-owned hand-off of a confirmed arrival roster."""

from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction

from settlement.models import SettlementResident
from shifts.models import WatchPeriod
from users.models import Employee, TemporaryWorkTransfer

from .arrival_rosters import (
    _access_snapshot,
    _canonical_sha256,
    _lock_employee_plan,
    _lock_timekeeper_access,
    _verified_timekeeper_access,
)
from .models import (
    ArrivalRosterMatch,
    ArrivalRosterRowReview,
    ArrivalRosterRoutingBatch,
    ArrivalRosterRoutingEvent,
    ArrivalRosterRoutingRow,
    ArrivalRosterVersion,
)


_PRODUCTION_ROLE_CODES = frozenset({'driver', 'excavator_operator'})


def _error(code, message):
    return ValidationError(message, code=code)


def _routing_preflight(*, version_id, actor_access_id):
    """Collect a deterministic lock plan without accepting subject identifiers."""
    access = _access_snapshot(actor_access_id)
    version = ArrivalRosterVersion._base_manager.filter(pk=version_id).values(
        'pk', 'watch_period_id',
    ).first()
    if version is None:
        raise _error('arrival_roster.routing_version_required', 'Версия реестра не найдена.')

    review_subjects = list(
        ArrivalRosterRowReview._base_manager.filter(version_id=version_id)
        .order_by('match_id')
        .values_list('selected_resident_id', 'selected_resident__employee_id')
    )
    resident_ids = sorted({resident_id for resident_id, _employee_id in review_subjects if resident_id})
    employee_ids = {access['employee_id']}
    employee_ids.update(
        employee_id for _resident_id, employee_id in review_subjects if employee_id
    )
    version_ids = list(
        ArrivalRosterVersion._base_manager.filter(watch_period_id=version['watch_period_id'])
        .order_by('pk')
        .values_list('pk', flat=True)
    )
    return access, sorted(employee_ids), resident_ids, version_ids


def _require_current_confirmed_version(*, version, versions):
    if (
        version.status == ArrivalRosterVersion.Status.SUPERSEDED
        or version.superseded_at is not None
    ):
        raise _error(
            'arrival_roster.routing_stale',
            'Переданная версия уже заменена новой утверждённой версией.',
        )
    if version.status != ArrivalRosterVersion.Status.CONFIRMED:
        raise _error(
            'arrival_roster.routing_version_not_confirmed',
            'Передать можно только утверждённую версию реестра.',
        )
    confirmed = [row for row in versions if row.status == ArrivalRosterVersion.Status.CONFIRMED]
    if len(confirmed) != 1 or confirmed[0].pk != version.pk:
        raise _error(
            'arrival_roster.routing_stale',
            'Версия больше не является действующей утверждённой версией периода.',
        )
    if (
        not version.confirmation_snapshot
        or not version.confirmation_sha256
        or version.confirmation_sha256 != _canonical_sha256(version.confirmation_snapshot)
    ):
        raise _error(
            'arrival_roster.routing_confirmation_invalid',
            'Снимок утверждённой версии повреждён или не совпадает с контрольной суммой.',
        )


def _require_existing_batch_consistency(*, batch, version):
    if (
        batch.watch_period_id != version.watch_period_id
        or batch.confirmation_sha256 != version.confirmation_sha256
    ):
        raise _error(
            'arrival_roster.routing_batch_invalid',
            'Существующая передача не соответствует утверждённой версии.',
        )


def _locked_roster_graph(*, version, resident_ids, locked_employees):
    """Lock and prove the exact Review → Match → Resident → Employee graph."""
    matches = list(
        ArrivalRosterMatch._base_manager.select_for_update(of=('self',))
        .filter(version=version)
        .order_by('pk')
    )
    reviews = list(
        ArrivalRosterRowReview._base_manager.select_for_update(of=('self',))
        .filter(version=version)
        .order_by('match_id')
    )
    if not matches or len(reviews) != len(matches):
        raise _error(
            'arrival_roster.routing_graph_incomplete',
            'В утверждённой версии неполный набор строк ручной проверки.',
        )
    matches_by_id = {match.pk: match for match in matches}
    if len(matches_by_id) != len(matches) or {review.match_id for review in reviews} != set(matches_by_id):
        raise _error(
            'arrival_roster.routing_graph_inconsistent',
            'Проверки не соответствуют точному набору сопоставлений версии.',
        )

    selected_resident_ids = sorted({review.selected_resident_id for review in reviews if review.selected_resident_id})
    if selected_resident_ids != resident_ids:
        raise _error(
            'arrival_roster.routing_subject_plan_changed',
            'Набор жильцов версии изменился во время передачи.',
        )
    residents = list(
        SettlementResident.objects.select_for_update(of=('self',))
        .filter(pk__in=resident_ids)
        .order_by('pk')
    )
    if [resident.pk for resident in residents] != resident_ids:
        raise _error(
            'arrival_roster.routing_resident_required',
            'Карточка жильца утверждённой строки не найдена.',
        )
    residents_by_id = {resident.pk: resident for resident in residents}

    for review in reviews:
        match = matches_by_id.get(review.match_id)
        resident = residents_by_id.get(review.selected_resident_id)
        if (
            match is None
            or review.version_id != version.pk
            or match.version_id != version.pk
            or review.resident_resolution != ArrivalRosterRowReview.ResidentResolution.SELECTED
            or resident is None
            or match.status != ArrivalRosterMatch.Status.EXACT
            or match.matched_resident_id != resident.pk
        ):
            raise _error(
                'arrival_roster.routing_graph_inconsistent',
                'Строка утверждённой версии не имеет точного согласованного жильца.',
            )
        if resident.employee_id and resident.employee_id not in locked_employees:
            raise _error(
                'arrival_roster.routing_subject_plan_changed',
                'Состав сотрудников изменился во время передачи.',
            )
        if resident.resident_type == SettlementResident.ResidentType.EMPLOYEE and not resident.employee_id:
            raise _error(
                'arrival_roster.routing_graph_inconsistent',
                'Внутренний жилец не связан с Employee.',
            )
    return matches_by_id, residents_by_id, reviews


def _qualification_snapshots(*, employee, transfers, as_of):
    """Use only the HR card and approved temporary transfer at one fixed date."""
    empty_role = {'role_code': None, 'qualification_state': 'not_production'}
    if employee is None:
        return empty_role, {'source': 'external_resident', 'as_of': as_of.isoformat()}

    active_transfers = transfers.get(employee.pk, [])
    if employee.personnel_position_id:
        if len(active_transfers) > 1:
            candidate_codes = sorted({
                transfer.target_specialization.access_role.code
                for transfer in active_transfers
                if (
                    transfer.target_specialization.is_active
                    and transfer.target_specialization.access_role_id
                    and transfer.target_specialization.access_role.code in _PRODUCTION_ROLE_CODES
                )
            })
            return (
                {
                    'role_code': None,
                    'qualification_state': 'ambiguous',
                    'candidate_role_codes': candidate_codes,
                },
                {
                    'source': 'approved_temporary_transfer_conflict',
                    'as_of': as_of.isoformat(),
                    'active_transfer_count': len(active_transfers),
                },
            )
        specialization = (
            active_transfers[0].target_specialization
            if active_transfers else employee.base_specialization
        )
        source = 'approved_temporary_transfer' if active_transfers else 'base_specialization'
    else:
        role_code = employee.work_category if employee.work_category in _PRODUCTION_ROLE_CODES else None
        return (
            {'role_code': role_code, 'qualification_state': 'exact' if role_code else 'not_production'},
            {'source': 'legacy_work_category', 'as_of': as_of.isoformat()},
        )

    role_code = None
    if (
        specialization
        and specialization.is_active
        and specialization.access_role_id
        and specialization.access_role.code in _PRODUCTION_ROLE_CODES
    ):
        role_code = specialization.access_role.code
    return (
        {'role_code': role_code, 'qualification_state': 'exact' if role_code else 'not_production'},
        {
            'source': source,
            'as_of': as_of.isoformat(),
            'specialization_code': specialization.code if specialization else None,
        },
    )


def _locked_active_transfers(*, employee_ids, as_of):
    transfers = defaultdict(list)
    for transfer in (
        TemporaryWorkTransfer.objects.select_related(
            'target_specialization', 'target_specialization__access_role',
        )
        .filter(
            employee_id__in=employee_ids,
            status=TemporaryWorkTransfer.Status.APPROVED,
            effective_from__lte=as_of,
            effective_to__gte=as_of,
        )
        .order_by('employee_id', '-reviewed_at', '-pk')
    ):
        transfers[transfer.employee_id].append(transfer)
    return transfers


def _participation_snapshot(review):
    return {
        'participation_status': review.participation_status,
        'arrival_mode': review.arrival_mode,
    }


def _dates_snapshot(review):
    return {
        'arrival_on': review.arrival_on.isoformat() if review.arrival_on else None,
        'departure_on': review.departure_on.isoformat() if review.departure_on else None,
    }


def _route_for_subject(*, review, employee, role_snapshot):
    if review.participation_status == ArrivalRosterRowReview.ParticipationStatus.NOT_ARRIVING:
        return ArrivalRosterRoutingRow.RouteState.NOT_PARTICIPATING
    if employee is None:
        return ArrivalRosterRoutingRow.RouteState.TO_CLERK
    if role_snapshot['qualification_state'] == 'ambiguous':
        return ArrivalRosterRoutingRow.RouteState.REVIEW_REQUIRED
    if role_snapshot['role_code'] in _PRODUCTION_ROLE_CODES:
        return ArrivalRosterRoutingRow.RouteState.TO_DEPUTY
    return ArrivalRosterRoutingRow.RouteState.TO_CLERK


def _trusted_insert_batch(*, version, actor_access):
    batch = ArrivalRosterRoutingBatch(
        arrival_roster_version=version,
        watch_period_id=version.watch_period_id,
        confirmation_sha256=version.confirmation_sha256,
        created_by_access=actor_access,
    )
    batch.full_clean()
    models.QuerySet.bulk_create(ArrivalRosterRoutingBatch._base_manager.all(), [batch])
    return batch


def _trusted_insert_rows(*, batch, plans):
    rows = [
        ArrivalRosterRoutingRow(
            batch=batch,
            row_review=plan['review'],
            match=plan['match'],
            resident=plan['resident'],
            employee=plan['employee'],
            participation_snapshot=plan['participation_snapshot'],
            dates_snapshot=plan['dates_snapshot'],
            role_snapshot=plan['role_snapshot'],
            role_basis_snapshot=plan['role_basis_snapshot'],
            route_state=plan['route_state'],
        )
        for plan in plans
    ]
    for row in rows:
        row.full_clean()
    models.QuerySet.bulk_create(ArrivalRosterRoutingRow._base_manager.all(), rows)
    return rows


def _trusted_insert_initial_events(*, rows, actor_access):
    events = []
    for row in rows:
        events.append(ArrivalRosterRoutingEvent(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.CREATED,
            actor_access=actor_access,
        ))
        event_type = {
            ArrivalRosterRoutingRow.RouteState.TO_DEPUTY: ArrivalRosterRoutingEvent.EventType.SENT_TO_DEPUTY,
            ArrivalRosterRoutingRow.RouteState.TO_CLERK: ArrivalRosterRoutingEvent.EventType.SENT_TO_CLERK,
            ArrivalRosterRoutingRow.RouteState.REVIEW_REQUIRED: ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW,
        }.get(row.route_state)
        if event_type:
            events.append(ArrivalRosterRoutingEvent(
                routing_row=row,
                event_type=event_type,
                actor_access=actor_access,
            ))
    for event in events:
        event.full_clean()
    models.QuerySet.bulk_create(ArrivalRosterRoutingEvent._base_manager.all(), events)


def _route_confirmed_arrival_roster_once(*, version_id, actor_access_id):
    access_plan, employee_ids, resident_ids, version_ids = _routing_preflight(
        version_id=version_id,
        actor_access_id=actor_access_id,
    )
    locked_employees = _lock_employee_plan(employee_ids)
    actor_context = _lock_timekeeper_access(access_plan, locked_employees=locked_employees)
    actor_access = _verified_timekeeper_access(actor_context)

    version_plan = ArrivalRosterVersion._base_manager.get(pk=version_id)
    period = (
        WatchPeriod.objects.select_for_update(of=('self',))
        .select_related('watch_composition')
        .get(pk=version_plan.watch_period_id)
    )
    versions = list(
        ArrivalRosterVersion._base_manager.select_for_update(of=('self',))
        .filter(pk__in=version_ids)
        .order_by('pk')
    )
    if [version.pk for version in versions] != version_ids:
        raise _error(
            'arrival_roster.routing_version_plan_changed',
            'Набор версий периода изменился во время передачи.',
        )
    version = next(version for version in versions if version.pk == version_id)
    _require_current_confirmed_version(version=version, versions=versions)

    existing_batch = (
        ArrivalRosterRoutingBatch._base_manager.select_for_update(of=('self',))
        .filter(arrival_roster_version=version)
        .first()
    )
    if existing_batch is not None:
        _require_existing_batch_consistency(batch=existing_batch, version=version)
        return existing_batch

    matches_by_id, residents_by_id, reviews = _locked_roster_graph(
        version=version,
        resident_ids=resident_ids,
        locked_employees=locked_employees,
    )
    transfers = _locked_active_transfers(employee_ids=locked_employees, as_of=period.starts_on)
    plans = []
    for review in reviews:
        resident = residents_by_id[review.selected_resident_id]
        employee = locked_employees.get(resident.employee_id)
        role_snapshot, role_basis_snapshot = _qualification_snapshots(
            employee=employee,
            transfers=transfers,
            as_of=period.starts_on,
        )
        plans.append({
            'review': review,
            'match': matches_by_id[review.match_id],
            'resident': resident,
            'employee': employee,
            'participation_snapshot': _participation_snapshot(review),
            'dates_snapshot': _dates_snapshot(review),
            'role_snapshot': role_snapshot,
            'role_basis_snapshot': role_basis_snapshot,
            'route_state': _route_for_subject(
                review=review,
                employee=employee,
                role_snapshot=role_snapshot,
            ),
        })
    batch = _trusted_insert_batch(version=version, actor_access=actor_access)
    rows = _trusted_insert_rows(batch=batch, plans=plans)
    _trusted_insert_initial_events(rows=rows, actor_access=actor_access)
    return batch


def _is_routing_batch_collision(error):
    cause = getattr(error, '__cause__', None)
    constraint_name = getattr(getattr(cause, 'diag', None), 'constraint_name', '') or ''
    if 'arrivalrosterroutingbatch' in constraint_name and 'arrival_roster_version' in constraint_name:
        return True
    message = str(cause or error)
    return (
        'UNIQUE constraint failed' in message
        and 'rotations_arrivalrosterroutingbatch.arrival_roster_version_id' in message
    )


def arrival_roster_routing_presentation(*, version):
    """Return the safe, human-facing routing status for one roster version."""
    batch = (
        ArrivalRosterRoutingBatch._base_manager
        .filter(arrival_roster_version_id=version.pk)
        .first()
    )
    if batch is None:
        return None
    counts = {
        'to_deputy': 0,
        'to_clerk': 0,
        'review_required': 0,
        'not_participating': 0,
    }
    for row in ArrivalRosterRoutingRow._base_manager.filter(batch=batch).values('route_state'):
        if row['route_state'] in counts:
            counts[row['route_state']] += 1
    return {
        'created_at': batch.created_at,
        'counts': counts,
    }


def route_confirmed_arrival_roster_version(*, version_id, actor_access_id):
    """Create the one immutable routing batch for the current confirmed version."""
    try:
        with transaction.atomic():
            return _route_confirmed_arrival_roster_once(
                version_id=version_id,
                actor_access_id=actor_access_id,
            )
    except IntegrityError as error:
        if not _is_routing_batch_collision(error):
            raise
    with transaction.atomic():
        return _route_confirmed_arrival_roster_once(
            version_id=version_id,
            actor_access_id=actor_access_id,
        )
