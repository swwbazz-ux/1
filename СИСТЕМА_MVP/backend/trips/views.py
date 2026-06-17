from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone

from shifts.models import EmployeeShift
from users.models import EmployeeAccess

from .forms import TripCreateForm
from .models import Trip, TripStatus


def excavator_work_view(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code != 'excavator_operator':
        return redirect('role_home')

    if request.method == 'POST':
        form = TripCreateForm(request.POST)
        if form.is_valid():
            form.create_trip(excavator_operator=access.employee)
            messages.success(request, 'Рейс создан. У водителя появился активный рейс.')
            return redirect('excavator_work')
    else:
        form = TripCreateForm()

    active_trips = Trip.objects.filter(status=TripStatus.ACTIVE).select_related('truck', 'excavator', 'rock_type', 'dump_point').order_by('-created_at')[:20]
    return render(
        request,
        'trips/excavator_work.html',
        {
            'access': access,
            'form': form,
            'active_trips': active_trips,
        },
    )


def driver_complete_trip_view(request, trip_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code != 'driver':
        return redirect('role_home')
    registration = getattr(access.employee, 'driver_registration', None)
    if not registration:
        return redirect('driver_registration')
    trip = Trip.objects.filter(id=trip_id, truck=registration.truck, status=TripStatus.ACTIVE).first()
    if trip and request.method == 'POST':
        trip.status = TripStatus.COMPLETED
        trip.driver = access.employee
        trip.completed_at = timezone.now()
        trip.unloading_shift = EmployeeShift.objects.filter(employee=access.employee, closed_at__isnull=True).order_by('-opened_at').first()
        trip.save(update_fields=['status', 'driver', 'completed_at', 'unloading_shift'])
        messages.success(request, 'Рейс выполнен.')
    return redirect('driver_shift')
