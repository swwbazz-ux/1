import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median

from django.db.models import F, Q
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from assignments.models import (
    HaulAssignment,
    HaulAssignmentAction,
)
from downtimes.models import DowntimeEvent
from shifts.models import EmployeeShift, ShiftReadingCorrection
from trips.models import Trip, TripStatus


class TimelineCategory:
    TRIP = 'trip'
    DOWNTIME_EXTERNAL = 'downtime_external'
    DOWNTIME_TECHNICAL = 'downtime_technical'
    DOWNTIME_REGULATED = 'downtime_regulated'
    DOWNTIME_REVIEW = 'downtime_review'
    NO_ASSIGNMENT = 'no_assignment'
    UNEXPLAINED = 'unexplained'
    DATA_CONFLICT = 'data_conflict'


DOWNTIME_CATEGORIES = {
    TimelineCategory.DOWNTIME_EXTERNAL,
    TimelineCategory.DOWNTIME_TECHNICAL,
    TimelineCategory.DOWNTIME_REGULATED,
    TimelineCategory.DOWNTIME_REVIEW,
}

REGULATED_DOWNTIME_NAMES = {
    'Заправка',
    'Обед',
    'Чистка кузова',
}

REVIEW_DOWNTIME_NAMES = {
    'Прочие',
}

EXTERNAL_DOWNTIME_NAMES = {
    'Ожидание погрузки',
    'Ожидание разгрузки',
    'Ожидание разгрузки ККД',
    'Ожидание разгрузки СКДР',
    'Ожидание фронта работ',
    'БВР',
    'Ожидание самосвалов',
    'Зачистка забоя',
    'Подготовка забоя',
    'Перегон экскаватора',
    'Климатические условия',
}

TECHNICAL_STATE_CODES = {
    'maintenance',
    'repair',
    'breakdown',
}

MIN_PLAUSIBLE_SHIFT_DURATION = timedelta(hours=1)
MAX_PLAUSIBLE_SHIFT_DURATION = timedelta(hours=16)
TRIP_SPAN_BLOCKING_FLAGS = {
    'invalid_trip_window',
    'completed_trip_without_completed_at',
    'open_status_trip_with_completed_at',
    'trip_unloading_shift_equipment_mismatch',
    'trip_driver_unloading_shift_mismatch',
    'trip_unloading_shift_time_mismatch',
    'invalid_trip_cancellation_window',
}


@dataclass(frozen=True)
class SourceSpan:
    start: datetime
    end: datetime
    source_type: str
    source_id: int
    label: str = ''
    category: str = ''


@dataclass(frozen=True)
class TimelineInterval:
    start: datetime
    end: datetime
    category: str
    source_ids: tuple[int, ...] = ()
    label: str = ''

    @property
    def duration_seconds(self):
        return max(0, int((self.end - self.start).total_seconds()))


@dataclass(frozen=True)
class DriverShiftTimeline:
    shift_id: int
    employee_id: int
    equipment_id: int
    start: datetime
    end: datetime
    intervals: tuple[TimelineInterval, ...]
    seconds_by_category: dict[str, int]
    total_seconds: int
    explained_seconds: int
    coverage_percent: float
    quality_flags: tuple[str, ...] = ()
    source_counts: dict[str, int] = None
    source_ids: dict[str, tuple[int, ...]] = None
    quality_metrics: dict[str, int] = None
    passport: dict = None
    cycle_samples: dict[tuple, tuple[int, ...]] = None

    @property
    def usable_for_formula_review(self):
        return bool(
            self.passport
            and self.passport.get(
                'rates_per_available_hour',
                {},
            ).get('is_formula_ready')
        )

    @property
    def productive_seconds(self):
        return self.seconds_by_category.get(TimelineCategory.TRIP, 0)

    @property
    def unexplained_seconds(self):
        return self.seconds_by_category.get(TimelineCategory.UNEXPLAINED, 0)

    @property
    def conflict_seconds(self):
        return self.seconds_by_category.get(TimelineCategory.DATA_CONFLICT, 0)

    @property
    def no_assignment_seconds(self):
        return self.seconds_by_category.get(TimelineCategory.NO_ASSIGNMENT, 0)

    @property
    def downtime_seconds(self):
        return sum(
            self.seconds_by_category.get(category, 0)
            for category in DOWNTIME_CATEGORIES
        )

    @property
    def available_seconds(self):
        return max(
            0,
            self.total_seconds - self.downtime_seconds - self.no_assignment_seconds,
        )


def classify_downtime_reason(reason):
    name = str(reason.name or '').strip()
    if name in REVIEW_DOWNTIME_NAMES:
        return TimelineCategory.DOWNTIME_REVIEW
    if name in REGULATED_DOWNTIME_NAMES:
        return TimelineCategory.DOWNTIME_REGULATED
    if reason.effective_equipment_state_code in TECHNICAL_STATE_CODES:
        return TimelineCategory.DOWNTIME_TECHNICAL
    if (
        reason.effective_equipment_state_code == 'waiting'
        and (reason.equipment_state_id or name in EXTERNAL_DOWNTIME_NAMES)
    ):
        return TimelineCategory.DOWNTIME_EXTERNAL
    return TimelineCategory.DOWNTIME_REVIEW


def _clip_span(start, end, window_start, window_end):
    clipped_start = max(start, window_start)
    clipped_end = min(end, window_end)
    if clipped_start >= clipped_end:
        return None
    return clipped_start, clipped_end


def _span_is_active(span, start, end):
    return span.start < end and span.end > start


def _source_ids(*groups):
    return tuple(sorted({span.source_id for group in groups for span in group}))


def _downtime_interval_category(active_downtimes):
    categories = {span.category for span in active_downtimes}
    if len(categories) == 1:
        return categories.pop()
    return TimelineCategory.DATA_CONFLICT


def _interval_for_slice(start, end, trips, downtimes, assignments):
    active_trips = [span for span in trips if _span_is_active(span, start, end)]
    active_downtimes = [
        span for span in downtimes if _span_is_active(span, start, end)
    ]
    active_assignments = [
        span for span in assignments if _span_is_active(span, start, end)
    ]

    has_conflict = (
        len(active_trips) > 1
        or len(active_downtimes) > 1
        or len(active_assignments) > 1
        or bool(active_trips and active_downtimes)
    )
    if has_conflict:
        return TimelineInterval(
            start=start,
            end=end,
            category=TimelineCategory.DATA_CONFLICT,
            source_ids=_source_ids(
                active_trips,
                active_downtimes,
                active_assignments,
            ),
            label='Противоречивые серверные события',
        )
    if active_trips:
        trip = active_trips[0]
        return TimelineInterval(
            start=start,
            end=end,
            category=TimelineCategory.TRIP,
            source_ids=(trip.source_id,),
            label=trip.label,
        )
    if active_downtimes:
        downtime = active_downtimes[0]
        return TimelineInterval(
            start=start,
            end=end,
            category=_downtime_interval_category(active_downtimes),
            source_ids=(downtime.source_id,),
            label=downtime.label,
        )
    if not active_assignments:
        return TimelineInterval(
            start=start,
            end=end,
            category=TimelineCategory.NO_ASSIGNMENT,
            label='Нет действующего назначения',
        )
    return TimelineInterval(
        start=start,
        end=end,
        category=TimelineCategory.UNEXPLAINED,
        source_ids=(active_assignments[0].source_id,),
        label='Необъяснённое время',
    )


def _merge_adjacent(intervals):
    merged = []
    for interval in intervals:
        if (
            merged
            and merged[-1].end == interval.start
            and merged[-1].category == interval.category
            and merged[-1].source_ids == interval.source_ids
            and merged[-1].label == interval.label
        ):
            previous = merged[-1]
            merged[-1] = TimelineInterval(
                start=previous.start,
                end=interval.end,
                category=previous.category,
                source_ids=previous.source_ids,
                label=previous.label,
            )
        else:
            merged.append(interval)
    return tuple(merged)


def _trip_spans(trips, window_start, window_end):
    spans = []
    for trip in trips:
        if _trip_quality_flags(trip) & TRIP_SPAN_BLOCKING_FLAGS:
            continue
        if trip.status == TripStatus.CANCELLED:
            if trip.cancelled_at is None:
                continue
            if trip.cancelled_at <= window_end:
                continue
            trip_end = window_end
        else:
            trip_end = trip.completed_at or window_end
        clipped = _clip_span(
            trip.created_at,
            trip_end,
            window_start,
            window_end,
        )
        if not clipped:
            continue
        spans.append(SourceSpan(
            start=clipped[0],
            end=clipped[1],
            source_type='trip',
            source_id=trip.id,
            label=f'Рейс: {trip.excavator} → {trip.dump_point}',
        ))
    return tuple(spans)


def _downtime_spans(downtimes, window_start, window_end):
    spans = []
    for event in downtimes:
        clipped = _clip_span(
            event.started_at,
            event.ended_at or window_end,
            window_start,
            window_end,
        )
        if not clipped:
            continue
        spans.append(SourceSpan(
            start=clipped[0],
            end=clipped[1],
            source_type='downtime',
            source_id=event.id,
            label=event.reason.name,
            category=classify_downtime_reason(event.reason),
        ))
    return tuple(spans)


def _assignment_spans(assignments, window_start, window_end):
    spans = []
    for assignment in assignments:
        active_from = assignment.accepted_at
        if active_from is None:
            continue
        clipped = _clip_span(
            active_from,
            assignment.ended_at or window_end,
            window_start,
            window_end,
        )
        if not clipped:
            continue
        spans.append(SourceSpan(
            start=clipped[0],
            end=clipped[1],
            source_type='assignment',
            source_id=assignment.id,
            label=f'Назначение под {assignment.excavator}',
        ))
    return tuple(spans)


def _driver_trip_records_for_shift(trips, shift, window_start, window_end):
    records = []
    for trip in trips:
        effective_end = (
            (trip.cancelled_at or trip.created_at)
            if trip.status == TripStatus.CANCELLED
            else trip.completed_at
        )
        loaded_during_shift = window_start <= trip.created_at < window_end
        unloaded_during_shift = trip.unloading_shift_id == shift.id
        completed_during_legacy_shift = bool(
            trip.unloading_shift_id is None
            and trip.completed_at is not None
            and window_start <= trip.completed_at < window_end
        )
        temporal_overlap = (
            trip.created_at < window_end
            and (
                effective_end is None
                or effective_end > window_start
            )
        )
        reverse_interval_overlap = (
            trip.completed_at is not None
            and trip.completed_at <= trip.created_at
            and trip.completed_at < window_end
            and trip.created_at > window_start
        )
        carryover_overlap = trip.is_carryover and temporal_overlap
        legacy_driver_match = (
            trip.unloading_shift_id is None
            and trip.driver_id == shift.employee_id
            and temporal_overlap
        )
        if (
            unloaded_during_shift
            or loaded_during_shift
            or completed_during_legacy_shift
            or reverse_interval_overlap
            or carryover_overlap
            or legacy_driver_match
        ):
            records.append(trip)
    return tuple(records)


def _trip_output_credit_status(trip, shift, window_start, window_end):
    if trip.status != TripStatus.COMPLETED:
        return None
    if trip.unloading_shift_id is not None:
        if trip.unloading_shift_id != shift.id:
            return None
        if (
            trip.completed_at is not None
            and trip.completed_at > window_end
        ):
            return None
        if (
            trip.completed_at is None
            or (
                _trip_quality_flags(trip)
                & TRIP_SPAN_BLOCKING_FLAGS
            )
        ):
            return 'ambiguous'
        return 'unloading_shift'
    if trip.completed_at is None:
        return None
    if not (window_start <= trip.completed_at < window_end):
        return None
    if _trip_quality_flags(trip) & TRIP_SPAN_BLOCKING_FLAGS:
        return 'ambiguous'
    if trip.driver_id == shift.employee_id:
        return 'legacy_driver'
    return 'ambiguous'


def _trip_is_open_at(trip, moment):
    if trip.created_at >= moment:
        return False
    if trip.completed_at is not None and trip.completed_at <= moment:
        return False
    if trip.status == TripStatus.CANCELLED:
        return (
            trip.cancelled_at is not None
            and trip.cancelled_at > moment
        )
    return True


def _strict_decimal_metric(records, value_getter):
    known_value = Decimal('0')
    complete_trip_count = 0
    missing_trip_count = 0
    for record in records:
        value = value_getter(record)
        if value is None:
            missing_trip_count += 1
            continue
        known_value += value
        complete_trip_count += 1
    return {
        'value': known_value if not missing_trip_count else None,
        'known_value': known_value,
        'complete_trip_count': complete_trip_count,
        'missing_trip_count': missing_trip_count,
        'is_complete': missing_trip_count == 0,
    }


def _non_negative(value):
    if value is None or value < 0:
        return None
    return value


def _per_available_hour(value, available_seconds):
    if value is None or available_seconds <= 0:
        return None
    return (
        Decimal(value) * Decimal('3600') / Decimal(available_seconds)
    ).quantize(Decimal('0.0001'))


def _rate_metric(metric, available_seconds, *, formula_ready):
    known_value = _per_available_hour(
        metric['known_value'],
        available_seconds,
    )
    strict_value = _per_available_hour(
        metric['value'],
        available_seconds,
    )
    return {
        'value': strict_value if formula_ready else None,
        'known_value': known_value,
        'is_complete': bool(
            formula_ready
            and metric['is_complete']
            and strict_value is not None
        ),
    }


def _count_metric(value):
    return {
        'value': Decimal(value),
        'known_value': Decimal(value),
        'complete_trip_count': value,
        'missing_trip_count': 0,
        'is_complete': True,
    }


def _decimal_delta(start, end):
    if start is None or end is None:
        return None
    return end - start


def _union_seconds(spans):
    ordered = sorted(
        (
            (span.start, span.end)
            for span in spans
            if span.start < span.end
        ),
        key=lambda item: (item[0], item[1]),
    )
    if not ordered:
        return 0
    total = 0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        total += int((current_end - current_start).total_seconds())
        current_start, current_end = start, end
    total += int((current_end - current_start).total_seconds())
    return max(0, total)


def _cycle_context(trip, shift):
    if (
        trip.truck.model_id is None
        or trip.excavator_id is None
        or trip.rock_type_id is None
        or trip.transport_distance_km is None
        or trip.transport_distance_km < 0
        or trip.actual_dump_point_id is None
    ):
        return None
    return (
        trip.truck.model_id,
        trip.excavator_id,
        trip.rock_type_id,
        trip.actual_dump_point_id,
        str(trip.transport_distance_km),
        trip.loading_horizon or '',
        trip.loading_block or '',
    )


def _cycle_samples_for_trips(trips, shift):
    samples = {}
    for trip in trips:
        if (
            trip.is_carryover
            or trip.created_at < shift.opened_at
            or trip.completed_at is None
            or trip.completed_at <= trip.created_at
            or _trip_quality_flags(trip)
        ):
            continue
        context = _cycle_context(trip, shift)
        if context is None:
            continue
        samples.setdefault(context, []).append(
            int((trip.completed_at - trip.created_at).total_seconds())
        )
    return {
        context: tuple(values)
        for context, values in samples.items()
    }


def cycle_statistics_from_samples(samples):
    segments = []
    for context, values in sorted(
        samples.items(),
        key=lambda item: tuple(str(value) for value in item[0]),
    ):
        ordered = sorted(values)
        sample_median = median(ordered)
        absolute_deviations = [
            abs(value - sample_median)
            for value in ordered
        ]
        segments.append({
            'context': {
                'truck_model_id': context[0],
                'excavator_id': context[1],
                'rock_type_id': context[2],
                'actual_dump_point_id': context[3],
                'transport_distance_km': context[4],
                'loading_horizon': context[5],
                'loading_block': context[6],
            },
            'sample_count': len(ordered),
            'average_seconds': round(sum(ordered) / len(ordered), 2),
            'median_seconds': round(float(sample_median), 2),
            'spread_seconds': max(ordered) - min(ordered),
            'median_absolute_deviation_seconds': round(
                float(median(absolute_deviations)),
                2,
            ),
        })
    return {
        'measurement': 'loaded_to_unloaded',
        'segment_count': len(segments),
        'sample_count': sum(
            segment['sample_count']
            for segment in segments
        ),
        'segments': segments,
        'status': (
            'observed'
            if segments
            else 'comparable_samples_unavailable'
        ),
    }


def cycle_aggregation_inputs_from_samples(samples):
    return [
        {
            'context': {
                'truck_model_id': context[0],
                'excavator_id': context[1],
                'rock_type_id': context[2],
                'actual_dump_point_id': context[3],
                'transport_distance_km': context[4],
                'loading_horizon': context[5],
                'loading_block': context[6],
            },
            'durations_seconds': list(sorted(values)),
        }
        for context, values in sorted(
            samples.items(),
            key=lambda item: tuple(str(value) for value in item[0]),
        )
    ]


def _passport_source_fingerprint(passport, intervals, source_ids):
    canonical = {
        'passport': passport,
        'intervals': [
            {
                'start': interval.start,
                'end': interval.end,
                'category': interval.category,
                'source_ids': interval.source_ids,
            }
            for interval in intervals
        ],
        'source_ids': source_ids,
    }
    encoded = json.dumps(
        canonical,
        cls=DjangoJSONEncoder,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _build_shift_passport(
    shift,
    *,
    window_start,
    window_end,
    trip_records,
    assignment_spans,
    corrections,
    seconds_by_category,
    total_seconds,
    explained_seconds,
    coverage_percent,
    quality_flags,
    source_counts,
    quality_metrics,
):
    credit_statuses = {
        trip.id: _trip_output_credit_status(
            trip,
            shift,
            window_start,
            window_end,
        )
        for trip in trip_records
    }
    if {
        'equipment_shift_overlap',
        'employee_shift_overlap',
    } & set(quality_flags):
        credit_statuses = {
            trip_id: (
                'ambiguous'
                if status == 'legacy_driver'
                else status
            )
            for trip_id, status in credit_statuses.items()
        }
    credited_trips = tuple(
        trip
        for trip in trip_records
        if credit_statuses[trip.id] in {
            'unloading_shift',
            'legacy_driver',
        }
    )
    ambiguous_output_trip_count = sum(
        status == 'ambiguous'
        for status in credit_statuses.values()
    )
    volume_metric = _strict_decimal_metric(
        credited_trips,
        lambda trip: _non_negative(trip.volume_m3),
    )
    tonnage_metric = _strict_decimal_metric(
        credited_trips,
        lambda trip: _non_negative(trip.tonnage),
    )
    m3_km_metric = _strict_decimal_metric(
        credited_trips,
        lambda trip: (
            trip.volume_m3 * trip.transport_distance_km
            if (
                trip.volume_m3 is not None
                and trip.volume_m3 >= 0
                and trip.transport_distance_km is not None
                and trip.transport_distance_km >= 0
            )
            else None
        ),
    )
    t_km_metric = _strict_decimal_metric(
        credited_trips,
        lambda trip: (
            trip.tonnage * trip.transport_distance_km
            if (
                trip.tonnage is not None
                and trip.tonnage >= 0
                and trip.transport_distance_km is not None
                and trip.transport_distance_km >= 0
            )
            else None
        ),
    )
    completed_trip_metric = _count_metric(len(credited_trips))

    downtime_seconds = sum(
        seconds_by_category.get(category, 0)
        for category in DOWNTIME_CATEGORIES
    )
    no_assignment_seconds = seconds_by_category.get(
        TimelineCategory.NO_ASSIGNMENT,
        0,
    )
    available_seconds = max(
        0,
        total_seconds - downtime_seconds - no_assignment_seconds,
    )
    production_metrics_complete = all(
        metric['is_complete']
        for metric in (
            volume_metric,
            tonnage_metric,
            m3_km_metric,
            t_km_metric,
        )
    )
    formula_ready = bool(
        not quality_flags
        and available_seconds > 0
        and production_metrics_complete
    )

    loaded_trip_count = sum(
        window_start <= trip.created_at < window_end
        for trip in trip_records
    )
    cancelled_trip_count = sum(
        trip.status == TripStatus.CANCELLED
        and trip.cancelled_at is not None
        and window_start < trip.cancelled_at <= window_end
        for trip in trip_records
    )
    open_trip_count_at_close = sum(
        _trip_is_open_at(trip, window_end)
        for trip in trip_records
    )
    carryover_in_trip_count = sum(
        trip.is_carryover
        and _trip_is_open_at(trip, window_start)
        for trip in trip_records
    )
    carryover_out_trip_count = sum(
        trip.is_carryover
        and _trip_is_open_at(trip, window_end)
        for trip in trip_records
    )

    explicit_route_trips = tuple(
        trip
        for trip in credited_trips
        if (
            trip.assigned_dump_point_id is not None
            and trip.actual_dump_point_id is not None
        )
    )
    route_match_count = sum(
        trip.assigned_dump_point_id == trip.actual_dump_point_id
        for trip in explicit_route_trips
    )
    route_mismatch_count = len(explicit_route_trips) - route_match_count

    corrections_by_metric = {}
    for correction in corrections:
        item = corrections_by_metric.setdefault(
            correction.metric,
            {
                'count': 0,
                'total_absolute_difference': Decimal('0'),
            },
        )
        item['count'] += 1
        item['total_absolute_difference'] += abs(
            correction.actual_value - correction.transferred_value
        )

    cycle_samples = _cycle_samples_for_trips(credited_trips, shift)
    return {
        'passport_schema_version': 1,
        'scope': 'employee_shift',
        'shift': {
            'id': shift.id,
            'employee_id': shift.employee_id,
            'equipment_id': shift.equipment_id,
            'watch_period_id': shift.watch_period_id,
            'shift_type': shift.shift_type,
        },
        'production': {
            'completed_trip_count': len(credited_trips),
            'output_attribution': {
                'unloading_shift_trip_count': sum(
                    status == 'unloading_shift'
                    for status in credit_statuses.values()
                ),
                'legacy_driver_trip_count': sum(
                    status == 'legacy_driver'
                    for status in credit_statuses.values()
                ),
                'ambiguous_trip_count': ambiguous_output_trip_count,
            },
            'volume_m3': volume_metric,
            'tonnage_t': tonnage_metric,
            'm3_km': m3_km_metric,
            't_km': t_km_metric,
            'completeness': {
                'credited_trip_count': len(credited_trips),
                'volume_missing_trip_count': (
                    volume_metric['missing_trip_count']
                ),
                'tonnage_missing_trip_count': (
                    tonnage_metric['missing_trip_count']
                ),
                'distance_missing_trip_count': sum(
                    trip.transport_distance_km is None
                    for trip in credited_trips
                ),
                'cycle_calibration_excluded_carryover_trip_count': sum(
                    trip.is_carryover
                    or trip.created_at < shift.opened_at
                    for trip in credited_trips
                ),
                'comparison_context_missing_trip_count': (
                    len(credited_trips)
                    - sum(len(values) for values in cycle_samples.values())
                ),
            },
        },
        'rates_per_available_hour': {
            'trip_count': _rate_metric(
                completed_trip_metric,
                available_seconds,
                formula_ready=formula_ready,
            ),
            'volume_m3': _rate_metric(
                volume_metric,
                available_seconds,
                formula_ready=formula_ready,
            ),
            'tonnage_t': _rate_metric(
                tonnage_metric,
                available_seconds,
                formula_ready=formula_ready,
            ),
            'm3_km': _rate_metric(
                m3_km_metric,
                available_seconds,
                formula_ready=formula_ready,
            ),
            't_km': _rate_metric(
                t_km_metric,
                available_seconds,
                formula_ready=formula_ready,
            ),
            'is_formula_ready': formula_ready,
        },
        'expected': {
            'actual_to_expected_ratio': None,
            'status': 'expected_model_unavailable',
        },
        'time': {
            'actual_start_at': window_start.isoformat(),
            'actual_end_at': window_end.isoformat(),
            'actual_duration_seconds': total_seconds,
            'scheduled_start_at': None,
            'scheduled_end_at': None,
            'scheduled_window_status': 'schedule_snapshot_unavailable',
            'start_deviation_seconds': None,
            'end_deviation_seconds': None,
            'confirmed_extra_productive_seconds': None,
            'unjustified_short_shift_seconds': None,
            'short_shift_status': 'policy_unavailable',
            'available_seconds': available_seconds,
            'loaded_to_unloaded_seconds': seconds_by_category.get(
                TimelineCategory.TRIP,
                0,
            ),
            'assignment_covered_seconds': _union_seconds(assignment_spans),
            'downtime_external_seconds': seconds_by_category.get(
                TimelineCategory.DOWNTIME_EXTERNAL,
                0,
            ),
            'downtime_technical_seconds': seconds_by_category.get(
                TimelineCategory.DOWNTIME_TECHNICAL,
                0,
            ),
            'downtime_regulated_seconds': seconds_by_category.get(
                TimelineCategory.DOWNTIME_REGULATED,
                0,
            ),
            'downtime_review_seconds': seconds_by_category.get(
                TimelineCategory.DOWNTIME_REVIEW,
                0,
            ),
            'no_assignment_seconds': no_assignment_seconds,
            'unexplained_seconds': seconds_by_category.get(
                TimelineCategory.UNEXPLAINED,
                0,
            ),
            'conflict_seconds': seconds_by_category.get(
                TimelineCategory.DATA_CONFLICT,
                0,
            ),
        },
        'cycles': cycle_statistics_from_samples(cycle_samples),
        'aggregation_inputs': {
            'cycle_samples': cycle_aggregation_inputs_from_samples(
                cycle_samples
            ),
        },
        'routing': {
            'credited_trip_count': len(credited_trips),
            'explicit_assigned_and_actual_count': len(explicit_route_trips),
            'match_count': route_match_count,
            'mismatch_count': route_mismatch_count,
            'missing_assigned_count': sum(
                trip.assigned_dump_point_id is None
                for trip in credited_trips
            ),
            'missing_actual_count': sum(
                trip.actual_dump_point_id is None
                for trip in credited_trips
            ),
        },
        'trip_states': {
            'loaded_during_shift_count': loaded_trip_count,
            'open_at_close_count': open_trip_count_at_close,
            'cancelled_count': cancelled_trip_count,
            'carryover_in_count': carryover_in_trip_count,
            'carryover_out_count': carryover_out_trip_count,
        },
        'open_close': {
            'opened_by_employee': shift.opened_by_id == shift.employee_id,
            'closed_by_employee': shift.closed_by_id == shift.employee_id,
            'service_closed': shift.is_service_closed,
            'window_valid': window_end > window_start,
            'start_readings_complete': all(
                value is not None
                for value in (
                    shift.start_fuel,
                    shift.start_mileage,
                    shift.start_engine_hours,
                )
            ),
            'end_readings_complete': all(
                value is not None
                for value in (
                    shift.end_fuel,
                    shift.end_mileage,
                    shift.end_engine_hours,
                )
            ),
        },
        'handover': {
            'start_fuel_l': shift.start_fuel,
            'end_fuel_l': shift.end_fuel,
            'net_fuel_change_l': _decimal_delta(
                shift.start_fuel,
                shift.end_fuel,
            ),
            'start_mileage_km': shift.start_mileage,
            'end_mileage_km': shift.end_mileage,
            'mileage_delta_km': _decimal_delta(
                shift.start_mileage,
                shift.end_mileage,
            ),
            'start_engine_hours': shift.start_engine_hours,
            'end_engine_hours': shift.end_engine_hours,
            'engine_hours_delta': _decimal_delta(
                shift.start_engine_hours,
                shift.end_engine_hours,
            ),
            'reading_correction_count': len(corrections),
            'corrections_by_metric': corrections_by_metric,
            'fuel_metric_status': 'net_change_not_consumption',
        },
        'quality': {
            'coverage_percent': coverage_percent,
            'explained_seconds': explained_seconds,
            'flags': list(quality_flags),
            'source_counts': source_counts,
            'quality_metrics': quality_metrics,
            'production_metrics_complete': (
                production_metrics_complete
            ),
            'data_usable_for_formula_review': formula_ready,
            'official_rating_eligible': False,
        },
        '_cycle_samples': cycle_samples,
    }


def _trip_quality_flags(trip):
    flags = set()
    if trip.volume_m3 is not None and trip.volume_m3 < 0:
        flags.add('negative_trip_volume_m3')
    if trip.tonnage is not None and trip.tonnage < 0:
        flags.add('negative_trip_tonnage')
    if (
        trip.planned_volume_m3 is not None
        and trip.planned_volume_m3 < 0
    ):
        flags.add('negative_trip_planned_volume_m3')
    if (
        trip.transport_distance_km is not None
        and trip.transport_distance_km < 0
    ):
        flags.add('negative_trip_transport_distance_km')
    if (
        trip.status == TripStatus.COMPLETED
        and trip.completed_at is None
    ):
        flags.add('completed_trip_without_completed_at')
    if (
        trip.status in {
            TripStatus.ACTIVE,
            TripStatus.LOADED_WAITING_UNLOAD,
        }
        and trip.completed_at is not None
    ):
        flags.add('open_status_trip_with_completed_at')
    if (
        trip.completed_at is not None
        and trip.completed_at <= trip.created_at
    ):
        flags.add('invalid_trip_window')
    if (
        trip.status == TripStatus.CANCELLED
        and trip.cancelled_at is None
    ):
        flags.add('cancelled_trip_without_cancelled_at')
    if (
        trip.cancelled_at is not None
        and trip.cancelled_at < trip.created_at
    ):
        flags.add('invalid_trip_cancellation_window')
    if (
        trip.status != TripStatus.CANCELLED
        and trip.cancelled_at is not None
    ):
        flags.add('non_cancelled_trip_with_cancelled_at')

    loading_shift = trip.loading_shift
    if loading_shift is not None:
        if loading_shift.equipment_id != trip.excavator_id:
            flags.add('trip_loading_shift_equipment_mismatch')
        if trip.excavator_operator_id is None:
            flags.add('trip_operator_missing_for_loading_shift')
        elif trip.excavator_operator_id != loading_shift.employee_id:
            flags.add('trip_operator_loading_shift_mismatch')
        if (
            trip.created_at < loading_shift.opened_at
            or (
                loading_shift.closed_at is not None
                and trip.created_at > loading_shift.closed_at
            )
        ):
            flags.add('trip_loading_shift_time_mismatch')

    unloading_shift = trip.unloading_shift
    if unloading_shift is None:
        return flags
    if unloading_shift.equipment_id != trip.truck_id:
        flags.add('trip_unloading_shift_equipment_mismatch')
    if trip.driver_id != unloading_shift.employee_id:
        flags.add('trip_driver_unloading_shift_mismatch')
    if trip.completed_at is not None and (
        trip.completed_at < unloading_shift.opened_at
        or (
            unloading_shift.closed_at is not None
            and trip.completed_at > unloading_shift.closed_at
        )
    ):
        flags.add('trip_unloading_shift_time_mismatch')
    return flags


def _downtime_quality_flags(event):
    if (
        event.ended_at is not None
        and event.ended_at <= event.started_at
    ):
        return {'invalid_downtime_window'}
    return set()


def _assignment_quality_flags(assignment):
    if (
        assignment.ended_at is not None
        and assignment.ended_at <= assignment.accepted_at
    ):
        return {'invalid_assignment_window'}
    return set()


def _record_relates_to_window(start, end, window_start, window_end):
    start_in_window = window_start <= start < window_end
    end_in_window = (
        end is not None
        and window_start < end <= window_end
    )
    overlaps_window = (
        start < window_end
        and (end is None or end > window_start)
    )
    reverse_interval_overlap = (
        end is not None
        and end <= start
        and end < window_end
        and start > window_start
    )
    return (
        start_in_window
        or end_in_window
        or overlaps_window
        or reverse_interval_overlap
    )


def _assignments_at_trip_loading(trip, assignments):
    return tuple(
        assignment
        for assignment in assignments
        if not _assignment_quality_flags(assignment)
        and assignment.accepted_at <= trip.created_at
        and (
            assignment.ended_at is None
            or trip.created_at < assignment.ended_at
        )
    )


def _build_timeline(
    shift,
    *,
    now,
    trips,
    downtimes,
    assignments,
    corrections,
    extra_quality_flags=(),
):
    window_start = shift.opened_at
    window_end = min(shift.closed_at, now) if shift.closed_at else now
    if window_end <= window_start:
        raise ValueError('Окончание временной ленты должно быть позже начала смены.')

    trip_records = _driver_trip_records_for_shift(
        trips,
        shift,
        window_start,
        window_end,
    )
    trips = _trip_spans(trip_records, window_start, window_end)
    downtime_records = tuple(
        event
        for event in downtimes
        if _record_relates_to_window(
            event.started_at,
            event.ended_at,
            window_start,
            window_end,
        )
    )
    all_assignments = tuple(assignments)
    assignment_records = tuple(
        assignment
        for assignment in all_assignments
        if assignment.accepted_at is not None
        and _record_relates_to_window(
            assignment.accepted_at,
            assignment.ended_at,
            window_start,
            window_end,
        )
    )
    downtimes = _downtime_spans(
        downtime_records,
        window_start,
        window_end,
    )
    assignments = _assignment_spans(
        assignment_records,
        window_start,
        window_end,
    )

    boundaries = {window_start, window_end}
    for span in (*trips, *downtimes, *assignments):
        boundaries.add(span.start)
        boundaries.add(span.end)
    ordered_boundaries = sorted(boundaries)
    unmatched_trip_ids = set()
    mismatched_trip_ids = set()
    trip_loading_assignment_records = {}
    for trip in trip_records:
        if (
            trip.status == TripStatus.CANCELLED
            and trip.cancelled_at is None
        ):
            continue
        if _trip_quality_flags(trip) & TRIP_SPAN_BLOCKING_FLAGS:
            continue
        loading_assignments = _assignments_at_trip_loading(
            trip,
            all_assignments,
        )
        for assignment in loading_assignments:
            trip_loading_assignment_records[assignment.id] = assignment
        matching_assignment = any(
            assignment.excavator_id == trip.excavator_id
            for assignment in loading_assignments
        )
        if matching_assignment:
            continue
        unmatched_trip_ids.add(trip.id)
        if loading_assignments:
            mismatched_trip_ids.add(trip.id)

    trip_without_assignment_seconds = 0
    trip_assignment_mismatch_seconds = 0
    for start, end in zip(ordered_boundaries, ordered_boundaries[1:]):
        if start >= end:
            continue
        active_trips = [
            span
            for span in trips
            if _span_is_active(span, start, end)
        ]
        if not active_trips:
            continue
        duration_seconds = max(0, int((end - start).total_seconds()))
        if any(
            trip.source_id in unmatched_trip_ids
            for trip in active_trips
        ):
            trip_without_assignment_seconds += duration_seconds
        if any(
            trip.source_id in mismatched_trip_ids
            for trip in active_trips
        ):
            trip_assignment_mismatch_seconds += duration_seconds

    intervals = _merge_adjacent([
        _interval_for_slice(
            start,
            end,
            trips,
            downtimes,
            assignments,
        )
        for start, end in zip(ordered_boundaries, ordered_boundaries[1:])
        if start < end
    ])

    seconds_by_category = {}
    for interval in intervals:
        seconds_by_category[interval.category] = (
            seconds_by_category.get(interval.category, 0)
            + interval.duration_seconds
        )
    total_seconds = max(0, int((window_end - window_start).total_seconds()))
    unexplained_or_conflict = (
        seconds_by_category.get(TimelineCategory.UNEXPLAINED, 0)
        + seconds_by_category.get(TimelineCategory.DATA_CONFLICT, 0)
    )
    explained_seconds = max(0, total_seconds - unexplained_or_conflict)
    coverage_percent = (
        round(explained_seconds * 100 / total_seconds, 2)
        if total_seconds
        else 0.0
    )
    duration = window_end - window_start
    quality_flags = list(extra_quality_flags)
    for trip in trip_records:
        quality_flags.extend(_trip_quality_flags(trip))
    for event in downtime_records:
        quality_flags.extend(_downtime_quality_flags(event))
    for assignment in assignment_records:
        quality_flags.extend(_assignment_quality_flags(assignment))
    if duration < MIN_PLAUSIBLE_SHIFT_DURATION:
        quality_flags.append('shift_duration_under_1h')
    if duration > MAX_PLAUSIBLE_SHIFT_DURATION:
        quality_flags.append('shift_duration_over_16h')
    if seconds_by_category.get(TimelineCategory.DATA_CONFLICT, 0):
        quality_flags.append('data_conflict')
    if seconds_by_category.get(TimelineCategory.UNEXPLAINED, 0):
        quality_flags.append('unexplained_time')
    if seconds_by_category.get(TimelineCategory.DOWNTIME_REVIEW, 0):
        quality_flags.append('downtime_requires_review')
    if trip_without_assignment_seconds:
        quality_flags.append('trip_without_assignment')
    if trip_assignment_mismatch_seconds:
        quality_flags.append('trip_assignment_mismatch')
    if shift.closed_at and any(
        _trip_is_open_at(trip, window_end)
        for trip in trip_records
    ):
        quality_flags.append('open_trip_on_closed_shift')
    if any(
        trip.completed_at
        and trip.completed_at - trip.created_at > MAX_PLAUSIBLE_SHIFT_DURATION
        for trip in trip_records
        if trip.status != TripStatus.CANCELLED
    ):
        quality_flags.append('trip_duration_over_16h')
    if any(
        _trip_output_credit_status(
            trip,
            shift,
            window_start,
            window_end,
        ) == 'ambiguous'
        for trip in trip_records
    ):
        quality_flags.append('ambiguous_trip_output_attribution')

    source_ids = {
        'trip_count': tuple(sorted({
            trip.id
            for trip in trip_records
            if (
                trip.status != TripStatus.CANCELLED
                or (
                    trip.cancelled_at is not None
                    and trip.cancelled_at > window_end
                )
            )
        })),
        'carryover_trip_count': tuple(sorted({
            trip.id
            for trip in trip_records
            if (
                trip.is_carryover
                and (
                    trip.status != TripStatus.CANCELLED
                    or (
                        trip.cancelled_at is not None
                        and trip.cancelled_at > window_end
                    )
                )
            )
        })),
        'downtime_event_count': tuple(sorted({
            event.id
            for event in downtime_records
        })),
        'downtime_reported_by_employee_count': tuple(sorted({
            event.id
            for event in downtime_records
            if event.employee_id == shift.employee_id
        })),
        'downtime_reported_by_other_count': tuple(sorted({
            event.id
            for event in downtime_records
            if (
                event.employee_id is not None
                and event.employee_id != shift.employee_id
            )
        })),
        'downtime_without_employee_count': tuple(sorted({
            event.id
            for event in downtime_records
            if event.employee_id is None
        })),
        'assignment_count': tuple(sorted({
            assignment.id
            for assignment in (
                *assignment_records,
                *trip_loading_assignment_records.values(),
            )
        })),
    }
    quality_flags = tuple(sorted(set(quality_flags)))
    source_counts = {
        key: len(ids)
        for key, ids in source_ids.items()
    }
    quality_metrics = {
        'trip_without_assignment_seconds': (
            trip_without_assignment_seconds
        ),
        'trip_assignment_mismatch_seconds': (
            trip_assignment_mismatch_seconds
        ),
    }
    passport = _build_shift_passport(
        shift,
        window_start=window_start,
        window_end=window_end,
        trip_records=trip_records,
        assignment_spans=assignments,
        corrections=corrections,
        seconds_by_category=seconds_by_category,
        total_seconds=total_seconds,
        explained_seconds=explained_seconds,
        coverage_percent=coverage_percent,
        quality_flags=quality_flags,
        source_counts=source_counts,
        quality_metrics=quality_metrics,
    )
    cycle_samples = passport.pop('_cycle_samples')
    passport['source_fingerprint'] = _passport_source_fingerprint(
        passport,
        intervals,
        source_ids,
    )
    return DriverShiftTimeline(
        shift_id=shift.id,
        employee_id=shift.employee_id,
        equipment_id=shift.equipment_id,
        start=window_start,
        end=window_end,
        intervals=intervals,
        seconds_by_category=seconds_by_category,
        total_seconds=total_seconds,
        explained_seconds=explained_seconds,
        coverage_percent=coverage_percent,
        quality_flags=quality_flags,
        source_counts=source_counts,
        source_ids=source_ids,
        quality_metrics=quality_metrics,
        passport=passport,
        cycle_samples=cycle_samples,
    )


def _empty_source_ids():
    return {
        'trip_count': (),
        'carryover_trip_count': (),
        'downtime_event_count': (),
        'downtime_reported_by_employee_count': (),
        'downtime_reported_by_other_count': (),
        'downtime_without_employee_count': (),
        'assignment_count': (),
    }


def _invalid_shift_timeline(shift, *, extra_quality_flags=()):
    source_ids = _empty_source_ids()
    quality_flags = tuple(sorted({
        'invalid_shift_window',
        *extra_quality_flags,
    }))
    source_counts = {
        key: 0
        for key in source_ids
    }
    quality_metrics = {
        'trip_without_assignment_seconds': 0,
        'trip_assignment_mismatch_seconds': 0,
    }
    passport = _build_shift_passport(
        shift,
        window_start=shift.opened_at,
        window_end=shift.opened_at,
        trip_records=(),
        assignment_spans=(),
        corrections=(),
        seconds_by_category={},
        total_seconds=0,
        explained_seconds=0,
        coverage_percent=0.0,
        quality_flags=quality_flags,
        source_counts=source_counts,
        quality_metrics=quality_metrics,
    )
    cycle_samples = passport.pop('_cycle_samples')
    passport['source_fingerprint'] = _passport_source_fingerprint(
        passport,
        (),
        source_ids,
    )
    return DriverShiftTimeline(
        shift_id=shift.id,
        employee_id=shift.employee_id,
        equipment_id=shift.equipment_id,
        start=shift.opened_at,
        end=shift.opened_at,
        intervals=(),
        seconds_by_category={},
        total_seconds=0,
        explained_seconds=0,
        coverage_percent=0.0,
        quality_flags=quality_flags,
        source_counts=source_counts,
        source_ids=source_ids,
        quality_metrics=quality_metrics,
        passport=passport,
        cycle_samples=cycle_samples,
    )


def _shift_has_invalid_window(shift, now):
    if shift.opened_at >= now:
        return True
    if shift.closed_at is None:
        return False
    return (
        shift.closed_at <= shift.opened_at
        or shift.closed_at > now
    )


def _overlap_quality_flags_against_all_shifts(shifts, *, now):
    if not shifts:
        return {}

    window_start = min(shift.opened_at for shift in shifts)
    window_end = max(
        shift.closed_at if shift.closed_at else now
        for shift in shifts
    )
    equipment_ids = {shift.equipment_id for shift in shifts}
    employee_ids = {shift.employee_id for shift in shifts}
    candidates = tuple(
        EmployeeShift.objects
        .filter(opened_at__lt=window_end)
        .filter(
            Q(closed_at__isnull=True)
            | Q(closed_at__gt=window_start)
        )
        .filter(
            Q(equipment_id__in=equipment_ids)
            | Q(employee_id__in=employee_ids)
        )
        .only(
            'id',
            'employee_id',
            'equipment_id',
            'opened_at',
            'closed_at',
        )
        .order_by('opened_at', 'id')
    )
    candidates_by_equipment = {}
    candidates_by_employee = {}
    for candidate in candidates:
        candidate_end = candidate.closed_at or now
        if (
            candidate.opened_at >= now
            or candidate_end <= candidate.opened_at
            or candidate_end > now
        ):
            continue
        candidates_by_equipment.setdefault(
            candidate.equipment_id,
            [],
        ).append(candidate)
        candidates_by_employee.setdefault(
            candidate.employee_id,
            [],
        ).append(candidate)

    flags_by_shift = {}
    for shift in shifts:
        shift_end = shift.closed_at or now
        equipment_overlap = any(
            candidate.id != shift.id
            and candidate.opened_at < shift_end
            and (candidate.closed_at or now) > shift.opened_at
            for candidate in candidates_by_equipment.get(
                shift.equipment_id,
                (),
            )
        )
        employee_overlap = any(
            candidate.id != shift.id
            and candidate.opened_at < shift_end
            and (candidate.closed_at or now) > shift.opened_at
            for candidate in candidates_by_employee.get(
                shift.employee_id,
                (),
            )
        )
        if equipment_overlap:
            flags_by_shift.setdefault(shift.id, set()).add(
                'equipment_shift_overlap'
            )
        if employee_overlap:
            flags_by_shift.setdefault(shift.id, set()).add(
                'employee_shift_overlap'
            )
    return flags_by_shift


def build_driver_shift_timelines(
    shifts,
    *,
    as_of=None,
    extra_quality_flags_by_shift=None,
):
    shifts = tuple(shifts)
    if not shifts:
        return ()
    extra_quality_flags_by_shift = extra_quality_flags_by_shift or {}

    for shift in shifts:
        if not shift.pk:
            raise ValueError(
                'Смена должна быть сохранена перед расчётом временной ленты.'
            )
        if not shift.equipment_id:
            raise ValueError('Для временной ленты Водителя в смене нужна техника.')

    now = as_of or timezone.now()
    invalid_shift_ids = {
        shift.id
        for shift in shifts
        if _shift_has_invalid_window(shift, now)
    }
    valid_shifts = tuple(
        shift
        for shift in shifts
        if shift.id not in invalid_shift_ids
    )
    if not valid_shifts:
        return tuple(
            _invalid_shift_timeline(
                shift,
                extra_quality_flags=extra_quality_flags_by_shift.get(
                    shift.id,
                    (),
                ),
            )
            for shift in shifts
        )

    overlap_flags = _overlap_quality_flags_against_all_shifts(
        valid_shifts,
        now=now,
    )
    window_start = min(shift.opened_at for shift in valid_shifts)
    window_end = max(
        min(shift.closed_at, now) if shift.closed_at else now
        for shift in valid_shifts
    )
    equipment_ids = {shift.equipment_id for shift in valid_shifts}
    valid_shift_ids = {shift.id for shift in valid_shifts}
    valid_shift_equipment_by_id = {
        shift.id: shift.equipment_id
        for shift in valid_shifts
    }

    trips_by_equipment = {equipment_id: [] for equipment_id in equipment_ids}
    trips = (
        Trip.objects
        .filter(
            Q(truck_id__in=equipment_ids)
            | Q(unloading_shift_id__in=valid_shift_ids)
        )
        .filter(
            Q(
                created_at__gte=window_start,
                created_at__lt=window_end,
            )
            | Q(
                completed_at__gte=window_start,
                completed_at__lte=window_end,
            )
            | Q(
                cancelled_at__gte=window_start,
                cancelled_at__lte=window_end,
            )
            | Q(unloading_shift_id__in=valid_shift_ids)
            | Q(
                created_at__lt=window_end,
                completed_at__isnull=True,
                status__in={
                    TripStatus.ACTIVE,
                    TripStatus.LOADED_WAITING_UNLOAD,
                },
            )
            | Q(
                created_at__lt=window_end,
                completed_at__gt=window_start,
            )
            | Q(
                created_at__lt=window_end,
                cancelled_at__gt=window_start,
            )
            | (
                Q(completed_at__isnull=False)
                & Q(completed_at__lte=F('created_at'))
                & Q(completed_at__lt=window_end)
                & Q(created_at__gt=window_start)
            )
        )
        .select_related(
            'excavator',
            'excavator__equipment_type',
            'truck',
            'truck__model',
            'dump_point',
            'assigned_dump_point',
            'actual_dump_point',
            'rock_type',
            'loading_shift',
            'unloading_shift',
        )
        .order_by('created_at', 'id')
    )
    for trip in trips:
        related_equipment_ids = set()
        if trip.truck_id in trips_by_equipment:
            related_equipment_ids.add(trip.truck_id)
        unloading_equipment_id = valid_shift_equipment_by_id.get(
            trip.unloading_shift_id,
        )
        if unloading_equipment_id is not None:
            related_equipment_ids.add(unloading_equipment_id)
        for equipment_id in related_equipment_ids:
            trips_by_equipment[equipment_id].append(trip)
    assignment_trip_load_times = [
        trip.created_at
        for trip in trips
        if (
            (
                trip.status != TripStatus.CANCELLED
                or trip.cancelled_at is not None
            )
            and not (
                _trip_quality_flags(trip)
                & TRIP_SPAN_BLOCKING_FLAGS
            )
        )
    ]

    downtimes_by_equipment = {equipment_id: [] for equipment_id in equipment_ids}
    downtimes = (
        DowntimeEvent.objects
        .filter(equipment_id__in=equipment_ids)
        .filter(
            Q(
                started_at__gte=window_start,
                started_at__lt=window_end,
            )
            | Q(
                ended_at__gt=window_start,
                ended_at__lte=window_end,
            )
            | Q(
                started_at__lt=window_end,
                ended_at__isnull=True,
            )
            | Q(
                started_at__lt=window_end,
                ended_at__gt=window_start,
            )
            | (
                Q(ended_at__isnull=False)
                & Q(ended_at__lte=F('started_at'))
                & Q(ended_at__lt=window_end)
                & Q(started_at__gt=window_start)
            )
        )
        .select_related('reason', 'reason__equipment_state')
        .order_by('started_at', 'id')
    )
    for downtime in downtimes:
        downtimes_by_equipment[downtime.equipment_id].append(downtime)

    assignments_by_equipment = {
        equipment_id: []
        for equipment_id in equipment_ids
    }
    assignment_relevance = (
        Q(
            accepted_at__gte=window_start,
            accepted_at__lt=window_end,
        )
        | Q(
            ended_at__gt=window_start,
            ended_at__lte=window_end,
        )
        | Q(
            accepted_at__lt=window_end,
            ended_at__isnull=True,
        )
        | Q(
            accepted_at__lt=window_end,
            ended_at__gt=window_start,
        )
        | (
            Q(ended_at__isnull=False)
            & Q(ended_at__lte=F('accepted_at'))
            & Q(ended_at__lt=window_end)
            & Q(accepted_at__gt=window_start)
        )
    )
    if assignment_trip_load_times:
        assignment_relevance |= (
            Q(accepted_at__lte=max(assignment_trip_load_times))
            & (
                Q(ended_at__isnull=True)
                | Q(ended_at__gt=min(assignment_trip_load_times))
            )
        )
    assignments = (
        HaulAssignment.objects
        .filter(
            truck_id__in=equipment_ids,
            action=HaulAssignmentAction.ASSIGN,
            accepted_at__isnull=False,
        )
        .filter(assignment_relevance)
        .select_related('excavator', 'excavator__equipment_type')
        .order_by('accepted_at', 'id')
    )
    for assignment in assignments:
        assignments_by_equipment[assignment.truck_id].append(assignment)

    corrections_by_shift = {
        shift_id: []
        for shift_id in valid_shift_ids
    }
    corrections = (
        ShiftReadingCorrection.objects
        .filter(new_shift_id__in=valid_shift_ids)
        .only(
            'id',
            'new_shift_id',
            'metric',
            'transferred_value',
            'actual_value',
        )
        .order_by('new_shift_id', 'metric', 'id')
    )
    for correction in corrections:
        corrections_by_shift[correction.new_shift_id].append(correction)

    timelines_by_shift_id = {
        shift.id: _build_timeline(
            shift,
            now=now,
            trips=trips_by_equipment[shift.equipment_id],
            downtimes=downtimes_by_equipment[shift.equipment_id],
            assignments=assignments_by_equipment[shift.equipment_id],
            corrections=corrections_by_shift[shift.id],
            extra_quality_flags=(
                set(overlap_flags.get(shift.id, ()))
                | set(extra_quality_flags_by_shift.get(shift.id, ()))
            ),
        )
        for shift in valid_shifts
    }
    return tuple(
        (
            _invalid_shift_timeline(
                shift,
                extra_quality_flags=extra_quality_flags_by_shift.get(
                    shift.id,
                    (),
                ),
            )
            if shift.id in invalid_shift_ids
            else timelines_by_shift_id[shift.id]
        )
        for shift in shifts
    )


def build_driver_shift_timeline(shift, *, as_of=None):
    return build_driver_shift_timelines((shift,), as_of=as_of)[0]
