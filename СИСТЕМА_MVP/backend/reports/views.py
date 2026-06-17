from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from django.contrib import messages
from django.db.models import Count, Sum
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

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

CARRYOVER_LABELS = {
    'yes': 'Да',
    'no': 'Нет',
}

VOLUME_REPORT_GROUPS = {
    'truck': ('Самосвал', lambda trip: str(trip.truck)),
    'excavator': ('Экскаватор', lambda trip: str(trip.excavator)),
    'rock_type': ('Порода/груз', lambda trip: str(trip.rock_type)),
    'dump_point': ('Точка разгрузки', lambda trip: str(trip.dump_point)),
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


def get_reports_access(request, allowed_roles):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return None
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in allowed_roles:
        return None
    return access


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
    }


def customer_daily_report_context(request):
    selected_date = parse_customer_report_date(request)
    report = build_customer_daily_report(selected_date)
    return {
        **report,
        'selected_date': selected_date,
        'date_input': selected_date.strftime('%Y-%m-%d'),
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

    append_customer_shift_table(sheet, 'I смена (дневная 08:00 - 20:00)', 13, 1, context['rows_by_shift']['day'])
    append_customer_shift_table(sheet, 'II смена (ночная 20:00 - 08:00)', 13, 12, context['rows_by_shift']['night'])

    for column_index in range(1, 23):
        sheet.column_dimensions[get_column_letter(column_index)].width = 16
    sheet.column_dimensions['K'].width = 36
    sheet.column_dimensions['V'].width = 36

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = 'attachment; filename=\"customer_daily_report.xlsx\"'
    workbook.save(response)
    return response


def management_dashboard_view(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'manager', 'admin', 'dispatcher'}:
        return redirect('role_home')

    completed_trips = Trip.objects.filter(status=TripStatus.COMPLETED).select_related(
        'truck',
        'excavator',
        'rock_type',
        'dump_point',
        'loading_shift',
        'unloading_shift',
    )
    active_trips = Trip.objects.filter(status=TripStatus.ACTIVE)
    selected_date = parse_customer_report_date(request)
    daily_trips = [
        trip
        for trip in completed_trips
        if trip_report_date(trip) == selected_date
    ]
    daily_total_volume = sum((trip.volume_m3 or 0) for trip in daily_trips)
    daily_total_tonnage = sum((trip.tonnage or 0) for trip in daily_trips)
    daily_plan_total = sum((trip.planned_volume_m3 or 0) for trip in daily_trips)
    daily_deviation = daily_total_volume - daily_plan_total
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

    return render(
        request,
        'reports/management_dashboard.html',
        {
            'access': access,
            'selected_date': selected_date,
            'daily_plan_total': daily_plan_total,
            'daily_total_volume': daily_total_volume,
            'daily_deviation': daily_deviation,
            'daily_total_tonnage': daily_total_tonnage,
            'daily_trip_count': len(daily_trips),
            'daily_top_excavators': daily_top_excavators,
            'daily_top_rocks': daily_top_rocks,
            'daily_max_excavator_volume': daily_max_excavator_volume,
            'daily_max_rock_volume': daily_max_rock_volume,
            'total_volume': completed_summary['total_volume'] or 0,
            'total_tonnage': completed_summary['total_tonnage'] or 0,
            'completed_trip_count': completed_summary['trip_count'] or 0,
            'active_trip_count': active_trips.count(),
            'carryover_trip_count': completed_trips.filter(is_carryover=True).count(),
            'top_excavators': top_excavators,
            'top_rocks': top_rocks,
            'max_excavator_volume': max_excavator_volume,
            'max_rock_volume': max_rock_volume,
            'recent_completed_trips': recent_completed_trips,
        },
    )
