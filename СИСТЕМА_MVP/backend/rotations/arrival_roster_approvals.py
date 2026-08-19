"""Fail-closed final approval of an arrival-roster version."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from settlement.models import SettlementResident
from shifts.models import WatchPeriod
from users.models import Employee

from .arrival_roster_pool import _employee_snapshot, _resident_snapshot
from .arrival_rosters import (
    _access_snapshot,
    _canonical_sha256,
    _lock_employee_plan,
    _lock_timekeeper_access,
    _trusted_confirm_arrival_roster_version,
    _trusted_create_arrival_roster_event,
    _trusted_supersede_arrival_roster_version,
    _verified_timekeeper_access,
    arrival_roster_issue_policy,
)
from .models import (
    ArrivalRosterEvent,
    ArrivalRosterIssue,
    ArrivalRosterIssueResolution,
    ArrivalRosterMatch,
    ArrivalRosterPoolRow,
    ArrivalRosterRowReview,
    ArrivalRosterVersion,
)


def _error(code, message):
    return ValidationError(message, code=code)


def _preflight(version_id, actor_access_id):
    access = _access_snapshot(actor_access_id)
    version = ArrivalRosterVersion.objects.filter(pk=version_id).values(
        'pk', 'watch_period_id', 'status', 'based_on_version_id',
    ).first()
    if version is None:
        raise _error('arrival_roster.version_required', 'Версия реестра не найдена.')
    employee_ids = set(
        ArrivalRosterPoolRow.objects.filter(version_id=version_id)
        .exclude(employee_id=None).values_list('employee_id', flat=True)
    )
    employee_ids.update(
        ArrivalRosterRowReview._base_manager.filter(version_id=version_id)
        .exclude(selected_resident__employee_id=None)
        .values_list('selected_resident__employee_id', flat=True)
    )
    employee_ids.add(access['employee_id'])
    resident_ids = list(
        ArrivalRosterRowReview._base_manager.filter(version_id=version_id)
        .exclude(selected_resident_id=None).order_by('selected_resident_id')
        .values_list('selected_resident_id', flat=True)
    )
    version_ids = list(
        ArrivalRosterVersion.objects.filter(watch_period_id=version['watch_period_id'])
        .order_by('pk').values_list('pk', flat=True)
    )
    return access, sorted(employee_ids), sorted(set(resident_ids)), version_ids


def _employee_confirmation_snapshot(employee):
    return {
        'employee_id': employee.pk,
        'status': employee.status,
        'is_active': employee.is_active,
        'hired_at': employee.hired_at.isoformat() if employee.hired_at else None,
        'dismissed_at': employee.dismissed_at.isoformat() if employee.dismissed_at else None,
        'watch_composition_id': employee.watch_composition_id,
        'updated_at': employee.updated_at.isoformat() if employee.updated_at else None,
    }


def _canonical_payload(*, version, period, actor_access, employees, residents,
                       matches, reviews, issues, resolutions):
    employee_map = {row.pk: row for row in employees}
    resident_map = {row.pk: row for row in residents}
    resolution_map = {row.issue_id: row for row in resolutions}
    pool_rows = list(
        ArrivalRosterPoolRow._base_manager.filter(version=version).order_by('pk')
    )
    pool_by_employee = {row.employee_id: row for row in pool_rows if row.employee_id}
    pool_by_match = {row.match_id: row for row in pool_rows}
    if not matches or len(reviews) != len(matches):
        raise _error('arrival_roster.empty_or_incomplete', 'Версия пуста или содержит непроверенные строки.')
    if (
        not period.is_active or period.watch_composition_id is None
        or not period.watch_composition.is_active
    ):
        raise _error('arrival_roster.watch_period_changed', 'Период или состав вахты больше не действует.')

    seen_employees = set()
    seen_residents = set()
    review_payload = []
    deferred_error = None
    for review in reviews:
        resident = resident_map.get(review.selected_resident_id)
        if review.resident_resolution != ArrivalRosterRowReview.ResidentResolution.SELECTED or resident is None:
            raise _error('arrival_roster.resident_required', 'Для каждой строки требуется точный активный жилец.')
        if resident.status != SettlementResident.Status.ACTIVE and deferred_error is None:
            deferred_error = _error('arrival_roster.resident_inactive', 'Выбран неактивный жилец.')
        if resident.pk in seen_residents:
            raise _error('arrival_roster.duplicate_resident', 'Жилец повторяется в версии.')
        seen_residents.add(resident.pk)
        employee = employee_map.get(resident.employee_id)
        if resident.resident_type == SettlementResident.ResidentType.EMPLOYEE:
            if employee is None or employee.pk != resident.employee_id:
                raise _error('arrival_roster.employee_resident_mismatch', 'Внутренний жилец не соответствует Employee.')
            if employee.pk in seen_employees:
                raise _error('arrival_roster.duplicate_employee', 'Сотрудник повторяется в версии.')
            seen_employees.add(employee.pk)
            pool_row_for_match = pool_by_match.get(review.match_id)
            if (
                pool_row_for_match is not None
                and pool_row_for_match.employee_id != employee.pk
                and deferred_error is None
            ):
                deferred_error = _error(
                    'arrival_roster.employee_resident_mismatch',
                    'Внутренний жилец не соответствует Employee строки.',
                )
            if (employee.status != Employee.Status.ACTIVE or not employee.is_active) and deferred_error is None:
                deferred_error = _error('arrival_roster.employee_inactive', 'Участник уволен или неактивен.')
            if (employee.hired_at is None or employee.hired_at > period.starts_on) and deferred_error is None:
                deferred_error = _error('arrival_roster.employee_dates_invalid', 'Кадровые даты участника неполны или изменились.')
            if employee.dismissed_at and employee.dismissed_at < period.starts_on and deferred_error is None:
                deferred_error = _error('arrival_roster.employee_dismissed', 'Участник уволен до начала периода.')
            pool_row = pool_by_employee.get(employee.pk)
            manually_added = bool(
                pool_row and pool_row.origin_kind == ArrivalRosterPoolRow.OriginKind.MANUAL_EMPLOYEE
            )
            if employee.watch_composition_id != period.watch_composition_id and not manually_added and deferred_error is None:
                deferred_error = _error('arrival_roster.composition_changed', 'Состав вахты участника изменился.')
        if not review.participation_status:
            raise _error('arrival_roster.participation_required', 'Не указано участие в заезде.')
        if review.participation_status != ArrivalRosterRowReview.ParticipationStatus.NOT_ARRIVING:
            if not review.arrival_on or not review.departure_on:
                raise _error('arrival_roster.dates_required', 'Для участника обязательны даты.')
            if review.departure_on < review.arrival_on:
                raise _error('arrival_roster.dates_invalid', 'Интервал дат некорректен.')
        if review.participation_status == ArrivalRosterRowReview.ParticipationStatus.ARRIVING and not review.arrival_mode:
            raise _error('arrival_roster.arrival_mode_required', 'Не указан способ прибытия.')
        review_payload.append({
            'match_id': review.match_id,
            'resident_id': resident.pk,
            'resident_revision': resident.revision,
            'resident_status': resident.status,
            'resident_type': resident.resident_type,
            'employee_id': resident.employee_id,
            'employee': _employee_confirmation_snapshot(employee) if employee else None,
            'participation': review.participation_status,
            'arrival_mode': review.arrival_mode,
            'arrival_on': review.arrival_on.isoformat() if review.arrival_on else None,
            'departure_on': review.departure_on.isoformat() if review.departure_on else None,
            'review_revision': review.revision,
            'decided_by_access_id': review.updated_by_access_id,
        })

    for row in pool_rows:
        if row.employee_id:
            employee = employee_map.get(row.employee_id)
            if employee is None or row.employee_snapshot != _employee_snapshot(employee):
                if deferred_error is None:
                    deferred_error = _error('arrival_roster.employee_source_changed', 'Кадровый исходный снимок изменился.')
            if employee is not None and row.watch_composition_id != employee.watch_composition_id and deferred_error is None:
                deferred_error = _error('arrival_roster.composition_changed', 'Принадлежность к составу вахты изменилась.')
        if row.resident_id:
            resident = resident_map.get(row.resident_id)
            if resident is None or row.resident_snapshot != _resident_snapshot(resident):
                if deferred_error is None:
                    deferred_error = _error('arrival_roster.resident_source_changed', 'Исходный снимок жильца изменился.')

    issue_payload = []
    for issue in issues:
        resolution = resolution_map.get(issue.pk)
        policy = arrival_roster_issue_policy(issue.code, issue.severity)
        blocking = policy['blocks_confirmation']
        if blocking:
            raise _error('arrival_roster.blocking_issue', f'Утверждение блокирует вопрос: {issue.code}.')
        issue_payload.append({
            'issue_id': issue.pk, 'code': issue.code, 'severity': issue.severity,
            'responsible_role': policy['role'],
            'resolution_revision': resolution.revision if resolution else None,
            'resolved': bool(resolution and resolution.is_resolved),
            'decided_by_access_id': resolution.updated_by_access_id if resolution else None,
        })

    actual_source_snapshot_sha256 = _canonical_sha256(version.snapshot)
    if version.snapshot_sha256 != actual_source_snapshot_sha256 and deferred_error is None:
        deferred_error = _error('arrival_roster.source_snapshot_changed', 'Исходный снимок версии изменён.')
    payload = {
        'schema': 1,
        'version_id': version.pk,
        'watch_period': {
            'id': period.pk, 'watch_composition_id': period.watch_composition_id,
            'starts_on': period.starts_on.isoformat(), 'ends_on': period.ends_on.isoformat(),
        },
        'source': {
            'kind': version.source_kind, 'fingerprint': version.source_fingerprint,
            'snapshot_sha256': actual_source_snapshot_sha256,
            'source_file_sha256': version.source_file.sha256 if version.source_file_id else None,
            'parser_profile_sha256': version.parser_profile.configuration_sha256 if version.parser_profile_id else None,
        },
        'rows': review_payload,
        'issues': issue_payload,
        'confirmed_by_access_id': actor_access.pk,
    }
    return payload, deferred_error


def _locked_proposal(*, version_id, actor_access_id):
    access_plan, employee_ids, resident_ids, version_ids = _preflight(version_id, actor_access_id)
    employees = list(_lock_employee_plan(employee_ids).values())
    actor_context = _lock_timekeeper_access(access_plan, locked_employees={row.pk: row for row in employees})
    actor_access = _verified_timekeeper_access(actor_context)
    version_plan = ArrivalRosterVersion.objects.get(pk=version_id)
    period = (
        WatchPeriod.objects.select_for_update(of=('self',))
        .select_related('watch_composition').get(pk=version_plan.watch_period_id)
    )
    versions = list(ArrivalRosterVersion._base_manager.select_for_update(of=('self',)).filter(pk__in=version_ids).order_by('pk'))
    if [row.pk for row in versions] != version_ids:
        raise _error('arrival_roster.version_plan_changed', 'Набор версий периода изменился.')
    version = next(row for row in versions if row.pk == version_id)
    if version.based_on_version_id:
        base = next((row for row in versions if row.pk == version.based_on_version_id), None)
        if base is None or base.watch_period_id != version.watch_period_id:
            raise _error('arrival_roster.replacement_base_invalid', 'Базовая версия относится к другому периоду.')
    residents = list(SettlementResident.objects.select_for_update(of=('self',)).filter(pk__in=resident_ids).order_by('pk'))
    matches = list(ArrivalRosterMatch._base_manager.select_for_update(of=('self',)).filter(version=version).order_by('pk'))
    reviews = list(ArrivalRosterRowReview._base_manager.select_for_update(of=('self',)).filter(version=version).order_by('match_id'))
    issues = list(ArrivalRosterIssue._base_manager.select_for_update(of=('self',)).filter(version=version).order_by('pk'))
    resolutions = list(ArrivalRosterIssueResolution._base_manager.select_for_update(of=('self',)).filter(issue__version=version).order_by('issue_id'))
    payload, deferred_error = _canonical_payload(
        version=version, period=period, actor_access=actor_access, employees=employees,
        residents=residents, matches=matches, reviews=reviews, issues=issues,
        resolutions=resolutions,
    )
    return version, versions, actor_context, payload, _canonical_sha256(payload), deferred_error


@transaction.atomic
def build_arrival_roster_confirmation_proposal(*, version_id, actor_access_id):
    version, _versions, _context, snapshot, sha256, deferred_error = _locked_proposal(
        version_id=version_id, actor_access_id=actor_access_id,
    )
    if version.status == ArrivalRosterVersion.Status.SUPERSEDED:
        raise _error('arrival_roster.superseded', 'Заменённую версию утверждать нельзя.')
    if deferred_error:
        raise deferred_error
    return {'version_id': version.pk, 'confirmation_snapshot': snapshot, 'confirmation_sha256': sha256}


def _confirm_arrival_roster_version_once(*, version_id, expected_sha256,
                                         actor_access_id):
    version, versions, actor_context, snapshot, sha256, deferred_error = _locked_proposal(
        version_id=version_id, actor_access_id=actor_access_id,
    )
    if version.status == ArrivalRosterVersion.Status.SUPERSEDED:
        raise _error('arrival_roster.superseded', 'Заменённую версию утверждать нельзя.')
    if expected_sha256 != sha256:
        raise _error('arrival_roster.stale_confirmation_sha256', 'Предложение утверждения устарело.')
    if deferred_error:
        raise deferred_error
    if version.status == ArrivalRosterVersion.Status.CONFIRMED:
        if version.confirmation_sha256 != sha256:
            raise _error('arrival_roster.confirmed_snapshot_changed', 'Утверждённый снимок не совпадает.')
        return version
    current = next((row for row in versions if row.status == ArrivalRosterVersion.Status.CONFIRMED), None)
    if current:
        if version.based_on_version_id != current.pk:
            raise _error('arrival_roster.replacement_lineage_required', 'Замена должна основываться на действующей утверждённой версии.')
    elif version.based_on_version_id:
        raise _error('arrival_roster.replacement_base_invalid', 'Базовая версия больше не является действующей.')
    now = timezone.now()
    if current:
        _trusted_supersede_arrival_roster_version(
            version_id=current.pk, superseded_at=now,
        )
    version = _trusted_confirm_arrival_roster_version(
        version_id=version.pk,
        actor_access_id=actor_context.access_id,
        confirmation_snapshot=snapshot,
        confirmation_sha256=sha256,
        confirmed_at=now,
    )
    _trusted_create_arrival_roster_event(
        version=version, actor_context=actor_context,
        action=ArrivalRosterEvent.Action.CONFIRMED,
        details={'confirmation_sha256': sha256},
    )
    return version


def _is_confirmed_period_collision(error):
    cause = getattr(error, '__cause__', None)
    constraint_name = getattr(getattr(cause, 'diag', None), 'constraint_name', None)
    if constraint_name == 'uniq_arrival_confirmed_period':
        return True
    message = str(cause or error)
    return (
        'UNIQUE constraint failed' in message
        and 'rotations_arrivalrosterversion.watch_period_id' in message
    )


def confirm_arrival_roster_version(*, version_id, expected_sha256, actor_access_id):
    try:
        with transaction.atomic():
            return _confirm_arrival_roster_version_once(
                version_id=version_id,
                expected_sha256=expected_sha256,
                actor_access_id=actor_access_id,
            )
    except IntegrityError as error:
        if _is_confirmed_period_collision(error):
            raise _error(
                'arrival_roster.confirmed_period_conflict',
                'Период уже получил другую утверждённую версию.',
            ) from error
        raise


__all__ = ['build_arrival_roster_confirmation_proposal', 'confirm_arrival_roster_version']
