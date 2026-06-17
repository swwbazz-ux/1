from collections import defaultdict
from datetime import datetime

from django.db.models import Count, Sum
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from trips.models import Trip, TripStatus
from users.models import EmployeeAccess

from .models import ReportTemplate


VOLUME_REPORT_COLUMNS = {
    'truck': ('Самосвал', lambda trip: str(trip.truck)),
    'excavator': ('Экскаватор', lambda trip: str(trip.excavator)),
    'rock_type': ('Порода', lambda trip: str(trip.rock_type)),
    'dump_point': ('Точка разгрузки', lambda trip: str(trip.dump_point)),
    'planned_volume_m3': ('План, м3', lambda trip: trip.planned_volume_m3 or ''),
    'volume_m3': ('Объем, м3', lambda trip: trip.volume_m3 or ''),
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
    'tonnage',
    'loading_horizon',
    'loading_block',
    'transport_distance_km',
    'loading_shift',
    'unloading_shift',
    'is_carryover',
    'completed_at',
]


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


def build_report_table(trips, selected_columns):
    headers = [VOLUME_REPORT_COLUMNS[column][0] for column in selected_columns]
    rows = [
        [VOLUME_REPORT_COLUMNS[column][1](trip) for column in selected_columns]
        for trip in trips
    ]
    return headers, rows


def apply_volume_report_filters(queryset, request):
    loading_shift_type = request.GET.get('loading_shift_type', '').strip()
    unloading_shift_type = request.GET.get('unloading_shift_type', '').strip()
    carryover = request.GET.get('carryover', '').strip()

    if loading_shift_type:
        queryset = queryset.filter(loading_shift__shift_type=loading_shift_type)
    if unloading_shift_type:
        queryset = queryset.filter(unloading_shift__shift_type=unloading_shift_type)
    if carryover == 'yes':
        queryset = queryset.filter(is_carryover=True)
    elif carryover == 'no':
        queryset = queryset.filter(is_carryover=False)
    return queryset


def volume_report_filter_context(request):
    return {
        'loading_shift_type': request.GET.get('loading_shift_type', '').strip(),
        'unloading_shift_type': request.GET.get('unloading_shift_type', '').strip(),
        'carryover': request.GET.get('carryover', '').strip(),
        'template': request.GET.get('template', '').strip(),
        'query_string': request.GET.urlencode(),
    }


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
    trips = apply_volume_report_filters(trips, request)
    selected_template = get_selected_report_template(request)
    selected_columns = get_selected_columns(selected_template)
    headers, rows = build_report_table(trips[:100], selected_columns)
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
            'filters': volume_report_filter_context(request),
            'report_templates': get_volume_report_templates(),
            'selected_template': selected_template,
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
    trips = apply_volume_report_filters(trips, request)
    selected_template = get_selected_report_template(request)
    selected_columns = get_selected_columns(selected_template)
    headers, rows = build_report_table(trips, selected_columns)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Объемы'
    sheet.append(headers)
    for row in rows:
        sheet.append(row)

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


def build_customer_daily_report(selected_date):
    trips = Trip.objects.filter(status=TripStatus.COMPLETED).select_related(
        'truck',
        'excavator',
        'rock_type',
        'dump_point',
        'loading_shift',
        'unloading_shift',
    )
    trips = [trip for trip in trips if trip_report_date(trip) == selected_date]

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

    append_customer_shift_table(sheet, 'I смена (дневная 08:00 - 20:00)', 10, 1, context['rows_by_shift']['day'])
    append_customer_shift_table(sheet, 'II смена (ночная 20:00 - 08:00)', 10, 12, context['rows_by_shift']['night'])

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
    recent_completed_trips = completed_trips.order_by('-completed_at')[:8]

    max_excavator_volume = max((item['total_volume'] or 0 for item in top_excavators), default=0)
    max_rock_volume = max((item['total_volume'] or 0 for item in top_rocks), default=0)

    return render(
        request,
        'reports/management_dashboard.html',
        {
            'access': access,
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
