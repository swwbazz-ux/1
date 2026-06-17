from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import redirect, render
from openpyxl import Workbook

from trips.models import Trip, TripStatus
from users.models import EmployeeAccess


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
    total_volume = trips.aggregate(total=Sum('volume_m3'))['total'] or 0
    return render(
        request,
        'reports/volume_report.html',
        {
            'access': access,
            'trips': trips[:100],
            'total_volume': total_volume,
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

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Объемы'
    sheet.append([
        'Самосвал',
        'Экскаватор',
        'Порода',
        'Точка разгрузки',
        'Объем, м3',
        'Тоннаж',
        'Смена загрузки',
        'Смена разгрузки',
        'Переходящий рейс',
        'Выполнен',
    ])
    for trip in trips:
        sheet.append([
            str(trip.truck),
            str(trip.excavator),
            str(trip.rock_type),
            str(trip.dump_point),
            float(trip.volume_m3 or 0),
            float(trip.tonnage or 0),
            trip.loading_shift.get_shift_type_display() if trip.loading_shift else '',
            trip.unloading_shift.get_shift_type_display() if trip.unloading_shift else '',
            'Да' if trip.is_carryover else 'Нет',
            trip.completed_at.strftime('%d.%m.%Y %H:%M') if trip.completed_at else '',
        ])

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = 'attachment; filename=\"volume_report.xlsx\"'
    workbook.save(response)
    return response
