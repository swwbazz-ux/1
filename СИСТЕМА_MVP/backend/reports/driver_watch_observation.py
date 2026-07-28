from collections import defaultdict

from django.db.models import Count, Q
from django.utils import timezone

from core.production_time import production_day_bounds, production_work_date
from shifts.models import EmployeeShift, ShiftType

from .driver_shift_timeline import (
    DOWNTIME_CATEGORIES,
    TimelineCategory,
    build_driver_shift_timelines,
)


OBSERVATION_CATEGORIES = (
    TimelineCategory.TRIP,
    TimelineCategory.DOWNTIME_EXTERNAL,
    TimelineCategory.DOWNTIME_TECHNICAL,
    TimelineCategory.DOWNTIME_REGULATED,
    TimelineCategory.DOWNTIME_REVIEW,
    TimelineCategory.NO_ASSIGNMENT,
    TimelineCategory.UNEXPLAINED,
    TimelineCategory.DATA_CONFLICT,
)

SOURCE_COUNT_KEYS = (
    'trip_count',
    'carryover_trip_count',
    'downtime_event_count',
    'downtime_reported_by_employee_count',
    'downtime_reported_by_other_count',
    'downtime_without_employee_count',
    'assignment_count',
)

QUALITY_METRIC_KEYS = (
    'trip_without_assignment_seconds',
)


def _observation_status(seconds_by_category, quality_flags):
    if (
        seconds_by_category[TimelineCategory.DATA_CONFLICT]
        or {
            'data_conflict',
            'equipment_shift_overlap',
            'employee_shift_overlap',
        } & set(quality_flags)
    ):
        return 'data_conflict'
    if (
        seconds_by_category[TimelineCategory.DOWNTIME_REVIEW]
        or seconds_by_category[TimelineCategory.UNEXPLAINED]
        or quality_flags
    ):
        return 'needs_review'
    return 'observed'


def _driver_closed_shift_queryset():
    return (
        EmployeeShift.objects
        .filter(equipment__isnull=False, closed_at__isnull=False)
        .filter(
            Q(workplace_code='driver')
            | Q(
                workplace_code='',
                equipment__equipment_type__name__contains='Самосвал',
            )
        )
        .select_related(
            'employee',
            'equipment',
            'equipment__equipment_type',
            'equipment__model',
        )
        .order_by('employee__full_name', 'shift_type', 'opened_at', 'id')
    )


def _build_driver_closed_shift_observation(
    shifts,
    *,
    now,
    extra_quality_flags_by_shift=None,
):
    shifts = tuple(shifts)
    if any(not shift.closed_at for shift in shifts):
        raise ValueError(
            'Теневая официальная сводка принимает только закрытые смены.'
        )
    timelines_by_shift = {
        timeline.shift_id: timeline
        for timeline in build_driver_shift_timelines(
            shifts,
            as_of=now,
            extra_quality_flags_by_shift=extra_quality_flags_by_shift,
        )
    }

    groups = defaultdict(list)
    for shift in shifts:
        groups[(shift.employee_id, shift.shift_type)].append(shift)

    rows = []
    summary_source_ids = {
        key: set()
        for key in SOURCE_COUNT_KEYS
    }
    for (_, row_shift_type), employee_shifts in groups.items():
        employee = employee_shifts[0].employee
        seconds_by_category = {category: 0 for category in OBSERVATION_CATEGORIES}
        total_seconds = 0
        explained_seconds = 0
        equipment = {}
        closed_shift_count = 0
        usable_shift_count = 0
        quality_flags = set()
        source_ids = {
            key: set()
            for key in SOURCE_COUNT_KEYS
        }
        quality_metrics = {key: 0 for key in QUALITY_METRIC_KEYS}

        for shift in employee_shifts:
            timeline = timelines_by_shift[shift.id]
            total_seconds += timeline.total_seconds
            explained_seconds += timeline.explained_seconds
            closed_shift_count += int(bool(shift.closed_at))
            usable_shift_count += int(
                timeline.usable_for_formula_review
            )
            quality_flags.update(timeline.quality_flags)
            for key in SOURCE_COUNT_KEYS:
                source_ids[key].update(
                    timeline.source_ids.get(key, ())
                )
            for key in QUALITY_METRIC_KEYS:
                quality_metrics[key] += timeline.quality_metrics.get(key, 0)
            equipment[shift.equipment_id] = str(shift.equipment)
            for category in OBSERVATION_CATEGORIES:
                seconds_by_category[category] += timeline.seconds_by_category.get(
                    category,
                    0,
                )

        shift_count = len(employee_shifts)
        downtime_seconds = sum(
            seconds_by_category[category]
            for category in DOWNTIME_CATEGORIES
        )
        source_counts = {
            key: len(ids)
            for key, ids in source_ids.items()
        }
        for key, ids in source_ids.items():
            summary_source_ids[key].update(ids)
        rows.append({
            'employee_id': employee.id,
            'full_name': employee.full_name,
            'shift_type': row_shift_type,
            'shift_type_label': dict(ShiftType.choices)[row_shift_type],
            'equipment': [
                {'id': equipment_id, 'name': equipment_name}
                for equipment_id, equipment_name in sorted(
                    equipment.items(),
                    key=lambda item: item[1],
                )
            ],
            'shift_count': shift_count,
            'closed_shift_count': closed_shift_count,
            'open_shift_count': shift_count - closed_shift_count,
            'usable_shift_count': usable_shift_count,
            'withheld_shift_count': shift_count - usable_shift_count,
            'quality_flags': sorted(quality_flags),
            'source_counts': source_counts,
            'quality_metrics': quality_metrics,
            'data_usable_for_formula_review': (
                usable_shift_count == shift_count
                and closed_shift_count == shift_count
            ),
            'seconds_by_category': seconds_by_category,
            'total_seconds': total_seconds,
            'explained_seconds': explained_seconds,
            'downtime_seconds': downtime_seconds,
            'coverage_percent': (
                round(explained_seconds * 100 / total_seconds, 2)
                if total_seconds
                else 0.0
            ),
            'observation_status': _observation_status(
                seconds_by_category,
                quality_flags,
            ),
        })

    total_seconds = sum(row['total_seconds'] for row in rows)
    explained_seconds = sum(row['explained_seconds'] for row in rows)
    usable_shift_count = sum(
        row['usable_shift_count']
        for row in rows
    )
    withheld_shift_count = sum(row['withheld_shift_count'] for row in rows)
    source_counts = {
        key: len(ids)
        for key, ids in summary_source_ids.items()
    }
    quality_metrics = {
        key: sum(row['quality_metrics'][key] for row in rows)
        for key in QUALITY_METRIC_KEYS
    }
    return {
        'generated_at': now.isoformat(),
        'row_count': len(rows),
        'summary': {
            'closed_shift_count': sum(row['shift_count'] for row in rows),
            'usable_shift_count': usable_shift_count,
            'withheld_shift_count': withheld_shift_count,
            'total_seconds': total_seconds,
            'explained_seconds': explained_seconds,
            'coverage_percent': (
                round(explained_seconds * 100 / total_seconds, 2)
                if total_seconds
                else 0.0
            ),
            'data_ready_for_formula_review': (
                bool(rows)
                and withheld_shift_count == 0
            ),
            'source_counts': source_counts,
            'quality_metrics': quality_metrics,
        },
        'rows': rows,
    }


def build_driver_watch_observation(watch_period, *, shift_type=None, as_of=None):
    if shift_type not in {None, ShiftType.DAY, ShiftType.NIGHT}:
        raise ValueError('Допустимые значения смены: day или night.')

    now = as_of or timezone.now()
    shifts = _driver_closed_shift_queryset().filter(
        watch_period=watch_period,
        opened_at__lt=now,
    )
    if shift_type:
        shifts = shifts.filter(shift_type=shift_type)
    shifts = tuple(shifts)
    date_mismatch_ids = {
        shift.id
        for shift in shifts
        if not (
            watch_period.starts_on
            <= production_work_date(shift.opened_at)
            <= watch_period.ends_on
        )
    }
    observation = _build_driver_closed_shift_observation(
        shifts,
        now=now,
        extra_quality_flags_by_shift={
            shift_id: {'watch_period_date_mismatch'}
            for shift_id in date_mismatch_ids
        },
    )
    observation.update({
        'scope_type': 'watch_period',
        'official_rating_eligible': False,
        'watch_period': {
            'id': watch_period.id,
            'name': watch_period.name,
            'starts_on': watch_period.starts_on.isoformat(),
            'ends_on': watch_period.ends_on.isoformat(),
        },
        'shift_type': shift_type,
    })
    return observation


def build_driver_period_shadow_observation(
    starts_on,
    ends_on,
    *,
    shift_type=None,
    as_of=None,
):
    if ends_on < starts_on:
        raise ValueError('Дата окончания не может быть раньше даты начала.')
    if (ends_on - starts_on).days > 30:
        raise ValueError('Теневой период не может превышать 31 день.')
    if shift_type not in {None, ShiftType.DAY, ShiftType.NIGHT}:
        raise ValueError('Допустимые значения смены: day или night.')

    now = as_of or timezone.now()
    period_start = production_day_bounds(starts_on)[0]
    period_end = production_day_bounds(ends_on)[1]
    shifts = _driver_closed_shift_queryset().filter(
        opened_at__gte=period_start,
        opened_at__lt=min(period_end, now),
    )
    if shift_type:
        shifts = shifts.filter(shift_type=shift_type)
    shifts = tuple(shifts)
    observation = _build_driver_closed_shift_observation(shifts, now=now)
    observation.update({
        'scope_type': 'date_range_shadow',
        'official_rating_eligible': False,
        'period': {
            'starts_on': starts_on.isoformat(),
            'ends_on': ends_on.isoformat(),
        },
        'shift_type': shift_type,
        'linkage_audit': {
            'candidate_closed_shift_count': len(shifts),
            'linked_shift_count': sum(
                int(shift.watch_period_id is not None)
                for shift in shifts
            ),
            'unlinked_shift_count': sum(
                int(shift.watch_period_id is None)
                for shift in shifts
            ),
        },
    })
    observation['summary']['data_ready_for_formula_review'] = False
    return observation


def build_driver_watch_linkage_audit(watch_period, *, shift_type=None):
    if shift_type not in {None, ShiftType.DAY, ShiftType.NIGHT}:
        raise ValueError('Допустимые значения смены: day или night.')

    period_start = production_day_bounds(watch_period.starts_on)[0]
    period_end = production_day_bounds(watch_period.ends_on)[1]
    base_shifts = _driver_closed_shift_queryset()
    if shift_type:
        base_shifts = base_shifts.filter(shift_type=shift_type)
    period_shifts = base_shifts.filter(
        opened_at__gte=period_start,
        opened_at__lt=period_end,
    )
    counts = period_shifts.aggregate(
        candidate_closed_shift_count=Count('id'),
        linked_to_selected_watch_count=Count(
            'id',
            filter=Q(watch_period=watch_period),
        ),
        unlinked_shift_count=Count(
            'id',
            filter=Q(watch_period__isnull=True),
        ),
        linked_to_other_watch_count=Count(
            'id',
            filter=Q(watch_period__isnull=False)
            & ~Q(watch_period=watch_period),
        ),
    )
    counts['selected_watch_outside_period_count'] = (
        base_shifts
        .filter(watch_period=watch_period)
        .filter(
            Q(opened_at__lt=period_start)
            | Q(opened_at__gte=period_end)
        )
        .count()
    )
    counts['linkage_ready'] = (
        counts['candidate_closed_shift_count'] > 0
        and counts['unlinked_shift_count'] == 0
        and counts['linked_to_other_watch_count'] == 0
        and counts['selected_watch_outside_period_count'] == 0
    )
    return counts
