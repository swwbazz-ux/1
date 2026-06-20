import secrets
from datetime import datetime
from io import BytesIO

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect, render
from django.utils import timezone
from openpyxl import Workbook

from assignments.models import AssignmentStatus, HaulAssignment
from references.models import Dormitory, DormitorySection, DumpPoint, Equipment, EquipmentType, RockType
from reports.models import ReportTemplate
from shifts.models import EmployeeShift
from trips.models import Trip, TripStatus

from .forms import (
    AdminAccessBlockForm,
    AccessActivationForm,
    AdminAccessRoleForm,
    AdminEmployeeEditForm,
    AdminEmployeeForm,
    DriverCloseShiftForm,
    DriverOpenShiftForm,
    DriverPrimaryRegistrationForm,
    is_valid_russian_mobile_phone,
    normalize_phone,
)
from .models import AdminActionLog, AdminConflict, DriverPrimaryRegistration, Employee, EmployeeAccess, Role


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
            {'title': 'Админка MVP', 'url': '/system-admin/', 'code': '1000', 'note': 'Сотрудники, доступы, справочники, конфликты и выгрузки'},
            {'title': 'Сотрудники админки', 'url': '/system-admin/employees/', 'code': '1000', 'note': 'Список сотрудников, фильтр по статусу, карточки и Excel'},
            {'title': 'Справочники админки', 'url': '/system-admin/references/', 'code': '1000', 'note': 'Единый реестр справочников первого этапа'},
            {'title': 'Конфликты админки', 'url': '/system-admin/conflicts/', 'code': '1000', 'note': 'Заблокированные рискованные действия и причины'},
            {'title': 'Журнал действий админки', 'url': '/system-admin/logs/', 'code': '1000', 'note': 'История важных административных действий'},
            {'title': 'Django-админка', 'url': '/admin/', 'code': 'администратор Django', 'note': 'Техническое управление справочниками и данными'},
        ],
    },
    {
        'section': 'Рабочие интерфейсы',
        'items': [
            {'title': 'Водитель самосвала', 'url': '/driver/shift/', 'code': '2000', 'note': 'Открытие/закрытие смены, активный рейс, подтверждение назначения'},
            {'title': 'Первичная регистрация водителя', 'url': '/driver/registration/', 'code': '2000', 'note': 'Первичное заполнение данных проживания; смена и техника выбираются при открытии смены'},
            {'title': 'Машинист экскаватора', 'url': '/excavator/work/', 'code': '3000', 'note': 'Создание рейса и параметры для отчета заказчику'},
            {'title': 'Горный мастер', 'url': '/mining-master/assignments/', 'code': '4000', 'note': 'Назначение самосвалов под экскаваторы'},
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
            {'title': 'Чеклист пилотной проверки отчетов', 'url': '/reports/pilot-checklist/', 'code': '5000 / 6000 / 1000', 'note': 'Рабочая навигация перед пилотом: экраны, Excel-выгрузки и вопросы для сверки с текущими отчетами'},
            {'title': 'Сценарий пилотного запуска', 'url': '/reports/pilot-scenario/', 'code': '5000 / 6000 / 1000', 'note': 'Пошаговая проверка пилота по ролям: от расстановки и рейса до отчетов и витрины'},
            {'title': 'Журнал замечаний пилота', 'url': '/reports/pilot-feedback/', 'code': '5000 / 6000 / 1000', 'note': 'Фиксация замечаний, приоритетов, решений и переносов во время пилотной проверки'},
        ],
    },
]


DEMO_ACCESS_CODES = [
    ('+79000000001', '100000', 'Администратор'),
    ('+79000000002', '200000', 'Водитель самосвала'),
    ('+79000000003', '300000', 'Машинист экскаватора'),
    ('+79000000004', '400000', 'Горный мастер'),
    ('+79000000005', '500000', 'Диспетчер'),
    ('+79000000007', '700000', 'Механик'),
    ('+79000000006', '600000', 'Руководство'),
]


def get_current_access(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return None
    return (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(
            id=access_id,
            is_active=True,
            employee__is_active=True,
            role__is_active=True,
        )
        .exclude(status__in=[EmployeeAccess.Status.BLOCKED, EmployeeAccess.Status.DEACTIVATED])
        .first()
    )


def require_admin_access(request):
    access = get_current_access(request)
    if not access:
        return None
    if access.role.code != 'admin':
        return None
    return access


def generate_unique_access_code():
    while True:
        code = ''.join(str(secrets.randbelow(10)) for _ in range(6))
        if not EmployeeAccess.objects.filter(access_code=code).exists():
            return code


def log_admin_action(actor, action, obj=None, old_value='', new_value='', comment=''):
    AdminActionLog.objects.create(
        actor=actor,
        action=action,
        object_type=obj.__class__.__name__ if obj else '',
        object_repr=str(obj) if obj else '',
        old_value=old_value,
        new_value=new_value,
        comment=comment,
    )


def build_workbook_response(workbook, filename):
    output = BytesIO()
    workbook.save(output)
    response = HttpResponse(
        output.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def excel_value(value):
    if isinstance(value, datetime) and timezone.is_aware(value):
        return timezone.localtime(value).replace(tzinfo=None)
    return value


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
        phone = request.POST.get('phone', '').strip()
        normalized_phone = normalize_phone(phone)
        access_code = request.POST.get('access_code', '').strip()
        if not access_code.isdigit():
            messages.error(request, 'Телефон или пинкод указаны неверно.')
            return render(request, 'users/login.html')
        access_candidates = (
            EmployeeAccess.objects
            .select_related('employee', 'role')
            .filter(access_code=access_code, is_active=True, employee__is_active=True, role__is_active=True)
        )
        access = None
        for candidate in access_candidates:
            employee_phone = normalize_phone(candidate.employee.phone)
            if employee_phone and is_valid_russian_mobile_phone(phone) and len(access_code) == 6 and normalized_phone == employee_phone:
                access = candidate
                break
            if not employee_phone and not normalized_phone:
                access = candidate
                break
        if access:
            access.last_login_at = timezone.now()
            if access.status == EmployeeAccess.Status.NOT_ACTIVATED:
                if access.primary_code_issued_at:
                    request.session['pending_activation_access_id'] = access.id
                    access.save(update_fields=['last_login_at'])
                    return redirect('activate_access')
                access.status = EmployeeAccess.Status.ACTIVATED
                access.activated_at = timezone.now()
                if access.employee.status == Employee.Status.NOT_ACTIVATED:
                    access.employee.status = Employee.Status.ACTIVE
                    access.employee.is_active = True
                    access.employee.save(update_fields=['status', 'is_active', 'updated_at'])
            request.session['employee_access_id'] = access.id
            access.save(update_fields=['last_login_at', 'status', 'activated_at'])
            return redirect('role_home')
        messages.error(request, 'Телефон или пинкод указаны неверно.')
    return render(request, 'users/login.html')


def activate_access_view(request):
    access_id = request.session.get('pending_activation_access_id')
    if not access_id:
        return redirect('login')
    access = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(id=access_id, is_active=True, status=EmployeeAccess.Status.NOT_ACTIVATED)
        .first()
    )
    if not access:
        request.session.pop('pending_activation_access_id', None)
        return redirect('login')

    if request.method == 'POST':
        form = AccessActivationForm(request.POST, access=access)
        if form.is_valid():
            access.access_code = form.cleaned_data['new_access_code']
            access.status = EmployeeAccess.Status.ACTIVATED
            access.activated_at = timezone.now()
            access.last_login_at = timezone.now()
            access.save(update_fields=['access_code', 'status', 'activated_at', 'last_login_at'])
            if access.employee.status == Employee.Status.NOT_ACTIVATED:
                access.employee.status = Employee.Status.ACTIVE
                access.employee.is_active = True
                access.employee.save(update_fields=['status', 'is_active', 'updated_at'])
            request.session.pop('pending_activation_access_id', None)
            request.session['employee_access_id'] = access.id
            messages.success(request, 'Постоянный пинкод создан. Первичный пинкод больше не действует.')
            return redirect('role_home')
    else:
        form = AccessActivationForm(access=access)

    return render(
        request,
        'users/activate_access.html',
        {
            'access': access,
            'form': form,
        },
    )


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
    if access.role.code == 'admin':
        return redirect('system_admin_dashboard')
    interface_name = ROLE_INTERFACE_NAMES.get(access.role.code, f'Интерфейс роли: {access.role.name}')
    return render(
        request,
        'users/role_home.html',
        {
            'access': access,
            'interface_name': interface_name,
        },
    )


def system_admin_dashboard_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')

    employee_status_counts = {
        item['status']: item['total']
        for item in Employee.objects.values('status').annotate(total=Count('id'))
    }
    access_status_counts = {
        item['status']: item['total']
        for item in EmployeeAccess.objects.values('status').annotate(total=Count('id'))
    }
    reference_counts = [
        ('Виды техники', EquipmentType.objects.count(), '/admin/references/equipmenttype/'),
        ('Техника', Equipment.objects.count(), '/admin/references/equipment/'),
        ('Породы', RockType.objects.count(), '/admin/references/rocktype/'),
        ('Точки разгрузки', DumpPoint.objects.count(), '/admin/references/dumppoint/'),
        ('Общежития', Dormitory.objects.count(), '/admin/references/dormitory/'),
        ('Секции общежитий', DormitorySection.objects.count(), '/admin/references/dormitorysection/'),
        ('Шаблоны отчетов', ReportTemplate.objects.count(), '/reports/templates/'),
    ]

    return render(
        request,
        'users/system_admin_dashboard.html',
        {
            'access': access,
            'employee_total': Employee.objects.count(),
            'active_total': employee_status_counts.get(Employee.Status.ACTIVE, 0),
            'not_activated_total': access_status_counts.get(EmployeeAccess.Status.NOT_ACTIVATED, 0),
            'blocked_total': access_status_counts.get(EmployeeAccess.Status.BLOCKED, 0),
            'deactivated_total': access_status_counts.get(EmployeeAccess.Status.DEACTIVATED, 0),
            'recent_employees': Employee.objects.order_by('-created_at')[:5],
            'recent_accesses': EmployeeAccess.objects.select_related('employee', 'role').order_by('-last_login_at', '-created_at')[:5],
            'recent_logs': AdminActionLog.objects.select_related('actor')[:8],
            'open_conflicts': AdminConflict.objects.select_related('employee', 'role').filter(status=AdminConflict.Status.OPEN)[:8],
            'reference_counts': reference_counts,
        },
    )


def system_admin_references_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')

    reference_sections = [
        {
            'title': 'Сотрудники и доступы',
            'items': [
                {'name': 'Сотрудники', 'count': Employee.objects.count(), 'url': 'system_admin_employees', 'external_url': ''},
                {'name': 'Роли', 'count': Role.objects.count(), 'url': '', 'external_url': '/admin/users/role/'},
                {'name': 'Доступы', 'count': EmployeeAccess.objects.count(), 'url': '', 'external_url': '/admin/users/employeeaccess/'},
            ],
        },
        {
            'title': 'Техника',
            'items': [
                {'name': 'Виды техники', 'count': EquipmentType.objects.count(), 'url': '', 'external_url': '/admin/references/equipmenttype/'},
                {'name': 'Техника', 'count': Equipment.objects.count(), 'url': '', 'external_url': '/admin/references/equipment/'},
            ],
        },
        {
            'title': 'Производственные справочники',
            'items': [
                {'name': 'Породы', 'count': RockType.objects.count(), 'url': '', 'external_url': '/admin/references/rocktype/'},
                {'name': 'Точки разгрузки', 'count': DumpPoint.objects.count(), 'url': '', 'external_url': '/admin/references/dumppoint/'},
                {'name': 'Шаблоны отчетов', 'count': ReportTemplate.objects.count(), 'url': '', 'external_url': '/reports/templates/'},
            ],
        },
        {
            'title': 'Проживание',
            'items': [
                {'name': 'Общежития', 'count': Dormitory.objects.count(), 'url': '', 'external_url': '/admin/references/dormitory/'},
                {'name': 'Секции общежитий', 'count': DormitorySection.objects.count(), 'url': '', 'external_url': '/admin/references/dormitorysection/'},
            ],
        },
    ]

    return render(
        request,
        'users/system_admin_references.html',
        {
            'access': access,
            'reference_sections': reference_sections,
        },
    )


def system_admin_conflicts_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')

    status = request.GET.get('status', '').strip()
    conflicts = AdminConflict.objects.select_related('employee', 'role').order_by('-created_at')
    if status:
        conflicts = conflicts.filter(status=status)

    return render(
        request,
        'users/system_admin_conflicts.html',
        {
            'access': access,
            'conflicts': conflicts,
            'statuses': AdminConflict.Status.choices,
            'selected_status': status,
        },
    )


def system_admin_conflict_action_view(request, conflict_id, action):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')

    conflict = get_object_or_404(AdminConflict, id=conflict_id)
    if request.method == 'POST':
        status_by_action = {
            'in-progress': AdminConflict.Status.IN_PROGRESS,
            'resolved': AdminConflict.Status.RESOLVED,
            'rejected': AdminConflict.Status.REJECTED,
        }
        new_status = status_by_action.get(action)
        if new_status:
            old_status = conflict.get_status_display()
            conflict.status = new_status
            conflict.resolved_by = access.employee
            conflict.resolved_at = timezone.now()
            conflict.save(update_fields=['status', 'resolved_by', 'resolved_at'])
            log_admin_action(
                access.employee,
                'Изменен статус административного конфликта',
                conflict,
                old_value=old_status,
                new_value=conflict.get_status_display(),
            )
            messages.success(request, 'Статус конфликта обновлен.')

    redirect_url = request.POST.get('next') or 'system_admin_conflicts'
    if redirect_url == 'dashboard':
        return redirect('system_admin_dashboard')
    return redirect('system_admin_conflicts')


def system_admin_logs_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')

    query = request.GET.get('q', '').strip()
    logs = AdminActionLog.objects.select_related('actor').order_by('-created_at')
    if query:
        logs = logs.filter(
            Q(action__icontains=query)
            | Q(object_type__icontains=query)
            | Q(object_repr__icontains=query)
            | Q(comment__icontains=query)
            | Q(actor__full_name__icontains=query)
        )

    return render(
        request,
        'users/system_admin_logs.html',
        {
            'access': access,
            'logs': logs[:200],
            'query': query,
        },
    )


def system_admin_employees_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')

    employees = Employee.objects.prefetch_related('accesses__role').order_by('full_name')
    status = request.GET.get('status', '').strip()
    access_status = request.GET.get('access_status', '').strip()
    query = request.GET.get('q', '').strip()
    if status:
        employees = employees.filter(status=status)
    if access_status:
        employees = employees.filter(accesses__status=access_status).distinct()
    if query:
        employees = employees.filter(full_name__icontains=query)

    return render(
        request,
        'users/system_admin_employees.html',
        {
            'access': access,
            'employees': employees,
            'statuses': Employee.Status.choices,
            'access_statuses': EmployeeAccess.Status.choices,
            'selected_status': status,
            'selected_access_status': access_status,
            'query': query,
        },
    )


def system_admin_employee_create_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')

    if request.method == 'POST':
        form = AdminEmployeeForm(request.POST, request.FILES)
        if form.is_valid():
            employee = form.save()
            role = form.cleaned_data['role']
            if form.cleaned_data['generate_access']:
                code = generate_unique_access_code()
                EmployeeAccess.objects.create(
                    employee=employee,
                    role=role,
                    access_code=code,
                    status=EmployeeAccess.Status.NOT_ACTIVATED,
                    primary_code_issued_at=timezone.now(),
                )
                log_admin_action(access.employee, 'Создан сотрудник и выдан первичный пинкод', employee, new_value=f'Роль: {role}; пинкод: {code}')
                messages.success(request, f'Сотрудник создан. Первичный пинкод: {code}')
            else:
                log_admin_action(access.employee, 'Создан сотрудник без пинкода', employee, new_value=f'Роль: {role}')
                messages.success(request, 'Сотрудник создан.')
            return redirect('system_admin_employee_detail', employee_id=employee.id)
    else:
        form = AdminEmployeeForm()

    return render(request, 'users/system_admin_employee_form.html', {'access': access, 'form': form, 'title': 'Создать сотрудника'})


def system_admin_employee_detail_view(request, employee_id):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')

    employee = get_object_or_404(Employee, id=employee_id)
    if request.method == 'POST':
        old_photo_name = employee.photo.name if employee.photo else ''
        if request.POST.get('remove_photo') == '1':
            if old_photo_name:
                employee.photo.storage.delete(old_photo_name)
                employee.photo = ''
                employee.save(update_fields=['photo', 'updated_at'])
                log_admin_action(access.employee, 'Удалено фото сотрудника', employee)
                messages.success(request, 'Фото сотрудника удалено.')
            return redirect('system_admin_employee_detail', employee_id=employee.id)
        form = AdminEmployeeEditForm(request.POST, request.FILES, instance=employee)
        if form.is_valid():
            saved_employee = form.save()
            if request.FILES.get('photo') and old_photo_name and old_photo_name != saved_employee.photo.name:
                saved_employee.photo.storage.delete(old_photo_name)
            log_admin_action(access.employee, 'Изменена карточка сотрудника', employee)
            messages.success(request, 'Карточка сотрудника сохранена.')
            return redirect('system_admin_employee_detail', employee_id=employee.id)
    else:
        form = AdminEmployeeEditForm(instance=employee)

    employee_accesses = employee.accesses.select_related('role').order_by('role__name')
    current_role_access = (
        employee_accesses
        .filter(is_active=True)
        .exclude(status=EmployeeAccess.Status.DEACTIVATED)
        .order_by('status', 'role__name')
        .first()
        or employee_accesses.first()
    )
    role_form_initial = {'role': current_role_access.role_id} if current_role_access else None

    return render(
        request,
        'users/system_admin_employee_detail.html',
        {
            'access': access,
            'employee': employee,
            'form': form,
            'role_form': AdminAccessRoleForm(initial=role_form_initial),
            'block_form': AdminAccessBlockForm(),
            'employee_accesses': employee_accesses,
            'current_role_access': current_role_access,
            'logs': AdminActionLog.objects.filter(object_repr=str(employee))[:10],
        },
    )


def system_admin_generate_access_view(request, employee_id):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')
    employee = get_object_or_404(Employee, id=employee_id)
    if request.method == 'POST':
        form = AdminAccessRoleForm(request.POST)
        if form.is_valid():
            role = form.cleaned_data['role']
            code = generate_unique_access_code()
            employee_access, _created = EmployeeAccess.objects.update_or_create(
                employee=employee,
                role=role,
                defaults={
                    'access_code': code,
                    'status': EmployeeAccess.Status.NOT_ACTIVATED,
                    'is_active': True,
                    'primary_code_issued_at': timezone.now(),
                    'activated_at': None,
                    'deactivated_at': None,
                    'blocked_at': None,
                    'block_reason': '',
                },
            )
            log_admin_action(access.employee, 'Выдан новый первичный пинкод', employee_access, new_value=code)
            messages.success(request, f'Новый первичный пинкод: {code}')
    return redirect('system_admin_employee_detail', employee_id=employee.id)


def system_admin_access_action_view(request, access_id, action):
    admin_access = require_admin_access(request)
    if not admin_access:
        return redirect('role_home')
    employee_access = get_object_or_404(EmployeeAccess.objects.select_related('employee'), id=access_id)
    if request.method == 'POST':
        if employee_access.id == admin_access.id and action in {'block', 'deactivate'}:
            messages.error(request, 'Нельзя заблокировать или деактивировать собственный доступ администратора.')
            return redirect('system_admin_employee_detail', employee_id=employee_access.employee.id)
        if action == 'block':
            form = AdminAccessBlockForm(request.POST)
            if form.is_valid():
                employee_access.status = EmployeeAccess.Status.BLOCKED
                employee_access.is_active = False
                employee_access.blocked_at = timezone.now()
                employee_access.block_reason = form.cleaned_data['reason']
                employee_access.save(update_fields=['status', 'is_active', 'blocked_at', 'block_reason'])
                log_admin_action(admin_access.employee, 'Заблокирован доступ', employee_access, comment=employee_access.block_reason)
                messages.success(request, 'Доступ заблокирован.')
        elif action == 'unblock':
            employee_access.status = EmployeeAccess.Status.ACTIVATED
            employee_access.is_active = True
            employee_access.blocked_at = None
            employee_access.block_reason = ''
            employee_access.save(update_fields=['status', 'is_active', 'blocked_at', 'block_reason'])
            log_admin_action(admin_access.employee, 'Разблокирован доступ', employee_access)
            messages.success(request, 'Доступ разблокирован.')
        elif action == 'deactivate':
            employee_access.status = EmployeeAccess.Status.DEACTIVATED
            employee_access.is_active = False
            employee_access.deactivated_at = timezone.now()
            employee_access.save(update_fields=['status', 'is_active', 'deactivated_at'])
            log_admin_action(admin_access.employee, 'Доступ деактивирован', employee_access)
            messages.success(request, 'Доступ деактивирован.')
    return redirect('system_admin_employee_detail', employee_id=employee_access.employee.id)


def system_admin_employee_status_action_view(request, employee_id, action):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')
    employee = get_object_or_404(Employee, id=employee_id)
    if request.method == 'POST':
        if employee.id == access.employee.id and action in {'deactivate', 'archive', 'delete'}:
            messages.error(request, 'Нельзя деактивировать, архивировать или удалить собственную учетную запись администратора.')
            return redirect('system_admin_employee_detail', employee_id=employee.id)
        if action == 'deactivate':
            employee.status = Employee.Status.DEACTIVATED
            employee.is_active = False
            employee.accesses.update(status=EmployeeAccess.Status.DEACTIVATED, is_active=False, deactivated_at=timezone.now())
            messages.success(request, 'Сотрудник деактивирован.')
            log_admin_action(access.employee, 'Сотрудник деактивирован', employee)
        elif action == 'archive':
            employee.status = Employee.Status.ARCHIVED
            employee.is_active = False
            employee.accesses.update(status=EmployeeAccess.Status.DEACTIVATED, is_active=False, deactivated_at=timezone.now())
            messages.success(request, 'Сотрудник отправлен в архив.')
            log_admin_action(access.employee, 'Сотрудник отправлен в архив', employee)
        elif action == 'delete':
            if employee.has_production_history():
                AdminConflict.objects.create(
                    employee=employee,
                    role=employee.accesses.select_related('role').first().role if employee.accesses.exists() else None,
                    conflict_type='Попытка удаления сотрудника с историей',
                    process='Админка MVP',
                    description='Полное удаление заблокировано: у сотрудника есть смены, рейсы, простои, назначения или диспетчерские действия.',
                )
                messages.error(request, 'Удаление запрещено: у сотрудника есть производственная история. Используйте архив.')
                log_admin_action(access.employee, 'Удаление сотрудника заблокировано', employee)
                return redirect('system_admin_employee_detail', employee_id=employee.id)
            employee_name = employee.full_name
            log_admin_action(access.employee, 'Сотрудник полностью удален', employee, old_value=employee_name)
            employee.delete()
            messages.success(request, f'Сотрудник {employee_name} удален.')
            return redirect('system_admin_employees')
    employee.save(update_fields=['status', 'is_active', 'updated_at'])
    return redirect('system_admin_employee_detail', employee_id=employee.id)


def system_admin_employee_export_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Сотрудники'
    sheet.append(['ФИО', 'Табельный номер', 'Телефон', 'Статус', 'Дата приема', 'Дата увольнения', 'Вахта', 'Место проживания'])
    for employee in Employee.objects.order_by('full_name'):
        sheet.append([
            employee.full_name,
            employee.personnel_number,
            employee.phone,
            employee.get_status_display(),
            excel_value(employee.hired_at),
            excel_value(employee.dismissed_at),
            employee.rotation,
            employee.residence_text,
        ])
    return build_workbook_response(workbook, 'admin_employees.xlsx')


def system_admin_access_export_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Доступы'
    sheet.append(['Сотрудник', 'Роль', 'Статус доступа', 'Дата выдачи', 'Дата активации', 'Последний вход'])
    for employee_access in EmployeeAccess.objects.select_related('employee', 'role').order_by('employee__full_name'):
        sheet.append([
            employee_access.employee.full_name,
            employee_access.role.name,
            employee_access.get_status_display(),
            excel_value(employee_access.primary_code_issued_at),
            excel_value(employee_access.activated_at),
            excel_value(employee_access.last_login_at),
        ])
    return build_workbook_response(workbook, 'admin_accesses.xlsx')


def system_admin_log_export_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Журнал действий'
    sheet.append(['Дата', 'Кто', 'Действие', 'Тип объекта', 'Объект', 'Комментарий'])
    for log in AdminActionLog.objects.select_related('actor').order_by('-created_at'):
        sheet.append([excel_value(log.created_at), log.actor.full_name if log.actor else '', log.action, log.object_type, log.object_repr, log.comment])
    return build_workbook_response(workbook, 'admin_action_log.xlsx')


def system_admin_conflict_export_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Конфликты'
    sheet.append(['Дата', 'Сотрудник', 'Роль', 'Тип', 'Процесс', 'Статус', 'Описание'])
    for conflict in AdminConflict.objects.select_related('employee', 'role').order_by('-created_at'):
        sheet.append([
            excel_value(conflict.created_at),
            conflict.employee.full_name if conflict.employee else '',
            conflict.role.name if conflict.role else '',
            conflict.conflict_type,
            conflict.process,
            conflict.get_status_display(),
            conflict.description,
        ])
    return build_workbook_response(workbook, 'admin_conflicts.xlsx')


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
    current_truck = open_shift.equipment if open_shift else None
    pending_assignment = None
    active_trip = None
    if current_truck:
        pending_assignment = HaulAssignment.objects.filter(
            truck=current_truck,
            status=AssignmentStatus.PENDING,
            ended_at__isnull=True,
        ).select_related('truck', 'excavator').order_by('-assigned_at').first()
        active_trip = Trip.objects.filter(
            truck=current_truck,
            status=TripStatus.ACTIVE,
        ).select_related('truck', 'excavator', 'rock_type', 'dump_point').order_by('-created_at').first()

    selected_truck_id = request.POST.get('truck') if request.method == 'POST' else request.GET.get('truck')
    last_closed_shift = None
    if selected_truck_id:
        last_closed_shift = EmployeeShift.objects.filter(
            equipment_id=selected_truck_id,
            closed_at__isnull=False,
        ).order_by('-closed_at').first()

    if request.method == 'POST' and not open_shift:
        form = DriverOpenShiftForm(request.POST, employee=access.employee)
        if form.is_valid():
            shift = form.save(commit=False)
            shift.employee = access.employee
            shift.opened_by = access.employee
            shift.shift_type = form.cleaned_data['shift_type']
            shift.equipment = form.cleaned_data['truck']
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
        if selected_truck_id:
            form_initial['truck'] = selected_truck_id
        form = DriverOpenShiftForm(initial=form_initial, employee=access.employee)

    return render(
        request,
        'users/driver_shift.html',
        {
            'access': access,
            'registration': registration,
            'current_truck': current_truck,
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
    open_shift = EmployeeShift.objects.filter(employee=access.employee, closed_at__isnull=True).order_by('-opened_at').first()
    if not open_shift or not open_shift.equipment:
        messages.error(request, 'Нельзя принять назначение: открытая смена с самосвалом не найдена.')
        return redirect('driver_shift')

    assignment = HaulAssignment.objects.filter(
        id=assignment_id,
        truck=open_shift.equipment,
        status=AssignmentStatus.PENDING,
    ).first()
    if assignment and request.method == 'POST':
        assignment.status = AssignmentStatus.ACCEPTED
        assignment.accepted_at = timezone.now()
        assignment.save(update_fields=['status', 'accepted_at'])
        messages.success(request, 'Назначение принято.')
    return redirect('driver_shift')

# Create your views here.
