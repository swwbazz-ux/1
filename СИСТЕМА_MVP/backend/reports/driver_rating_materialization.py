from __future__ import annotations

import copy
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from assignments.models import AssignmentStatus, EquipmentAssignment
from shifts.models import ShiftType
from users.models import Employee, WatchComposition, WorkSchedule

from .driver_rating_scope_membership import (
    discover_driver_rating_assignment_group_scope,
    discover_driver_rating_group_scope,
    driver_rating_assignment_group_latest_closed_at,
)
from .driver_shift_passport_snapshots import _fingerprint
from .driver_watch_rating import (
    DRIVER_RATING_FORMULA_VERSION,
    build_driver_rating_assignment_group_period,
    build_driver_rating_period,
)
from .models import (
    DriverRatingPeriodMaterializedSnapshot,
    DriverRatingSnapshotRefreshStatus,
    RatingPeriod,
)


logger = logging.getLogger(__name__)

DRIVER_RATING_MATERIALIZED_PAYLOAD_SCHEMA_VERSION = 1
DEFAULT_REFRESH_SECONDS = 300
DEFAULT_SOFT_STALE_SECONDS = 600
DEFAULT_HARD_EXPIRE_SECONDS = 1800
_PROCESS_LOCKS = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class DriverRatingMaterializationError(RuntimeError):
    pass


class DriverRatingSnapshotUnavailable(RuntimeError):
    def __init__(self, code, public_status, *, http_status):
        super().__init__(public_status)
        self.code = code
        self.public_status = public_status
        self.http_status = http_status


@dataclass(frozen=True)
class DriverRatingRefreshResult:
    status: str
    snapshot_id: int | None
    revision: int
    changed: bool


def _positive_setting(name, default):
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def driver_rating_refresh_seconds():
    return _positive_setting(
        'DRIVER_RATING_SNAPSHOT_REFRESH_SECONDS',
        DEFAULT_REFRESH_SECONDS,
    )


def driver_rating_soft_stale_seconds():
    return _positive_setting(
        'DRIVER_RATING_SNAPSHOT_SOFT_STALE_SECONDS',
        DEFAULT_SOFT_STALE_SECONDS,
    )


def driver_rating_hard_expire_seconds():
    return max(
        driver_rating_soft_stale_seconds(),
        _positive_setting(
            'DRIVER_RATING_SNAPSHOT_HARD_EXPIRE_SECONDS',
            DEFAULT_HARD_EXPIRE_SECONDS,
        ),
    )


def normalize_employee_ids(values):
    if values is None:
        return ()
    return tuple(sorted({int(value) for value in values}))


def driver_rating_member_fingerprint(
    member_employee_ids,
    member_latest_closed_at,
):
    try:
        member_employee_ids = normalize_employee_ids(
            member_employee_ids
        )
    except (TypeError, ValueError):
        return ''
    member_id_set = set(member_employee_ids)
    normalized_latest = {}
    if isinstance(member_latest_closed_at, dict):
        for employee_id, value in member_latest_closed_at.items():
            try:
                employee_id = int(employee_id)
            except (TypeError, ValueError):
                continue
            if employee_id not in member_id_set:
                continue
            parsed = parse_datetime(str(value or ''))
            if parsed is None:
                continue
            normalized_latest[str(employee_id)] = parsed.isoformat()
    return _fingerprint({
        'contract': 'driver-rating-materialized-members-v1',
        'employee_ids': member_employee_ids,
        'latest_closed_at': dict(sorted(normalized_latest.items())),
    })


def driver_rating_scope_fingerprint(
    *,
    scope_code,
    rating_period,
    watch_composition,
    shift_type,
    allowed_employee_ids,
    expected_employee_ids,
    formula_version=DRIVER_RATING_FORMULA_VERSION,
):
    if not isinstance(rating_period, RatingPeriod):
        raise TypeError('rating_period должен быть экземпляром RatingPeriod.')
    if not isinstance(watch_composition, WatchComposition):
        raise TypeError(
            'watch_composition должен быть экземпляром WatchComposition.'
        )
    if shift_type not in {ShiftType.DAY, ShiftType.NIGHT}:
        raise ValueError('Тип смены должен быть day или night.')
    scope_code = str(scope_code or '').strip()
    if not scope_code:
        raise ValueError('Техническая область рейтинга не задана.')
    return _fingerprint({
        'contract': 'driver-rating-materialized-scope-v1',
        'scope_code': scope_code,
        'formula_version': formula_version,
        'payload_schema_version': (
            DRIVER_RATING_MATERIALIZED_PAYLOAD_SCHEMA_VERSION
        ),
        'rating_period': {
            'id': rating_period.id,
            'name': rating_period.name,
            'starts_on': rating_period.starts_on,
            'ends_before': rating_period.ends_before,
            'is_active': rating_period.is_active,
            'updated_at': rating_period.updated_at,
        },
        'watch_composition': {
            'id': watch_composition.id,
            'code': watch_composition.code,
            'name': watch_composition.name,
            'is_active': watch_composition.is_active,
        },
        'shift_type': shift_type,
        'allowed_employee_ids': normalize_employee_ids(
            allowed_employee_ids
        ),
        'expected_employee_ids': normalize_employee_ids(
            expected_employee_ids
        ),
    })


def _lock_identity(
    *,
    scope_code,
    rating_period,
    watch_composition,
    shift_type,
):
    return ':'.join((
        str(scope_code),
        str(rating_period.id),
        str(watch_composition.id),
        str(shift_type),
    ))


def _assignment_group_participant_records(participants):
    records = []
    for participant in participants:
        records.append(_participant_group_snapshot_record({
            'employee_id': participant.employee_id,
            'work_schedule_id': participant.work_schedule_id,
            'brigade_number': participant.brigade_number,
            'shift_type': participant.shift_type,
            'equipment_id': participant.equipment_id,
        }))
    return records


def _frozen_assignment_group_participant_records(
    snapshot,
    *,
    work_schedule_id,
    brigade_number,
    shift_type,
):
    values = snapshot.participant_group_snapshots
    if not isinstance(values, list) or not values:
        raise DriverRatingMaterializationError(
            'Опубликованный снимок не содержит исторический состав группы.'
        )
    records = [
        _participant_group_snapshot_record(value)
        for value in values
    ]
    employee_ids = [record['employee_id'] for record in records]
    if len(employee_ids) != len(set(employee_ids)):
        raise DriverRatingMaterializationError(
            'Исторический состав группы содержит повтор сотрудника.'
        )
    if not 1 <= len(records) <= 53:
        raise DriverRatingMaterializationError(
            'Исторический состав группы должен содержать от 1 до 53 '
            'участников.'
        )
    if any(
        record['work_schedule_id'] != work_schedule_id
        or record['brigade_number'] != brigade_number
        or record['shift_type'] != shift_type
        or record['equipment_id'] is None
        for record in records
    ):
        raise DriverRatingMaterializationError(
            'Исторический состав не соответствует ключу группы.'
        )
    return sorted(records, key=lambda record: record['employee_id'])


def _assert_participants_not_frozen_in_another_group(
    *,
    scope_code,
    rating_period,
    row_identity,
    participant_records,
):
    participant_ids = {
        record['employee_id']
        for record in participant_records
    }
    snapshots = (
        DriverRatingPeriodMaterializedSnapshot.objects
        .select_for_update()
        .filter(
            scope_code=scope_code,
            rating_period=rating_period,
            watch_composition__isnull=True,
            revision__gt=0,
        )
        .exclude(
            work_schedule=row_identity['work_schedule'],
            brigade_number=row_identity['brigade_number'],
            shift_type=row_identity['shift_type'],
        )
        .only(
            'id',
            'work_schedule_id',
            'brigade_number',
            'shift_type',
            'participant_group_snapshots',
            'participant_group_fingerprint',
        )
    )
    conflicts = set()
    for other_snapshot in snapshots:
        other_records = _frozen_assignment_group_participant_records(
            other_snapshot,
            work_schedule_id=other_snapshot.work_schedule_id,
            brigade_number=other_snapshot.brigade_number,
            shift_type=other_snapshot.shift_type,
        )
        if (
            not other_snapshot.participant_group_fingerprint
            or other_snapshot.participant_group_fingerprint
            != driver_rating_participant_group_fingerprint(other_records)
        ):
            raise DriverRatingMaterializationError(
                'Исторический состав соседней группы не прошёл '
                'проверку целостности.'
            )
        conflicts.update(
            participant_ids
            & {record['employee_id'] for record in other_records}
        )
    if conflicts:
        raise DriverRatingMaterializationError(
            'Сотрудник уже зафиксирован в другой группе этого периода: '
            + ', '.join(str(value) for value in sorted(conflicts))
            + '.'
        )


def driver_rating_participant_group_fingerprint(participants):
    records = sorted(
        (
            _participant_group_snapshot_record(value)
            for value in participants
        ),
        key=lambda value: value['employee_id'],
    )
    return _fingerprint({
        'contract': 'driver-rating-participant-group-v1',
        'participants': records,
    })


def driver_rating_assignment_group_scope_fingerprint(
    *,
    scope_code,
    rating_period,
    work_schedule,
    brigade_number,
    shift_type,
    participants,
    formula_version=DRIVER_RATING_FORMULA_VERSION,
):
    if not isinstance(rating_period, RatingPeriod):
        raise TypeError('rating_period должен быть экземпляром RatingPeriod.')
    if not isinstance(work_schedule, WorkSchedule):
        raise TypeError('work_schedule должен быть экземпляром WorkSchedule.')
    if shift_type not in {ShiftType.DAY, ShiftType.NIGHT}:
        raise ValueError('Тип смены должен быть day или night.')
    try:
        brigade_number = int(brigade_number)
    except (TypeError, ValueError) as error:
        raise ValueError('Номер бригады группы рейтинга не задан.') from error
    if brigade_number not in Employee.BrigadeNumber.values:
        raise ValueError('У группы рейтинга указан неверный номер бригады.')
    scope_code = str(scope_code or '').strip()
    if not scope_code:
        raise ValueError('Техническая область рейтинга не задана.')
    participant_records = sorted(
        (
            _participant_group_snapshot_record(value)
            for value in participants
        ),
        key=lambda value: value['employee_id'],
    )
    if not 1 <= len(participant_records) <= 53:
        raise ValueError(
            'Состав рейтинга должен содержать от 1 до 53 участников.'
        )
    return _fingerprint({
        'contract': 'driver-rating-materialized-assignment-group-v1',
        'scope_code': scope_code,
        'formula_version': formula_version,
        'payload_schema_version': (
            DRIVER_RATING_MATERIALIZED_PAYLOAD_SCHEMA_VERSION
        ),
        'rating_period': {
            'id': rating_period.id,
            'name': rating_period.name,
            'starts_on': rating_period.starts_on,
            'ends_before': rating_period.ends_before,
            'is_active': rating_period.is_active,
            'updated_at': rating_period.updated_at,
        },
        'group': {
            'work_schedule_id': work_schedule.id,
            'brigade_number': brigade_number,
            'shift_type': shift_type,
        },
        'participants': participant_records,
    })


def _assignment_group_lock_identity(
    *,
    scope_code,
    rating_period,
    work_schedule,
    brigade_number,
    shift_type,
):
    return ':'.join((
        'assignment-period',
        str(scope_code),
        str(rating_period.id),
    ))


@contextmanager
def _driver_rating_refresh_lock(identity):
    if connection.vendor == 'postgresql':
        acquired = False
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s))',
                ['driver-rating-materialization', identity],
            )
            acquired = bool(cursor.fetchone()[0])
        try:
            yield acquired
        finally:
            if acquired:
                with connection.cursor() as cursor:
                    cursor.execute(
                        (
                            'SELECT pg_advisory_unlock('
                            'hashtext(%s), hashtext(%s))'
                        ),
                        ['driver-rating-materialization', identity],
                    )
        return

    with _PROCESS_LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.setdefault(
            identity,
            threading.Lock(),
        )
    acquired = process_lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            process_lock.release()


def _set_repeatable_read_if_available():
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        cursor.execute(
            'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'
        )


def _prepare_materialized_payload(
    rating,
    *,
    rating_period,
    watch_composition,
    shift_type,
):
    if not isinstance(rating, dict):
        raise DriverRatingMaterializationError(
            'Формула рейтинга вернула неподдерживаемый результат.'
        )
    payload = copy.deepcopy(rating)
    payload.pop('generated_at', None)
    payload.pop('available_rating_periods', None)
    payload.pop('available_watch_compositions', None)
    payload.update({
        'official': False,
        'official_rating_eligible': False,
        'scope_type': 'rating_period',
    })
    if payload.get('formula_version') != DRIVER_RATING_FORMULA_VERSION:
        raise DriverRatingMaterializationError(
            'Версия формулы в готовом результате не совпадает с сервером.'
        )
    if payload.get('shift_type') != shift_type:
        raise DriverRatingMaterializationError(
            'Тип смены в готовом результате не совпадает с группой.'
        )
    period_payload = payload.get('rating_period')
    composition_payload = payload.get('watch_composition')
    if (
        not isinstance(period_payload, dict)
        or period_payload.get('id') != rating_period.id
    ):
        raise DriverRatingMaterializationError(
            'Период в готовом результате не совпадает с группой.'
        )
    if (
        not isinstance(composition_payload, dict)
        or composition_payload.get('id') != watch_composition.id
    ):
        raise DriverRatingMaterializationError(
            'Состав в готовом результате не совпадает с группой.'
        )
    if payload.get('official') is not False:
        raise DriverRatingMaterializationError(
            'Рабочий снимок рейтинга не может быть официальным.'
        )
    return payload


def _prepare_assignment_group_materialized_payload(
    rating,
    *,
    rating_period,
    work_schedule,
    brigade_number,
    shift_type,
    scope_fingerprint,
):
    if not isinstance(rating, dict):
        raise DriverRatingMaterializationError(
            'Формула рейтинга вернула неподдерживаемый результат.'
        )
    payload = copy.deepcopy(rating)
    payload.pop('generated_at', None)
    payload.pop('available_rating_periods', None)
    payload.pop('available_watch_compositions', None)
    payload.update({
        'official': False,
        'official_rating_eligible': False,
        'scope_type': 'rating_period',
        'scope_fingerprint': scope_fingerprint,
    })
    if payload.get('formula_version') != DRIVER_RATING_FORMULA_VERSION:
        raise DriverRatingMaterializationError(
            'Версия формулы в готовом результате не совпадает с сервером.'
        )
    if payload.get('shift_type') != shift_type:
        raise DriverRatingMaterializationError(
            'Тип смены в готовом результате не совпадает с группой.'
        )
    period_payload = payload.get('rating_period')
    group_payload = payload.get('group_identity')
    schedule_payload = (
        group_payload.get('work_schedule')
        if isinstance(group_payload, dict)
        else None
    )
    if (
        not isinstance(period_payload, dict)
        or period_payload.get('id') != rating_period.id
    ):
        raise DriverRatingMaterializationError(
            'Период в готовом результате не совпадает с группой.'
        )
    if (
        not isinstance(group_payload, dict)
        or not isinstance(schedule_payload, dict)
        or schedule_payload.get('id') != work_schedule.id
        or group_payload.get('brigade_number') != brigade_number
        or group_payload.get('shift_type') != shift_type
    ):
        raise DriverRatingMaterializationError(
            'График, бригада или смена результата не совпадают с группой.'
        )
    if payload.get('official') is not False:
        raise DriverRatingMaterializationError(
            'Рабочий снимок рейтинга не может быть официальным.'
        )
    return payload


def _normalize_member_metadata(
    payload,
    *,
    expected_employee_ids,
    member_employee_ids,
    member_latest_closed_at,
):
    payload_employee_ids = {
        int(entry['employee_id'])
        for entry in payload.get('entries', ())
        if isinstance(entry, dict) and entry.get('employee_id') is not None
    }
    members = set(normalize_employee_ids(expected_employee_ids))
    members.update(normalize_employee_ids(member_employee_ids))
    members.update(payload_employee_ids)
    normalized_latest = {}
    for employee_id, value in (member_latest_closed_at or {}).items():
        try:
            normalized_employee_id = int(employee_id)
        except (TypeError, ValueError):
            continue
        if normalized_employee_id not in members:
            continue
        parsed = (
            value
            if hasattr(value, 'isoformat')
            else parse_datetime(str(value or ''))
        )
        if parsed is None:
            continue
        normalized_latest[str(normalized_employee_id)] = (
            parsed.isoformat()
        )
    return sorted(members), dict(sorted(normalized_latest.items()))


def _participant_group_snapshot_record(value):
    if not isinstance(value, dict):
        raise DriverRatingMaterializationError(
            'Исторические данные участника имеют неверный формат.'
        )
    try:
        employee_id = int(value['employee_id'])
    except (KeyError, TypeError, ValueError) as error:
        raise DriverRatingMaterializationError(
            'В исторических данных участника отсутствует employee_id.'
        ) from error
    if employee_id <= 0:
        raise DriverRatingMaterializationError(
            'В исторических данных участника указан неверный employee_id.'
        )

    def nullable_id(field_name):
        raw_value = value.get(field_name)
        if raw_value is None:
            return None
        try:
            normalized = int(raw_value)
        except (TypeError, ValueError) as error:
            raise DriverRatingMaterializationError(
                f'Поле {field_name} в исторических данных имеет неверный формат.'
            ) from error
        if normalized <= 0:
            raise DriverRatingMaterializationError(
                f'Поле {field_name} в исторических данных должно быть положительным.'
            )
        return normalized

    brigade_number = value.get('brigade_number')
    if brigade_number is not None:
        try:
            brigade_number = int(brigade_number)
        except (TypeError, ValueError) as error:
            raise DriverRatingMaterializationError(
                'Номер бригады в исторических данных имеет неверный формат.'
            ) from error
    shift_type = str(value.get('shift_type') or '')
    if shift_type not in {ShiftType.DAY, ShiftType.NIGHT}:
        raise DriverRatingMaterializationError(
            'Тип смены в исторических данных должен быть day или night.'
        )
    return {
        'employee_id': employee_id,
        'work_schedule_id': nullable_id('work_schedule_id'),
        'brigade_number': brigade_number,
        'shift_type': shift_type,
        'equipment_id': nullable_id('equipment_id'),
    }


def _current_participant_group_snapshots(member_employee_ids, *, shift_type):
    employee_ids = normalize_employee_ids(member_employee_ids)
    if not employee_ids:
        return []
    employee_rows = {
        row['id']: row
        for row in Employee.objects.filter(id__in=employee_ids).values(
            'id',
            'work_schedule_id',
            'brigade_number',
        )
    }
    missing_employee_ids = sorted(set(employee_ids) - set(employee_rows))
    if missing_employee_ids:
        raise DriverRatingMaterializationError(
            'Не найдены сотрудники для исторической фиксации группы: '
            + ', '.join(str(value) for value in missing_employee_ids)
            + '.'
        )
    assignment_rows = list(
        EquipmentAssignment.objects.filter(
            employee_id__in=employee_ids,
            role__code='driver',
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
            shift__isnull=True,
            shift_type=shift_type,
        )
        .order_by('employee_id', 'id')
        .values('employee_id', 'equipment_id', 'shift_type')
    )
    assignments_by_employee = {}
    for row in assignment_rows:
        employee_id = int(row['employee_id'])
        if employee_id in assignments_by_employee:
            raise DriverRatingMaterializationError(
                'У сотрудника найдено несколько действующих назначений.'
            )
        assignments_by_employee[employee_id] = row

    snapshots = []
    for employee_id in employee_ids:
        employee_row = employee_rows[employee_id]
        assignment_row = assignments_by_employee.get(employee_id)
        snapshots.append({
            'employee_id': employee_id,
            'work_schedule_id': employee_row['work_schedule_id'],
            'brigade_number': employee_row['brigade_number'],
            'shift_type': shift_type,
            'equipment_id': (
                assignment_row['equipment_id']
                if assignment_row is not None
                else None
            ),
        })
    return snapshots


def _merge_participant_group_snapshots(existing, current):
    if existing is None:
        existing = []
    if not isinstance(existing, list):
        raise DriverRatingMaterializationError(
            'Исторические данные группы имеют неверный формат.'
        )
    merged = {}
    for value in existing:
        record = _participant_group_snapshot_record(value)
        if record['employee_id'] in merged:
            raise DriverRatingMaterializationError(
                'Исторические данные группы содержат повтор сотрудника.'
            )
        merged[record['employee_id']] = record
    for value in current:
        record = _participant_group_snapshot_record(value)
        merged.setdefault(record['employee_id'], record)
    return [merged[employee_id] for employee_id in sorted(merged)]


def _record_refresh_failure(
    *,
    scope_code,
    rating_period,
    watch_composition,
    shift_type,
    scope_fingerprint,
    error,
):
    attempted_at = timezone.now()
    identity = {
        'scope_code': scope_code,
        'rating_period': rating_period,
        'watch_composition': watch_composition,
        'shift_type': shift_type,
    }
    with transaction.atomic():
        snapshot = (
            DriverRatingPeriodMaterializedSnapshot.objects
            .select_for_update()
            .filter(**identity)
            .first()
        )
        is_new = snapshot is None
        if snapshot is None:
            snapshot = DriverRatingPeriodMaterializedSnapshot(
                **identity,
                formula_version=DRIVER_RATING_FORMULA_VERSION,
                payload_schema_version=(
                    DRIVER_RATING_MATERIALIZED_PAYLOAD_SCHEMA_VERSION
                ),
                scope_fingerprint=scope_fingerprint,
            )
        snapshot.last_attempt_at = attempted_at
        snapshot.last_failure_at = attempted_at
        snapshot.last_refresh_status = (
            DriverRatingSnapshotRefreshStatus.FAILED
        )
        snapshot.failure_code = 'calculation_failed'
        snapshot.consecutive_failure_count = (
            int(snapshot.consecutive_failure_count or 0) + 1
        )
        snapshot.last_error = (
            f'{type(error).__name__}: {error}'
        )[:500]
        if is_new:
            snapshot.save()
        else:
            snapshot.save(update_fields=[
                'last_attempt_at',
                'last_failure_at',
                'last_refresh_status',
                'failure_code',
                'consecutive_failure_count',
                'last_error',
                'updated_at',
            ])
    return snapshot


def _record_assignment_group_refresh_failure(
    *,
    scope_code,
    rating_period,
    work_schedule,
    brigade_number,
    shift_type,
    scope_fingerprint,
    error,
):
    attempted_at = timezone.now()
    identity = {
        'scope_code': scope_code,
        'rating_period': rating_period,
        'work_schedule': work_schedule,
        'brigade_number': brigade_number,
        'watch_composition': None,
        'shift_type': shift_type,
    }
    with transaction.atomic():
        snapshot = (
            DriverRatingPeriodMaterializedSnapshot.objects
            .select_for_update()
            .filter(**identity)
            .first()
        )
        is_new = snapshot is None
        if snapshot is None:
            snapshot = DriverRatingPeriodMaterializedSnapshot(
                **identity,
                formula_version=DRIVER_RATING_FORMULA_VERSION,
                payload_schema_version=(
                    DRIVER_RATING_MATERIALIZED_PAYLOAD_SCHEMA_VERSION
                ),
                scope_fingerprint=scope_fingerprint,
            )
        snapshot.last_attempt_at = attempted_at
        snapshot.last_failure_at = attempted_at
        snapshot.last_refresh_status = (
            DriverRatingSnapshotRefreshStatus.FAILED
        )
        snapshot.failure_code = 'calculation_failed'
        snapshot.consecutive_failure_count = (
            int(snapshot.consecutive_failure_count or 0) + 1
        )
        snapshot.last_error = f'{type(error).__name__}: {error}'[:500]
        if is_new:
            snapshot.save()
        else:
            snapshot.save(update_fields=[
                'last_attempt_at',
                'last_failure_at',
                'last_refresh_status',
                'failure_code',
                'consecutive_failure_count',
                'last_error',
                'updated_at',
            ])
    return snapshot


def refresh_driver_rating_group(
    rating_period,
    watch_composition,
    *,
    shift_type,
    scope_code=None,
):
    scope_code = (
        str(scope_code or getattr(settings, 'PORTAL_SITE_CODE', '')).strip()
    )
    if connection.vendor == 'postgresql' and connection.in_atomic_block:
        raise DriverRatingMaterializationError(
            'Обновление общего снимка требует отдельной верхнеуровневой '
            'транзакции PostgreSQL.'
        )
    rating_period_id = rating_period.id
    watch_composition_id = watch_composition.id
    scope_fingerprint = ''
    identity = _lock_identity(
        scope_code=scope_code,
        rating_period=rating_period,
        watch_composition=watch_composition,
        shift_type=shift_type,
    )

    with _driver_rating_refresh_lock(identity) as acquired:
        if not acquired:
            return DriverRatingRefreshResult(
                status='locked',
                snapshot_id=None,
                revision=0,
                changed=False,
            )
        try:
            with transaction.atomic():
                _set_repeatable_read_if_available()
                rating_period = RatingPeriod.objects.get(
                    pk=rating_period_id,
                )
                watch_composition = WatchComposition.objects.get(
                    pk=watch_composition_id,
                )
                group_scope = discover_driver_rating_group_scope(
                    rating_period,
                    watch_composition,
                    shift_type=shift_type,
                )
                scope_fingerprint = driver_rating_scope_fingerprint(
                    scope_code=scope_code,
                    rating_period=rating_period,
                    watch_composition=watch_composition,
                    shift_type=shift_type,
                    allowed_employee_ids=(
                        group_scope.allowed_employee_ids
                    ),
                    expected_employee_ids=(
                        group_scope.expected_employee_ids
                    ),
                )
                rating = build_driver_rating_period(
                    rating_period,
                    watch_composition,
                    shift_type=shift_type,
                    allowed_employee_ids=(
                        group_scope.allowed_employee_ids
                    ),
                    expected_employee_ids=(
                        group_scope.expected_employee_ids
                    ),
                )
                payload = _prepare_materialized_payload(
                    rating,
                    rating_period=rating_period,
                    watch_composition=watch_composition,
                    shift_type=shift_type,
                )
                (
                    normalized_members,
                    normalized_latest,
                ) = _normalize_member_metadata(
                    payload,
                    expected_employee_ids=(
                        group_scope.expected_employee_ids
                    ),
                    member_employee_ids=(
                        group_scope.historical_employee_ids
                    ),
                    member_latest_closed_at=(
                        group_scope.latest_closed_at
                    ),
                )
                current_participant_group_snapshots = (
                    _current_participant_group_snapshots(
                        normalized_members,
                        shift_type=shift_type,
                    )
                )
                payload_fingerprint = _fingerprint(payload)
                member_fingerprint = driver_rating_member_fingerprint(
                    normalized_members,
                    normalized_latest,
                )
                source_fingerprint = str(
                    payload.get('source_fingerprint') or ''
                )
                shift_score_fingerprint = str(
                    payload.get('shift_score_fingerprint') or ''
                )
                succeeded_at = timezone.now()
                row_identity = {
                    'scope_code': scope_code,
                    'rating_period': rating_period,
                    'watch_composition': watch_composition,
                    'shift_type': shift_type,
                }
                snapshot = (
                    DriverRatingPeriodMaterializedSnapshot.objects
                    .select_for_update()
                    .filter(**row_identity)
                    .first()
                )
                if snapshot is None:
                    snapshot = DriverRatingPeriodMaterializedSnapshot(
                        **row_identity
                    )
                content_changed = any((
                    snapshot.revision == 0,
                    snapshot.formula_version
                    != DRIVER_RATING_FORMULA_VERSION,
                    snapshot.payload_schema_version
                    != DRIVER_RATING_MATERIALIZED_PAYLOAD_SCHEMA_VERSION,
                    snapshot.scope_fingerprint != scope_fingerprint,
                    snapshot.source_fingerprint != source_fingerprint,
                    snapshot.shift_score_fingerprint
                    != shift_score_fingerprint,
                    snapshot.payload_fingerprint
                    != payload_fingerprint,
                    snapshot.member_fingerprint
                    != member_fingerprint,
                    snapshot.member_employee_ids
                    != normalized_members,
                    snapshot.member_latest_closed_at
                    != normalized_latest,
                ))
                legacy_without_group_snapshot = (
                    snapshot.pk is not None
                    and snapshot.revision > 0
                    and snapshot.participant_group_snapshots is None
                )
                if legacy_without_group_snapshot:
                    participant_group_snapshots = None
                else:
                    participant_group_snapshots = (
                        _merge_participant_group_snapshots(
                            snapshot.participant_group_snapshots,
                            current_participant_group_snapshots,
                        )
                    )
                changed = content_changed or (
                    snapshot.participant_group_snapshots
                    != participant_group_snapshots
                )
                snapshot.formula_version = DRIVER_RATING_FORMULA_VERSION
                snapshot.payload_schema_version = (
                    DRIVER_RATING_MATERIALIZED_PAYLOAD_SCHEMA_VERSION
                )
                snapshot.scope_fingerprint = scope_fingerprint
                if changed:
                    snapshot.source_fingerprint = source_fingerprint
                    snapshot.shift_score_fingerprint = (
                        shift_score_fingerprint
                    )
                    snapshot.payload_fingerprint = payload_fingerprint
                    snapshot.member_fingerprint = member_fingerprint
                    snapshot.payload = payload
                    snapshot.member_employee_ids = normalized_members
                    snapshot.member_latest_closed_at = normalized_latest
                    snapshot.participant_group_snapshots = (
                        participant_group_snapshots
                    )
                    snapshot.revision = int(snapshot.revision or 0) + 1
                    snapshot.published_at = succeeded_at
                snapshot.last_success_at = succeeded_at
                snapshot.last_attempt_at = succeeded_at
                snapshot.last_refresh_status = (
                    DriverRatingSnapshotRefreshStatus.READY
                )
                snapshot.failure_code = ''
                snapshot.consecutive_failure_count = 0
                snapshot.last_error = ''
                if changed:
                    snapshot.save()
                else:
                    snapshot.save(update_fields=[
                        'last_success_at',
                        'last_attempt_at',
                        'last_refresh_status',
                        'failure_code',
                        'consecutive_failure_count',
                        'last_error',
                        'updated_at',
                    ])
            return DriverRatingRefreshResult(
                status='published' if changed else 'verified',
                snapshot_id=snapshot.id,
                revision=snapshot.revision,
                changed=changed,
            )
        except Exception as error:
            logger.exception(
                (
                    'Не удалось обновить общий снимок рейтинга '
                    '%s/%s/%s/%s.'
                ),
                scope_code,
                rating_period_id,
                watch_composition_id,
                shift_type,
            )
            try:
                _record_refresh_failure(
                    scope_code=scope_code,
                    rating_period=rating_period,
                    watch_composition=watch_composition,
                    shift_type=shift_type,
                    scope_fingerprint=scope_fingerprint,
                    error=error,
                )
            except Exception:
                logger.exception(
                    'Не удалось записать состояние ошибки снимка рейтинга.'
                )
            raise DriverRatingMaterializationError(
                'Общий снимок рейтинга не обновлён.'
            ) from error


def refresh_driver_rating_assignment_group(
    rating_period,
    work_schedule,
    *,
    brigade_number,
    shift_type,
    scope_code=None,
):
    """Материализует группу график + бригада + тип смены."""

    scope_code = (
        str(scope_code or getattr(settings, 'PORTAL_SITE_CODE', '')).strip()
    )
    if connection.vendor == 'postgresql' and connection.in_atomic_block:
        raise DriverRatingMaterializationError(
            'Обновление общего снимка требует отдельной верхнеуровневой '
            'транзакции PostgreSQL.'
        )
    try:
        brigade_number = int(brigade_number)
    except (TypeError, ValueError) as error:
        raise DriverRatingMaterializationError(
            'Номер бригады группы рейтинга не задан.'
        ) from error
    rating_period_id = rating_period.id
    work_schedule_id = work_schedule.id
    scope_fingerprint = ''
    identity = _assignment_group_lock_identity(
        scope_code=scope_code,
        rating_period=rating_period,
        work_schedule=work_schedule,
        brigade_number=brigade_number,
        shift_type=shift_type,
    )

    with _driver_rating_refresh_lock(identity) as acquired:
        if not acquired:
            return DriverRatingRefreshResult(
                status='locked',
                snapshot_id=None,
                revision=0,
                changed=False,
            )
        try:
            with transaction.atomic():
                _set_repeatable_read_if_available()
                rating_period = RatingPeriod.objects.get(
                    pk=rating_period_id,
                )
                work_schedule = WorkSchedule.objects.get(
                    pk=work_schedule_id,
                )
                row_identity = {
                    'scope_code': scope_code,
                    'rating_period': rating_period,
                    'work_schedule': work_schedule,
                    'brigade_number': brigade_number,
                    'watch_composition': None,
                    'shift_type': shift_type,
                }
                snapshot = (
                    DriverRatingPeriodMaterializedSnapshot.objects
                    .select_for_update()
                    .filter(**row_identity)
                    .first()
                )
                if snapshot is not None and snapshot.revision > 0:
                    participant_group_snapshots = (
                        _frozen_assignment_group_participant_records(
                            snapshot,
                            work_schedule_id=work_schedule.id,
                            brigade_number=brigade_number,
                            shift_type=shift_type,
                        )
                    )
                    frozen_participant_fingerprint = (
                        driver_rating_participant_group_fingerprint(
                            participant_group_snapshots
                        )
                    )
                    if (
                        not snapshot.participant_group_fingerprint
                        or snapshot.participant_group_fingerprint
                        != frozen_participant_fingerprint
                        or not isinstance(snapshot.payload, dict)
                        or snapshot.payload.get('scope_fingerprint')
                        != snapshot.scope_fingerprint
                    ):
                        raise DriverRatingMaterializationError(
                            'Исторический состав опубликованного снимка '
                            'не прошёл проверку целостности.'
                        )
                    expected_employee_ids = tuple(
                        record['employee_id']
                        for record in participant_group_snapshots
                    )
                    member_latest_closed_at = (
                        driver_rating_assignment_group_latest_closed_at(
                            rating_period,
                            employee_ids=expected_employee_ids,
                            shift_type=shift_type,
                        )
                    )
                else:
                    group_scope = (
                        discover_driver_rating_assignment_group_scope(
                            rating_period,
                            work_schedule_id=work_schedule.id,
                            brigade_number=brigade_number,
                            shift_type=shift_type,
                        )
                    )
                    participant_group_snapshots = (
                        _assignment_group_participant_records(
                            group_scope.participants
                        )
                    )
                    _assert_participants_not_frozen_in_another_group(
                        scope_code=scope_code,
                        rating_period=rating_period,
                        row_identity=row_identity,
                        participant_records=(
                            participant_group_snapshots
                        ),
                    )
                    expected_employee_ids = (
                        group_scope.expected_employee_ids
                    )
                    member_latest_closed_at = (
                        group_scope.latest_closed_at
                    )
                scope_fingerprint = (
                    driver_rating_assignment_group_scope_fingerprint(
                        scope_code=scope_code,
                        rating_period=rating_period,
                        work_schedule=work_schedule,
                        brigade_number=brigade_number,
                        shift_type=shift_type,
                        participants=participant_group_snapshots,
                    )
                )
                participant_group_fingerprint = (
                    driver_rating_participant_group_fingerprint(
                        participant_group_snapshots
                    )
                )
                rating = build_driver_rating_assignment_group_period(
                    rating_period,
                    work_schedule,
                    brigade_number=brigade_number,
                    shift_type=shift_type,
                    participants=participant_group_snapshots,
                )
                payload = _prepare_assignment_group_materialized_payload(
                    rating,
                    rating_period=rating_period,
                    work_schedule=work_schedule,
                    brigade_number=brigade_number,
                    shift_type=shift_type,
                    scope_fingerprint=scope_fingerprint,
                )
                (
                    normalized_members,
                    normalized_latest,
                ) = _normalize_member_metadata(
                    payload,
                    expected_employee_ids=expected_employee_ids,
                    member_employee_ids=(),
                    member_latest_closed_at=member_latest_closed_at,
                )
                payload_fingerprint = _fingerprint(payload)
                member_fingerprint = driver_rating_member_fingerprint(
                    normalized_members,
                    normalized_latest,
                )
                source_fingerprint = str(
                    payload.get('source_fingerprint') or ''
                )
                shift_score_fingerprint = str(
                    payload.get('shift_score_fingerprint') or ''
                )
                succeeded_at = timezone.now()
                if snapshot is None:
                    snapshot = DriverRatingPeriodMaterializedSnapshot(
                        **row_identity
                    )
                content_changed = any((
                    snapshot.revision == 0,
                    snapshot.formula_version
                    != DRIVER_RATING_FORMULA_VERSION,
                    snapshot.payload_schema_version
                    != DRIVER_RATING_MATERIALIZED_PAYLOAD_SCHEMA_VERSION,
                    snapshot.scope_fingerprint != scope_fingerprint,
                    snapshot.source_fingerprint != source_fingerprint,
                    snapshot.shift_score_fingerprint
                    != shift_score_fingerprint,
                    snapshot.payload_fingerprint != payload_fingerprint,
                    snapshot.member_fingerprint != member_fingerprint,
                    snapshot.member_employee_ids != normalized_members,
                    snapshot.member_latest_closed_at != normalized_latest,
                    snapshot.participant_group_snapshots
                    != participant_group_snapshots,
                    snapshot.participant_group_fingerprint
                    != participant_group_fingerprint,
                ))
                snapshot.formula_version = DRIVER_RATING_FORMULA_VERSION
                snapshot.payload_schema_version = (
                    DRIVER_RATING_MATERIALIZED_PAYLOAD_SCHEMA_VERSION
                )
                snapshot.scope_fingerprint = scope_fingerprint
                if content_changed:
                    snapshot.source_fingerprint = source_fingerprint
                    snapshot.shift_score_fingerprint = (
                        shift_score_fingerprint
                    )
                    snapshot.payload_fingerprint = payload_fingerprint
                    snapshot.member_fingerprint = member_fingerprint
                    snapshot.payload = payload
                    snapshot.member_employee_ids = normalized_members
                    snapshot.member_latest_closed_at = normalized_latest
                    snapshot.participant_group_snapshots = (
                        participant_group_snapshots
                    )
                    snapshot.participant_group_fingerprint = (
                        participant_group_fingerprint
                    )
                    snapshot.revision = int(snapshot.revision or 0) + 1
                    snapshot.published_at = succeeded_at
                snapshot.last_success_at = succeeded_at
                snapshot.last_attempt_at = succeeded_at
                snapshot.last_refresh_status = (
                    DriverRatingSnapshotRefreshStatus.READY
                )
                snapshot.failure_code = ''
                snapshot.consecutive_failure_count = 0
                snapshot.last_error = ''
                if content_changed:
                    snapshot.save()
                else:
                    snapshot.save(update_fields=[
                        'last_success_at',
                        'last_attempt_at',
                        'last_refresh_status',
                        'failure_code',
                        'consecutive_failure_count',
                        'last_error',
                        'updated_at',
                    ])
            return DriverRatingRefreshResult(
                status='published' if content_changed else 'verified',
                snapshot_id=snapshot.id,
                revision=snapshot.revision,
                changed=content_changed,
            )
        except Exception as error:
            logger.exception(
                (
                    'Не удалось обновить снимок рейтинга группы '
                    '%s/%s/%s/%s/%s.'
                ),
                scope_code,
                rating_period_id,
                work_schedule_id,
                brigade_number,
                shift_type,
            )
            try:
                _record_assignment_group_refresh_failure(
                    scope_code=scope_code,
                    rating_period=rating_period,
                    work_schedule=work_schedule,
                    brigade_number=brigade_number,
                    shift_type=shift_type,
                    scope_fingerprint=scope_fingerprint,
                    error=error,
                )
            except Exception:
                logger.exception(
                    'Не удалось записать состояние ошибки снимка рейтинга.'
                )
            raise DriverRatingMaterializationError(
                'Общий снимок рейтинга не обновлён.'
            ) from error


def materialized_driver_rating_rows(
    rating_period,
    *,
    scope_code=None,
):
    scope_code = (
        str(scope_code or getattr(settings, 'PORTAL_SITE_CODE', '')).strip()
    )
    return (
        DriverRatingPeriodMaterializedSnapshot.objects
        .filter(
            scope_code=scope_code,
            rating_period=rating_period,
            watch_composition__isnull=False,
        )
        .select_related('watch_composition')
        .order_by('watch_composition_id', 'shift_type')
    )


def materialized_driver_rating_assignment_group_rows(
    rating_period,
    *,
    scope_code=None,
):
    scope_code = (
        str(scope_code or getattr(settings, 'PORTAL_SITE_CODE', '')).strip()
    )
    return (
        DriverRatingPeriodMaterializedSnapshot.objects
        .filter(
            scope_code=scope_code,
            rating_period=rating_period,
            watch_composition__isnull=True,
            work_schedule__isnull=False,
            brigade_number__isnull=False,
        )
        .select_related('work_schedule')
        .order_by('work_schedule_id', 'brigade_number', 'shift_type')
    )


def _snapshot_unavailable(code, status, http_status):
    raise DriverRatingSnapshotUnavailable(
        code,
        status,
        http_status=http_status,
    )


def get_materialized_driver_rating_period(
    rating_period,
    watch_composition,
    *,
    shift_type,
    allowed_employee_ids,
    expected_employee_ids,
    scope_code=None,
    now=None,
):
    scope_code = (
        str(scope_code or getattr(settings, 'PORTAL_SITE_CODE', '')).strip()
    )
    expected_scope_fingerprint = driver_rating_scope_fingerprint(
        scope_code=scope_code,
        rating_period=rating_period,
        watch_composition=watch_composition,
        shift_type=shift_type,
        allowed_employee_ids=allowed_employee_ids,
        expected_employee_ids=expected_employee_ids,
    )
    snapshot = (
        DriverRatingPeriodMaterializedSnapshot.objects
        .filter(
            scope_code=scope_code,
            rating_period=rating_period,
            watch_composition=watch_composition,
            shift_type=shift_type,
        )
        .first()
    )
    if snapshot is None or snapshot.last_success_at is None:
        _snapshot_unavailable(
            'snapshot_missing',
            (
                'Общий серверный снимок рейтинга ещё не сформирован. '
                'Расчёт выполняется в фоне.'
            ),
            503,
        )
    if (
        snapshot.formula_version != DRIVER_RATING_FORMULA_VERSION
        or snapshot.payload_schema_version
        != DRIVER_RATING_MATERIALIZED_PAYLOAD_SCHEMA_VERSION
    ):
        _snapshot_unavailable(
            'snapshot_formula_mismatch',
            (
                'Готовый снимок относится к другой версии формулы. '
                'Результат временно скрыт до фонового пересчёта.'
            ),
            409,
        )
    if snapshot.scope_fingerprint != expected_scope_fingerprint:
        _snapshot_unavailable(
            'snapshot_scope_mismatch',
            (
                'Состав рейтинговой группы изменился. '
                'Результат временно скрыт до фонового пересчёта.'
            ),
            409,
        )
    if (
        not isinstance(snapshot.payload, dict)
        or not snapshot.payload_fingerprint
        or _fingerprint(snapshot.payload) != snapshot.payload_fingerprint
    ):
        _snapshot_unavailable(
            'snapshot_integrity_failed',
            (
                'Готовый снимок не прошёл проверку целостности. '
                'Результат временно скрыт.'
            ),
            409,
        )
    if (
        not snapshot.member_fingerprint
        or driver_rating_member_fingerprint(
            snapshot.member_employee_ids,
            snapshot.member_latest_closed_at,
        ) != snapshot.member_fingerprint
    ):
        _snapshot_unavailable(
            'snapshot_member_integrity_failed',
            (
                'Состав готового снимка не прошёл проверку целостности. '
                'Результат временно скрыт.'
            ),
            409,
        )
    stored_period = snapshot.payload.get('rating_period')
    stored_composition = snapshot.payload.get('watch_composition')
    if (
        snapshot.payload.get('formula_version')
        != snapshot.formula_version
        or snapshot.payload.get('source_fingerprint', '')
        != snapshot.source_fingerprint
        or snapshot.payload.get('shift_score_fingerprint', '')
        != snapshot.shift_score_fingerprint
        or snapshot.payload.get('official') is not False
        or not isinstance(stored_period, dict)
        or stored_period.get('id') != rating_period.id
        or not isinstance(stored_composition, dict)
        or stored_composition.get('id') != watch_composition.id
        or snapshot.payload.get('shift_type') != shift_type
    ):
        _snapshot_unavailable(
            'snapshot_contract_failed',
            (
                'Готовый снимок не соответствует выбранной группе. '
                'Результат временно скрыт.'
            ),
            409,
        )

    now = now or timezone.now()
    age_seconds = (now - snapshot.last_success_at).total_seconds()
    if age_seconds < -60:
        _snapshot_unavailable(
            'snapshot_time_invalid',
            (
                'Время готового снимка требует проверки. '
                'Результат временно скрыт.'
            ),
            409,
        )
    age_seconds = max(0, int(age_seconds))
    if age_seconds > driver_rating_hard_expire_seconds():
        _snapshot_unavailable(
            'snapshot_expired',
            (
                'Общий снимок рейтинга давно не обновлялся. '
                'Результат скрыт до восстановления фонового расчёта.'
            ),
            409,
        )

    last_attempt_failed = (
        snapshot.last_refresh_status
        == DriverRatingSnapshotRefreshStatus.FAILED
        and snapshot.last_attempt_at is not None
        and snapshot.last_attempt_at >= snapshot.last_success_at
    )
    delayed = (
        age_seconds > driver_rating_soft_stale_seconds()
        or last_attempt_failed
    )
    result = copy.deepcopy(snapshot.payload)
    refresh_seconds = driver_rating_refresh_seconds()
    next_refresh_base = (
        snapshot.last_attempt_at or snapshot.last_success_at
    )
    result.update({
        'generated_at': snapshot.last_success_at.isoformat(),
        'snapshot_status': 'delayed' if delayed else 'fresh',
        'snapshot_revision': snapshot.revision,
        'snapshot_age_seconds': age_seconds,
        'published_at': (
            snapshot.published_at.isoformat()
            if snapshot.published_at
            else None
        ),
        'last_success_at': snapshot.last_success_at.isoformat(),
        'next_refresh_expected_at': (
            next_refresh_base + timedelta(seconds=refresh_seconds)
        ).isoformat(),
    })
    if delayed:
        result['snapshot_warning'] = (
            'Обновление рейтинга задерживается; показан последний '
            'совместимый серверный снимок.'
        )
    return result


def get_materialized_driver_rating_assignment_group(
    rating_period,
    work_schedule,
    *,
    brigade_number,
    shift_type,
    scope_code=None,
    now=None,
):
    """Читает опубликованный assignment-based снимок без live re-scope."""

    if not isinstance(rating_period, RatingPeriod):
        raise TypeError('rating_period должен быть экземпляром RatingPeriod.')
    if not isinstance(work_schedule, WorkSchedule):
        raise TypeError('work_schedule должен быть экземпляром WorkSchedule.')
    try:
        brigade_number = int(brigade_number)
    except (TypeError, ValueError) as error:
        raise ValueError('Номер бригады группы рейтинга не задан.') from error
    if shift_type not in {ShiftType.DAY, ShiftType.NIGHT}:
        raise ValueError('Тип смены должен быть day или night.')
    scope_code = (
        str(scope_code or getattr(settings, 'PORTAL_SITE_CODE', '')).strip()
    )
    snapshot = (
        DriverRatingPeriodMaterializedSnapshot.objects
        .filter(
            scope_code=scope_code,
            rating_period=rating_period,
            work_schedule=work_schedule,
            brigade_number=brigade_number,
            watch_composition__isnull=True,
            shift_type=shift_type,
        )
        .first()
    )
    if snapshot is None or snapshot.last_success_at is None:
        _snapshot_unavailable(
            'snapshot_missing',
            (
                'Общий серверный снимок рейтинга ещё не сформирован. '
                'Расчёт выполняется в фоне.'
            ),
            503,
        )
    if (
        snapshot.formula_version != DRIVER_RATING_FORMULA_VERSION
        or snapshot.payload_schema_version
        != DRIVER_RATING_MATERIALIZED_PAYLOAD_SCHEMA_VERSION
    ):
        _snapshot_unavailable(
            'snapshot_formula_mismatch',
            (
                'Готовый снимок относится к другой версии формулы. '
                'Результат временно скрыт до фонового пересчёта.'
            ),
            409,
        )
    if (
        not snapshot.scope_fingerprint
        or not isinstance(snapshot.payload, dict)
        or snapshot.payload.get('scope_fingerprint')
        != snapshot.scope_fingerprint
    ):
        _snapshot_unavailable(
            'snapshot_scope_mismatch',
            (
                'Состав рейтинговой группы не прошёл проверку. '
                'Результат временно скрыт до фонового пересчёта.'
            ),
            409,
        )
    if (
        not snapshot.payload_fingerprint
        or _fingerprint(snapshot.payload) != snapshot.payload_fingerprint
    ):
        _snapshot_unavailable(
            'snapshot_integrity_failed',
            (
                'Готовый снимок не прошёл проверку целостности. '
                'Результат временно скрыт.'
            ),
            409,
        )
    if (
        not snapshot.member_fingerprint
        or driver_rating_member_fingerprint(
            snapshot.member_employee_ids,
            snapshot.member_latest_closed_at,
        ) != snapshot.member_fingerprint
    ):
        _snapshot_unavailable(
            'snapshot_member_integrity_failed',
            (
                'Состав готового снимка не прошёл проверку целостности. '
                'Результат временно скрыт.'
            ),
            409,
        )
    try:
        participant_records = [
            _participant_group_snapshot_record(value)
            for value in snapshot.participant_group_snapshots
        ]
    except (TypeError, DriverRatingMaterializationError):
        _snapshot_unavailable(
            'snapshot_member_integrity_failed',
            (
                'Исторический состав готового снимка повреждён. '
                'Результат временно скрыт.'
            ),
            409,
        )
    participant_ids = {
        record['employee_id']
        for record in participant_records
    }
    try:
        member_ids = set(normalize_employee_ids(
            snapshot.member_employee_ids
        ))
    except (TypeError, ValueError):
        member_ids = set()
    if (
        len(participant_records) != len(participant_ids)
        or not member_ids.issubset(participant_ids)
        or any(
            record['work_schedule_id'] != work_schedule.id
            or record['brigade_number'] != brigade_number
            or record['shift_type'] != shift_type
            or record['equipment_id'] is None
            for record in participant_records
        )
    ):
        _snapshot_unavailable(
            'snapshot_member_integrity_failed',
            (
                'Исторический состав не соответствует выбранной группе. '
                'Результат временно скрыт.'
            ),
            409,
        )
    if (
        not snapshot.participant_group_fingerprint
        or snapshot.participant_group_fingerprint
        != driver_rating_participant_group_fingerprint(
            participant_records
        )
    ):
        _snapshot_unavailable(
            'snapshot_scope_mismatch',
            (
                'Исторический состав рейтинговой группы изменён. '
                'Результат временно скрыт.'
            ),
            409,
        )
    expected_scope_fingerprint = (
        driver_rating_assignment_group_scope_fingerprint(
            scope_code=scope_code,
            rating_period=rating_period,
            work_schedule=work_schedule,
            brigade_number=brigade_number,
            shift_type=shift_type,
            participants=participant_records,
        )
    )
    if snapshot.scope_fingerprint != expected_scope_fingerprint:
        _snapshot_unavailable(
            'snapshot_scope_mismatch',
            (
                'Исторический состав рейтинговой группы изменён. '
                'Результат временно скрыт.'
            ),
            409,
        )

    stored_period = snapshot.payload.get('rating_period')
    stored_group = snapshot.payload.get('group_identity')
    stored_schedule = (
        stored_group.get('work_schedule')
        if isinstance(stored_group, dict)
        else None
    )
    if (
        snapshot.payload.get('formula_version')
        != snapshot.formula_version
        or snapshot.payload.get('source_fingerprint', '')
        != snapshot.source_fingerprint
        or snapshot.payload.get('shift_score_fingerprint', '')
        != snapshot.shift_score_fingerprint
        or snapshot.payload.get('official') is not False
        or not isinstance(stored_period, dict)
        or stored_period.get('id') != rating_period.id
        or not isinstance(stored_group, dict)
        or not isinstance(stored_schedule, dict)
        or stored_schedule.get('id') != work_schedule.id
        or stored_group.get('brigade_number') != brigade_number
        or stored_group.get('shift_type') != shift_type
        or snapshot.payload.get('shift_type') != shift_type
    ):
        _snapshot_unavailable(
            'snapshot_contract_failed',
            (
                'Готовый снимок не соответствует выбранной группе. '
                'Результат временно скрыт.'
            ),
            409,
        )

    now = now or timezone.now()
    age_seconds = (now - snapshot.last_success_at).total_seconds()
    if age_seconds < -60:
        _snapshot_unavailable(
            'snapshot_time_invalid',
            (
                'Время готового снимка требует проверки. '
                'Результат временно скрыт.'
            ),
            409,
        )
    age_seconds = max(0, int(age_seconds))
    if age_seconds > driver_rating_hard_expire_seconds():
        _snapshot_unavailable(
            'snapshot_expired',
            (
                'Общий снимок рейтинга давно не обновлялся. '
                'Результат скрыт до восстановления фонового расчёта.'
            ),
            409,
        )

    last_attempt_failed = (
        snapshot.last_refresh_status
        == DriverRatingSnapshotRefreshStatus.FAILED
        and snapshot.last_attempt_at is not None
        and snapshot.last_attempt_at >= snapshot.last_success_at
    )
    delayed = (
        age_seconds > driver_rating_soft_stale_seconds()
        or last_attempt_failed
    )
    result = copy.deepcopy(snapshot.payload)
    refresh_seconds = driver_rating_refresh_seconds()
    next_refresh_base = snapshot.last_attempt_at or snapshot.last_success_at
    result.update({
        'generated_at': snapshot.last_success_at.isoformat(),
        'snapshot_status': 'delayed' if delayed else 'fresh',
        'snapshot_revision': snapshot.revision,
        'snapshot_age_seconds': age_seconds,
        'published_at': (
            snapshot.published_at.isoformat()
            if snapshot.published_at
            else None
        ),
        'last_success_at': snapshot.last_success_at.isoformat(),
        'next_refresh_expected_at': (
            next_refresh_base + timedelta(seconds=refresh_seconds)
        ).isoformat(),
    })
    if delayed:
        result['snapshot_warning'] = (
            'Обновление рейтинга задерживается; показан последний '
            'совместимый серверный снимок.'
        )
    return result
