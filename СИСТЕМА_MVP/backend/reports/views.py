from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib import messages
from django.db.models import Count, Sum
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import DumpPoint, Equipment, RockType
from trips.models import Trip, TripStatus
from users.models import EmployeeAccess

from .models import ReportTemplate, ReportType


def calculate_trip_deviation(trip):
    if trip.planned_volume_m3 is None and trip.volume_m3 is None:
        return ''
    return (trip.volume_m3 or 0) - (trip.planned_volume_m3 or 0)


def calculate_trip_plan_completion_percent(trip):
    if not trip.planned_volume_m3 or trip.volume_m3 is None:
        return ''
    return ((trip.volume_m3 / trip.planned_volume_m3) * Decimal('100')).quantize(Decimal('0.01'))


VOLUME_REPORT_COLUMNS = {
    'truck': ('Самосвал', lambda trip: str(trip.truck)),
    'excavator': ('Экскаватор', lambda trip: str(trip.excavator)),
    'rock_type': ('Порода', lambda trip: str(trip.rock_type)),
    'dump_point': ('Точка разгрузки', lambda trip: str(trip.dump_point)),
    'planned_volume_m3': ('План, м3', lambda trip: trip.planned_volume_m3 or ''),
    'volume_m3': ('Объем, м3', lambda trip: trip.volume_m3 or ''),
    'deviation_m3': ('Отклонение, м3', calculate_trip_deviation),
    'plan_completion_percent': ('Выполнение, %', calculate_trip_plan_completion_percent),
    'tonnage': ('Тоннаж', lambda trip: trip.tonnage or ''),
    'loading_horizon': ('Горизонт', lambda trip: trip.loading_horizon),
    'loading_block': ('Блок', lambda trip: trip.loading_block),
    'transport_distance_km': ('Плечо, км', lambda trip: trip.transport_distance_km or ''),
    'downtime_text': ('Простои', lambda trip: trip.downtime_text),
    'note': ('Примечание', lambda trip: trip.note),
    'loading_shift': ('Смена загрузки', lambda trip: trip.loading_shift.get_shift_type_display() if trip.loading_shift else ''),
    'unloading_shift': ('Смена разгрузки', lambda trip: trip.unloading_shift.get_shift_type_display() if trip.unloading_shift else ''),
    'is_carryover': ('Переходящий рейс', lambda trip: 'Да' if trip.is_carryover else 'Нет'),
    'completed_at': ('Выполнен', lambda trip: trip.completed_at.strftime('%d.%m.%Y %H:%M') if trip.completed_at else ''),
}

DEFAULT_VOLUME_REPORT_COLUMNS = [
    'truck',
    'excavator',
    'rock_type',
    'dump_point',
    'planned_volume_m3',
    'volume_m3',
    'deviation_m3',
    'plan_completion_percent',
    'tonnage',
    'loading_horizon',
    'loading_block',
    'transport_distance_km',
    'loading_shift',
    'unloading_shift',
    'is_carryover',
    'completed_at',
]

REPORT_TEMPLATE_FILTER_FIELDS = [
    'date_from',
    'date_to',
    'loading_shift_type',
    'unloading_shift_type',
    'carryover',
    'truck',
    'excavator',
    'rock_type',
    'dump_point',
]

VOLUME_REPORT_FILTER_LABELS = {
    'date_from': 'Дата с',
    'date_to': 'Дата по',
    'loading_shift_type': 'Смена загрузки',
    'unloading_shift_type': 'Смена разгрузки',
    'carryover': 'Переходящий рейс',
    'truck': 'Самосвал',
    'excavator': 'Экскаватор',
    'rock_type': 'Порода/груз',
    'dump_point': 'Точка разгрузки',
}

SHIFT_TYPE_LABELS = {
    'day': 'Дневная',
    'night': 'Ночная',
}

UNLOADING_WAITING_REASONS = {
    'ожидание разгрузки ккд': 'ККД',
    'ожидание разгрузки скдр': 'СКДР',
}

CARRYOVER_LABELS = {
    'yes': 'Да',
    'no': 'Нет',
}

VOLUME_REPORT_GROUPS = {
    'truck': ('Самосвал', lambda trip: str(trip.truck)),
    'excavator': ('Экскаватор', lambda trip: str(trip.excavator)),
    'rock_type': ('Порода/груз', lambda trip: str(trip.rock_type)),
    'dump_point': ('Точка разгрузки', lambda trip: str(trip.dump_point)),
    'completed_hour': (
        'Час выполнения рейса',
        lambda trip: timezone.localtime(trip.completed_at).strftime('%H:00') if trip.completed_at else 'не задано',
    ),
    'loading_shift': ('Смена загрузки', lambda trip: trip.loading_shift.get_shift_type_display() if trip.loading_shift else 'не задано'),
    'unloading_shift': ('Смена разгрузки', lambda trip: trip.unloading_shift.get_shift_type_display() if trip.unloading_shift else 'не задано'),
}


def get_object_label(model, object_id):
    if not str(object_id).isdigit():
        return str(object_id)
    instance = model.objects.filter(id=object_id).first()
    return str(instance) if instance else str(object_id)


def get_filter_display_value(key, value):
    value = str(value or '').strip()
    if not value:
        return ''
    if key in {'loading_shift_type', 'unloading_shift_type'}:
        return SHIFT_TYPE_LABELS.get(value, value)
    if key == 'carryover':
        return CARRYOVER_LABELS.get(value, value)
    if key == 'truck':
        return get_object_label(Equipment, value)
    if key == 'excavator':
        return get_object_label(Equipment, value)
    if key == 'rock_type':
        return get_object_label(RockType, value)
    if key == 'dump_point':
        return get_object_label(DumpPoint, value)
    return value


def get_active_filter_rows(filters):
    rows = []
    for key in REPORT_TEMPLATE_FILTER_FIELDS:
        display_value = get_filter_display_value(key, filters.get(key, ''))
        if display_value:
            rows.append([VOLUME_REPORT_FILTER_LABELS[key], display_value])
    return rows


def get_group_by_label(group_by):
    if group_by in VOLUME_REPORT_GROUPS:
        return VOLUME_REPORT_GROUPS[group_by][0]
    return 'Без группировки'


def get_volume_report_templates():
    return ReportTemplate.objects.filter(is_active=True).order_by('name')


def get_selected_report_template(request):
    template_id = request.GET.get('template', '').strip()
    if not template_id:
        return None
    return ReportTemplate.objects.filter(id=template_id, is_active=True).first()


def get_selected_columns(template):
    if not template:
        return DEFAULT_VOLUME_REPORT_COLUMNS
    columns = [column for column in template.columns if column in VOLUME_REPORT_COLUMNS]
    return columns or DEFAULT_VOLUME_REPORT_COLUMNS


def get_template_column_labels(template):
    if not template:
        return {}
    return template.column_labels or {}


def get_column_label(column, column_labels=None):
    default_label = VOLUME_REPORT_COLUMNS[column][0]
    if not column_labels:
        return default_label
    custom_label = str(column_labels.get(column, '')).strip()
    return custom_label or default_label


def report_template_column_options(selected_columns, column_labels=None):
    return [
        {
            'code': code,
            'label': label,
            'custom_label': str((column_labels or {}).get(code, '')).strip(),
            'checked': code in selected_columns,
        }
        for code, (label, _getter) in VOLUME_REPORT_COLUMNS.items()
    ]


def report_template_group_options(selected_group_by):
    return [
        {
            'code': code,
            'label': label,
            'selected': code == selected_group_by,
        }
        for code, (label, _getter) in VOLUME_REPORT_GROUPS.items()
    ]


def get_template_filters(template):
    if not template:
        return {}
    return template.filters or {}


def get_effective_filter_value(request, template, key):
    request_value = request.GET.get(key, '').strip()
    if request_value:
        return request_value
    return str(get_template_filters(template).get(key, '')).strip()


def get_selected_group_by(request, template):
    request_value = request.GET.get('group_by')
    if request_value is not None:
        request_value = request_value.strip()
        if request_value == 'none':
            return ''
        return request_value if request_value in VOLUME_REPORT_GROUPS else ''
    if template and template.group_by in VOLUME_REPORT_GROUPS:
        return template.group_by
    return ''


def get_volume_report_filters(request, template=None):
    return {
        key: get_effective_filter_value(request, template, key)
        for key in REPORT_TEMPLATE_FILTER_FIELDS
    }


def parse_filter_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None


def get_downtime_report_filters(request):
    return {
        'date_from': request.GET.get('date_from', '').strip(),
        'date_to': request.GET.get('date_to', '').strip(),
        'status': request.GET.get('status', '').strip(),
        'critical': request.GET.get('critical', '').strip(),
        'equipment': request.GET.get('equipment', '').strip(),
        'reason': request.GET.get('reason', '').strip(),
        'query_string': request.GET.urlencode(),
    }


def apply_downtime_report_filters(queryset, filters):
    date_from = parse_filter_date(filters.get('date_from'))
    date_to = parse_filter_date(filters.get('date_to'))
    status = filters.get('status')
    critical = filters.get('critical')
    equipment = filters.get('equipment', '')
    reason = filters.get('reason', '')

    if date_from:
        queryset = queryset.filter(started_at__date__gte=date_from)
    if date_to:
        queryset = queryset.filter(started_at__date__lte=date_to)
    if status == 'open':
        queryset = queryset.filter(ended_at__isnull=True)
    elif status == 'closed':
        queryset = queryset.filter(ended_at__isnull=False)
    if critical == 'yes':
        queryset = queryset.filter(reason__is_critical=True)
    elif critical == 'no':
        queryset = queryset.filter(reason__is_critical=False)
    if equipment.isdigit():
        queryset = queryset.filter(equipment_id=equipment)
    if reason.isdigit():
        queryset = queryset.filter(reason_id=reason)
    return queryset


def downtime_duration_hours(event):
    end_time = event.ended_at or timezone.now()
    seconds = max((end_time - event.started_at).total_seconds(), 0)
    return (Decimal(str(seconds)) / Decimal('3600')).quantize(Decimal('0.01'))


def downtime_status_label(event):
    return 'Открыт' if event.ended_at is None else 'Закрыт'


def downtime_report_rows(events):
    rows = []
    for event in events:
        rows.append({
            'started_at': event.started_at,
            'ended_at': event.ended_at,
            'equipment': event.equipment,
            'reason': event.reason,
            'is_critical': event.reason.is_critical,
            'status': downtime_status_label(event),
            'duration_hours': downtime_duration_hours(event),
            'employee': event.employee,
            'comment': event.comment,
        })
    return rows


def downtime_summary_by(rows, key):
    summary = {}
    for row in rows:
        name = str(row[key]) if row[key] else '-'
        if name not in summary:
            summary[name] = {
                'name': name,
                'count': 0,
                'open_count': 0,
                'critical_count': 0,
                'duration_hours': Decimal('0'),
            }
        summary[name]['count'] += 1
        if row['ended_at'] is None:
            summary[name]['open_count'] += 1
        if row['is_critical']:
            summary[name]['critical_count'] += 1
        summary[name]['duration_hours'] += row['duration_hours']
    result = []
    for item in summary.values():
        item['duration_hours'] = item['duration_hours'].quantize(Decimal('0.01'))
        result.append(item)
    return sorted(result, key=lambda item: (item['open_count'], item['duration_hours'], item['count']), reverse=True)


def downtime_daily_summary(rows):
    summary = {}
    for row in rows:
        day = timezone.localtime(row['started_at']).date()
        key = day.isoformat()
        if key not in summary:
            summary[key] = {
                'date': day,
                'count': 0,
                'open_count': 0,
                'critical_count': 0,
                'duration_hours': Decimal('0'),
            }
        summary[key]['count'] += 1
        if row['ended_at'] is None:
            summary[key]['open_count'] += 1
        if row['is_critical']:
            summary[key]['critical_count'] += 1
        summary[key]['duration_hours'] += row['duration_hours']
    result = []
    for item in summary.values():
        item['duration_hours'] = item['duration_hours'].quantize(Decimal('0.01'))
        result.append(item)
    return sorted(result, key=lambda item: item['date'], reverse=True)


def get_unloading_waiting_destination(reason):
    normalized = str(reason or '').strip().lower()
    for marker, destination in UNLOADING_WAITING_REASONS.items():
        if marker in normalized:
            return destination
    return ''


def downtime_unloading_waiting_summary(rows):
    summary = {}
    for row in rows:
        destination = get_unloading_waiting_destination(row['reason'])
        if not destination:
            continue
        if destination not in summary:
            summary[destination] = {
                'destination': destination,
                'reason': f'Ожидание разгрузки {destination}',
                'event_count': 0,
                'open_count': 0,
                'equipment_names': set(),
                'duration_hours': Decimal('0'),
            }
        summary[destination]['event_count'] += 1
        if row['ended_at'] is None:
            summary[destination]['open_count'] += 1
        summary[destination]['equipment_names'].add(str(row['equipment']))
        summary[destination]['duration_hours'] += row['duration_hours']

    result = []
    for item in summary.values():
        duration_hours = item['duration_hours'].quantize(Decimal('0.01'))
        duration_minutes = (item['duration_hours'] * Decimal('60')).quantize(Decimal('0.01'))
        event_count = item['event_count']
        item['duration_hours'] = duration_hours
        item['duration_minutes'] = duration_minutes
        item['equipment_count'] = len(item['equipment_names'])
        item['avg_minutes_per_event'] = (
            duration_minutes / Decimal(event_count)
        ).quantize(Decimal('0.01')) if event_count else Decimal('0')
        del item['equipment_names']
        result.append(item)
    return sorted(result, key=lambda item: item['destination'])


def build_report_table(trips, selected_columns, column_labels=None):
    headers = [get_column_label(column, column_labels) for column in selected_columns]
    rows = [
        [VOLUME_REPORT_COLUMNS[column][1](trip) for column in selected_columns]
        for trip in trips
    ]
    return headers, rows


def build_grouped_report_table(trips, group_by, column_labels=None):
    group_label, group_getter = VOLUME_REPORT_GROUPS[group_by]
    if group_by in VOLUME_REPORT_COLUMNS:
        group_label = get_column_label(group_by, column_labels)
    volume_label = get_column_label('volume_m3', column_labels)
    tonnage_label = get_column_label('tonnage', column_labels)
    grouped = defaultdict(lambda: {
        'volume_m3': 0,
        'tonnage': 0,
        'trip_count': 0,
    })

    for trip in trips:
        key = group_getter(trip) or 'не задано'
        grouped[key]['volume_m3'] += trip.volume_m3 or 0
        grouped[key]['tonnage'] += trip.tonnage or 0
        grouped[key]['trip_count'] += 1

    headers = [group_label, volume_label, tonnage_label, 'Рейсы']
    rows = [
        [group_name, values['volume_m3'], values['tonnage'], values['trip_count']]
        for group_name, values in grouped.items()
    ]
    rows.sort(key=lambda row: str(row[0]))
    return headers, rows


def is_number(value):
    return isinstance(value, (int, float)) or hasattr(value, 'as_tuple')


def append_total_row(sheet, headers, rows, row_index, total_column_indexes):
    if not rows:
        return row_index

    total_row = [''] * len(headers)
    total_row[0] = 'Итого'
    for column_index in total_column_indexes:
        total = sum(row[column_index] for row in rows if is_number(row[column_index]))
        total_row[column_index] = total

    sheet.append(total_row)
    for cell in sheet[row_index]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='EAF4FF')
        cell.border = Border(top=Side(style='thin', color='9DBAD5'))
    return row_index + 1


def style_volume_report_workbook(sheet, table_header_row, total_columns):
    header_fill = PatternFill('solid', fgColor='12232E')
    header_font = Font(color='FFFFFF', bold=True)
    thin_border = Border(bottom=Side(style='thin', color='D9E2EC'))

    for cell in sheet[table_header_row]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    for row in sheet.iter_rows(min_row=table_header_row + 1):
        for cell in row:
            cell.alignment = Alignment(vertical='top', wrap_text=True)
            cell.border = thin_border
            if is_number(cell.value):
                cell.number_format = '#,##0.00'

    for row in range(1, table_header_row):
        sheet.row_dimensions[row].height = 22

    sheet.freeze_panes = f'A{table_header_row + 1}'
    sheet.auto_filter.ref = f'A{table_header_row}:{get_column_letter(total_columns)}{sheet.max_row}'

    for column_index in range(1, total_columns + 1):
        column_letter = get_column_letter(column_index)
        max_length = 0
        for cell in sheet[column_letter]:
            if cell.value is None:
                continue
            max_length = max(max_length, len(str(cell.value)))
        sheet.column_dimensions[column_letter].width = min(max(max_length + 2, 14), 42)


def write_volume_report_excel(sheet, headers, rows, selected_template, selected_group_by, filters, access, total_column_indexes):
    total_columns = max(len(headers), 4)
    generated_at = timezone.localtime(timezone.now()).strftime('%d.%m.%Y %H:%M')
    template_name = selected_template.name if selected_template else 'Стандартный отчет'
    employee_name = access.employee.full_name if access and access.employee else ''
    active_filter_rows = get_active_filter_rows(filters)

    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=total_columns)
    sheet['A1'] = 'Отчет по объемам'
    sheet['A1'].font = Font(size=16, bold=True, color='12232E')
    sheet['A1'].alignment = Alignment(horizontal='left')

    metadata_rows = [
        ['Сформирован', generated_at],
        ['Шаблон', template_name],
        ['Группировка', get_group_by_label(selected_group_by)],
    ]
    if employee_name:
        metadata_rows.append(['Сформировал', employee_name])

    for metadata_row in metadata_rows:
        sheet.append(metadata_row)

    sheet.append([])
    sheet.append(['Активные фильтры'])
    sheet.cell(row=sheet.max_row, column=1).font = Font(bold=True)
    if active_filter_rows:
        for filter_row in active_filter_rows:
            sheet.append(filter_row)
    else:
        sheet.append(['Нет'])

    sheet.append([])
    table_header_row = sheet.max_row + 1
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    append_total_row(sheet, headers, rows, sheet.max_row + 1, total_column_indexes)

    for row in range(2, table_header_row):
        sheet.cell(row=row, column=1).font = Font(bold=True)

    style_volume_report_workbook(sheet, table_header_row, total_columns)


def apply_volume_report_filters(queryset, filters):
    date_from = parse_filter_date(filters.get('date_from'))
    date_to = parse_filter_date(filters.get('date_to'))
    loading_shift_type = filters.get('loading_shift_type', '')
    unloading_shift_type = filters.get('unloading_shift_type', '')
    carryover = filters.get('carryover', '')
    truck = filters.get('truck', '')
    excavator = filters.get('excavator', '')
    rock_type = filters.get('rock_type', '')
    dump_point = filters.get('dump_point', '')

    if date_from:
        queryset = queryset.filter(completed_at__date__gte=date_from)
    if date_to:
        queryset = queryset.filter(completed_at__date__lte=date_to)
    if loading_shift_type:
        queryset = queryset.filter(loading_shift__shift_type=loading_shift_type)
    if unloading_shift_type:
        queryset = queryset.filter(unloading_shift__shift_type=unloading_shift_type)
    if carryover == 'yes':
        queryset = queryset.filter(is_carryover=True)
    elif carryover == 'no':
        queryset = queryset.filter(is_carryover=False)
    if truck.isdigit():
        queryset = queryset.filter(truck_id=truck)
    if excavator.isdigit():
        queryset = queryset.filter(excavator_id=excavator)
    if rock_type.isdigit():
        queryset = queryset.filter(rock_type_id=rock_type)
    if dump_point.isdigit():
        queryset = queryset.filter(dump_point_id=dump_point)
    return queryset


def volume_report_filter_context(request, selected_template):
    filters = get_volume_report_filters(request, selected_template)
    return {
        **filters,
        'template': request.GET.get('template', '').strip(),
        'group_by': request.GET.get('group_by', '').strip(),
        'query_string': request.GET.urlencode(),
    }


def report_filter_choices():
    return {
        'trucks': Equipment.objects.filter(is_active=True, equipment_type__name__icontains='Самосвал').order_by('garage_number'),
        'excavators': Equipment.objects.filter(is_active=True, equipment_type__name__icontains='Экскаватор').order_by('garage_number'),
        'rock_types': RockType.objects.filter(is_active=True).order_by('name'),
        'dump_points': DumpPoint.objects.filter(is_active=True).order_by('name'),
    }


def get_detail_total_column_indexes(selected_columns):
    total_columns = {'planned_volume_m3', 'volume_m3', 'deviation_m3', 'tonnage'}
    return [
        index
        for index, column in enumerate(selected_columns)
        if column in total_columns
    ]


def get_grouped_total_column_indexes():
    return [1, 2, 3]


def volume_report_view(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'dispatcher', 'admin', 'manager'}:
        return redirect('role_home')

    trips = Trip.objects.filter(status=TripStatus.COMPLETED).select_related(
        'truck',
        'excavator',
        'rock_type',
        'dump_point',
        'loading_shift',
        'unloading_shift',
    ).order_by('-completed_at')
    selected_template = get_selected_report_template(request)
    filters = get_volume_report_filters(request, selected_template)
    trips = apply_volume_report_filters(trips, filters)
    selected_columns = get_selected_columns(selected_template)
    column_labels = get_template_column_labels(selected_template)
    selected_group_by = get_selected_group_by(request, selected_template)
    if selected_group_by:
        headers, rows = build_grouped_report_table(trips, selected_group_by, column_labels)
    else:
        headers, rows = build_report_table(trips[:100], selected_columns, column_labels)
    total_volume = trips.aggregate(total=Sum('volume_m3'))['total'] or 0
    total_tonnage = trips.aggregate(total=Sum('tonnage'))['total'] or 0
    return render(
        request,
        'reports/volume_report.html',
        {
            'access': access,
            'report_headers': headers,
            'report_rows': rows,
            'total_volume': total_volume,
            'total_tonnage': total_tonnage,
            'filters': volume_report_filter_context(request, selected_template),
            'report_templates': get_volume_report_templates(),
            'selected_template': selected_template,
            'selected_group_by': selected_group_by,
            'group_options': report_template_group_options(selected_group_by),
            **report_filter_choices(),
        },
    )


def volume_report_export_view(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'dispatcher', 'admin', 'manager'}:
        return redirect('role_home')

    trips = Trip.objects.filter(status=TripStatus.COMPLETED).select_related(
        'truck',
        'excavator',
        'rock_type',
        'dump_point',
        'loading_shift',
        'unloading_shift',
    ).order_by('-completed_at')
    selected_template = get_selected_report_template(request)
    filters = get_volume_report_filters(request, selected_template)
    trips = apply_volume_report_filters(trips, filters)
    selected_columns = get_selected_columns(selected_template)
    column_labels = get_template_column_labels(selected_template)
    selected_group_by = get_selected_group_by(request, selected_template)
    if selected_group_by:
        headers, rows = build_grouped_report_table(trips, selected_group_by, column_labels)
        total_column_indexes = get_grouped_total_column_indexes()
    else:
        headers, rows = build_report_table(trips, selected_columns, column_labels)
        total_column_indexes = get_detail_total_column_indexes(selected_columns)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Объемы'
    write_volume_report_excel(sheet, headers, rows, selected_template, selected_group_by, filters, access, total_column_indexes)

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = 'attachment; filename=\"volume_report.xlsx\"'
    workbook.save(response)
    return response


def downtime_report_context(request):
    filters = get_downtime_report_filters(request)
    selected_single_date = filters['date_from'] if filters['date_from'] and filters['date_from'] == filters['date_to'] else ''
    events = (
        DowntimeEvent.objects
        .select_related('equipment', 'equipment__equipment_type', 'reason', 'employee')
        .order_by('-started_at')
    )
    events = apply_downtime_report_filters(events, filters)
    all_rows = downtime_report_rows(events)
    visible_rows = all_rows[:200]
    total_duration_hours = sum((row['duration_hours'] for row in all_rows), Decimal('0')).quantize(Decimal('0.01'))
    open_count = sum(1 for row in all_rows if row['ended_at'] is None)
    closed_count = len(all_rows) - open_count
    critical_count = sum(1 for row in all_rows if row['is_critical'])
    unloading_waiting_summary = downtime_unloading_waiting_summary(all_rows)
    unloading_waiting_total_minutes = sum(
        (item['duration_minutes'] for item in unloading_waiting_summary),
        Decimal('0'),
    ).quantize(Decimal('0.01'))
    unloading_waiting_event_count = sum(item['event_count'] for item in unloading_waiting_summary)
    unloading_waiting_equipment_count = sum(item['equipment_count'] for item in unloading_waiting_summary)
    return {
        'filters': filters,
        'selected_single_date': selected_single_date,
        'rows': visible_rows,
        'export_rows': all_rows,
        'visible_count': len(visible_rows),
        'total_count': len(all_rows),
        'open_count': open_count,
        'closed_count': closed_count,
        'critical_count': critical_count,
        'total_duration_hours': total_duration_hours,
        'daily_summary': downtime_daily_summary(all_rows),
        'equipment_summary': downtime_summary_by(all_rows, 'equipment'),
        'reason_summary': downtime_summary_by(all_rows, 'reason'),
        'unloading_waiting_summary': unloading_waiting_summary,
        'unloading_waiting_total_minutes': unloading_waiting_total_minutes,
        'unloading_waiting_event_count': unloading_waiting_event_count,
        'unloading_waiting_equipment_count': unloading_waiting_equipment_count,
        'unloading_waiting_reconciliation': UNLOADING_WAITING_EXCEL_RECONCILIATION,
        'equipment_choices': Equipment.objects.filter(is_active=True).order_by('equipment_type__name', 'garage_number'),
        'reason_choices': DowntimeReason.objects.filter(is_active=True).order_by('name'),
        'status_choices': [
            {'code': '', 'label': 'Все'},
            {'code': 'open', 'label': 'Открытые'},
            {'code': 'closed', 'label': 'Закрытые'},
        ],
        'critical_choices': [
            {'code': '', 'label': 'Все'},
            {'code': 'yes', 'label': 'Только критические'},
            {'code': 'no', 'label': 'Только обычные'},
        ],
    }


def downtime_report_view(request):
    access = get_reports_access(request, {'admin', 'dispatcher', 'manager', 'mechanic'})
    if not access:
        return redirect('login' if not request.session.get('employee_access_id') else 'role_home')
    return render(
        request,
        'reports/downtime_report.html',
        {
            'access': access,
            **downtime_report_context(request),
        },
    )


def downtime_report_export_view(request):
    access = get_reports_access(request, {'admin', 'dispatcher', 'manager', 'mechanic'})
    if not access:
        return redirect('login' if not request.session.get('employee_access_id') else 'role_home')
    context = downtime_report_context(request)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Простои'
    sheet['A1'] = 'Отчет по простоям техники'
    sheet['A1'].font = Font(bold=True, size=14)
    sheet['A2'] = f"Сформировал: {access.employee.full_name}"
    sheet['A3'] = f"Дата формирования: {timezone.localtime(timezone.now()):%d.%m.%Y %H:%M}"
    summary_rows = [
        ['Показатель', 'Значение'],
        ['Всего событий', context['total_count']],
        ['Открытые', context['open_count']],
        ['Закрытые', context['closed_count']],
        ['Критические', context['critical_count']],
        ['Длительность, ч', context['total_duration_hours']],
    ]
    for row_index, row in enumerate(summary_rows, start=5):
        for column_index, value in enumerate(row, start=1):
            cell = sheet.cell(row_index, column_index, value)
            if row_index == 5:
                cell.font = Font(bold=True, color='FFFFFF')
                cell.fill = PatternFill('solid', fgColor='17232E')

    def write_downtime_summary(title, rows, start_row, start_col):
        sheet.cell(start_row, start_col, title)
        sheet.cell(start_row, start_col).font = Font(bold=True, size=12)
        headers = ['Название', 'События', 'Открытые', 'Критические', 'Длительность, ч']
        for offset, header in enumerate(headers):
            cell = sheet.cell(start_row + 1, start_col + offset, header)
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='17232E')
        for row_index, item in enumerate(rows[:10], start=start_row + 2):
            values = [item['name'], item['count'], item['open_count'], item['critical_count'], item['duration_hours']]
            for offset, value in enumerate(values):
                sheet.cell(row_index, start_col + offset, value)

    write_downtime_summary('Сводка по технике', context['equipment_summary'], 5, 4)
    write_downtime_summary('Сводка по причинам', context['reason_summary'], 5, 10)
    write_downtime_summary(
        'Сводка по датам',
        [
            {
                **item,
                'name': item['date'].strftime('%d.%m.%Y'),
            }
            for item in context['daily_summary']
        ],
        12,
        4,
    )

    headers = ['Начало', 'Окончание', 'Статус', 'Техника', 'Причина', 'Критичность', 'Длительность, ч', 'Кто зафиксировал', 'Комментарий']
    table_start = 25
    for column_index, header in enumerate(headers, start=1):
        cell = sheet.cell(table_start, column_index, header)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='17232E')
        cell.alignment = Alignment(wrap_text=True, vertical='top')
    for row_index, row in enumerate(context['export_rows'], start=table_start + 1):
        values = [
            timezone.localtime(row['started_at']).strftime('%d.%m.%Y %H:%M'),
            timezone.localtime(row['ended_at']).strftime('%d.%m.%Y %H:%M') if row['ended_at'] else 'открыт',
            row['status'],
            str(row['equipment']),
            str(row['reason']),
            'Критический' if row['is_critical'] else 'Обычный',
            row['duration_hours'],
            str(row['employee']) if row['employee'] else '-',
            row['comment'] or '-',
        ]
        for column_index, value in enumerate(values, start=1):
            cell = sheet.cell(row_index, column_index, value)
            cell.alignment = Alignment(wrap_text=True, vertical='top')
    for column_index in range(1, len(headers) + 1):
        sheet.column_dimensions[get_column_letter(column_index)].width = 18
    sheet.column_dimensions['I'].width = 36
    sheet.auto_filter.ref = f'A{table_start}:I{table_start + max(len(context["export_rows"]), 1)}'
    sheet.freeze_panes = f'A{table_start + 1}'

    unloading_sheet = workbook.create_sheet('ОР ККД СКДР')
    unloading_sheet['A1'] = 'Сверка ожидания разгрузки ККД/СКДР'
    unloading_sheet['A1'].font = Font(bold=True, size=14)
    unloading_sheet['A2'] = 'Источник старой формы: ОР ККД СКДР март.xlsx'
    unloading_sheet['A3'] = 'Принцип MVP: ожидание разгрузки фиксируется как событие простоя самосвала.'
    reconciliation_headers = ['Старый блок', 'Старые поля', 'Блок MVP', 'Статус', 'Примечание']
    for column_index, header in enumerate(reconciliation_headers, start=1):
        cell = unloading_sheet.cell(5, column_index, header)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='17232E')
        cell.alignment = Alignment(wrap_text=True, vertical='top')
    for row_index, item in enumerate(context['unloading_waiting_reconciliation'], start=6):
        values = [item['old_block'], item['old_fields'], item['mvp_block'], item['status'], item['note']]
        for column_index, value in enumerate(values, start=1):
            cell = unloading_sheet.cell(row_index, column_index, value)
            cell.alignment = Alignment(wrap_text=True, vertical='top')

    summary_start = 6 + len(context['unloading_waiting_reconciliation']) + 2
    unloading_sheet.cell(summary_start, 1, 'Сводка по выбранным фильтрам')
    unloading_sheet.cell(summary_start, 1).font = Font(bold=True, size=12)
    summary_headers = ['Направление', 'Причина', 'События', 'Открытые', 'Техника', 'Минуты', 'Часы', 'Среднее мин/событие']
    for column_index, header in enumerate(summary_headers, start=1):
        cell = unloading_sheet.cell(summary_start + 1, column_index, header)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='17232E')
        cell.alignment = Alignment(wrap_text=True, vertical='top')
    for row_index, item in enumerate(context['unloading_waiting_summary'], start=summary_start + 2):
        values = [
            item['destination'],
            item['reason'],
            item['event_count'],
            item['open_count'],
            item['equipment_count'],
            item['duration_minutes'],
            item['duration_hours'],
            item['avg_minutes_per_event'],
        ]
        for column_index, value in enumerate(values, start=1):
            unloading_sheet.cell(row_index, column_index, value)
    if not context['unloading_waiting_summary']:
        unloading_sheet.cell(summary_start + 2, 1, 'По выбранным фильтрам ожидания разгрузки ККД/СКДР нет.')

    for column_index in range(1, len(summary_headers) + 1):
        unloading_sheet.column_dimensions[get_column_letter(column_index)].width = 22
    unloading_sheet.column_dimensions['B'].width = 30
    unloading_sheet.column_dimensions['E'].width = 40
    unloading_sheet.freeze_panes = 'A6'

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = 'attachment; filename=\"downtime_report.xlsx\"'
    workbook.save(response)
    return response


def get_reports_access(request, allowed_roles):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return None
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in allowed_roles:
        return None
    return access


PILOT_REPORT_CHECKLIST_SECTIONS = [
    {
        'title': '1. Витрина руководства',
        'items': [
            {
                'text': 'Открыть управленческую витрину и проверить суточные KPI, выполнение плана, день/ночь и динамику за 7 дней.',
                'url': '/reports/management/',
                'url_text': 'Открыть витрину',
            },
            {
                'text': 'Выгрузить витрину в Excel и сверить листы: Сводка, Динамика 7 дней, День ночь.',
                'url': '/reports/management/export/',
                'url_text': 'Выгрузить Excel',
            },
        ],
    },
    {
        'title': '2. Диспетчерский контроль',
        'items': [
            {
                'text': 'Проверить активные рейсы, неподтвержденные назначения, открытые механические простои и последние завершенные рейсы.',
                'url': '/dispatcher/control/',
                'url_text': 'Открыть пульт',
            },
        ],
    },
    {
        'title': '3. Отчет по объемам',
        'items': [
            {
                'text': 'Проверить фильтры, группировки, переходящие рейсы и выбранный шаблон отчета.',
                'url': '/reports/volume/',
                'url_text': 'Открыть отчет',
            },
            {
                'text': 'Проверить Excel-выгрузку отчета по объемам с теми же фильтрами и столбцами.',
                'url': '/reports/volume/export/',
                'url_text': 'Выгрузить Excel',
            },
            {
                'text': 'Проверить конструктор шаблонов: состав столбцов, подписи, фильтры и группировку.',
                'url': '/reports/templates/',
                'url_text': 'Открыть конструктор',
            },
        ],
    },
    {
        'title': '4. Суточный отчет заказчику',
        'items': [
            {
                'text': 'Открыть суточный отчет и проверить смены, объемы, месячную сводку и механические простои.',
                'url': '/reports/customer-daily/',
                'url_text': 'Открыть отчет',
            },
            {
                'text': 'Выгрузить суточный отчет в Excel и сверить структуру с текущей формой заказчика.',
                'url': '/reports/customer-daily/export/',
                'url_text': 'Выгрузить Excel',
            },
        ],
    },
    {
        'title': '5. Механические простои',
        'items': [
            {
                'text': 'Проверить отчет по простоям: открытые/закрытые события, критичность, причины, техника и длительность.',
                'url': '/reports/downtimes/',
                'url_text': 'Открыть отчет',
            },
            {
                'text': 'Выгрузить отчет по простоям в Excel и проверить, что полная история попадает в файл.',
                'url': '/reports/downtimes/export/',
                'url_text': 'Выгрузить Excel',
            },
        ],
    },
    {
        'title': '6. Вопросы перед пилотом',
        'items': [
            {
                'text': 'Сверить, какие текущие Excel-формы диспетчерская обязана сдавать каждый день.',
                'url': '',
                'url_text': '',
            },
            {
                'text': 'Отметить расхождения между системой и действующими отчетами: недостающие столбцы, названия, формулы и порядок строк.',
                'url': '',
                'url_text': '',
            },
            {
                'text': 'Решить, что исправляем до пилота, а что фиксируем как ограничение первой версии.',
                'url': '',
                'url_text': '',
            },
        ],
    },
]


PILOT_LAUNCH_SCENARIO_STEPS = [
    {
        'title': '1. Вход и карта интерфейсов',
        'role': 'Диспетчер / руководство',
        'access_code': '5000 / 6000',
        'url': '/interfaces/',
        'checks': [
            'Открыть карту интерфейсов.',
            'Проверить ссылки на рабочие экраны и отчеты.',
            'Убедиться, что вход по коду открывает интерфейс по роли.',
        ],
        'expected_result': 'Пользователь быстро находит нужный экран и не ищет адреса вручную.',
    },
    {
        'title': '2. Расстановка техники',
        'role': 'Горный мастер',
        'access_code': '4000',
        'url': '/master/assignments/',
        'checks': [
            'Выбрать экскаватор.',
            'Выбрать самосвал.',
            'Создать назначение.',
            'Проверить, что назначение видно водителю и диспетчеру.',
        ],
        'expected_result': 'Рабочая связка экскаватор - самосвал появляется в системе.',
    },
    {
        'title': '3. Работа водителя',
        'role': 'Водитель самосвала',
        'access_code': '2000',
        'url': '/driver/shift/',
        'checks': [
            'Открыть смену.',
            'Проверить стартовые показатели техники.',
            'Подтвердить назначение кнопкой Принял.',
            'Завершить активный рейс кнопкой Выполнено.',
        ],
        'expected_result': 'Водитель выполняет минимум действий, а рейс попадает в отчет.',
    },
    {
        'title': '4. Создание рейса',
        'role': 'Машинист экскаватора',
        'access_code': '3000',
        'url': '/excavator/shift/',
        'checks': [
            'Выбрать назначенный самосвал.',
            'Выбрать породу или груз.',
            'Выбрать точку разгрузки.',
            'Создать рейс.',
        ],
        'expected_result': 'Система считает объем и тоннаж, рейс становится активным у водителя и диспетчера.',
    },
    {
        'title': '5. Диспетчерский контроль',
        'role': 'Диспетчер',
        'access_code': '5000',
        'url': '/dispatcher/control/',
        'checks': [
            'Проверить активные рейсы.',
            'Проверить назначения без подтверждения.',
            'Проверить незакрытые смены.',
            'Проверить открытые простои и журнал действий.',
        ],
        'expected_result': 'Диспетчер видит текущую смену в процессе, а не собирает ее вручную после факта.',
    },
    {
        'title': '6. Простои техники',
        'role': 'Механик',
        'access_code': '7000',
        'url': '/mechanic/downtimes/',
        'checks': [
            'Проверить открытые простои.',
            'Создать или закрыть простой.',
            'Открыть отчет по простоям.',
            'Проверить сводку ОР ККД/СКДР и Excel-выгрузку.',
        ],
        'expected_result': 'Простои фиксируются как события техники и попадают в отчетность.',
    },
    {
        'title': '7. Отчеты и витрина',
        'role': 'Диспетчер / руководство',
        'access_code': '5000 / 6000',
        'url': '/reports/pilot-checklist/',
        'checks': [
            'Открыть отчет по объемам и группировку по часу.',
            'Открыть суточный отчет заказчику.',
            'Открыть витрину руководства.',
            'Выгрузить основные Excel-файлы.',
        ],
        'expected_result': 'Отчеты формируются из данных системы и готовы к сверке со старыми Excel-формами.',
    },
]


PILOT_FEEDBACK_QUESTIONS = [
    'Достаточно ли группировки по часу вместо старой почасовой матрицы?',
    'Кто должен фиксировать ожидание разгрузки ККД/СКДР?',
    'Как считать среднее мин на 1 а/с?',
    'Как связать укрупненные грузы с точными породами заказчика?',
    'Какие действия в интерфейсах лишние или неудобные?',
    'Какие данные невозможно вводить стабильно в карьере?',
]


PILOT_REPORT_EXCEL_COVERAGE = [
    {
        'file': 'Отчет_Коппер. Рисорсез_Март.xlsx',
        'purpose': 'Суточный отчет заказчику',
        'coverage': 'частично покрыт',
        'system_link': '/reports/customer-daily/',
        'next_step': 'Сверить форму, порядок блоков и обязательные итоговые строки.',
    },
    {
        'file': 'почасовой Март.xlsx',
        'purpose': 'Почасовой отчет диспетчерской',
        'coverage': 'частично покрыт группировкой по часу',
        'system_link': '/reports/volume/?group_by=completed_hour',
        'next_step': 'Проверить с диспетчерской, нужна ли точная старая почасовая матрица после первого пилота.',
    },
    {
        'file': 'ОР ККД СКДР март.xlsx',
        'purpose': 'Ожидание разгрузки ККД/СКДР',
        'coverage': 'частично покрыт отдельной сводкой простоев',
        'system_link': '/reports/downtimes/',
        'next_step': 'На пилоте определить, кто фиксирует ожидание разгрузки: водитель, диспетчер или автоматический контроль рейса.',
    },
    {
        'file': 'СВОД Простоев на ККД Март.xlsx',
        'purpose': 'Свод простоев и невыполненного объема',
        'coverage': 'частично покрыт',
        'system_link': '/reports/downtimes/',
        'next_step': 'Решить, нужен ли расчет невыполненного объема в MVP.',
    },
    {
        'file': 'Работа экс Март (1).xlsx',
        'purpose': 'Работа экскаваторов',
        'coverage': 'частично покрыто',
        'system_link': '/reports/management/',
        'next_step': 'Проверить группировку по экскаваторам, объемам, рейсам и плечу.',
    },
    {
        'file': 'Работа экс Март ПЕРЕГОНЫ ПРИЧИНЫ.xlsx',
        'purpose': 'Перегоны и смена фронта работ',
        'coverage': 'не покрыт',
        'system_link': '',
        'next_step': 'Нужен модуль статусов и перегонов экскаватора.',
    },
    {
        'file': 'КИП/КТГ и КИО/КТГ',
        'purpose': 'Показатели использования и технической готовности',
        'coverage': 'не покрыт',
        'system_link': '',
        'next_step': 'Определить формулы, источники рабочего времени, простоев и ремонтов.',
    },
    {
        'file': 'График ТО.xlsx',
        'purpose': 'Планирование технического обслуживания',
        'coverage': 'не покрыт',
        'system_link': '',
        'next_step': 'Отнести к развитию механического модуля после первого пилота.',
    },
    {
        'file': 'удельный_веса_руд_и_пород_Малмыжского_местородения.xlsx',
        'purpose': 'Справочник пород, плотностей и коэффициентов разрыхления',
        'coverage': 'структурно сверено',
        'system_link': '/admin/references/rocktype/',
        'next_step': 'На пилоте уточнить соответствие укрупненных рабочих названий точным породам заказчика.',
    },
]


CUSTOMER_DAILY_EXCEL_RECONCILIATION = [
    {
        'old_block': 'Заголовок отчета заказчику',
        'old_fields': 'Дата, заказчик, подрядчик, дневная и ночная смена',
        'mvp_block': 'Шапка страницы и Excel-лист "Суточный отчет"',
        'status': 'покрыто',
        'note': 'Название отчета и выбранная дата выводятся на экран и в Excel-выгрузку.',
    },
    {
        'old_block': 'Работа выемочного оборудования',
        'old_fields': 'Тип грунта, экскаватор, план, факт, горизонт, блок, место разгрузки, плечо, простои, примечание',
        'mvp_block': 'Таблицы I смена и II смена',
        'status': 'покрыто частично',
        'note': 'Базовые поля уже есть. Перед пилотом нужно сверить точные названия пород и порядок строк с действующей формой.',
    },
    {
        'old_block': 'Суточная сводка',
        'old_fields': 'План, факт, отклонение, день, ночь, сутки',
        'mvp_block': 'Блок "Суточная сводка"',
        'status': 'покрыто',
        'note': 'План/факт/отклонение считаются по рейсам и плановым заданиям, внесенным в систему.',
    },
    {
        'old_block': 'С начала месяца',
        'old_fields': 'План с начала месяца, факт с начала месяца, отклонение',
        'mvp_block': 'Блок "С начала месяца"',
        'status': 'покрыто частично',
        'note': 'Факт берется из рейсов с начала месяца. Отдельную модель месячного плана нужно уточнить перед промышленным запуском.',
    },
    {
        'old_block': 'Итоги по породам',
        'old_fields': 'Горная масса, добыча руды, сульфидная, переходная, вскрыша, окисленная, рыхлая, скальная, ПСП, ППСП',
        'mvp_block': 'Блок "Итоги по породам"',
        'status': 'покрыто частично',
        'note': 'Система группирует по справочнику пород. Нужно сверить справочник пород с действующими названиями заказчика.',
    },
    {
        'old_block': 'Средневзвешенное плечо',
        'old_fields': 'Среднее плечо по породам, сменам, суткам и с начала месяца',
        'mvp_block': 'Плечо в строках сменных таблиц',
        'status': 'требует доработки',
        'note': 'В рейсах хранится плечо, но отдельный расчет средневзвешенного плеча пока не вынесен в сводку.',
    },
    {
        'old_block': 'Расчет выполненных работ по самосвалам',
        'old_fields': '№ самосвала, рейсы, км, объем, итог, м3*км, простои',
        'mvp_block': 'Отчет по объемам и конструктор отчетов',
        'status': 'покрыто частично',
        'note': 'Рейсы, объем, тоннаж, самосвал и плечо есть в данных. М3*км и группировку точно как в старой расчетной вкладке нужно добавить отдельным шаблоном.',
    },
    {
        'old_block': 'Простои и примечания',
        'old_fields': 'Простои в сменных строках и комментарии по технике',
        'mvp_block': 'Простои в рейсах и механические простои',
        'status': 'покрыто частично',
        'note': 'Механические простои уже выделены отдельно. Производственные простои экскаватора нужно довести через справочник статусов экскаватора.',
    },
]


UNLOADING_WAITING_EXCEL_RECONCILIATION = [
    {
        'old_block': 'Дневные листы по датам',
        'old_fields': 'Дата, смена, направление ККД/СКДР',
        'mvp_block': 'Фильтры отчета по простоям и сводка ожидания разгрузки',
        'status': 'покрыто частично',
        'note': 'Дата берется по началу события простоя. Разделение по сменам нужно уточнить после решения, кто фиксирует событие.',
    },
    {
        'old_block': 'Строки по самосвалам',
        'old_fields': 'Номер самосвала, причина ожидания, минуты ожидания',
        'mvp_block': 'События простоев по технике',
        'status': 'покрыто',
        'note': 'Каждое ожидание разгрузки хранится как событие простоя самосвала с началом, окончанием, причиной и длительностью.',
    },
    {
        'old_block': 'Итоги по направлению',
        'old_fields': 'Количество машин/событий, всего минут, среднее минут на 1 а/с',
        'mvp_block': 'Сводка ОР ККД/СКДР',
        'status': 'покрыто частично',
        'note': 'Система считает события, технику, минуты, часы и среднее минут на событие. Формулировку среднего нужно сверить с диспетчерской.',
    },
    {
        'old_block': 'Форма Excel для передачи',
        'old_fields': 'Отдельная таблица ожидания разгрузки ККД/СКДР',
        'mvp_block': 'Лист "ОР ККД СКДР" в Excel-выгрузке простоев',
        'status': 'покрыто частично',
        'note': 'Выгрузка уже содержит отдельный лист сверки. Точную старую раскладку по листам дат можно делать после пилотной проверки.',
    },
]


def pilot_report_checklist_view(request):
    access = get_reports_access(request, {'admin', 'dispatcher', 'manager'})
    if not access:
        return redirect('login' if not request.session.get('employee_access_id') else 'role_home')

    return render(
        request,
        'reports/pilot_checklist.html',
        {
            'access': access,
            'sections': PILOT_REPORT_CHECKLIST_SECTIONS,
            'excel_coverage': PILOT_REPORT_EXCEL_COVERAGE,
            'progress_stage': '9 из 10',
            'progress_percent': 99,
            'remaining_stages': 1,
        },
    )


def pilot_launch_scenario_view(request):
    access = get_reports_access(request, {'admin', 'dispatcher', 'manager'})
    if not access:
        return redirect('login' if not request.session.get('employee_access_id') else 'role_home')

    return render(
        request,
        'reports/pilot_scenario.html',
        {
            'access': access,
            'steps': PILOT_LAUNCH_SCENARIO_STEPS,
            'feedback_questions': PILOT_FEEDBACK_QUESTIONS,
            'progress_stage': '9 из 10',
            'progress_percent': 99,
            'remaining_stages': 1,
        },
    )


def report_template_builder_view(request):
    access = get_reports_access(request, {'admin', 'dispatcher'})
    if not access:
        return redirect('login' if not request.session.get('employee_access_id') else 'role_home')

    edit_template = None
    edit_template_id = request.GET.get('edit', '').strip()
    if edit_template_id:
        edit_template = ReportTemplate.objects.filter(id=edit_template_id).first()

    if request.method == 'POST':
        template_id = request.POST.get('template_id', '').strip()
        name = request.POST.get('name', '').strip()
        columns = [
            column
            for column in request.POST.getlist('columns')
            if column in VOLUME_REPORT_COLUMNS
        ]
        column_labels = {}
        for column in VOLUME_REPORT_COLUMNS:
            custom_label = request.POST.get(f'column_label_{column}', '').strip()
            default_label = VOLUME_REPORT_COLUMNS[column][0]
            if custom_label and custom_label != default_label:
                column_labels[column] = custom_label
        filters = {
            key: request.POST.get(key, '').strip()
            for key in REPORT_TEMPLATE_FILTER_FIELDS
            if request.POST.get(key, '').strip()
        }
        group_by = request.POST.get('group_by', '').strip()
        if group_by not in VOLUME_REPORT_GROUPS:
            group_by = ''
        is_active = request.POST.get('is_active') == 'on'

        template = None
        if template_id:
            template = ReportTemplate.objects.filter(id=template_id).first()
            if not template:
                messages.error(request, 'Шаблон отчета не найден.')
                return redirect('report_template_builder')

        duplicate_query = ReportTemplate.objects.filter(name=name)
        if template:
            duplicate_query = duplicate_query.exclude(id=template.id)

        if not name:
            messages.error(request, 'Укажи название шаблона отчета.')
        elif not columns:
            messages.error(request, 'Выбери хотя бы один столбец отчета.')
        elif duplicate_query.exists():
            messages.error(request, 'Шаблон с таким названием уже существует.')
        else:
            if not template:
                template = ReportTemplate(created_by=access.employee)
            template.name = name
            template.report_type = ReportType.SHIFT_VOLUME
            template.columns = columns
            template.column_labels = column_labels
            template.filters = filters
            template.group_by = group_by
            template.is_active = is_active
            template.updated_by = access.employee
            template.save()
            messages.success(request, 'Шаблон отчета сохранен.')
            return redirect('report_template_builder')

        edit_template = template

    selected_columns = get_selected_columns(edit_template) if edit_template else DEFAULT_VOLUME_REPORT_COLUMNS
    column_labels = get_template_column_labels(edit_template)
    template_filters = get_template_filters(edit_template)
    selected_group_by = edit_template.group_by if edit_template and edit_template.group_by in VOLUME_REPORT_GROUPS else ''
    return render(
        request,
        'reports/report_template_builder.html',
        {
            'access': access,
            'report_templates': ReportTemplate.objects.order_by('name'),
            'edit_template': edit_template,
            'column_options': report_template_column_options(selected_columns, column_labels),
            'template_filters': template_filters,
            'selected_group_by': selected_group_by,
            'group_options': report_template_group_options(selected_group_by),
            **report_filter_choices(),
        },
    )


def parse_customer_report_date(request):
    date_value = request.GET.get('date', '').strip()
    if date_value:
        try:
            return datetime.strptime(date_value, '%Y-%m-%d').date()
        except ValueError:
            pass

    latest_trip = (
        Trip.objects
        .filter(status=TripStatus.COMPLETED)
        .order_by('-completed_at', '-created_at')
        .first()
    )
    if latest_trip and latest_trip.completed_at:
        return timezone.localtime(latest_trip.completed_at).date()
    if latest_trip:
        return timezone.localtime(latest_trip.created_at).date()
    return timezone.localdate()


def trip_report_date(trip):
    if trip.loading_shift:
        return timezone.localtime(trip.loading_shift.opened_at).date()
    if trip.completed_at:
        return timezone.localtime(trip.completed_at).date()
    return timezone.localtime(trip.created_at).date()


def trip_shift_type(trip):
    if trip.loading_shift:
        return trip.loading_shift.shift_type
    if trip.unloading_shift:
        return trip.unloading_shift.shift_type
    return 'day'


def calculate_plan_completion_percent(volume, plan):
    if not plan:
        return None
    return (((volume or Decimal('0')) / plan) * Decimal('100')).quantize(Decimal('0.1'))


def build_management_daily_trend(trips, selected_date):
    trend_start = selected_date - timedelta(days=6)
    trend_by_date = {}
    for day_offset in range(7):
        report_date = trend_start + timedelta(days=day_offset)
        trend_by_date[report_date] = {
            'date': report_date,
            'volume': Decimal('0'),
            'plan': Decimal('0'),
            'tonnage': Decimal('0'),
            'trip_count': 0,
        }

    for trip in trips:
        report_date = trip_report_date(trip)
        if report_date not in trend_by_date:
            continue
        trend_by_date[report_date]['volume'] += trip.volume_m3 or 0
        trend_by_date[report_date]['plan'] += trip.planned_volume_m3 or 0
        trend_by_date[report_date]['tonnage'] += trip.tonnage or 0
        trend_by_date[report_date]['trip_count'] += 1

    daily_trend = []
    for values in trend_by_date.values():
        completion_percent = calculate_plan_completion_percent(values['volume'], values['plan'])
        daily_trend.append({
            **values,
            'deviation': values['volume'] - values['plan'],
            'completion_percent': completion_percent,
            'has_plan': completion_percent is not None,
        })
    return daily_trend


def customer_report_group_key(trip, include_report_date=False):
    key = (
        trip_shift_type(trip),
        str(trip.rock_type),
        str(trip.excavator),
        str(trip.dump_point),
        trip.loading_horizon,
        trip.loading_block,
        trip.transport_distance_km,
        trip.planned_volume_m3,
    )
    if include_report_date:
        return (trip_report_date(trip), *key)
    return key


def calculate_customer_accumulated_totals(trips):
    grouped = defaultdict(lambda: {
        'planned_volume': 0,
        'volume_m3': 0,
        'tonnage': 0,
        'trip_count': 0,
    })

    for trip in trips:
        key = customer_report_group_key(trip, include_report_date=True)
        grouped[key]['planned_volume'] = trip.planned_volume_m3 or 0
        grouped[key]['volume_m3'] += trip.volume_m3 or 0
        grouped[key]['tonnage'] += trip.tonnage or 0
        grouped[key]['trip_count'] += 1

    total_plan = sum(values['planned_volume'] for values in grouped.values())
    total_volume = sum(values['volume_m3'] for values in grouped.values())
    total_tonnage = sum(values['tonnage'] for values in grouped.values())
    total_trip_count = sum(values['trip_count'] for values in grouped.values())

    return {
        'plan': total_plan,
        'volume': total_volume,
        'deviation': total_volume - total_plan,
        'tonnage': total_tonnage,
        'trip_count': total_trip_count,
    }


def build_mechanic_downtime_rows(selected_date):
    events = (
        DowntimeEvent.objects
        .filter(started_at__date=selected_date)
        .select_related('equipment', 'reason', 'employee')
        .order_by('started_at')
    )
    rows = []
    for event in events:
        end_time = event.ended_at or timezone.now()
        duration_minutes = max(int((end_time - event.started_at).total_seconds() // 60), 0)
        rows.append({
            'started_at': event.started_at,
            'ended_at': event.ended_at,
            'equipment': event.equipment,
            'reason': event.reason,
            'employee': event.employee,
            'comment': event.comment,
            'duration_minutes': duration_minutes,
            'duration_hours': Decimal(duration_minutes) / Decimal('60'),
            'is_open': event.ended_at is None,
        })
    return rows


def build_customer_daily_report(selected_date):
    trips = Trip.objects.filter(status=TripStatus.COMPLETED).select_related(
        'truck',
        'excavator',
        'rock_type',
        'dump_point',
        'loading_shift',
        'unloading_shift',
    )
    all_trips = list(trips)
    trips = [trip for trip in all_trips if trip_report_date(trip) == selected_date]
    month_start = selected_date.replace(day=1)
    month_trips = [
        trip
        for trip in all_trips
        if month_start <= trip_report_date(trip) <= selected_date
    ]
    month_totals = calculate_customer_accumulated_totals(month_trips)

    grouped = defaultdict(lambda: {
        'volume_m3': 0,
        'tonnage': 0,
        'trip_count': 0,
        'carryover_count': 0,
        'trucks': set(),
        'downtimes': set(),
        'notes': set(),
    })

    for trip in trips:
        key = customer_report_group_key(trip)
        grouped[key]['volume_m3'] += trip.volume_m3 or 0
        grouped[key]['tonnage'] += trip.tonnage or 0
        grouped[key]['trip_count'] += 1
        grouped[key]['carryover_count'] += 1 if trip.is_carryover else 0
        grouped[key]['trucks'].add(str(trip.truck))
        if trip.downtime_text:
            grouped[key]['downtimes'].add(trip.downtime_text)
        if trip.note:
            grouped[key]['notes'].add(trip.note)

    rows_by_shift = {'day': [], 'night': []}
    for (
        shift_type,
        rock_type,
        excavator,
        dump_point,
        loading_horizon,
        loading_block,
        transport_distance_km,
        planned_volume_m3,
    ), values in grouped.items():
        note_parts = []
        if values['carryover_count']:
            note_parts.append(f"переходящих рейсов: {values['carryover_count']}")
        if values['trucks']:
            note_parts.append('самосвалы: ' + ', '.join(sorted(values['trucks'])))
        if values['notes']:
            note_parts.extend(sorted(values['notes']))
        rows_by_shift[shift_type].append({
            'rock_type': rock_type,
            'excavator': excavator,
            'planned_volume': planned_volume_m3,
            'volume_m3': values['volume_m3'],
            'volume_deviation': values['volume_m3'] - planned_volume_m3 if planned_volume_m3 is not None else None,
            'horizon': loading_horizon,
            'block': loading_block,
            'dump_point': dump_point,
            'distance_km': transport_distance_km,
            'downtime': '; '.join(sorted(values['downtimes'])),
            'note': '; '.join(note_parts),
            'tonnage': values['tonnage'],
            'trip_count': values['trip_count'],
        })

    for shift_rows in rows_by_shift.values():
        shift_rows.sort(key=lambda row: (row['excavator'], row['rock_type'], row['dump_point']))

    day_total = sum(row['volume_m3'] for row in rows_by_shift['day'])
    night_total = sum(row['volume_m3'] for row in rows_by_shift['night'])
    day_plan_total = sum(row['planned_volume'] or 0 for row in rows_by_shift['day'])
    night_plan_total = sum(row['planned_volume'] or 0 for row in rows_by_shift['night'])
    day_tonnage = sum(row['tonnage'] for row in rows_by_shift['day'])
    night_tonnage = sum(row['tonnage'] for row in rows_by_shift['night'])
    day_trip_count = sum(row['trip_count'] for row in rows_by_shift['day'])
    night_trip_count = sum(row['trip_count'] for row in rows_by_shift['night'])

    rock_summary = (
        Trip.objects
        .filter(id__in=[trip.id for trip in trips])
        .values('rock_type__name')
        .annotate(total_volume=Sum('volume_m3'), total_tonnage=Sum('tonnage'), trip_count=Count('id'))
        .order_by('-total_volume')
    )
    mechanic_downtime_rows = build_mechanic_downtime_rows(selected_date)

    return {
        'rows_by_shift': rows_by_shift,
        'day_total': day_total,
        'night_total': night_total,
        'total_volume': day_total + night_total,
        'day_plan_total': day_plan_total,
        'night_plan_total': night_plan_total,
        'total_plan': day_plan_total + night_plan_total,
        'day_deviation': day_total - day_plan_total,
        'night_deviation': night_total - night_plan_total,
        'total_deviation': (day_total + night_total) - (day_plan_total + night_plan_total),
        'day_tonnage': day_tonnage,
        'night_tonnage': night_tonnage,
        'total_tonnage': day_tonnage + night_tonnage,
        'day_trip_count': day_trip_count,
        'night_trip_count': night_trip_count,
        'total_trip_count': day_trip_count + night_trip_count,
        'month_start': month_start,
        'month_plan_total': month_totals['plan'],
        'month_total_volume': month_totals['volume'],
        'month_total_deviation': month_totals['deviation'],
        'month_total_tonnage': month_totals['tonnage'],
        'month_total_trip_count': month_totals['trip_count'],
        'rock_summary': rock_summary,
        'mechanic_downtime_rows': mechanic_downtime_rows,
        'mechanic_downtime_count': len(mechanic_downtime_rows),
        'mechanic_open_downtime_count': sum(1 for row in mechanic_downtime_rows if row['is_open']),
        'mechanic_downtime_hours': sum((row['duration_hours'] for row in mechanic_downtime_rows), Decimal('0')).quantize(Decimal('0.01')),
    }


def customer_daily_report_context(request):
    selected_date = parse_customer_report_date(request)
    report = build_customer_daily_report(selected_date)
    return {
        **report,
        'selected_date': selected_date,
        'date_input': selected_date.strftime('%Y-%m-%d'),
        'excel_reconciliation': CUSTOMER_DAILY_EXCEL_RECONCILIATION,
    }


def customer_daily_report_view(request):
    access = get_reports_access(request, {'dispatcher', 'admin', 'manager'})
    if not access:
        return redirect('login' if not request.session.get('employee_access_id') else 'role_home')

    return render(
        request,
        'reports/customer_daily_report.html',
        {
            'access': access,
            **customer_daily_report_context(request),
        },
    )


def append_customer_shift_table(sheet, title, start_row, start_col, rows):
    headers = [
        'Тип грунта',
        'Экскаватор',
        'План, м3',
        'Факт, м3',
        'Отклонение, м3',
        'Горизонт',
        'Блок',
        'Место разгрузки',
        'Плечо, км',
        'Простои, час',
        'Примечание',
    ]
    sheet.cell(start_row, start_col, title)
    sheet.cell(start_row, start_col).font = Font(bold=True, size=13)
    for offset, header in enumerate(headers):
        cell = sheet.cell(start_row + 1, start_col + offset, header)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='17232E')
        cell.alignment = Alignment(wrap_text=True, vertical='top')

    for row_index, row in enumerate(rows, start=start_row + 2):
        values = [
            row['rock_type'],
            row['excavator'],
            row['planned_volume'] or 'не задано',
            row['volume_m3'],
            row['volume_deviation'] if row['volume_deviation'] is not None else '-',
            row['horizon'] or 'не задано',
            row['block'] or 'не задано',
            row['dump_point'],
            row['distance_km'] or 'не задано',
            row['downtime'] or 'не задано',
            row['note'],
        ]
        for offset, value in enumerate(values):
            cell = sheet.cell(row_index, start_col + offset, value)
            cell.alignment = Alignment(wrap_text=True, vertical='top')

    return start_row + max(len(rows), 1) + 3


def append_customer_reconciliation_sheet(workbook, reconciliation_rows):
    sheet = workbook.create_sheet('Сверка с Excel')
    sheet['A1'] = 'Сверка суточного отчета с действующей Excel-формой заказчика'
    sheet['A1'].font = Font(bold=True, size=14)
    sheet['A2'] = 'Эталон для сверки: Отчет_Коппер. Рисорсез_Март.xlsx'
    sheet['A2'].alignment = Alignment(wrap_text=True)

    headers = ['Блок старой формы', 'Поля старой формы', 'Где в MVP', 'Статус', 'Комментарий']
    for offset, header in enumerate(headers, start=1):
        cell = sheet.cell(4, offset, header)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='17232E')
        cell.alignment = Alignment(wrap_text=True, vertical='top')

    for row_index, row in enumerate(reconciliation_rows, start=5):
        values = [
            row['old_block'],
            row['old_fields'],
            row['mvp_block'],
            row['status'],
            row['note'],
        ]
        for offset, value in enumerate(values, start=1):
            cell = sheet.cell(row_index, offset, value)
            cell.alignment = Alignment(wrap_text=True, vertical='top')

    widths = [28, 42, 34, 20, 58]
    for column_index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(column_index)].width = width

    return sheet


def customer_daily_report_export_view(request):
    access = get_reports_access(request, {'dispatcher', 'admin', 'manager'})
    if not access:
        return redirect('login' if not request.session.get('employee_access_id') else 'role_home')

    context = customer_daily_report_context(request)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Суточный отчет'
    sheet['A1'] = 'Отчет о работе ООО "Коппер Рисорсез" на ООО "Амур Минералс"'
    sheet['A1'].font = Font(bold=True, size=14)
    sheet['A2'] = f"Дата отчета: {context['selected_date']:%d.%m.%Y}"
    sheet['A4'] = 'Суточная сводка'
    sheet['A4'].font = Font(bold=True)
    summary_rows = [
        ['Показатель', 'День', 'Ночь', 'Сутки'],
        ['План, м3', context['day_plan_total'], context['night_plan_total'], context['total_plan']],
        ['Факт, м3', context['day_total'], context['night_total'], context['total_volume']],
        ['Отклонение, м3', context['day_deviation'], context['night_deviation'], context['total_deviation']],
        ['Тоннаж', context['day_tonnage'], context['night_tonnage'], context['total_tonnage']],
        ['Рейсы', context['day_trip_count'], context['night_trip_count'], context['total_trip_count']],
        ['Механические простои', '', '', context['mechanic_downtime_count']],
        ['Открытые механические простои', '', '', context['mechanic_open_downtime_count']],
        ['Механические простои, ч', '', '', context['mechanic_downtime_hours']],
    ]
    for row in summary_rows:
        sheet.append(row)

    sheet['F4'] = f"С начала месяца ({context['month_start']:%d.%m.%Y} - {context['selected_date']:%d.%m.%Y})"
    sheet['F4'].font = Font(bold=True)
    month_rows = [
        ['Показатель', 'Значение'],
        ['План, м3', context['month_plan_total']],
        ['Факт, м3', context['month_total_volume']],
        ['Отклонение, м3', context['month_total_deviation']],
        ['Тоннаж', context['month_total_tonnage']],
        ['Рейсы', context['month_total_trip_count']],
    ]
    for row_index, row in enumerate(month_rows, start=5):
        for column_offset, value in enumerate(row, start=6):
            cell = sheet.cell(row_index, column_offset, value)
            if row_index == 5:
                cell.font = Font(bold=True, color='FFFFFF')
                cell.fill = PatternFill('solid', fgColor='17232E')

    day_end_row = append_customer_shift_table(sheet, 'I смена (дневная 08:00 - 20:00)', 13, 1, context['rows_by_shift']['day'])
    night_end_row = append_customer_shift_table(sheet, 'II смена (ночная 20:00 - 08:00)', 13, 12, context['rows_by_shift']['night'])

    downtime_start_row = max(day_end_row, night_end_row) + 1
    sheet.cell(downtime_start_row, 1, 'Механические простои за дату')
    sheet.cell(downtime_start_row, 1).font = Font(bold=True, size=13)
    downtime_headers = ['Начало', 'Окончание', 'Техника', 'Причина', 'Длительность, ч', 'Кто зафиксировал', 'Комментарий']
    for offset, header in enumerate(downtime_headers, start=1):
        cell = sheet.cell(downtime_start_row + 1, offset, header)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='17232E')
        cell.alignment = Alignment(wrap_text=True, vertical='top')
    if context['mechanic_downtime_rows']:
        for row_index, downtime in enumerate(context['mechanic_downtime_rows'], start=downtime_start_row + 2):
            values = [
                timezone.localtime(downtime['started_at']).strftime('%d.%m.%Y %H:%M'),
                timezone.localtime(downtime['ended_at']).strftime('%d.%m.%Y %H:%M') if downtime['ended_at'] else 'открыт',
                str(downtime['equipment']),
                str(downtime['reason']),
                downtime['duration_hours'],
                str(downtime['employee']) if downtime['employee'] else '-',
                downtime['comment'] or '-',
            ]
            for offset, value in enumerate(values, start=1):
                cell = sheet.cell(row_index, offset, value)
                cell.alignment = Alignment(wrap_text=True, vertical='top')
    else:
        sheet.cell(downtime_start_row + 2, 1, 'Механических простоев за выбранную дату нет.')

    for column_index in range(1, 23):
        sheet.column_dimensions[get_column_letter(column_index)].width = 16
    sheet.column_dimensions['K'].width = 36
    sheet.column_dimensions['V'].width = 36
    append_customer_reconciliation_sheet(workbook, context['excel_reconciliation'])

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = 'attachment; filename=\"customer_daily_report.xlsx\"'
    workbook.save(response)
    return response


def management_dashboard_context(request, access):
    completed_trips = Trip.objects.filter(status=TripStatus.COMPLETED).select_related(
        'truck',
        'excavator',
        'rock_type',
        'dump_point',
        'loading_shift',
        'unloading_shift',
    )
    active_trips = Trip.objects.filter(status=TripStatus.ACTIVE)
    open_mechanic_downtimes = (
        DowntimeEvent.objects
        .filter(ended_at__isnull=True)
        .select_related('equipment', 'reason', 'employee')
        .order_by('started_at')[:8]
    )
    selected_date = parse_customer_report_date(request)
    completed_trip_list = list(completed_trips)
    daily_trips = [
        trip
        for trip in completed_trip_list
        if trip_report_date(trip) == selected_date
    ]
    daily_total_volume = sum((trip.volume_m3 or 0) for trip in daily_trips)
    daily_total_tonnage = sum((trip.tonnage or 0) for trip in daily_trips)
    daily_plan_total = sum((trip.planned_volume_m3 or 0) for trip in daily_trips)
    daily_deviation = daily_total_volume - daily_plan_total
    daily_plan_completion_percent = calculate_plan_completion_percent(daily_total_volume, daily_plan_total)
    daily_plan_completion_class = (
        'success'
        if daily_plan_completion_percent is not None and daily_plan_completion_percent >= Decimal('100')
        else 'danger'
    )
    daily_shift_totals = {
        'day': {
            'label': 'Дневная смена',
            'css_class': 'day',
            'volume': Decimal('0'),
            'plan': Decimal('0'),
            'tonnage': Decimal('0'),
            'trip_count': 0,
        },
        'night': {
            'label': 'Ночная смена',
            'css_class': 'night',
            'volume': Decimal('0'),
            'plan': Decimal('0'),
            'tonnage': Decimal('0'),
            'trip_count': 0,
        },
        'unknown': {
            'label': 'Смена не указана',
            'css_class': 'unknown',
            'volume': Decimal('0'),
            'plan': Decimal('0'),
            'tonnage': Decimal('0'),
            'trip_count': 0,
        },
    }
    for trip in daily_trips:
        shift_type = trip_shift_type(trip) or 'unknown'
        if shift_type not in daily_shift_totals:
            shift_type = 'unknown'
        daily_shift_totals[shift_type]['volume'] += trip.volume_m3 or 0
        daily_shift_totals[shift_type]['plan'] += trip.planned_volume_m3 or 0
        daily_shift_totals[shift_type]['tonnage'] += trip.tonnage or 0
        daily_shift_totals[shift_type]['trip_count'] += 1
    daily_shift_comparison = []
    max_daily_shift_volume = max((item['volume'] for item in daily_shift_totals.values()), default=Decimal('0'))
    max_daily_shift_plan = max((item['plan'] for item in daily_shift_totals.values()), default=Decimal('0'))
    for key in ('day', 'night', 'unknown'):
        item = daily_shift_totals[key]
        if key == 'unknown' and item['trip_count'] == 0:
            continue
        daily_shift_comparison.append({
            **item,
            'deviation': item['volume'] - item['plan'],
        })
    daily_trend = build_management_daily_trend(completed_trip_list, selected_date)
    max_daily_trend_volume = max((item['volume'] for item in daily_trend), default=Decimal('0'))
    max_daily_trend_plan = max((item['plan'] for item in daily_trend), default=Decimal('0'))
    trend_total_volume = sum((item['volume'] for item in daily_trend), Decimal('0'))
    trend_total_plan = sum((item['plan'] for item in daily_trend), Decimal('0'))
    trend_total_deviation = trend_total_volume - trend_total_plan
    trend_trip_count = sum(item['trip_count'] for item in daily_trend)
    trend_completion_percent = calculate_plan_completion_percent(trend_total_volume, trend_total_plan)
    trend_best_day = max(daily_trend, key=lambda item: item['volume']) if trend_trip_count else None
    trend_worst_day = min(
        (item for item in daily_trend if item['has_plan'] or item['trip_count']),
        key=lambda item: item['deviation'],
        default=None,
    )
    completed_summary = completed_trips.aggregate(
        total_volume=Sum('volume_m3'),
        total_tonnage=Sum('tonnage'),
        trip_count=Count('id'),
    )
    top_excavators = (
        completed_trips
        .values('excavator__garage_number')
        .annotate(total_volume=Sum('volume_m3'), trip_count=Count('id'))
        .order_by('-total_volume')[:5]
    )
    top_rocks = (
        completed_trips
        .values('rock_type__name')
        .annotate(total_volume=Sum('volume_m3'), trip_count=Count('id'))
        .order_by('-total_volume')[:5]
    )
    daily_excavator_totals = defaultdict(lambda: {'volume': 0, 'trip_count': 0})
    daily_rock_totals = defaultdict(lambda: {'volume': 0, 'trip_count': 0})
    for trip in daily_trips:
        excavator_name = f'Экскаватор {trip.excavator.garage_number}' if trip.excavator else '-'
        rock_name = str(trip.rock_type) if trip.rock_type else '-'
        daily_excavator_totals[excavator_name]['volume'] += trip.volume_m3 or 0
        daily_excavator_totals[excavator_name]['trip_count'] += 1
        daily_rock_totals[rock_name]['volume'] += trip.volume_m3 or 0
        daily_rock_totals[rock_name]['trip_count'] += 1
    daily_top_excavators = sorted(
        [
            {'name': name, **values}
            for name, values in daily_excavator_totals.items()
        ],
        key=lambda item: item['volume'],
        reverse=True,
    )[:5]
    daily_top_rocks = sorted(
        [
            {'name': name, **values}
            for name, values in daily_rock_totals.items()
        ],
        key=lambda item: item['volume'],
        reverse=True,
    )[:5]
    recent_completed_trips = completed_trips.order_by('-completed_at')[:8]

    max_excavator_volume = max((item['total_volume'] or 0 for item in top_excavators), default=0)
    max_rock_volume = max((item['total_volume'] or 0 for item in top_rocks), default=0)
    daily_max_excavator_volume = max((item['volume'] or 0 for item in daily_top_excavators), default=0)
    daily_max_rock_volume = max((item['volume'] or 0 for item in daily_top_rocks), default=0)

    return {
        'access': access,
        'selected_date': selected_date,
        'daily_plan_total': daily_plan_total,
        'daily_total_volume': daily_total_volume,
        'daily_deviation': daily_deviation,
        'daily_plan_completion_percent': daily_plan_completion_percent,
        'daily_plan_completion_class': daily_plan_completion_class,
        'daily_total_tonnage': daily_total_tonnage,
        'daily_trip_count': len(daily_trips),
        'daily_top_excavators': daily_top_excavators,
        'daily_top_rocks': daily_top_rocks,
        'daily_max_excavator_volume': daily_max_excavator_volume,
        'daily_max_rock_volume': daily_max_rock_volume,
        'daily_shift_comparison': daily_shift_comparison,
        'max_daily_shift_volume': max_daily_shift_volume,
        'max_daily_shift_plan': max_daily_shift_plan,
        'daily_trend': daily_trend,
        'max_daily_trend_volume': max_daily_trend_volume,
        'max_daily_trend_plan': max_daily_trend_plan,
        'trend_total_volume': trend_total_volume,
        'trend_total_plan': trend_total_plan,
        'trend_total_deviation': trend_total_deviation,
        'trend_trip_count': trend_trip_count,
        'trend_completion_percent': trend_completion_percent,
        'trend_best_day': trend_best_day,
        'trend_worst_day': trend_worst_day,
        'total_volume': completed_summary['total_volume'] or 0,
        'total_tonnage': completed_summary['total_tonnage'] or 0,
        'completed_trip_count': completed_summary['trip_count'] or 0,
        'active_trip_count': active_trips.count(),
        'open_mechanic_downtime_count': DowntimeEvent.objects.filter(ended_at__isnull=True).count(),
        'carryover_trip_count': completed_trips.filter(is_carryover=True).count(),
        'mechanic_downtime_count': len(build_mechanic_downtime_rows(selected_date)),
        'open_mechanic_downtimes': open_mechanic_downtimes,
        'top_excavators': top_excavators,
        'top_rocks': top_rocks,
        'max_excavator_volume': max_excavator_volume,
        'max_rock_volume': max_rock_volume,
        'recent_completed_trips': recent_completed_trips,
    }


def management_dashboard_view(request):
    access = get_reports_access(request, {'manager', 'admin', 'dispatcher'})
    if not access:
        return redirect('login' if not request.session.get('employee_access_id') else 'role_home')
    return render(request, 'reports/management_dashboard.html', management_dashboard_context(request, access))


def write_key_value_rows(sheet, start_row, rows):
    for offset, (label, value) in enumerate(rows):
        row = start_row + offset
        sheet.cell(row=row, column=1, value=label)
        sheet.cell(row=row, column=2, value=value)
    return start_row + len(rows)


def style_management_export_sheet(sheet):
    header_fill = PatternFill('solid', fgColor='12232E')
    header_font = Font(color='FFFFFF', bold=True)
    for row in sheet.iter_rows():
        for cell in row:
            cell.alignment = Alignment(vertical='top', wrap_text=True)
            if cell.row == 1:
                cell.font = Font(bold=True, size=14)
    for row in sheet.iter_rows():
        if row[0].value and all(cell.value for cell in row[:2]):
            if row[0].row > 1 and str(row[0].value).startswith(('Показатель', 'Дата', 'Смена')):
                for cell in row:
                    cell.fill = header_fill
                    cell.font = header_font
    for column in range(1, sheet.max_column + 1):
        sheet.column_dimensions[get_column_letter(column)].width = 22


def management_dashboard_export_view(request):
    access = get_reports_access(request, {'manager', 'admin', 'dispatcher'})
    if not access:
        return redirect('login' if not request.session.get('employee_access_id') else 'role_home')

    context = management_dashboard_context(request, access)
    workbook = Workbook()
    summary_sheet = workbook.active
    summary_sheet.title = 'Сводка'
    summary_sheet['A1'] = 'Витрина руководства'
    summary_sheet['A2'] = f"Дата среза: {context['selected_date']:%d.%m.%Y}"
    summary_sheet['A3'] = f"Сформировал: {access.employee.full_name}"

    rows = [
        ('Факт за сутки, м3', context['daily_total_volume']),
        ('План за сутки, м3', context['daily_plan_total']),
        ('Выполнение плана, %', context['daily_plan_completion_percent'] or 'Нет плана'),
        ('Отклонение за сутки, м3', context['daily_deviation']),
        ('Тоннаж за сутки, т', context['daily_total_tonnage']),
        ('Рейсы за сутки', context['daily_trip_count']),
        ('Активные рейсы', context['active_trip_count']),
        ('Открытые механические простои', context['open_mechanic_downtime_count']),
        ('Переходящие рейсы', context['carryover_trip_count']),
        ('Факт за 7 дней, м3', context['trend_total_volume']),
        ('План за 7 дней, м3', context['trend_total_plan']),
        ('Выполнение за неделю, %', context['trend_completion_percent'] or 'Нет плана'),
        ('Отклонение за неделю, м3', context['trend_total_deviation']),
        ('Рейсы за 7 дней', context['trend_trip_count']),
    ]
    summary_sheet.append([])
    summary_sheet.append(['Показатель', 'Значение'])
    write_key_value_rows(summary_sheet, 6, rows)

    trend_sheet = workbook.create_sheet('Динамика 7 дней')
    trend_sheet.append(['Дата', 'Факт, м3', 'План, м3', 'Выполнение, %', 'Отклонение, м3', 'Рейсы', 'Тоннаж, т'])
    for item in context['daily_trend']:
        trend_sheet.append([
            item['date'].strftime('%d.%m.%Y'),
            item['volume'],
            item['plan'],
            item['completion_percent'] if item['has_plan'] else 'Нет плана',
            item['deviation'],
            item['trip_count'],
            item['tonnage'],
        ])

    shifts_sheet = workbook.create_sheet('День ночь')
    shifts_sheet.append(['Смена', 'Факт, м3', 'План, м3', 'Отклонение, м3', 'Рейсы', 'Тоннаж, т'])
    for item in context['daily_shift_comparison']:
        shifts_sheet.append([
            item['label'],
            item['volume'],
            item['plan'],
            item['deviation'],
            item['trip_count'],
            item['tonnage'],
        ])

    for sheet in workbook.worksheets:
        style_management_export_sheet(sheet)

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = 'attachment; filename="management_dashboard.xlsx"'
    workbook.save(response)
    return response
