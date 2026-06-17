from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import redirect, render
from openpyxl import Workbook

from trips.models import Trip, TripStatus
from users.models import EmployeeAccess

from .models import ReportTemplate


VOLUME_REPORT_COLUMNS = {
    'truck': ('Самосвал', lambda trip: str(trip.truck)),
    'excavator': ('Экскаватор', lambda trip: str(trip.excavator)),
    'rock_type': ('Порода', lambda trip: str(trip.rock_type)),
    'dump_point': ('Точка разгрузки', lambda trip: str(trip.dump_point)),
    'volume_m3': ('Объем, м3', lambda trip: trip.volume_m3 or ''),
    'tonnage': ('Тоннаж', lambda trip: trip.tonnage or ''),
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
    'volume_m3',
    'tonnage',
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
