from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
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


def dispatcher_control_view(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'dispatcher', 'admin', 'manager'}:
        return redirect('role_home')

    active_trips = (
        Trip.objects
        .filter(status=TripStatus.ACTIVE)
        .select_related('truck', 'excavator', 'rock_type', 'dump_point', 'excavator_operator')
        .order_by('created_at')
    )
    pending_assignments = (
        HaulAssignment.objects
        .filter(status=AssignmentStatus.PENDING, ended_at__isnull=True)
        .select_related('truck', 'excavator', 'assigned_by')
        .order_by('assigned_at')
    )
    accepted_assignments = (
        HaulAssignment.objects
        .filter(status=AssignmentStatus.ACCEPTED, ended_at__isnull=True)
        .select_related('truck', 'excavator', 'assigned_by')
        .order_by('-accepted_at')[:30]
    )
    recent_completed_trips = (
        Trip.objects
        .filter(status=TripStatus.COMPLETED)
        .select_related('truck', 'excavator', 'rock_type', 'dump_point', 'driver')
        .order_by('-completed_at')[:30]
    )

    return render(
        request,
        'trips/dispatcher_control.html',
        {
            'access': access,
            'active_trips': active_trips,
            'pending_assignments': pending_assignments,
            'accepted_assignments': accepted_assignments,
            'recent_completed_trips': recent_completed_trips,
            'active_trips_count': active_trips.count(),
            'pending_assignments_count': pending_assignments.count(),
            'accepted_assignments_count': len(accepted_assignments),
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
        unloading_shift = EmployeeShift.objects.filter(employee=access.employee, closed_at__isnull=True).order_by('-opened_at').first()
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
