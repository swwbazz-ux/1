import json
import re
from collections import defaultdict
from decimal import Decimal

from django.contrib import messages
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from assignments.models import AssignmentStatus, ExcavatorPlacement, HaulAssignment
from downtimes.models import DowntimeEvent
from references.models import Equipment
from shifts.models import EmployeeShift
from users.access_auth import find_employee_access_by_credentials
from users.models import EmployeeAccess
from users.session_device import get_session_device_kind, set_session_device_kind

from .forms import TripCreateForm
from .dispatcher_header import build_dispatcher_header_context, close_dispatcher_shift, get_active_dispatcher_shift, open_dispatcher_shift
from .models import DispatcherActionLog, DispatcherActionType, Trip, TripStatus


DISPATCHER_FILTER_KEYS = (
    'truck',
    'excavator',
    'show_active_trips',
    'show_pending_assignments',
    'show_accepted_assignments',
)

DISPATCHER_PLAN_TOTAL_TONS = Decimal('420000')


def format_dispatcher_number(value):
    value = int(value or 0)
    return f'{value:,}'.replace(',', ' ')


def format_dispatcher_datetime(value):
    if not value:
        return ''
    return timezone.localtime(value).strftime('%d.%m %H:%M')


def format_dispatcher_decimal(value):
    if value is None:
        return ''
    return f'{value:g}'


def equipment_short_name(equipment):
    if not equipment:
        return '-'
    return str(equipment.garage_number or equipment).replace('Экс ', 'EX-').replace('Экс', 'EX-')


def equipment_icon_key(equipment, status='green'):
    type_name = (getattr(getattr(equipment, 'equipment_type', None), 'name', '') or '').lower()
    prefix = 'excavator' if 'экскаватор' in type_name else 'truck'
    if status not in {'green', 'yellow', 'red', 'gray', 'blue'}:
        status = 'gray'
    return f'img/equipment/{prefix}-{status}.png'


def authenticate_dispatcher_shared_shift_start(request):
    phone = request.POST.get('reauth_phone', '').strip()
    access_code = re.sub(r'\D', '', request.POST.get('reauth_access_code', ''))
    device_kind = request.POST.get('device_kind', '').strip()
    if not phone or not access_code:
        return None, 'Для начала смены на общем компьютере введите телефон и код горного диспетчера.'
    if phone and not phone.startswith(('+', '7', '8')):
        phone = f'+7 {phone}'

    access = find_employee_access_by_credentials(phone, access_code, role_code='dispatcher')
    if not access:
        return None, 'Телефон или код горного диспетчера указаны неверно.'

    request.session['employee_access_id'] = access.id
    set_session_device_kind(request, device_kind)
    access.last_login_at = timezone.now()
    access.save(update_fields=['last_login_at'])
    return access, ''


def dispatcher_truck_garage_number(truck, fallback_index):
    raw_number = str(getattr(truck, 'garage_number', '') or '').strip()
    match = re.search(r'\d+', raw_number)
    if match:
        number = int(match.group(0))
        if number == 53:
            return None
        return str(number)
    return None


def dispatcher_employee_badge(employee):
    if not employee:
        return None
    photo_url = ''
    if getattr(employee, 'photo', None):
        try:
            photo_url = employee.photo.url
        except ValueError:
            photo_url = ''
    initials = ''.join(part[0] for part in (employee.full_name or '').split()[:2]).upper()
    return {
        'name': employee.full_name or '',
        'phone': employee.phone or '',
        'position': employee.position or '',
        'photo': photo_url,
        'initials': initials or '??',
    }


def add_dispatcher_detail(details, seen_labels, label, value):
    if value in {None, ''} or label in seen_labels:
        return
    seen_labels.add(label)
    details.append({'label': label, 'value': str(value)})


def dispatcher_trip_amount(trip):
    return trip.tonnage or trip.volume_m3 or Decimal('0')


def dispatcher_chart_percent(value, max_value):
    if not max_value:
        return 0
    return max(4, min(100, int((value / max_value) * 100)))


def dispatcher_summary_chart_rows(group_items, label_index, *, meta_index=None, max_rows=6):
    accents = ('green', 'blue', 'yellow', 'red')
    grouped_rows = defaultdict(lambda: {'volume': Decimal('0'), 'meta': set()})
    for key, row in group_items:
        label = key[label_index] or 'не указано'
        grouped_rows[label]['volume'] += row['volume']
        if meta_index is not None and key[meta_index]:
            grouped_rows[label]['meta'].add(key[meta_index])
    sorted_rows = sorted(grouped_rows.items(), key=lambda item: item[1]['volume'], reverse=True)[:max_rows]
    max_volume = max([row['volume'] for _, row in sorted_rows] or [Decimal('0')])
    return [
        {
            'label': label,
            'value': f'{format_dispatcher_number(row["volume"])} т',
            'percent': dispatcher_chart_percent(row['volume'], max_volume),
            'accent': accents[index % len(accents)],
            'meta': ', '.join(sorted(row['meta'])[:2]),
        }
        for index, (label, row) in enumerate(sorted_rows)
    ]


def dispatcher_trip_equipment_summary_rows(trips, *, equipment_attr='truck', max_rows=6):
    accents = ('green', 'blue', 'yellow', 'red')
    grouped_rows = defaultdict(lambda: Decimal('0'))
    for trip in trips:
        label = equipment_short_name(getattr(trip, equipment_attr, None))
        grouped_rows[label] += dispatcher_trip_amount(trip)
    sorted_rows = sorted(grouped_rows.items(), key=lambda item: item[1], reverse=True)[:max_rows]
    max_volume = max([volume for _, volume in sorted_rows] or [Decimal('0')])
    return [
        {
            'label': label,
            'value': f'{format_dispatcher_number(volume)} т',
            'percent': dispatcher_chart_percent(volume, max_volume),
            'accent': accents[index % len(accents)],
            'meta': 'текущая смена',
        }
        for index, (label, volume) in enumerate(sorted_rows)
    ]


def dispatcher_empty_shift_report(*, is_truck=False):
    counterpart_label = 'Экскаваторы' if is_truck else 'Самосвалы'
    return {
        'metrics': [
            {'label': 'Рейсы', 'value': '0'},
            {'label': 'Объем', 'value': '0 т'},
            {'label': 'Активные', 'value': '0'},
            {'label': 'Завершены', 'value': '0'},
            {'label': counterpart_label, 'value': '0'},
            {'label': 'Разгрузки', 'value': '0'},
        ],
        'charts': [
            {'type': 'route' if is_truck else 'matrix', 'title': 'Текущая смена', 'rows': []},
            {'type': 'donut-list', 'title': 'По разгрузке', 'rows': []},
            {'type': 'donut-list', 'title': 'По породе', 'rows': []},
            {'type': 'donut-list', 'title': 'По комплексам' if is_truck else 'По самосвалам', 'rows': []},
        ],
        'tables': [],
    }


def dispatcher_shift_report_for_equipment(equipment, *, equipment_kind='', shift_trips=None):
    equipment_type = (equipment_kind or getattr(getattr(equipment, 'equipment_type', None), 'name', '') or '').lower()
    is_truck = 'самосвал' in equipment_type
    is_excavator = 'экскаватор' in equipment_type
    trips = []
    if equipment and shift_trips:
        if is_truck:
            trips = [trip for trip in shift_trips if trip.truck_id == equipment.id]
        elif is_excavator:
            trips = [trip for trip in shift_trips if trip.excavator_id == equipment.id]
    if not trips:
        return dispatcher_empty_shift_report(is_truck=bool(is_truck))

    total_volume = sum((dispatcher_trip_amount(trip) for trip in trips), Decimal('0'))
    completed_count = sum(1 for trip in trips if trip.status == TripStatus.COMPLETED)
    active_count = sum(1 for trip in trips if trip.status == TripStatus.ACTIVE)
    dump_points = {str(trip.dump_point) for trip in trips}
    counterpart_ids = {trip.excavator_id if is_truck else trip.truck_id for trip in trips}
    metrics = [
        {'label': 'Рейсы', 'value': str(len(trips))},
        {'label': 'Объем', 'value': f'{format_dispatcher_number(total_volume)} т'},
        {'label': 'Активные', 'value': str(active_count)},
        {'label': 'Завершены', 'value': str(completed_count)},
    ]
    if is_truck:
        metrics.append({'label': 'Экскаваторы', 'value': str(len(counterpart_ids))})
    else:
        metrics.append({'label': 'Самосвалы', 'value': str(len(counterpart_ids))})
    metrics.append({'label': 'Разгрузки', 'value': str(len(dump_points))})

    grouped = {}
    for trip in trips:
        if is_truck:
            key = (equipment_short_name(trip.excavator), str(trip.dump_point), str(trip.rock_type))
        else:
            face = ' / '.join(part for part in [trip.loading_horizon, trip.loading_block] if part) or 'не указан'
            key = (face, str(trip.dump_point), str(trip.rock_type))
        row = grouped.setdefault(key, {'count': 0, 'volume': Decimal('0'), 'last': None, 'trucks': set()})
        row['count'] += 1
        row['volume'] += dispatcher_trip_amount(trip)
        row['last'] = max(row['last'] or trip.created_at, trip.completed_at or trip.created_at)
        if is_excavator:
            row['trucks'].add(equipment_short_name(trip.truck))

    sorted_groups = sorted(grouped.items(), key=lambda item: item[1]['volume'], reverse=True)[:6]
    max_volume = max([row['volume'] for _, row in sorted_groups] or [Decimal('0')])
    rows = []
    chart_rows = []
    for key, row in sorted_groups:
        if is_truck:
            rows.append([key[0], key[1], key[2], str(row['count']), f'{format_dispatcher_number(row["volume"])} т', format_dispatcher_datetime(row['last'])])
            chart_rows.append({
                'source': key[0],
                'target': key[1],
                'meta': key[2],
                'value': f'{format_dispatcher_number(row["volume"])} т',
                'percent': dispatcher_chart_percent(row['volume'], max_volume),
                'accent': 'green' if len(chart_rows) == 0 else 'blue' if len(chart_rows) == 1 else 'yellow',
            })
        else:
            rows.append([key[0], key[1], key[2], str(len(row['trucks'])), str(row['count']), f'{format_dispatcher_number(row["volume"])} т'])
            chart_rows.append({
                'label': key[0],
                'target': key[1],
                'meta': key[2],
                'value': f'{format_dispatcher_number(row["volume"])} т',
                'percent': dispatcher_chart_percent(row['volume'], max_volume),
                'accent': 'green' if len(chart_rows) == 0 else 'yellow' if len(chart_rows) == 1 else 'blue',
            })

    if is_truck:
        return {
            'metrics': metrics[:6],
            'charts': [
                {
                    'type': 'route',
                    'title': 'Маршруты',
                    'rows': chart_rows,
                },
                {
                    'type': 'donut-list',
                    'title': 'По разгрузке',
                    'rows': dispatcher_summary_chart_rows(grouped.items(), 1, meta_index=2),
                },
                {
                    'type': 'donut-list',
                    'title': 'По породе',
                    'rows': dispatcher_summary_chart_rows(grouped.items(), 2, meta_index=1),
                },
                {
                    'type': 'donut-list',
                    'title': 'По комплексам',
                    'rows': dispatcher_summary_chart_rows(grouped.items(), 0, meta_index=1),
                },
            ],
            'tables': [],
        }
    return {
        'metrics': metrics[:6],
        'charts': [
            {
                'type': 'matrix',
                'title': 'По забоям',
                'rows': chart_rows,
            },
            {
                'type': 'donut-list',
                'title': 'По разгрузке',
                'rows': dispatcher_summary_chart_rows(grouped.items(), 1, meta_index=2),
            },
            {
                'type': 'donut-list',
                'title': 'По породе',
                'rows': dispatcher_summary_chart_rows(grouped.items(), 2, meta_index=1),
            },
            {
                'type': 'donut-list',
                'title': 'По самосвалам',
                'rows': dispatcher_trip_equipment_summary_rows(trips, equipment_attr='truck'),
            },
        ],
        'tables': [],
    }

def dispatcher_complex_truck_rows(card):
    return list(card.get('truck_rows') or [])


def dispatcher_tons_from_label(value):
    if not value:
        return Decimal('0')
    digits = ''.join(char for char in str(value) if char.isdigit())
    return Decimal(digits or '0')


def dispatcher_complex_face_label(card):
    horizon = card.get('current_horizon') or ''
    block = card.get('current_block') or ''
    label = ' / '.join(part for part in [horizon, block] if part and '-' not in part)
    return label or 'Забой не указан'


def dispatcher_complex_location_parts(card):
    return (card.get('current_horizon') or 'Гор. -', card.get('current_block') or 'Блок -')


def dispatcher_complex_shift_report(card):
    status_key = card.get('status_key') or 'normal'
    assigned = int(card.get('assigned') or 0)
    need = int(card.get('need') or 0)
    balance = assigned - need
    percent = int(card.get('percent') or 0)
    truck_rows = dispatcher_complex_truck_rows(card)
    current_truck_rows = [row for row in truck_rows if row['state_key'] == 'current']
    removed_truck_rows = [row for row in truck_rows if row['state_key'] == 'removed']
    plan_value = f'{card.get("plan_tons", "0")} т'
    fact_value = f'{card.get("fact_tons", "0")} т'
    forecast_value = f'{card.get("forecast_tons", "0")} т'
    if status_key == 'danger':
        problem = 'остановлен'
        action = 'ремонт / перераспределить самосвалы'
    elif status_key == 'risk':
        problem = 'риск выполнения'
        action = 'добавить транспорт'
    else:
        problem = 'без отклонений'
        action = 'контроль нормы'

    def grouped_chart_rows(source_rows, field, meta_field):
        totals = defaultdict(Decimal)
        meta = defaultdict(set)
        for row in source_rows:
            label = row.get(field) or 'не указано'
            totals[label] += dispatcher_tons_from_label(row.get('value'))
            if row.get(meta_field):
                meta[label].add(row.get(meta_field))
        sorted_rows = sorted(totals.items(), key=lambda item: item[1], reverse=True)
        max_value = max((value for _, value in sorted_rows), default=Decimal('0'))
        accents = ('green', 'blue', 'yellow', 'red')
        return [
            {
                'label': label,
                'meta': ', '.join(sorted(meta[label])[:3]),
                'value': f'{format_dispatcher_number(value)} т',
                'percent': dispatcher_chart_percent(value, max_value) if max_value else 0,
                'accent': accents[index % len(accents)],
            }
            for index, (label, value) in enumerate(sorted_rows)
        ]

    material_rows = grouped_chart_rows(truck_rows, 'rock', 'target')
    unload_rows = grouped_chart_rows(truck_rows, 'target', 'rock')
    return {
        'metrics': [
            {'label': 'План', 'value': plan_value},
            {'label': 'Факт', 'value': fact_value},
            {'label': 'Самосвалы', 'value': f'{assigned} / {need}'},
            {'label': 'Работали', 'value': str(len(truck_rows))},
            {'label': 'Выведены', 'value': str(len(removed_truck_rows))},
        ],
        'charts': [
            {
                'type': 'bar',
                'title': 'План / факт',
                'rows': [
                    {'label': 'Факт смены', 'meta': 'выполнение комплекса', 'value': fact_value, 'percent': max(4, percent), 'accent': 'green' if status_key == 'normal' else 'yellow' if status_key == 'risk' else 'red'},
                    {'label': 'Прогноз', 'meta': 'ожидаемый итог', 'value': forecast_value, 'percent': min(100, max(4, percent + 8)), 'accent': 'blue'},
                    {'label': 'План', 'meta': 'сменное задание', 'value': plan_value, 'percent': 100, 'accent': 'green'},
                ],
            },
            {
                'type': 'donut-list',
                'title': 'Порода',
                'rows': material_rows,
            },
            {
                'type': 'donut-list',
                'title': 'Разгрузка',
                'rows': unload_rows,
            },
            {
                'type': 'truck-ledger',
                'title': 'Самосвалы',
                'rows': truck_rows,
            },
            {
                'type': 'bar',
                'title': 'Баланс',
                'rows': [
                    {'label': 'Назначено', 'meta': 'самосвалы в комплексе', 'value': str(assigned), 'percent': dispatcher_chart_percent(Decimal(assigned), Decimal(max(need, assigned, 1))), 'accent': 'green' if assigned >= need else 'yellow'},
                    {'label': 'Нужно', 'meta': 'расчетная потребность', 'value': str(need), 'percent': 100, 'accent': 'blue'},
                    {'label': 'Баланс', 'meta': action, 'value': f'+{balance}' if balance > 0 else str(balance), 'percent': dispatcher_chart_percent(Decimal(abs(balance)), Decimal(max(need, 1))), 'accent': 'red' if balance < 0 else 'green'},
                ],
            },
        ],
        'tables': [],
        'problem': problem,
        'truck_rows': truck_rows,
        'current_trucks': [row['truck'] for row in current_truck_rows],
        'removed_trucks': [row['truck'] for row in removed_truck_rows],
    }


def build_dispatcher_equipment_card(
    *,
    card_id,
    equipment=None,
    type_name='',
    number='',
    icon='',
    status='gray',
    status_label='',
    zone='',
    percent=0,
    employee=None,
    details=None,
    shift_report=None,
    category='equipment',
):
    card_details = []
    seen_labels = set()
    if equipment:
        type_name = type_name or equipment.equipment_type.name
        number = number or equipment_short_name(equipment)
        model = equipment.model
        add_dispatcher_detail(card_details, seen_labels, 'Гаражный N', equipment.garage_number)
        if equipment.vin:
            add_dispatcher_detail(card_details, seen_labels, 'VIN/серийный N', equipment.vin)
        add_dispatcher_detail(card_details, seen_labels, 'Модель', model.name if model else 'не указана')
        if model and model.payload_tons:
            add_dispatcher_detail(card_details, seen_labels, 'ГП, т', format_dispatcher_decimal(model.payload_tons))
        if model and model.body_volume_m3:
            add_dispatcher_detail(card_details, seen_labels, 'Кузов/ковш, м3', format_dispatcher_decimal(model.body_volume_m3))
    for row in details or []:
        add_dispatcher_detail(card_details, seen_labels, row.get('label'), row.get('value'))
    return {
        'id': str(card_id),
        'type': type_name,
        'label': number,
        'number': number,
        'icon': icon,
        'status_key': status,
        'status_label': status_label,
        'zone': zone,
        'percent': percent,
        'employee': dispatcher_employee_badge(employee),
        'details': card_details,
        'shift_report': shift_report or {},
        'category': category,
    }


def build_dispatcher_dashboard_context(*, dispatcher_shift, active_trips, pending_assignments, accepted_assignments, recent_completed_trips, open_shifts, open_mechanic_downtimes, trucks, excavators, recent_dispatcher_actions):
    active_trips_list = list(active_trips)
    pending_assignments_list = list(pending_assignments)
    accepted_assignments_list = list(accepted_assignments)
    recent_completed_trips_list = list(recent_completed_trips)
    open_downtime_list = list(open_mechanic_downtimes)
    trucks_list = list(trucks)
    excavators_list = list(excavators)
    shift_trip_queryset = Trip.objects.none()
    if dispatcher_shift:
        shift_trip_queryset = (
            Trip.objects
            .filter(created_at__gte=dispatcher_shift.opened_at)
            .select_related('truck', 'excavator', 'rock_type', 'dump_point')
            .order_by('-created_at')
        )
    shift_trips = list(shift_trip_queryset[:500])
    open_shift_by_equipment_id = {}
    for shift in open_shifts:
        if shift.equipment_id and shift.equipment_id not in open_shift_by_equipment_id:
            open_shift_by_equipment_id[shift.equipment_id] = shift
    downtime_by_equipment_id = {}
    for downtime in open_downtime_list:
        downtime_by_equipment_id.setdefault(downtime.equipment_id, downtime)
    active_trip_by_truck_id = {}
    active_trip_by_excavator_id = {}
    for trip in active_trips_list:
        active_trip_by_truck_id.setdefault(trip.truck_id, trip)
        active_trip_by_excavator_id.setdefault(trip.excavator_id, trip)
    latest_trip_by_equipment_id = {}
    for trip in recent_completed_trips_list:
        latest_trip_by_equipment_id.setdefault(trip.truck_id, trip)
        latest_trip_by_equipment_id.setdefault(trip.excavator_id, trip)
    assignment_by_truck_id = {}
    for assignment in accepted_assignments_list + pending_assignments_list:
        assignment_by_truck_id.setdefault(assignment.truck_id, assignment)
    equipment_cards = {}

    def status_label_for(status, label=''):
        if label:
            return label
        return {
            'green': 'работает',
            'gray': 'свободен',
            'yellow': 'ожидание',
            'red': 'простой',
            'danger': 'остановлен',
            'risk': 'риск',
            'normal': 'норма',
        }.get(status, status or '')

    def shift_details(equipment):
        shift = open_shift_by_equipment_id.get(equipment.id) if equipment else None
        if not shift:
            return []
        return [
            {'label': 'Смена', 'value': shift.get_shift_type_display()},
            {'label': 'Смена открыта', 'value': format_dispatcher_datetime(shift.opened_at)},
        ]

    completed_tons = Decimal('0')
    if dispatcher_shift:
        completed_tons = (
            Trip.objects
            .filter(status=TripStatus.COMPLETED, completed_at__gte=dispatcher_shift.opened_at)
            .aggregate(total=Sum('tonnage'))['total']
            or Decimal('0')
        )
    if dispatcher_shift and completed_tons == 0:
        completed_tons = (
            Trip.objects
            .filter(status=TripStatus.COMPLETED, completed_at__gte=dispatcher_shift.opened_at)
            .aggregate(total=Sum('volume_m3'))['total']
            or Decimal('0')
        )
    active_volume = sum((trip.tonnage or trip.volume_m3 or Decimal('0')) for trip in active_trips_list) if dispatcher_shift else Decimal('0')
    fact_tons = completed_tons + active_volume
    display_fact_tons = fact_tons
    forecast_tons = min(DISPATCHER_PLAN_TOTAL_TONS, display_fact_tons)
    completion_percent = int((display_fact_tons / DISPATCHER_PLAN_TOTAL_TONS) * 100) if DISPATCHER_PLAN_TOTAL_TONS else 0
    completion_percent = max(0, min(99, completion_percent))
    deficit_tons = forecast_tons - DISPATCHER_PLAN_TOTAL_TONS

    by_excavator = defaultdict(lambda: {
        'pending': 0,
        'accepted': 0,
        'active_trips': 0,
        'volume': Decimal('0'),
        'trucks': set(),
    })
    for assignment in pending_assignments_list:
        row = by_excavator[assignment.excavator_id]
        row['pending'] += 1
        row['trucks'].add(assignment.truck_id)
    for assignment in accepted_assignments_list:
        row = by_excavator[assignment.excavator_id]
        row['accepted'] += 1
        row['trucks'].add(assignment.truck_id)
    if dispatcher_shift:
        for trip in active_trips_list:
            row = by_excavator[trip.excavator_id]
            row['active_trips'] += 1
            row['volume'] += trip.tonnage or trip.volume_m3 or Decimal('0')
            row['trucks'].add(trip.truck_id)

    active_downtime_ids = {downtime.equipment_id for downtime in open_downtime_list}
    active_placement_ids = set(
        ExcavatorPlacement.objects
        .filter(zone=ExcavatorPlacement.Zone.ACTIVE, excavator__in=excavators_list)
        .values_list('excavator_id', flat=True)
    )
    active_excavator_ids = set(active_placement_ids)
    active_excavator_ids.update(assignment.excavator_id for assignment in pending_assignments_list + accepted_assignments_list if assignment.excavator_id)

    def garage_number_int(equipment):
        match = re.search(r'\d+', str(getattr(equipment, 'garage_number', '') or ''))
        return int(match.group(0)) if match else 9999

    excavator_by_id = {excavator.id: excavator for excavator in excavators_list}
    shown_excavators = sorted(
        [excavator_by_id[equipment_id] for equipment_id in active_excavator_ids if equipment_id in excavator_by_id],
        key=garage_number_int,
    )

    trips_by_excavator_id = defaultdict(list)
    for trip in shift_trips:
        if trip.excavator_id:
            trips_by_excavator_id[trip.excavator_id].append(trip)

    complex_cards = []
    for excavator in shown_excavators:
        index = garage_number_int(excavator)
        row = by_excavator[excavator.id]
        need = max(len(row['trucks']), row['accepted'] + row['pending'], 0)
        assigned = row['accepted'] + row['active_trips']
        plan = Decimal('0')
        fact = row['volume']
        percent = 0
        if plan > 0:
            percent = int((fact / plan) * 100)
            percent = max(0, min(100, percent))
        status_key = 'normal'
        status_label = 'СТАТУС: НОРМА'
        if excavator.id in active_downtime_ids:
            status_key = 'danger'
            status_label = 'СТАТУС: ОСТАНОВЛЕН'

        complex_trips = trips_by_excavator_id.get(excavator.id, [])
        current_horizon = ''
        current_block = ''
        rock_values = []
        unload_totals = defaultdict(Decimal)
        truck_rows = []
        latest_trip = None
        for trip in complex_trips:
            if not latest_trip or (trip.completed_at or trip.created_at) > (latest_trip.completed_at or latest_trip.created_at):
                latest_trip = trip
            if trip.loading_horizon and not current_horizon:
                current_horizon = f'Гор. {trip.loading_horizon}'
            if trip.loading_block and not current_block:
                current_block = f'Блок {trip.loading_block}'
            if trip.rock_type:
                rock_values.append(str(trip.rock_type))
            if trip.dump_point:
                unload_totals[str(trip.dump_point)] += dispatcher_trip_amount(trip)

        current_assignments = [assignment for assignment in accepted_assignments_list + pending_assignments_list if assignment.excavator_id == excavator.id]
        current_truck_ids = {assignment.truck_id for assignment in current_assignments}
        volume_by_truck = defaultdict(Decimal)
        target_by_truck = {}
        rock_by_truck = {}
        for trip in complex_trips:
            if not trip.truck_id:
                continue
            volume_by_truck[trip.truck_id] += dispatcher_trip_amount(trip)
            if trip.dump_point:
                target_by_truck[trip.truck_id] = str(trip.dump_point)
            if trip.rock_type:
                rock_by_truck[trip.truck_id] = str(trip.rock_type)
        max_truck_volume = max(volume_by_truck.values(), default=Decimal('0'))
        truck_by_id = {truck.id: truck for truck in trucks_list}
        for truck_id in sorted(current_truck_ids, key=lambda item: garage_number_int(truck_by_id.get(item)) if item in truck_by_id else 9999):
            truck = truck_by_id.get(truck_id)
            if not truck:
                continue
            truck_status = 'red' if truck_id in active_downtime_ids else 'yellow' if any(assignment.truck_id == truck_id and assignment.status == AssignmentStatus.PENDING for assignment in current_assignments) else 'gray'
            truck_volume = volume_by_truck.get(truck_id, Decimal('0'))
            truck_rows.append({
                'truck': dispatcher_truck_garage_number(truck, 0) or equipment_short_name(truck),
                'truck_id': truck_id,
                'state_key': 'current',
                'state': 'ожидает' if truck_status == 'yellow' else 'ремонт' if truck_status == 'red' else 'в составе',
                'target': target_by_truck.get(truck_id, ''),
                'rock': rock_by_truck.get(truck_id, ''),
                'value': f'{format_dispatcher_number(truck_volume)} т',
                'percent': dispatcher_chart_percent(truck_volume, max_truck_volume) if max_truck_volume else 0,
                'accent': truck_status,
                'label': dispatcher_truck_garage_number(truck, 0) or equipment_short_name(truck),
                'meta': '',
            })
        forecast = fact
        current_rock = rock_values[0] if rock_values else ''
        complex_cards.append({
            'id': f'K-{index}',
            'excavator_slot': index,
            'material': current_rock,
            'status_key': status_key,
            'status_label': status_label,
            'percent': percent,
            'excavator': excavator,
            'excavator_name': equipment_short_name(excavator),
            'excavator_icon': equipment_icon_key(excavator, 'red' if status_key == 'danger' else 'yellow' if status_key == 'risk' else 'green'),
            'truck_icon': 'img/equipment/truck-red.png' if status_key == 'danger' else 'img/equipment/truck-yellow.png' if status_key == 'risk' else 'img/equipment/truck-gray.png',
            'assigned': assigned,
            'need': need,
            'plan_tons': format_dispatcher_number(plan),
            'fact_tons': format_dispatcher_number(fact),
            'forecast_tons': format_dispatcher_number(forecast),
            'card_id': f'complex-K-{index}',
            'equipment_card_id': str(excavator.id) if excavator else '',
            'truck_rows': truck_rows,
            'current_horizon': current_horizon,
            'current_block': current_block,
            'current_rock': current_rock,
        })

    excavator_tiles = []
    for index, excavator in enumerate(excavators_list[:12], start=1):
        board_number = garage_number_int(excavator)
        status = 'green'
        label = 'работает'
        percent = 0
        if excavator.id in active_downtime_ids:
            status = 'red'
            label = 'ремонт'
            percent = 0
        elif excavator.id in active_excavator_ids:
            status = 'green'
            label = 'работает'
        excavator_tiles.append({
            'equipment': excavator,
            'name': equipment_short_name(excavator),
            'complex': f'K-{board_number}' if excavator.id in active_excavator_ids else '',
            'status': status,
            'label': label,
            'percent': percent,
            'icon': equipment_icon_key(excavator, status),
            'card_id': str(excavator.id) if excavator else '',
            'board_number': board_number,
        })

    excavator_garage_tiles = []
    inactive_excavator_tiles = [tile for tile in excavator_tiles if tile.get('equipment') and tile['equipment'].id not in active_excavator_ids]
    for index, tile in enumerate(inactive_excavator_tiles[:12], start=1):
        garage_tile = tile.copy()
        garage_tile['display_name'] = str(tile.get('board_number') or index)
        garage_tile['is_placeholder'] = False
        excavator_garage_tiles.append(garage_tile)
    while len(excavator_garage_tiles) < 12:
        index = len(excavator_garage_tiles) + 1
        excavator_garage_tiles.append({
            'equipment': None,
            'name': 'Будущий экскаватор',
            'status': 'empty',
            'label': 'резерв',
            'icon': 'img/equipment/excavator-gray.png',
            'board_number': index,
            'display_name': '',
            'percent': 0,
            'is_placeholder': True,
        })

    total_trucks = len(trucks_list)
    accepted_truck_ids = {assignment.truck_id for assignment in accepted_assignments_list}
    pending_truck_ids = {assignment.truck_id for assignment in pending_assignments_list}
    active_trip_truck_ids = {trip.truck_id for trip in active_trips_list}
    downtime_truck_ids = active_downtime_ids & {truck.id for truck in trucks_list}
    working_trucks = len(accepted_truck_ids | active_trip_truck_ids)
    waiting_trucks = len(pending_truck_ids - accepted_truck_ids)
    repair_trucks = len(downtime_truck_ids)
    loading_trucks = len(active_trip_truck_ids)

    balance_rows = []
    for card in complex_cards:
        balance = card['assigned'] - card['need']
        complex_report = dispatcher_complex_shift_report(card)
        current_trucks = complex_report.get('current_trucks') or []
        removed_trucks = complex_report.get('removed_trucks') or []
        current_truck_rows = [row for row in (complex_report.get('truck_rows') or []) if row.get('state_key') == 'current']
        truck_tiles = []
        for row in current_truck_rows:
            status = row.get('accent') if row.get('accent') in {'green', 'yellow', 'red', 'gray'} else 'gray'
            truck_tiles.append({
                'name': row.get('truck'),
                'status': status,
                'label': row.get('state') or 'в составе',
                'icon': f'img/equipment/truck-{status}.png',
                'percent': row.get('percent') or 0,
                'card_id': str(row.get('truck_id') or ''),
            })
        unload_totals = {}
        for row in current_truck_rows:
            target = row.get('target')
            tons = dispatcher_tons_from_label(row.get('value'))
            if target and tons > 0:
                unload_totals[target] = unload_totals.get(target, Decimal('0')) + tons
        total_unload_tons = sum(unload_totals.values(), Decimal('0'))
        unload_points = []
        for target, tons in unload_totals.items():
            if total_unload_tons <= 0:
                continue
            unload_points.append({
                'name': target,
                'percent': int((tons * Decimal('100') / total_unload_tons).quantize(Decimal('1'))),
            })
        rock_values = [row.get('rock') for row in current_truck_rows if row.get('rock')]
        current_rock = rock_values[0] if rock_values else (card.get('material') or '')
        if card['status_key'] == 'danger':
            attention_label = 'Комплекс остановлен, состав под контролем'
        elif card['status_key'] == 'risk':
            attention_label = 'Нужна проверка транспорта и маршрута'
        else:
            attention_label = 'Работает по плану'
        current_horizon, current_block = dispatcher_complex_location_parts(card)
        card.update({
            'balance': balance,
            'balance_label': f'+{balance}' if balance > 0 else str(balance),
            'balance_status': 'plus' if balance > 0 else 'minus' if balance < 0 else 'zero',
            'current_trucks': current_trucks,
            'removed_trucks': removed_trucks,
            'active_truck_tiles': truck_tiles,
            'truck_scale_class': 'truck-fill-1' if len(truck_tiles) <= 6 else 'truck-fill-2' if len(truck_tiles) <= 12 else 'truck-fill-3' if len(truck_tiles) <= 18 else 'truck-fill-4',
            'truck_column_count': 6,
            'truck_preview': current_trucks[:6],
            'truck_overflow': max(len(current_trucks) - 6, 0),
            'mobile_truck_overflow': max(len(current_trucks) - 16, 0),
            'current_face': dispatcher_complex_face_label(card),
            'current_horizon': current_horizon,
            'current_block': current_block,
            'current_rock': current_rock,
            'unload_points': unload_points,
            'attention_label': attention_label,
        })
        balance_rows.append({
            'complex': card['id'],
            'assigned': card['assigned'],
            'need': card['need'],
            'balance': balance,
            'balance_label': f'+{balance}' if balance > 0 else str(balance),
            'status': 'plus' if balance > 0 else 'minus' if balance < 0 else 'zero',
        })

    status_order = {'danger': 0, 'risk': 1, 'normal': 2}
    complex_zones = sorted(complex_cards, key=lambda card: (status_order.get(card['status_key'], 3), card['id']))
    while len(complex_zones) < 9:
        index = len(complex_zones) + 1
        complex_zones.append({
            'id': f'K-{index}',
            'is_empty': True,
            'status_key': 'empty',
            'status_label': 'СВОБОДНАЯ ЗОНА',
            'percent': 0,
            'material': '',
            'excavator_name': '',
            'excavator_icon': 'img/equipment/excavator-gray.png',
            'truck_icon': 'img/equipment/truck-gray.png',
            'assigned': 0,
            'need': 0,
            'plan_tons': '0',
            'fact_tons': '0',
            'forecast_tons': '0',
            'card_id': '',
            'equipment_card_id': '',
            'balance': 0,
            'balance_label': '0',
            'balance_status': 'zero',
            'current_trucks': [],
            'removed_trucks': [],
            'active_truck_tiles': [],
            'truck_scale_class': 'truck-fill-1',
            'truck_column_count': 1,
            'truck_preview': [],
            'truck_overflow': 0,
            'mobile_truck_overflow': 0,
            'current_face': '',
            'current_rock': '',
            'unload_points': [],
            'attention_label': '',
        })

    assigned_truck_ids = accepted_truck_ids | pending_truck_ids
    active_complex_truck_names = {
        str(truck.get('name'))
        for card in complex_cards
        for truck in card.get('active_truck_tiles', [])
        if truck.get('name')
    }
    truck_garage_tiles = []
    for index, truck in enumerate([truck for truck in trucks_list if truck.id not in assigned_truck_ids], start=1):
        if len(truck_garage_tiles) >= 52:
            break
        truck_number = dispatcher_truck_garage_number(truck, len(truck_garage_tiles) + 1)
        if truck_number is None:
            continue
        if str(truck_number) in active_complex_truck_names:
            continue
        truck_percent = 62 + ((index * 7) % 34)
        truck_garage_tiles.append({
            'equipment': truck,
            'name': truck_number,
            'status': 'red' if truck.id in downtime_truck_ids else 'gray',
            'label': 'ремонт' if truck.id in downtime_truck_ids else 'свободен',
            'icon': equipment_icon_key(truck, 'red' if truck.id in downtime_truck_ids else 'gray'),
            'percent': 0 if truck.id in downtime_truck_ids else truck_percent,
            'card_id': str(truck.id),
        })

    for tile in excavator_tiles:
        equipment = tile.get('equipment')
        if not equipment or not tile.get('card_id'):
            continue
        downtime = downtime_by_equipment_id.get(equipment.id)
        active_trip = active_trip_by_excavator_id.get(equipment.id)
        latest_trip = latest_trip_by_equipment_id.get(equipment.id)
        details = shift_details(equipment)
        details.extend([
            {'label': 'Комплекс', 'value': tile.get('complex')},
            {'label': 'Выработка', 'value': f'{tile.get("percent", 0)}%'},
        ])
        if active_trip:
            details.extend([
                {'label': 'Рейс', 'value': 'активный'},
                {'label': 'Самосвал рейса', 'value': equipment_short_name(active_trip.truck)},
                {'label': 'Разгрузка', 'value': active_trip.dump_point},
                {'label': 'Порода', 'value': active_trip.rock_type},
            ])
        if latest_trip:
            details.append({'label': 'Последний рейс', 'value': format_dispatcher_datetime(latest_trip.completed_at)})
        if downtime:
            details.extend([
                {'label': 'Простой', 'value': downtime.reason},
                {'label': 'С начала', 'value': format_dispatcher_datetime(downtime.started_at)},
            ])
        equipment_cards[str(tile['card_id'])] = build_dispatcher_equipment_card(
            card_id=tile['card_id'],
            equipment=equipment,
            number=tile.get('display_name') or tile.get('name'),
            icon=tile.get('icon'),
            status=tile.get('status'),
            status_label=status_label_for(tile.get('status'), tile.get('label')),
            zone=tile.get('complex') or 'гараж',
            percent=tile.get('percent', 0),
            employee=getattr(open_shift_by_equipment_id.get(equipment.id), 'employee', None),
            details=details,
            shift_report=dispatcher_shift_report_for_equipment(
                equipment,
                equipment_kind='Экскаватор',
                shift_trips=shift_trips,
            ),
        )

    for card in complex_cards:
        complex_report = dispatcher_complex_shift_report(card)
        details = [
            {'label': 'Экскаватор', 'value': card.get('excavator_name')},
            {'label': 'Текущий состав', 'value': ', '.join(complex_report.get('current_trucks') or [])},
            {'label': 'Выведены из состава', 'value': ', '.join(complex_report.get('removed_trucks') or [])},
            {'label': 'Порода', 'value': card.get('material')},
            {'label': 'Самосвалы', 'value': f'{card.get("assigned", 0)} / {card.get("need", 0)}'},
            {'label': 'Баланс транспорта', 'value': f'+{card["assigned"] - card["need"]}' if card['assigned'] > card['need'] else str(card['assigned'] - card['need'])},
            {'label': 'План смены', 'value': f'{card.get("plan_tons")} т'},
            {'label': 'Факт смены', 'value': f'{card.get("fact_tons")} т'},
            {'label': 'Прогноз', 'value': f'{card.get("forecast_tons")} т'},
        ]
        if card.get('status_key') == 'risk':
            details.append({'label': 'Причина', 'value': 'дефицит транспорта / риск выполнения'})
            details.append({'label': 'Действие', 'value': 'добавить самосвалы'})
        elif card.get('status_key') == 'danger':
            details.append({'label': 'Причина', 'value': 'комплекс остановлен'})
            details.append({'label': 'Действие', 'value': 'ремонт или расформирование'})
        else:
            details.append({'label': 'Причина', 'value': 'без отклонений'})
            details.append({'label': 'Действие', 'value': 'контроль нормы'})
        equipment_cards[str(card['card_id'])] = build_dispatcher_equipment_card(
            card_id=card['card_id'],
            type_name='Комплекс',
            number=card.get('id'),
            icon=card.get('excavator_icon'),
            status=card.get('status_key'),
            status_label=card.get('status_label'),
            zone=card.get('material'),
            percent=card.get('percent', 0),
            details=details,
            shift_report=complex_report,
            category='complex',
        )

    for complex_card in complex_cards:
        for tile in complex_card.get('active_truck_tiles', []):
            card_id = str(tile.get('card_id') or '')
            if not card_id or card_id in equipment_cards:
                continue
            equipment = truck_by_id.get(int(card_id)) if card_id.isdigit() else None
            status_label = status_label_for(tile.get('status'), tile.get('label'))
            equipment_cards[card_id] = build_dispatcher_equipment_card(
                card_id=card_id,
                type_name='Самосвал',
                equipment=equipment,
                number=tile.get('name'),
                icon=tile.get('icon'),
                status=tile.get('status'),
                status_label=status_label,
                zone=f'{complex_card.get("id")} / в составе',
                percent=tile.get('percent', 0),
                details=[
                    {'label': 'Гаражный N', 'value': tile.get('name')},
                    {'label': 'Комплекс', 'value': complex_card.get('id')},
                    {'label': 'Состояние', 'value': tile.get('label')},
                    {'label': 'Выработка', 'value': f'{tile.get("percent", 0)}%'},
                    {'label': 'Забой', 'value': complex_card.get('current_face')},
                    {'label': 'Порода', 'value': complex_card.get('current_rock')},
                    {'label': 'Разгрузки', 'value': ', '.join(point.get('name') for point in complex_card.get('unload_points', []) if point.get('name'))},
                ],
                shift_report=dispatcher_shift_report_for_equipment(
                    equipment,
                    equipment_kind='Самосвал',
                    shift_trips=shift_trips,
                ),
            )

    for tile in truck_garage_tiles:
        equipment = tile.get('equipment')
        status_label = status_label_for(tile.get('status'), tile.get('label'))
        details = [{'label': 'Выработка', 'value': f'{tile.get("percent", 0)}%'}]
        if equipment:
            downtime = downtime_by_equipment_id.get(equipment.id)
            active_trip = active_trip_by_truck_id.get(equipment.id)
            assignment = assignment_by_truck_id.get(equipment.id)
            latest_trip = latest_trip_by_equipment_id.get(equipment.id)
            details = shift_details(equipment) + details
            if assignment:
                details.extend([
                    {'label': 'Назначение', 'value': 'принято' if assignment.status == AssignmentStatus.ACCEPTED else 'ожидает'},
                    {'label': 'Экскаватор', 'value': equipment_short_name(assignment.excavator)},
                    {'label': 'Назначен', 'value': format_dispatcher_datetime(assignment.assigned_at)},
                ])
            if active_trip:
                details.extend([
                    {'label': 'Рейс', 'value': 'активный'},
                    {'label': 'Экскаватор рейса', 'value': equipment_short_name(active_trip.excavator)},
                    {'label': 'Разгрузка', 'value': active_trip.dump_point},
                    {'label': 'Порода', 'value': active_trip.rock_type},
                ])
            if latest_trip:
                details.append({'label': 'Последний рейс', 'value': format_dispatcher_datetime(latest_trip.completed_at)})
            if downtime:
                details.extend([
                    {'label': 'Простой', 'value': downtime.reason},
                    {'label': 'С начала', 'value': format_dispatcher_datetime(downtime.started_at)},
                ])
            card = build_dispatcher_equipment_card(
                card_id=tile['card_id'],
                equipment=equipment,
                number=tile.get('name'),
                icon=tile.get('icon'),
                status=tile.get('status'),
                status_label=status_label,
                zone='гараж',
                percent=tile.get('percent', 0),
                employee=getattr(open_shift_by_equipment_id.get(equipment.id), 'employee', None),
                details=details,
                shift_report=dispatcher_shift_report_for_equipment(
                    equipment,
                    equipment_kind='Самосвал',
                    shift_trips=shift_trips,
                ),
            )
        equipment_cards[str(tile['card_id'])] = card

    action_items = []
    pending_complex = next((card for card in complex_cards if card['status_key'] == 'risk'), None)
    if pending_complex:
        action_items.append({
            'priority': 1,
            'status': 'warning',
            'title': f'{pending_complex["id"]}: есть неподтвержденные назначения',
            'meta': 'Проверить принятие самосвалов водителями',
            'action': 'контроль назначений',
        })
    if open_downtime_list:
        first_downtime = open_downtime_list[0]
        action_items.append({'priority': 2, 'status': 'warning', 'title': f'{equipment_short_name(first_downtime.equipment)} ремонт', 'meta': str(first_downtime.reason), 'action': 'перераспределить транспорт'})
    action_items = action_items[:4]

    event_rows = []
    for downtime in open_downtime_list[:4]:
        event_rows.append({
            'time': timezone.localtime(downtime.started_at).strftime('%H:%M'),
            'object': equipment_short_name(downtime.equipment),
            'text': str(downtime.reason),
            'status': 'danger',
        })
    for action in list(recent_dispatcher_actions)[:5]:
        event_rows.append({
            'time': timezone.localtime(action.created_at).strftime('%H:%M'),
            'object': action.get_action_type_display()[:8],
            'text': action.target_summary,
            'status': 'warning',
        })
    ore_tons = Decimal('0')
    overburden_tons = Decimal('0')
    for trip in shift_trips:
        rock_name = str(trip.rock_type or '').lower()
        amount = dispatcher_trip_amount(trip)
        if 'вскрыш' in rock_name:
            overburden_tons += amount
        else:
            ore_tons += amount

    return {
        'dispatcher_kpis': {
            'plan_tons': format_dispatcher_number(DISPATCHER_PLAN_TOTAL_TONS),
            'fact_tons': format_dispatcher_number(display_fact_tons),
            'forecast_tons': format_dispatcher_number(forecast_tons),
            'deficit_tons': format_dispatcher_number(abs(deficit_tons)),
            'deficit_is_negative': deficit_tons < 0,
            'completion_percent': completion_percent,
            'ore_tons': format_dispatcher_number(ore_tons),
            'overburden_tons': format_dispatcher_number(overburden_tons),
            'excavators_working': sum(1 for tile in excavator_tiles if tile['status'] == 'green'),
            'excavators_total': len(excavator_tiles),
            'trucks_working': working_trucks,
            'trucks_total': total_trucks,
            'alerts': len([event for event in event_rows if event['status'] in {'danger', 'warning'}]),
        },
        'excavator_tiles': excavator_tiles,
        'excavator_garage_tiles': excavator_garage_tiles,
        'complex_cards': complex_cards,
        'complex_zones': complex_zones[:12],
        'truck_garage_tiles': truck_garage_tiles,
        'equipment_cards': equipment_cards,
        'truck_balance': {
            'total': total_trucks,
            'working': working_trucks,
            'waiting': waiting_trucks,
            'loading': loading_trucks,
            'repair': repair_trucks,
            'rows': balance_rows,
        },
        'action_items': action_items,
        'event_rows': event_rows[:7],
        'loss_reasons': [
            {'label': str(downtime.reason), 'value': 1, 'status': 'danger'}
            for downtime in open_downtime_list
        ],
        'forecast_points': [],
        'current_time': timezone.localtime().strftime('%H:%M'),
        'current_date': timezone.localdate().strftime('%d.%m.%Y'),
    }


def log_dispatcher_action(*, actor, action_type, target_summary, trip=None, shift=None, haul_assignment=None, reason=''):
    DispatcherActionLog.objects.create(
        actor=actor,
        action_type=action_type,
        trip=trip,
        shift=shift,
        haul_assignment=haul_assignment,
        target_summary=target_summary,
        reason=str(reason or '').strip(),
    )


def get_dispatcher_control_url(request):
    query_parts = []
    for key in DISPATCHER_FILTER_KEYS:
        value = request.POST.get(key, '').strip()
        if value == '':
            value = request.GET.get(key, '').strip()
        if value != '':
            query_parts.append(f'{key}={value}')
    base_url = reverse('dispatcher_control')
    if not query_parts:
        return base_url
    return f"{base_url}?{'&'.join(query_parts)}"


def dispatcher_access_from_request(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return None
    return (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(id=access_id, is_active=True, role__code__in={'dispatcher', 'admin', 'manager'})
        .first()
    )


def dispatcher_shift_required_response(access):
    if get_active_dispatcher_shift(access):
        return None
    return JsonResponse(
        {'ok': False, 'error': 'Смена горного диспетчера закрыта. Изменения на пульте недоступны.'},
        status=409,
    )


def dispatcher_shift_required_redirect(request, access, redirect_url):
    if get_active_dispatcher_shift(access):
        return None
    messages.error(request, 'Смена горного диспетчера закрыта. Изменения на пульте недоступны.')
    return redirect(redirect_url)


def dispatcher_json_payload(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return {}


def close_haul_assignments(queryset, now):
    assignments = list(queryset)
    for assignment in assignments:
        assignment.ended_at = now
    if assignments:
        HaulAssignment.objects.bulk_update(assignments, ['ended_at'])
    return assignments


@require_POST
def dispatcher_move_excavator_view(request):
    access = dispatcher_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к диспетчерскому пульту.'}, status=403)
    shift_error = dispatcher_shift_required_response(access)
    if shift_error:
        return shift_error
    payload = dispatcher_json_payload(request)
    excavator = get_object_or_404(
        Equipment.objects.select_related('equipment_type'),
        id=payload.get('excavator_id'),
        equipment_type__name__icontains='Экскаватор',
        is_active=True,
    )
    zone = payload.get('zone')
    if zone not in {ExcavatorPlacement.Zone.ACTIVE, ExcavatorPlacement.Zone.INACTIVE}:
        return JsonResponse({'ok': False, 'error': 'Некорректная зона экскаватора.'}, status=400)

    placement, _ = ExcavatorPlacement.objects.get_or_create(excavator=excavator)
    placement.zone = zone
    placement.changed_by = access.employee
    placement.save(update_fields=['zone', 'changed_by', 'changed_at'])

    if zone == ExcavatorPlacement.Zone.INACTIVE:
        now = timezone.now()
        closed = close_haul_assignments(
            HaulAssignment.objects
            .filter(excavator=excavator, ended_at__isnull=True)
            .exclude(status=AssignmentStatus.CANCELLED),
            now,
        )
        summary = f'{equipment_short_name(excavator)} возвращен в гараж, комплекс расформирован ({len(closed)} самосв.)'
    else:
        summary = f'{equipment_short_name(excavator)} переведен в активную смену'

    log_dispatcher_action(
        actor=access.employee,
        action_type=DispatcherActionType.CANCEL_ASSIGNMENT,
        target_summary=summary,
    )
    return JsonResponse({'ok': True})


@require_POST
def dispatcher_assign_truck_view(request):
    access = dispatcher_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к диспетчерскому пульту.'}, status=403)
    shift_error = dispatcher_shift_required_response(access)
    if shift_error:
        return shift_error
    payload = dispatcher_json_payload(request)
    action = payload.get('action')
    now = timezone.now()

    if action == 'release_complex':
        excavator = get_object_or_404(
            Equipment.objects.select_related('equipment_type'),
            id=payload.get('excavator_id'),
            equipment_type__name__icontains='Экскаватор',
            is_active=True,
        )
        closed = close_haul_assignments(
            HaulAssignment.objects
            .filter(excavator=excavator, ended_at__isnull=True)
            .exclude(status=AssignmentStatus.CANCELLED),
            now,
        )
        log_dispatcher_action(
            actor=access.employee,
            action_type=DispatcherActionType.CANCEL_ASSIGNMENT,
            target_summary=f'{equipment_short_name(excavator)}: самосвалы сброшены в гараж ({len(closed)})',
        )
        return JsonResponse({'ok': True, 'closed': len(closed)})

    truck = get_object_or_404(
        Equipment.objects.select_related('equipment_type'),
        id=payload.get('truck_id'),
        equipment_type__name__icontains='Самосвал',
        is_active=True,
    )
    active_assignments = (
        HaulAssignment.objects
        .filter(truck=truck, ended_at__isnull=True)
        .exclude(status=AssignmentStatus.CANCELLED)
    )
    if action == 'release':
        closed = close_haul_assignments(active_assignments, now)
        log_dispatcher_action(
            actor=access.employee,
            action_type=DispatcherActionType.CANCEL_ASSIGNMENT,
            target_summary=f'{equipment_short_name(truck)} снят с комплекса и возвращен в гараж',
        )
        return JsonResponse({'ok': True, 'closed': len(closed)})

    if action != 'assign':
        return JsonResponse({'ok': False, 'error': 'Некорректное действие с самосвалом.'}, status=400)

    excavator = get_object_or_404(
        Equipment.objects.select_related('equipment_type'),
        id=payload.get('excavator_id'),
        equipment_type__name__icontains='Экскаватор',
        is_active=True,
    )
    placement, _ = ExcavatorPlacement.objects.get_or_create(excavator=excavator)
    if placement.zone != ExcavatorPlacement.Zone.ACTIVE:
        placement.zone = ExcavatorPlacement.Zone.ACTIVE
        placement.changed_by = access.employee
        placement.save(update_fields=['zone', 'changed_by', 'changed_at'])

    close_haul_assignments(active_assignments, now)
    assignment = HaulAssignment.objects.create(
        truck=truck,
        excavator=excavator,
        assigned_by=access.employee,
        status=AssignmentStatus.PENDING,
    )
    log_dispatcher_action(
        actor=access.employee,
        action_type=DispatcherActionType.CANCEL_ASSIGNMENT,
        target_summary=f'{equipment_short_name(truck)} назначен под {equipment_short_name(excavator)}',
        haul_assignment=assignment,
    )
    return JsonResponse({'ok': True, 'assignment_id': assignment.id})


def excavator_work_view(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code != 'excavator_operator':
        return redirect('role_home')

    if request.method == 'POST':
        form = TripCreateForm(request.POST, excavator_operator=access.employee)
        if form.is_valid():
            form.create_trip(excavator_operator=access.employee)
            messages.success(request, 'Рейс создан. У водителя появился активный рейс.')
            return redirect('excavator_work')
    else:
        form = TripCreateForm(excavator_operator=access.employee)

    open_shift = (
        EmployeeShift.objects
        .filter(employee=access.employee, closed_at__isnull=True)
        .select_related('equipment')
        .order_by('-opened_at')
        .first()
    )
    current_excavator = open_shift.equipment if open_shift else None
    available_assignments = list(form.fields['assignment'].queryset)
    active_trips_queryset = (
        Trip.objects
        .filter(status=TripStatus.ACTIVE)
        .select_related('truck', 'excavator', 'rock_type', 'dump_point')
        .order_by('-created_at')
    )
    if current_excavator:
        active_trips_queryset = active_trips_queryset.filter(excavator=current_excavator)
    else:
        active_trips_queryset = active_trips_queryset.filter(excavator_operator=access.employee)
    active_trips = list(active_trips_queryset[:20])
    active_truck_ids = {trip.truck_id for trip in active_trips}
    for assignment in available_assignments:
        assignment.has_active_trip = assignment.truck_id in active_truck_ids

    return render(
        request,
        'trips/excavator_work.html',
        {
            'access': access,
            'form': form,
            'open_shift': open_shift,
            'current_excavator': current_excavator,
            'available_assignments': available_assignments,
            'active_trips': active_trips,
            'available_assignments_count': len(available_assignments),
            'active_trips_count': len(active_trips),
            'completed_today_count': Trip.objects.filter(excavator_operator=access.employee, status=TripStatus.COMPLETED, completed_at__date=timezone.localdate()).count(),
        },
    )


def dispatcher_control_view(request, *, access_override=None, enforce_dispatcher_access=True, dispatcher_header_override=None, context_overrides=None):
    if access_override is None:
        access_id = request.session.get('employee_access_id')
        if not access_id:
            return redirect('login')
        access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    else:
        access = access_override
    if not access:
        return redirect('role_home')
    if enforce_dispatcher_access and access.role.code not in {'dispatcher', 'admin', 'manager'}:
        return redirect('role_home')
    dispatcher_header = dispatcher_header_override or build_dispatcher_header_context(access, request)
    dispatcher_shift = dispatcher_header.get('active_shift')

    truck_id = request.GET.get('truck', '').strip()
    excavator_id = request.GET.get('excavator', '').strip()
    show_active_trips = request.GET.get('show_active_trips', '1') == '1'
    show_pending_assignments = request.GET.get('show_pending_assignments', '1') == '1'
    show_accepted_assignments = request.GET.get('show_accepted_assignments', '1') == '1'

    active_trips = (
        Trip.objects
        .filter(status=TripStatus.ACTIVE)
        .select_related('truck', 'excavator', 'rock_type', 'dump_point', 'excavator_operator')
        .order_by('created_at')
    )
    if dispatcher_shift:
        active_trips = active_trips.filter(created_at__gte=dispatcher_shift.opened_at)
    else:
        active_trips = active_trips.none()
    if truck_id:
        active_trips = active_trips.filter(truck_id=truck_id)
    if excavator_id:
        active_trips = active_trips.filter(excavator_id=excavator_id)
    if not show_active_trips:
        active_trips = active_trips.none()

    pending_assignments = (
        HaulAssignment.objects
        .filter(status=AssignmentStatus.PENDING, ended_at__isnull=True)
        .select_related('truck', 'excavator', 'assigned_by')
        .order_by('assigned_at')
    )
    if truck_id:
        pending_assignments = pending_assignments.filter(truck_id=truck_id)
    if excavator_id:
        pending_assignments = pending_assignments.filter(excavator_id=excavator_id)
    if not show_pending_assignments:
        pending_assignments = pending_assignments.none()

    accepted_assignments = (
        HaulAssignment.objects
        .filter(status=AssignmentStatus.ACCEPTED, ended_at__isnull=True)
        .select_related('truck', 'excavator', 'assigned_by')
        .order_by('-accepted_at')
    )
    if truck_id:
        accepted_assignments = accepted_assignments.filter(truck_id=truck_id)
    if excavator_id:
        accepted_assignments = accepted_assignments.filter(excavator_id=excavator_id)
    if not show_accepted_assignments:
        accepted_assignments = accepted_assignments.none()

    recent_completed_trips = (
        Trip.objects
        .filter(status=TripStatus.COMPLETED)
        .select_related('truck', 'excavator', 'rock_type', 'dump_point', 'driver')
        .order_by('-completed_at')
    )
    if dispatcher_shift:
        recent_completed_trips = recent_completed_trips.filter(completed_at__gte=dispatcher_shift.opened_at)
    else:
        recent_completed_trips = recent_completed_trips.none()
    if truck_id:
        recent_completed_trips = recent_completed_trips.filter(truck_id=truck_id)
    if excavator_id:
        recent_completed_trips = recent_completed_trips.filter(excavator_id=excavator_id)

    open_shifts = (
        EmployeeShift.objects
        .filter(closed_at__isnull=True)
        .select_related('employee', 'equipment', 'opened_by')
        .order_by('opened_at')
    )
    if dispatcher_shift:
        open_shifts = open_shifts.exclude(id=dispatcher_shift.id)
    if truck_id:
        open_shifts = open_shifts.filter(equipment_id=truck_id)
    if excavator_id:
        open_shifts = open_shifts.filter(equipment_id=excavator_id)
    open_shifts = list(open_shifts[:40])

    employee_ids = [shift.employee_id for shift in open_shifts]
    role_by_employee_id = {
        access.employee_id: access.role.name
        for access in (
            EmployeeAccess.objects
            .filter(employee_id__in=employee_ids, is_active=True, role__is_active=True)
            .select_related('role')
            .order_by('employee_id', 'id')
        )
    }
    for shift in open_shifts:
        shift.role_name = role_by_employee_id.get(shift.employee_id, '-')

    trucks = Equipment.objects.filter(equipment_type__name='Самосвал', is_active=True).order_by('garage_number')
    excavators = Equipment.objects.filter(equipment_type__name='Экскаватор', is_active=True).order_by('garage_number')
    recent_dispatcher_actions = (
        DispatcherActionLog.objects
        .select_related('actor')
        .order_by('-created_at')[:12]
    )
    open_mechanic_downtimes = (
        DowntimeEvent.objects
        .filter(ended_at__isnull=True)
        .select_related('equipment', 'reason', 'employee')
        .order_by('started_at')
    )
    downtime_equipment_ids = [equipment_id for equipment_id in [truck_id, excavator_id] if equipment_id]
    if downtime_equipment_ids:
        open_mechanic_downtimes = open_mechanic_downtimes.filter(equipment_id__in=downtime_equipment_ids)
    open_mechanic_downtimes_count = open_mechanic_downtimes.count()
    dispatcher_dashboard = build_dispatcher_dashboard_context(
        dispatcher_shift=dispatcher_shift,
        active_trips=active_trips,
        pending_assignments=pending_assignments,
        accepted_assignments=accepted_assignments,
        recent_completed_trips=recent_completed_trips,
        open_shifts=open_shifts,
        open_mechanic_downtimes=open_mechanic_downtimes[:30],
        trucks=trucks,
        excavators=excavators,
        recent_dispatcher_actions=recent_dispatcher_actions,
    )

    context = {
            'access': access,
            'dispatcher_header': dispatcher_header,
            'dispatcher_dashboard': dispatcher_dashboard,
            'dispatcher_page_title': 'Горный диспетчер',
            'dispatcher_compat_title': 'Диспетчерский пульт',
            'dispatcher_board_label': 'Горный диспетчер',
            'dispatcher_move_excavator_url': reverse('dispatcher_move_excavator'),
            'dispatcher_assign_truck_url': reverse('dispatcher_assign_truck'),
            'active_trips': active_trips,
            'pending_assignments': pending_assignments,
            'accepted_assignments': accepted_assignments[:30],
            'recent_completed_trips': recent_completed_trips[:30],
            'open_shifts': open_shifts,
            'open_mechanic_downtimes': open_mechanic_downtimes[:30],
            'active_trips_count': active_trips.count(),
            'pending_assignments_count': pending_assignments.count(),
            'accepted_assignments_count': accepted_assignments.count(),
            'open_shifts_count': len(open_shifts),
            'open_mechanic_downtimes_count': open_mechanic_downtimes_count,
            'trucks': trucks,
            'excavators': excavators,
            'recent_dispatcher_actions': recent_dispatcher_actions,
            'filters': {
                'truck': truck_id,
                'excavator': excavator_id,
                'show_active_trips': show_active_trips,
                'show_pending_assignments': show_pending_assignments,
                'show_accepted_assignments': show_accepted_assignments,
            },
            'dispatcher_filter_items': [
                ('truck', truck_id),
                ('excavator', excavator_id),
                ('show_active_trips', '1' if show_active_trips else '0'),
                ('show_pending_assignments', '1' if show_pending_assignments else '0'),
                ('show_accepted_assignments', '1' if show_accepted_assignments else '0'),
            ],
        }
    if context_overrides:
        context.update(context_overrides)

    return render(request, 'trips/dispatcher_control.html', context)


def dispatcher_toggle_shift_view(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')

    redirect_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('dispatcher_control')
    if request.method != 'POST':
        return redirect(redirect_url)

    action = request.POST.get('shift_action')
    if action == 'start':
        has_reauth_credentials = bool(request.POST.get('reauth_phone') and request.POST.get('reauth_access_code'))
        if get_session_device_kind(request) == 'shared' or not has_reauth_credentials:
            reauth_access, reauth_error = authenticate_dispatcher_shared_shift_start(request)
            if reauth_error:
                messages.error(request, reauth_error)
                return redirect(redirect_url)
            access = reauth_access
        if EmployeeShift.objects.filter(employee=access.employee, closed_at__isnull=True).exists():
            messages.warning(request, 'Смена горного диспетчера уже открыта.')
            return redirect(redirect_url)
        open_dispatcher_shift(access)
        messages.success(request, 'Смена горного диспетчера открыта.')
        return redirect(redirect_url)

    if action == 'end':
        shift = close_dispatcher_shift(access)
        if not shift:
            messages.warning(request, 'Открытая смена горного диспетчера не найдена.')
            return redirect(redirect_url)
        messages.success(request, 'Смена горного диспетчера завершена.')
        return redirect(redirect_url)

    messages.error(request, 'Неизвестное действие со сменой диспетчера.')
    return redirect(redirect_url)


def dispatcher_service_close_shift_view(request, shift_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')
    redirect_url = get_dispatcher_control_url(request)

    if request.method != 'POST':
        return redirect(redirect_url)
    shift_error = dispatcher_shift_required_redirect(request, access, redirect_url)
    if shift_error:
        return shift_error
    reason = request.POST.get('reason', '').strip()

    shift = (
        EmployeeShift.objects
        .select_related('employee', 'equipment')
        .filter(id=shift_id, closed_at__isnull=True)
        .first()
    )
    if not shift:
        messages.error(request, 'Открытая смена для служебного закрытия не найдена.')
        return redirect(redirect_url)

    shift.closed_at = timezone.now()
    shift.closed_by = access.employee
    shift.is_service_closed = True
    shift.save(update_fields=['closed_at', 'closed_by', 'is_service_closed'])
    log_dispatcher_action(
        actor=access.employee,
        action_type=DispatcherActionType.SERVICE_CLOSE_SHIFT,
        shift=shift,
        target_summary=f'{shift.employee} / {shift.equipment or "-"} / {shift.get_shift_type_display()}',
        reason=reason,
    )
    messages.success(request, f'Смена сотрудника {shift.employee} закрыта служебно.')
    return redirect(redirect_url)


def dispatcher_cancel_assignment_view(request, assignment_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')
    redirect_url = get_dispatcher_control_url(request)

    if request.method != 'POST':
        return redirect(redirect_url)
    shift_error = dispatcher_shift_required_redirect(request, access, redirect_url)
    if shift_error:
        return shift_error
    reason = request.POST.get('reason', '').strip()

    assignment = (
        HaulAssignment.objects
        .select_related('truck', 'excavator')
        .filter(id=assignment_id, ended_at__isnull=True, status__in={AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED})
        .first()
    )
    if not assignment:
        messages.error(request, 'Активное назначение для отмены не найдено.')
        return redirect(redirect_url)

    assignment.status = AssignmentStatus.CANCELLED
    assignment.ended_at = timezone.now()
    assignment.save(update_fields=['status', 'ended_at'])
    log_dispatcher_action(
        actor=access.employee,
        action_type=DispatcherActionType.CANCEL_ASSIGNMENT,
        haul_assignment=assignment,
        target_summary=f'{assignment.truck} под {assignment.excavator}',
        reason=reason,
    )
    messages.success(request, f'Назначение {assignment.truck} под {assignment.excavator} отменено.')
    return redirect(redirect_url)


def dispatcher_cancel_trip_view(request, trip_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')
    redirect_url = get_dispatcher_control_url(request)

    if request.method != 'POST':
        return redirect(redirect_url)
    shift_error = dispatcher_shift_required_redirect(request, access, redirect_url)
    if shift_error:
        return shift_error
    reason = request.POST.get('reason', '').strip()

    trip = (
        Trip.objects
        .select_related('truck', 'excavator')
        .filter(id=trip_id, status=TripStatus.ACTIVE)
        .first()
    )
    if not trip:
        messages.error(request, 'Активный рейс для отмены не найден.')
        return redirect(redirect_url)

    trip.status = TripStatus.CANCELLED
    trip.save(update_fields=['status'])
    log_dispatcher_action(
        actor=access.employee,
        action_type=DispatcherActionType.CANCEL_TRIP,
        trip=trip,
        target_summary=f'{trip.truck} -> {trip.dump_point}',
        reason=reason,
    )
    messages.success(request, f'Рейс {trip.truck} -> {trip.dump_point} отменен.')
    return redirect(redirect_url)


def dispatcher_complete_trip_view(request, trip_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')
    redirect_url = get_dispatcher_control_url(request)

    if request.method != 'POST':
        return redirect(redirect_url)
    shift_error = dispatcher_shift_required_redirect(request, access, redirect_url)
    if shift_error:
        return shift_error
    reason = request.POST.get('reason', '').strip()

    trip = (
        Trip.objects
        .select_related('truck', 'excavator', 'loading_shift')
        .filter(id=trip_id, status=TripStatus.ACTIVE)
        .first()
    )
    if not trip:
        messages.error(request, 'Активный рейс для служебного завершения не найден.')
        return redirect(redirect_url)

    unloading_shift = (
        EmployeeShift.objects
        .filter(equipment=trip.truck, closed_at__isnull=True)
        .order_by('-opened_at')
        .first()
    )
    if not unloading_shift:
        messages.error(request, 'Нельзя служебно завершить рейс: не найдена открытая смена по этому самосвалу.')
        return redirect(redirect_url)

    trip.status = TripStatus.COMPLETED
    trip.driver = unloading_shift.employee
    trip.completed_at = timezone.now()
    trip.unloading_shift = unloading_shift
    trip.is_carryover = bool(
        trip.loading_shift
        and unloading_shift
        and trip.loading_shift.shift_type != unloading_shift.shift_type
    )
    trip.save(update_fields=['status', 'driver', 'completed_at', 'unloading_shift', 'is_carryover'])
    log_dispatcher_action(
        actor=access.employee,
        action_type=DispatcherActionType.COMPLETE_TRIP,
        trip=trip,
        target_summary=f'{trip.truck} -> {trip.dump_point}',
        reason=reason,
    )
    messages.success(request, f'Рейс {trip.truck} завершен служебно.')
    return redirect(redirect_url)


def driver_complete_trip_view(request, trip_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code != 'driver':
        return redirect('role_home')
    if not hasattr(access.employee, 'driver_registration'):
        return redirect('driver_registration')
    unloading_shift = EmployeeShift.objects.filter(employee=access.employee, closed_at__isnull=True).order_by('-opened_at').first()
    if not unloading_shift or not unloading_shift.equipment:
        messages.error(request, 'Нельзя завершить рейс: открытая смена с самосвалом не найдена.')
        return redirect('driver_shift')
    trip = Trip.objects.filter(id=trip_id, truck=unloading_shift.equipment, status=TripStatus.ACTIVE).first()
    if trip and request.method == 'POST':
        trip.status = TripStatus.COMPLETED
        trip.driver = access.employee
        trip.completed_at = timezone.now()
        trip.unloading_shift = unloading_shift
        trip.is_carryover = bool(
            trip.loading_shift
            and unloading_shift
            and trip.loading_shift.shift_type != unloading_shift.shift_type
        )
        trip.save(update_fields=['status', 'driver', 'completed_at', 'unloading_shift', 'is_carryover'])
        messages.success(request, 'Рейс выполнен.')
    return redirect('driver_shift')
