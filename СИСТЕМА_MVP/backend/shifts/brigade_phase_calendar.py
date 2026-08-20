import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction

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

_SHA256_PATTERN = re.compile(r'^[0-9a-f]{64}$')
_SOURCE_KIND = 'official_schedule_order'
_ROW_KEYS = frozenset({'brigade_number', 'phase'})


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
    if not access.role.is_active or access.role.code != 'timekeeper':
        raise _error(ERROR_ACCESS_WRONG_ROLE, 'Доступ не принадлежит активной роли табельщика.')
    return access


def _lock_watch_period(watch_period_id):
    try:
        return WatchPeriod.objects.select_for_update(of=('self',)).get(pk=watch_period_id)
    except WatchPeriod.DoesNotExist as error:
        raise _error(ERROR_WATCH_PERIOD_NOT_FOUND, 'Период вахты не найден.') from error


def _lock_work_schedule(work_schedule_id):
    try:
        schedule = WorkSchedule.objects.select_for_update(of=('self',)).get(pk=work_schedule_id)
    except WorkSchedule.DoesNotExist as error:
        raise _error(ERROR_WORK_SCHEDULE_NOT_FOUND, 'График работы не найден.') from error
    if not schedule.is_active:
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
