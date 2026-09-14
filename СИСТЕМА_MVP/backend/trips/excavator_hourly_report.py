from collections import defaultdict
from datetime import timedelta

from django.utils import formats, timezone

from shifts.equipment_plan_groups import (
    equipment_is_belaz_truck,
    equipment_is_nhl_truck,
)

from .models import Trip, TripStatus


FLEET_GROUPS = (
    ('belaz', 'БелАЗ'),
    ('nhl', 'NHL'),
    ('unknown', 'Тип не определён'),
)


def _fleet_code(truck):
    if equipment_is_belaz_truck(truck):
        return 'belaz'
    if equipment_is_nhl_truck(truck):
        return 'nhl'
    return 'unknown'


def _period_label(start, end):
    if start.date() == end.date():
        return f'{start:%H:%M}–{end:%H:%M}'
    return f'{start:%d.%m %H:%M}–{end:%d.%m %H:%M}'


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
            loaded_at__lte=captured_at,
        )
        .exclude(status=TripStatus.CANCELLED)
        .select_related(
            'truck__equipment_type',
            'truck__model',
            'assigned_dump_point',
        )
        .order_by('loaded_at', 'id')
    )

    counts = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    point_names = {}
    point_ids = {}
    totals = defaultdict(lambda: [0, 0])
    grand = [0, 0]

    for trip in trips:
        # assigned_dump_point is the immutable dispatch destination captured
        # when the Excavator operator sends the truck. dump_point can later be
        # overwritten with the driver's actual destination, so it is not a
        # safe source for legacy rows where the assignment snapshot is absent.
        point = trip.assigned_dump_point
        point_key = str(point.pk) if point else 'unknown'
        point_names[point_key] = point.name if point else 'Точка не определена'
        point_ids[point_key] = point.pk if point else None
        fleet_code = _fleet_code(trip.truck)
        bucket = 0 if trip.loaded_at < current_start else 1
        counts[point_key][fleet_code][bucket] += 1
        totals[fleet_code][bucket] += 1
        grand[bucket] += 1

    groups = []
    for point_key in sorted(point_names, key=lambda key: (point_names[key].casefold(), key)):
        rows = []
        for fleet_code, fleet_label in FLEET_GROUPS:
            previous, current = counts[point_key][fleet_code]
            if previous or current:
                rows.append({
                    'code': fleet_code,
                    'label': fleet_label,
                    'previous': previous,
                    'current': current,
                })
        if rows:
            groups.append({
                'dump_point_id': point_ids[point_key],
                'dump_point': point_names[point_key],
                'rows': rows,
            })

    total_rows = []
    for fleet_code, fleet_label in FLEET_GROUPS:
        previous, current = totals[fleet_code]
        if previous or current:
            total_rows.append({
                'code': fleet_code,
                'label': f'Итого {fleet_label}',
                'previous': previous,
                'current': current,
            })

    return {
        'schema_version': 1,
        'generated_at': captured_at.isoformat(),
        'freshness_label': f'На {local_now:%H:%M}',
        'excavator': {
            'id': excavator.pk,
            'name': excavator.garage_number,
        },
        'work_date': formats.date_format(local_now.date(), 'j E'),
        'periods': {
            'previous': {
                'start': previous_start.isoformat(),
                'end': current_start.isoformat(),
                'label': _period_label(previous_start, current_start),
            },
            'current': {
                'start': current_start.isoformat(),
                'end': captured_at.isoformat(),
                'label': _period_label(current_start, local_now),
            },
        },
        'groups': groups,
        'totals': {
            'rows': total_rows,
            'grand': {'previous': grand[0], 'current': grand[1]},
        },
        'is_empty': not any(grand),
    }
