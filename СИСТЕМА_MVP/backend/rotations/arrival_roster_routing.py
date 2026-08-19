"""Atomic, server-owned hand-off of a confirmed arrival roster."""

from collections import defaultdict
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
