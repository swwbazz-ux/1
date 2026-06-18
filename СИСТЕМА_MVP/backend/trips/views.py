from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from references.models import Equipment
from shifts.models import EmployeeShift
from users.models import EmployeeAccess

from .forms import TripCreateForm
from .models import DispatcherActionLog, DispatcherActionType, Trip, TripStatus


def log_dispatcher_action(*, actor, action_type, target_summary, trip=None, shift=None, haul_assignment=None):
    DispatcherActionLog.objects.create(
        actor=actor,
        action_type=action_type,
        trip=trip,
        shift=shift,
        haul_assignment=haul_assignment,
        target_summary=target_summary,
    )


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

    return render(
        request,
        'trips/dispatcher_control.html',
        {
            'access': access,
            'active_trips': active_trips,
            'pending_assignments': pending_assignments,
            'accepted_assignments': accepted_assignments[:30],
            'recent_completed_trips': recent_completed_trips[:30],
            'open_shifts': open_shifts,
            'active_trips_count': active_trips.count(),
            'pending_assignments_count': pending_assignments.count(),
            'accepted_assignments_count': accepted_assignments.count(),
            'open_shifts_count': len(open_shifts),
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
        },
    )


def dispatcher_service_close_shift_view(request, shift_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')

    if request.method != 'POST':
        return redirect('dispatcher_control')

    shift = (
        EmployeeShift.objects
        .select_related('employee', 'equipment')
        .filter(id=shift_id, closed_at__isnull=True)
        .first()
    )
    if not shift:
        messages.error(request, 'Открытая смена для служебного закрытия не найдена.')
        return redirect('dispatcher_control')

    shift.closed_at = timezone.now()
    shift.closed_by = access.employee
    shift.is_service_closed = True
    shift.save(update_fields=['closed_at', 'closed_by', 'is_service_closed'])
    log_dispatcher_action(
        actor=access.employee,
        action_type=DispatcherActionType.SERVICE_CLOSE_SHIFT,
        shift=shift,
        target_summary=f'{shift.employee} / {shift.equipment or "-"} / {shift.get_shift_type_display()}',
    )
    messages.success(request, f'Смена сотрудника {shift.employee} закрыта служебно.')
    return redirect('dispatcher_control')


def dispatcher_cancel_assignment_view(request, assignment_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')

    if request.method != 'POST':
        return redirect('dispatcher_control')

    assignment = (
        HaulAssignment.objects
        .select_related('truck', 'excavator')
        .filter(id=assignment_id, ended_at__isnull=True, status__in={AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED})
        .first()
    )
    if not assignment:
        messages.error(request, 'Активное назначение для отмены не найдено.')
        return redirect('dispatcher_control')

    assignment.status = AssignmentStatus.CANCELLED
    assignment.ended_at = timezone.now()
    assignment.save(update_fields=['status', 'ended_at'])
    log_dispatcher_action(
        actor=access.employee,
        action_type=DispatcherActionType.CANCEL_ASSIGNMENT,
        haul_assignment=assignment,
        target_summary=f'{assignment.truck} под {assignment.excavator}',
    )
    messages.success(request, f'Назначение {assignment.truck} под {assignment.excavator} отменено.')
    return redirect('dispatcher_control')


def dispatcher_cancel_trip_view(request, trip_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')

    if request.method != 'POST':
        return redirect('dispatcher_control')

    trip = (
        Trip.objects
        .select_related('truck', 'excavator')
        .filter(id=trip_id, status=TripStatus.ACTIVE)
        .first()
    )
    if not trip:
        messages.error(request, 'Активный рейс для отмены не найден.')
        return redirect('dispatcher_control')

    trip.status = TripStatus.CANCELLED
    trip.save(update_fields=['status'])
    log_dispatcher_action(
        actor=access.employee,
        action_type=DispatcherActionType.CANCEL_TRIP,
        trip=trip,
        target_summary=f'{trip.truck} -> {trip.dump_point}',
    )
    messages.success(request, f'Рейс {trip.truck} -> {trip.dump_point} отменен.')
    return redirect('dispatcher_control')


def dispatcher_complete_trip_view(request, trip_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')

    if request.method != 'POST':
        return redirect('dispatcher_control')

    trip = (
        Trip.objects
        .select_related('truck', 'excavator', 'loading_shift')
        .filter(id=trip_id, status=TripStatus.ACTIVE)
        .first()
    )
    if not trip:
        messages.error(request, 'Активный рейс для служебного завершения не найден.')
        return redirect('dispatcher_control')

    unloading_shift = (
        EmployeeShift.objects
        .filter(equipment=trip.truck, closed_at__isnull=True)
        .order_by('-opened_at')
        .first()
    )
    if not unloading_shift:
        messages.error(request, 'Нельзя служебно завершить рейс: не найдена открытая смена по этому самосвалу.')
        return redirect('dispatcher_control')

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
    )
    messages.success(request, f'Рейс {trip.truck} завершен служебно.')
    return redirect('dispatcher_control')


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
