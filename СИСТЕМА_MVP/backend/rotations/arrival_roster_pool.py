from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from settlement.models import SettlementResident
from shifts.models import WatchPeriod
from users.models import Employee

from .arrival_rosters import (
    _access_snapshot,
    _canonical_sha256,
    _lock_employee_plan,
    _lock_timekeeper_access,
    _lock_version,
    _require_reviewable_version,
    _trusted_create_arrival_roster_issue,
    _trusted_create_arrival_roster_match,
    _trusted_create_arrival_roster_event,
    _trusted_write_review,
    _verified_timekeeper_access,
)
from .models import (
    ArrivalRosterEvent,
    ArrivalRosterIssue,
    ArrivalRosterMatch,
    ArrivalRosterPoolRow,
    ArrivalRosterRowReview,
    ArrivalRosterVersion,
)


def _validation_error(code, message):
    return ValidationError(message, code=code)


@dataclass(frozen=True, slots=True)
class ArrivalRosterBulkConfirmationResult:
    changed: int
    already_confirmed: int
    skipped: int

    def __bool__(self):
        return self.changed > 0

    def __str__(self):
        return str(self.changed)


def _employee_snapshot(employee):
    return {
        'employee_id': employee.pk,
        'full_name': employee.full_name,
        'personnel_position_id': employee.personnel_position_id,
        'position': employee.position,
        'status': employee.status,
        'is_active': employee.is_active,
        'hired_at': employee.hired_at.isoformat() if employee.hired_at else None,
        'dismissed_at': employee.dismissed_at.isoformat() if employee.dismissed_at else None,
        'watch_composition_id': employee.watch_composition_id,
        'updated_at': employee.updated_at.isoformat() if employee.updated_at else None,
    }


def _resident_snapshot(resident):
    return {
        'resident_id': resident.pk,
        'stable_id': str(resident.stable_id),
        'resident_type': resident.resident_type,
        'status': resident.status,
        'revision': resident.revision,
        'employee_id': resident.employee_id,
    }


def _pool_row_payload(*, version, resident, employee, watch_composition,
                      origin_kind, suggested_participation,
                      employee_snapshot, resident_snapshot, basis):
    return {
        'version_id': version.pk,
        'resident_id': resident.pk if resident else None,
        'employee_id': employee.pk if employee else None,
        'watch_composition_id': watch_composition.pk if watch_composition else None,
        'origin_kind': origin_kind,
        'suggested_participation': suggested_participation,
        'employee_snapshot': employee_snapshot,
        'resident_snapshot': resident_snapshot,
        'basis': basis,
    }


def _trusted_create_pool_row(*, version, match, resident, employee,
                             watch_composition, origin_kind,
                             suggested_participation, basis, actor_context):
    actor_access = _verified_timekeeper_access(actor_context)
    version_record = (
        ArrivalRosterVersion._base_manager
        .filter(pk=version.pk)
        .values('source_kind', 'status', 'watch_period_id')
        .first()
    )
    match_record = (
        ArrivalRosterMatch._base_manager
        .filter(pk=match.pk)
        .values(
            'version_id', 'status', 'method', 'quality',
            'matched_resident_id',
        )
        .first()
    )
    if (
        version_record is None
        or version_record['source_kind'] != ArrivalRosterVersion.SourceKind.EMPLOYEE_POOL
        or version_record['status'] not in {
            ArrivalRosterVersion.Status.DRAFT,
            ArrivalRosterVersion.Status.REVIEW_REQUIRED,
        }
        or match.version_id != version.pk
        or match_record is None
        or match_record != {
            'version_id': match.version_id,
            'status': match.status,
            'method': match.method,
            'quality': match.quality,
            'matched_resident_id': match.matched_resident_id,
        }
    ):
        raise _validation_error(
            'arrival_roster.pool_context_mismatch',
            'Строка пула не соответствует версии реестра.',
        )
    employee_snapshot = _employee_snapshot(employee) if employee is not None else {}
    resident_snapshot = _resident_snapshot(resident) if resident is not None else {}
    if employee is not None:
        current_employee = Employee.objects.select_related('watch_composition').get(pk=employee.pk)
        if _employee_snapshot(current_employee) != employee_snapshot:
            raise _validation_error(
                'arrival_roster.pool_snapshot_mismatch',
                'Кадровые данные изменились во время формирования строки.',
            )
        if watch_composition is not None and current_employee.watch_composition_id != watch_composition.pk:
            raise _validation_error(
                'arrival_roster.pool_watch_composition_mismatch',
                'Принадлежность сотрудника к вахте изменилась.',
            )
    if resident is not None:
        current_resident = SettlementResident.objects.get(pk=resident.pk)
        if _resident_snapshot(current_resident) != resident_snapshot:
            raise _validation_error(
                'arrival_roster.pool_snapshot_mismatch',
                'Карточка жильца изменилась во время формирования строки.',
            )
    payload = _pool_row_payload(
        version=version,
        resident=resident,
        employee=employee,
        watch_composition=watch_composition,
        origin_kind=origin_kind,
        suggested_participation=suggested_participation,
        employee_snapshot=employee_snapshot,
        resident_snapshot=resident_snapshot,
        basis=basis,
    )
    row = ArrivalRosterPoolRow(
        version=version,
        resident=resident,
        employee=employee,
        watch_composition=watch_composition,
        match=match,
        origin_kind=origin_kind,
        suggested_participation=suggested_participation,
        employee_snapshot=employee_snapshot,
        resident_snapshot=resident_snapshot,
        snapshot_sha256=_canonical_sha256(payload),
        created_by_access=actor_access,
        basis=basis,
    )
    row.full_clean()
    ArrivalRosterPoolRow._base_manager.bulk_create([row])
    return row


def _trusted_create_match_and_review(*, version, actor_context, resident,
                                     method, evidence, participation=None):
    actor_access = _verified_timekeeper_access(actor_context)
    exact = resident is not None
    match = _trusted_create_arrival_roster_match(
        version=version,
        status=(ArrivalRosterMatch.Status.EXACT if exact else ArrivalRosterMatch.Status.UNMATCHED),
        method=method,
        quality='exact' if exact else 'unmatched',
        matched_resident=resident,
        evidence=evidence,
    )
    review = ArrivalRosterRowReview(
        version=version,
        match=match,
        resident_resolution=(
            ArrivalRosterRowReview.ResidentResolution.SELECTED
            if exact
            else ArrivalRosterRowReview.ResidentResolution.UNREVIEWED
        ),
        selected_resident=resident,
        participation_status=participation,
        revision=1,
        updated_by_access=actor_access,
    )
    _trusted_write_review(review=review, creating=True)
    return match, review


def _issue_specs_for_employee(employee, period, *, composition_required=True):
    issues = []
    if employee.status != Employee.Status.ACTIVE or not employee.is_active:
        issues.append((
            'employee_inactive',
            'Карточка сотрудника не является действующей.',
        ))
    if employee.hired_at is None:
        issues.append((
            'employee_hire_date_missing',
            'Для сотрудника не указана дата приёма.',
        ))
    elif employee.hired_at > period.starts_on:
        issues.append((
            'employee_hired_after_period_start',
            'Сотрудник принят после начала выбранного периода.',
        ))
    if employee.dismissed_at is not None and employee.dismissed_at < period.starts_on:
        issues.append((
            'employee_dismissed_before_period',
            'Сотрудник уволен до начала выбранного периода.',
        ))
    if composition_required:
        if employee.watch_composition_id is None:
            issues.append((
                'employee_watch_composition_missing',
                'Для сотрудника не указана официальная принадлежность к вахте.',
            ))
        elif employee.watch_composition_id != period.watch_composition_id:
            issues.append((
                'employee_watch_composition_mismatch',
                'Принадлежность сотрудника не соответствует выбранному периоду.',
            ))
    return issues


def _resident_plan(employee_ids):
    return list(
        SettlementResident.objects
        .filter(employee_id__in=sorted(employee_ids))
        .order_by('pk')
        .values_list('pk', 'employee_id')
    )


def _lock_resident_plan(planned_pairs, *, employee_ids):
    resident_ids = [resident_id for resident_id, _employee_id in planned_pairs]
    residents = list(
        SettlementResident.objects
        .select_for_update(of=('self',))
        .filter(pk__in=resident_ids)
        .order_by('pk')
    )
    actual_pairs = [(resident.pk, resident.employee_id) for resident in residents]
    current_pairs = list(
        SettlementResident.objects
        .filter(employee_id__in=sorted(employee_ids))
        .order_by('pk')
        .values_list('pk', 'employee_id')
    )
    if actual_pairs != planned_pairs or current_pairs != planned_pairs:
        raise _validation_error(
            'arrival_roster.resident_plan_changed',
            'Карточки жильцов изменились. Повторите операцию.',
        )
    by_employee = {}
    for resident in residents:
        by_employee.setdefault(resident.employee_id, []).append(resident)
    return by_employee


def _resident_for_employee(employee, residents):
    if len(residents) != 1:
        return None, (
            'employee_resident_missing' if not residents else 'employee_resident_ambiguous',
            'Для сотрудника не найдена единственная карточка внутреннего жильца.',
        )
    resident = residents[0]
    if (
        resident.resident_type != SettlementResident.ResidentType.EMPLOYEE
        or resident.employee_id != employee.pk
        or resident.status != SettlementResident.Status.ACTIVE
    ):
        return None, (
            'employee_resident_unavailable',
            'Карточка внутреннего жильца неактивна или противоречит сотруднику.',
        )
    return resident, None


def _create_blocking_issue(*, version, match, code, message, employee_id):
    return _trusted_create_arrival_roster_issue(
        version=version,
        match=match,
        severity=ArrivalRosterIssue.Severity.ERROR,
        code=code,
        message=message,
        details={'employee_id': employee_id},
    )


def _assert_not_duplicate(*, version, employee=None, resident=None):
    pool_query = ArrivalRosterPoolRow.objects.filter(version=version)
    if (resident is not None and pool_query.filter(resident=resident).exists()) or (
        employee is not None and pool_query.filter(employee=employee).exists()
    ):
        raise _validation_error(
            'arrival_roster.pool_duplicate',
            'Этот жилец или сотрудник уже добавлен в версию реестра.',
        )
    if (
        resident is not None and (
            ArrivalRosterMatch.objects.filter(version=version, matched_resident=resident).exists()
            or ArrivalRosterRowReview._base_manager.filter(
            version=version,
            selected_resident=resident,
            ).exists()
        )
    ):
        raise _validation_error(
            'arrival_roster.pool_duplicate',
            'Этот жилец уже присутствует в версии реестра.',
        )


@transaction.atomic
def create_arrival_roster_from_employee_pool(*, watch_period_id, actor_access_id):
    access_snapshot = _access_snapshot(actor_access_id)
    period_plan = (
        WatchPeriod.objects
        .filter(pk=watch_period_id, is_active=True)
        .values('pk', 'watch_composition_id')
        .first()
    )
    if period_plan is None:
        raise _validation_error(
            'arrival_roster.watch_period_required',
            'Выбранный период вахты недоступен.',
        )
    subject_ids = list(
        Employee.objects
        .filter(watch_composition_id=period_plan['watch_composition_id'])
        .order_by('pk')
        .values_list('pk', flat=True)
    )
    resident_plan = _resident_plan(subject_ids)
    version_ids = list(
        ArrivalRosterVersion.objects
        .filter(watch_period_id=period_plan['pk'])
        .order_by('pk')
        .values_list('pk', flat=True)
    )

    locked_employees = _lock_employee_plan([access_snapshot['employee_id'], *subject_ids])
    actor_context = _lock_timekeeper_access(
        access_snapshot,
        locked_employees=locked_employees,
    )
    actor_access = _verified_timekeeper_access(actor_context)
    period = (
        WatchPeriod.objects
        .select_for_update(of=('self',))
        .select_related('watch_composition')
        .get(pk=period_plan['pk'])
    )
    if (
        not period.is_active
        or period.watch_composition_id != period_plan['watch_composition_id']
    ):
        raise _validation_error(
            'arrival_roster.watch_period_changed',
            'Выбранный период вахты изменился. Повторите операцию.',
        )
    if period.watch_composition_id is None or not period.watch_composition.is_active:
        raise _validation_error(
            'arrival_roster.watch_composition_required',
            'Для периода требуется активный утверждённый состав вахты.',
        )

    existing_versions = list(
        ArrivalRosterVersion.objects
        .select_for_update(of=('self',))
        .filter(pk__in=version_ids)
        .order_by('pk')
    )
    if (
        [version.pk for version in existing_versions] != version_ids
        or list(
            ArrivalRosterVersion.objects
            .filter(watch_period=period)
            .order_by('pk')
            .values_list('pk', flat=True)
        ) != version_ids
    ):
        raise _validation_error(
            'arrival_roster.version_plan_changed',
            'Версии реестра изменились. Повторите операцию.',
        )
    residents_by_employee = _lock_resident_plan(
        resident_plan,
        employee_ids=subject_ids,
    )
    version_number = max(
        (version.version_number for version in existing_versions),
        default=0,
    ) + 1
    if list(
        Employee.objects
        .filter(watch_composition_id=period.watch_composition_id)
        .order_by('pk')
        .values_list('pk', flat=True)
    ) != subject_ids:
        raise _validation_error(
            'arrival_roster.employee_plan_changed',
            'Состав сотрудников изменился. Повторите операцию.',
        )
    employees = [locked_employees[employee_id] for employee_id in subject_ids]

    plans = []
    for employee in employees:
        resident, resident_issue = _resident_for_employee(
            employee,
            residents_by_employee.get(employee.pk, []),
        )
        employee_snapshot = _employee_snapshot(employee)
        issue_specs = _issue_specs_for_employee(employee, period)
        if resident_issue:
            issue_specs.append(resident_issue)
        plans.append({
            'employee': employee,
            'resident': resident,
            'employee_snapshot': employee_snapshot,
            'resident_snapshot': _resident_snapshot(resident) if resident else {},
            'issues': issue_specs,
        })

    source_snapshot = {
        'watch_period_id': period.pk,
        'watch_composition_id': period.watch_composition_id,
        'employees': [
            {
                'employee': plan['employee_snapshot'],
                'resident': plan['resident_snapshot'],
                'issue_codes': sorted(code for code, _message in plan['issues']),
            }
            for plan in plans
        ],
    }
    source_fingerprint = _canonical_sha256(source_snapshot)
    version = ArrivalRosterVersion(
        watch_period=period,
        version_number=version_number,
        status=ArrivalRosterVersion.Status.REVIEW_REQUIRED,
        source_kind=ArrivalRosterVersion.SourceKind.EMPLOYEE_POOL,
        source_file=None,
        parser_profile=None,
        created_by_access=actor_access,
        source_fingerprint=source_fingerprint,
    )
    version.save()

    pool_rows = []
    for plan in plans:
        employee = plan['employee']
        resident = plan['resident']
        participation = (
            ArrivalRosterPoolRow.SuggestedParticipation.ARRIVING
            if resident is not None and not plan['issues']
            else None
        )
        match, _review = _trusted_create_match_and_review(
            version=version,
            actor_context=actor_context,
            resident=resident,
            method='employee_pool_exact' if resident else 'employee_pool_unresolved',
            evidence={'employee_id': employee.pk},
            participation=None,
        )
        pool_rows.append(_trusted_create_pool_row(
            version=version,
            match=match,
            resident=resident,
            employee=employee,
            watch_composition=employee.watch_composition,
            origin_kind=ArrivalRosterPoolRow.OriginKind.AUTOMATIC_EMPLOYEE,
            suggested_participation=participation,
            basis='',
            actor_context=actor_context,
        ))
        for code, message in plan['issues']:
            _create_blocking_issue(
                version=version,
                match=match,
                code=code,
                message=message,
                employee_id=employee.pk,
            )

    blocking_count = version.issues.filter(severity=ArrivalRosterIssue.Severity.ERROR).count()
    immutable_snapshot = {
        'source_kind': ArrivalRosterVersion.SourceKind.EMPLOYEE_POOL,
        'source_fingerprint': source_fingerprint,
        'source': source_snapshot,
        'version_number': version_number,
        'pool_row_hashes': [row.snapshot_sha256 for row in pool_rows],
        'blocking_issue_count': blocking_count,
    }
    version.status = (
        ArrivalRosterVersion.Status.REVIEW_REQUIRED
        if blocking_count
        else ArrivalRosterVersion.Status.DRAFT
    )
    version.source_row_count = len(plans)
    version.normalized_row_count = len(pool_rows)
    version.blocking_issue_count = blocking_count
    version.warning_count = 0
    version.snapshot = immutable_snapshot
    version.snapshot_sha256 = _canonical_sha256(immutable_snapshot)
    version.save(update_fields=[
        'status', 'source_row_count', 'normalized_row_count',
        'blocking_issue_count', 'warning_count', 'snapshot',
        'snapshot_sha256', 'updated_at',
    ])
    _trusted_create_arrival_roster_event(
        version=version,
        actor_context=actor_context,
        action=ArrivalRosterEvent.Action.POOL_CREATED,
        details={
            'source_fingerprint': source_fingerprint,
            'employees': len(plans),
            'pool_rows': len(pool_rows),
            'blocking_issues': blocking_count,
        },
    )
    return version


@transaction.atomic
def add_employee_to_arrival_roster(*, version_id, employee_id, basis,
                                   actor_access_id):
    access_snapshot = _access_snapshot(actor_access_id)
    version_plan = (
        ArrivalRosterVersion.objects
        .filter(pk=version_id)
        .values('pk', 'watch_period_id')
        .first()
    )
    if version_plan is None or not Employee.objects.filter(pk=employee_id).exists():
        raise _validation_error(
            'arrival_roster.employee_required',
            'Сотрудник или версия реестра не найдены.',
        )
    resident_plan = _resident_plan([employee_id])
    locked_employees = _lock_employee_plan([
        access_snapshot['employee_id'],
        employee_id,
    ])
    actor_context = _lock_timekeeper_access(
        access_snapshot,
        locked_employees=locked_employees,
    )
    actor_access = _verified_timekeeper_access(actor_context)
    period = (
        WatchPeriod.objects
        .select_for_update(of=('self',))
        .get(pk=version_plan['watch_period_id'])
    )
    version = (
        ArrivalRosterVersion.objects
        .select_for_update(of=('self',))
        .get(pk=version_plan['pk'])
    )
    _require_reviewable_version(version)
    if version.watch_period_id != period.pk:
        raise _validation_error(
            'arrival_roster.version_plan_changed',
            'Версия реестра изменилась. Повторите операцию.',
        )
    residents_by_employee = _lock_resident_plan(
        resident_plan,
        employee_ids=[employee_id],
    )
    employee = locked_employees[employee_id]
    resident, resident_issue = _resident_for_employee(
        employee,
        residents_by_employee.get(employee.pk, []),
    )
    _assert_not_duplicate(version=version, employee=employee, resident=resident)
    basis = ' '.join(str(basis or '').split())
    if not basis:
        raise _validation_error(
            'arrival_roster.pool_basis_required',
            'Укажите основание добавления сотрудника.',
        )
    if employee.dismissed_at is not None and employee.dismissed_at < period.starts_on:
        raise _validation_error(
            'arrival_roster.employee_dismissed_before_period',
            'Сотрудник уволен до начала выбранного периода и не может быть добавлен.',
        )
    issue_specs = _issue_specs_for_employee(
        employee,
        period,
        composition_required=True,
    )
    if resident_issue:
        issue_specs.append(resident_issue)
    match, _review = _trusted_create_match_and_review(
        version=version,
        actor_context=actor_context,
        resident=resident,
        method='manual_employee_exact',
        evidence={'employee_id': employee.pk},
        participation=ArrivalRosterPoolRow.SuggestedParticipation.ADDITIONAL,
    )
    row = _trusted_create_pool_row(
        version=version,
        match=match,
        resident=resident,
        employee=employee,
        watch_composition=employee.watch_composition,
        origin_kind=ArrivalRosterPoolRow.OriginKind.MANUAL_EMPLOYEE,
        suggested_participation=ArrivalRosterPoolRow.SuggestedParticipation.ADDITIONAL,
        basis=basis,
        actor_context=actor_context,
    )
    for code, message in issue_specs:
        _create_blocking_issue(
            version=version,
            match=match,
            code=code,
            message=message,
            employee_id=employee.pk,
        )
    _trusted_create_arrival_roster_event(
        version=version,
        actor_context=actor_context,
        action=ArrivalRosterEvent.Action.POOL_EMPLOYEE_ADDED,
        details={
            'employee_id': employee.pk,
            'resident_id': resident.pk if resident else None,
        },
    )
    return row


@transaction.atomic
def add_external_resident_to_arrival_roster(*, version_id, resident_id, basis,
                                            actor_access_id):
    version, actor_context = _lock_version(
        version_id=version_id,
        actor_access_id=actor_access_id,
    )
    _verified_timekeeper_access(actor_context)
    basis = ' '.join(str(basis or '').split())
    if not basis:
        raise _validation_error(
            'arrival_roster.pool_basis_required',
            'Укажите основание добавления внешнего жильца.',
        )
    try:
        resident = (
            SettlementResident.objects
            .select_for_update(of=('self',))
            .get(pk=resident_id)
        )
    except SettlementResident.DoesNotExist as error:
        raise _validation_error(
            'arrival_roster.external_resident_required',
            'Внешняя карточка жильца не найдена.',
        ) from error
    if not resident.is_external or resident.status != SettlementResident.Status.ACTIVE:
        raise _validation_error(
            'arrival_roster.external_resident_unavailable',
            'Внешняя карточка жильца неактивна или имеет другой тип.',
        )
    _assert_not_duplicate(version=version, resident=resident)
    match, _review = _trusted_create_match_and_review(
        version=version,
        actor_context=actor_context,
        resident=resident,
        method='manual_external_exact',
        evidence={'resident_id': resident.pk},
        participation=ArrivalRosterPoolRow.SuggestedParticipation.ADDITIONAL,
    )
    row = _trusted_create_pool_row(
        version=version,
        match=match,
        resident=resident,
        employee=None,
        watch_composition=None,
        origin_kind=ArrivalRosterPoolRow.OriginKind.MANUAL_EXTERNAL,
        suggested_participation=ArrivalRosterPoolRow.SuggestedParticipation.ADDITIONAL,
        basis=basis,
        actor_context=actor_context,
    )
    _trusted_create_arrival_roster_event(
        version=version,
        actor_context=actor_context,
        action=ArrivalRosterEvent.Action.POOL_EXTERNAL_ADDED,
        details={'resident_id': resident.pk},
    )
    return row


@transaction.atomic
def confirm_unambiguous_arrival_roster_rows(*, version_id, actor_access_id):
    version, actor_context = _lock_version(
        version_id=version_id,
        actor_access_id=actor_access_id,
    )
    actor_access = _verified_timekeeper_access(actor_context)
    has_version_blocker = version.issues.filter(
        severity=ArrivalRosterIssue.Severity.ERROR,
        match__isnull=True,
    ).exists()

    rows = list(
        ArrivalRosterPoolRow.objects
        .select_related(
            'employee', 'resident', 'match', 'match__row_review',
            'watch_composition',
        )
        .prefetch_related('match__issues')
        .filter(
            version=version,
            origin_kind=ArrivalRosterPoolRow.OriginKind.AUTOMATIC_EMPLOYEE,
        )
        .order_by('employee_id', 'pk')
    )
    reviews_by_match_id = {
        review.match_id: review
        for review in (
            ArrivalRosterRowReview.objects
            .select_for_update()
            .filter(version=version, match_id__in=[row.match_id for row in rows])
            .order_by('match_id', 'pk')
        )
    }
    changed = 0
    already_confirmed = 0
    skipped = 0
    for row in rows:
        employee = row.employee
        resident = row.resident
        review = reviews_by_match_id.get(row.match_id)
        has_blocker = any(
            issue.severity == ArrivalRosterIssue.Severity.ERROR
            for issue in row.match.issues.all()
        )
        if (
            has_version_blocker
            or row.suggested_participation != ArrivalRosterPoolRow.SuggestedParticipation.ARRIVING
            or employee is None
            or resident is None
            or review is None
            or has_blocker
            or row.watch_composition_id != version.watch_period.watch_composition_id
            or employee.watch_composition_id != version.watch_period.watch_composition_id
            or employee.status != Employee.Status.ACTIVE
            or not employee.is_active
            or (
                employee.dismissed_at is not None
                and employee.dismissed_at < version.watch_period.starts_on
            )
            or resident.resident_type != SettlementResident.ResidentType.EMPLOYEE
            or resident.employee_id != employee.pk
            or resident.status != SettlementResident.Status.ACTIVE
            or row.match.status != ArrivalRosterMatch.Status.EXACT
            or row.match.matched_resident_id != resident.pk
            or review.resident_resolution != ArrivalRosterRowReview.ResidentResolution.SELECTED
            or review.selected_resident_id != resident.pk
        ):
            skipped += 1
            continue
        if review.participation_status == ArrivalRosterRowReview.ParticipationStatus.ARRIVING:
            already_confirmed += 1
            continue
        if review.participation_status or review.arrival_mode:
            skipped += 1
            continue

        review.participation_status = ArrivalRosterRowReview.ParticipationStatus.ARRIVING
        review.revision += 1
        review.updated_by_access = actor_access
        _trusted_write_review(review=review, creating=False)
        _trusted_create_arrival_roster_event(
            version=version,
            actor_context=actor_context,
            match=row.match,
            review_revision=review.revision,
            action=ArrivalRosterEvent.Action.PARTICIPATION_CHANGED,
            details={},
        )
        changed += 1
    return ArrivalRosterBulkConfirmationResult(
        changed=changed,
        already_confirmed=already_confirmed,
        skipped=skipped,
    )


__all__ = [
    'add_employee_to_arrival_roster',
    'add_external_resident_to_arrival_roster',
    'confirm_unambiguous_arrival_roster_rows',
    'create_arrival_roster_from_employee_pool',
]
