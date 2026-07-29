from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.core.cache import cache
from django.db.models import OuterRef, Q, Subquery, TextField
from django.db.models.functions import Cast, MD5
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.production_time import production_day_bounds, production_work_date
from shifts.models import EmployeeShift, ShiftType, WatchPeriod
from users.models import WatchComposition

from .driver_watch_observation import (
    _driver_rating_period_queryset,
    build_driver_rating_period_linkage_audit,
    build_driver_watch_linkage_audit,
)
from .driver_shift_passport_snapshots import _fingerprint
from .models import DriverShiftPassportSnapshot, RatingPeriod


logger = logging.getLogger(__name__)


DRIVER_RATING_FORMULA_VERSION = 'DRIVER_WATCH_V2_NO_DISTANCE'
DRIVER_RATING_CACHE_SECONDS = 300
DRIVER_RATING_MIN_COMPARABLE_CYCLES = 5
DRIVER_RATING_WEIGHTS = {
    'production': Decimal('0.45'),
    'work_time': Decimal('0.20'),
    'stability': Decimal('0.15'),
    'assignments': Decimal('0.10'),
    'digital_accounting': Decimal('0.10'),
}
DRIVER_RATING_LEVELS = {
    1: 'Алмазный уровень',
    2: 'Платиновый уровень',
    3: 'Золотой уровень',
    4: 'Серебряный уровень',
    5: 'Медный уровень',
}
DRIVER_RATING_BLOCKING_FLAGS = {
    'ambiguous_trip_output_attribution',
    'data_conflict',
    'downtime_requires_review',
    'employee_shift_overlap',
    'equipment_shift_overlap',
    'invalid_shift_window',
    'shift_duration_over_16h',
    'shift_duration_under_1h',
    'trip_duration_over_16h',
    'watch_period_date_mismatch',
}
DRIVER_RATING_NONBLOCKING_FLAGS = {
    'open_trip_on_closed_shift',
    'trip_assignment_mismatch',
    'trip_without_assignment',
    'unexplained_time',
}

ZERO = Decimal('0')
FIFTY = Decimal('50')
HUNDRED = Decimal('100')
SCORE_QUANTUM = Decimal('0.0001')
DISPLAY_QUANTUM = Decimal('0.01')


def _decimal(value, default=None):
    if value in (None, ''):
        return default
    try:
        return Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return default


def _quantize(value, quantum=SCORE_QUANTUM):
    return Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP)


def _clip(value, lower, upper):
    return min(max(Decimal(value), Decimal(lower)), Decimal(upper))


def _median(values):
    ordered = sorted(Decimal(value) for value in values)
    count = len(ordered)
    if not count:
        return None
    midpoint = count // 2
    if count % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal('2')


def _mean(values):
    values = tuple(Decimal(value) for value in values)
    if not values:
        return None
    return sum(values, ZERO) / Decimal(len(values))


def _bool(value):
    return value is True


def _trip_relation_id(trip, key):
    direct_value = trip.get(f'{key}_id')
    if direct_value is not None:
        return direct_value
    relation = trip.get(key)
    return relation.get('id') if isinstance(relation, dict) else None


def _trip_model_id(trip):
    truck = trip.get('truck')
    if isinstance(truck, dict) and truck.get('model_id') is not None:
        return truck['model_id']
    return trip.get('truck_model_id')


def _normalize_context_text(value):
    normalized = unicodedata.normalize('NFKC', str(value or ''))
    return re.sub(r'\s+', ' ', normalized).strip().casefold()


def _trip_context(trip):
    return {
        'model_id': _trip_model_id(trip),
        'rock_id': _trip_relation_id(trip, 'rock_type'),
        'excavator_id': _trip_relation_id(trip, 'excavator'),
        'dump_id': (
            _trip_relation_id(trip, 'actual_dump_point')
            or _trip_relation_id(trip, 'dump_point')
        ),
        'loading_horizon': _normalize_context_text(
            trip.get('loading_horizon')
        ),
        'loading_block': _normalize_context_text(
            trip.get('loading_block')
        ),
    }


def _trip_cycle_seconds(trip):
    created_at = parse_datetime(str(trip.get('created_at') or ''))
    completed_at = parse_datetime(str(trip.get('completed_at') or ''))
    if created_at is None or completed_at is None or completed_at <= created_at:
        return None
    return Decimal(str((completed_at - created_at).total_seconds()))


def _normalize_employee_ids(allowed_employee_ids):
    if allowed_employee_ids is None:
        return None
    return tuple(sorted({
        int(employee_id)
        for employee_id in allowed_employee_ids
    }))


def _snapshot_manifest_employee_id(snapshot):
    payload = snapshot.payload if isinstance(snapshot.payload, dict) else {}
    manifest = payload.get('source_manifest')
    shift_manifest = (
        manifest.get('shift')
        if isinstance(manifest, dict)
        else None
    )
    if not isinstance(shift_manifest, dict):
        return None
    employee_id = shift_manifest.get('employee_id')
    try:
        return int(employee_id)
    except (TypeError, ValueError):
        return None


def _latest_shift_snapshots(
    watch_period,
    shift_type,
    *,
    allowed_employee_ids=None,
):
    latest_snapshot_id = (
        DriverShiftPassportSnapshot.objects
        .filter(shift_id=OuterRef('shift_id'))
        .order_by('-revision', '-id')
        .values('id')[:1]
    )
    snapshots = (
        DriverShiftPassportSnapshot.objects
        .filter(
            shift__watch_period=watch_period,
            shift__shift_type=shift_type,
            shift__closed_at__isnull=False,
            id=Subquery(latest_snapshot_id),
        )
        .filter(
            Q(shift__workplace_code='driver')
            | Q(
                shift__workplace_code='',
                shift__equipment__equipment_type__name__contains='Самосвал',
            )
        )
        .select_related(
            'shift__employee',
            'shift__equipment__equipment_type',
            'shift__equipment__model',
            'shift__watch_period__watch_composition',
        )
        .order_by('shift_id')
    )
    allowed_employee_ids = _normalize_employee_ids(allowed_employee_ids)
    if allowed_employee_ids is not None:
        snapshots = snapshots.filter(
            Q(shift__employee_id__in=allowed_employee_ids)
            | Q(
                payload__source_manifest__shift__employee_id__in=(
                    allowed_employee_ids
                ),
            ),
        )
    return list(snapshots)


def _rating_period_bounds(rating_period):
    return (
        production_day_bounds(rating_period.starts_on)[0],
        production_day_bounds(rating_period.ends_before)[0],
    )


def _snapshot_manifest_shift(snapshot):
    payload = snapshot.payload if isinstance(snapshot.payload, dict) else {}
    manifest = payload.get('source_manifest')
    if not isinstance(manifest, dict):
        return None
    shift_manifest = manifest.get('shift')
    return shift_manifest if isinstance(shift_manifest, dict) else None


def _manifest_datetime(value):
    if value in (None, ''):
        return None
    return parse_datetime(str(value))


def _snapshot_manifest_is_in_rating_period(snapshot, rating_period):
    shift_manifest = _snapshot_manifest_shift(snapshot)
    opened_at = (
        _manifest_datetime(shift_manifest.get('opened_at'))
        if shift_manifest is not None
        else None
    )
    if opened_at is None:
        # Invalid manifests never become usable. Retaining a live in-period
        # candidate lets strict validation withhold it instead of losing it.
        opened_at = snapshot.shift.opened_at
    if opened_at is None:
        return False
    work_date = production_work_date(opened_at)
    return rating_period.starts_on <= work_date < rating_period.ends_before


def _rating_period_closed_shifts(
    rating_period,
    watch_composition,
    shift_type,
    *,
    allowed_employee_ids=None,
):
    period_start, period_end = _rating_period_bounds(rating_period)
    shifts = (
        EmployeeShift.objects
        .filter(
            opened_at__gte=period_start,
            opened_at__lt=period_end,
            watch_period__watch_composition=watch_composition,
            shift_type=shift_type,
            closed_at__isnull=False,
        )
        .filter(
            Q(workplace_code='driver')
            | Q(
                workplace_code='',
                equipment__equipment_type__name__contains='Самосвал',
            ),
        )
    )
    allowed_employee_ids = _normalize_employee_ids(allowed_employee_ids)
    if allowed_employee_ids is not None:
        shifts = shifts.filter(
            Q(employee_id__in=allowed_employee_ids)
            | Q(
                passport_snapshots__payload__source_manifest__shift__employee_id__in=(
                    allowed_employee_ids
                ),
            ),
        ).distinct()
    return shifts


def _latest_rating_period_snapshots(
    rating_period,
    watch_composition,
    shift_type,
    *,
    allowed_employee_ids=None,
):
    latest_snapshot_id = (
        DriverShiftPassportSnapshot.objects
        .filter(shift_id=OuterRef('shift_id'))
        .order_by('-revision', '-id')
        .values('id')[:1]
    )
    snapshots = (
        DriverShiftPassportSnapshot.objects
        .filter(
            id=Subquery(latest_snapshot_id),
        )
        .filter(
            Q(
                shift__watch_period__watch_composition=watch_composition,
                shift__shift_type=shift_type,
            )
            | Q(
                payload__source_manifest__shift__watch_period__watch_composition__id=(
                    watch_composition.id
                ),
                payload__source_manifest__shift__shift_type=shift_type,
            )
        )
        .select_related(
            'shift__employee',
            'shift__equipment__equipment_type',
            'shift__equipment__model',
            'shift__watch_period__watch_composition',
        )
        .order_by('shift_id')
    )
    allowed_employee_ids = _normalize_employee_ids(allowed_employee_ids)
    if allowed_employee_ids is not None:
        snapshots = snapshots.filter(
            Q(shift__employee_id__in=allowed_employee_ids)
            | Q(
                payload__source_manifest__shift__employee_id__in=(
                    allowed_employee_ids
                ),
            ),
        )
    return [
        snapshot
        for snapshot in snapshots
        if _snapshot_manifest_is_in_rating_period(
            snapshot,
            rating_period,
        )
    ]


def _source_signature(
    watch_period,
    shift_type,
    *,
    allowed_employee_ids=None,
):
    allowed_employee_ids = _normalize_employee_ids(allowed_employee_ids)
    latest_snapshot_id = (
        DriverShiftPassportSnapshot.objects
        .filter(shift_id=OuterRef('shift_id'))
        .order_by('-revision', '-id')
        .values('id')[:1]
    )
    snapshots = (
        DriverShiftPassportSnapshot.objects
        .filter(
            shift__watch_period=watch_period,
            shift__shift_type=shift_type,
            shift__closed_at__isnull=False,
            id=Subquery(latest_snapshot_id),
        )
    )
    shifts = (
        EmployeeShift.objects
        .filter(
            watch_period=watch_period,
            shift_type=shift_type,
            closed_at__isnull=False,
        )
        .filter(
            Q(workplace_code='driver')
            | Q(
                workplace_code='',
                equipment__equipment_type__name__contains='Самосвал',
            ),
        )
    )
    if allowed_employee_ids is not None:
        snapshots = snapshots.filter(
            Q(shift__employee_id__in=allowed_employee_ids)
            | Q(
                payload__source_manifest__shift__employee_id__in=(
                    allowed_employee_ids
                ),
            ),
        )
        shifts = shifts.filter(
            Q(employee_id__in=allowed_employee_ids)
            | Q(
                passport_snapshots__payload__source_manifest__shift__employee_id__in=(
                    allowed_employee_ids
                ),
            ),
        ).distinct()
    snapshot_state_rows = list(
        snapshots
        .annotate(
            stored_payload_digest=MD5(
                Cast('payload', output_field=TextField()),
            ),
        )
        .order_by('shift_id')
        .values_list(
            'id',
            'shift_id',
            'revision',
            'source_fingerprint',
            'payload_fingerprint',
            'stored_payload_digest',
            'captured_at',
        )
    )
    snapshot_state_fingerprint = hashlib.sha256(
        '\n'.join(
            '|'.join(
                (
                    str(snapshot_id),
                    str(shift_id),
                    str(revision),
                    str(source_fingerprint),
                    str(payload_fingerprint),
                    str(stored_payload_digest),
                    captured_at.isoformat() if captured_at else '',
                )
            )
            for (
                snapshot_id,
                shift_id,
                revision,
                source_fingerprint,
                payload_fingerprint,
                stored_payload_digest,
                captured_at,
            ) in snapshot_state_rows
        ).encode('utf-8')
    ).hexdigest()
    shift_state_rows = list(
        shifts
        .order_by('id')
        .values_list(
            'id',
            'employee_id',
            'watch_period_id',
            'shift_type',
            'workplace_code',
            'equipment_id',
            'opened_at',
            'closed_at',
            'is_service_closed',
        )
    )
    shift_state_fingerprint = hashlib.sha256(
        '\n'.join(
            '|'.join(
                (
                    str(shift_id),
                    str(employee_id),
                    str(watch_period_id),
                    str(row_shift_type),
                    str(workplace_code),
                    str(equipment_id or 0),
                    opened_at.isoformat() if opened_at else '',
                    closed_at.isoformat() if closed_at else '',
                    '1' if is_service_closed else '0',
                )
            )
            for (
                shift_id,
                employee_id,
                watch_period_id,
                row_shift_type,
                workplace_code,
                equipment_id,
                opened_at,
                closed_at,
                is_service_closed,
            ) in shift_state_rows
        ).encode('utf-8')
    ).hexdigest()
    max_snapshot_id = max(
        (row[0] for row in snapshot_state_rows),
        default=0,
    )
    max_captured_at = max(
        (
            row[6]
            for row in snapshot_state_rows
            if row[6] is not None
        ),
        default=None,
    )
    max_shift_id = max(
        (row[0] for row in shift_state_rows),
        default=0,
    )
    max_closed_at = max(
        (
            row[7]
            for row in shift_state_rows
            if row[7] is not None
        ),
        default=None,
    )
    return ':'.join(
        (
            str(watch_period.watch_composition_id or 0),
            watch_period.starts_on.isoformat(),
            watch_period.ends_on.isoformat(),
            (
                'all'
                if allowed_employee_ids is None
                else ','.join(map(str, allowed_employee_ids))
            ),
            str(len(snapshot_state_rows)),
            str(max_snapshot_id),
            max_captured_at.isoformat() if max_captured_at else '',
            snapshot_state_fingerprint,
            str(len(shift_state_rows)),
            str(max_shift_id),
            max_closed_at.isoformat() if max_closed_at else '',
            shift_state_fingerprint,
        )
    )


def _rating_period_source_signature(
    rating_period,
    watch_composition,
    shift_type,
    *,
    allowed_employee_ids=None,
    expected_employee_ids=None,
):
    allowed_employee_ids = _normalize_employee_ids(allowed_employee_ids)
    expected_employee_ids = _normalize_employee_ids(
        expected_employee_ids
    ) or ()
    snapshots = _latest_rating_period_snapshots(
        rating_period,
        watch_composition,
        shift_type,
        allowed_employee_ids=allowed_employee_ids,
    )
    selected_shifts = list(
        _rating_period_closed_shifts(
            rating_period,
            watch_composition,
            shift_type,
            allowed_employee_ids=allowed_employee_ids,
        )
        .select_related('watch_period')
        .order_by('id')
    )
    cohort_shifts = _driver_rating_period_queryset(
        rating_period,
        shift_type=shift_type,
        employee_ids=allowed_employee_ids,
    )
    cohort_employee_ids = {
        shift.employee_id
        for shift in selected_shifts
    } | set(expected_employee_ids)
    if allowed_employee_ids is not None:
        cohort_employee_ids &= set(allowed_employee_ids)
    cohort_shifts = cohort_shifts.filter(
        employee_id__in=cohort_employee_ids,
    )
    cohort_shift_rows = list(
        cohort_shifts
        .select_related('watch_period')
        .order_by('id')
    )
    shifts_by_id = {
        shift.id: shift
        for shift in cohort_shift_rows
    }
    for snapshot in snapshots:
        shifts_by_id[snapshot.shift_id] = snapshot.shift
    shifts = [
        shifts_by_id[shift_id]
        for shift_id in sorted(shifts_by_id)
    ]
    watch_periods = {
        shift.watch_period_id: {
            'id': shift.watch_period_id,
            'name': shift.watch_period.name,
            'starts_on': shift.watch_period.starts_on,
            'ends_on': shift.watch_period.ends_on,
            'watch_composition_id': (
                shift.watch_period.watch_composition_id
            ),
        }
        for shift in shifts
        if shift.watch_period_id
    }
    return _fingerprint({
        'scope_version': 'driver-rating-period-v1',
        'rating_period': {
            'id': rating_period.id,
            'name': rating_period.name,
            'starts_on': rating_period.starts_on,
            'ends_before': rating_period.ends_before,
            'updated_at': rating_period.updated_at,
        },
        'watch_composition': {
            'id': watch_composition.id,
            'code': watch_composition.code,
            'name': watch_composition.name,
            'is_active': watch_composition.is_active,
        },
        'shift_type': shift_type,
        'allowed_employee_ids': allowed_employee_ids,
        'expected_employee_ids': expected_employee_ids,
        'watch_periods': [
            watch_periods[watch_period_id]
            for watch_period_id in sorted(watch_periods)
        ],
        'shifts': [
            {
                'id': shift.id,
                'employee_id': shift.employee_id,
                'watch_period_id': shift.watch_period_id,
                'shift_type': shift.shift_type,
                'workplace_code': shift.workplace_code,
                'equipment_id': shift.equipment_id,
                'opened_at': shift.opened_at,
                'closed_at': shift.closed_at,
                'is_service_closed': shift.is_service_closed,
            }
            for shift in shifts
        ],
        'snapshots': [
            {
                'id': snapshot.id,
                'shift_id': snapshot.shift_id,
                'revision': snapshot.revision,
                'source_fingerprint': snapshot.source_fingerprint,
                'payload_fingerprint': snapshot.payload_fingerprint,
                'stored_payload_fingerprint': _fingerprint(
                    snapshot.payload
                ),
                'captured_at': snapshot.captured_at,
            }
            for snapshot in snapshots
        ],
    })


def _empty_rating(
    watch_period,
    shift_type,
    status,
    *,
    withheld_reasons=(),
    linkage_audit=None,
):
    if hasattr(withheld_reasons, 'items'):
        withheld_reason_payload = {
            str(reason): int(count)
            for reason, count in sorted(withheld_reasons.items())
        }
    else:
        withheld_reason_payload = {
            str(reason): 0
            for reason in sorted(set(withheld_reasons))
        }
    withheld_shift_count = sum(withheld_reason_payload.values())
    return {
        'available': False,
        'official': False,
        'rating_mode': 'working',
        'formula_version': DRIVER_RATING_FORMULA_VERSION,
        'formula_label': 'Рабочая формула без м³·км и т·км',
        'status': status,
        'generated_at': timezone.now().isoformat(),
        'source_fingerprint': '',
        'watch_period': (
            {
                'id': watch_period.id,
                'name': watch_period.name,
                'starts_on': watch_period.starts_on.isoformat(),
                'ends_on': watch_period.ends_on.isoformat(),
            }
            if watch_period
            else None
        ),
        'shift_type': shift_type,
        'shift_type_label': dict(ShiftType.choices).get(shift_type, ''),
        'weights': {
            key: str(value)
            for key, value in DRIVER_RATING_WEIGHTS.items()
        },
        'distance_metrics': {
            'weight': '0',
            'status': 'planned',
            'label': 'м³·км и т·км пока не учитываются',
        },
        'linkage_audit': linkage_audit or {},
        'summary': {
            'employee_count': 0,
            'rated_shift_count': 0,
            'withheld_shift_count': withheld_shift_count,
            'withheld_reasons': withheld_reason_payload,
        },
        'entries': [],
    }


def _snapshot_datetime_equal(manifest_value, live_value):
    if manifest_value in (None, ''):
        return live_value is None
    parsed = _manifest_datetime(manifest_value)
    if parsed is None or live_value is None:
        return False
    # DjangoJSONEncoder preserves milliseconds, not arbitrary microseconds.
    live_value = live_value.replace(
        microsecond=(live_value.microsecond // 1000) * 1000,
    )
    return parsed == live_value


def _snapshot_shift_structure_matches(snapshot, shift_manifest):
    return all((
        _snapshot_datetime_equal(
            shift_manifest.get('opened_at'),
            snapshot.shift.opened_at,
        ),
        _snapshot_datetime_equal(
            shift_manifest.get('closed_at'),
            snapshot.shift.closed_at,
        ),
        shift_manifest.get('workplace_code')
        == snapshot.shift.workplace_code,
        shift_manifest.get('equipment_id')
        == snapshot.shift.equipment_id,
    ))


def _validate_snapshot(snapshot, *, strict_shift_structure=False):
    payload = snapshot.payload
    passport = payload.get('passport') if isinstance(payload, dict) else None
    manifest = (
        payload.get('source_manifest')
        if isinstance(payload, dict)
        else None
    )
    shift_manifest = (
        manifest.get('shift')
        if isinstance(manifest, dict)
        else None
    )
    required = (
        isinstance(passport, dict),
        isinstance(manifest, dict),
        isinstance(shift_manifest, dict),
        payload.get('schema_version') == 1 if isinstance(payload, dict) else False,
        manifest.get('manifest_schema_version') == 1 if isinstance(manifest, dict) else False,
        shift_manifest.get('id') == snapshot.shift_id if isinstance(shift_manifest, dict) else False,
        shift_manifest.get('employee_id') == snapshot.shift.employee_id if isinstance(shift_manifest, dict) else False,
        shift_manifest.get('shift_type') == snapshot.shift.shift_type if isinstance(shift_manifest, dict) else False,
        bool(snapshot.source_fingerprint),
        bool(snapshot.payload_fingerprint),
    )
    watch_manifest = (
        shift_manifest.get('watch_period')
        if isinstance(shift_manifest, dict)
        else None
    )
    if not all(required) or not isinstance(watch_manifest, dict):
        return None, 'passport_contract_invalid'
    if snapshot.source_fingerprint != _fingerprint(manifest):
        return None, 'source_fingerprint_mismatch'
    if snapshot.payload_fingerprint != _fingerprint(payload):
        return None, 'payload_fingerprint_mismatch'
    if (
        strict_shift_structure
        and not _snapshot_shift_structure_matches(
            snapshot,
            shift_manifest,
        )
    ):
        return None, 'snapshot_shift_structural_mismatch'
    if watch_manifest.get('id') != snapshot.shift.watch_period_id:
        return None, 'watch_period_snapshot_mismatch'
    watch_composition_manifest = watch_manifest.get('watch_composition')
    live_watch_composition_id = (
        snapshot.shift.watch_period.watch_composition_id
        if snapshot.shift.watch_period_id
        else None
    )
    if (
        not isinstance(watch_composition_manifest, dict)
        or watch_composition_manifest.get('id')
        != live_watch_composition_id
    ):
        return None, 'watch_composition_snapshot_mismatch'

    quality = passport.get('quality')
    quality_flags = set(
        quality.get('flags') or ()
        if isinstance(quality, dict)
        else ()
    )
    blocking_flags = sorted(
        quality_flags & DRIVER_RATING_BLOCKING_FLAGS
    )
    if blocking_flags:
        return None, f'blocking_quality:{",".join(blocking_flags)}'
    unknown_flags = sorted(
        quality_flags
        - DRIVER_RATING_BLOCKING_FLAGS
        - DRIVER_RATING_NONBLOCKING_FLAGS
    )
    if unknown_flags:
        return None, f'unknown_quality:{",".join(unknown_flags)}'

    production = passport.get('production')
    time_data = passport.get('time')
    routing = passport.get('routing')
    open_close = passport.get('open_close')
    if not all(
        isinstance(value, dict)
        for value in (production, time_data, routing, open_close, quality)
    ):
        return None, 'passport_sections_missing'
    available_seconds = _decimal(time_data.get('available_seconds'))
    if available_seconds is None or available_seconds <= 0:
        return None, 'available_time_invalid'
    if _decimal(time_data.get('downtime_review_seconds'), ZERO) > 0:
        return None, 'downtime_requires_review'

    return {
        'snapshot': snapshot,
        'payload': payload,
        'passport': passport,
        'manifest': manifest,
        'shift_manifest': shift_manifest,
        'production': production,
        'time': time_data,
        'routing': routing,
        'open_close': open_close,
        'quality': quality,
        'available_seconds': available_seconds,
    }, ''


def _credited_trips(record):
    shift_id = record['snapshot'].shift_id
    result = []
    for trip in record['manifest'].get('trips') or ():
        if (
            isinstance(trip, dict)
            and _trip_relation_id(trip, 'unloading_shift') == shift_id
        ):
            result.append(trip)
    return result


def _calibration(records):
    volume_samples = defaultdict(list)
    cycle_samples = {
        'exact': defaultdict(list),
        'excavator_route': defaultdict(list),
        'route': defaultdict(list),
        'model_rock': defaultdict(list),
        'model': defaultdict(list),
        'peer': defaultdict(list),
    }
    for record in records:
        shift_id = record['snapshot'].shift_id
        for trip in record['credited_trips']:
            context = _trip_context(trip)
            model_id = context['model_id']
            rock_id = context['rock_id']
            volume = _decimal(trip.get('volume_m3'))
            if volume is not None and volume >= 0:
                volume_samples[(model_id, rock_id)].append(
                    (shift_id, volume)
                )
            cycle_seconds = _trip_cycle_seconds(trip)
            if (
                trip.get('is_carryover') is True
                or cycle_seconds is None
                or cycle_seconds < 60
                or cycle_seconds > 14400
            ):
                continue
            cycle_samples['exact'][
                (
                    model_id,
                    rock_id,
                    context['excavator_id'],
                    context['dump_id'],
                    context['loading_horizon'],
                    context['loading_block'],
                )
            ].append((shift_id, cycle_seconds))
            cycle_samples['excavator_route'][
                (
                    model_id,
                    rock_id,
                    context['excavator_id'],
                    context['dump_id'],
                )
            ].append((shift_id, cycle_seconds))
            cycle_samples['route'][
                (model_id, rock_id, context['dump_id'])
            ].append((shift_id, cycle_seconds))
            cycle_samples['model_rock'][
                (model_id, rock_id)
            ].append((shift_id, cycle_seconds))
            cycle_samples['model'][model_id].append(
                (shift_id, cycle_seconds)
            )
            cycle_samples['peer']['peer'].append(
                (shift_id, cycle_seconds)
            )

    cycle_medians = {
        level: {
            key: _median(value for _, value in values)
            for key, values in samples.items()
        }
        for level, samples in cycle_samples.items()
    }
    return (
        volume_samples,
        cycle_samples,
        cycle_medians,
    )


def _median_without_shift(
    samples,
    excluded_shift_id,
    *,
    minimum_count=1,
):
    values = [
        value
        for shift_id, value in samples
        if shift_id != excluded_shift_id
    ]
    if len(values) < minimum_count:
        return None
    return _median(values)


def _context_cycle_median(
    context,
    cycle_samples,
    cycle_medians,
    *,
    excluded_shift_id=None,
    cache_by_shift=None,
):
    candidates = (
        (
            'exact',
            (
                context['model_id'],
                context['rock_id'],
                context['excavator_id'],
                context['dump_id'],
                context['loading_horizon'],
                context['loading_block'],
            ),
        ),
        (
            'excavator_route',
            (
                context['model_id'],
                context['rock_id'],
                context['excavator_id'],
                context['dump_id'],
            ),
        ),
        (
            'route',
            (
                context['model_id'],
                context['rock_id'],
                context['dump_id'],
            ),
        ),
        (
            'model_rock',
            (context['model_id'], context['rock_id']),
        ),
        ('model', context['model_id']),
    )
    for level, key in candidates:
        samples = cycle_samples[level].get(key, ())
        if excluded_shift_id is None:
            if len(samples) >= 20:
                return cycle_medians[level][key]
            continue
        cache_key = (level, key, excluded_shift_id)
        if cache_by_shift is not None and cache_key in cache_by_shift:
            candidate = cache_by_shift[cache_key]
        else:
            candidate = _median_without_shift(
                samples,
                excluded_shift_id,
                minimum_count=20,
            )
            if cache_by_shift is not None:
                cache_by_shift[cache_key] = candidate
        if candidate is not None:
            return candidate

    peer_samples = cycle_samples['peer'].get('peer', ())
    if excluded_shift_id is None:
        return cycle_medians['peer'].get('peer')
    peer_key = ('peer', 'peer', excluded_shift_id)
    if cache_by_shift is not None and peer_key in cache_by_shift:
        return cache_by_shift[peer_key]
    candidate = _median_without_shift(
        peer_samples,
        excluded_shift_id,
        minimum_count=1,
    )
    if cache_by_shift is not None:
        cache_by_shift[peer_key] = candidate
    return candidate


def _shift_work_units(
    record,
    volume_samples,
    cycle_samples,
    cycle_medians,
):
    shift_id = record['snapshot'].shift_id
    calibration_cache = {}
    peer_cycle = _median_without_shift(
        cycle_samples['peer'].get('peer', ()),
        shift_id,
        minimum_count=1,
    )
    work_units = ZERO
    for trip in record['credited_trips']:
        context = _trip_context(trip)
        model_id = context['model_id']
        rock_id = context['rock_id']
        volume = _decimal(trip.get('volume_m3'))
        volume_key = (model_id, rock_id)
        volume_reference = _median_without_shift(
            volume_samples.get(volume_key, ()),
            shift_id,
            minimum_count=1,
        )
        if (
            volume is not None
            and volume_reference is not None
            and volume_reference > 0
        ):
            fill_factor = _clip(
                volume / volume_reference,
                Decimal('0.80'),
                Decimal('1.20'),
            )
        else:
            fill_factor = Decimal('1')
        context_cycle = _context_cycle_median(
            context,
            cycle_samples,
            cycle_medians,
            excluded_shift_id=shift_id,
            cache_by_shift=calibration_cache,
        )
        if (
            peer_cycle is not None
            and peer_cycle > 0
            and context_cycle is not None
        ):
            difficulty = _clip(
                context_cycle / peer_cycle,
                Decimal('0.75'),
                Decimal('1.50'),
            )
        else:
            difficulty = Decimal('1')
        work_units += fill_factor * difficulty
    return work_units


def _midrank_percentiles(records):
    ordered = sorted(record['production_rate'] for record in records)
    count = len(ordered)
    if count <= 1:
        return {record['snapshot'].shift_id: FIFTY for record in records}
    positions = defaultdict(list)
    for position, value in enumerate(ordered, start=1):
        positions[value].append(position)
    scores = {}
    for record in records:
        ranks = positions[record['production_rate']]
        midrank = (
            Decimal(ranks[0]) + Decimal(ranks[-1])
        ) / Decimal('2')
        scores[record['snapshot'].shift_id] = (
            HUNDRED
            * (midrank - Decimal('1'))
            / Decimal(count - 1)
        )
    return scores


def _shift_cycle_dispersion(
    record,
    cycle_samples,
    cycle_medians,
    calibration_cache,
):
    shift_id = record['snapshot'].shift_id
    normalized_cycles = []
    for trip in record['credited_trips']:
        if trip.get('is_carryover') is True:
            continue
        cycle_seconds = _trip_cycle_seconds(trip)
        if (
            cycle_seconds is None
            or cycle_seconds < 60
            or cycle_seconds > 14400
        ):
            continue
        context_cycle = _context_cycle_median(
            _trip_context(trip),
            cycle_samples,
            cycle_medians,
            excluded_shift_id=shift_id,
            cache_by_shift=calibration_cache,
        )
        if context_cycle is None or context_cycle <= 0:
            continue
        normalized_cycles.append(cycle_seconds / context_cycle)
    if len(normalized_cycles) < DRIVER_RATING_MIN_COMPARABLE_CYCLES:
        return None
    center = _median(normalized_cycles)
    if center is None or center <= 0:
        return None
    return _median(
        abs(value - center)
        for value in normalized_cycles
    ) / center


def _stability_scores(records, cycle_samples, cycle_medians):
    comparable = []
    dispersions = {}
    calibration_cache = {}
    for record in records:
        shift_id = record['snapshot'].shift_id
        dispersion = _shift_cycle_dispersion(
            record,
            cycle_samples,
            cycle_medians,
            calibration_cache,
        )
        dispersions[shift_id] = dispersion
        if dispersion is not None:
            comparable.append(dispersion)

    ordered = sorted(comparable)
    if len(ordered) <= 1:
        return {
            record['snapshot'].shift_id: FIFTY
            for record in records
        }
    positions = defaultdict(list)
    for position, value in enumerate(ordered, start=1):
        positions[value].append(position)

    result = {}
    count = len(ordered)
    for record in records:
        shift_id = record['snapshot'].shift_id
        dispersion = dispersions[shift_id]
        if dispersion is None:
            result[shift_id] = FIFTY
            continue
        ranks = positions[dispersion]
        midrank = (
            Decimal(ranks[0]) + Decimal(ranks[-1])
        ) / Decimal('2')
        result[shift_id] = (
            HUNDRED
            * (Decimal(count) - midrank)
            / Decimal(count - 1)
        )
    return result


def _work_time_score(record):
    time_data = record['time']
    if (
        time_data.get('scheduled_window_status')
        == 'schedule_snapshot_unavailable'
    ):
        return FIFTY
    unjustified_seconds = _decimal(
        time_data.get('unjustified_short_shift_seconds')
    )
    if unjustified_seconds is None:
        return HUNDRED
    penalty = _clip(
        unjustified_seconds / Decimal('3600'),
        ZERO,
        Decimal('1'),
    )
    return HUNDRED * (Decimal('1') - penalty)


def _assignments_score(record):
    routing = record['routing']
    match_count = _decimal(routing.get('match_count'), ZERO)
    mismatch_count = _decimal(routing.get('mismatch_count'), ZERO)
    missing_count = max(
        _decimal(routing.get('missing_actual_count'), ZERO),
        _decimal(routing.get('missing_assigned_count'), ZERO),
    )
    route_denominator = match_count + mismatch_count + missing_count
    route_score = (
        HUNDRED * match_count / route_denominator
        if route_denominator > 0
        else FIFTY
    )

    quality_metrics = record['quality'].get('quality_metrics') or {}
    bad_assignment_seconds = max(
        _decimal(
            quality_metrics.get('trip_without_assignment_seconds'),
            ZERO,
        ),
        _decimal(
            quality_metrics.get('trip_assignment_mismatch_seconds'),
            ZERO,
        ),
    )
    assignment_ratio = _clip(
        bad_assignment_seconds / max(record['available_seconds'], Decimal('1')),
        ZERO,
        Decimal('1'),
    )
    assignment_score = HUNDRED * (Decimal('1') - assignment_ratio)
    return (route_score + assignment_score) / Decimal('2')


def _handover_score(record):
    open_close = record['open_close']
    checks = (
        _bool(open_close.get('window_valid')),
        _bool(open_close.get('opened_by_employee')),
        _bool(open_close.get('start_readings_complete')),
        _bool(open_close.get('end_readings_complete')),
        (
            _bool(open_close.get('closed_by_employee'))
            or _bool(open_close.get('service_closed'))
        ),
    )
    return HUNDRED * Decimal(sum(checks)) / Decimal(len(checks))


def _digital_accounting_score(record):
    production = record['production']
    routing = record['routing']
    attribution = production.get('output_attribution') or {}
    volume = production.get('volume_m3') or {}
    tonnage = production.get('tonnage_t') or {}
    accounting_checks = (
        _bool(volume.get('is_complete')),
        _bool(tonnage.get('is_complete')),
        attribution.get('ambiguous_trip_count') == 0,
        attribution.get('legacy_driver_trip_count') == 0,
        (
            routing.get('missing_actual_count') == 0
            and routing.get('missing_assigned_count') == 0
        ),
    )
    accounting_score = (
        HUNDRED
        * Decimal(sum(accounting_checks))
        / Decimal(len(accounting_checks))
    )
    return (accounting_score + _handover_score(record)) / Decimal('2')


def _confidence_score(record, assignments, digital):
    coverage = _decimal(
        record['quality'].get('coverage_percent'),
        ZERO,
    )
    source_core = (assignments + digital) / Decimal('2')
    schedule_component = (
        ZERO
        if (
            record['time'].get('scheduled_window_status')
            == 'schedule_snapshot_unavailable'
        )
        else HUNDRED
    )
    return (
        Decimal('0.50') * coverage
        + Decimal('0.30') * source_core
        + Decimal('0.20') * schedule_component
    )


def _employee_entries(records):
    groups = defaultdict(list)
    for record in records:
        groups[record['snapshot'].shift.employee_id].append(record)

    entries = []
    for employee_id, employee_records in groups.items():
        employee = employee_records[0]['snapshot'].shift.employee
        shift_scores = [record['shift_score'] for record in employee_records]
        block_scores = {
            block: _mean(
                record['blocks'][block]
                for record in employee_records
            )
            for block in DRIVER_RATING_WEIGHTS
        }
        equipment_names = sorted({
            str(record['snapshot'].shift.equipment)
            for record in employee_records
            if record['snapshot'].shift.equipment_id
        })
        volume_m3 = sum(
            (
                _decimal(
                    (
                        record['production'].get('volume_m3')
                        or {}
                    ).get('known_value'),
                    ZERO,
                )
                for record in employee_records
            ),
            ZERO,
        )
        tonnage_t = sum(
            (
                _decimal(
                    (
                        record['production'].get('tonnage_t')
                        or {}
                    ).get('known_value'),
                    ZERO,
                )
                for record in employee_records
            ),
            ZERO,
        )
        entries.append({
            'employee_id': employee_id,
            'full_name': employee.full_name,
            'equipment': equipment_names,
            'shift_count': len(employee_records),
            'trip_count': sum(
                int(
                    record['production'].get(
                        'completed_trip_count',
                        0,
                    )
                )
                for record in employee_records
            ),
            'volume_m3': str(_quantize(volume_m3, Decimal('0.01'))),
            'tonnage_t': str(_quantize(tonnage_t, Decimal('0.01'))),
            'score': _quantize(_mean(shift_scores)),
            'blocks': {
                block: _quantize(value)
                for block, value in block_scores.items()
            },
            'confidence': _quantize(
                _mean(
                    record['confidence']
                    for record in employee_records
                )
            ),
            'source_shift_ids': sorted(
                record['snapshot'].shift_id
                for record in employee_records
            ),
        })

    entries.sort(key=lambda item: (-item['score'], item['employee_id']))
    dense_place_by_score = {}
    for index, entry in enumerate(entries, start=1):
        if entry['score'] not in dense_place_by_score:
            dense_place_by_score[entry['score']] = (
                len(dense_place_by_score) + 1
            )
        entry['place'] = dense_place_by_score[entry['score']]
        entry['shared_score_place'] = entry['place']
        entry['display_order'] = index
        entry['level'] = DRIVER_RATING_LEVELS.get(entry['place'], '')
        entry['score'] = str(entry['score'])
        entry['confidence'] = str(entry['confidence'])
        entry['blocks'] = {
            key: str(value)
            for key, value in entry['blocks'].items()
        }
    return entries


def build_driver_watch_rating(
    watch_period,
    *,
    shift_type,
    allowed_employee_ids=None,
):
    if not isinstance(watch_period, WatchPeriod):
        raise TypeError('watch_period должен быть экземпляром WatchPeriod.')
    if shift_type not in {ShiftType.DAY, ShiftType.NIGHT}:
        raise ValueError('Для рейтинга укажите shift_type: day или night.')
    allowed_employee_ids = _normalize_employee_ids(allowed_employee_ids)
    if watch_period.watch_composition_id is None:
        return _empty_rating(
            watch_period,
            shift_type,
            'У вахты не указан утверждённый состав.',
            withheld_reasons=('watch_composition_missing',),
        )

    linkage_audit = build_driver_watch_linkage_audit(
        watch_period,
        shift_type=shift_type,
        employee_ids=allowed_employee_ids,
    )
    if not linkage_audit['linked_to_selected_watch_count']:
        return _empty_rating(
            watch_period,
            shift_type,
            'У выбранной вахты нет связанных закрытых водительских смен.',
            withheld_reasons=('watch_has_no_linked_driver_shifts',),
            linkage_audit=linkage_audit,
        )
    if linkage_audit['selected_watch_outside_period_count']:
        return _empty_rating(
            watch_period,
            shift_type,
            'Смена выбранной вахты выходит за её календарные границы.',
            withheld_reasons={
                'selected_watch_date_mismatch': linkage_audit[
                    'selected_watch_outside_period_count'
                ],
            },
            linkage_audit=linkage_audit,
        )

    closed_shifts = (
        EmployeeShift.objects
        .filter(
            watch_period=watch_period,
            shift_type=shift_type,
            closed_at__isnull=False,
        )
        .filter(
            Q(workplace_code='driver')
            | Q(
                workplace_code='',
                equipment__equipment_type__name__contains='Самосвал',
            )
        )
    )
    if allowed_employee_ids is not None:
        closed_shifts = closed_shifts.filter(
            Q(employee_id__in=allowed_employee_ids)
            | Q(
                passport_snapshots__payload__source_manifest__shift__employee_id__in=(
                    allowed_employee_ids
                ),
            ),
        ).distinct()
    closed_shift_rows = dict(
        closed_shifts.values_list('id', 'employee_id')
    )
    snapshots = _latest_shift_snapshots(
        watch_period,
        shift_type,
        allowed_employee_ids=allowed_employee_ids,
    )

    records = []
    withheld_reasons = defaultdict(int)
    excluded_employee_ids = set()
    snapshot_shift_ids = {
        snapshot.shift_id
        for snapshot in snapshots
    }
    missing_snapshot_shift_ids = (
        set(closed_shift_rows) - snapshot_shift_ids
    )
    if missing_snapshot_shift_ids:
        withheld_reasons['passport_coverage_incomplete'] += len(
            missing_snapshot_shift_ids
        )
        excluded_employee_ids.update(
            closed_shift_rows[shift_id]
            for shift_id in missing_snapshot_shift_ids
        )

    for snapshot in snapshots:
        record, reason = _validate_snapshot(snapshot)
        if record is None:
            withheld_reasons[reason] += 1
            excluded_employee_ids.add(snapshot.shift.employee_id)
            manifest_employee_id = _snapshot_manifest_employee_id(snapshot)
            if manifest_employee_id is not None:
                excluded_employee_ids.add(manifest_employee_id)
            continue
        record['credited_trips'] = _credited_trips(record)
        if (
            len(record['credited_trips'])
            != int(record['production'].get('completed_trip_count', 0))
        ):
            withheld_reasons['trip_attribution_mismatch'] += 1
            excluded_employee_ids.add(snapshot.shift.employee_id)
            manifest_employee_id = _snapshot_manifest_employee_id(snapshot)
            if manifest_employee_id is not None:
                excluded_employee_ids.add(manifest_employee_id)
            continue
        records.append(record)

    partially_covered_records = [
        record
        for record in records
        if record['snapshot'].shift.employee_id in excluded_employee_ids
    ]
    if partially_covered_records:
        withheld_reasons['employee_partial_coverage'] += len(
            partially_covered_records
        )
        records = [
            record
            for record in records
            if record['snapshot'].shift.employee_id
            not in excluded_employee_ids
        ]

    if not records:
        return _empty_rating(
            watch_period,
            shift_type,
            'Нет смен, пригодных для рабочего расчёта.',
            withheld_reasons=withheld_reasons,
            linkage_audit=linkage_audit,
        )

    (
        volume_samples,
        cycle_samples,
        cycle_medians,
    ) = _calibration(records)
    for record in records:
        record['work_units'] = _shift_work_units(
            record,
            volume_samples,
            cycle_samples,
            cycle_medians,
        )
        record['production_rate'] = (
            record['work_units']
            * Decimal('3600')
            / record['available_seconds']
        )

    production_scores = _midrank_percentiles(records)
    stability_scores = _stability_scores(
        records,
        cycle_samples,
        cycle_medians,
    )
    shift_score_lines = []
    for record in records:
        shift_id = record['snapshot'].shift_id
        blocks = {
            'production': production_scores[shift_id],
            'work_time': _work_time_score(record),
            'stability': stability_scores[shift_id],
            'assignments': _assignments_score(record),
            'digital_accounting': _digital_accounting_score(record),
        }
        raw_score = sum(
            (
                blocks[key] * DRIVER_RATING_WEIGHTS[key]
                for key in DRIVER_RATING_WEIGHTS
            ),
            ZERO,
        )
        record['blocks'] = blocks
        record['shift_score'] = _quantize(raw_score)
        record['confidence'] = _confidence_score(
            record,
            blocks['assignments'],
            blocks['digital_accounting'],
        )
        shift_score_lines.append(
            f'{shift_id}:{record["shift_score"]:.4f}'
        )

    shift_score_fingerprint = hashlib.sha256(
        '\n'.join(
            line
            for _, line in sorted(
                (
                    int(line.split(':', 1)[0]),
                    line,
                )
                for line in shift_score_lines
            )
        ).encode('utf-8')
    ).hexdigest().upper()
    source_fingerprint = hashlib.sha256(
        '|'.join(
            (
                DRIVER_RATING_FORMULA_VERSION,
                str(watch_period.id),
                shift_type,
                *sorted(
                    (
                        f'{snapshot.payload_fingerprint}:'
                        f'{_fingerprint(snapshot.payload)}'
                    )
                    for snapshot in snapshots
                ),
                *(
                    f'shift:{shift_id}:employee:{closed_shift_rows[shift_id]}'
                    for shift_id in sorted(closed_shift_rows)
                ),
            )
        ).encode('utf-8')
    ).hexdigest()

    entries = _employee_entries(records)
    return {
        'available': True,
        'official': False,
        'rating_mode': 'working',
        'formula_version': DRIVER_RATING_FORMULA_VERSION,
        'formula_label': 'Рабочая формула без м³·км и т·км',
        'status': (
            'Рабочий рейтинг рассчитан. '
            'м³·км и т·км пока не учитываются.'
        ),
        'generated_at': timezone.now().isoformat(),
        'source_fingerprint': source_fingerprint,
        'shift_score_fingerprint': shift_score_fingerprint,
        'watch_period': {
            'id': watch_period.id,
            'name': watch_period.name,
            'starts_on': watch_period.starts_on.isoformat(),
            'ends_on': watch_period.ends_on.isoformat(),
        },
        'shift_type': shift_type,
        'shift_type_label': dict(ShiftType.choices)[shift_type],
        'weights': {
            key: str(value)
            for key, value in DRIVER_RATING_WEIGHTS.items()
        },
        'distance_metrics': {
            'weight': '0',
            'status': 'planned',
            'label': 'м³·км и т·км пока не учитываются',
        },
        'linkage_audit': linkage_audit,
        'summary': {
            'employee_count': len(entries),
            'rated_shift_count': len(records),
            'withheld_shift_count': len(closed_shift_rows) - len(records),
            'withheld_reasons': dict(sorted(withheld_reasons.items())),
            'trip_count': sum(
                entry['trip_count']
                for entry in entries
            ),
            'volume_m3': str(
                _quantize(
                    sum(
                        (
                            _decimal(entry['volume_m3'], ZERO)
                            for entry in entries
                        ),
                        ZERO,
                    ),
                    Decimal('0.01'),
                )
            ),
            'tonnage_t': str(
                _quantize(
                    sum(
                        (
                            _decimal(entry['tonnage_t'], ZERO)
                            for entry in entries
                        ),
                        ZERO,
                    ),
                    Decimal('0.01'),
                )
            ),
        },
        'entries': entries,
    }


def _rating_period_payload(rating_period):
    return {
        'id': rating_period.id,
        'name': rating_period.name,
        'starts_on': rating_period.starts_on.isoformat(),
        'ends_before': rating_period.ends_before.isoformat(),
    }


def _watch_composition_payload(watch_composition):
    return {
        'id': watch_composition.id,
        'code': watch_composition.code,
        'name': watch_composition.name,
    }


def _empty_rating_period(
    rating_period,
    watch_composition,
    shift_type,
    status,
    *,
    withheld_reasons=(),
    linkage_audit=None,
):
    payload = _empty_rating(
        None,
        shift_type,
        status,
        withheld_reasons=withheld_reasons,
        linkage_audit=linkage_audit,
    )
    payload.pop('watch_period', None)
    payload.update({
        'scope_type': 'rating_period',
        'rating_period': _rating_period_payload(rating_period),
        'watch_composition': _watch_composition_payload(
            watch_composition
        ),
    })
    return payload


def _rating_period_linkage_withheld_reasons(linkage_audit):
    reasons = {}
    for audit_key, reason in (
        ('unlinked_shift_count', 'rating_period_unlinked_shift'),
        (
            'linked_to_other_composition_count',
            'rating_period_other_composition_shift',
        ),
        (
            'selected_watch_date_mismatch_count',
            'watch_period_date_mismatch',
        ),
    ):
        count = int(linkage_audit.get(audit_key) or 0)
        if count:
            reasons[reason] = count
    return reasons


def build_driver_rating_period(
    rating_period,
    watch_composition,
    *,
    shift_type,
    allowed_employee_ids=None,
    expected_employee_ids=None,
):
    """Строит рабочий рейтинг для независимого календарного окна.

    RatingPeriod задаёт только даты отбора. Структурная принадлежность
    каждой смены по-прежнему берётся из неизменяемой связи
    EmployeeShift.watch_period -> WatchComposition.
    """

    if not isinstance(rating_period, RatingPeriod):
        raise TypeError(
            'rating_period должен быть экземпляром RatingPeriod.'
        )
    if not isinstance(watch_composition, WatchComposition):
        raise TypeError(
            'watch_composition должен быть экземпляром WatchComposition.'
        )
    if rating_period.ends_before <= rating_period.starts_on:
        raise ValueError(
            'Конец периода рейтинга должен быть позже даты начала.'
        )
    if shift_type not in {ShiftType.DAY, ShiftType.NIGHT}:
        raise ValueError('Для рейтинга укажите shift_type: day или night.')
    allowed_employee_ids = _normalize_employee_ids(allowed_employee_ids)
    expected_employee_ids = _normalize_employee_ids(
        expected_employee_ids
    )

    snapshots = _latest_rating_period_snapshots(
        rating_period,
        watch_composition,
        shift_type,
        allowed_employee_ids=allowed_employee_ids,
    )
    linkage_audit = build_driver_rating_period_linkage_audit(
        rating_period,
        watch_composition,
        shift_type=shift_type,
        employee_ids=allowed_employee_ids,
        expected_employee_ids=expected_employee_ids,
    )
    if (
        not linkage_audit['linked_to_selected_composition_count']
        and not snapshots
    ):
        return _empty_rating_period(
            rating_period,
            watch_composition,
            shift_type,
            'В выбранном периоде нет связанных закрытых смен этого состава.',
            withheld_reasons=(
                'rating_period_has_no_linked_driver_shifts',
            ),
            linkage_audit=linkage_audit,
        )
    linkage_withheld_reasons = _rating_period_linkage_withheld_reasons(
        linkage_audit
    )
    if linkage_withheld_reasons:
        return _empty_rating_period(
            rating_period,
            watch_composition,
            shift_type,
            (
                'Связи смен внутри периода рейтинга требуют проверки; '
                'расчёт удержан.'
            ),
            withheld_reasons=linkage_withheld_reasons,
            linkage_audit=linkage_audit,
        )

    closed_shifts = _rating_period_closed_shifts(
        rating_period,
        watch_composition,
        shift_type,
        allowed_employee_ids=allowed_employee_ids,
    )
    closed_shift_rows = dict(
        closed_shifts.values_list('id', 'employee_id')
    )
    for snapshot in snapshots:
        closed_shift_rows.setdefault(
            snapshot.shift_id,
            (
                _snapshot_manifest_employee_id(snapshot)
                or snapshot.shift.employee_id
            ),
        )

    records = []
    withheld_reasons = defaultdict(int)
    excluded_employee_ids = set()
    snapshot_shift_ids = {
        snapshot.shift_id
        for snapshot in snapshots
    }
    missing_snapshot_shift_ids = (
        set(closed_shift_rows) - snapshot_shift_ids
    )
    if missing_snapshot_shift_ids:
        withheld_reasons['passport_coverage_incomplete'] += len(
            missing_snapshot_shift_ids
        )
        excluded_employee_ids.update(
            closed_shift_rows[shift_id]
            for shift_id in missing_snapshot_shift_ids
        )

    for snapshot in snapshots:
        record, reason = _validate_snapshot(
            snapshot,
            strict_shift_structure=True,
        )
        if record is None:
            withheld_reasons[reason] += 1
            excluded_employee_ids.add(snapshot.shift.employee_id)
            manifest_employee_id = _snapshot_manifest_employee_id(snapshot)
            if manifest_employee_id is not None:
                excluded_employee_ids.add(manifest_employee_id)
            continue
        record['credited_trips'] = _credited_trips(record)
        if (
            len(record['credited_trips'])
            != int(record['production'].get('completed_trip_count', 0))
        ):
            withheld_reasons['trip_attribution_mismatch'] += 1
            excluded_employee_ids.add(snapshot.shift.employee_id)
            manifest_employee_id = _snapshot_manifest_employee_id(snapshot)
            if manifest_employee_id is not None:
                excluded_employee_ids.add(manifest_employee_id)
            continue
        records.append(record)

    partially_covered_records = [
        record
        for record in records
        if record['snapshot'].shift.employee_id in excluded_employee_ids
    ]
    if partially_covered_records:
        withheld_reasons['employee_partial_coverage'] += len(
            partially_covered_records
        )
        records = [
            record
            for record in records
            if record['snapshot'].shift.employee_id
            not in excluded_employee_ids
        ]

    if not records:
        return _empty_rating_period(
            rating_period,
            watch_composition,
            shift_type,
            'Нет смен, пригодных для рабочего расчёта.',
            withheld_reasons=withheld_reasons,
            linkage_audit=linkage_audit,
        )

    (
        volume_samples,
        cycle_samples,
        cycle_medians,
    ) = _calibration(records)
    for record in records:
        record['work_units'] = _shift_work_units(
            record,
            volume_samples,
            cycle_samples,
            cycle_medians,
        )
        record['production_rate'] = (
            record['work_units']
            * Decimal('3600')
            / record['available_seconds']
        )

    production_scores = _midrank_percentiles(records)
    stability_scores = _stability_scores(
        records,
        cycle_samples,
        cycle_medians,
    )
    shift_score_lines = []
    for record in records:
        shift_id = record['snapshot'].shift_id
        blocks = {
            'production': production_scores[shift_id],
            'work_time': _work_time_score(record),
            'stability': stability_scores[shift_id],
            'assignments': _assignments_score(record),
            'digital_accounting': _digital_accounting_score(record),
        }
        raw_score = sum(
            (
                blocks[key] * DRIVER_RATING_WEIGHTS[key]
                for key in DRIVER_RATING_WEIGHTS
            ),
            ZERO,
        )
        record['blocks'] = blocks
        record['shift_score'] = _quantize(raw_score)
        record['confidence'] = _confidence_score(
            record,
            blocks['assignments'],
            blocks['digital_accounting'],
        )
        shift_score_lines.append(
            f'{shift_id}:{record["shift_score"]:.4f}'
        )

    shift_score_fingerprint = hashlib.sha256(
        '\n'.join(
            line
            for _, line in sorted(
                (
                    int(line.split(':', 1)[0]),
                    line,
                )
                for line in shift_score_lines
            )
        ).encode('utf-8')
    ).hexdigest().upper()
    source_fingerprint = hashlib.sha256(
        '|'.join(
            (
                DRIVER_RATING_FORMULA_VERSION,
                'driver-rating-period-v1',
                str(rating_period.id),
                rating_period.name,
                rating_period.starts_on.isoformat(),
                rating_period.ends_before.isoformat(),
                str(watch_composition.id),
                watch_composition.code,
                watch_composition.name,
                shift_type,
                *sorted(
                    (
                        f'{snapshot.payload_fingerprint}:'
                        f'{_fingerprint(snapshot.payload)}'
                    )
                    for snapshot in snapshots
                ),
                *(
                    f'shift:{shift_id}:employee:{closed_shift_rows[shift_id]}'
                    for shift_id in sorted(closed_shift_rows)
                ),
            )
        ).encode('utf-8')
    ).hexdigest()

    entries = _employee_entries(records)
    return {
        'available': True,
        'official': False,
        'rating_mode': 'working',
        'scope_type': 'rating_period',
        'formula_version': DRIVER_RATING_FORMULA_VERSION,
        'formula_label': 'Рабочая формула без м³·км и т·км',
        'status': (
            'Рабочий рейтинг рассчитан. '
            'м³·км и т·км пока не учитываются.'
        ),
        'generated_at': timezone.now().isoformat(),
        'source_fingerprint': source_fingerprint,
        'shift_score_fingerprint': shift_score_fingerprint,
        'rating_period': _rating_period_payload(rating_period),
        'watch_composition': _watch_composition_payload(
            watch_composition
        ),
        'shift_type': shift_type,
        'shift_type_label': dict(ShiftType.choices)[shift_type],
        'weights': {
            key: str(value)
            for key, value in DRIVER_RATING_WEIGHTS.items()
        },
        'distance_metrics': {
            'weight': '0',
            'status': 'planned',
            'label': 'м³·км и т·км пока не учитываются',
        },
        'linkage_audit': linkage_audit,
        'summary': {
            'employee_count': len(entries),
            'rated_shift_count': len(records),
            'withheld_shift_count': len(closed_shift_rows) - len(records),
            'withheld_reasons': dict(sorted(withheld_reasons.items())),
            'trip_count': sum(
                entry['trip_count']
                for entry in entries
            ),
            'volume_m3': str(
                _quantize(
                    sum(
                        (
                            _decimal(entry['volume_m3'], ZERO)
                            for entry in entries
                        ),
                        ZERO,
                    ),
                    Decimal('0.01'),
                )
            ),
            'tonnage_t': str(
                _quantize(
                    sum(
                        (
                            _decimal(entry['tonnage_t'], ZERO)
                            for entry in entries
                        ),
                        ZERO,
                    ),
                    Decimal('0.01'),
                )
            ),
        },
        'entries': entries,
    }


def get_cached_driver_watch_rating(
    watch_period,
    *,
    shift_type,
    allowed_employee_ids=None,
):
    allowed_employee_ids = _normalize_employee_ids(allowed_employee_ids)
    signature = _source_signature(
        watch_period,
        shift_type,
        allowed_employee_ids=allowed_employee_ids,
    )
    scope_fingerprint = (
        'all'
        if allowed_employee_ids is None
        else hashlib.sha256(
            ','.join(map(str, allowed_employee_ids)).encode('utf-8')
        ).hexdigest()
    )
    cache_key = (
        'driver-watch-rating:'
        f'{DRIVER_RATING_FORMULA_VERSION}:'
        f'{watch_period.id}:{shift_type}:{scope_fingerprint}:'
        f'{hashlib.sha256(signature.encode("utf-8")).hexdigest()}'
    )
    try:
        cached = cache.get(cache_key)
    except Exception:
        logger.exception(
            'Не удалось прочитать рабочий рейтинг из кэша; '
            'выполняется прямой расчёт.',
        )
        cached = None
    if cached is not None:
        return cached
    rating = build_driver_watch_rating(
        watch_period,
        shift_type=shift_type,
        allowed_employee_ids=allowed_employee_ids,
    )
    try:
        cache.set(cache_key, rating, DRIVER_RATING_CACHE_SECONDS)
    except Exception:
        logger.exception(
            'Не удалось сохранить рабочий рейтинг в кэш; '
            'рассчитанный результат возвращён без кэширования.',
        )
    return rating


def get_cached_driver_rating_period(
    rating_period,
    watch_composition,
    *,
    shift_type,
    allowed_employee_ids=None,
    expected_employee_ids=None,
):
    allowed_employee_ids = _normalize_employee_ids(allowed_employee_ids)
    expected_employee_ids = _normalize_employee_ids(
        expected_employee_ids
    )
    signature = _rating_period_source_signature(
        rating_period,
        watch_composition,
        shift_type,
        allowed_employee_ids=allowed_employee_ids,
        expected_employee_ids=expected_employee_ids,
    )
    scope_fingerprint = (
        'all'
        if allowed_employee_ids is None
        else hashlib.sha256(
            ','.join(map(str, allowed_employee_ids)).encode('utf-8')
        ).hexdigest()
    )
    expected_fingerprint = hashlib.sha256(
        ','.join(map(str, expected_employee_ids or ())).encode('utf-8')
    ).hexdigest()
    cache_identity = '|'.join((
        str(rating_period.id),
        str(watch_composition.id),
        shift_type,
        scope_fingerprint,
        expected_fingerprint,
        signature,
    ))
    cache_key = (
        'driver-rating-period:'
        f'{DRIVER_RATING_FORMULA_VERSION}:'
        f'{hashlib.sha256(cache_identity.encode("utf-8")).hexdigest()}'
    )
    try:
        cached = cache.get(cache_key)
    except Exception:
        logger.exception(
            'Не удалось прочитать рейтинг периода из кэша; '
            'выполняется прямой расчёт.',
        )
        cached = None
    if cached is not None:
        return cached
    rating = build_driver_rating_period(
        rating_period,
        watch_composition,
        shift_type=shift_type,
        allowed_employee_ids=allowed_employee_ids,
        expected_employee_ids=expected_employee_ids,
    )
    try:
        cache.set(cache_key, rating, DRIVER_RATING_CACHE_SECONDS)
    except Exception:
        logger.exception(
            'Не удалось сохранить рейтинг периода в кэш; '
            'рассчитанный результат возвращён без кэширования.',
        )
    return rating
