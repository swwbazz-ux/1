from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import F, Q
from django.utils import timezone

from assignments.models import (
    HaulAssignment,
    HaulAssignmentAction,
)
from downtimes.models import DowntimeEvent
from shifts.models import EmployeeShift
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

    @property
    def usable_for_formula_review(self):
        return not self.quality_flags

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
        clipped = _clip_span(
            trip.created_at,
            trip.completed_at or window_end,
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
        loaded_during_shift = window_start <= trip.created_at < window_end
        unloaded_during_shift = trip.unloading_shift_id == shift.id
        completed_during_legacy_shift = bool(
            trip.unloading_shift_id is None
            and trip.completed_at is not None
            and window_start <= trip.completed_at <= window_end
        )
        temporal_overlap = (
            trip.created_at < window_end
            and (
                trip.completed_at is None
                or trip.completed_at > window_start
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


def _trip_quality_flags(trip):
    flags = set()
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
        trip.completed_at is None
        for trip in trip_records
    ):
        quality_flags.append('open_trip_on_closed_shift')
    if any(
        trip.completed_at
        and trip.completed_at - trip.created_at > MAX_PLAUSIBLE_SHIFT_DURATION
        for trip in trip_records
    ):
        quality_flags.append('trip_duration_over_16h')

    source_ids = {
        'trip_count': tuple(sorted({
            trip.id
            for trip in trip_records
        })),
        'carryover_trip_count': tuple(sorted({
            trip.id
            for trip in trip_records
            if trip.is_carryover
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
        quality_flags=tuple(sorted(set(quality_flags))),
        source_counts={
            key: len(ids)
            for key, ids in source_ids.items()
        },
        source_ids=source_ids,
        quality_metrics={
            'trip_without_assignment_seconds': (
                trip_without_assignment_seconds
            ),
        },
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
        quality_flags=tuple(sorted({
            'invalid_shift_window',
            *extra_quality_flags,
        })),
        source_counts={
            key: 0
            for key in source_ids
        },
        source_ids=source_ids,
        quality_metrics={
            'trip_without_assignment_seconds': 0,
        },
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

    trips_by_equipment = {equipment_id: [] for equipment_id in equipment_ids}
    trips = (
        Trip.objects
        .filter(truck_id__in=equipment_ids)
        .exclude(status=TripStatus.CANCELLED)
        .filter(
            Q(
                created_at__gte=window_start,
                created_at__lt=window_end,
            )
            | Q(
                completed_at__gt=window_start,
                completed_at__lte=window_end,
            )
            | Q(unloading_shift_id__in=valid_shift_ids)
            | Q(
                created_at__lt=window_end,
                completed_at__isnull=True,
            )
            | Q(
                created_at__lt=window_end,
                completed_at__gt=window_start,
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
            'dump_point',
            'rock_type',
            'unloading_shift',
        )
        .order_by('created_at', 'id')
    )
    for trip in trips:
        trips_by_equipment[trip.truck_id].append(trip)
    assignment_trip_load_times = [
        trip.created_at
        for trip in trips
        if not (_trip_quality_flags(trip) & TRIP_SPAN_BLOCKING_FLAGS)
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

    timelines_by_shift_id = {
        shift.id: _build_timeline(
            shift,
            now=now,
            trips=trips_by_equipment[shift.equipment_id],
            downtimes=downtimes_by_equipment[shift.equipment_id],
            assignments=assignments_by_equipment[shift.equipment_id],
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
