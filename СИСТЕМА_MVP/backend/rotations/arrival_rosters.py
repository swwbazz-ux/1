import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from settlement.models import SettlementResident
from shifts.models import WatchPeriod
from users.models import Employee, EmployeeAccess

from .arrival_roster_parser import (
    MAX_FILE_SIZE,
    PROFILE_CODE,
    PROFILE_VERSION,
    UnsafeArrivalWorkbook,
    parse_arrival_workbook,
    parser_profile_snapshot,
)
from .models import (
    ArrivalRosterEvent,
    ArrivalRosterIssue,
    ArrivalRosterIssueResolution,
    ArrivalRosterMatch,
    ArrivalRosterMatchCandidate,
    ArrivalRosterMatchRow,
    ArrivalRosterNormalizedRow,
    ArrivalRosterParserProfile,
    ArrivalRosterPoolRow,
    ArrivalRosterRowReview,
    ArrivalRosterSourceFile,
    ArrivalRosterSourceRow,
    ArrivalRosterVersion,
)


ALLOWED_CONTENT_TYPES = {
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/octet-stream',
    'application/zip',
    '',
}


def _validation_error(code, message):
    return ValidationError(message, code=code)


_EVENT_DETAIL_KEYS = {
    ArrivalRosterEvent.Action.UPLOADED: {'sha256', 'byte_size'},
    ArrivalRosterEvent.Action.REUSED: {'sha256'},
    ArrivalRosterEvent.Action.PARSED: {
        'source_rows', 'normalized_rows', 'blocking_issues', 'warnings',
    },
    ArrivalRosterEvent.Action.RESIDENT_SELECTED: set(),
    ArrivalRosterEvent.Action.RESIDENT_CLEARED: set(),
    ArrivalRosterEvent.Action.PARTICIPATION_CHANGED: set(),
    ArrivalRosterEvent.Action.ARRIVAL_MODE_CHANGED: set(),
    ArrivalRosterEvent.Action.DATES_CHANGED: set(),
    ArrivalRosterEvent.Action.NOTES_CHANGED: set(),
    ArrivalRosterEvent.Action.ISSUE_RESOLVED: set(),
    ArrivalRosterEvent.Action.ISSUE_REOPENED: set(),
    ArrivalRosterEvent.Action.POOL_CREATED: {
        'source_fingerprint', 'employees', 'pool_rows', 'blocking_issues',
    },
    ArrivalRosterEvent.Action.POOL_EMPLOYEE_ADDED: {'employee_id', 'resident_id'},
    ArrivalRosterEvent.Action.POOL_EXTERNAL_ADDED: {'resident_id'},
}

_GENERIC_ISSUE_RESOLUTION_FORBIDDEN_CODES = {
    'match_unmatched',
    'conflicting_shift_hints',
    'unknown_shift_hint',
    'formula_in_content',
    'missing_sheet',
    'content_outside_profile',
}

_STRUCTURAL_SOURCE_ISSUE_CODES = {
    'content_outside_profile',
    'empty_primary_sheet',
    'formula_in_content',
    'invalid_header',
    'invalid_name',
    'missing_name',
    'missing_sheet',
}

_MATCH_ISSUE_CODES = {
    'match_ambiguous',
    'match_conflict',
    'match_probable',
    'match_unmatched',
}

_DATE_ISSUE_CODES = {'conflicting_arrival_dates', 'date_requires_review'}
_PARTICIPATION_ISSUE_CODES = {'participation_requires_review'}
_SHIFT_ISSUE_CODES = {'conflicting_shift_hints', 'unknown_shift_hint'}


_VERIFIED_TIMEKEEPER_CONTEXT_MARKER = object()


@dataclass(frozen=True, slots=True)
class _VerifiedTimekeeperContext:
    actor_access: EmployeeAccess = field(repr=False, compare=False)
    access_id: int
    employee_id: int
    role_id: int
    access_status: str
    access_is_active: bool
    employee_status: str
    employee_is_active: bool
    role_code: str
    role_is_active: bool
    _marker: object = field(repr=False, compare=False)


def _verified_timekeeper_access(context):
    if (
        type(context) is not _VerifiedTimekeeperContext
        or context._marker is not _VERIFIED_TIMEKEEPER_CONTEXT_MARKER
    ):
        raise _validation_error(
            'arrival_roster.verified_context_required',
            'Не подтверждён точный доступ табельщика.',
        )
    access = context.actor_access
    employee = access._state.fields_cache.get('employee')
    role = access._state.fields_cache.get('role')
    if (
        access.pk != context.access_id
        or access.employee_id != context.employee_id
        or access.role_id != context.role_id
        or access.status != context.access_status
        or access.is_active != context.access_is_active
        or employee is None
        or employee.pk != context.employee_id
        or employee.status != context.employee_status
        or employee.is_active != context.employee_is_active
        or role is None
        or role.pk != context.role_id
        or role.code != context.role_code
        or role.is_active != context.role_is_active
        or access.status != EmployeeAccess.Status.ACTIVATED
        or not access.is_active
        or employee.status != Employee.Status.ACTIVE
        or not employee.is_active
        or role.code != 'timekeeper'
        or not role.is_active
    ):
        raise _validation_error(
            'arrival_roster.verified_context_invalid',
            'Подтверждённый доступ табельщика больше не соответствует проверенному контексту.',
        )
    return access


def _trusted_create_arrival_roster_event(*, version, actor_context, action,
                                         match=None, issue=None,
                                         review_revision=None, details=None):
    actor_access = _verified_timekeeper_access(actor_context)
    details = dict(details or {})
    allowed_keys = _EVENT_DETAIL_KEYS.get(action)
    if allowed_keys is None or set(details) - allowed_keys:
        raise _validation_error(
            'arrival_roster.unsafe_event_details',
            'Журнал содержит недопустимые сведения.',
        )
    if any(not isinstance(value, (str, int, bool, type(None))) for value in details.values()):
        raise _validation_error(
            'arrival_roster.unsafe_event_details',
            'Журнал содержит недопустимые сведения.',
        )
    if match is not None and match.version_id != version.pk:
        raise _validation_error(
            'arrival_roster.event_version_mismatch',
            'Строка относится к другой версии реестра.',
        )
    if issue is not None and issue.version_id != version.pk:
        raise _validation_error(
            'arrival_roster.event_version_mismatch',
            'Вопрос относится к другой версии реестра.',
        )
    row_actions = {
        ArrivalRosterEvent.Action.RESIDENT_SELECTED,
        ArrivalRosterEvent.Action.RESIDENT_CLEARED,
        ArrivalRosterEvent.Action.PARTICIPATION_CHANGED,
        ArrivalRosterEvent.Action.ARRIVAL_MODE_CHANGED,
        ArrivalRosterEvent.Action.DATES_CHANGED,
        ArrivalRosterEvent.Action.NOTES_CHANGED,
    }
    issue_actions = {
        ArrivalRosterEvent.Action.ISSUE_RESOLVED,
        ArrivalRosterEvent.Action.ISSUE_REOPENED,
    }
    if action in row_actions and (match is None or issue is not None or not review_revision):
        raise _validation_error(
            'arrival_roster.invalid_event_shape',
            'Событие ручной проверки сформировано некорректно.',
        )
    if action in issue_actions and (issue is None or match is not None or not review_revision):
        raise _validation_error(
            'arrival_roster.invalid_event_shape',
            'Событие вопроса сформировано некорректно.',
        )
    if action not in row_actions | issue_actions and (
        match is not None or issue is not None or review_revision is not None
    ):
        raise _validation_error(
            'arrival_roster.invalid_event_shape',
            'Служебное событие сформировано некорректно.',
        )
    event = ArrivalRosterEvent(
        version=version,
        actor_access=actor_access,
        match=match,
        issue=issue,
        review_revision=review_revision,
        action=action,
        details=details,
    )
    event.full_clean()
    ArrivalRosterEvent._base_manager.bulk_create([event])
    return event


def _canonical_sha256(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
    ).hexdigest()


_PRIVATE_METADATA_KEYS = {
    'access', 'access_id', 'full_name', 'password', 'phone', 'pin',
    'raw_phone', 'session', 'token',
}


def _validate_safe_metadata(value, *, code):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise _validation_error(code, 'Служебные доказательства имеют недопустимый формат.') from error
    if len(encoded.encode('utf-8')) > 16 * 1024:
        raise _validation_error(code, 'Служебные доказательства превышают допустимый размер.')

    def walk(item):
        if isinstance(item, dict):
            for key, nested in item.items():
                normalized_key = str(key).casefold()
                if normalized_key in _PRIVATE_METADATA_KEYS:
                    raise _validation_error(
                        code,
                        'Служебные доказательства содержат закрытые сведения.',
                    )
                walk(nested)
        elif isinstance(item, list):
            for nested in item:
                walk(nested)
        elif not isinstance(item, (str, int, float, bool, type(None))):
            raise _validation_error(code, 'Служебные доказательства имеют недопустимый формат.')

    walk(value)


def _trusted_create_arrival_roster_match(*, version, status, method, quality,
                                         matched_resident=None, evidence=None):
    evidence = dict(evidence or {})
    _validate_safe_metadata(evidence, code='arrival_roster.unsafe_match_evidence')
    if not ArrivalRosterVersion._base_manager.filter(pk=version.pk).exists():
        raise _validation_error('arrival_roster.match_version_required', 'Версия реестра не найдена.')
    match = ArrivalRosterMatch(
        version=version,
        status=status,
        method=method,
        quality=quality,
        matched_resident=matched_resident,
        evidence=evidence,
    )
    match.full_clean()
    ArrivalRosterMatch._base_manager.bulk_create([match])
    return match


def _trusted_create_arrival_roster_match_row(*, match, normalized_row):
    if normalized_row.source_row.version_id != match.version_id:
        raise _validation_error(
            'arrival_roster.match_row_version_mismatch',
            'Исходная строка относится к другой версии реестра.',
        )
    link = ArrivalRosterMatchRow(match=match, normalized_row=normalized_row)
    link.full_clean()
    ArrivalRosterMatchRow._base_manager.bulk_create([link])
    return link


def _trusted_create_arrival_roster_match_candidate(*, match, resident, evidence=None):
    evidence = dict(evidence or {})
    _validate_safe_metadata(evidence, code='arrival_roster.unsafe_match_evidence')
    candidate = ArrivalRosterMatchCandidate(
        match=match,
        resident=resident,
        evidence=evidence,
    )
    candidate.full_clean()
    ArrivalRosterMatchCandidate._base_manager.bulk_create([candidate])
    return candidate


def _trusted_create_arrival_roster_issue(*, version, severity, code, message,
                                         source_row=None, normalized_row=None,
                                         match=None, details=None):
    details = dict(details or {})
    _validate_safe_metadata(details, code='arrival_roster.unsafe_issue_details')
    related_versions = {
        source_row.version_id if source_row is not None else None,
        normalized_row.source_row.version_id if normalized_row is not None else None,
        match.version_id if match is not None else None,
    } - {None}
    if related_versions - {version.pk}:
        raise _validation_error(
            'arrival_roster.issue_version_mismatch',
            'Вопрос относится к другой версии реестра.',
        )
    issue = ArrivalRosterIssue(
        version=version,
        source_row=source_row,
        normalized_row=normalized_row,
        match=match,
        severity=severity,
        code=code,
        message=message,
        details=details,
    )
    issue.full_clean()
    ArrivalRosterIssue._base_manager.bulk_create([issue])
    return issue


def _read_uploaded_xlsx(uploaded_file):
    original_name = Path(str(uploaded_file.name or '')).name
    if not original_name.casefold().endswith('.xlsx'):
        raise _validation_error('arrival_roster.xlsx_required', 'Разрешены только файлы .xlsx.')
    if original_name.casefold().endswith('.xlsm'):
        raise _validation_error('arrival_roster.xlsx_required', 'Книги с макросами не принимаются.')
    content_type = str(getattr(uploaded_file, 'content_type', '') or '').casefold()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise _validation_error('arrival_roster.invalid_content_type', 'Тип загруженного файла не поддерживается.')
    chunks = []
    byte_size = 0
    digest = hashlib.sha256()
    for chunk in uploaded_file.chunks():
        byte_size += len(chunk)
        if byte_size > MAX_FILE_SIZE:
            raise _validation_error(
                'arrival_roster.file_too_large',
                'Размер файла превышает 10 МиБ.',
            )
        digest.update(chunk)
        chunks.append(chunk)
    if byte_size == 0:
        raise _validation_error('arrival_roster.empty_file', 'Загружен пустой файл.')
    return {
        'payload': b''.join(chunks),
        'sha256': digest.hexdigest(),
        'byte_size': byte_size,
        'original_name': original_name,
        'content_type': content_type or 'application/octet-stream',
    }


def _access_snapshot(actor_access_id):
    snapshot = (
        EmployeeAccess.objects
        .filter(pk=actor_access_id)
        .values('pk', 'employee_id', 'role_id')
        .first()
    )
    if snapshot is None:
        raise _validation_error(
            'arrival_roster.access_required',
            'Точный доступ табельщика не найден.',
        )
    return snapshot


def _lock_employee_plan(employee_ids):
    planned_ids = sorted({int(employee_id) for employee_id in employee_ids})
    employees = list(
        Employee.objects
        .select_for_update(of=('self',))
        .select_related('watch_composition')
        .filter(pk__in=planned_ids)
        .order_by('pk')
    )
    if [employee.pk for employee in employees] != planned_ids:
        raise _validation_error(
            'arrival_roster.employee_plan_changed',
            'Состав сотрудников изменился. Повторите операцию.',
        )
    return {employee.pk: employee for employee in employees}


def _lock_timekeeper_access(snapshot, *, locked_employees=None):
    if locked_employees is None:
        _lock_employee_plan([snapshot['employee_id']])
    elif snapshot['employee_id'] not in locked_employees:
        raise _validation_error(
            'arrival_roster.employee_plan_changed',
            'Состав сотрудников изменился. Повторите операцию.',
        )
    try:
        access = (
            EmployeeAccess.objects
            .select_for_update(of=('self',))
            .select_related('employee', 'role')
            .get(pk=snapshot['pk'])
        )
    except EmployeeAccess.DoesNotExist as error:
        raise _validation_error(
            'arrival_roster.access_required',
            'Точный доступ табельщика не найден.',
        ) from error
    if access.employee_id != snapshot['employee_id'] or access.role_id != snapshot['role_id']:
        raise _validation_error(
            'arrival_roster.stale_access',
            'Доступ табельщика изменился. Войдите повторно.',
        )
    if (
        access.status != EmployeeAccess.Status.ACTIVATED
        or not access.is_active
        or not access.employee.is_active
        or access.employee.status != Employee.Status.ACTIVE
        or not access.role.is_active
        or access.role.code != 'timekeeper'
    ):
        raise _validation_error(
            'arrival_roster.access_denied',
            'Доступ табельщика неактивен или не имеет нужной роли.',
        )
    return _VerifiedTimekeeperContext(
        actor_access=access,
        access_id=access.pk,
        employee_id=access.employee_id,
        role_id=access.role_id,
        access_status=access.status,
        access_is_active=access.is_active,
        employee_status=access.employee.status,
        employee_is_active=access.employee.is_active,
        role_code=access.role.code,
        role_is_active=access.role.is_active,
        _marker=_VERIFIED_TIMEKEEPER_CONTEXT_MARKER,
    )


def _positive_revision(value):
    try:
        revision = int(value)
    except (TypeError, ValueError) as error:
        raise _validation_error(
            'arrival_roster.invalid_revision',
            'Некорректная ревизия ручной проверки.',
        ) from error
    if revision < 0:
        raise _validation_error(
            'arrival_roster.invalid_revision',
            'Некорректная ревизия ручной проверки.',
        )
    return revision


def _require_reviewable_version(version):
    if version.status not in {
        ArrivalRosterVersion.Status.DRAFT,
        ArrivalRosterVersion.Status.REVIEW_REQUIRED,
    }:
        raise _validation_error(
            'arrival_roster.version_not_reviewable',
            'Эту версию реестра нельзя изменять.',
        )


def _lock_version(*, version_id, actor_access_id):
    snapshot = _access_snapshot(actor_access_id)
    version_snapshot = (
        ArrivalRosterVersion.objects
        .filter(pk=version_id)
        .values('pk', 'watch_period_id')
        .first()
    )
    if version_snapshot is None:
        raise _validation_error(
            'arrival_roster.version_required',
            'Версия реестра не найдена.',
        )
    actor_context = _lock_timekeeper_access(snapshot)
    WatchPeriod.objects.select_for_update().get(pk=version_snapshot['watch_period_id'])
    version = ArrivalRosterVersion.objects.select_for_update().get(pk=version_snapshot['pk'])
    _require_reviewable_version(version)
    return version, actor_context


def _lock_match_context(*, match_id, actor_access_id):
    match_snapshot = (
        ArrivalRosterMatch.objects
        .filter(pk=match_id)
        .values('pk', 'version_id')
        .first()
    )
    if match_snapshot is None:
        raise _validation_error(
            'arrival_roster.match_required',
            'Строка реестра не найдена.',
        )
    version, actor_context = _lock_version(
        version_id=match_snapshot['version_id'],
        actor_access_id=actor_access_id,
    )
    match = ArrivalRosterMatch.objects.select_for_update().get(pk=match_snapshot['pk'])
    if match.version_id != version.pk:
        raise _validation_error(
            'arrival_roster.stale_match',
            'Строка больше не относится к выбранной версии.',
        )
    review = (
        ArrivalRosterRowReview._base_manager
        .select_for_update()
        .filter(match=match)
        .first()
    )
    return version, match, review, actor_context


def _trusted_write_review(*, review, creating):
    review.full_clean()
    if creating:
        ArrivalRosterRowReview._base_manager.bulk_create([review])
        return review
    review.updated_at = timezone.now()
    fields = {
        'resident_resolution': review.resident_resolution,
        'selected_resident_id': review.selected_resident_id,
        'participation_status': review.participation_status,
        'arrival_mode': review.arrival_mode,
        'arrival_on': review.arrival_on,
        'departure_on': review.departure_on,
        'basis': review.basis,
        'comment': review.comment,
        'revision': review.revision,
        'updated_by_access_id': review.updated_by_access_id,
        'updated_at': review.updated_at,
    }
    updated = ArrivalRosterRowReview._base_manager.filter(pk=review.pk).update(**fields)
    if updated != 1:
        raise _validation_error(
            'arrival_roster.stale_review',
            'Ручная проверка строки изменилась. Обновите страницу.',
        )
    return review


def _change_review(*, match_id, expected_revision, actor_access_id, mutate, actions):
    expected_revision = _positive_revision(expected_revision)
    version, match, review, actor_context = _lock_match_context(
        match_id=match_id,
        actor_access_id=actor_access_id,
    )
    actor_access = _verified_timekeeper_access(actor_context)
    current_revision = review.revision if review else 0
    if current_revision != expected_revision:
        raise _validation_error(
            'arrival_roster.stale_review_revision',
            'Строка уже изменена. Обновите страницу и повторите действие.',
        )
    creating = review is None
    if creating:
        review = ArrivalRosterRowReview(
            version=version,
            match=match,
            revision=1,
            updated_by_access=actor_access,
        )
    else:
        review.revision += 1
        review.updated_by_access = actor_access
    mutate(review)
    if (
        review.selected_resident_id
        and ArrivalRosterRowReview._base_manager.filter(
            version=version,
            selected_resident_id=review.selected_resident_id,
        ).exclude(pk=review.pk).exists()
    ):
        raise _validation_error(
            'arrival_roster.duplicate_resident',
            'Этот жилец уже выбран в другой строке текущей версии.',
        )
    try:
        _trusted_write_review(review=review, creating=creating)
    except IntegrityError as error:
        raise _validation_error(
            'arrival_roster.duplicate_resident',
            'Этот жилец уже выбран в другой строке текущей версии.',
        ) from error
    for action, details in actions(review):
        _trusted_create_arrival_roster_event(
            version=version,
            actor_context=actor_context,
            match=match,
            review_revision=review.revision,
            action=action,
            details=details,
        )
    return review


@transaction.atomic
def select_arrival_roster_resident(*, match_id, resident_id, expected_revision,
                                   actor_access_id):
    def mutate(review):
        resident = (
            SettlementResident.objects
            .select_related('employee')
            .filter(pk=resident_id)
            .first()
        )
        if resident is None or resident.status != SettlementResident.Status.ACTIVE:
            raise _validation_error(
                'arrival_roster.resident_unavailable',
                'Выбранный жилец не найден или недоступен.',
            )
        if resident.employee_id and (
            not resident.employee.is_active
            or resident.employee.status != Employee.Status.ACTIVE
        ):
            raise _validation_error(
                'arrival_roster.resident_unavailable',
                'Выбранный жилец не найден или недоступен.',
            )
        pool_row = (
            ArrivalRosterPoolRow._base_manager
            .filter(match_id=match_id)
            .only('employee_id')
            .first()
        )
        if pool_row is not None and pool_row.employee_id is not None:
            if resident.employee_id != pool_row.employee_id:
                raise _validation_error(
                    'arrival_roster.pool_employee_resident_mismatch',
                    'Жилец не соответствует сотруднику исходного списка.',
                )
        review.resident_resolution = ArrivalRosterRowReview.ResidentResolution.SELECTED
        review.selected_resident = resident

    return _change_review(
        match_id=match_id,
        expected_revision=expected_revision,
        actor_access_id=actor_access_id,
        mutate=mutate,
        actions=lambda review: [(ArrivalRosterEvent.Action.RESIDENT_SELECTED, {})],
    )


@transaction.atomic
def clear_arrival_roster_resident(*, match_id, expected_revision, actor_access_id):
    def mutate(review):
        review.resident_resolution = ArrivalRosterRowReview.ResidentResolution.CLEARED
        review.selected_resident = None

    return _change_review(
        match_id=match_id,
        expected_revision=expected_revision,
        actor_access_id=actor_access_id,
        mutate=mutate,
        actions=lambda review: [(ArrivalRosterEvent.Action.RESIDENT_CLEARED, {})],
    )


@transaction.atomic
def set_arrival_roster_participation(*, match_id, participation_status, arrival_mode,
                                     expected_revision, actor_access_id):
    valid_statuses = set(ArrivalRosterRowReview.ParticipationStatus.values)
    valid_modes = {'', *ArrivalRosterRowReview.ArrivalMode.values}
    participation_status = str(participation_status or '')
    arrival_mode = str(arrival_mode or '')
    if participation_status not in valid_statuses or arrival_mode not in valid_modes:
        raise _validation_error(
            'arrival_roster.invalid_participation',
            'Некорректно указаны участие или способ прибытия.',
        )
    if arrival_mode and participation_status != ArrivalRosterRowReview.ParticipationStatus.ARRIVING:
        raise _validation_error(
            'arrival_roster.invalid_arrival_mode',
            'Способ прибытия можно указать только для заезжающего человека.',
        )

    def mutate(review):
        review.participation_status = participation_status
        review.arrival_mode = arrival_mode or None

    return _change_review(
        match_id=match_id,
        expected_revision=expected_revision,
        actor_access_id=actor_access_id,
        mutate=mutate,
        actions=lambda review: [
            (ArrivalRosterEvent.Action.PARTICIPATION_CHANGED, {}),
            (ArrivalRosterEvent.Action.ARRIVAL_MODE_CHANGED, {}),
        ],
    )


@transaction.atomic
def set_arrival_roster_dates(*, match_id, arrival_on, departure_on,
                             expected_revision, actor_access_id):
    if arrival_on and departure_on and departure_on < arrival_on:
        raise _validation_error(
            'arrival_roster.invalid_dates',
            'Дата выбытия не может быть раньше даты заселения.',
        )

    def mutate(review):
        review.arrival_on = arrival_on
        review.departure_on = departure_on

    return _change_review(
        match_id=match_id,
        expected_revision=expected_revision,
        actor_access_id=actor_access_id,
        mutate=mutate,
        actions=lambda review: [(ArrivalRosterEvent.Action.DATES_CHANGED, {})],
    )


@transaction.atomic
def set_arrival_roster_notes(*, match_id, basis, comment, expected_revision,
                             actor_access_id):
    basis = str(basis or '').strip()
    comment = str(comment or '').strip()
    if not basis and not comment:
        raise _validation_error(
            'arrival_roster.notes_required',
            'Укажите основание или комментарий.',
        )

    def mutate(review):
        review.basis = basis
        review.comment = comment

    return _change_review(
        match_id=match_id,
        expected_revision=expected_revision,
        actor_access_id=actor_access_id,
        mutate=mutate,
        actions=lambda review: [(ArrivalRosterEvent.Action.NOTES_CHANGED, {})],
    )


def _russian_note(value):
    value = str(value or '').strip()
    if not value or not re.search(r'[А-Яа-яЁё]', value):
        raise _validation_error(
            'arrival_roster.russian_note_required',
            'Укажите пояснение на русском языке.',
        )
    return value


def _trusted_write_issue_resolution(*, resolution, creating):
    resolution.full_clean()
    if creating:
        ArrivalRosterIssueResolution._base_manager.bulk_create([resolution])
        return resolution
    resolution.updated_at = timezone.now()
    updated = ArrivalRosterIssueResolution._base_manager.filter(pk=resolution.pk).update(
        is_resolved=resolution.is_resolved,
        resolution_note=resolution.resolution_note,
        revision=resolution.revision,
        updated_by_access_id=resolution.updated_by_access_id,
        updated_at=resolution.updated_at,
    )
    if updated != 1:
        raise _validation_error(
            'arrival_roster.stale_issue',
            'Вопрос уже изменён. Обновите страницу.',
        )
    return resolution


def _change_issue(*, issue_id, expected_revision, actor_access_id, is_resolved,
                  resolution_note, action):
    expected_revision = _positive_revision(expected_revision)
    issue_snapshot = (
        ArrivalRosterIssue.objects
        .filter(pk=issue_id)
        .values('pk', 'version_id')
        .first()
    )
    if issue_snapshot is None:
        raise _validation_error('arrival_roster.issue_required', 'Вопрос не найден.')
    version, actor_context = _lock_version(
        version_id=issue_snapshot['version_id'],
        actor_access_id=actor_access_id,
    )
    actor_access = _verified_timekeeper_access(actor_context)
    issue = ArrivalRosterIssue.objects.select_for_update().get(pk=issue_snapshot['pk'])
    if (
        issue.severity == ArrivalRosterIssue.Severity.ERROR
        or issue.code in _GENERIC_ISSUE_RESOLUTION_FORBIDDEN_CODES
    ):
        raise _validation_error(
            'arrival_roster.blocking_issue_requires_action',
            'Этот вопрос устраняется только фактическим действием в ответственном разделе.',
        )
    resolution = (
        ArrivalRosterIssueResolution._base_manager
        .select_for_update()
        .filter(issue=issue)
        .first()
    )
    current_revision = resolution.revision if resolution else 0
    if current_revision != expected_revision:
        raise _validation_error(
            'arrival_roster.stale_review_revision',
            'Вопрос уже изменён. Обновите страницу и повторите действие.',
        )
    creating = resolution is None
    if creating:
        resolution = ArrivalRosterIssueResolution(
            issue=issue,
            revision=1,
            updated_by_access=actor_access,
        )
    else:
        resolution.revision += 1
        resolution.updated_by_access = actor_access
    resolution.is_resolved = is_resolved
    resolution.resolution_note = _russian_note(resolution_note)
    _trusted_write_issue_resolution(resolution=resolution, creating=creating)
    _trusted_create_arrival_roster_event(
        version=version,
        actor_context=actor_context,
        issue=issue,
        review_revision=resolution.revision,
        action=action,
        details={},
    )
    return resolution


@transaction.atomic
def resolve_arrival_roster_issue(*, issue_id, expected_revision, resolution_note,
                                 actor_access_id):
    return _change_issue(
        issue_id=issue_id,
        expected_revision=expected_revision,
        actor_access_id=actor_access_id,
        is_resolved=True,
        resolution_note=resolution_note,
        action=ArrivalRosterEvent.Action.ISSUE_RESOLVED,
    )


@transaction.atomic
def reopen_arrival_roster_issue(*, issue_id, expected_revision, resolution_note,
                                actor_access_id):
    return _change_issue(
        issue_id=issue_id,
        expected_revision=expected_revision,
        actor_access_id=actor_access_id,
        is_resolved=False,
        resolution_note=resolution_note,
        action=ArrivalRosterEvent.Action.ISSUE_REOPENED,
    )


@transaction.atomic
def search_arrival_roster_residents(*, version_id, query, actor_access_id):
    _lock_version(version_id=version_id, actor_access_id=actor_access_id)
    query = ' '.join(str(query or '').split())
    if len(query) < 3:
        raise _validation_error(
            'arrival_roster.search_too_short',
            'Введите не менее трёх символов.',
        )
    residents = (
        SettlementResident.objects
        .select_related('employee__personnel_position')
        .filter(status=SettlementResident.Status.ACTIVE)
        .filter(Q(employee__full_name__icontains=query) | Q(full_name__icontains=query))
        .order_by('pk')[:20]
    )
    result = []
    for resident in residents:
        if resident.employee_id:
            if (
                not resident.employee.is_active
                or resident.employee.status != Employee.Status.ACTIVE
            ):
                continue
            position = (
                resident.employee.personnel_position.name
                if resident.employee.personnel_position_id
                else resident.employee.position
            )
            description = position or 'Должность не указана'
        else:
            description = ' · '.join(
                item for item in [resident.organization, resident.position_title] if item
            ) or 'Внешний жилец'
        result.append({
            'id': resident.pk,
            'name': resident.display_name,
            'description': description,
        })
    return result[:20]


def arrival_roster_match_readiness(*, match_id):
    try:
        match = (
            ArrivalRosterMatch.objects
            .select_related(
                'version',
                'matched_resident__employee',
                'row_review__selected_resident__employee',
            )
            .prefetch_related('candidates__resident__employee')
            .get(pk=match_id)
        )
    except ArrivalRosterMatch.DoesNotExist as error:
        raise _validation_error(
            'arrival_roster.match_required',
            'Строка реестра не найдена.',
        ) from error
    try:
        review = match.row_review
    except ArrivalRosterRowReview.DoesNotExist:
        review = None

    resident = None
    if review and review.resident_resolution == ArrivalRosterRowReview.ResidentResolution.SELECTED:
        resident = review.selected_resident
    elif not review or review.resident_resolution != ArrivalRosterRowReview.ResidentResolution.CLEARED:
        resident = match.matched_resident

    issues = list(
        ArrivalRosterIssue.objects
        .filter(version=match.version)
        .filter(
            Q(match=match)
            | Q(normalized_row__match_link__match=match)
            | Q(match__isnull=True, normalized_row__isnull=True)
        )
        .distinct()
    )
    issue_codes = {issue.code for issue in issues}

    def result(code, label, blockers):
        return {
            'code': code,
            'label': label,
            'ready': code == 'ready',
            'blocking_codes': sorted(set(blockers)),
        }

    structural_codes = issue_codes.intersection(_STRUCTURAL_SOURCE_ISSUE_CODES)
    if structural_codes:
        return result(
            'corrected_file',
            'Требуется новый исправленный файл',
            structural_codes,
        )

    shift_codes = issue_codes.intersection(_SHIFT_ISSUE_CODES)
    if shift_codes:
        return result(
            'deputy',
            'Требуется заместитель начальника участка',
            shift_codes,
        )

    resident_active = bool(resident and resident.status == SettlementResident.Status.ACTIVE)
    if resident_active and resident.employee_id:
        resident_active = bool(
            resident.employee.is_active
            and resident.employee.status == Employee.Status.ACTIVE
        )
    if not resident_active:
        candidates = list(match.candidates.all())
        if resident and resident.employee_id:
            return result('oup', 'Требуется ОУП', {'resident_inactive'})
        if resident and not resident.employee_id:
            return result('clerk', 'Требуется делопроизводитель', {'resident_inactive'})
        if any(
            candidate.resident.employee_id
            and (
                not candidate.resident.employee.is_active
                or candidate.resident.employee.status != Employee.Status.ACTIVE
            )
            for candidate in candidates
        ):
            return result('oup', 'Требуется ОУП', issue_codes or {'resident_required'})
        if any(not candidate.resident.employee_id for candidate in candidates):
            return result(
                'clerk',
                'Требуется делопроизводитель',
                issue_codes or {'resident_required'},
            )
        return result(
            'timekeeper',
            'Требуется решение табельщика',
            issue_codes.intersection(_MATCH_ISSUE_CODES) or {'resident_required'},
        )

    participation = review.participation_status if review else None
    if participation not in ArrivalRosterRowReview.ParticipationStatus.values:
        return result(
            'timekeeper',
            'Требуется решение табельщика',
            issue_codes.intersection(_PARTICIPATION_ISSUE_CODES) or {'participation_required'},
        )

    arrival_on = review.arrival_on if review else None
    departure_on = review.departure_on if review else None
    if arrival_on and departure_on and departure_on < arrival_on:
        return result('timekeeper', 'Требуется решение табельщика', {'invalid_dates'})
    if participation in {
        ArrivalRosterRowReview.ParticipationStatus.ARRIVING,
        ArrivalRosterRowReview.ParticipationStatus.ADDITIONAL,
    } and (not arrival_on or not departure_on):
        return result(
            'timekeeper',
            'Требуется решение табельщика',
            issue_codes.intersection(_DATE_ISSUE_CODES) or {'dates_required'},
        )
    if (
        participation == ArrivalRosterRowReview.ParticipationStatus.EXTENDED
        and not departure_on
    ):
        return result(
            'timekeeper',
            'Требуется решение табельщика',
            issue_codes.intersection(_DATE_ISSUE_CODES) or {'departure_required'},
        )
    if review and review.arrival_mode and (
        review.arrival_mode not in ArrivalRosterRowReview.ArrivalMode.values
        or participation != ArrivalRosterRowReview.ParticipationStatus.ARRIVING
    ):
        return result('timekeeper', 'Требуется решение табельщика', {'invalid_arrival_mode'})

    known_factually_resolved = (
        _MATCH_ISSUE_CODES
        | _DATE_ISSUE_CODES
        | _PARTICIPATION_ISSUE_CODES
    )
    unknown_blocking_codes = {
        issue.code
        for issue in issues
        if issue.severity == ArrivalRosterIssue.Severity.ERROR
        and issue.code not in known_factually_resolved
    }
    if unknown_blocking_codes:
        return result(
            'timekeeper',
            'Требуется решение табельщика',
            unknown_blocking_codes,
        )
    return result('ready', 'Готово', set())


def _profile():
    configuration, configuration_sha256 = parser_profile_snapshot()
    profile = (
        ArrivalRosterParserProfile.objects
        .filter(code=PROFILE_CODE, version=PROFILE_VERSION)
        .first()
    )
    if profile is None:
        profile = ArrivalRosterParserProfile(
            code=PROFILE_CODE,
            version=PROFILE_VERSION,
            configuration=configuration,
            configuration_sha256=configuration_sha256,
        )
        try:
            with transaction.atomic():
                profile.save()
        except IntegrityError:
            profile = ArrivalRosterParserProfile.objects.get(
                code=PROFILE_CODE,
                version=PROFILE_VERSION,
            )
    if (
        profile.configuration != configuration
        or profile.configuration_sha256 != configuration_sha256
    ):
        raise _validation_error(
            'arrival_roster.profile_changed',
            'Профиль проверки изменился без повышения версии.',
        )
    return profile


def _trusted_create_source_file(*, file_data, actor_access):
    payload = bytes(file_data['payload'])
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    actual_byte_size = len(payload)
    if (
        file_data.get('sha256') != actual_sha256
        or file_data.get('byte_size') != actual_byte_size
    ):
        raise _validation_error(
            'rotations.arrival_roster.source_file_integrity_error',
            'Контрольная сумма или размер исходного файла не совпадают с его содержимым.',
        )

    existing = ArrivalRosterSourceFile.objects.filter(sha256=actual_sha256).first()
    if existing is not None:
        if existing.byte_size != actual_byte_size:
            raise _validation_error(
                'arrival_roster.hash_conflict',
                'Обнаружен конфликт контрольной суммы файла.',
            )
        return existing, None

    source = ArrivalRosterSourceFile(
        sha256=actual_sha256,
        original_name=file_data['original_name'],
        byte_size=actual_byte_size,
        content_type=file_data['content_type'],
        uploaded_by_access=actor_access,
    )
    storage_name = f'{actual_sha256[:2]}/{actual_sha256}.xlsx'
    source.file = ContentFile(payload, name=storage_name)
    source.full_clean()
    source.file.save(storage_name, ContentFile(payload), save=False)
    created_storage_name = source.file.name
    try:
        with transaction.atomic():
            ArrivalRosterSourceFile._base_manager.bulk_create([source])
    except IntegrityError:
        source.file.storage.delete(created_storage_name)
        source = ArrivalRosterSourceFile.objects.get(sha256=actual_sha256)
        return source, None
    except Exception:
        source.file.storage.delete(created_storage_name)
        raise
    return source, created_storage_name


def _trusted_bulk_create_source_rows(*, version, parsed_rows):
    source_rows = []
    seen_keys = set()
    for item in parsed_rows:
        row_sha256 = _canonical_sha256({
            'values': item.raw_values,
            'styles': item.raw_styles,
        })
        if item.row_sha256 != row_sha256:
            raise _validation_error(
                'rotations.arrival_roster.source_row_integrity_error',
                'Контрольная сумма исходной строки не совпадает с её содержимым.',
            )
        row_key = (item.sheet_name, item.row_number)
        if row_key in seen_keys:
            raise _validation_error(
                'rotations.arrival_roster.duplicate_source_row',
                'В результате разбора обнаружена повторяющаяся исходная строка.',
            )
        seen_keys.add(row_key)
        source_row = ArrivalRosterSourceRow(
            version=version,
            sheet_name=item.sheet_name,
            row_number=item.row_number,
            row_kind=item.row_kind,
            raw_values=item.raw_values,
            raw_styles=item.raw_styles,
            row_sha256=row_sha256,
        )
        source_row.full_clean()
        source_rows.append(source_row)

    with transaction.atomic():
        return ArrivalRosterSourceRow._base_manager.bulk_create(source_rows)


def _resident_record(resident):
    if resident.employee_id:
        employee = resident.employee
        position = (
            employee.personnel_position.name
            if employee.personnel_position_id
            else employee.position
        )
        full_name = employee.full_name
        phone = employee.phone
    else:
        position = resident.position_title
        full_name = resident.full_name
        phone = resident.phone
    return {
        'resident': resident,
        'name_key': ' '.join(str(full_name or '').replace('\xa0', ' ').split()).casefold().replace('ё', 'е'),
        'position_key': ' '.join(str(position or '').replace('\xa0', ' ').split()).casefold().replace('ё', 'е'),
        'phones': _phone_tokens(phone),
        'active': resident.status == SettlementResident.Status.ACTIVE,
    }


def _phone_tokens(value):
    digits = ''.join(character for character in str(value or '') if character.isdigit())
    if len(digits) == 10:
        digits = f'7{digits}'
    elif len(digits) == 11 and digits.startswith('8'):
        digits = f'7{digits[1:]}'
    return {digits} if len(digits) == 11 and digits.startswith('7') else set()


def _match_plan(normalized, residents):
    same_name = [record for record in residents if record['name_key'] == normalized.normalized_name_key]
    row_phones = set(normalized.normalized_phones)
    phone_matches = [record for record in residents if row_phones.intersection(record['phones'])]
    exact = [record for record in phone_matches if record in same_name and record['active']]
    conflicting_phone = [record for record in phone_matches if record not in same_name]
    archived_name = [record for record in same_name if not record['active']]
    if len(exact) == 1 and not conflicting_phone:
        return 'exact', 'phone_and_name', 'exact', exact[0], []
    if len(exact) > 1 or conflicting_phone:
        candidates = {record['resident'].pk: record for record in [*exact, *conflicting_phone]}
        return 'conflict', 'identifier_conflict', 'conflict', None, list(candidates.values())
    if archived_name:
        return 'conflict', 'archived_name', 'conflict', None, archived_name
    same_position = [
        record for record in same_name
        if normalized.normalized_position_key
        and record['position_key'] == normalized.normalized_position_key
        and record['active']
    ]
    if len(same_position) == 1:
        return 'probable', 'name_and_position', 'probable', None, same_position
    if len(same_name) > 1 or len(same_position) > 1:
        return 'ambiguous', 'multiple_name_candidates', 'ambiguous', None, [
            record for record in same_name if record['active']
        ]
    if len(same_name) == 1 and same_name[0]['active']:
        return 'probable', 'name_only', 'probable', None, same_name
    return 'unmatched', 'not_found', 'unmatched', None, []


def _persist_parse_result(*, version, parsed):
    source_rows = _trusted_bulk_create_source_rows(
        version=version,
        parsed_rows=parsed.source_rows,
    )
    source_by_key = {}
    for source_row in source_rows:
        source_by_key[(source_row.sheet_name, source_row.row_number)] = source_row

    normalized_models = []
    for item in parsed.normalized_rows:
        normalized = ArrivalRosterNormalizedRow(
            source_row=source_by_key[(item.sheet_name, item.row_number)],
            raw_full_name=item.raw_full_name,
            normalized_full_name=item.normalized_full_name,
            normalized_name_key=item.normalized_name_key,
            name_comment=item.name_comment,
            source_position=item.source_position,
            normalized_position_key=item.normalized_position_key,
            raw_shift_hint=item.raw_shift_hint,
            raw_date=item.raw_date,
            arrival_date_candidate=item.arrival_date_candidate,
            date_comment=item.date_comment,
            route_text=item.route_text,
            raw_phone=item.raw_phone,
            normalized_phones=item.normalized_phones,
            comments=item.comments,
            participation_hint=item.participation_hint,
            color_hint=item.color_hint,
        )
        normalized.save()
        normalized_models.append(normalized)

    residents = [
        _resident_record(resident)
        for resident in (
            SettlementResident.objects
            .select_related('employee__personnel_position')
            .order_by('pk')
        )
    ]
    exact_match_by_resident = {}
    exact_rows_by_resident = {}
    proven_residents_by_name = {}
    for normalized in normalized_models:
        status, method, quality, exact_record, candidates = _match_plan(normalized, residents)
        proven_record = proven_residents_by_name.get(normalized.normalized_name_key)
        if (
            exact_record is None
            and status == ArrivalRosterMatch.Status.PROBABLE
            and not normalized.normalized_phones
            and proven_record is not None
            and normalized.normalized_position_key
            and normalized.normalized_position_key == proven_record['position_key']
        ):
            status = ArrivalRosterMatch.Status.EXACT
            method = 'proven_cross_sheet_identity'
            quality = 'exact'
            exact_record = proven_record
            candidates = []
        if exact_record is not None and exact_record['resident'].pk in exact_match_by_resident:
            match = exact_match_by_resident[exact_record['resident'].pk]
            previous_rows = exact_rows_by_resident[exact_record['resident'].pk]
            if (
                normalized.source_row.sheet_name == 'Список сотрудников'
                and any(row.source_row.sheet_name == 'Список сотрудников' for row in previous_rows)
            ):
                _trusted_create_arrival_roster_issue(
                    version=version,
                    source_row=normalized.source_row,
                    normalized_row=normalized,
                    match=match,
                    severity=ArrivalRosterIssue.Severity.ERROR,
                    code='duplicate_primary_resident',
                    message='Один жилец повторяется в основном списке.',
                )
            prior_shifts = {row.raw_shift_hint for row in previous_rows if row.raw_shift_hint}
            prior_dates = {
                row.arrival_date_candidate
                for row in previous_rows
                if row.arrival_date_candidate
            }
            if normalized.raw_shift_hint and prior_shifts and normalized.raw_shift_hint not in prior_shifts:
                _trusted_create_arrival_roster_issue(
                    version=version,
                    source_row=normalized.source_row,
                    normalized_row=normalized,
                    match=match,
                    severity=ArrivalRosterIssue.Severity.ERROR,
                    code='conflicting_shift_hints',
                    message='В разных листах указаны разные смены-подсказки.',
                )
            if (
                normalized.arrival_date_candidate
                and prior_dates
                and normalized.arrival_date_candidate not in prior_dates
            ):
                _trusted_create_arrival_roster_issue(
                    version=version,
                    source_row=normalized.source_row,
                    normalized_row=normalized,
                    match=match,
                    severity=ArrivalRosterIssue.Severity.ERROR,
                    code='conflicting_arrival_dates',
                    message='В разных листах указаны разные даты прибытия.',
                )
            previous_rows.append(normalized)
        else:
            match = _trusted_create_arrival_roster_match(
                version=version,
                status=status,
                method=method,
                quality=quality,
                matched_resident=exact_record['resident'] if exact_record else None,
                evidence={
                    'name_key_sha256': hashlib.sha256(
                        normalized.normalized_name_key.encode('utf-8')
                    ).hexdigest(),
                    'has_phone': bool(normalized.normalized_phones),
                    'source_sheet': normalized.source_row.sheet_name,
                    'source_row': normalized.source_row.row_number,
                },
            )
            if exact_record is not None:
                exact_match_by_resident[exact_record['resident'].pk] = match
                exact_rows_by_resident[exact_record['resident'].pk] = [normalized]
                proven_residents_by_name[normalized.normalized_name_key] = exact_record
            for candidate in candidates:
                _trusted_create_arrival_roster_match_candidate(
                    match=match,
                    resident=candidate['resident'],
                    evidence={
                        'name_agrees': candidate['name_key'] == normalized.normalized_name_key,
                        'position_agrees': bool(
                            normalized.normalized_position_key
                            and candidate['position_key'] == normalized.normalized_position_key
                        ),
                        'phone_agrees': bool(
                            set(normalized.normalized_phones).intersection(candidate['phones'])
                        ),
                        'resident_active': candidate['active'],
                    },
                )
            if status != ArrivalRosterMatch.Status.EXACT:
                messages = {
                    'probable': 'Найден вероятный жилец; требуется подтверждение табельщиком.',
                    'ambiguous': 'Найдено несколько возможных жильцов.',
                    'unmatched': 'Жилец не найден; создавать новую карточку автоматически запрещено.',
                    'conflict': 'Идентификаторы строки противоречат данным жильцов.',
                }
                _trusted_create_arrival_roster_issue(
                    version=version,
                    source_row=normalized.source_row,
                    normalized_row=normalized,
                    match=match,
                    severity=ArrivalRosterIssue.Severity.ERROR,
                    code=f'match_{status}',
                    message=messages[status],
                    details={'candidate_count': len(candidates)},
                )
        _trusted_create_arrival_roster_match_row(match=match, normalized_row=normalized)

    for parsed_issue in parsed.issues:
        source_row = source_by_key.get((parsed_issue.sheet_name, parsed_issue.row_number))
        normalized_row = None
        match = None
        if source_row is not None:
            try:
                normalized_row = source_row.normalized
            except ArrivalRosterNormalizedRow.DoesNotExist:
                normalized_row = None
            if normalized_row is not None:
                try:
                    match = normalized_row.match_link.match
                except ArrivalRosterMatchRow.DoesNotExist:
                    match = None
        _trusted_create_arrival_roster_issue(
            version=version,
            source_row=source_row,
            normalized_row=normalized_row,
            match=match,
            severity=parsed_issue.severity,
            code=parsed_issue.code,
            message=parsed_issue.message,
            details=parsed_issue.details or {},
        )


@transaction.atomic
def upload_arrival_roster(*, uploaded_file, watch_period_id, actor_access_id):
    file_data = _read_uploaded_xlsx(uploaded_file)
    parsed = parse_arrival_workbook(file_data['payload'])
    snapshot = _access_snapshot(actor_access_id)
    actor_context = _lock_timekeeper_access(snapshot)
    actor_access = _verified_timekeeper_access(actor_context)
    try:
        period = (
            WatchPeriod.objects
            .select_for_update(of=('self',))
            .get(pk=watch_period_id, is_active=True)
        )
    except WatchPeriod.DoesNotExist as error:
        raise _validation_error(
            'arrival_roster.watch_period_required',
            'Выбранный период вахты недоступен.',
        ) from error

    profile = _profile()
    source_file = None
    created_storage_name = None
    try:
        source_file, created_storage_name = _trusted_create_source_file(
            file_data=file_data,
            actor_access=actor_access,
        )
        existing = (
            ArrivalRosterVersion.objects
            .filter(
                watch_period=period,
                source_file=source_file,
                parser_profile=profile,
            )
            .first()
        )
        if existing is not None:
            _trusted_create_arrival_roster_event(
                version=existing,
                actor_context=actor_context,
                action=ArrivalRosterEvent.Action.REUSED,
                details={'sha256': source_file.sha256},
            )
            return existing, False

        last_version = (
            ArrivalRosterVersion.objects
            .select_for_update(of=('self',))
            .filter(watch_period=period)
            .order_by('-version_number', '-pk')
            .first()
        )
        version = ArrivalRosterVersion(
            watch_period=period,
            version_number=(last_version.version_number + 1) if last_version else 1,
            status=ArrivalRosterVersion.Status.REVIEW_REQUIRED,
            source_kind=ArrivalRosterVersion.SourceKind.EXCEL,
            source_file=source_file,
            parser_profile=profile,
            created_by_access=actor_access,
            source_fingerprint=source_file.sha256,
        )
        version.save()
        _persist_parse_result(version=version, parsed=parsed)

        blocking_count = version.issues.filter(
            severity=ArrivalRosterIssue.Severity.ERROR,
        ).count()
        warning_count = version.issues.filter(
            severity=ArrivalRosterIssue.Severity.WARNING,
        ).count()
        source_rows = list(
            version.source_rows.order_by('sheet_name', 'row_number').values(
                'sheet_name', 'row_number', 'row_kind', 'row_sha256',
            )
        )
        matches = list(
            version.matches.order_by('pk').values(
                'status', 'method', 'quality', 'matched_resident_id',
            )
        )
        immutable_snapshot = {
            'watch_period_id': period.pk,
            'version_number': version.version_number,
            'source_sha256': source_file.sha256,
            'parser_profile': {
                'code': profile.code,
                'version': profile.version,
                'sha256': profile.configuration_sha256,
            },
            'workbook': parsed.workbook_summary,
            'source_rows': source_rows,
            'matches': matches,
            'blocking_issue_count': blocking_count,
            'warning_count': warning_count,
        }
        version.status = (
            ArrivalRosterVersion.Status.REVIEW_REQUIRED
            if blocking_count
            else ArrivalRosterVersion.Status.DRAFT
        )
        version.source_row_count = len(source_rows)
        version.normalized_row_count = len(parsed.normalized_rows)
        version.blocking_issue_count = blocking_count
        version.warning_count = warning_count
        version.snapshot = immutable_snapshot
        version.snapshot_sha256 = _canonical_sha256(immutable_snapshot)
        version.save(
            update_fields=[
                'status', 'source_row_count', 'normalized_row_count',
                'blocking_issue_count', 'warning_count', 'snapshot',
                'snapshot_sha256', 'updated_at',
            ]
        )
        _trusted_create_arrival_roster_event(
            version=version,
            actor_context=actor_context,
            action=ArrivalRosterEvent.Action.UPLOADED,
            details={
                'sha256': source_file.sha256,
                'byte_size': source_file.byte_size,
            },
        )
        _trusted_create_arrival_roster_event(
            version=version,
            actor_context=actor_context,
            action=ArrivalRosterEvent.Action.PARSED,
            details={
                'source_rows': version.source_row_count,
                'normalized_rows': version.normalized_row_count,
                'blocking_issues': version.blocking_issue_count,
                'warnings': version.warning_count,
            },
        )
        return version, True
    except Exception:
        if created_storage_name and source_file is not None:
            source_file.file.storage.delete(created_storage_name)
        raise


__all__ = [
    'UnsafeArrivalWorkbook',
    'arrival_roster_match_readiness',
    'clear_arrival_roster_resident',
    'reopen_arrival_roster_issue',
    'resolve_arrival_roster_issue',
    'search_arrival_roster_residents',
    'select_arrival_roster_resident',
    'set_arrival_roster_dates',
    'set_arrival_roster_notes',
    'set_arrival_roster_participation',
    'upload_arrival_roster',
]
