"""Данные для экрана «Часовая сетка смены».

Старая почасовая форма — матрица «экскаватор × парк × час × точка разгрузки».
Она верна по содержанию, но по ширине занимает три-четыре экрана, поэтому на
мониторе её нельзя читать целиком.

Здесь та же самая выборка рейсов сворачивается в компактную структуру: строки —
экскаватор и парк машин, колонки — часы, а разрез по точкам разгрузки лежит
внутри каждой клетки. Экран раскрывает его по требованию: точки колонками
внутри выбранного часа или строками внутри выбранного экскаватора.

Источник рейсов и правило отнесения к часу те же, что в
`build_hourly_shift_matrix`: час берётся по времени погрузки на экскаваторе,
в сводку входят рейсы «загружен, едет» и «завершён».
"""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from downtimes.models import DowntimeEvent
from references.models import Equipment
from trips.models import TripStatus

from .dispatcher_shift_forms import (
    ZERO,
    decimal_sum,
    effective_dump_point,
    is_nhl,
    load_shift_trips,
    natural_key,
    shift_meta,
)

# Те же статусы, что у почасовой сводки и выемочного оборудования
# (`build_dispatcher_shift_report`): рейс с непроконтролированной разгрузкой
# погружен и объём у него есть, поэтому в час он попадать обязан — иначе сетка
# показывает меньше соседних вкладок того же отчёта. Статус появился в системе
# позже сетки; getattr — чтобы код работал и на сборках без него.
UNCONTROLLED_STATUS = getattr(TripStatus, 'UNCONTROLLED', None)
GRID_STATUSES = tuple(
    status for status in (
        TripStatus.LOADED_WAITING_UNLOAD,
        TripStatus.COMPLETED,
        UNCONTROLLED_STATUS,
    )
    if status is not None
)

FLEETS = ('belaz', 'nhl')
FLEET_LABELS = {'belaz': 'БелАЗ', 'nhl': 'NHL'}


def _empty_fleet_hour():
    return {'trips': 0, 'volume': ZERO, 'running': 0, 'points': {}}


def _hour_index(hours, moment):
    for hour in hours:
        if hour['start'] <= moment < hour['end']:
            return hour['index']
    return None


def build_hourly_grid(selected_date, shift_type, *, now=None):
    now = now or timezone.now()
    meta = shift_meta(selected_date, shift_type)
    trips = load_shift_trips(selected_date, shift_type, statuses=GRID_STATUSES)

    def hour_slot(start, end, outside):
        return {
            'start': start,
            'end': end,
            'label': f'{timezone.localtime(start):%H}',
            'range_label': f'{timezone.localtime(start):%H:%M}–{timezone.localtime(end):%H:%M}',
            'is_future': start > now,
            'is_current': start <= now < end,
            'is_outside': outside,
            'trips': 0,
            'volume': ZERO,
            'running': 0,
            'points': {},
        }

    hours = []
    hour_start = meta['start']
    while hour_start < meta['end']:
        hour_end = min(hour_start + timedelta(hours=1), meta['end'])
        hours.append(hour_slot(hour_start, hour_end, False))
        hour_start = hour_end

    nominal_hours = len(hours)

    # Смену открывают и закрывают не по гудку: бывает, что смену открыли
    # заранее и грузили ещё до её часов, или доработали после. Такие рейсы
    # принадлежат смене (они привязаны к ней сменой погрузки), но в часы смены
    # не попадают — раньше они просто пропадали из сетки и из её итогов, хотя
    # на других вкладках отчёта считались. Поэтому за каждый час, в котором
    # реально были рейсы, добавляем крайнюю колонку.
    outside_starts = set()
    for trip in trips:
        if meta['start'] <= trip.created_at < meta['end']:
            continue
        local = timezone.localtime(trip.created_at)
        outside_starts.add(local.replace(minute=0, second=0, microsecond=0))
    for start in outside_starts:
        hours.append(hour_slot(start, start + timedelta(hours=1), True))

    hours.sort(key=lambda hour: hour['start'])
    for index, hour in enumerate(hours):
        hour['index'] = index

    excavators = {}
    point_names = {}

    def excavator_row(equipment):
        key = equipment.id if equipment else 0
        if key not in excavators:
            excavators[key] = {
                'id': key,
                'label': equipment.garage_number if equipment else 'Без экскаватора',
                'sort': natural_key(equipment.garage_number if equipment else 'я'),
                'fleets': {
                    fleet: {
                        'key': fleet,
                        'label': FLEET_LABELS[fleet],
                        'hours': [_empty_fleet_hour() for _ in hours],
                        'trips': 0,
                        'volume': ZERO,
                    }
                    for fleet in FLEETS
                },
                'points': {},
                'idle': {},
                'trips': 0,
                'volume': ZERO,
                'running': 0,
            }
        return excavators[key]

    for equipment in Equipment.objects.filter(is_active=True).select_related('equipment_type'):
        if 'экскаватор' in equipment.equipment_type.name.lower():
            excavator_row(equipment)

    for trip in trips:
        index = _hour_index(hours, trip.created_at)
        if index is None:
            continue
        row = excavator_row(trip.excavator)
        fleet = 'nhl' if is_nhl(trip.truck) else 'belaz'
        point = effective_dump_point(trip)
        point_key = point.id if point else 0
        point_names[point_key] = str(point) if point else 'Без точки'
        volume = trip.volume_m3 or ZERO
        # «В пути» — только рейсы, которые едут на разгрузку. Рейс с
        # непроконтролированной разгрузкой уже не в пути: он выгружен, но
        # разгрузку никто не подтвердил, и ждёт разбора диспетчера.
        is_running = trip.status == TripStatus.LOADED_WAITING_UNLOAD

        cell = row['fleets'][fleet]['hours'][index]
        cell['trips'] += 1
        cell['volume'] += volume
        cell['points'][point_key] = cell['points'].get(point_key, 0) + 1
        if is_running:
            cell['running'] += 1

        row['fleets'][fleet]['trips'] += 1
        row['fleets'][fleet]['volume'] += volume
        row['trips'] += 1
        row['volume'] += volume
        if is_running:
            row['running'] += 1

        row_point = row['points'].setdefault(point_key, {'trips': 0, 'volume': ZERO, 'hours': {}})
        row_point['trips'] += 1
        row_point['volume'] += volume
        row_point['hours'][index] = row_point['hours'].get(index, 0) + 1

        hours[index]['trips'] += 1
        hours[index]['volume'] += volume
        hours[index]['points'][point_key] = hours[index]['points'].get(point_key, 0) + 1
        if is_running:
            hours[index]['running'] += 1

    # Простои: относим к часу, в котором простой начался, и считаем его до конца
    # простоя, до конца часа или до текущего момента — что раньше.
    equipment_ids = [key for key in excavators if key]
    if equipment_ids:
        downtimes = (
            DowntimeEvent.objects
            .filter(equipment_id__in=equipment_ids, started_at__lt=meta['end'])
            .filter(Q(ended_at__isnull=True) | Q(ended_at__gt=meta['start']))
            .select_related('reason')
        )
        for event in downtimes:
            index = _hour_index(hours, event.started_at)
            if index is None:
                continue
            row = excavators.get(event.equipment_id)
            if not row:
                continue
            hour_end = hours[index]['end']
            finished = min(event.ended_at or now, now, hour_end)
            minutes = max(0, int((finished - event.started_at).total_seconds() // 60))
            slot = row['idle'].setdefault(index, {'minutes': 0, 'reasons': []})
            slot['minutes'] += minutes
            reason_name = str(event.reason) if event.reason_id else 'Простой'
            if reason_name not in slot['reasons']:
                slot['reasons'].append(reason_name)

    ordered = sorted(excavators.values(), key=lambda row: row['sort'])
    current_index = next((hour['index'] for hour in hours if hour['is_current']), None)
    closed_hours = sum(1 for hour in hours if not hour['is_future'] and not hour['is_current'])
    total_trips = sum(hour['trips'] for hour in hours)
    total_volume = decimal_sum(hour['volume'] for hour in hours)
    total_running = sum(hour['running'] for hour in hours)
    elapsed = closed_hours + (1 if current_index is not None else 0)

    return {
        'meta': meta,
        'hours': hours,
        'rows': ordered,
        'point_names': point_names,
        'current_hour': current_index,
        'closed_hours': closed_hours,
        'elapsed_hours': elapsed,
        'trips': total_trips,
        'volume': total_volume,
        'running': total_running,
        'rate': (total_volume / elapsed) if elapsed else ZERO,
        # Прогноз считается на смену, а не на добавленные крайние часы: иначе
        # смена, открытая пораньше, «дорисовывала» бы себе плановый объём.
        'forecast': (total_volume / elapsed * nominal_hours) if elapsed else ZERO,
        'nominal_hours': nominal_hours,
        'outside_hours': sum(1 for hour in hours if hour['is_outside']),
    }


def hourly_grid_payload(grid):
    """Плоский JSON для экрана: сервер считает, страница только рисует."""
    return {
        'meta': {
            'label': grid['meta']['label'],
            'time_range': grid['meta']['time_range'],
        },
        'hours': [
            {
                'index': hour['index'],
                'label': hour['label'],
                'range': hour['range_label'],
                'future': hour['is_future'],
                'current': hour['is_current'],
                'outside': hour['is_outside'],
                'trips': hour['trips'],
                'volume': float(hour['volume']),
                'running': hour['running'],
                'points': {str(key): value for key, value in hour['points'].items()},
            }
            for hour in grid['hours']
        ],
        'points': {str(key): name for key, name in grid['point_names'].items()},
        'rows': [
            {
                'id': row['id'],
                'label': row['label'],
                'trips': row['trips'],
                'volume': float(row['volume']),
                'running': row['running'],
                'fleets': [
                    {
                        'key': fleet['key'],
                        'label': fleet['label'],
                        'trips': fleet['trips'],
                        'volume': float(fleet['volume']),
                        'hours': [
                            {
                                'trips': cell['trips'],
                                'volume': float(cell['volume']),
                                'running': cell['running'],
                                'points': {str(key): value for key, value in cell['points'].items()},
                            }
                            for cell in fleet['hours']
                        ],
                    }
                    for fleet in (row['fleets'][key] for key in FLEETS)
                ],
                'points': [
                    {
                        'key': str(key),
                        'name': grid['point_names'].get(key, 'Без точки'),
                        'trips': value['trips'],
                        'volume': float(value['volume']),
                        'hours': {str(index): count for index, count in value['hours'].items()},
                    }
                    for key, value in sorted(row['points'].items(), key=lambda item: -item[1]['trips'])
                ],
                'idle': {
                    str(index): {'minutes': slot['minutes'], 'reason': ', '.join(slot['reasons'])}
                    for index, slot in row['idle'].items()
                },
            }
            for row in grid['rows']
        ],
        'totals': {
            'trips': grid['trips'],
            'volume': float(grid['volume']),
            'running': grid['running'],
            'rate': float(grid['rate']),
            'forecast': float(grid['forecast']),
            'elapsed_hours': grid['elapsed_hours'],
            'hours_total': grid['nominal_hours'],
            'hours_outside': grid['outside_hours'],
            'current_hour': grid['current_hour'],
        },
    }
