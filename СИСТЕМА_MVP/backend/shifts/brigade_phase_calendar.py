import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from users.models import Employee, EmployeeAccess, WorkSchedule

from .models import (
    WatchPeriod,
    WatchPeriodBrigadePhaseRow,
    WatchPeriodBrigadePhaseVersion,
)


class BrigadePhaseCalendarError(ValidationError):
    """Controlled failure of a closed brigade-phase calendar command."""


ERROR_ACCESS_NOT_FOUND = 'shifts.brigade_phase.access_not_found'
ERROR_ACCESS_INACTIVE = 'shifts.brigade_phase.access_inactive'
ERROR_ACCESS_BLOCKED = 'shifts.brigade_phase.access_blocked'
ERROR_ACCESS_WRONG_ROLE = 'shifts.brigade_phase.access_wrong_role'
ERROR_EMPLOYEE_INACTIVE = 'shifts.brigade_phase.employee_inactive'
ERROR_WATCH_PERIOD_NOT_FOUND = 'shifts.brigade_phase.watch_period_not_found'
ERROR_WORK_SCHEDULE_NOT_FOUND = 'shifts.brigade_phase.work_schedule_not_found'
ERROR_WORK_SCHEDULE_INACTIVE = 'shifts.brigade_phase.work_schedule_inactive'
ERROR_INVALID_SOURCE = 'shifts.brigade_phase.invalid_source'
ERROR_SOURCE_NOT_EFFECTIVE = 'shifts.brigade_phase.source_not_effective_for_period'
ERROR_INVALID_BRIGADE_SET = 'shifts.brigade_phase.invalid_brigade_set'
ERROR_INCONSISTENT_GRAPH = 'shifts.brigade_phase.inconsistent_graph'
ERROR_VERSION_NOT_FOUND = 'shifts.brigade_phase.version_not_found'
ERROR_VERSION_NOT_DRAFT = 'shifts.brigade_phase.version_not_draft'
ERROR_VERSION_STALE = 'shifts.brigade_phase.version_stale'
ERROR_SOURCE_INVALID = 'shifts.brigade_phase.source_invalid'
ERROR_SOURCE_FINGERPRINT_INVALID = 'shifts.brigade_phase.source_fingerprint_invalid'
ERROR_GRAPH_INCOMPLETE = 'shifts.brigade_phase.graph_incomplete'
ERROR_GRAPH_INCONSISTENT = 'shifts.brigade_phase.graph_inconsistent'
ERROR_SCHEDULE_DESIGNATION_MISMATCH = (
    'shifts.brigade_phase.schedule_designation_mismatch'
)
ERROR_POLICY_NOT_DEFINED = 'shifts.brigade_phase.policy_not_defined'
ERROR_POLICY_MISMATCH = 'shifts.brigade_phase.policy_mismatch'
ERROR_CONFIRMED_VERSION_NOT_FOUND = (
    'shifts.brigade_phase.confirmed_version_not_found'
)
ERROR_CONFIRMED_VERSION_INCONSISTENT = (
    'shifts.brigade_phase.confirmed_version_inconsistent'
)
ERROR_BRIGADE_NOT_FOUND = 'shifts.brigade_phase.brigade_not_found'

_ACTOR_ROLE_CODES = frozenset({'timekeeper', 'admin'})
WORK_SCHEDULE_CODE_11 = 'schedule_11'
WORK_SCHEDULE_CODE_12 = 'schedule_12'

_SHA256_PATTERN = re.compile(r'^[0-9a-f]{64}$')
_SOURCE_KIND = 'official_schedule_order'
_ROW_KEYS = frozenset({'brigade_number', 'phase'})
_SOURCE_KEYS = frozenset({'source_kind', 'order', 'schedule'})
_ORDER_KEYS = frozenset(
    {'number', 'date', 'effective_from', 'document_sha256'}
)
_SCHEDULE_KEYS = frozenset({'designation', 'document_sha256'})
_CONFIRMATION_POLICIES = {
    WORK_SCHEDULE_CODE_11: {
        'brigade_count': 2,
        'designation': 'график№11/1',
        'phase_counts': {'day': 1, 'night': 0, 'off': 1},
    },
    WORK_SCHEDULE_CODE_12: {
        'brigade_count': 4,
        'designation': 'график№12/1',
        'phase_counts': {'day': 1, 'night': 1, 'off': 2},
    },
}


@dataclass(frozen=True, slots=True)
class ConfirmedBrigadePhase:
    version_id: int
    row_id: int
    watch_period_id: int
    work_schedule_id: int
    brigade_number: int
    phase: str
    source_fingerprint: str


def _error(code, message):
    return BrigadePhaseCalendarError(message, code=code)


def _normalize_required_text(value, field_label):
    if not isinstance(value, str):
        raise _error(ERROR_INVALID_SOURCE, f'Поле «{field_label}» заполнено неверно.')
    normalized = ' '.join(value.split())
    if not normalized:
        raise _error(ERROR_INVALID_SOURCE, f'Поле «{field_label}» обязательно.')
    return normalized


def _normalize_date(value, field_label):
    if isinstance(value, datetime):
        raise _error(ERROR_INVALID_SOURCE, f'Поле «{field_label}» должно содержать дату.')
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value.strip())
        except ValueError as error:
            raise _error(
                ERROR_INVALID_SOURCE,
                f'Поле «{field_label}» должно содержать дату в формате ГГГГ-ММ-ДД.',
            ) from error
        return parsed
    raise _error(ERROR_INVALID_SOURCE, f'Поле «{field_label}» должно содержать дату.')


def _normalize_sha256(value, field_label):
    if not isinstance(value, str):
        raise _error(ERROR_INVALID_SOURCE, f'Поле «{field_label}» заполнено неверно.')
    normalized = value.strip().lower()
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise _error(ERROR_INVALID_SOURCE, f'Поле «{field_label}» должно содержать SHA-256.')
    return normalized


def _build_source_snapshot(
    *,
    order_number,
    order_date,
    effective_from,
    order_document_sha256,
    schedule_designation,
    schedule_document_sha256,
):
    normalized_order_date = _normalize_date(order_date, 'Дата приказа')
    normalized_effective_from = _normalize_date(effective_from, 'Дата начала действия')
    if normalized_order_date > normalized_effective_from:
        raise _error(
            ERROR_INVALID_SOURCE,
            'Дата приказа не может быть позже даты начала его действия.',
        )
    snapshot = {
        'source_kind': _SOURCE_KIND,
        'order': {
            'number': _normalize_required_text(order_number, 'Номер приказа'),
            'date': normalized_order_date.isoformat(),
            'effective_from': normalized_effective_from.isoformat(),
            'document_sha256': _normalize_sha256(
                order_document_sha256,
                'SHA-256 приказа',
            ),
        },
        'schedule': {
            'designation': _normalize_required_text(
                schedule_designation,
                'Обозначение графика',
            ),
            'document_sha256': _normalize_sha256(
                schedule_document_sha256,
                'SHA-256 графика',
            ),
        },
    }
    return snapshot, normalized_effective_from


def _canonical_fingerprint(snapshot):
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()


def _normalize_brigade_phases(brigade_phases, *, brigade_count):
    if isinstance(brigade_count, bool) or not isinstance(brigade_count, int) or brigade_count < 1:
        raise _error(
            ERROR_INVALID_BRIGADE_SET,
            'В графике должно быть указано положительное количество бригад.',
        )
    if (
        not isinstance(brigade_phases, Sequence)
        or isinstance(brigade_phases, (str, bytes, bytearray))
    ):
        raise _error(
            ERROR_INVALID_BRIGADE_SET,
            'Фазы бригад должны быть переданы структурированным списком.',
        )

    normalized = []
    seen_numbers = set()
    allowed_phases = set(WatchPeriodBrigadePhaseRow.Phase.values)
    for item in brigade_phases:
        if not isinstance(item, Mapping) or set(item) != _ROW_KEYS:
            raise _error(
                ERROR_INVALID_BRIGADE_SET,
                'Каждая строка должна содержать только номер бригады и фазу.',
            )
        brigade_number = item['brigade_number']
        if isinstance(brigade_number, bool) or not isinstance(brigade_number, int):
            raise _error(ERROR_INVALID_BRIGADE_SET, 'Номер бригады должен быть целым числом.')
        phase = item['phase']
        if not isinstance(phase, str):
            raise _error(ERROR_INVALID_BRIGADE_SET, 'Фаза бригады указана неверно.')
        phase = phase.strip().lower()
        if phase not in allowed_phases:
            raise _error(
                ERROR_INVALID_BRIGADE_SET,
                'Фаза бригады должна быть day, night или off.',
            )
        if brigade_number in seen_numbers:
            raise _error(ERROR_INVALID_BRIGADE_SET, 'Номера бригад не должны повторяться.')
        seen_numbers.add(brigade_number)
        normalized.append((brigade_number, phase))

    expected_numbers = set(range(1, brigade_count + 1))
    if seen_numbers != expected_numbers:
        raise _error(
            ERROR_INVALID_BRIGADE_SET,
            'Нужно указать фазы для всех бригад графика без пропусков и лишних строк.',
        )
    return tuple(sorted(normalized))


def _access_plan(actor_access_id):
    plan = (
        EmployeeAccess.objects.filter(pk=actor_access_id)
        .values('pk', 'employee_id', 'role_id')
        .first()
    )
    if plan is None:
        raise _error(ERROR_ACCESS_NOT_FOUND, 'Точный доступ табельщика не найден.')
    return plan


def _lock_timekeeper_access(plan):
    try:
        employee = (
            Employee.objects.select_for_update(of=('self',))
            .get(pk=plan['employee_id'])
        )
    except Employee.DoesNotExist as error:
        raise _error(ERROR_ACCESS_NOT_FOUND, 'Сотрудник доступа не найден.') from error
    try:
        access = (
            EmployeeAccess.objects.select_for_update(of=('self',))
            .select_related('employee', 'role')
            .get(pk=plan['pk'])
        )
    except EmployeeAccess.DoesNotExist as error:
        raise _error(ERROR_ACCESS_NOT_FOUND, 'Точный доступ табельщика не найден.') from error

    if access.employee_id != employee.pk or access.role_id != plan['role_id']:
        raise _error(ERROR_ACCESS_NOT_FOUND, 'Точный доступ табельщика изменился.')
    if access.status == EmployeeAccess.Status.BLOCKED:
        raise _error(ERROR_ACCESS_BLOCKED, 'Доступ табельщика заблокирован.')
    if access.status != EmployeeAccess.Status.ACTIVATED or not access.is_active:
        raise _error(ERROR_ACCESS_INACTIVE, 'Доступ табельщика неактивен.')
    if employee.status != Employee.Status.ACTIVE or not employee.is_active:
        raise _error(ERROR_EMPLOYEE_INACTIVE, 'Сотрудник табельщика неактивен.')
    if not access.role.is_active or access.role.code not in _ACTOR_ROLE_CODES:
        raise _error(
            ERROR_ACCESS_WRONG_ROLE,
            'Доступ не принадлежит активной роли табельщика или администратора.',
        )
    return access


def _lock_watch_period(watch_period_id):
    try:
        return WatchPeriod.objects.select_for_update(of=('self',)).get(pk=watch_period_id)
    except WatchPeriod.DoesNotExist as error:
        raise _error(ERROR_WATCH_PERIOD_NOT_FOUND, 'Период вахты не найден.') from error


def _lock_work_schedule(work_schedule_id, *, require_active=True):
    try:
        schedule = WorkSchedule.objects.select_for_update(of=('self',)).get(pk=work_schedule_id)
    except WorkSchedule.DoesNotExist as error:
        raise _error(ERROR_WORK_SCHEDULE_NOT_FOUND, 'График работы не найден.') from error
    if require_active and not schedule.is_active:
        raise _error(ERROR_WORK_SCHEDULE_INACTIVE, 'График работы неактивен.')
    return schedule


def _lock_existing_graph(*, watch_period, work_schedule):
    versions = list(
        WatchPeriodBrigadePhaseVersion._base_manager.select_for_update(of=('self',))
        .filter(watch_period=watch_period, work_schedule=work_schedule)
        .order_by('version_number', 'pk')
    )
    rows = list(
        WatchPeriodBrigadePhaseRow._base_manager.select_for_update(of=('self',))
        .filter(version_id__in=[version.pk for version in versions])
        .order_by('version_id', 'brigade_number', 'pk')
    )
    return versions, rows


def _rows_by_version(versions, rows, *, brigade_count):
    grouped = {version.pk: [] for version in versions}
    for row in rows:
        if row.version_id not in grouped:
            raise _error(ERROR_INCONSISTENT_GRAPH, 'Строка календаря не связана с версией.')
        grouped[row.version_id].append((row.brigade_number, row.phase))

    expected_numbers = set(range(1, brigade_count + 1))
    allowed_phases = set(WatchPeriodBrigadePhaseRow.Phase.values)
    for version in versions:
        version_rows = grouped[version.pk]
        numbers = [brigade_number for brigade_number, _phase in version_rows]
        if (
            len(numbers) != len(set(numbers))
            or set(numbers) != expected_numbers
            or any(phase not in allowed_phases for _number, phase in version_rows)
        ):
            raise _error(
                ERROR_INCONSISTENT_GRAPH,
                'Существующая версия календаря содержит неполный или повреждённый набор строк.',
            )
        grouped[version.pk] = tuple(sorted(version_rows))
    return grouped


def _validate_versions(versions):
    numbers = [version.version_number for version in versions]
    if len(numbers) != len(set(numbers)) or any(number < 1 for number in numbers):
        raise _error(ERROR_INCONSISTENT_GRAPH, 'Нумерация версий календаря повреждена.')
    confirmed = [
        version
        for version in versions
        if version.status == WatchPeriodBrigadePhaseVersion.Status.CONFIRMED
    ]
    if len(confirmed) > 1:
        raise _error(ERROR_INCONSISTENT_GRAPH, 'Найдено несколько действующих версий календаря.')
    return confirmed[0] if confirmed else None


def _trusted_insert_version(version):
    version.full_clean()
    models.QuerySet.bulk_create(
        WatchPeriodBrigadePhaseVersion._base_manager.all(),
        [version],
    )
    return version


def _trusted_insert_rows(rows):
    for row in rows:
        row.full_clean()
    models.QuerySet.bulk_create(WatchPeriodBrigadePhaseRow._base_manager.all(), rows)
    return rows


def _create_watch_period_brigade_phase_draft_once(
    *,
    watch_period_id,
    work_schedule_id,
    actor_access_id,
    order_number,
    order_date,
    effective_from,
    order_document_sha256,
    schedule_designation,
    schedule_document_sha256,
    brigade_phases,
):
    actor_access = _lock_timekeeper_access(_access_plan(actor_access_id))
    watch_period = _lock_watch_period(watch_period_id)
    work_schedule = _lock_work_schedule(work_schedule_id)

    source_snapshot, normalized_effective_from = _build_source_snapshot(
        order_number=order_number,
        order_date=order_date,
        effective_from=effective_from,
        order_document_sha256=order_document_sha256,
        schedule_designation=schedule_designation,
        schedule_document_sha256=schedule_document_sha256,
    )
    if normalized_effective_from > watch_period.starts_on:
        raise _error(
            ERROR_SOURCE_NOT_EFFECTIVE,
            'Официальный источник действует позже начала периода вахты.',
        )
    normalized_phases = _normalize_brigade_phases(
        brigade_phases,
        brigade_count=work_schedule.brigade_count,
    )
    source_fingerprint = _canonical_fingerprint(source_snapshot)

    versions, locked_rows = _lock_existing_graph(
        watch_period=watch_period,
        work_schedule=work_schedule,
    )
    current_confirmed = _validate_versions(versions)
    rows_by_version = _rows_by_version(
        versions,
        locked_rows,
        brigade_count=work_schedule.brigade_count,
    )

    for version in versions:
        if _canonical_fingerprint(version.source_snapshot) != version.source_fingerprint:
            raise _error(
                ERROR_INCONSISTENT_GRAPH,
                'Снимок источника существующей версии не соответствует fingerprint.',
            )
        if (
            version.status == WatchPeriodBrigadePhaseVersion.Status.DRAFT
            and version.created_by_access_id == actor_access.pk
            and version.source_snapshot == source_snapshot
            and version.source_fingerprint == source_fingerprint
            and rows_by_version[version.pk] == normalized_phases
        ):
            return version

    version = WatchPeriodBrigadePhaseVersion(
        watch_period=watch_period,
        work_schedule=work_schedule,
        version_number=max((item.version_number for item in versions), default=0) + 1,
        status=WatchPeriodBrigadePhaseVersion.Status.DRAFT,
        based_on_version=current_confirmed,
        created_by_access=actor_access,
        confirmed_by_access=None,
        superseded_by_access=None,
        confirmed_at=None,
        superseded_at=None,
        source_snapshot=source_snapshot,
        source_fingerprint=source_fingerprint,
    )
    _trusted_insert_version(version)
    rows = [
        WatchPeriodBrigadePhaseRow(
            version=version,
            brigade_number=brigade_number,
            phase=phase,
        )
        for brigade_number, phase in normalized_phases
    ]
    _trusted_insert_rows(rows)
    return version


def create_watch_period_brigade_phase_draft(
    *,
    watch_period_id,
    work_schedule_id,
    actor_access_id,
    order_number,
    order_date,
    effective_from,
    order_document_sha256,
    schedule_designation,
    schedule_document_sha256,
    brigade_phases,
):
    """Create or return the exact immutable draft for a timekeeper and source."""
    try:
        with transaction.atomic():
            return _create_watch_period_brigade_phase_draft_once(
                watch_period_id=watch_period_id,
                work_schedule_id=work_schedule_id,
                actor_access_id=actor_access_id,
                order_number=order_number,
                order_date=order_date,
                effective_from=effective_from,
                order_document_sha256=order_document_sha256,
                schedule_designation=schedule_designation,
                schedule_document_sha256=schedule_document_sha256,
                brigade_phases=brigade_phases,
            )
    except BrigadePhaseCalendarError:
        raise
    except (IntegrityError, ValidationError) as error:
        raise _error(
            ERROR_INCONSISTENT_GRAPH,
            'Не удалось атомарно сохранить календарь фаз. Повторите операцию.',
        ) from error


def _version_plan(version_id):
    return (
        WatchPeriodBrigadePhaseVersion._base_manager.filter(pk=version_id)
        .values('pk', 'watch_period_id', 'work_schedule_id')
        .first()
    )


def _normalize_official_designation(value):
    if not isinstance(value, str):
        raise _error(
            ERROR_SOURCE_INVALID,
            'Обозначение официального графика заполнено неверно.',
        )
    normalized = unicodedata.normalize('NFC', value)
    normalized = ' '.join(normalized.split()).casefold()
    normalized = re.sub(r'\s*№\s*', '№', normalized)
    normalized = re.sub(r'\s*/\s*', '/', normalized)
    return normalized


def _validate_confirmation_source(*, version, watch_period):
    snapshot = version.source_snapshot
    if not isinstance(snapshot, Mapping) or set(snapshot) != _SOURCE_KEYS:
        raise _error(ERROR_SOURCE_INVALID, 'Снимок официального источника повреждён.')
    order = snapshot.get('order')
    schedule = snapshot.get('schedule')
    if (
        snapshot.get('source_kind') != _SOURCE_KIND
        or not isinstance(order, Mapping)
        or set(order) != _ORDER_KEYS
        or not isinstance(schedule, Mapping)
        or set(schedule) != _SCHEDULE_KEYS
    ):
        raise _error(ERROR_SOURCE_INVALID, 'Снимок официального источника повреждён.')
    try:
        normalized_snapshot, effective_from = _build_source_snapshot(
            order_number=order['number'],
            order_date=order['date'],
            effective_from=order['effective_from'],
            order_document_sha256=order['document_sha256'],
            schedule_designation=schedule['designation'],
            schedule_document_sha256=schedule['document_sha256'],
        )
    except BrigadePhaseCalendarError as error:
        raise _error(
            ERROR_SOURCE_INVALID,
            'Снимок официального источника содержит некорректные реквизиты.',
        ) from error
    if normalized_snapshot != snapshot:
        raise _error(
            ERROR_SOURCE_INVALID,
            'Снимок официального источника не является каноническим.',
        )
    if effective_from > watch_period.starts_on:
        raise _error(
            ERROR_SOURCE_NOT_EFFECTIVE,
            'Официальный источник действует позже начала периода вахты.',
        )
    if (
        not isinstance(version.source_fingerprint, str)
        or not _SHA256_PATTERN.fullmatch(version.source_fingerprint)
        or _canonical_fingerprint(snapshot) != version.source_fingerprint
    ):
        raise _error(
            ERROR_SOURCE_FINGERPRINT_INVALID,
            'Fingerprint официального источника не соответствует снимку.',
        )
    return snapshot


def _target_rows(versions, locked_rows, *, target, brigade_count):
    version_ids = {version.pk for version in versions}
    if any(row.version_id not in version_ids for row in locked_rows):
        raise _error(
            ERROR_GRAPH_INCONSISTENT,
            'Строка календаря не связана с заблокированным набором версий.',
        )
    rows = [row for row in locked_rows if row.version_id == target.pk]
    expected_numbers = set(range(1, brigade_count + 1))
    actual_numbers = [row.brigade_number for row in rows]
    actual_number_set = set(actual_numbers)
    allowed_phases = set(WatchPeriodBrigadePhaseRow.Phase.values)

    if (
        len(actual_numbers) < brigade_count
        and len(actual_numbers) == len(actual_number_set)
        and actual_number_set.issubset(expected_numbers)
        and all(row.phase in allowed_phases for row in rows)
    ):
        raise _error(
            ERROR_GRAPH_INCOMPLETE,
            'В draft отсутствуют строки некоторых бригад.',
        )
    if (
        len(actual_numbers) != brigade_count
        or len(actual_numbers) != len(actual_number_set)
        or actual_number_set != expected_numbers
        or any(row.phase not in allowed_phases for row in rows)
    ):
        raise _error(
            ERROR_GRAPH_INCONSISTENT,
            'Строки draft не соответствуют точному составу бригад графика.',
        )
    return tuple(
        sorted(
            (row.brigade_number, row.phase)
            for row in rows
        )
    )


def _validate_confirmation_policy(*, work_schedule, snapshot, rows):
    policy = _CONFIRMATION_POLICIES.get(work_schedule.code)
    if policy is None:
        raise _error(
            ERROR_POLICY_NOT_DEFINED,
            'Для этого графика ещё не утверждена серверная policy подтверждения.',
        )
    if work_schedule.brigade_count != policy['brigade_count']:
        raise _error(
            ERROR_GRAPH_INCONSISTENT,
            'Количество бригад графика не соответствует официальной policy.',
        )
    designation = _normalize_official_designation(
        snapshot['schedule']['designation']
    )
    if designation != policy['designation']:
        raise _error(
            ERROR_SCHEDULE_DESIGNATION_MISMATCH,
            'Обозначение источника не соответствует выбранному официальному графику.',
        )
    phase_counts = Counter(phase for _brigade_number, phase in rows)
    actual_counts = {
        phase: phase_counts.get(phase, 0)
        for phase in WatchPeriodBrigadePhaseRow.Phase.values
    }
    if actual_counts != policy['phase_counts']:
        raise _error(
            ERROR_POLICY_MISMATCH,
            'Распределение DAY/NIGHT/OFF не соответствует официальной policy графика.',
        )


def _trusted_supersede_version(version, *, actor_access, transition_at):
    if version.status != WatchPeriodBrigadePhaseVersion.Status.CONFIRMED:
        raise _error(
            ERROR_GRAPH_INCONSISTENT,
            'Текущая версия перестала быть утверждённой.',
        )
    version.status = WatchPeriodBrigadePhaseVersion.Status.SUPERSEDED
    version.superseded_at = transition_at
    version.superseded_by_access = actor_access
    version.full_clean()
    updated = models.QuerySet.update(
        WatchPeriodBrigadePhaseVersion._base_manager.filter(
            pk=version.pk,
            status=WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
        ),
        status=version.status,
        superseded_at=version.superseded_at,
        superseded_by_access=version.superseded_by_access,
    )
    if updated != 1:
        raise _error(
            ERROR_GRAPH_INCONSISTENT,
            'Не удалось атомарно заменить предыдущую утверждённую версию.',
        )
    return version


def _trusted_confirm_version(version, *, actor_access, transition_at):
    if version.status != WatchPeriodBrigadePhaseVersion.Status.DRAFT:
        raise _error(ERROR_VERSION_NOT_DRAFT, 'Версия перестала быть draft.')
    version.status = WatchPeriodBrigadePhaseVersion.Status.CONFIRMED
    version.confirmed_at = transition_at
    version.confirmed_by_access = actor_access
    version.full_clean()
    updated = models.QuerySet.update(
        WatchPeriodBrigadePhaseVersion._base_manager.filter(
            pk=version.pk,
            status=WatchPeriodBrigadePhaseVersion.Status.DRAFT,
        ),
        status=version.status,
        confirmed_at=version.confirmed_at,
        confirmed_by_access=version.confirmed_by_access,
    )
    if updated != 1:
        raise _error(
            ERROR_GRAPH_INCONSISTENT,
            'Не удалось атомарно подтвердить draft календаря фаз.',
        )
    return version


def _confirm_watch_period_brigade_phase_version_once(*, version_id, actor_access_id):
    plan = _version_plan(version_id)
    actor_access = _lock_timekeeper_access(_access_plan(actor_access_id))
    if plan is None:
        raise _error(ERROR_VERSION_NOT_FOUND, 'Версия календаря фаз не найдена.')

    watch_period = _lock_watch_period(plan['watch_period_id'])
    work_schedule = _lock_work_schedule(
        plan['work_schedule_id'],
        require_active=False,
    )
    versions, locked_rows = _lock_existing_graph(
        watch_period=watch_period,
        work_schedule=work_schedule,
    )
    target = next((version for version in versions if version.pk == plan['pk']), None)
    if (
        target is None
        or target.watch_period_id != watch_period.pk
        or target.work_schedule_id != work_schedule.pk
    ):
        raise _error(ERROR_VERSION_NOT_FOUND, 'Версия календаря фаз изменилась или удалена.')

    current_confirmed = _validate_versions(versions)
    if target.status == WatchPeriodBrigadePhaseVersion.Status.SUPERSEDED:
        raise _error(
            ERROR_VERSION_STALE,
            'Эта версия уже заменена более новой утверждённой версией.',
        )
    if target.status == WatchPeriodBrigadePhaseVersion.Status.CONFIRMED:
        if current_confirmed is target:
            return target
        raise _error(
            ERROR_VERSION_STALE,
            'Эта версия больше не является текущей утверждённой версией.',
        )
    if target.status != WatchPeriodBrigadePhaseVersion.Status.DRAFT:
        raise _error(ERROR_VERSION_NOT_DRAFT, 'Подтвердить можно только draft-версию.')
    if not work_schedule.is_active:
        raise _error(ERROR_WORK_SCHEDULE_INACTIVE, 'График работы неактивен.')

    snapshot = _validate_confirmation_source(
        version=target,
        watch_period=watch_period,
    )
    target_rows = _target_rows(
        versions,
        locked_rows,
        target=target,
        brigade_count=work_schedule.brigade_count,
    )
    _validate_confirmation_policy(
        work_schedule=work_schedule,
        snapshot=snapshot,
        rows=target_rows,
    )

    if current_confirmed is None:
        if target.based_on_version_id is not None:
            raise _error(
                ERROR_VERSION_STALE,
                'Основание draft больше не соответствует текущему календарю.',
            )
    elif target.based_on_version_id != current_confirmed.pk:
        raise _error(
            ERROR_VERSION_STALE,
            'После создания draft была подтверждена другая версия календаря.',
        )

    transition_at = timezone.now()
    if current_confirmed is not None:
        _trusted_supersede_version(
            current_confirmed,
            actor_access=actor_access,
            transition_at=transition_at,
        )
    _trusted_confirm_version(
        target,
        actor_access=actor_access,
        transition_at=transition_at,
    )
    return target


def confirm_watch_period_brigade_phase_version(*, version_id, actor_access_id):
    """Confirm an exact draft and atomically supersede its current lineage base."""
    try:
        with transaction.atomic():
            return _confirm_watch_period_brigade_phase_version_once(
                version_id=version_id,
                actor_access_id=actor_access_id,
            )
    except BrigadePhaseCalendarError:
        raise
    except (IntegrityError, ValidationError) as error:
        raise _error(
            ERROR_GRAPH_INCONSISTENT,
            'Не удалось атомарно подтвердить календарь фаз. Повторите операцию.',
        ) from error


def _read_watch_period(watch_period_id):
    try:
        return WatchPeriod.objects.get(pk=watch_period_id)
    except WatchPeriod.DoesNotExist as error:
        raise _error(ERROR_WATCH_PERIOD_NOT_FOUND, 'Период вахты не найден.') from error


def _read_work_schedule(work_schedule_id):
    try:
        return WorkSchedule.objects.get(pk=work_schedule_id)
    except WorkSchedule.DoesNotExist as error:
        raise _error(ERROR_WORK_SCHEDULE_NOT_FOUND, 'График работы не найден.') from error


def _read_confirmed_version(*, watch_period, work_schedule):
    try:
        version = WatchPeriodBrigadePhaseVersion._base_manager.get(
            watch_period=watch_period,
            work_schedule=work_schedule,
            status=WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
        )
    except WatchPeriodBrigadePhaseVersion.DoesNotExist as error:
        raise _error(
            ERROR_CONFIRMED_VERSION_NOT_FOUND,
            'Для периода и графика нет утверждённой версии календаря фаз.',
        ) from error
    except WatchPeriodBrigadePhaseVersion.MultipleObjectsReturned as error:
        raise _error(
            ERROR_CONFIRMED_VERSION_INCONSISTENT,
            'Для периода и графика найдено несколько утверждённых версий.',
        ) from error
    if (
        version.watch_period_id != watch_period.pk
        or version.work_schedule_id != work_schedule.pk
        or version.status != WatchPeriodBrigadePhaseVersion.Status.CONFIRMED
        or version.created_by_access_id is None
        or version.confirmed_by_access_id is None
        or version.superseded_by_access_id is not None
        or version.confirmed_at is None
        or version.superseded_at is not None
    ):
        raise _error(
            ERROR_CONFIRMED_VERSION_INCONSISTENT,
            'Утверждённая версия имеет несогласованный статус или аудит.',
        )
    try:
        version.full_clean()
    except ValidationError as error:
        raise _error(
            ERROR_CONFIRMED_VERSION_INCONSISTENT,
            'Утверждённая версия не проходит проверку модели.',
        ) from error
    return version


def _normalize_requested_brigade_number(brigade_number, *, brigade_count):
    if (
        isinstance(brigade_number, bool)
        or not isinstance(brigade_number, int)
        or brigade_number < 1
        or brigade_number > brigade_count
    ):
        raise _error(
            ERROR_BRIGADE_NOT_FOUND,
            'Номер бригады не входит в состав выбранного графика.',
        )
    return brigade_number


def resolve_confirmed_brigade_phase(
    *,
    watch_period_id,
    work_schedule_id,
    brigade_number,
):
    """Resolve one phase from the exact, fully revalidated confirmed calendar."""
    watch_period = _read_watch_period(watch_period_id)
    work_schedule = _read_work_schedule(work_schedule_id)
    requested_brigade_number = _normalize_requested_brigade_number(
        brigade_number,
        brigade_count=work_schedule.brigade_count,
    )
    version = _read_confirmed_version(
        watch_period=watch_period,
        work_schedule=work_schedule,
    )
    snapshot = _validate_confirmation_source(
        version=version,
        watch_period=watch_period,
    )
    rows = list(
        WatchPeriodBrigadePhaseRow._base_manager.filter(version=version)
        .order_by('brigade_number', 'pk')
    )
    normalized_rows = _target_rows(
        [version],
        rows,
        target=version,
        brigade_count=work_schedule.brigade_count,
    )
    _validate_confirmation_policy(
        work_schedule=work_schedule,
        snapshot=snapshot,
        rows=normalized_rows,
    )
    resolved_row = next(
        (
            row
            for row in rows
            if row.brigade_number == requested_brigade_number
        ),
        None,
    )
    if resolved_row is None:
        raise _error(
            ERROR_BRIGADE_NOT_FOUND,
            'Строка указанной бригады не найдена в утверждённой версии.',
        )
    return ConfirmedBrigadePhase(
        version_id=version.pk,
        row_id=resolved_row.pk,
        watch_period_id=watch_period.pk,
        work_schedule_id=work_schedule.pk,
        brigade_number=resolved_row.brigade_number,
        phase=resolved_row.phase,
        source_fingerprint=version.source_fingerprint,
    )
