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

from shifts.models import ShiftType
from users.models import WatchComposition

from .driver_rating_scope_membership import (
    discover_driver_rating_group_scope,
)
from .driver_shift_passport_snapshots import _fingerprint
from .driver_watch_rating import (
    DRIVER_RATING_FORMULA_VERSION,
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
                changed = any((
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
        )
        .select_related('watch_composition')
        .order_by('watch_composition_id', 'shift_type')
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
