from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from shifts.models import EmployeeShift
from trips.models import Trip, TripStatus

from .forms import DriverCloseShiftForm, DriverOpenShiftForm, DriverPrimaryRegistrationForm
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


INTERFACE_MAP = [
    {
        'section': 'Вход и администрирование',
        'items': [
            {'title': 'Единый вход', 'url': '/', 'code': 'любой демо-код', 'note': 'Открывает интерфейс по роли'},
            {'title': 'Карта интерфейсов', 'url': '/interfaces/', 'code': '-', 'note': 'Все готовые экраны MVP в одном месте'},
            {'title': 'Django-админка', 'url': '/admin/', 'code': 'администратор Django', 'note': 'Управление справочниками и данными через стандартную админку'},
        ],
    },
    {
        'section': 'Рабочие интерфейсы',
        'items': [
            {'title': 'Водитель самосвала', 'url': '/driver/shift/', 'code': '2000', 'note': 'Открытие/закрытие смены, активный рейс, подтверждение назначения'},
            {'title': 'Первичная регистрация водителя', 'url': '/driver/registration/', 'code': '2000', 'note': 'Первичный выбор смены, техники и проживания'},
            {'title': 'Машинист экскаватора', 'url': '/excavator/shift/', 'code': '3000', 'note': 'Создание рейса и параметры для отчета заказчику'},
            {'title': 'Горный мастер', 'url': '/master/assignments/', 'code': '4000', 'note': 'Назначение самосвалов под экскаваторы'},
            {'title': 'Диспетчерский пульт', 'url': '/dispatcher/control/', 'code': '5000', 'note': 'Контроль активных рейсов и назначений'},
            {'title': 'Механическая служба', 'url': '/mechanic/downtimes/', 'code': '7000 / роль механика', 'note': 'Открытие и закрытие механических простоев по технике'},
        ],
    },
    {
        'section': 'Отчеты и руководство',
        'items': [
            {'title': 'Отчет по объемам', 'url': '/reports/volume/', 'code': '5000 / 6000', 'note': 'Фильтры, шаблоны, группировки и Excel'},
            {'title': 'Конструктор шаблонов отчетов', 'url': '/reports/templates/', 'code': '5000 / 1000', 'note': 'Столбцы, названия, фильтры, группировки, расчетные поля'},
            {'title': 'Суточный отчет заказчику', 'url': '/reports/customer-daily/', 'code': '5000 / 6000', 'note': 'Суточный отчет к 08:00 и Excel-выгрузка'},
            {'title': 'Отчет по механическим простоям', 'url': '/reports/downtimes/', 'code': '5000 / 6000 / 7000', 'note': 'Фильтры по датам, технике, причине, статусу и Excel'},
            {'title': 'Витрина руководства', 'url': '/reports/management/', 'code': '6000', 'note': 'Суточный срез, накопленная картина и показатели'},
            {'title': 'Excel-выгрузка витрины руководства', 'url': '/reports/management/export/', 'code': '6000', 'note': 'Сводка, динамика за 7 дней и сравнение день/ночь в Excel'},
        ],
    },
]


DEMO_ACCESS_CODES = [
    ('1000', 'Администратор'),
    ('2000', 'Водитель самосвала'),
    ('3000', 'Машинист экскаватора'),
    ('4000', 'Горный мастер'),
    ('5000', 'Диспетчер'),
    ('7000', 'Механик'),
    ('6000', 'Руководство'),
]


def interface_map_view(request):
    return render(
        request,
        'users/interface_map.html',
        {
            'interface_sections': INTERFACE_MAP,
            'demo_access_codes': DEMO_ACCESS_CODES,
        },
    )


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
    if access.role.code == 'mechanic':
        return redirect('mechanic_dashboard')
    if access.role.code == 'manager':
        return redirect('management_dashboard')
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

    last_closed_shift = EmployeeShift.objects.filter(
        equipment=registration.truck,
        closed_at__isnull=False,
    ).order_by('-closed_at').first()

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
        form_initial = {}
        if last_closed_shift:
            form_initial = {
                'start_fuel': last_closed_shift.end_fuel,
                'start_mileage': last_closed_shift.end_mileage,
                'start_engine_hours': last_closed_shift.end_engine_hours,
            }
        form = DriverOpenShiftForm(initial=form_initial)

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
            'close_form': DriverCloseShiftForm(instance=open_shift) if open_shift else None,
            'last_closed_shift': last_closed_shift,
        },
    )


def driver_close_shift_view(request):
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
    if not open_shift:
        messages.error(request, 'Открытая смена не найдена.')
        return redirect('driver_shift')

    if request.method == 'POST':
        form = DriverCloseShiftForm(request.POST, instance=open_shift)
        if form.is_valid():
            shift = form.save(commit=False)
            shift.closed_at = timezone.now()
            shift.closed_by = access.employee
            shift.save(update_fields=['end_fuel', 'end_mileage', 'end_engine_hours', 'closed_at', 'closed_by'])
            messages.success(request, 'Смена закрыта.')
    return redirect('driver_shift')


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
