import hashlib
from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, Q
from django.utils import timezone

from core.production_time import production_day_bounds, production_work_date
from shifts.models import EmployeeShift, ShiftType

from .driver_shift_timeline import (
    DOWNTIME_CATEGORIES,
    TimelineCategory,
    build_driver_shift_timelines,
    cycle_aggregation_inputs_from_samples,
    cycle_statistics_from_samples,
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
    'trip_assignment_mismatch_seconds',
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


PASSPORT_DECIMAL_METRICS = (
    'volume_m3',
    'tonnage_t',
    'm3_km',
    't_km',
)

PASSPORT_TIME_SUM_KEYS = (
    'actual_duration_seconds',
    'available_seconds',
    'loaded_to_unloaded_seconds',
    'assignment_covered_seconds',
    'downtime_external_seconds',
    'downtime_technical_seconds',
    'downtime_regulated_seconds',
    'downtime_review_seconds',
    'no_assignment_seconds',
    'unexplained_seconds',
    'conflict_seconds',
)


def _aggregate_decimal_metric(passports, key):
    metrics = [
        passport['production'][key]
        for passport in passports
    ]
    known_value = sum(
        (metric['known_value'] for metric in metrics),
        Decimal('0'),
    )
    missing_trip_count = sum(
        metric['missing_trip_count']
        for metric in metrics
    )
    complete_trip_count = sum(
        metric['complete_trip_count']
        for metric in metrics
    )
    return {
        'value': known_value if not missing_trip_count else None,
        'known_value': known_value,
        'complete_trip_count': complete_trip_count,
        'missing_trip_count': missing_trip_count,
        'is_complete': missing_trip_count == 0,
    }


def _aggregate_rate(metric, available_seconds, *, formula_ready):
    if available_seconds <= 0:
        known_value = None
        strict_value = None
    else:
        known_value = (
            Decimal(metric['known_value'])
            * Decimal('3600')
            / Decimal(available_seconds)
        ).quantize(Decimal('0.0001'))
        strict_value = (
            (
                Decimal(metric['value'])
                * Decimal('3600')
                / Decimal(available_seconds)
            ).quantize(Decimal('0.0001'))
            if metric['value'] is not None
            else None
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


def _merge_cycle_samples(sample_groups):
    merged = defaultdict(list)
    for samples in sample_groups:
        for context, values in samples.items():
            merged[context].extend(values)
    return {
        context: tuple(values)
        for context, values in merged.items()
    }


def _aggregate_shift_passports(passports, cycle_sample_groups):
    passports = tuple(passports)
    decimal_metrics = {
        key: _aggregate_decimal_metric(passports, key)
        for key in PASSPORT_DECIMAL_METRICS
    }
    completed_trip_count = sum(
        passport['production']['completed_trip_count']
        for passport in passports
    )
    completed_trip_metric = {
        'value': Decimal(completed_trip_count),
        'known_value': Decimal(completed_trip_count),
        'complete_trip_count': completed_trip_count,
        'missing_trip_count': 0,
        'is_complete': True,
    }
    time = {
        key: sum(passport['time'][key] for passport in passports)
        for key in PASSPORT_TIME_SUM_KEYS
    }
    formula_ready = bool(passports) and all(
        passport['rates_per_available_hour']['is_formula_ready']
        for passport in passports
    )
    available_seconds = time['available_seconds']
    output_attribution = {
        key: sum(
            passport['production']['output_attribution'][key]
            for passport in passports
        )
        for key in (
            'unloading_shift_trip_count',
            'legacy_driver_trip_count',
            'ambiguous_trip_count',
        )
    }
    production_completeness = {
        key: sum(
            passport['production']['completeness'][key]
            for passport in passports
        )
        for key in (
            'credited_trip_count',
            'volume_missing_trip_count',
            'tonnage_missing_trip_count',
            'distance_missing_trip_count',
            'cycle_calibration_excluded_carryover_trip_count',
            'comparison_context_missing_trip_count',
        )
    }
    routing = {
        key: sum(passport['routing'][key] for passport in passports)
        for key in (
            'credited_trip_count',
            'explicit_assigned_and_actual_count',
            'match_count',
            'mismatch_count',
            'missing_assigned_count',
            'missing_actual_count',
        )
    }
    trip_states = {
        key: sum(passport['trip_states'][key] for passport in passports)
        for key in (
            'loaded_during_shift_count',
            'open_at_close_count',
            'cancelled_count',
            'carryover_in_count',
            'carryover_out_count',
        )
    }
    corrections_by_metric = {}
    for passport in passports:
        for metric, values in passport['handover'][
            'corrections_by_metric'
        ].items():
            target = corrections_by_metric.setdefault(
                metric,
                {
                    'count': 0,
                    'total_absolute_difference': Decimal('0'),
                },
            )
            target['count'] += values['count']
            target['total_absolute_difference'] += (
                values['total_absolute_difference']
            )
    quality_flags = sorted({
        flag
        for passport in passports
        for flag in passport['quality']['flags']
    })
    passport_quality_metrics = {
        key: sum(
            passport['quality']['quality_metrics'].get(key, 0)
            for passport in passports
        )
        for key in (
            'trip_without_assignment_seconds',
            'trip_assignment_mismatch_seconds',
        )
    }
    source_fingerprints = sorted(
        passport['source_fingerprint']
        for passport in passports
    )
    aggregate_fingerprint = hashlib.sha256(
        '|'.join(source_fingerprints).encode('ascii')
    ).hexdigest()
    merged_cycle_samples = _merge_cycle_samples(cycle_sample_groups)
    return {
        'passport_schema_version': 1,
        'source_fingerprint': aggregate_fingerprint,
        'scope': 'employee_period',
        'shift_count': len(passports),
        'production': {
            'completed_trip_count': completed_trip_count,
            'output_attribution': output_attribution,
            **decimal_metrics,
            'completeness': production_completeness,
        },
        'rates_per_available_hour': {
            'trip_count': _aggregate_rate(
                completed_trip_metric,
                available_seconds,
                formula_ready=formula_ready,
            ),
            **{
                key: _aggregate_rate(
                    metric,
                    available_seconds,
                    formula_ready=formula_ready,
                )
                for key, metric in decimal_metrics.items()
            },
            'is_formula_ready': formula_ready,
        },
        'expected': {
            'actual_to_expected_ratio': None,
            'status': 'expected_model_unavailable',
        },
        'time': {
            'actual_start_at': None,
            'actual_end_at': None,
            **time,
            'scheduled_start_at': None,
            'scheduled_end_at': None,
            'scheduled_window_status': 'schedule_snapshot_unavailable',
            'start_deviation_seconds': None,
            'end_deviation_seconds': None,
            'confirmed_extra_productive_seconds': None,
            'unjustified_short_shift_seconds': None,
            'short_shift_status': 'policy_unavailable',
        },
        'cycles': cycle_statistics_from_samples(merged_cycle_samples),
        'aggregation_inputs': {
            'cycle_samples': cycle_aggregation_inputs_from_samples(
                merged_cycle_samples
            ),
        },
        'routing': routing,
        'trip_states': trip_states,
        'open_close': {
            'shift_count': len(passports),
            'opened_by_employee_shift_count': sum(
                passport['open_close']['opened_by_employee']
                for passport in passports
            ),
            'closed_by_employee_shift_count': sum(
                passport['open_close']['closed_by_employee']
                for passport in passports
            ),
            'service_closed_shift_count': sum(
                passport['open_close']['service_closed']
                for passport in passports
            ),
            'valid_window_shift_count': sum(
                passport['open_close']['window_valid']
                for passport in passports
            ),
            'complete_start_readings_shift_count': sum(
                passport['open_close']['start_readings_complete']
                for passport in passports
            ),
            'complete_end_readings_shift_count': sum(
                passport['open_close']['end_readings_complete']
                for passport in passports
            ),
        },
        'handover': {
            'reading_correction_count': sum(
                passport['handover']['reading_correction_count']
                for passport in passports
            ),
            'corrections_by_metric': corrections_by_metric,
            'fuel_metric_status': 'net_change_not_consumption',
            'shift_detail_status': 'available_in_shift_passports',
        },
        'quality': {
            'flags': quality_flags,
            'quality_metrics': passport_quality_metrics,
            'data_usable_for_formula_review': formula_ready,
            'official_rating_eligible': False,
        },
    }


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
        shift_passports = []
        cycle_sample_groups = []

        for shift in employee_shifts:
            timeline = timelines_by_shift[shift.id]
            shift_passports.append(timeline.passport)
            cycle_sample_groups.append(timeline.cycle_samples)
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
            'passport': _aggregate_shift_passports(
                shift_passports,
                cycle_sample_groups,
            ),
            'shift_passports': shift_passports,
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


def build_driver_watch_observation(
    watch_period,
    *,
    shift_type=None,
    as_of=None,
    employee_ids=None,
):
    if shift_type not in {None, ShiftType.DAY, ShiftType.NIGHT}:
        raise ValueError('Допустимые значения смены: day или night.')

    now = as_of or timezone.now()
    shifts = _driver_closed_shift_queryset().filter(
        watch_period=watch_period,
        opened_at__lt=now,
    )
    if employee_ids is not None:
        shifts = shifts.filter(employee_id__in=employee_ids)
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
    employee_ids=None,
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
    if employee_ids is not None:
        shifts = shifts.filter(employee_id__in=employee_ids)
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


def build_driver_watch_linkage_audit(
    watch_period,
    *,
    shift_type=None,
    employee_ids=None,
):
    if shift_type not in {None, ShiftType.DAY, ShiftType.NIGHT}:
        raise ValueError('Допустимые значения смены: day или night.')

    period_start = production_day_bounds(watch_period.starts_on)[0]
    period_end = production_day_bounds(watch_period.ends_on)[1]
    base_shifts = _driver_closed_shift_queryset()
    if employee_ids is not None:
        base_shifts = base_shifts.filter(employee_id__in=employee_ids)
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
