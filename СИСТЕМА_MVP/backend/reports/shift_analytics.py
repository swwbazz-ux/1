from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from downtimes.models import DowntimeEvent
from references.models import TruckCapacityRule
from trips.models import OPEN_TRIP_STATUSES, Trip, TripClientAction, TripStatus


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

DYNAMICS_CHART_MODE_LABELS = {
    'cumulative': 'накопительно м3',
    'rate': 'темп м3/час',
    'trips': 'рейсы',
}

DYNAMICS_CHART_MODE_AXIS_LABELS = {
    'cumulative': 'м3',
    'rate': 'м3/час',
    'trips': 'рейсы',
}

DYNAMICS_CHART_X_AXIS_LABELS = {
    'hour': 'часы',
    'shift': 'смены',
    'day': 'дни',
    'month': 'месяцы',
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


CHART_LEFT = Decimal('45')
CHART_RIGHT = Decimal('998')
CHART_TOP = Decimal('36')
CHART_BOTTOM = Decimal('250')
CHART_HEIGHT = CHART_BOTTOM - CHART_TOP
CHART_WIDTH = CHART_RIGHT - CHART_LEFT
DAY_SHIFT_START_HOUR = 7
DAY_SHIFT_END_HOUR = 19
NIGHT_SHIFT_START_HOUR = 19
NIGHT_SHIFT_END_HOUR = 7


def chart_x(index, last_index):
    if last_index <= 0:
        return CHART_LEFT + (CHART_WIDTH / Decimal('2'))
    return CHART_LEFT + (Decimal(index) / Decimal(last_index) * CHART_WIDTH)


def chart_y(volume, max_volume):
    if not max_volume:
        return CHART_BOTTOM
    return CHART_BOTTOM - (volume / max_volume * CHART_HEIGHT)


def build_chart_points(rows, max_volume):
    if not rows or not max_volume:
        return ''
    if len(rows) == 1:
        y = int(chart_y(rows[0]['volume_m3'], max_volume))
        return f'460,{y} 540,{y}'
    points = []
    last_index = len(rows) - 1
    for index, row in enumerate(rows):
        x = chart_x(index, last_index)
        y = chart_y(row['volume_m3'], max_volume)
        points.append(f'{int(x)},{int(y)}')
    return ' '.join(points)


def build_chart_area_points(points):
    if not points:
        return ''
    point_parts = points.split()
    if not point_parts:
        return ''
    first_x = point_parts[0].split(',', 1)[0]
    last_x = point_parts[-1].split(',', 1)[0]
    return f'{first_x},{int(CHART_BOTTOM)} {points} {last_x},{int(CHART_BOTTOM)}'


def build_chart_area_path(points):
    if not points:
        return ''
    point_parts = points.split()
    if not point_parts:
        return ''
    path_points = ' L '.join(part.replace(',', ' ') for part in point_parts)
    first_x = point_parts[0].split(',', 1)[0]
    last_x = point_parts[-1].split(',', 1)[0]
    return f'M {first_x} {int(CHART_BOTTOM)} L {path_points} L {last_x} {int(CHART_BOTTOM)} Z'


def chart_time_x(event_at, range_start, range_end):
    total_seconds = max((range_end - range_start).total_seconds(), 1)
    event_seconds = (event_at - range_start).total_seconds()
    event_seconds = min(max(event_seconds, 0), total_seconds)
    return CHART_LEFT + (Decimal(str(event_seconds)) / Decimal(str(total_seconds)) * CHART_WIDTH)


def build_time_chart_points(rows, max_value, range_start, range_end):
    if not rows or not max_value:
        return ''
    points = []
    for row in rows:
        x = chart_time_x(row['event_at'], range_start, range_end)
        y = chart_y(row['value'], max_value)
        points.append(f'{int(x)},{int(y)}')
    return ' '.join(points)


def build_chart_y_axis_ticks(max_volume):
    if not max_volume:
        return []
    ticks = []
    for step in range(4, -1, -1):
        value = max_volume * Decimal(step) / Decimal('4')
        y = int(chart_y(value, max_volume))
        ticks.append({
            'label': format_volume(value),
            'y': y,
            'text_y': y + 5,
        })
    return ticks


def chart_tick_label(row, granularity, include_date):
    label = row['label']
    if granularity == 'hour':
        return label.replace(':00', '') if include_date else label[-5:]
    return label


def build_chart_x_axis_ticks(rows, granularity='day'):
    if not rows:
        return []
    last_index = len(rows) - 1
    if granularity == 'hour':
        indexes = list(range(len(rows)))
    elif len(rows) <= 8:
        indexes = list(range(len(rows)))
    else:
        step = max(1, (last_index + 6) // 7)
        indexes = list(range(0, len(rows), step))
        if indexes[-1] != last_index:
            indexes.append(last_index)
    include_date = granularity == 'hour' and len({row['label'][:5] for row in rows}) > 1
    return [
        {
            'label': chart_tick_label(rows[index], granularity, include_date),
            'x': int(chart_x(index, last_index)),
            'y': 286,
            'anchor': 'start' if index == 0 else 'end' if index == last_index else 'middle',
        }
        for index in indexes
    ]


def dynamics_time_range(date_from, date_to, granularity, shift_type):
    tz = timezone.get_current_timezone()
    if granularity == 'hour':
        if shift_type == 'night':
            range_start = timezone.make_aware(
                datetime.combine(date_from, time(hour=NIGHT_SHIFT_START_HOUR)),
                tz,
            )
            range_end = timezone.make_aware(
                datetime.combine(date_to + timedelta(days=1), time(hour=NIGHT_SHIFT_END_HOUR)),
                tz,
            )
        else:
            range_start = timezone.make_aware(
                datetime.combine(date_from, time(hour=DAY_SHIFT_START_HOUR)),
                tz,
            )
            range_end = timezone.make_aware(
                datetime.combine(date_to, time(hour=DAY_SHIFT_END_HOUR)),
                tz,
            )
        return range_start, range_end
    range_start = timezone.make_aware(datetime.combine(date_from, time.min), tz)
    range_end = timezone.make_aware(datetime.combine(date_to + timedelta(days=1), time.min), tz)
    return range_start, range_end


def build_time_chart_x_axis_ticks(range_start, range_end, granularity, shift_type):
    ticks = []
    if granularity == 'hour':
        total_hours = max(int((range_end - range_start).total_seconds() // 3600), 1)
        step_hours = 1 if total_hours <= 12 else max(2, (total_hours + 11) // 12)
        tick_at = range_start
        while tick_at <= range_end:
            x = int(chart_time_x(tick_at, range_start, range_end))
            ticks.append({
                'label': timezone.localtime(tick_at).strftime('%H:%M'),
                'x': x,
                'y': 286,
                'anchor': 'start' if tick_at == range_start else 'end' if tick_at == range_end else 'middle',
            })
            tick_at += timedelta(hours=step_hours)
        if ticks[-1]['x'] != int(CHART_RIGHT):
            ticks.append({
                'label': timezone.localtime(range_end).strftime('%H:%M'),
                'x': int(CHART_RIGHT),
                'y': 286,
                'anchor': 'end',
            })
        return ticks
    return build_chart_x_axis_ticks([
        {'label': timezone.localtime(range_start).strftime('%d.%m')},
        {'label': timezone.localtime(range_end - timedelta(seconds=1)).strftime('%d.%m')},
    ], granularity)


def empty_dynamics_bucket(bucket_key, bucket_label):
    return {
        'key': bucket_key,
        'label': bucket_label,
        'volume_m3': Decimal('0'),
        'trip_count': 0,
        'excavators': {},
    }


def hourly_shift_hours(shift_type):
    if shift_type == 'night':
        return list(range(NIGHT_SHIFT_START_HOUR, 24)) + list(range(0, NIGHT_SHIFT_END_HOUR + 1))
    return list(range(DAY_SHIFT_START_HOUR, DAY_SHIFT_END_HOUR + 1))


def hourly_shift_bucket_date(selected_date, shift_type, hour):
    if shift_type == 'night' and hour <= NIGHT_SHIFT_END_HOUR:
        return selected_date + timedelta(days=1)
    return selected_date


def hourly_shift_operational_date(local_dt, shift_type):
    if shift_type == 'night':
        if local_dt.hour >= NIGHT_SHIFT_START_HOUR:
            return local_dt.date()
        if local_dt.hour < NIGHT_SHIFT_END_HOUR:
            return local_dt.date() - timedelta(days=1)
        return None
    if DAY_SHIFT_START_HOUR <= local_dt.hour < DAY_SHIFT_END_HOUR:
        return local_dt.date()
    return None


def local_dt_in_hourly_shift_range(local_dt, date_from, date_to, shift_type):
    operational_date = hourly_shift_operational_date(local_dt, shift_type)
    return operational_date is not None and date_from <= operational_date <= date_to


def hourly_shift_bucket_rows(buckets, date_from, date_to, shift_type='day'):
    rows = []
    selected_date = date_from
    while selected_date <= date_to:
        for hour in hourly_shift_hours(shift_type):
            bucket_date = hourly_shift_bucket_date(selected_date, shift_type, hour)
            bucket_key = f'{bucket_date:%Y-%m-%d}-{hour:02d}'
            bucket_label = f'{bucket_date:%d.%m} {hour:02d}:00'
            rows.append(buckets.setdefault(bucket_key, empty_dynamics_bucket(bucket_key, bucket_label)))
        selected_date += timedelta(days=1)
    return rows


def build_dynamics_chart_series(bucket_rows, excavator_rows, max_volume):
    if not bucket_rows or not max_volume:
        return []

    colors = ['#7de05e', '#5fc7d8', '#ffb454', '#7aa7ff', '#d784ff', '#ef5b58', '#9be06d', '#52a7ff']
    series = []

    if len(excavator_rows) > 1:
        total_points = build_chart_points(bucket_rows, max_volume)
        if total_points:
            series.append({
                'label': 'Сумма',
                'color': '#7de05e',
                'points': total_points,
                'area_points': '',
                'area_path': '',
                'is_total': True,
            })

    for index, excavator in enumerate(excavator_rows[:8]):
        excavator_key = excavator['id'] or 0
        rows = []
        for bucket in bucket_rows:
            rows.append({
                'volume_m3': bucket.get('excavators', {}).get(excavator_key, Decimal('0')),
            })
        points = build_chart_points(rows, max_volume)
        if not points:
            continue
        series.append({
            'label': excavator['label'],
            'color': colors[index % len(colors)],
            'points': points,
            'area_points': '',
            'area_path': '',
            'is_total': False,
        })
    if series:
        series[0]['area_points'] = build_chart_area_points(series[0]['points'])
        series[0]['area_path'] = build_chart_area_path(series[0]['points'])
    return series


def trip_loaded_action_times(trips):
    trip_ids = [trip.id for trip in trips if trip.id]
    if not trip_ids:
        return {}
    action_times = {}
    for trip_id, created_at in (
        TripClientAction.objects
        .filter(action_type='truck_loaded', trip_id__in=trip_ids)
        .order_by('created_at')
        .values_list('trip_id', 'created_at')
    ):
        action_times.setdefault(trip_id, created_at)
    return action_times


def build_event_chart_rows(events, chart_mode, range_start, range_end):
    if not events:
        return []
    rows = [{'event_at': range_start, 'value': Decimal('0')}]
    if chart_mode == 'rate':
        window = timedelta(minutes=60)
        for event in events:
            event_at = event['event_at']
            value = sum(
                (item['volume_m3'] for item in events if event_at - window < item['event_at'] <= event_at),
                Decimal('0'),
            )
            rows.append({'event_at': event_at, 'value': value})
        rows.append({'event_at': range_end, 'value': rows[-1]['value']})
        return rows

    running_value = Decimal('0')
    for event in events:
        running_value += Decimal('1') if chart_mode == 'trips' else event['volume_m3']
        rows.append({'event_at': event['event_at'], 'value': running_value})
    rows.append({'event_at': range_end, 'value': running_value})
    return rows


def build_dynamics_event_chart_series(events, excavator_rows, chart_mode, range_start, range_end):
    rows = build_event_chart_rows(events, chart_mode, range_start, range_end)
    max_value = max((row['value'] for row in rows), default=Decimal('0'))
    if not rows or not max_value:
        return [], Decimal('0')
    points = build_time_chart_points(rows, max_value, range_start, range_end)
    if not points:
        return [], max_value
    if len(excavator_rows) == 1:
        label = excavator_rows[0]['label']
    else:
        label = 'Сумма'
    return [{
        'label': label,
        'color': '#7de05e',
        'points': points,
        'area_points': build_chart_area_points(points),
        'area_path': build_chart_area_path(points),
        'is_total': True,
    }], max_value


def build_excavator_dynamics(date_from, date_to, granularity='day', excavator_ids=None, shift_type='day', chart_mode='cumulative'):
    granularity = granularity if granularity in DYNAMICS_GRANULARITY_LABELS else 'day'
    shift_type = shift_type if shift_type in {'day', 'night'} else 'day'
    chart_mode = chart_mode if chart_mode in DYNAMICS_CHART_MODE_LABELS else 'cumulative'
    excavator_ids = [int(item) for item in (excavator_ids or []) if str(item).isdigit()]
    query_date_to = date_to + timedelta(days=1) if granularity == 'hour' and shift_type == 'night' else date_to
    trips = trip_queryset_for_loading_range(date_from, query_date_to)
    if excavator_ids:
        trips = trips.filter(excavator_id__in=excavator_ids)
    trips = list(trips)
    loaded_action_times = trip_loaded_action_times(trips)
    chart_range_start, chart_range_end = dynamics_time_range(date_from, date_to, granularity, shift_type)

    buckets = {}
    excavators = {}
    event_rows = []
    total_volume = Decimal('0')
    total_trips = 0

    for trip in trips:
        local_dt = timezone.localtime(trip.created_at)
        if granularity == 'hour' and not local_dt_in_hourly_shift_range(local_dt, date_from, date_to, shift_type):
            continue
        bucket_key, bucket_label = dynamics_bucket(trip.created_at, granularity)
        bucket = buckets.setdefault(bucket_key, empty_dynamics_bucket(bucket_key, bucket_label))
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
        bucket['excavators'][excavator_key] = bucket['excavators'].get(excavator_key, Decimal('0')) + volume
        excavator['volume_m3'] += volume
        excavator['trip_count'] += 1
        event_at = loaded_action_times.get(trip.id, trip.created_at)
        if chart_range_start <= event_at <= chart_range_end:
            event_rows.append({
                'event_at': event_at,
                'volume_m3': volume,
                'excavator_id': trip.excavator_id,
            })
        total_volume += volume
        total_trips += 1

    if granularity == 'hour':
        bucket_rows = hourly_shift_bucket_rows(buckets, date_from, date_to, shift_type)
    else:
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
        average_bucket = row['volume_m3'] / len(bucket_rows) if bucket_rows else Decimal('0')
        row['average_bucket_volume_display'] = format_volume(average_bucket)

    best_excavator = excavator_rows[0] if excavator_rows else None
    peak_bucket = max(bucket_rows, key=lambda item: item['volume_m3'], default=None)
    event_rows = sorted(event_rows, key=lambda item: item['event_at'])
    chart_series, chart_max_value = build_dynamics_event_chart_series(
        event_rows,
        excavator_rows,
        chart_mode,
        chart_range_start,
        chart_range_end,
    )
    if not chart_series:
        chart_max_value = max_bucket_volume
        chart_series = build_dynamics_chart_series(bucket_rows, excavator_rows, max_bucket_volume)
    analysis_signals = []
    if best_excavator:
        analysis_signals.append({
            'kind': 'green',
            'text': f"Лидер периода: {best_excavator['label']}, {best_excavator['volume_display']} м3.",
        })
    if peak_bucket:
        analysis_signals.append({
            'kind': 'cyan',
            'text': f"Максимальный период: {peak_bucket['label']}, {peak_bucket['volume_display']} м3.",
        })
    if len(excavator_rows) > 1 and best_excavator and total_volume:
        best_share = percent(best_excavator['volume_m3'], total_volume)
        analysis_signals.append({
            'kind': 'neutral',
            'text': f"Доля лидера в выбранной группе: {format_volume(best_share)}%.",
        })

    return {
        'date_from': date_from,
        'date_to': date_to,
        'granularity': granularity,
        'shift_type': shift_type,
        'chart_mode': chart_mode,
        'chart_mode_label': DYNAMICS_CHART_MODE_LABELS[chart_mode],
        'chart_mode_choices': [
            {'code': code, 'label': label}
            for code, label in DYNAMICS_CHART_MODE_LABELS.items()
        ],
        'chart_y_axis_title': DYNAMICS_CHART_MODE_AXIS_LABELS[chart_mode],
        'chart_x_axis_title': DYNAMICS_CHART_X_AXIS_LABELS[granularity],
        'granularity_label': DYNAMICS_GRANULARITY_LABELS[granularity],
        'selected_excavator_ids': excavator_ids,
        'bucket_rows': bucket_rows,
        'excavator_rows': excavator_rows,
        'chart_points': build_chart_points(bucket_rows, max_bucket_volume),
        'chart_series': chart_series,
        'chart_y_axis_ticks': build_chart_y_axis_ticks(chart_max_value),
        'chart_x_axis_ticks': build_time_chart_x_axis_ticks(chart_range_start, chart_range_end, granularity, shift_type),
        'max_bucket_volume_display': format_volume(max_bucket_volume),
        'best_excavator': best_excavator,
        'peak_bucket': peak_bucket,
        'analysis_signals': analysis_signals,
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
