"""Atomic, server-owned hand-off of a confirmed arrival roster."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

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

BATCH_STATE_CURRENT = 'current'
BATCH_STATE_STALE = 'stale'
BATCH_STATE_INCONSISTENT = 'inconsistent'

EVIDENCE_NOT_ARRIVING = 'not_arriving'
EVIDENCE_SENT_TO_CLERK = 'sent_to_clerk'
EVIDENCE_OFFICIAL_ASSIGNMENT_PUBLISHED = 'official_assignment_published'
EVIDENCE_PENDING = 'pending'
EVIDENCE_REQUIRES_REVIEW = 'requires_review'
EVIDENCE_STALE = 'stale'
EVIDENCE_INCONSISTENT = 'inconsistent'

ERROR_BATCH_NOT_FOUND = 'batch_not_found'
ERROR_BATCH_STALE = 'batch_stale'
ERROR_BATCH_INCONSISTENT = 'batch_inconsistent'
ERROR_ROUTING_PENDING = 'routing_pending'
ERROR_ROUTING_REQUIRES_REVIEW = 'routing_requires_review'
ERROR_ROUTING_STALE = 'routing_stale'
ERROR_ROUTING_INCONSISTENT = 'routing_inconsistent'
ERROR_OFFICIAL_ASSIGNMENT_MISSING = 'official_assignment_missing'
ERROR_OFFICIAL_ASSIGNMENT_INCONSISTENT = 'official_assignment_inconsistent'
ERROR_UNKNOWN_ROUTE_STATE = 'unknown_route_state'


@dataclass(frozen=True, slots=True)
class ArrivalRosterRoutingRowEvidence:
    routing_row_id: int
    resident_id: int
    employee_id: int | None
    route_state: str
    participating: bool
    evidence_state: str
    blocker_code: str | None
    latest_event_id: int | None
    latest_event_type: str | None
    crew_plan_slot_id: int | None
    equipment_assignment_id: int | None
    assignment_shift_type: str | None


@dataclass(frozen=True, slots=True)
class ArrivalRosterRoutingBatchEvidence:
    batch_id: int
    version_id: int
    watch_period_id: int
    batch_state: str
    batch_blocker_code: str | None
    rows: tuple[ArrivalRosterRoutingRowEvidence, ...]


@dataclass(frozen=True, slots=True)
class _ProductionAssignmentEvidence:
    blocker_code: str | None
    message: str | None
    equipment: object | None
    shift_type: str | None


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


def _queue_active_transfers(*, employee_ids, as_of):
    """Read the current approved OUP qualification source without taking locks."""
    transfers = defaultdict(list)
    if not employee_ids:
        return transfers
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


def _queue_date_label(value):
    if not value:
        return '—'
    try:
        return date.fromisoformat(value).strftime('%d.%m.%Y')
    except (TypeError, ValueError):
        return '—'


def deputy_arrival_roster_routing_queue():
    """Return the deputy's safe, read-only pending official-assignment queue."""
    rows = list(
        ArrivalRosterRoutingRow._base_manager.select_related(
            'batch__arrival_roster_version__watch_period', 'employee',
        )
        .filter(
            batch__arrival_roster_version__status=ArrivalRosterVersion.Status.CONFIRMED,
            batch__arrival_roster_version__superseded_at__isnull=True,
            route_state=ArrivalRosterRoutingRow.RouteState.TO_DEPUTY,
            employee__isnull=False,
        )
        .exclude(
            events__event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
        )
        .order_by('batch__arrival_roster_version__watch_period__starts_on', 'employee__full_name')
        .distinct()
    )

    role_labels = {
        'driver': 'Водитель',
        'excavator_operator': 'Машинист экскаватора',
    }
    latest_event_by_row = {}
    for event in (
        ArrivalRosterRoutingEvent._base_manager.filter(
            routing_row_id__in=[row.pk for row in rows],
        )
        .order_by('routing_row_id', 'created_at', 'pk')
        .values('routing_row_id', 'event_type')
    ):
        latest_event_by_row[event['routing_row_id']] = event['event_type']
    transfer_cache = {}
    groups = {}
    for row in rows:
        participation = row.participation_snapshot or {}
        if participation.get('participation_status') == ArrivalRosterRowReview.ParticipationStatus.NOT_ARRIVING:
            continue
        saved_role = (row.role_snapshot or {}).get('role_code')
        if saved_role not in role_labels:
            continue
        period = row.batch.arrival_roster_version.watch_period
        cache_key = period.pk
        if cache_key not in transfer_cache:
            employee_ids = [
                candidate.employee_id
                for candidate in rows
                if candidate.batch.arrival_roster_version.watch_period_id == period.pk
                and candidate.employee_id
            ]
            transfer_cache[cache_key] = _queue_active_transfers(
                employee_ids=employee_ids,
                as_of=period.starts_on,
            )
        current_role, _current_basis = _qualification_snapshots(
            employee=row.employee,
            transfers=transfer_cache[cache_key],
            as_of=period.starts_on,
        )
        role_changed = (
            current_role.get('qualification_state') != 'exact'
            or current_role.get('role_code') != saved_role
        )
        requires_review = (
            role_changed
            or latest_event_by_row.get(row.pk)
            == ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW
        )
        group_key = (period.starts_on, period.name, saved_role)
        groups.setdefault(group_key, {
            'watch_period': period.name,
            'role': role_labels[saved_role],
            'items': [],
        })['items'].append({
            'full_name': row.employee.full_name,
            'personnel_number': row.employee.personnel_number or '',
            'arrival_on': _queue_date_label((row.dates_snapshot or {}).get('arrival_on')),
            'departure_on': _queue_date_label((row.dates_snapshot or {}).get('departure_on')),
            'status': (
                'Роль изменена ОУП — требуется проверка'
                if role_changed
                else (
                    'Требуется проверка'
                    if requires_review
                    else 'Ожидает назначения техники и смены'
                )
            ),
            'is_blocked': requires_review,
        })

    ordered_groups = []
    for key in sorted(groups, key=lambda item: (item[0], item[1].casefold(), item[2])):
        group = groups[key]
        group['items'].sort(
            key=lambda item: (
                item['full_name'].casefold(),
                item['personnel_number'].casefold(),
                item['arrival_on'],
                item['departure_on'],
            ),
        )
        ordered_groups.append(group)
    return {
        'groups': ordered_groups,
    }


def _clerk_role_label(role_snapshot):
    """Return a user-facing OUP role label without exposing stored JSON."""
    role_code = (role_snapshot or {}).get('role_code')
    return {
        'driver': 'Водитель',
        'excavator_operator': 'Машинист экскаватора',
        None: 'Непроизводственная роль',
    }.get(role_code, 'Роль требует проверки')


def _clerk_queue_item(*, row, readiness_source, equipment=None, shift_label=None, reason=None):
    """Project one immutable routing row into safe clerk-facing fields only."""
    resident = row.resident
    employee = row.employee
    name = employee.full_name if employee is not None else resident.display_name
    return {
        'name': name,
        'subject_type': 'Сотрудник' if employee is not None else 'Внешний жилец',
        'role': _clerk_role_label(row.role_snapshot),
        'watch_period': row.batch.arrival_roster_version.watch_period.name,
        'arrival_on': _queue_date_label((row.dates_snapshot or {}).get('arrival_on')),
        'departure_on': _queue_date_label((row.dates_snapshot or {}).get('departure_on')),
        'readiness_source': readiness_source,
        'equipment': str(equipment) if equipment is not None else None,
        'shift': shift_label,
        'reason': reason,
        '_sort_key': (
            row.batch.arrival_roster_version.watch_period.starts_on,
            row.batch.arrival_roster_version.watch_period.name.casefold(),
            name.casefold(),
            '0' if employee is not None else '1',
        ),
    }


def _clerk_queue_production_evidence(*, row, event, transfers):
    """Return exact production provenance without changing the T2.6 policy."""
    from assignments.models import (
        AssignmentStatus,
        CrewPlanStatus,
        EquipmentAssignment,
        WorkShiftType,
    )

    saved_role = (row.role_snapshot or {}).get('role_code')
    period = row.batch.arrival_roster_version.watch_period
    current_role, _basis = _qualification_snapshots(
        employee=row.employee,
        transfers=transfers,
        as_of=period.starts_on,
    )
    if (
        saved_role not in _PRODUCTION_ROLE_CODES
        or (row.role_snapshot or {}).get('qualification_state') != 'exact'
        or current_role.get('qualification_state') != 'exact'
        or current_role.get('role_code') != saved_role
    ):
        return _ProductionAssignmentEvidence(
            blocker_code=ERROR_ROUTING_REQUIRES_REVIEW,
            message='Роль изменена ОУП — требуется проверка',
            equipment=None,
            shift_type=None,
        )

    slot = event.crew_plan_slot
    assignment = event.equipment_assignment
    if slot is None or assignment is None:
        return _ProductionAssignmentEvidence(
            blocker_code=ERROR_OFFICIAL_ASSIGNMENT_INCONSISTENT,
            message='Связь назначения техники и смены нарушена.',
            equipment=None,
            shift_type=None,
        )
    plan = slot.plan
    assignment_is_exact = (
        event.actor_access.role.code == 'deputy_mining_manager'
        and plan.status in {CrewPlanStatus.PUBLISHED, CrewPlanStatus.SUPERSEDED}
        and slot.employee_id == row.employee_id
        and assignment.source_crew_plan_slot_id == slot.pk
        and assignment.source_kind == EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN
        and assignment.status == AssignmentStatus.ACCEPTED
        and assignment.ended_at is None
        and assignment.shift_id is None
        and assignment.employee_id == row.employee_id
        and assignment.role_id == plan.role_id
        and assignment.equipment_id == slot.equipment_id
        and assignment.shift_type == slot.shift_type
        and plan.role.code == saved_role
        and slot.shift_type in {WorkShiftType.SHIFT_1, WorkShiftType.SHIFT_2}
    )
    arrival_on = _routing_snapshot_date(row.dates_snapshot, 'arrival_on')
    departure_on = _routing_snapshot_date(row.dates_snapshot, 'departure_on')
    dates_are_exact = bool(
        arrival_on
        and departure_on
        and arrival_on <= plan.work_date <= departure_on
        and period.starts_on <= plan.work_date <= period.ends_on
    )
    if not assignment_is_exact or not dates_are_exact:
        return _ProductionAssignmentEvidence(
            blocker_code=ERROR_OFFICIAL_ASSIGNMENT_INCONSISTENT,
            message='Связь назначения техники и смены нарушена.',
            equipment=None,
            shift_type=None,
        )
    return _ProductionAssignmentEvidence(
        blocker_code=None,
        message=None,
        equipment=assignment.equipment,
        shift_type=slot.shift_type,
    )


def _clerk_queue_production_reason(*, row, event, transfers):
    """Compatibility projection for the existing T2.6 human queue."""
    from assignments.models import WorkShiftType

    evidence = _clerk_queue_production_evidence(
        row=row,
        event=event,
        transfers=transfers,
    )
    shift_label = None
    if evidence.shift_type is not None:
        shift_label = {
            WorkShiftType.SHIFT_1: 'День',
            WorkShiftType.SHIFT_2: 'Ночь',
        }[evidence.shift_type]
    return evidence.message, evidence.equipment, shift_label


def _routing_row_evidence(
    *,
    row,
    latest_event,
    evidence_state,
    blocker_code=None,
    participating=True,
    assignment_shift_type=None,
):
    return ArrivalRosterRoutingRowEvidence(
        routing_row_id=row.pk,
        resident_id=row.resident_id,
        employee_id=row.employee_id,
        route_state=row.route_state,
        participating=participating,
        evidence_state=evidence_state,
        blocker_code=blocker_code,
        latest_event_id=(latest_event.pk if latest_event else None),
        latest_event_type=(latest_event.event_type if latest_event else None),
        crew_plan_slot_id=(latest_event.crew_plan_slot_id if latest_event else None),
        equipment_assignment_id=(
            latest_event.equipment_assignment_id if latest_event else None
        ),
        assignment_shift_type=assignment_shift_type,
    )


def _routing_row_core_is_consistent(*, row, version_id):
    return bool(
        row.row_review.version_id == version_id
        and row.match.version_id == version_id
        and row.row_review.match_id == row.match_id
        and row.row_review.selected_resident_id == row.resident_id
        and row.resident.employee_id == row.employee_id
        and isinstance(row.participation_snapshot, dict)
        and isinstance(row.dates_snapshot, dict)
        and isinstance(row.role_snapshot, dict)
        and isinstance(row.role_basis_snapshot, dict)
    )


def _resolve_routing_row_evidence(*, row, version_id, transfers):
    events = row.routing_events
    latest_event = events[-1] if events else None
    participation = (row.participation_snapshot or {}).get('participation_status')
    participating_statuses = {
        ArrivalRosterRowReview.ParticipationStatus.ARRIVING,
        ArrivalRosterRowReview.ParticipationStatus.EXTENDED,
        ArrivalRosterRowReview.ParticipationStatus.ADDITIONAL,
    }
    participating = participation in participating_statuses

    if not _routing_row_core_is_consistent(row=row, version_id=version_id):
        return _routing_row_evidence(
            row=row,
            latest_event=latest_event,
            participating=participating,
            evidence_state=EVIDENCE_INCONSISTENT,
            blocker_code=ERROR_ROUTING_INCONSISTENT,
        )
    if participation not in {
        *participating_statuses,
        ArrivalRosterRowReview.ParticipationStatus.NOT_ARRIVING,
    }:
        return _routing_row_evidence(
            row=row,
            latest_event=latest_event,
            participating=False,
            evidence_state=EVIDENCE_INCONSISTENT,
            blocker_code=ERROR_ROUTING_INCONSISTENT,
        )

    route_states = {choice for choice, _label in ArrivalRosterRoutingRow.RouteState.choices}
    if row.route_state not in route_states:
        return _routing_row_evidence(
            row=row,
            latest_event=latest_event,
            participating=participating,
            evidence_state=EVIDENCE_INCONSISTENT,
            blocker_code=ERROR_UNKNOWN_ROUTE_STATE,
        )

    if latest_event and latest_event.event_type == ArrivalRosterRoutingEvent.EventType.STALE:
        return _routing_row_evidence(
            row=row,
            latest_event=latest_event,
            participating=participating,
            evidence_state=EVIDENCE_STALE,
            blocker_code=ERROR_ROUTING_STALE,
        )
    if (
        row.route_state == ArrivalRosterRoutingRow.RouteState.REVIEW_REQUIRED
        or latest_event
        and latest_event.event_type == ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW
    ):
        return _routing_row_evidence(
            row=row,
            latest_event=latest_event,
            participating=participating,
            evidence_state=EVIDENCE_REQUIRES_REVIEW,
            blocker_code=ERROR_ROUTING_REQUIRES_REVIEW,
        )

    if participation == ArrivalRosterRowReview.ParticipationStatus.NOT_ARRIVING:
        if row.route_state != ArrivalRosterRoutingRow.RouteState.NOT_PARTICIPATING:
            return _routing_row_evidence(
                row=row,
                latest_event=latest_event,
                participating=False,
                evidence_state=EVIDENCE_INCONSISTENT,
                blocker_code=ERROR_ROUTING_INCONSISTENT,
            )
        return _routing_row_evidence(
            row=row,
            latest_event=latest_event,
            participating=False,
            evidence_state=EVIDENCE_NOT_ARRIVING,
        )

    if row.route_state == ArrivalRosterRoutingRow.RouteState.NOT_PARTICIPATING:
        return _routing_row_evidence(
            row=row,
            latest_event=latest_event,
            evidence_state=EVIDENCE_INCONSISTENT,
            blocker_code=ERROR_ROUTING_INCONSISTENT,
        )

    if row.route_state == ArrivalRosterRoutingRow.RouteState.TO_CLERK:
        is_nonproduction = (
            (row.role_snapshot or {}).get('qualification_state') == 'not_production'
            and (row.role_snapshot or {}).get('role_code') not in _PRODUCTION_ROLE_CODES
        )
        if (
            latest_event
            and latest_event.event_type == ArrivalRosterRoutingEvent.EventType.SENT_TO_CLERK
            and is_nonproduction
        ):
            return _routing_row_evidence(
                row=row,
                latest_event=latest_event,
                evidence_state=EVIDENCE_SENT_TO_CLERK,
            )
        if latest_event is None or latest_event.event_type == ArrivalRosterRoutingEvent.EventType.CREATED:
            return _routing_row_evidence(
                row=row,
                latest_event=latest_event,
                evidence_state=EVIDENCE_PENDING,
                blocker_code=ERROR_ROUTING_PENDING,
            )
        if not is_nonproduction:
            return _routing_row_evidence(
                row=row,
                latest_event=latest_event,
                evidence_state=EVIDENCE_REQUIRES_REVIEW,
                blocker_code=ERROR_ROUTING_REQUIRES_REVIEW,
            )
        return _routing_row_evidence(
            row=row,
            latest_event=latest_event,
            evidence_state=EVIDENCE_INCONSISTENT,
            blocker_code=ERROR_ROUTING_INCONSISTENT,
        )

    if row.route_state != ArrivalRosterRoutingRow.RouteState.TO_DEPUTY or row.employee_id is None:
        return _routing_row_evidence(
            row=row,
            latest_event=latest_event,
            evidence_state=EVIDENCE_INCONSISTENT,
            blocker_code=ERROR_ROUTING_INCONSISTENT,
        )
    if (
        latest_event is None
        or latest_event.event_type in {
            ArrivalRosterRoutingEvent.EventType.CREATED,
            ArrivalRosterRoutingEvent.EventType.SENT_TO_DEPUTY,
        }
    ):
        return _routing_row_evidence(
            row=row,
            latest_event=latest_event,
            evidence_state=EVIDENCE_PENDING,
            blocker_code=ERROR_OFFICIAL_ASSIGNMENT_MISSING,
        )
    if latest_event.event_type != ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED:
        return _routing_row_evidence(
            row=row,
            latest_event=latest_event,
            evidence_state=EVIDENCE_INCONSISTENT,
            blocker_code=ERROR_ROUTING_INCONSISTENT,
        )

    production = _clerk_queue_production_evidence(
        row=row,
        event=latest_event,
        transfers=transfers,
    )
    if production.blocker_code == ERROR_ROUTING_REQUIRES_REVIEW:
        return _routing_row_evidence(
            row=row,
            latest_event=latest_event,
            evidence_state=EVIDENCE_REQUIRES_REVIEW,
            blocker_code=production.blocker_code,
        )
    if production.blocker_code:
        return _routing_row_evidence(
            row=row,
            latest_event=latest_event,
            evidence_state=EVIDENCE_INCONSISTENT,
            blocker_code=production.blocker_code,
        )
    return _routing_row_evidence(
        row=row,
        latest_event=latest_event,
        evidence_state=EVIDENCE_OFFICIAL_ASSIGNMENT_PUBLISHED,
        assignment_shift_type=production.shift_type,
    )


def resolve_arrival_roster_routing_evidence(*, batch_id):
    """Resolve exact immutable routing provenance for one batch, without writes."""
    try:
        batch = (
            ArrivalRosterRoutingBatch._base_manager.select_related(
                'arrival_roster_version__watch_period',
            )
            .filter(pk=batch_id)
            .first()
        )
    except (TypeError, ValueError):
        batch = None
    if batch is None:
        raise _error(ERROR_BATCH_NOT_FOUND, 'Передача реестра не найдена.')

    version = batch.arrival_roster_version
    batch_is_consistent = bool(
        batch.watch_period_id == version.watch_period_id
        and batch.confirmation_sha256 == version.confirmation_sha256
        and version.confirmation_sha256
    )
    confirmed_version_ids = list(
        ArrivalRosterVersion._base_manager.filter(
            watch_period_id=batch.watch_period_id,
            status=ArrivalRosterVersion.Status.CONFIRMED,
        )
        .order_by('pk')
        .values_list('pk', flat=True)
    )
    if not batch_is_consistent:
        batch_state = BATCH_STATE_INCONSISTENT
        batch_blocker_code = ERROR_BATCH_INCONSISTENT
    elif (
        version.status == ArrivalRosterVersion.Status.SUPERSEDED
        or version.superseded_at is not None
        or confirmed_version_ids
        and confirmed_version_ids != [version.pk]
    ):
        batch_state = BATCH_STATE_STALE
        batch_blocker_code = ERROR_BATCH_STALE
    elif (
        version.status == ArrivalRosterVersion.Status.CONFIRMED
        and confirmed_version_ids == [version.pk]
    ):
        batch_state = BATCH_STATE_CURRENT
        batch_blocker_code = None
    else:
        batch_state = BATCH_STATE_INCONSISTENT
        batch_blocker_code = ERROR_BATCH_INCONSISTENT

    rows = list(
        ArrivalRosterRoutingRow._base_manager.select_related(
            'row_review',
            'match',
            'resident',
            'employee',
        )
        .prefetch_related(
            models.Prefetch(
                'events',
                queryset=(
                    ArrivalRosterRoutingEvent._base_manager.select_related(
                        'actor_access__role',
                        'crew_plan_slot__plan__role',
                        'crew_plan_slot__equipment',
                        'equipment_assignment__equipment',
                    ).order_by('created_at', 'pk')
                ),
                to_attr='routing_events',
            ),
        )
        .filter(batch_id=batch.pk)
        .order_by('row_review_id', 'pk')
    )
    transfers = _queue_active_transfers(
        employee_ids=sorted({row.employee_id for row in rows if row.employee_id}),
        as_of=version.watch_period.starts_on,
    )
    resolved_rows = tuple(
        _resolve_routing_row_evidence(
            row=row,
            version_id=version.pk,
            transfers=transfers,
        )
        for row in rows
    )
    return ArrivalRosterRoutingBatchEvidence(
        batch_id=batch.pk,
        version_id=version.pk,
        watch_period_id=batch.watch_period_id,
        batch_state=batch_state,
        batch_blocker_code=batch_blocker_code,
        rows=resolved_rows,
    )


def settlement_clerk_arrival_roster_routing_queue():
    """Return the clerk's consolidated, read-only settlement-ready queue.

    The projection never creates events or repairs damaged provenance.  Every
    route is accepted only when its immutable history still proves readiness.
    """
    rows = list(
        ArrivalRosterRoutingRow._base_manager.select_related(
            'batch__arrival_roster_version__watch_period',
            'employee',
            'resident',
        )
        .prefetch_related(
            models.Prefetch(
                'events',
                queryset=(
                    ArrivalRosterRoutingEvent._base_manager.select_related(
                        'actor_access__role',
                        'crew_plan_slot__plan__role',
                        'crew_plan_slot__equipment',
                        'equipment_assignment__equipment',
                    ).order_by('created_at', 'pk')
                ),
                to_attr='routing_events',
            ),
        )
        .filter(
            batch__arrival_roster_version__status=ArrivalRosterVersion.Status.CONFIRMED,
            batch__arrival_roster_version__superseded_at__isnull=True,
        )
        .order_by(
            'batch__arrival_roster_version__watch_period__starts_on',
            'batch__arrival_roster_version__watch_period__name',
            'employee__full_name',
            'resident__full_name',
        )
    )
    employee_ids = sorted({row.employee_id for row in rows if row.employee_id})
    transfers_by_period = {}
    ready = []
    review = []

    for row in rows:
        participation = (row.participation_snapshot or {}).get('participation_status')
        if participation == ArrivalRosterRowReview.ParticipationStatus.NOT_ARRIVING:
            continue
        events = row.routing_events
        latest_event = events[-1] if events else None
        if latest_event is None:
            review.append(_clerk_queue_item(
                row=row,
                readiness_source='',
                reason='История передачи неполная — требуется проверка.',
            ))
            continue

        if latest_event.event_type in {
            ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW,
            ArrivalRosterRoutingEvent.EventType.STALE,
        } or row.route_state == ArrivalRosterRoutingRow.RouteState.REVIEW_REQUIRED:
            review.append(_clerk_queue_item(
                row=row,
                readiness_source='',
                reason=(
                    'Передача устарела: создана исправленная версия.'
                    if latest_event.event_type == ArrivalRosterRoutingEvent.EventType.STALE
                    else 'Требуется проверка исходных данных.'
                ),
            ))
            continue

        if row.route_state == ArrivalRosterRoutingRow.RouteState.TO_CLERK:
            is_nonproduction = (
                (row.role_snapshot or {}).get('qualification_state') == 'not_production'
                and (row.role_snapshot or {}).get('role_code') not in _PRODUCTION_ROLE_CODES
            )
            if (
                latest_event.event_type == ArrivalRosterRoutingEvent.EventType.SENT_TO_CLERK
                and is_nonproduction
            ):
                ready.append(_clerk_queue_item(
                    row=row,
                    readiness_source='Направлен непосредственно',
                ))
            else:
                review.append(_clerk_queue_item(
                    row=row,
                    readiness_source='',
                    reason=(
                        'Роль ОУП требует проверки.'
                        if not is_nonproduction
                        else 'История прямого направления нарушена.'
                    ),
                ))
            continue

        if row.route_state != ArrivalRosterRoutingRow.RouteState.TO_DEPUTY or row.employee_id is None:
            continue
        if latest_event.event_type != ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED:
            # The deputy owns rows that have not yet received an official event.
            continue

        period = row.batch.arrival_roster_version.watch_period
        if period.pk not in transfers_by_period:
            transfers_by_period[period.pk] = _queue_active_transfers(
                employee_ids=employee_ids,
                as_of=period.starts_on,
            )
        reason, equipment, shift_label = _clerk_queue_production_reason(
            row=row,
            event=latest_event,
            transfers=transfers_by_period[period.pk],
        )
        if reason:
            review.append(_clerk_queue_item(
                row=row,
                readiness_source='',
                reason=reason,
            ))
        else:
            ready.append(_clerk_queue_item(
                row=row,
                readiness_source='Техника и смена назначены',
                equipment=equipment,
                shift_label=shift_label,
            ))

    ready.sort(key=lambda item: item['_sort_key'])
    review.sort(key=lambda item: item['_sort_key'])
    for item in [*ready, *review]:
        item.pop('_sort_key')
    return {'ready': ready, 'review': review}


def _routing_snapshot_date(snapshot, field_name):
    try:
        return date.fromisoformat((snapshot or {}).get(field_name))
    except (TypeError, ValueError):
        return None


def _routing_event_exists(*, row, event_type, crew_plan_slot=None, equipment_assignment=None):
    return ArrivalRosterRoutingEvent._base_manager.select_for_update(of=('self',)).filter(
        routing_row=row,
        event_type=event_type,
        crew_plan_slot=crew_plan_slot,
        equipment_assignment=equipment_assignment,
    ).exists()


def _trusted_insert_routing_events(events):
    if not events:
        return
    for event in events:
        event.full_clean()
    models.QuerySet.bulk_create(ArrivalRosterRoutingEvent._base_manager.all(), events)


@transaction.atomic
def _record_crew_plan_role_review(*, plan, actor_access):
    """Persist the controlled OUP-role conflict after a draft publication rolls back."""
    from assignments.models import CrewPlanSlot

    plan_id = getattr(plan, 'pk', plan)
    slots = list(
        CrewPlanSlot.objects.select_for_update(of=('self',))
        .filter(plan_id=plan_id, employee__isnull=False)
        .select_related('employee', 'plan__role')
        .order_by('employee_id', 'equipment_id', 'shift_type')
    )
    if not slots:
        return
    employee_ids = sorted({slot.employee_id for slot in slots})
    rows_by_employee = defaultdict(list)
    for row in (
        ArrivalRosterRoutingRow._base_manager.select_for_update(
            of=(
                'self',
                'batch',
                'batch__arrival_roster_version',
                'batch__arrival_roster_version__watch_period',
            ),
        )
        .select_related('batch__arrival_roster_version__watch_period', 'employee')
        .filter(
            employee_id__in=employee_ids,
            route_state=ArrivalRosterRoutingRow.RouteState.TO_DEPUTY,
            batch__arrival_roster_version__status=ArrivalRosterVersion.Status.CONFIRMED,
            batch__arrival_roster_version__superseded_at__isnull=True,
        )
        .order_by('employee_id', 'batch__arrival_roster_version__watch_period_id', 'pk')
    ):
        rows_by_employee[row.employee_id].append(row)

    transfers_by_period = {}
    events = []
    for slot in slots:
        for row in rows_by_employee.get(slot.employee_id, []):
            period = row.batch.arrival_roster_version.watch_period
            if period.pk not in transfers_by_period:
                transfers_by_period[period.pk] = _locked_active_transfers(
                    employee_ids=employee_ids,
                    as_of=period.starts_on,
                )
            current_role, _basis = _qualification_snapshots(
                employee=row.employee,
                transfers=transfers_by_period[period.pk],
                as_of=period.starts_on,
            )
            saved_role = (row.role_snapshot or {}).get('role_code')
            if (
                saved_role != slot.plan.role.code
                or (row.role_snapshot or {}).get('qualification_state') != 'exact'
                or current_role.get('qualification_state') != 'exact'
                or current_role.get('role_code') != saved_role
            ):
                if not _routing_event_exists(
                    row=row,
                    event_type=ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW,
                ):
                    events.append(ArrivalRosterRoutingEvent(
                        routing_row=row,
                        event_type=ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW,
                        actor_access=actor_access,
                    ))
    _trusted_insert_routing_events(events)


def _record_published_crew_plan_routing(*, plan, slots, actor_access):
    """Append exact routing evidence after one deputy CrewPlan publication.

    This is a trusted in-transaction hook.  Its caller has already locked and
    verified the precise deputy EmployeeAccess before it locked the CrewPlan.
    """
    from assignments.models import AssignmentStatus, EquipmentAssignment

    if not actor_access or actor_access.employee_id != getattr(plan, 'published_by_id', None):
        raise _error(
            'arrival_roster.routing_deputy_access_required',
            'Для фиксации назначения требуется точный доступ заместителя.',
        )

    assigned_slots = [slot for slot in slots if slot.employee_id]
    if not assigned_slots:
        return
    assignments_by_slot = defaultdict(list)
    for assignment in (
        EquipmentAssignment._base_manager.select_for_update(of=('self',))
        .filter(source_crew_plan_slot_id__in=[slot.pk for slot in assigned_slots])
        .select_related('source_crew_plan_slot')
        .order_by('source_crew_plan_slot_id', 'pk')
    ):
        assignments_by_slot[assignment.source_crew_plan_slot_id].append(assignment)

    employee_ids = sorted({slot.employee_id for slot in assigned_slots})
    rows_by_employee = defaultdict(list)
    for row in (
        ArrivalRosterRoutingRow._base_manager.select_for_update(
            of=(
                'self',
                'batch',
                'batch__arrival_roster_version',
                'batch__arrival_roster_version__watch_period',
            ),
        )
        .select_related(
            'batch__arrival_roster_version__watch_period', 'employee',
        )
        .filter(
            employee_id__in=employee_ids,
            route_state=ArrivalRosterRoutingRow.RouteState.TO_DEPUTY,
            batch__arrival_roster_version__status=ArrivalRosterVersion.Status.CONFIRMED,
            batch__arrival_roster_version__superseded_at__isnull=True,
        )
        .order_by('employee_id', 'batch__arrival_roster_version__watch_period_id', 'pk')
    ):
        rows_by_employee[row.employee_id].append(row)

    transfers_by_period = {}
    events = []
    for slot in assigned_slots:
        rows = rows_by_employee.get(slot.employee_id, [])
        if not rows:
            continue
        exact_rows = []
        mismatch_rows = []
        assignments = assignments_by_slot.get(slot.pk, [])
        assignment = assignments[0] if len(assignments) == 1 else None
        assignment_is_exact = bool(
            assignment
            and assignment.status == AssignmentStatus.ACCEPTED
            and assignment.ended_at is None
            and assignment.shift_id is None
            and assignment.source_kind == EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN
            and assignment.employee_id == slot.employee_id
            and assignment.role_id == plan.role_id
            and assignment.equipment_id == slot.equipment_id
            and assignment.shift_type == slot.shift_type
        )
        for row in rows:
            period = row.batch.arrival_roster_version.watch_period
            if period.pk not in transfers_by_period:
                transfers_by_period[period.pk] = _locked_active_transfers(
                    employee_ids=[
                        routed_row.employee_id
                        for row_group in rows_by_employee.values()
                        for routed_row in row_group
                    ],
                    as_of=period.starts_on,
                )
            current_role, _basis = _qualification_snapshots(
                employee=row.employee,
                transfers=transfers_by_period[period.pk],
                as_of=period.starts_on,
            )
            participation = (row.participation_snapshot or {}).get('participation_status')
            arrival_on = _routing_snapshot_date(row.dates_snapshot, 'arrival_on')
            departure_on = _routing_snapshot_date(row.dates_snapshot, 'departure_on')
            role_is_exact = (
                (row.role_snapshot or {}).get('qualification_state') == 'exact'
                and (row.role_snapshot or {}).get('role_code') == plan.role.code
                and current_role.get('qualification_state') == 'exact'
                and current_role.get('role_code') == plan.role.code
            )
            dates_are_exact = bool(
                participation != ArrivalRosterRowReview.ParticipationStatus.NOT_ARRIVING
                and arrival_on
                and departure_on
                and arrival_on <= plan.work_date <= departure_on
                and period.starts_on <= plan.work_date <= period.ends_on
            )
            if role_is_exact and dates_are_exact:
                exact_rows.append(row)
            else:
                mismatch_rows.append(row)

        if len(exact_rows) == 1 and not mismatch_rows and assignment_is_exact:
            row = exact_rows[0]
            if not _routing_event_exists(
                row=row,
                event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
                crew_plan_slot=slot,
                equipment_assignment=assignment,
            ):
                events.append(ArrivalRosterRoutingEvent(
                    routing_row=row,
                    event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
                    actor_access=actor_access,
                    crew_plan_slot=slot,
                    equipment_assignment=assignment,
                ))
            continue

        for row in [*exact_rows, *mismatch_rows]:
            if not _routing_event_exists(
                row=row,
                event_type=ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW,
            ):
                events.append(ArrivalRosterRoutingEvent(
                    routing_row=row,
                    event_type=ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW,
                    actor_access=actor_access,
                ))
    _trusted_insert_routing_events(events)


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
