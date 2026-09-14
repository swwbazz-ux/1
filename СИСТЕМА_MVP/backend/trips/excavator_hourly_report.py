from collections import defaultdict
from datetime import timedelta

from django.utils import formats, timezone

from shifts.equipment_plan_groups import equipment_is_belaz_truck, equipment_is_nhl_truck

from .models import Trip, TripStatus


def _period_label(start, end):
    if start.date() == end.date():
        return f'{start:%H:%M}–{end:%H:%M}'
    return f'{start:%d.%m %H:%M}–{end:%d.%m %H:%M}'


def _fleet_code(trip):
    belongs_to_belaz = equipment_is_belaz_truck(trip.truck)
    belongs_to_nhl = equipment_is_nhl_truck(trip.truck)
    if belongs_to_belaz == belongs_to_nhl:
        return 'unknown'
    return 'belaz' if belongs_to_belaz else 'nhl'


def build_excavator_hourly_report(excavator, *, captured_at=None):
    captured_at = captured_at or timezone.now()
    local_now = timezone.localtime(captured_at)
    current_start = local_now.replace(minute=0, second=0, microsecond=0)
    previous_start = current_start - timedelta(hours=1)

    trips = (
        Trip.objects
        .filter(
            excavator=excavator,
            loaded_at__gte=previous_start,
            loaded_at__lt=captured_at,
        )
        .exclude(status=TripStatus.CANCELLED)
        .select_related('assigned_dump_point', 'truck__equipment_type', 'truck__model')
        .order_by('loaded_at', 'id')
    )

    counts = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    point_names = {}
    point_ids = {}
    source_counts = [0, 0]
    unclassified_counts = [0, 0]
    unknown_point_counts = [0, 0]

    for trip in trips:
        bucket = 0 if trip.loaded_at < current_start else 1
        source_counts[bucket] += 1

        # assigned_dump_point is the immutable destination captured when the
        # Excavator operator sends the truck. The mutable dump_point and the
        # driver's actual_dump_point are never used as a silent replacement.
        point = trip.assigned_dump_point
        point_key = str(point.pk) if point else 'unknown'
        point_names[point_key] = point.name if point else 'Точка не определена'
        point_ids[point_key] = point.pk if point else None
        if point is None:
            unknown_point_counts[bucket] += 1

        fleet_code = _fleet_code(trip)
        if fleet_code == 'unknown':
            unclassified_counts[bucket] += 1
            continue
        counts[point_key][fleet_code][bucket] += 1

    ordered_point_keys = sorted(
        point_names,
        key=lambda key: (key == 'unknown', point_names[key].casefold(), key),
    )
    period_specs = (
        ('current', 'Текущий час', current_start, local_now, 1),
        ('previous', 'Предыдущий час', previous_start, current_start, 0),
    )
    hours = []
    for code, title, start, end, bucket in period_specs:
        rows = []
        belaz_total = 0
        nhl_total = 0
        for point_key in ordered_point_keys:
            belaz_count = counts[point_key]['belaz'][bucket]
            nhl_count = counts[point_key]['nhl'][bucket]
            if not (belaz_count or nhl_count):
                continue
            rows.append({
                'dump_point_id': point_ids[point_key],
                'dump_point': point_names[point_key],
                'belaz': belaz_count,
                'nhl': nhl_count,
            })
            belaz_total += belaz_count
            nhl_total += nhl_count

        hours.append({
            'code': code,
            'title': title,
            'period': {
                'start': start.isoformat(),
                'end': end.isoformat(),
                'label': _period_label(start, end),
            },
            'rows': rows,
            'totals': {
                'belaz': belaz_total,
                'nhl': nhl_total,
                'trip_count': belaz_total + nhl_total,
            },
            'source_trip_count': source_counts[bucket],
            'unclassified_trip_count': unclassified_counts[bucket],
            'is_empty': source_counts[bucket] == 0,
        })

    return {
        'schema_version': 2,
        'generated_at': captured_at.isoformat(),
        'freshness_label': f'Данные на {local_now:%H:%M}',
        'excavator': {
            'id': excavator.pk,
            'name': excavator.garage_number,
        },
        'work_date': formats.date_format(local_now.date(), 'j E'),
        'hours': hours,
        'data_quality': {
            'unclassified_trip_count': sum(unclassified_counts),
            'unknown_dump_point_trip_count': sum(unknown_point_counts),
            'complete': not any(unclassified_counts) and not any(unknown_point_counts),
        },
    }
