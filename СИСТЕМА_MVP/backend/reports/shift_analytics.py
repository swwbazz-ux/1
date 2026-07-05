from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from downtimes.models import DowntimeEvent
from references.models import TruckCapacityRule
from trips.models import OPEN_TRIP_STATUSES, Trip, TripStatus


SHIFT_LABELS = {
    '': 'Все смены',
    'day': 'Дневная',
    'night': 'Ночная',
}

DYNAMICS_GRANULARITY_LABELS = {
    'hour': 'по часам',
    'shift': 'по сменам',
    'day': 'по дням',
    'month': 'по месяцам',
}


def zero_decimal():
    return Decimal('0')


def as_decimal(value):
    if value is None:
        return Decimal('0')
    return Decimal(value)


def format_decimal(value, places=2):
    value = as_decimal(value).quantize(Decimal('1') if places == 0 else Decimal('0.' + '0' * (places - 1) + '1'))
    if places == 0:
        return f'{int(value):,}'.replace(',', ' ')
    return f'{value:,.{places}f}'.replace(',', ' ').replace('.', ',')


def format_volume(value):
    return format_decimal(value, places=0)


def percent(part, total):
    total = as_decimal(total)
    if not total:
        return Decimal('0')
    return ((as_decimal(part) / total) * Decimal('100')).quantize(Decimal('0.1'))


def trip_loading_date(trip):
    if trip.loading_shift_id and trip.loading_shift and trip.loading_shift.opened_at:
        return timezone.localtime(trip.loading_shift.opened_at).date()
    return timezone.localtime(trip.created_at).date()


def trip_unloading_date(trip):
    if trip.unloading_shift_id and trip.unloading_shift and trip.unloading_shift.opened_at:
        return timezone.localtime(trip.unloading_shift.opened_at).date()
    if trip.completed_at:
        return timezone.localtime(trip.completed_at).date()
    return timezone.localtime(trip.created_at).date()


def trip_loading_shift_type(trip):
    if trip.loading_shift_id and trip.loading_shift:
        return trip.loading_shift.shift_type
    return ''


def trip_unloading_shift_type(trip):
    if trip.unloading_shift_id and trip.unloading_shift:
        return trip.unloading_shift.shift_type
    return ''


def shift_matches(value, selected_shift_type):
    return not selected_shift_type or value == selected_shift_type


def calculated_trip_volume_and_tonnage(trip):
    volume = trip.volume_m3
    if volume is None and trip.truck_id and trip.truck and trip.truck.model_id:
        rule = TruckCapacityRule.objects.filter(equipment_model=trip.truck.model, rock_type=trip.rock_type).first()
        if rule:
            volume = rule.volume_m3
        elif trip.truck.model and trip.truck.model.body_volume_m3:
            volume = trip.truck.model.body_volume_m3

    tonnage = trip.tonnage
    if tonnage is None and volume is not None and trip.rock_type_id and trip.rock_type and trip.rock_type.density:
        tonnage = (Decimal(volume) * Decimal(trip.rock_type.density)).quantize(Decimal('0.01'))
    return as_decimal(volume), as_decimal(tonnage)


def trip_queryset_for_loading(selected_date):
    return (
        Trip.objects
        .filter(status__in=(TripStatus.LOADED_WAITING_UNLOAD, TripStatus.COMPLETED))
        .filter(created_at__date=selected_date)
        .select_related(
            'truck',
            'truck__model',
            'excavator',
            'excavator_operator',
            'rock_type',
            'dump_point',
            'assigned_dump_point',
            'actual_dump_point',
            'loading_shift',
            'unloading_shift',
        )
        .order_by('-created_at')
    )


def trip_queryset_for_loading_range(date_from, date_to):
    return (
        Trip.objects
        .filter(status__in=(TripStatus.LOADED_WAITING_UNLOAD, TripStatus.COMPLETED))
        .filter(created_at__date__gte=date_from, created_at__date__lte=date_to)
        .select_related(
            'truck',
            'truck__model',
            'excavator',
            'excavator__equipment_type',
            'excavator_operator',
            'rock_type',
            'loading_shift',
        )
        .order_by('created_at')
    )


def excavator_label(excavator):
    if not excavator:
        return 'Экскаватор не указан'
    if excavator.garage_number:
        return f'Экскаватор {excavator.garage_number}'
    return str(excavator)


def dynamics_bucket(created_at, granularity):
    local_dt = timezone.localtime(created_at)
    if granularity == 'hour':
        bucket_start = local_dt.replace(minute=0, second=0, microsecond=0)
        return bucket_start.strftime('%Y-%m-%d-%H'), bucket_start.strftime('%d.%m %H:00')
    if granularity == 'month':
        return local_dt.strftime('%Y-%m'), local_dt.strftime('%m.%Y')
    if granularity == 'shift':
        shift_type = 'day' if 8 <= local_dt.hour < 20 else 'night'
        shift_label = 'день' if shift_type == 'day' else 'ночь'
        return f'{local_dt:%Y-%m-%d}-{shift_type}', f'{local_dt:%d.%m} {shift_label}'
    return local_dt.strftime('%Y-%m-%d'), local_dt.strftime('%d.%m')


def build_chart_points(rows, max_volume):
    if not rows or not max_volume:
        return ''
    if len(rows) == 1:
        y = Decimal('220') - (rows[0]['volume_m3'] / max_volume * Decimal('180'))
        return f'500,{int(y)}'
    points = []
    last_index = len(rows) - 1
    for index, row in enumerate(rows):
        x = Decimal('32') + (Decimal(index) / Decimal(last_index) * Decimal('936'))
        y = Decimal('220') - (row['volume_m3'] / max_volume * Decimal('180'))
        points.append(f'{int(x)},{int(y)}')
    return ' '.join(points)


def build_excavator_dynamics(date_from, date_to, granularity='day', excavator_ids=None):
    granularity = granularity if granularity in DYNAMICS_GRANULARITY_LABELS else 'day'
    excavator_ids = [int(item) for item in (excavator_ids or []) if str(item).isdigit()]
    trips = trip_queryset_for_loading_range(date_from, date_to)
    if excavator_ids:
        trips = trips.filter(excavator_id__in=excavator_ids)

    buckets = {}
    excavators = {}
    total_volume = Decimal('0')
    total_trips = 0

    for trip in trips:
        bucket_key, bucket_label = dynamics_bucket(trip.created_at, granularity)
        bucket = buckets.setdefault(bucket_key, {
            'key': bucket_key,
            'label': bucket_label,
            'volume_m3': Decimal('0'),
            'trip_count': 0,
        })
        excavator_key = trip.excavator_id or 0
        excavator = excavators.setdefault(excavator_key, {
            'id': trip.excavator_id,
            'label': excavator_label(trip.excavator),
            'volume_m3': Decimal('0'),
            'trip_count': 0,
        })
        volume, _tonnage = calculated_trip_volume_and_tonnage(trip)
        bucket['volume_m3'] += volume
        bucket['trip_count'] += 1
        excavator['volume_m3'] += volume
        excavator['trip_count'] += 1
        total_volume += volume
        total_trips += 1

    bucket_rows = sorted(buckets.values(), key=lambda item: item['key'])
    excavator_rows = sorted(excavators.values(), key=lambda item: (item['volume_m3'], item['trip_count']), reverse=True)
    max_bucket_volume = max((row['volume_m3'] for row in bucket_rows), default=Decimal('0'))
    max_excavator_volume = max((row['volume_m3'] for row in excavator_rows), default=Decimal('0'))

    for row in bucket_rows:
        row['volume_display'] = format_volume(row['volume_m3'])
        row['share'] = percent(row['volume_m3'], max_bucket_volume) if max_bucket_volume else Decimal('0')
        row['bar'] = int(row['share'])

    for row in excavator_rows:
        row['volume_display'] = format_volume(row['volume_m3'])
        row['share'] = percent(row['volume_m3'], max_excavator_volume) if max_excavator_volume else Decimal('0')
        row['bar'] = int(row['share'])
        row['total_share'] = percent(row['volume_m3'], total_volume) if total_volume else Decimal('0')

    return {
        'date_from': date_from,
        'date_to': date_to,
        'granularity': granularity,
        'granularity_label': DYNAMICS_GRANULARITY_LABELS[granularity],
        'selected_excavator_ids': excavator_ids,
        'bucket_rows': bucket_rows,
        'excavator_rows': excavator_rows,
        'chart_points': build_chart_points(bucket_rows, max_bucket_volume),
        'max_bucket_volume_display': format_volume(max_bucket_volume),
        'total_volume': total_volume,
        'total_volume_display': format_volume(total_volume),
        'trip_count': total_trips,
        'bucket_count': len(bucket_rows),
        'excavator_count': len(excavator_rows),
    }


def trip_queryset_for_unloading(selected_date):
    return (
        Trip.objects
        .filter(status=TripStatus.COMPLETED)
        .filter(
            Q(completed_at__date=selected_date)
            | Q(completed_at__isnull=True, created_at__date=selected_date)
        )
        .select_related(
            'truck',
            'truck__model',
            'driver',
            'excavator',
            'rock_type',
            'dump_point',
            'assigned_dump_point',
            'actual_dump_point',
            'loading_shift',
            'unloading_shift',
        )
        .order_by('-completed_at', '-created_at')
    )


def downtime_queryset_for_date(selected_date):
    return (
        DowntimeEvent.objects
        .filter(started_at__date=selected_date)
        .select_related(
            'equipment',
            'equipment__equipment_type',
            'employee',
            'reason',
            'reason__equipment_state',
        )
        .order_by('-started_at')
    )


def downtime_shift_type(event):
    hour = timezone.localtime(event.started_at).hour
    return 'day' if 8 <= hour < 20 else 'night'


def downtime_duration_hours(event, now=None):
    now = now or timezone.now()
    ended_at = event.ended_at or now
    seconds = max(int((ended_at - event.started_at).total_seconds()), 0)
    return (Decimal(seconds) / Decimal('3600')).quantize(Decimal('0.01'))


def new_trip_group(label):
    return {
        'label': label or 'Не указано',
        'trip_count': 0,
        'loaded_count': 0,
        'unloaded_count': 0,
        'open_count': 0,
        'carryover_count': 0,
        'volume_m3': Decimal('0'),
        'tonnage': Decimal('0'),
        'trucks': set(),
        'excavators': set(),
        'employees': set(),
        'rocks': set(),
        'dump_points': set(),
        'faces': set(),
    }


def add_trip_to_group(group, trip, *, role):
    volume, tonnage = calculated_trip_volume_and_tonnage(trip)
    group['trip_count'] += 1
    group['volume_m3'] += volume
    group['tonnage'] += tonnage
    if role == 'loading':
        group['loaded_count'] += 1
    if role == 'unloading':
        group['unloaded_count'] += 1
    if trip.status in OPEN_TRIP_STATUSES:
        group['open_count'] += 1
    if trip.is_carryover:
        group['carryover_count'] += 1
    if trip.truck_id:
        group['trucks'].add(str(trip.truck))
    if trip.excavator_id:
        group['excavators'].add(str(trip.excavator))
    if trip.rock_type_id:
        group['rocks'].add(str(trip.rock_type))
    dump_point = trip.actual_dump_point or trip.assigned_dump_point or trip.dump_point
    if dump_point:
        group['dump_points'].add(str(dump_point))
    face = ' / '.join(part for part in [trip.loading_horizon, trip.loading_block] if part)
    if face:
        group['faces'].add(face)


def finish_trip_group_rows(grouped):
    rows = sorted(grouped.values(), key=lambda item: (item['volume_m3'], item['trip_count']), reverse=True)
    max_volume = max((row['volume_m3'] for row in rows), default=Decimal('0'))
    for row in rows:
        row['volume_display'] = format_volume(row['volume_m3'])
        row['tonnage_display'] = format_decimal(row['tonnage'], places=1)
        row['share'] = percent(row['volume_m3'], max_volume) if max_volume else Decimal('0')
        row['bar'] = int(row['share'])
        row['trucks_display'] = ', '.join(sorted(row['trucks'])) or '-'
        row['excavators_display'] = ', '.join(sorted(row['excavators'])) or '-'
        row['employees_display'] = ', '.join(sorted(row['employees'])) or '-'
        row['rocks_display'] = ', '.join(sorted(row['rocks'])) or '-'
        row['dump_points_display'] = ', '.join(sorted(row['dump_points'])) or '-'
        row['faces_display'] = ', '.join(sorted(row['faces'])) or '-'
    return rows


def group_trips(trips, key_getter, label_getter, *, role):
    grouped = {}
    for trip in trips:
        key = key_getter(trip) or 'not-set'
        if key not in grouped:
            grouped[key] = new_trip_group(label_getter(trip))
        add_trip_to_group(grouped[key], trip, role=role)
    return finish_trip_group_rows(grouped)


def build_employee_rows(loading_trips, unloading_trips):
    grouped = {}
    for trip in loading_trips:
        key = f'operator-{trip.excavator_operator_id or "none"}'
        if key not in grouped:
            label = trip.excavator_operator.full_name if trip.excavator_operator_id else 'Машинист не указан'
            grouped[key] = new_trip_group(label)
            grouped[key]['role'] = 'Машинист экскаватора'
        add_trip_to_group(grouped[key], trip, role='loading')

    for trip in unloading_trips:
        key = f'driver-{trip.driver_id or "none"}'
        if key not in grouped:
            label = trip.driver.full_name if trip.driver_id else 'Водитель не указан'
            grouped[key] = new_trip_group(label)
            grouped[key]['role'] = 'Водитель самосвала'
        add_trip_to_group(grouped[key], trip, role='unloading')

    rows = finish_trip_group_rows(grouped)
    for row in rows:
        row.setdefault('role', '-')
    return rows


def build_downtime_rows(events, now=None):
    grouped = {}
    for event in events:
        key = event.reason_id or 'not-set'
        if key not in grouped:
            color_group = event.reason.effective_color_group if event.reason_id else 'yellow'
            grouped[key] = {
                'label': str(event.reason) if event.reason_id else 'Не указано',
                'count': 0,
                'open_count': 0,
                'duration_hours': Decimal('0'),
                'equipment': set(),
                'employees': set(),
                'color_group': color_group,
            }
        row = grouped[key]
        row['count'] += 1
        row['duration_hours'] += downtime_duration_hours(event, now=now)
        if event.ended_at is None:
            row['open_count'] += 1
        if event.equipment_id:
            row['equipment'].add(str(event.equipment))
        if event.employee_id:
            row['employees'].add(str(event.employee))

    rows = sorted(grouped.values(), key=lambda item: (item['open_count'], item['duration_hours'], item['count']), reverse=True)
    max_duration = max((row['duration_hours'] for row in rows), default=Decimal('0'))
    for row in rows:
        row['duration_display'] = format_decimal(row['duration_hours'], places=2)
        row['bar'] = int(percent(row['duration_hours'], max_duration)) if max_duration else 0
        row['equipment_display'] = ', '.join(sorted(row['equipment'])) or '-'
        row['employees_display'] = ', '.join(sorted(row['employees'])) or '-'
    return rows


def build_shift_analytics(selected_date, shift_type=''):
    shift_type = shift_type if shift_type in {'', 'day', 'night'} else ''
    loading_trips = [
        trip
        for trip in trip_queryset_for_loading(selected_date)
        if shift_matches(trip_loading_shift_type(trip), shift_type)
    ]
    unloading_trips = [
        trip
        for trip in trip_queryset_for_unloading(selected_date)
        if shift_matches(trip_unloading_shift_type(trip), shift_type)
    ]
    downtime_events = [
        event
        for event in downtime_queryset_for_date(selected_date)
        if shift_matches(downtime_shift_type(event), shift_type)
    ]

    total_volume = Decimal('0')
    total_tonnage = Decimal('0')
    for trip in loading_trips:
        volume, tonnage = calculated_trip_volume_and_tonnage(trip)
        total_volume += volume
        total_tonnage += tonnage

    downtime_hours = sum((downtime_duration_hours(event) for event in downtime_events), Decimal('0'))
    open_trip_count = sum(1 for trip in loading_trips if trip.status in OPEN_TRIP_STATUSES)
    carryover_count = sum(1 for trip in unloading_trips if trip.is_carryover)

    return {
        'selected_date': selected_date,
        'shift_type': shift_type,
        'shift_label': SHIFT_LABELS[shift_type],
        'totals': {
            'loaded_trip_count': len(loading_trips),
            'unloaded_trip_count': len(unloading_trips),
            'open_trip_count': open_trip_count,
            'carryover_count': carryover_count,
            'volume_m3': total_volume,
            'volume_display': format_volume(total_volume),
            'tonnage': total_tonnage,
            'tonnage_display': format_decimal(total_tonnage, places=1),
            'downtime_count': len(downtime_events),
            'downtime_hours': downtime_hours,
            'downtime_hours_display': format_decimal(downtime_hours, places=2),
        },
        'excavator_rows': group_trips(
            loading_trips,
            lambda trip: trip.excavator_id,
            lambda trip: str(trip.excavator) if trip.excavator_id else 'Экскаватор не указан',
            role='loading',
        ),
        'truck_rows': group_trips(
            unloading_trips,
            lambda trip: trip.truck_id,
            lambda trip: str(trip.truck) if trip.truck_id else 'Самосвал не указан',
            role='unloading',
        ),
        'employee_rows': build_employee_rows(loading_trips, unloading_trips),
        'rock_rows': group_trips(
            loading_trips,
            lambda trip: trip.rock_type_id,
            lambda trip: str(trip.rock_type) if trip.rock_type_id else 'Порода не указана',
            role='loading',
        ),
        'dump_point_rows': group_trips(
            loading_trips,
            lambda trip: (trip.actual_dump_point_id or trip.assigned_dump_point_id or trip.dump_point_id),
            lambda trip: str(trip.actual_dump_point or trip.assigned_dump_point or trip.dump_point or 'Точка не указана'),
            role='loading',
        ),
        'face_rows': group_trips(
            loading_trips,
            lambda trip: ' / '.join(part for part in [trip.loading_horizon, trip.loading_block] if part),
            lambda trip: ' / '.join(part for part in [trip.loading_horizon, trip.loading_block] if part) or 'Горизонт/блок не указаны',
            role='loading',
        ),
        'downtime_reason_rows': build_downtime_rows(downtime_events),
        'loading_trips': loading_trips,
        'unloading_trips': unloading_trips,
        'downtime_events': downtime_events,
    }
