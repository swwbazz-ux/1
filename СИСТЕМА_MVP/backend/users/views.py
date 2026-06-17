from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from shifts.models import EmployeeShift
from trips.models import Trip, TripStatus

from .forms import DriverOpenShiftForm, DriverPrimaryRegistrationForm
from .models import DriverPrimaryRegistration, EmployeeAccess


ROLE_INTERFACE_NAMES = {
    'admin': 'Админка',
    'driver': 'Интерфейс водителя самосвала',
    'excavator_operator': 'Интерфейс машиниста экскаватора',
    'mining_master': 'Интерфейс горного мастера',
    'dispatcher': 'Диспетчерский экран',
    'mechanic': 'Интерфейс механика',
    'manager': 'Витрина руководства',
}


def login_view(request):
    if request.method == 'POST':
        access_code = request.POST.get('access_code', '').strip()
        access = (
            EmployeeAccess.objects
            .select_related('employee', 'role')
            .filter(access_code=access_code, is_active=True, employee__is_active=True, role__is_active=True)
            .first()
        )
        if access:
            request.session['employee_access_id'] = access.id
            return redirect('role_home')
        messages.error(request, 'Доступ не найден или отключен.')
    return render(request, 'users/login.html')


def logout_view(request):
    request.session.flush()
    return redirect('login')


def role_home_view(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access:
        request.session.flush()
        return redirect('login')
    if access.role.code == 'driver':
        if not hasattr(access.employee, 'driver_registration'):
            return redirect('driver_registration')
        return redirect('driver_shift')
    if access.role.code == 'mining_master':
        return redirect('mining_master_assignments')
    if access.role.code == 'excavator_operator':
        return redirect('excavator_work')
    if access.role.code == 'dispatcher':
        return redirect('dispatcher_control')
    if access.role.code == 'manager':
        return redirect('volume_report')
    interface_name = ROLE_INTERFACE_NAMES.get(access.role.code, f'Интерфейс роли: {access.role.name}')
    return render(
        request,
        'users/role_home.html',
        {
            'access': access,
            'interface_name': interface_name,
        },
    )


def driver_registration_view(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code != 'driver':
        return redirect('role_home')

    registration = getattr(access.employee, 'driver_registration', None)
    if registration:
        return redirect('role_home')

    if request.method == 'POST':
        form = DriverPrimaryRegistrationForm(request.POST, employee=access.employee)
        if form.is_valid():
            DriverPrimaryRegistration.objects.create(employee=access.employee, **form.cleaned_data)
            messages.success(request, 'Первичная регистрация сохранена.')
            return redirect('role_home')
    else:
        form = DriverPrimaryRegistrationForm(employee=access.employee)

    return render(request, 'users/driver_registration.html', {'form': form, 'access': access})


def driver_shift_view(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code != 'driver':
        return redirect('role_home')
    registration = getattr(access.employee, 'driver_registration', None)
    if not registration:
        return redirect('driver_registration')

    open_shift = EmployeeShift.objects.filter(employee=access.employee, closed_at__isnull=True).order_by('-opened_at').first()
    pending_assignment = HaulAssignment.objects.filter(
        truck=registration.truck,
        status=AssignmentStatus.PENDING,
        ended_at__isnull=True,
    ).select_related('truck', 'excavator').order_by('-assigned_at').first()
    active_trip = Trip.objects.filter(
        truck=registration.truck,
        status=TripStatus.ACTIVE,
    ).select_related('truck', 'excavator', 'rock_type', 'dump_point').order_by('-created_at').first()

    if request.method == 'POST' and not open_shift:
        form = DriverOpenShiftForm(request.POST)
        if form.is_valid():
            shift = form.save(commit=False)
            shift.employee = access.employee
            shift.opened_by = access.employee
            shift.shift_type = registration.shift_type
            shift.equipment = registration.truck
            shift.opened_at = timezone.now()
            shift.save()
            messages.success(request, 'Смена открыта.')
            return redirect('driver_shift')
    else:
        form = DriverOpenShiftForm()

    return render(
        request,
        'users/driver_shift.html',
        {
            'access': access,
            'registration': registration,
            'open_shift': open_shift,
            'pending_assignment': pending_assignment,
            'active_trip': active_trip,
            'form': form,
        },
    )


def driver_accept_assignment_view(request, assignment_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code != 'driver':
        return redirect('role_home')
    registration = getattr(access.employee, 'driver_registration', None)
    if not registration:
        return redirect('driver_registration')
    assignment = HaulAssignment.objects.filter(
        id=assignment_id,
        truck=registration.truck,
        status=AssignmentStatus.PENDING,
    ).first()
    if assignment and request.method == 'POST':
        assignment.status = AssignmentStatus.ACCEPTED
        assignment.accepted_at = timezone.now()
        assignment.save(update_fields=['status', 'accepted_at'])
        messages.success(request, 'Назначение принято.')
    return redirect('driver_shift')

# Create your views here.
