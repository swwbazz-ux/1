import secrets
import json
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.forms import modelform_factory
from django.forms.models import construct_instance
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from openpyxl import Workbook

from assignments.models import AssignmentStatus, ExcavatorPlacement, HaulAssignment, HaulAssignmentAction
from assignments.services import (
    WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES,
    apply_pending_haul_assignment,
    clear_active_equipment_assignment,
    get_active_equipment_assignment,
    reconcile_due_haul_assignments,
    work_assignment_state,
)
from core.models import OperationalStateEvent, bump_operational_state
from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import Dormitory, DormitorySection, DumpPoint, Equipment, EquipmentState, EquipmentType, RockType
from reports.models import ReportTemplate
from shifts.forms import EquipmentPlanGroupForm
from shifts.models import AchievementPrize, EmployeeShift, EquipmentPlanGroup, EquipmentShiftPlan, PlanAssignmentStatus, PlanCalculationMode, ShiftPlan, ShiftPlanScope
from shifts.services import (
    calculate_truck_shift_progress,
    close_driver_shift,
    open_driver_shift,
    plan_status_label,
    plan_unit_label,
    progress_cycle_visual_context,
)
from trips.models import DispatcherActionLog, OPEN_TRIP_STATUSES, Trip, TripClientAction, TripStatus

from .access_auth import find_employee_access_by_credentials
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
from .oup_undo import (
    get_oup_action_undo_state,
    undo_oup_action,
)
from .role_apps import (
    get_role_app_for_request,
    role_app_manifest_response,
    role_app_service_worker_response,
)
from .session_device import (
    detect_session_device_kind,
    get_session_device_kind,
    set_session_device_kind,
)


ROLE_INTERFACE_NAMES = {
    'admin': 'Админка',
    'driver': 'Интерфейс водителя самосвала',
    'excavator_operator': 'Интерфейс машиниста экскаватора',
    'mining_master': 'Интерфейс горного мастера',
    'deputy_mining_manager': 'Планирование смены зам. начальника горного участка',
    'oup': 'Рабочее место ОУП',
    'dispatcher': 'Диспетчерский экран',
    'mechanic': 'Интерфейс механика',
    'manager': 'Витрина руководства',
}


ADMIN_RESTORABLE_EMPLOYEE_STATUSES = {
    Employee.Status.DEACTIVATED,
    Employee.Status.ARCHIVED,
    Employee.Status.DISMISSED,
    Employee.Status.DELETED,
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
            {'title': 'Работа водителя самосвала', 'url': '/driver/', 'code': '2000', 'note': 'Главный PWA-экран Работа, смена, простои и путевка'},
            {'title': 'Первичная регистрация водителя', 'url': '/driver/registration/', 'code': '2000', 'note': 'Первичное заполнение данных проживания; смена и техника выбираются при открытии смены'},
            {'title': 'Машинист экскаватора', 'url': '/excavator/work/', 'code': '3000', 'note': 'Создание рейса и параметры для отчета заказчику'},
            {'title': 'Горный мастер', 'url': '/mining-master/assignments/', 'code': '4000', 'note': 'Назначение самосвалов под экскаваторы'},
            {'title': 'Зам. начальника горного участка', 'url': '/deputy-mining-manager/', 'code': 'роль зам. начальника', 'note': 'Расстановка сотрудников по технике на две смены'},
            {'title': 'Отдел управления персоналом', 'url': '/oup/', 'code': '800000 / роль ОУП', 'note': 'Создание, ведение и увольнение сотрудников'},
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


DRIVER_SHELL_VERSION = 'driver-mobile-shell-v99'

DRIVER_MANIFEST = {
    'id': '/driver/',
    'name': 'Водитель самосвала',
    'short_name': 'Водитель',
    'description': 'Мобильное рабочее место водителя самосвала: работа, смена, простои и путевка.',
    'start_url': '/driver/',
    'scope': '/driver/',
    'display': 'standalone',
    'display_override': ['standalone', 'fullscreen'],
    'orientation': 'portrait',
    'background_color': '#030708',
    'theme_color': '#030708',
    'categories': ['business', 'productivity'],
    'icons': [
        {
            'src': '/static/img/pwa/driver-180.png',
            'sizes': '180x180',
            'type': 'image/png',
        },
        {
            'src': '/static/img/pwa/driver-192.png',
            'sizes': '192x192',
            'type': 'image/png',
        },
        {
            'src': '/static/img/pwa/driver-512.png',
            'sizes': '512x512',
            'type': 'image/png',
        },
        {
            'src': '/static/img/pwa/driver-maskable-512.png',
            'sizes': '512x512',
            'type': 'image/png',
            'purpose': 'maskable',
        },
    ],
}

DRIVER_SERVICE_WORKER_JS = f"""
const CACHE_NAME = "{DRIVER_SHELL_VERSION}";
const CACHE_PREFIX = "driver-mobile-shell-";
const APP_SHELL_URL = "/driver/";
const LEGACY_SHELL_URL = "/driver/shift/";
const MANIFEST_URL = "/driver.webmanifest";
const CORE_ASSETS = [
    APP_SHELL_URL,
    LEGACY_SHELL_URL,
    MANIFEST_URL,
    "/static/css/app.css",
    "/static/js/realtime-client.js",
    "/static/favicon.ico",
    "/static/img/equipment/truck-green.png",
    "/static/img/equipment/excavator-green.png",
    "/static/img/pwa/driver-180.png",
    "/static/img/pwa/driver-192.png",
    "/static/img/pwa/driver-512.png",
    "/static/img/pwa/driver-maskable-512.png"
];

self.addEventListener("install", (event) => {{
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.addAll(CORE_ASSETS))
    );
    self.skipWaiting();
}});

self.addEventListener("activate", (event) => {{
    event.waitUntil(
        caches.keys().then((keys) => Promise.all(
            keys
                .filter((key) => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME)
                .map((key) => caches.delete(key))
        )).then(() => self.clients.claim())
    );
}});

async function networkFirst(request, fallbackUrl) {{
    const cache = await caches.open(CACHE_NAME);
    try {{
        const freshRequest = new Request(request, {{ cache: "no-store" }});
        const response = await fetch(freshRequest);
        if (response && response.ok) {{
            cache.put(request, response.clone());
        }}
        return response;
    }} catch (error) {{
        return (await cache.match(request)) || (fallbackUrl ? cache.match(fallbackUrl) : undefined) || Response.error();
    }}
}}

async function cacheFirst(request) {{
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(request, {{ ignoreSearch: true }});
    if (cached) {{
        return cached;
    }}
    const response = await fetch(request);
    if (response && response.ok) {{
        cache.put(request, response.clone());
    }}
    return response;
}}

self.addEventListener("fetch", (event) => {{
    const request = event.request;
    if (request.method !== "GET") {{
        return;
    }}
    const url = new URL(request.url);
    if (url.origin !== self.location.origin) {{
        return;
    }}
    if (request.headers.get("x-requested-with") === "XMLHttpRequest") {{
        event.respondWith(fetch(request));
        return;
    }}
    if (request.mode === "navigate" || url.pathname === APP_SHELL_URL || url.pathname === LEGACY_SHELL_URL) {{
        event.respondWith(networkFirst(request, APP_SHELL_URL));
        return;
    }}
    if (url.pathname === MANIFEST_URL) {{
        event.respondWith(networkFirst(request, MANIFEST_URL));
        return;
    }}
    if (url.pathname.startsWith("/static/")) {{
        event.respondWith(cacheFirst(request));
    }}
}});

self.addEventListener("message", (event) => {{
    if (!event.data || !event.data.type) {{
        return;
    }}
    if (event.data.type === "SKIP_WAITING") {{
        self.skipWaiting();
    }}
    if (event.data.type === "GET_VERSION" && event.ports && event.ports[0]) {{
        event.ports[0].postMessage({{ version: CACHE_NAME }});
    }}
}});
""".strip()


def get_current_access(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return None
    access_queryset = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(
            id=access_id,
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
            employee__is_active=True,
            employee__status=Employee.Status.ACTIVE,
            role__is_active=True,
        )
    )
    role_app = get_role_app_for_request(request)
    if role_app:
        access_queryset = access_queryset.filter(role__code=role_app.role_code)
    return access_queryset.first()


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
        object_id=str(obj.pk) if obj and obj.pk else '',
        object_repr=str(obj) if obj else '',
        old_value=old_value,
        new_value=new_value,
        comment=comment,
    )



def redirect_after_admin_action(request, fallback_view, **kwargs):
    next_url = request.POST.get('next', '')
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect(fallback_view, **kwargs)

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


def driver_manifest_view(request):
    return role_app_manifest_response(request, 'driver')


def driver_service_worker_view(request):
    return role_app_service_worker_response(request, 'driver', DRIVER_SERVICE_WORKER_JS)


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
    role_app = get_role_app_for_request(request)
    if request.method == 'GET' and role_app:
        if get_current_access(request):
            return redirect('role_home')
        if request.session.get('employee_access_id'):
            request.session.flush()
    selected_device_kind = request.POST.get('device_kind') if request.method == 'POST' else detect_session_device_kind(request)
    if selected_device_kind not in {'personal', 'shared'}:
        selected_device_kind = detect_session_device_kind(request)
    if request.method == 'POST':
        phone = request.POST.get('phone', '').strip()
        access_code = request.POST.get('access_code', '').strip()
        access = find_employee_access_by_credentials(
            phone,
            access_code,
            role_code=role_app.role_code if role_app else None,
        )
        if access:
            request.session.cycle_key()
            access.last_login_at = timezone.now()
            if access.status == EmployeeAccess.Status.NOT_ACTIVATED:
                if access.primary_code_issued_at:
                    request.session['pending_activation_access_id'] = access.id
                    set_session_device_kind(request, selected_device_kind)
                    access.save(update_fields=['last_login_at'])
                    return redirect('activate_access')
                access.status = EmployeeAccess.Status.ACTIVATED
                access.activated_at = timezone.now()
                if access.employee.status == Employee.Status.NOT_ACTIVATED:
                    access.employee.status = Employee.Status.ACTIVE
                    access.employee.is_active = True
                    access.employee.save(update_fields=['status', 'is_active', 'updated_at'])
            request.session['employee_access_id'] = access.id
            set_session_device_kind(request, selected_device_kind)
            access.save(update_fields=['last_login_at', 'status', 'activated_at'])
            return redirect('role_home')
        if role_app:
            messages.error(
                request,
                f'Телефон или пинкод указаны неверно для приложения «{role_app.short_name}».',
            )
        else:
            messages.error(request, 'Телефон или пинкод указаны неверно.')
    return render(request, 'users/login.html', {'selected_device_kind': selected_device_kind})


def activate_access_view(request):
    access_id = request.session.get('pending_activation_access_id')
    if not access_id:
        return redirect('login')
    access_queryset = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(id=access_id, is_active=True, status=EmployeeAccess.Status.NOT_ACTIVATED)
    )
    role_app = get_role_app_for_request(request)
    if role_app:
        access_queryset = access_queryset.filter(role__code=role_app.role_code)
    access = access_queryset.first()
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
            request.session.cycle_key()
            request.session['employee_access_id'] = access.id
            set_session_device_kind(request, get_session_device_kind(request))
            if access.role.code != 'oup':
                messages.success(
                    request,
                    'Постоянный пинкод создан. Первичный пинкод больше не действует.',
                )
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
    access = get_current_access(request)
    if not access:
        request.session.flush()
        return redirect('login')
    if access.role.code == 'driver':
        if not hasattr(access.employee, 'driver_registration'):
            return redirect('driver_registration')
        return redirect('driver_work')
    if access.role.code == 'mining_master':
        return redirect('mining_master_assignments')
    if access.role.code == 'deputy_mining_manager':
        return redirect('deputy_mining_manager_placement')
    if access.role.code == 'oup':
        return redirect('oup_home')
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
        ('Состояния техники', EquipmentState.objects.count(), '/admin/references/equipmentstate/'),
        ('Причины простоев', DowntimeReason.objects.count(), '/admin/downtimes/downtimereason/'),
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
            'shift_fact_total': Trip.objects.count() + DowntimeEvent.objects.count(),
        },
    )


@require_POST
def system_admin_reset_shift_test_data_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')

    deleted_counts = {
        'рейсы': Trip.objects.count(),
        'простои': DowntimeEvent.objects.count(),
        'оперативные события': OperationalStateEvent.objects.count(),
        'клиентские действия рейсов': TripClientAction.objects.count(),
        'диспетчерские журналы действий': DispatcherActionLog.objects.count(),
    }

    with transaction.atomic():
        TripClientAction.objects.all().delete()
        DispatcherActionLog.objects.all().delete()
        Trip.objects.all().delete()
        DowntimeEvent.objects.all().delete()
        OperationalStateEvent.objects.all().delete()
        bump_operational_state(
            'SystemAdmin:test_shift_data_reset',
            event_type='test_shift_data_reset',
            object_type='SystemAdmin',
            payload={'action': 'test_shift_data_reset', 'deleted_counts': deleted_counts},
        )
        log_admin_action(
            access.employee,
            'Сброшены тестовые показатели смены',
            new_value=json.dumps(deleted_counts, ensure_ascii=False),
            comment='Удалены только рейсы, простои, оперативные события и журналы действий. Справочники, сотрудники, техника и планы сохранены.',
        )

    deleted_total = sum(deleted_counts.values())
    messages.success(request, f'Тестовые показатели смены сброшены. Удалено записей: {deleted_total}.')
    return redirect('system_admin_dashboard')


def system_admin_references_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')

    reference_configs = get_system_admin_reference_configs()
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
                {'name': 'Виды техники', 'count': EquipmentType.objects.count(), 'url': '', 'external_url': '/admin/references/equipmenttype/', 'detail_code': 'equipment-types'},
                {'name': 'Техника', 'count': Equipment.objects.count(), 'url': '', 'external_url': '/admin/references/equipment/', 'detail_code': 'equipment'},
                {'name': 'Состояния техники', 'count': EquipmentState.objects.count(), 'url': '', 'external_url': '/admin/references/equipmentstate/', 'detail_code': 'equipment-states'},
            ],
        },
        {
            'title': 'Производственные справочники',
            'items': [
                {'name': 'Породы', 'count': RockType.objects.count(), 'url': '', 'external_url': '/admin/references/rocktype/', 'detail_code': 'rocks'},
                {'name': 'Точки разгрузки', 'count': DumpPoint.objects.count(), 'url': '', 'external_url': '/admin/references/dumppoint/', 'detail_code': 'dump-points'},
                {'name': 'Шаблоны отчетов', 'count': ReportTemplate.objects.count(), 'url': '', 'external_url': '/reports/templates/'},
                {'name': 'Ежесменные планы техники', 'count': EquipmentPlanGroup.objects.count(), 'url': '', 'external_url': '/admin/shifts/equipmentplangroup/', 'detail_code': 'equipment-plan-groups'},
                {'name': 'Приз за 100% плана', 'count': AchievementPrize.objects.count(), 'url': '', 'external_url': '/admin/shifts/achievementprize/', 'detail_code': 'achievement-prizes'},
                {'name': 'Сменные планы (история)', 'count': ShiftPlan.objects.count(), 'url': '', 'external_url': '/admin/shifts/shiftplan/', 'detail_code': 'shift-plans'},
                {'name': 'Планы техники (история)', 'count': EquipmentShiftPlan.objects.count(), 'url': '', 'external_url': '/admin/shifts/equipmentshiftplan/', 'detail_code': 'equipment-shift-plans'},
            ],
        },
        {
            'title': 'Простои',
            'items': [
                {'name': 'Общий список простоев', 'count': DowntimeReason.objects.count(), 'url': '', 'external_url': '/admin/downtimes/downtimereason/', 'detail_code': 'downtime-reasons'},
                {'name': 'Простои водителя самосвала', 'count': DowntimeReason.objects.filter(show_for_truck_driver=True).count(), 'url': '', 'external_url': '/admin/downtimes/downtimereason/', 'detail_code': 'truck-driver-downtimes'},
                {'name': 'Простои машиниста экскаватора', 'count': DowntimeReason.objects.filter(show_for_excavator_operator=True).count(), 'url': '', 'external_url': '/admin/downtimes/downtimereason/', 'detail_code': 'excavator-operator-downtimes'},
                {'name': 'Детальные простои механика', 'count': DowntimeReason.objects.filter(show_for_mechanic=True).count(), 'url': '', 'external_url': '/admin/downtimes/downtimereason/', 'detail_code': 'mechanic-downtimes'},
            ],
        },
        {
            'title': 'Проживание',
            'items': [
                {'name': 'Общежития', 'count': Dormitory.objects.count(), 'url': '', 'external_url': '/admin/references/dormitory/', 'detail_code': 'dormitories'},
                {'name': 'Секции общежитий', 'count': DormitorySection.objects.count(), 'url': '', 'external_url': '/admin/references/dormitorysection/', 'detail_code': 'dormitory-sections'},
            ],
        },
    ]
    reference_total = 0
    empty_total = 0
    for section in reference_sections:
        section_count = 0
        empty_count = 0
        for item in section['items']:
            count = item['count']
            section_count += count
            reference_total += count
            if count:
                item['status_label'] = 'Заполнен'
                item['status_class'] = 'ok'
            else:
                empty_count += 1
                empty_total += 1
                item['status_label'] = 'Пусто'
                item['status_class'] = 'warning'
            if item.get('detail_code') in reference_configs:
                item['target_label'] = 'Рабочий экран'
            else:
                item['target_label'] = 'Админка' if item['external_url'].startswith('/admin/') else 'Рабочий экран'
        section['count'] = section_count
        section['empty_count'] = empty_count
        section['status_label'] = 'Требует заполнения' if empty_count else 'Готов'
        section['status_class'] = 'warning' if empty_count else 'ok'

    return render(
        request,
        'users/system_admin_references.html',
        {
            'access': access,
            'reference_sections': reference_sections,
            'reference_total': reference_total,
            'empty_total': empty_total,
        },
    )


def get_system_admin_reference_configs():
    return {
        'equipment-types': {
            'title': 'Виды техники',
            'section': 'Техника',
            'model': EquipmentType,
            'search_fields': ['name'],
            'preview_fields': ['name', 'is_active'],
            'admin_url': '/admin/references/equipmenttype/',
        },
        'equipment': {
            'title': 'Техника',
            'section': 'Техника',
            'model': Equipment,
            'search_fields': ['garage_number', 'vin', 'equipment_type__name', 'model__name'],
            'preview_fields': ['equipment_type', 'garage_number', 'model', 'vin'],
            'select_related': ['equipment_type', 'model'],
            'admin_url': '/admin/references/equipment/',
        },
        'equipment-states': {
            'title': 'Состояния техники',
            'section': 'Техника',
            'model': EquipmentState,
            'search_fields': ['code', 'name', 'short_label', 'description'],
            'preview_fields': ['code', 'name', 'short_label', 'color_group', 'semantic_group'],
            'admin_url': '/admin/references/equipmentstate/',
        },
        'downtime-reasons': {
            'title': 'Общий список простоев',
            'section': 'Простои',
            'model': DowntimeReason,
            'fields': [
                'name',
                'short_label',
                'equipment_type',
                'equipment_state',
                'is_critical',
                'show_for_truck_driver',
                'show_for_excavator_operator',
                'show_for_mechanic',
                'sort_order',
                'is_active',
            ],
            'search_fields': ['name', 'short_label', 'equipment_type__name', 'equipment_state__name'],
            'preview_fields': ['short_label', 'equipment_type', 'equipment_state', 'show_for_truck_driver', 'show_for_excavator_operator', 'show_for_mechanic'],
            'select_related': ['equipment_type', 'equipment_state'],
            'admin_url': '/admin/downtimes/downtimereason/',
        },
        'truck-driver-downtimes': {
            'title': 'Простои водителя самосвала',
            'section': 'Простои',
            'model': DowntimeReason,
            'fields': ['name', 'short_label', 'equipment_type', 'equipment_state', 'is_critical', 'show_for_truck_driver', 'sort_order', 'is_active'],
            'search_fields': ['name', 'short_label', 'equipment_type__name', 'equipment_state__name'],
            'preview_fields': ['short_label', 'equipment_type', 'equipment_state', 'is_critical', 'show_for_truck_driver'],
            'select_related': ['equipment_type', 'equipment_state'],
            'base_filter': {'show_for_truck_driver': True},
            'initial': {'show_for_truck_driver': True},
            'admin_url': '/admin/downtimes/downtimereason/',
        },
        'excavator-operator-downtimes': {
            'title': 'Простои машиниста экскаватора',
            'section': 'Простои',
            'model': DowntimeReason,
            'fields': ['name', 'short_label', 'equipment_type', 'equipment_state', 'is_critical', 'show_for_excavator_operator', 'sort_order', 'is_active'],
            'search_fields': ['name', 'short_label', 'equipment_type__name', 'equipment_state__name'],
            'preview_fields': ['short_label', 'equipment_type', 'equipment_state', 'is_critical', 'show_for_excavator_operator'],
            'select_related': ['equipment_type', 'equipment_state'],
            'base_filter': {'show_for_excavator_operator': True},
            'initial': {'show_for_excavator_operator': True},
            'admin_url': '/admin/downtimes/downtimereason/',
        },
        'mechanic-downtimes': {
            'title': 'Детальные простои механика',
            'section': 'Простои',
            'model': DowntimeReason,
            'fields': ['name', 'short_label', 'equipment_type', 'equipment_state', 'is_critical', 'show_for_mechanic', 'sort_order', 'is_active'],
            'search_fields': ['name', 'short_label', 'equipment_type__name', 'equipment_state__name'],
            'preview_fields': ['short_label', 'equipment_type', 'equipment_state', 'is_critical', 'show_for_mechanic'],
            'select_related': ['equipment_type', 'equipment_state'],
            'base_filter': {'show_for_mechanic': True},
            'initial': {'show_for_mechanic': True},
            'admin_url': '/admin/downtimes/downtimereason/',
        },
        'rocks': {
            'title': 'Породы',
            'section': 'Производство',
            'model': RockType,
            'search_fields': ['name'],
            'preview_fields': ['name', 'density', 'loosening_factor'],
            'admin_url': '/admin/references/rocktype/',
        },
        'dump-points': {
            'title': 'Точки разгрузки',
            'section': 'Производство',
            'model': DumpPoint,
            'search_fields': ['name'],
            'preview_fields': ['name', 'is_active'],
            'admin_url': '/admin/references/dumppoint/',
        },
        'equipment-plan-groups': {
            'title': 'Ежесменные планы техники',
            'section': 'Производство',
            'model': EquipmentPlanGroup,
            'form_class': EquipmentPlanGroupForm,
            'description': 'Один активный план задается на группу техники и автоматически фиксируется snapshot при открытии смены.',
            'fields': ['name', 'code', 'calculation_mode', 'plan_value', 'equipment', 'is_active', 'active_from', 'comment'],
            'search_fields': ['name', 'code', 'comment', 'equipment__garage_number', 'equipment__equipment_type__name', 'equipment__model__name'],
            'preview_fields': ['calculation_mode', 'plan_value', 'equipment', 'is_active', 'active_from', 'updated_by', 'updated_at'],
            'select_related': ['updated_by'],
            'prefetch_related': ['equipment', 'equipment__equipment_type', 'equipment__model'],
            'initial': {'is_active': True},
            'field_choices': {
                'calculation_mode': [
                    (PlanCalculationMode.TRIPS, 'По рейсам'),
                    (PlanCalculationMode.VOLUME, 'По объему, м3'),
                ],
            },
            'admin_url': '/admin/shifts/equipmentplangroup/',
        },
        'achievement-prizes': {
            'title': 'Приз за 100% плана',
            'section': 'Производство',
            'model': AchievementPrize,
            'description': 'Одна активная призовая картинка для водителей самосвалов и машинистов экскаваторов. Активная картинка выдается только после выполнения 100% сменного плана.',
            'fields': ['title', 'image', 'is_active'],
            'search_fields': ['title'],
            'preview_fields': ['title', 'image', 'is_active', 'updated_at'],
            'initial': {'title': 'План выполнен', 'is_active': True},
            'admin_url': '/admin/shifts/achievementprize/',
        },
        'shift-plans': {
            'title': 'Сменные планы (история)',
            'section': 'Производство',
            'model': ShiftPlan,
            'description': 'Старая схема планов по дате и смене сохранена для истории и совместимости. Основной способ - ежесменные планы техники.',
            'fields': ['plan_scope', 'name', 'plan_volume_m3', 'is_active', 'comment'],
            'search_fields': ['name', 'comment'],
            'preview_fields': ['plan_scope', 'plan_volume_m3'],
            'select_related': ['created_by'],
            'initial': {'plan_scope': ShiftPlanScope.DAY_SHIFT, 'name': 'Дневной сменный план', 'is_active': True},
            'hide_actions_card': True,
            'admin_url': '/admin/shifts/shiftplan/',
        },
        'equipment-shift-plans': {
            'title': 'Планы техники (история)',
            'section': 'Производство',
            'model': EquipmentShiftPlan,
            'description': 'Старая схема планов по конкретной технике на дату/смену. Для новых смен используйте ежесменные планы техники.',
            'fields': ['shift_plan', 'equipment', 'employee', 'calculation_mode', 'plan_trips', 'plan_volume_m3', 'is_active', 'comment'],
            'search_fields': ['shift_plan__name', 'equipment__garage_number', 'equipment__equipment_type__name', 'employee__full_name', 'comment'],
            'preview_fields': ['shift_plan', 'equipment', 'employee', 'calculation_mode', 'plan_trips', 'plan_volume_m3'],
            'select_related': ['shift_plan', 'equipment', 'equipment__equipment_type', 'employee'],
            'initial': {'is_active': True},
            'field_choices': {
                'calculation_mode': [
                    (PlanCalculationMode.TRIPS, 'По рейсам'),
                    (PlanCalculationMode.VOLUME, 'По объему, м3'),
                ],
            },
            'admin_url': '/admin/shifts/equipmentshiftplan/',
        },
        'dormitories': {
            'title': 'Общежития',
            'section': 'Проживание',
            'model': Dormitory,
            'search_fields': ['number'],
            'preview_fields': ['number', 'is_active'],
            'admin_url': '/admin/references/dormitory/',
        },
        'dormitory-sections': {
            'title': 'Секции общежитий',
            'section': 'Проживание',
            'model': DormitorySection,
            'search_fields': ['name', 'block__name', 'block__dormitory__number'],
            'preview_fields': ['block', 'name', 'day_capacity', 'night_capacity'],
            'select_related': ['block', 'block__dormitory'],
            'admin_url': '/admin/references/dormitorysection/',
        },
    }


def build_reference_form(model, config=None):
    config = config or {}
    if config.get('form_class'):
        form_class = config['form_class']
        field_choices = config.get('field_choices') or {}
        if not field_choices:
            return form_class

        class ReferenceForm(form_class):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                for field_name, choices in field_choices.items():
                    if field_name in self.fields:
                        self.fields[field_name].choices = choices

        return ReferenceForm

    editable_fields = config.get('fields') or [
        field.name
        for field in model._meta.fields
        if field.name != 'id' and getattr(field, 'editable', True)
    ]
    form_class = modelform_factory(model, fields=editable_fields)
    field_choices = config.get('field_choices') or {}
    if not field_choices:
        return form_class

    class ReferenceForm(form_class):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            for field_name, choices in field_choices.items():
                if field_name in self.fields:
                    self.fields[field_name].choices = choices

    return ReferenceForm


def prepare_reference_record_for_save(reference_code, record, access):
    if reference_code == 'shift-plans':
        if not record.date:
            record.date = timezone.localdate()
        record.plan_trips = None
        record.plan_tonnage = None
        if not record.created_by_id:
            record.created_by = access.employee
    elif reference_code == 'equipment-shift-plans':
        record.plan_tonnage = None
    elif reference_code == 'equipment-plan-groups':
        record.updated_by = access.employee
    return record


def build_reference_queryset(config):
    queryset = config['model'].objects.all()
    base_filter = config.get('base_filter') or {}
    if base_filter:
        queryset = queryset.filter(**base_filter)
    select_related = config.get('select_related') or []
    if select_related:
        queryset = queryset.select_related(*select_related)
    prefetch_related = config.get('prefetch_related') or []
    if prefetch_related:
        queryset = queryset.prefetch_related(*prefetch_related)
    return queryset


def build_reference_search_filter(search_fields, query):
    search_filter = Q()
    for field_name in search_fields:
        search_filter |= Q(**{f'{field_name}__icontains': query})
    return search_filter


def get_reference_status(record):
    if hasattr(record, 'is_active') and not record.is_active:
        return 'Отключен', 'neutral'
    return 'Активен', 'ok'


def get_reference_record_preview(record, config):
    preview = []
    for field_name in config.get('preview_fields', []):
        try:
            field = record._meta.get_field(field_name)
            label = field.verbose_name
            value = getattr(record, field_name)
            if getattr(field, 'many_to_many', False):
                value = ', '.join(str(item) for item in value.all()) or 'Не указано'
            elif field.get_internal_type() == 'BooleanField':
                value = 'Да' if value else 'Нет'
            elif getattr(field, 'choices', None):
                value = getattr(record, f'get_{field_name}_display')()
            elif value in (None, ''):
                value = 'Не указано'
        except FieldDoesNotExist:
            label = field_name.replace('_', ' ')
            value = getattr(record, field_name, '')
            if callable(value):
                value = value()
            if value in (None, ''):
                value = 'Не указано'
        preview.append({'label': label, 'value': value})
    return preview


def system_admin_reference_detail_view(request, reference_code):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')

    configs = get_system_admin_reference_configs()
    config = configs.get(reference_code)
    if not config:
        messages.error(request, 'Справочник не найден.')
        return redirect('system_admin_references')

    model = config['model']
    form_class = build_reference_form(model, config)
    query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()
    edit_id = request.GET.get('edit', '').strip()
    selected_record = None
    if edit_id.isdigit():
        selected_record = get_object_or_404(build_reference_queryset(config), id=edit_id)

    def reference_detail_redirect_url(record_id=None):
        params = []
        if query:
            params.append(('q', query))
        if status_filter:
            params.append(('status', status_filter))
        if record_id:
            params.append(('edit', record_id))
        query_string = urlencode(params)
        url = reverse('system_admin_reference_detail', kwargs={'reference_code': reference_code})
        return f'{url}?{query_string}' if query_string else url

    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        record_id = request.POST.get('record_id', '').strip()
        record = None
        if record_id.isdigit():
            record = get_object_or_404(model, id=record_id)

        if action in {'disable', 'enable'} and record and hasattr(record, 'is_active'):
            old_value = 'Активен' if record.is_active else 'Отключен'
            record.is_active = action == 'enable'
            record.save(update_fields=['is_active'])
            new_value = 'Активен' if record.is_active else 'Отключен'
            log_admin_action(access.employee, f'Справочник: {config["title"]}', record, old_value, new_value)
            messages.success(request, 'Состояние записи обновлено.')
            return redirect(reference_detail_redirect_url(record.id))

        form = form_class(request.POST, request.FILES, instance=record)
        if form.is_valid():
            saved_record = form.save(commit=False)
            saved_record = prepare_reference_record_for_save(reference_code, saved_record, access)
            saved_record.save()
            form.save_m2m()
            log_admin_action(access.employee, f'Справочник: {config["title"]}', saved_record, '', 'Сохранено')
            messages.success(request, 'Запись справочника сохранена.')
            return redirect(reference_detail_redirect_url(saved_record.id))
    else:
        form_initial = None if selected_record else config.get('initial')
        form = form_class(instance=selected_record, initial=form_initial)

    records_queryset = build_reference_queryset(config)
    if query:
        records_queryset = records_queryset.filter(build_reference_search_filter(config.get('search_fields', []), query)).distinct()
    if status_filter and hasattr(model, 'is_active'):
        records_queryset = records_queryset.filter(is_active=status_filter == 'active')

    records = []
    for record in records_queryset[:300]:
        status_label, status_class = get_reference_status(record)
        records.append({
            'object': record,
            'title': str(record),
            'status_label': status_label,
            'status_class': status_class,
            'preview': get_reference_record_preview(record, config),
        })

    count_queryset = build_reference_queryset(config)
    active_total = count_queryset.filter(is_active=True).count() if hasattr(model, 'is_active') else count_queryset.count()
    inactive_total = count_queryset.filter(is_active=False).count() if hasattr(model, 'is_active') else 0

    return render(
        request,
        'users/system_admin_reference_detail.html',
        {
            'access': access,
            'reference_code': reference_code,
            'reference_config': config,
            'form': form,
            'selected_record': selected_record,
            'records': records,
            'records_total': count_queryset.count(),
            'active_total': active_total,
            'inactive_total': inactive_total,
            'query': query,
            'status_filter': status_filter,
            'has_active_status': hasattr(model, 'is_active'),
        },
    )


def system_admin_conflicts_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')

    status = request.GET.get('status', '').strip()
    query = request.GET.get('q', '').strip()
    conflicts = AdminConflict.objects.select_related('employee', 'role').order_by('-created_at')
    if status:
        conflicts = conflicts.filter(status=status)
    if query:
        conflicts = conflicts.filter(
            Q(conflict_type__icontains=query)
            | Q(process__icontains=query)
            | Q(description__icontains=query)
            | Q(comment__icontains=query)
            | Q(employee__full_name__icontains=query)
            | Q(role__name__icontains=query)
        )

    conflict_status_counts = {
        item['status']: item['total']
        for item in AdminConflict.objects.values('status').annotate(total=Count('id'))
    }
    conflicts = list(conflicts[:200])
    for conflict in conflicts:
        if conflict.status == AdminConflict.Status.OPEN:
            conflict.status_class = 'danger'
        elif conflict.status == AdminConflict.Status.IN_PROGRESS:
            conflict.status_class = 'warning'
        elif conflict.status == AdminConflict.Status.RESOLVED:
            conflict.status_class = 'ok'
        else:
            conflict.status_class = 'neutral'

    return render(
        request,
        'users/system_admin_conflicts.html',
        {
            'access': access,
            'conflicts': conflicts,
            'statuses': AdminConflict.Status.choices,
            'selected_status': status,
            'query': query,
            'open_total': conflict_status_counts.get(AdminConflict.Status.OPEN, 0),
            'in_progress_total': conflict_status_counts.get(AdminConflict.Status.IN_PROGRESS, 0),
            'resolved_total': conflict_status_counts.get(AdminConflict.Status.RESOLVED, 0),
            'rejected_total': conflict_status_counts.get(AdminConflict.Status.REJECTED, 0),
            'conflict_total': sum(conflict_status_counts.values()),
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
    log_type = request.GET.get('type', '').strip()
    logs = AdminActionLog.objects.select_related('actor', 'reversal_of').order_by('-created_at')
    if query:
        logs = logs.filter(
            Q(action__icontains=query)
            | Q(object_type__icontains=query)
            | Q(object_repr__icontains=query)
            | Q(comment__icontains=query)
            | Q(actor__full_name__icontains=query)
        )
    if log_type:
        if log_type == 'access':
            logs = logs.filter(Q(action__icontains='доступ') | Q(action__icontains='пинкод') | Q(object_type__icontains='Access'))
        elif log_type == 'employee':
            logs = logs.filter(Q(action__icontains='сотрудник') | Q(object_type__icontains='Employee'))
        elif log_type == 'conflict':
            logs = logs.filter(Q(action__icontains='конфликт') | Q(object_type__icontains='AdminConflict'))
        elif log_type == 'reference':
            logs = logs.filter(Q(action__icontains='Справочник') | Q(object_type__icontains='references'))
        elif log_type == 'oup':
            logs = logs.filter(Q(action__startswith='ОУП:') | Q(action_code='admin_oup_action_reversed'))

    total_logs = AdminActionLog.objects.count()
    access_total = AdminActionLog.objects.filter(Q(action__icontains='доступ') | Q(action__icontains='пинкод') | Q(object_type__icontains='Access')).count()
    employee_total = AdminActionLog.objects.filter(Q(action__icontains='сотрудник') | Q(object_type__icontains='Employee')).count()
    conflict_total = AdminActionLog.objects.filter(Q(action__icontains='конфликт') | Q(object_type__icontains='AdminConflict')).count()
    oup_total = AdminActionLog.objects.filter(action__startswith='ОУП:').count()
    logs = list(logs[:200])
    for log in logs:
        action_text = f'{log.action} {log.object_type}'.lower()
        if log.action.startswith('ОУП:'):
            log.type_label = 'ОУП'
            log.type_class = 'info'
            log.undo_state = get_oup_action_undo_state(log)
        elif log.action_code == 'admin_oup_action_reversed':
            log.type_label = 'Отмена ОУП'
            log.type_class = 'ok'
            log.undo_state = None
        elif 'конфликт' in action_text or 'adminconflict' in action_text:
            log.type_label = 'Конфликт'
            log.type_class = 'danger'
        elif 'доступ' in action_text or 'пинкод' in action_text or 'access' in action_text:
            log.type_label = 'Доступ'
            log.type_class = 'warning'
        elif 'сотрудник' in action_text or 'employee' in action_text:
            log.type_label = 'Сотрудник'
            log.type_class = 'ok'
        elif 'справочник' in action_text:
            log.type_label = 'Справочник'
            log.type_class = 'neutral'
        else:
            log.type_label = 'Действие'
            log.type_class = 'neutral'
            log.undo_state = None

    return render(
        request,
        'users/system_admin_logs.html',
        {
            'access': access,
            'logs': logs,
            'query': query,
            'selected_log_type': log_type,
            'total_logs': total_logs,
            'access_log_total': access_total,
            'employee_log_total': employee_total,
            'conflict_log_total': conflict_total,
            'oup_log_total': oup_total,
            'return_url': request.get_full_path(),
        },
    )


@require_POST
def system_admin_undo_oup_action_view(request, log_id):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')
    try:
        result, _reversal = undo_oup_action(
            log_id=log_id,
            actor=access.employee,
            comment=request.POST.get('comment', '').strip(),
        )
    except ValidationError as error:
        messages.error(request, '; '.join(error.messages))
    else:
        messages.success(request, result)
    return redirect_after_admin_action(request, 'system_admin_logs')


def system_admin_exports_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')

    export_groups = [
        {
            'title': 'Администрирование',
            'items': [
                {
                    'title': 'Сотрудники',
                    'description': 'Кадровая карточка, статус, телефон, вахта и проживание.',
                    'url_name': 'system_admin_employee_export',
                    'count': Employee.objects.count(),
                    'status_label': 'готово',
                    'status_class': 'ok',
                },
                {
                    'title': 'Доступы',
                    'description': 'Роли, статусы входа, даты выдачи, активации и последнего входа.',
                    'url_name': 'system_admin_access_export',
                    'count': EmployeeAccess.objects.count(),
                    'status_label': 'готово',
                    'status_class': 'ok',
                },
                {
                    'title': 'Журнал действий',
                    'description': 'История административных действий для сверки и аудита.',
                    'url_name': 'system_admin_log_export',
                    'count': AdminActionLog.objects.count(),
                    'status_label': 'готово',
                    'status_class': 'ok',
                },
                {
                    'title': 'Конфликты',
                    'description': 'Заблокированные рискованные действия и статусы разбора.',
                    'url_name': 'system_admin_conflict_export',
                    'count': AdminConflict.objects.count(),
                    'status_label': 'готово',
                    'status_class': 'warning' if AdminConflict.objects.filter(status=AdminConflict.Status.OPEN).exists() else 'ok',
                },
            ],
        },
        {
            'title': 'Рабочие отчеты MVP',
            'items': [
                {
                    'title': 'Объемы',
                    'description': 'Производственный отчет по рейсам, группировкам и шаблонам.',
                    'external_url': '/reports/volume/export/',
                    'count': Trip.objects.count(),
                    'status_label': 'отчет',
                    'status_class': 'neutral',
                },
                {
                    'title': 'Суточный отчет заказчику',
                    'description': 'Суточная форма по дате отчета для внешней сверки.',
                    'external_url': '/reports/customer-daily/export/',
                    'count': Trip.objects.count(),
                    'status_label': 'отчет',
                    'status_class': 'neutral',
                },
                {
                    'title': 'Витрина руководства',
                    'description': 'Excel-срез руководителя: сводка, динамика и сравнение смен.',
                    'external_url': '/reports/management/export/',
                    'count': Trip.objects.count(),
                    'status_label': 'отчет',
                    'status_class': 'neutral',
                },
                {
                    'title': 'Механические простои',
                    'description': 'Отчет по простоям техники с фильтрами механической службы.',
                    'external_url': '/reports/downtimes/export/',
                    'count': 0,
                    'status_label': 'отчет',
                    'status_class': 'neutral',
                },
            ],
        },
    ]
    export_total = sum(len(group['items']) for group in export_groups)
    ready_total = sum(1 for group in export_groups for item in group['items'] if item['status_class'] == 'ok')
    warning_total = sum(1 for group in export_groups for item in group['items'] if item['status_class'] == 'warning')

    return render(
        request,
        'users/system_admin_exports.html',
        {
            'access': access,
            'export_groups': export_groups,
            'export_total': export_total,
            'ready_total': ready_total,
            'warning_total': warning_total,
        },
    )


def system_admin_employees_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')

    employees = Employee.objects.prefetch_related('accesses__role').order_by('full_name')
    status = request.GET.get('status', '').strip()
    access_status = request.GET.get('access_status', '').strip()
    role_id = request.GET.get('role', '').strip()
    query = request.GET.get('q', '').strip()
    if status:
        employees = employees.filter(status=status)
    if access_status:
        employees = employees.filter(accesses__status=access_status).distinct()
    if role_id.isdigit():
        employees = employees.filter(accesses__role_id=int(role_id)).distinct()
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
            'roles': Role.objects.filter(is_active=True).order_by('name'),
            'selected_status': status,
            'selected_access_status': access_status,
            'selected_role': role_id,
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
            try:
                with transaction.atomic():
                    role = form.cleaned_data['role']
                    employee = form.save(commit=False)
                    if role.code in {
                        Employee.WorkCategory.DRIVER,
                        Employee.WorkCategory.EXCAVATOR_OPERATOR,
                    }:
                        employee.work_category = role.code
                    employee.save()
                    code = ''
                    if form.cleaned_data['generate_access']:
                        code = generate_unique_access_code()
                        EmployeeAccess.objects.create(
                            employee=employee,
                            role=role,
                            access_code=code,
                            status=EmployeeAccess.Status.NOT_ACTIVATED,
                            primary_code_issued_at=timezone.now(),
                        )
                    else:
                        EmployeeAccess.objects.create(
                            employee=employee,
                            role=role,
                            access_code='',
                            status=EmployeeAccess.Status.NOT_ACTIVATED,
                        )
                    work_assignment = form.save_work_assignment(
                        employee=employee,
                        assigned_by=access.employee,
                    )
            except ValidationError as error:
                form.add_error('assignment_equipment', error)
            else:
                assignment_label = (
                    f'{work_assignment.work_shift_label}; {work_assignment.equipment}'
                    if work_assignment else 'не задано'
                )
                if code:
                    log_admin_action(
                        access.employee,
                        'Создан сотрудник и выдан первичный пинкод',
                        employee,
                        new_value=f'Роль: {role}; назначение: {assignment_label}; пинкод: {code}',
                    )
                    messages.success(
                        request,
                        f'Сотрудник создан. Первичный пинкод: {code}',
                        extra_tags='employee-card-silent',
                    )
                else:
                    log_admin_action(
                        access.employee,
                        'Создан сотрудник без пинкода',
                        employee,
                        new_value=f'Роль: {role}; назначение: {assignment_label}',
                    )
                    messages.success(request, 'Сотрудник создан.', extra_tags='employee-card-silent')
                return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee.id)
    else:
        form = AdminEmployeeForm()

    return render(
        request,
        'users/employee_card.html',
        {
            'access': access,
            'form': form,
            'title': 'Создать сотрудника',
            'page_mode': 'create',
            'employee_card_context': 'admin',
            'can_submit_employee_card': True,
        },
    )


def system_admin_employee_detail_view(request, employee_id):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')

    employee = get_object_or_404(Employee, id=employee_id)
    if request.method == 'POST':
        initial_status = employee.status
        old_photo_name = employee.photo.name if employee.photo else ''
        if request.POST.get('remove_photo') == '1':
            if old_photo_name:
                employee.photo.storage.delete(old_photo_name)
                employee.photo = ''
                employee.save(update_fields=['photo', 'updated_at'])
                log_admin_action(access.employee, 'Удалено фото сотрудника', employee)
                messages.success(request, 'Фото сотрудника удалено.')
            return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee.id)
        form = AdminEmployeeEditForm(request.POST, request.FILES, instance=employee)
        if form.is_valid():
            try:
                with transaction.atomic():
                    locked_employee = Employee.objects.select_for_update().get(pk=employee.pk)
                    if locked_employee.status != initial_status:
                        raise ValidationError(
                            'Статус сотрудника уже изменился. Обновите страницу.',
                            code='stale_employee_status',
                        )
                    form.instance = construct_instance(
                        form,
                        locked_employee,
                        form._meta.fields,
                        form._meta.exclude,
                    )
                    saved_employee = form.save()
                    work_assignment = form.save_work_assignment(assigned_by=access.employee)
            except ValidationError as error:
                form.add_error(
                    None if getattr(error, 'code', '') == 'stale_employee_status' else 'assignment_equipment',
                    error,
                )
            else:
                if request.FILES.get('photo') and old_photo_name and old_photo_name != saved_employee.photo.name:
                    saved_employee.photo.storage.delete(old_photo_name)
                assignment_label = (
                    f'{work_assignment.work_shift_label}; {work_assignment.equipment}'
                    if work_assignment else 'назначение снято'
                )
                log_admin_action(
                    access.employee,
                    'Изменена карточка сотрудника',
                    saved_employee,
                    new_value=f'Рабочее назначение: {assignment_label}',
                )
                messages.success(
                    request,
                    'Карточка сотрудника и рабочее назначение сохранены.',
                    extra_tags='employee-card-silent',
                )
                return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee.id)
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
    active_equipment_assignment = get_active_equipment_assignment(employee)
    work_assignment_role = active_equipment_assignment.role if active_equipment_assignment else None
    if not work_assignment_role and current_role_access:
        work_assignment_role = current_role_access.role

    return render(
        request,
        'users/employee_card.html',
        {
            'access': access,
            'employee': employee,
            'form': form,
            'title': employee.full_name,
            'page_mode': 'detail',
            'employee_card_context': 'admin',
            'can_submit_employee_card': True,
            'role_form': AdminAccessRoleForm(initial=role_form_initial),
            'block_form': AdminAccessBlockForm(),
            'employee_accesses': employee_accesses,
            'current_role_access': current_role_access,
            'active_equipment_assignment': active_equipment_assignment,
            'work_assignment_role': work_assignment_role,
            'work_assignment_supports_equipment': bool(
                work_assignment_role
                and work_assignment_role.code in WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES
            ),
            'can_restore_employee': (
                employee.status in ADMIN_RESTORABLE_EMPLOYEE_STATUSES
                or not employee.is_active
            ),
            'logs': AdminActionLog.objects.filter(
                Q(object_type='Employee', object_id=str(employee.id))
                | Q(object_id='', object_repr=str(employee))
            )[:10],
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
            with transaction.atomic():
                employee = Employee.objects.select_for_update().get(pk=employee.pk)
                if EmployeeShift.objects.filter(employee=employee, closed_at__isnull=True).exists():
                    messages.error(request, 'Сначала закройте текущую смену сотрудника, затем сбросьте PIN.')
                    return redirect_after_admin_action(
                        request,
                        'system_admin_employee_detail',
                        employee_id=employee.id,
                    )
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
    return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee.id)


@require_POST
def system_admin_change_access_role_view(request, access_id):
    admin_access = require_admin_access(request)
    if not admin_access:
        return redirect('role_home')

    form = AdminAccessRoleForm(request.POST)
    employee_access = get_object_or_404(
        EmployeeAccess.objects.select_related('employee', 'role'),
        id=access_id,
    )
    employee_id = employee_access.employee_id
    if not form.is_valid():
        messages.error(request, 'Выберите новую роль сотрудника.')
        return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee_id)

    new_role = form.cleaned_data['role']
    if employee_access.role_id == new_role.id:
        messages.info(request, 'У сотрудника уже назначена эта роль.')
        return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee_id)
    if employee_access.id == admin_access.id:
        messages.error(request, 'Нельзя изменить собственную роль администратора.')
        return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee_id)

    with transaction.atomic():
        locked_employee = Employee.objects.select_for_update().get(id=employee_id)
        employee_access = (
            EmployeeAccess.objects
            .select_for_update()
            .select_related('role')
            .get(id=access_id, employee_id=employee_id)
        )
        if EmployeeShift.objects.filter(employee_id=employee_id, closed_at__isnull=True).exists():
            messages.error(request, 'Сначала закройте текущую смену сотрудника, затем измените его роль.')
            return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee_id)
        if (
            EmployeeAccess.objects
            .filter(employee_id=employee_id, role=new_role)
            .exclude(id=employee_access.id)
            .exists()
        ):
            messages.error(request, 'У сотрудника уже есть отдельный доступ с выбранной ролью.')
            return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee_id)

        old_role = employee_access.role
        cleared_assignments = clear_active_equipment_assignment(
            employee=locked_employee,
            assigned_by=admin_access.employee,
            role_code=old_role.code,
        )
        employee_access.role = new_role
        employee_access.save(update_fields=['role'])

    log_admin_action(
        admin_access.employee,
        'Изменена роль доступа сотрудника',
        employee_access,
        old_value=old_role.name,
        new_value=new_role.name,
        comment='PIN, пароль и статус доступа сохранены.',
    )
    assignment_note = ' Старое назначение на технику снято.' if cleared_assignments else ''
    messages.success(
        request,
        f'Роль изменена: {old_role.name} → {new_role.name}. PIN и пароль сохранены.{assignment_note}',
    )
    return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee_id)


def system_admin_access_action_view(request, access_id, action):
    admin_access = require_admin_access(request)
    if not admin_access:
        return redirect('role_home')
    employee_access = get_object_or_404(EmployeeAccess.objects.select_related('employee'), id=access_id)
    if request.method == 'POST':
        with transaction.atomic():
            employee = Employee.objects.select_for_update().get(pk=employee_access.employee_id)
            employee_access = (
                EmployeeAccess.objects.select_for_update()
                .select_related('role')
                .get(pk=employee_access.pk, employee=employee)
            )
            if employee_access.id == admin_access.id and action in {'block', 'deactivate'}:
                messages.error(request, 'Нельзя заблокировать или деактивировать собственный доступ администратора.')
                return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee.id)
            if action in {'block', 'deactivate'} and EmployeeShift.objects.filter(
                employee=employee,
                closed_at__isnull=True,
            ).exists():
                messages.error(request, 'Сначала закройте текущую смену сотрудника, затем измените его доступ.')
                return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee.id)
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
                if employee.status in {Employee.Status.DISMISSED, Employee.Status.DELETED}:
                    messages.error(request, 'Нельзя разблокировать доступ у уволенного или удаленного сотрудника.')
                    return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee.id)
                employee_access.status = EmployeeAccess.Status.ACTIVATED
                employee_access.is_active = True
                employee_access.blocked_at = None
                employee_access.block_reason = ''
                employee_access.deactivated_at = None
                employee_access.save(update_fields=['status', 'is_active', 'blocked_at', 'block_reason', 'deactivated_at'])
                if employee.status in {
                    Employee.Status.NOT_ACTIVATED,
                    Employee.Status.DEACTIVATED,
                    Employee.Status.ARCHIVED,
                }:
                    employee.status = Employee.Status.ACTIVE
                    employee.is_active = True
                    employee.save(update_fields=['status', 'is_active'])
                log_admin_action(admin_access.employee, 'Разблокирован доступ', employee_access)
                messages.success(request, 'Доступ разблокирован.')
            elif action == 'deactivate':
                employee_access.status = EmployeeAccess.Status.DEACTIVATED
                employee_access.is_active = False
                employee_access.deactivated_at = timezone.now()
                employee_access.save(update_fields=['status', 'is_active', 'deactivated_at'])
                clear_active_equipment_assignment(
                    employee=employee,
                    assigned_by=admin_access.employee,
                    role_code=employee_access.role.code,
                )
                log_admin_action(admin_access.employee, 'Доступ деактивирован', employee_access)
                messages.success(request, 'Доступ деактивирован.')
    return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee_access.employee.id)


def restore_employee_access(employee, requested_access_id=None):
    accesses = (
        EmployeeAccess.objects.select_for_update()
        .select_related('role')
        .filter(employee=employee)
    )
    employee_access = None
    if requested_access_id and str(requested_access_id).isdigit():
        employee_access = accesses.filter(id=int(requested_access_id)).first()
    if not employee_access:
        employee_access = (
            accesses.filter(status=EmployeeAccess.Status.DEACTIVATED)
            .order_by('-created_at', '-id')
            .first()
            or accesses.order_by('-is_active', '-created_at', '-id').first()
        )
    if not employee_access:
        return None, 'missing'

    if (
        employee_access.status == EmployeeAccess.Status.BLOCKED
        or employee_access.blocked_at
        or employee_access.block_reason
    ):
        if (
            employee_access.status != EmployeeAccess.Status.BLOCKED
            or employee_access.is_active
            or employee_access.deactivated_at
        ):
            employee_access.status = EmployeeAccess.Status.BLOCKED
            employee_access.is_active = False
            employee_access.deactivated_at = None
            employee_access.save(
                update_fields=['status', 'is_active', 'deactivated_at']
            )
        return employee_access, 'blocked'

    if employee_access.is_active and employee_access.status != EmployeeAccess.Status.DEACTIVATED:
        return employee_access, 'already_active'

    if employee_access.activated_at:
        employee_access.status = EmployeeAccess.Status.ACTIVATED
    elif employee_access.primary_code_issued_at:
        employee_access.status = EmployeeAccess.Status.NOT_ACTIVATED
    elif employee_access.access_code and employee_access.last_login_at:
        employee_access.status = EmployeeAccess.Status.ACTIVATED
    else:
        employee_access.status = EmployeeAccess.Status.NOT_ACTIVATED
    employee_access.is_active = True
    employee_access.deactivated_at = None
    employee_access.blocked_at = None
    employee_access.block_reason = ''
    employee_access.save(
        update_fields=[
            'status',
            'is_active',
            'deactivated_at',
            'blocked_at',
            'block_reason',
        ]
    )
    return employee_access, 'restored'


def system_admin_employee_status_action_view(request, employee_id, action):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')
    employee = get_object_or_404(Employee, id=employee_id)
    if request.method == 'POST':
        with transaction.atomic():
            employee = Employee.objects.select_for_update().get(pk=employee.pk)
            if action == 'restore':
                if (
                    employee.status not in ADMIN_RESTORABLE_EMPLOYEE_STATUSES
                    and employee.is_active
                ):
                    messages.info(request, 'Сотрудник уже находится в рабочем состоянии.')
                    return redirect_after_admin_action(
                        request,
                        'system_admin_employee_detail',
                        employee_id=employee.id,
                    )

                old_status = employee.get_status_display()
                employee.status = Employee.Status.ACTIVE
                employee.is_active = True
                employee.dismissed_at = None
                employee.save(
                    update_fields=['status', 'is_active', 'dismissed_at', 'updated_at']
                )
                employee_access, access_result = restore_employee_access(
                    employee,
                    request.POST.get('access_id'),
                )

                if access_result == 'restored':
                    if employee_access.status == EmployeeAccess.Status.ACTIVATED:
                        access_note = (
                            f' Доступ «{employee_access.role.name}» включен; '
                            'действующий PIN/пароль сохранен.'
                        )
                    elif employee_access.access_code:
                        access_note = (
                            f' Доступ «{employee_access.role.name}» возвращен в ожидание '
                            'первого входа; первичный PIN сохранен.'
                        )
                    else:
                        access_note = (
                            f' Доступ «{employee_access.role.name}» включен, но PIN еще '
                            'не выдан.'
                        )
                elif access_result == 'blocked':
                    access_note = (
                        f' Доступ «{employee_access.role.name}» остался заблокированным; '
                        'разблокируйте его отдельно.'
                    )
                elif access_result == 'already_active':
                    access_note = (
                        f' Доступ «{employee_access.role.name}» уже был активен; '
                        'PIN/пароль не изменялся.'
                    )
                else:
                    access_note = ' Доступ не найден; назначьте роль и выдайте PIN отдельно.'

                log_admin_action(
                    access.employee,
                    'Сотрудник восстановлен администратором',
                    employee,
                    old_value=old_status,
                    new_value=employee.get_status_display(),
                    comment=(
                        access_note.strip()
                        + ' Смена и техника автоматически не восстанавливались.'
                    ),
                )
                bump_operational_state(
                    'Employee:admin_restored',
                    event_type='personnel_changed',
                    object_type='Employee',
                    object_id=employee.id,
                    payload={
                        'action': 'admin_restored',
                        'employee_ids': [employee.id],
                        'status': employee.status,
                        'is_active': employee.is_active,
                        'access_id': employee_access.id if employee_access else None,
                        'access_result': access_result,
                    },
                )
                messages.success(
                    request,
                    'Сотрудник восстановлен.' + access_note
                    + ' Смена и техника не назначались автоматически.',
                )
                return redirect_after_admin_action(
                    request,
                    'system_admin_employee_detail',
                    employee_id=employee.id,
                )

            if employee.id == access.employee.id and action in {'deactivate', 'archive', 'delete'}:
                messages.error(request, 'Нельзя деактивировать, архивировать или удалить собственную учетную запись администратора.')
                return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee.id)
            if action in {'deactivate', 'archive', 'delete'} and EmployeeShift.objects.filter(
                employee=employee,
                closed_at__isnull=True,
            ).exists():
                messages.error(request, 'Сначала закройте текущую смену сотрудника, затем измените его статус.')
                return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee.id)
            if action == 'deactivate':
                employee.status = Employee.Status.DEACTIVATED
                employee.is_active = False
                employee.accesses.update(status=EmployeeAccess.Status.DEACTIVATED, is_active=False, deactivated_at=timezone.now())
                clear_active_equipment_assignment(employee=employee, assigned_by=access.employee)
                messages.success(request, 'Сотрудник деактивирован.')
                log_admin_action(access.employee, 'Сотрудник деактивирован', employee)
            elif action == 'archive':
                employee.status = Employee.Status.ARCHIVED
                employee.is_active = False
                employee.accesses.update(status=EmployeeAccess.Status.DEACTIVATED, is_active=False, deactivated_at=timezone.now())
                clear_active_equipment_assignment(employee=employee, assigned_by=access.employee)
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
                    return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee.id)
                employee_name = employee.full_name
                log_admin_action(access.employee, 'Сотрудник полностью удален', employee, old_value=employee_name)
                employee.delete()
                messages.success(request, f'Сотрудник {employee_name} удален.')
                return redirect('system_admin_employees')
            else:
                return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee.id)
            employee.save(update_fields=['status', 'is_active', 'updated_at'])
    return redirect_after_admin_action(request, 'system_admin_employee_detail', employee_id=employee.id)


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
    sheet.append([
        'Дата', 'Кто', 'Действие', 'Код действия', 'Тип объекта', 'Объект',
        'Комментарий', 'Отменяет запись', 'Дата отмены', 'Кто отменил',
    ])
    logs = AdminActionLog.objects.select_related('actor', 'reversal_of').order_by('-created_at')
    reversals = {
        item.reversal_of_id: item
        for item in logs
        if item.reversal_of_id
    }
    for log in logs:
        reversal = reversals.get(log.id)
        sheet.append([
            excel_value(log.created_at),
            log.actor.full_name if log.actor else '',
            log.action,
            log.action_code,
            log.object_type,
            log.object_repr,
            log.comment,
            log.reversal_of_id or '',
            excel_value(reversal.created_at) if reversal else '',
            reversal.actor.full_name if reversal and reversal.actor else '',
        ])
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


def driver_format_duration_label(seconds):
    seconds = max(0, int(seconds or 0))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f'{hours:02d}:{minutes:02d}:{seconds % 60:02d}'


def driver_report_duration_label(seconds, *, total=False):
    rounded_minutes = max(0, int((max(0, int(seconds or 0)) + 30) // 60))
    hours, minutes = divmod(rounded_minutes, 60)
    if total and hours:
        return f'{hours}:{minutes:02d} мин.'
    if hours and minutes:
        return f'{hours} ч. {minutes} мин.'
    if hours:
        hour_word = 'час' if hours % 10 == 1 and hours % 100 != 11 else ('часа' if hours % 10 in {2, 3, 4} and hours % 100 not in {12, 13, 14} else 'часов')
        return f'{hours} {hour_word}.'
    return f'{rounded_minutes} мин.'


def driver_shift_downtime_seconds(equipment, shift, *, until=None):
    if not equipment or not shift or not shift.opened_at:
        return 0
    period_end = until or shift.closed_at or timezone.now()
    events = DowntimeEvent.objects.filter(
        equipment=equipment,
        started_at__lt=period_end,
    ).filter(Q(ended_at__isnull=True) | Q(ended_at__gt=shift.opened_at))
    total_seconds = 0
    for event in events.only('started_at', 'ended_at'):
        overlap_start = max(event.started_at, shift.opened_at)
        overlap_end = min(event.ended_at or period_end, period_end)
        total_seconds += max(0, int((overlap_end - overlap_start).total_seconds()))
    return total_seconds


def driver_downtime_reason_status_key(reason):
    if reason:
        return reason.effective_color_group
    return 'yellow'


def driver_downtime_event_payload(event, *, action='', closed=False, shift=None):
    now = timezone.now()
    started_at = event.started_at or now
    ended_at = event.ended_at
    elapsed_until = ended_at or now
    elapsed_seconds = max(0, int((elapsed_until - started_at).total_seconds()))
    reason = event.reason if event.reason_id else None
    shift_total_seconds = driver_shift_downtime_seconds(event.equipment, shift)
    return {
        'ok': True,
        'action': action,
        'active': not bool(ended_at),
        'closed': bool(closed),
        'event_id': event.id,
        'reason_id': event.reason_id,
        'reason': str(reason) if reason else '',
        'started_at': started_at.isoformat(),
        'ended_at': ended_at.isoformat() if ended_at else '',
        'elapsed_seconds': elapsed_seconds,
        'elapsed_label': driver_format_duration_label(elapsed_seconds),
        'shift_total_seconds': shift_total_seconds,
        'shift_total_label': driver_format_duration_label(shift_total_seconds),
        'status_key': driver_downtime_reason_status_key(reason),
    }


def driver_json_payload(request):
    if 'application/json' not in (request.headers.get('Content-Type') or ''):
        return request.POST
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except (TypeError, ValueError, UnicodeDecodeError):
        return {}


def driver_wants_json(request):
    return (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or 'application/json' in (request.headers.get('Accept') or '')
    )


def driver_employee_short_name(employee):
    parts = [part for part in (getattr(employee, 'full_name', '') or '').split() if part]
    if not parts:
        return 'Водитель'
    if len(parts) == 1:
        return parts[0]
    initials = ''.join(f'{part[0]}.' for part in parts[1:3] if part)
    return f'{parts[0]} {initials}'.strip()


def driver_equipment_number(equipment):
    return str(getattr(equipment, 'garage_number', '') or equipment or '').strip()


def driver_excavator_short_label(equipment):
    number = driver_equipment_number(equipment)
    if not number:
        return '—'
    upper_number = number.upper()
    if upper_number.startswith(('ЭКС', 'ЭКГ', 'EX')):
        return number
    return f'ЭКС-{number}'


def driver_complex_label_for_excavator(equipment):
    number = driver_equipment_number(equipment)
    if not number:
        return 'К-—'
    upper_number = number.upper()
    if upper_number.startswith('К-'):
        return number
    return f'К-{number}'


def driver_prefixed_context_value(prefix, value):
    value = str(value or '').strip()
    if not value:
        return f'{prefix} —'
    if value.lower().startswith(prefix.lower()):
        return value
    return f'{prefix} {value}'


def driver_compact_context_value(prefix, compact_prefix, value):
    value = str(value or '').strip()
    if not value:
        return f'{compact_prefix}—'
    for candidate in (prefix, compact_prefix):
        if value.lower().startswith(candidate.lower()):
            value = value[len(candidate):].strip()
            break
    return f'{compact_prefix}{value}'


def driver_assignment_countdown_label(assignment):
    if not assignment:
        return '05:00'
    if assignment.effective_at:
        remaining_seconds = max(0, int((assignment.effective_at - timezone.now()).total_seconds()))
    elif assignment.assigned_at:
        elapsed_seconds = max(0, int((timezone.now() - assignment.assigned_at).total_seconds()))
        remaining_seconds = max(0, (5 * 60) - elapsed_seconds)
    else:
        remaining_seconds = 5 * 60
    minutes, seconds = divmod(remaining_seconds, 60)
    return f'{minutes:02d}:{seconds:02d}'


def shift_plan_display_context(progress):
    status = progress.get('plan_status') if progress else ''
    percent_value = progress.get('progress_percent') if progress else None
    has_plan = percent_value is not None
    visual = progress_cycle_visual_context(percent_value if has_plan else 0)
    plan_value = progress.get('plan_value') if progress else None
    calculation_mode = progress.get('calculation_mode') if progress else ''
    return {
        'percent': visual['percent'] if has_plan else 0,
        'status': status or PlanAssignmentStatus.NO_PLAN_GROUP,
        'status_label': plan_status_label(status),
        'short_label': 'Нет группы' if status == PlanAssignmentStatus.NO_PLAN_GROUP else 'Нет плана' if status == PlanAssignmentStatus.NO_ACTIVE_PLAN else plan_status_label(status),
        'has_plan': has_plan,
        'value': plan_value,
        'unit': plan_unit_label(calculation_mode),
        'group_name': progress.get('plan_group_name') if progress else '',
        'visual': visual,
    }


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
    work_assignment = get_active_equipment_assignment(access.employee, 'driver')
    assignment_state = work_assignment_state(access.employee, work_assignment)
    current_truck = open_shift.equipment if open_shift else None
    assigned_truck = work_assignment.equipment if work_assignment and assignment_state == 'assigned' else None
    header_truck = current_truck or assigned_truck
    if current_truck:
        reconcile_due_haul_assignments(truck_id=current_truck.id)
    current_assignment = None
    pending_assignment_action = None
    active_trip = None
    active_downtime = None
    shift_trips = []
    if current_truck:
        open_assignments = list(
            HaulAssignment.objects
            .filter(
                truck=current_truck,
                ended_at__isnull=True,
            )
            .exclude(status=AssignmentStatus.CANCELLED)
            .select_related('truck', 'excavator')
            .order_by('-assigned_at')
        )
        accepted_assignment = next(
            (assignment for assignment in open_assignments if assignment.status == AssignmentStatus.ACCEPTED),
            None,
        )
        pending_assignment = next(
            (assignment for assignment in open_assignments if assignment.status == AssignmentStatus.PENDING),
            None,
        )
        current_assignment = accepted_assignment
        pending_assignment_action = pending_assignment
        active_trip = Trip.objects.filter(
            truck=current_truck,
            status__in=OPEN_TRIP_STATUSES,
        ).select_related(
            'truck',
            'excavator',
            'rock_type',
            'dump_point',
            'assigned_dump_point',
            'actual_dump_point',
        ).order_by('-created_at').first()
        active_downtime = (
            DowntimeEvent.objects
            .select_related('reason', 'reason__equipment_state')
            .filter(equipment=current_truck, employee=access.employee, ended_at__isnull=True)
            .order_by('-started_at')
            .first()
        )
        shift_trips = list(
            Trip.objects
            .select_related('excavator', 'rock_type', 'dump_point', 'assigned_dump_point', 'actual_dump_point')
            .filter(Q(unloading_shift=open_shift) | Q(loading_shift=open_shift) | Q(driver=access.employee, completed_at__gte=open_shift.opened_at))
            .distinct()
            .order_by('created_at')[:30]
        )

    for trip in shift_trips:
        started_at = timezone.localtime(trip.created_at) if trip.created_at else None
        completed_at = timezone.localtime(trip.completed_at) if trip.completed_at else None
        finish_for_duration = completed_at or timezone.localtime(timezone.now())
        duration_seconds = 0
        if started_at:
            duration_seconds = max(0, int((finish_for_duration - started_at).total_seconds()))
        duration_minutes = max(1, round(duration_seconds / 60)) if duration_seconds else 0
        trip.driver_excavator_label = trip.excavator.garage_number if trip.excavator_id else '—'
        driver_dump_point = trip.actual_dump_point or trip.dump_point or trip.assigned_dump_point
        trip.driver_dump_point_label = str(driver_dump_point) if driver_dump_point else '—'
        started_label = started_at.strftime('%H:%M') if started_at else '—'
        completed_label = completed_at.strftime('%H:%M') if completed_at else '...'
        trip.driver_time_range_label = f'{started_label}–{completed_label}'
        trip.driver_duration_label = f'{duration_minutes}м' if completed_at else 'в рейсе'

    completed_shift_trips = [trip for trip in shift_trips if trip.status == TripStatus.COMPLETED]
    shift_trip_count = len(completed_shift_trips)
    report_trip_map = {}
    for trip in completed_shift_trips:
        report_key = (trip.driver_excavator_label, trip.driver_dump_point_label)
        report_trip_map.setdefault(report_key, 0)
        report_trip_map[report_key] += 1
    driver_shift_report_trip_rows = [
        {'excavator': key[0], 'dump_point': key[1], 'count': count}
        for key, count in report_trip_map.items()
    ]

    driver_shift_downtime_events = []
    driver_shift_downtime_rows = []
    driver_shift_timeline = []
    if current_truck and open_shift:
        shift_period_end = timezone.now()
        driver_shift_downtime_events = list(
            DowntimeEvent.objects
            .select_related('reason')
            .filter(
                equipment=current_truck,
                employee=access.employee,
                started_at__lt=shift_period_end,
            )
            .filter(Q(ended_at__isnull=True) | Q(ended_at__gt=open_shift.opened_at))
            .order_by('started_at')
        )
        downtime_totals = {}
        for event in driver_shift_downtime_events:
            overlap_start = max(event.started_at, open_shift.opened_at)
            overlap_end = min(event.ended_at or shift_period_end, shift_period_end)
            duration_seconds = max(0, int((overlap_end - overlap_start).total_seconds()))
            reason_label = event.reason.button_label
            downtime_totals.setdefault(reason_label, 0)
            downtime_totals[reason_label] += duration_seconds
            driver_shift_timeline.append({
                'at': overlap_start,
                'time': timezone.localtime(overlap_start).strftime('%H:%M'),
                'kind': 'downtime-start',
                'title': f'Начат простой: {reason_label}',
                'meta': '',
            })
            if event.ended_at:
                driver_shift_timeline.append({
                    'at': overlap_end,
                    'time': timezone.localtime(overlap_end).strftime('%H:%M'),
                    'kind': 'downtime-end',
                    'title': f'Завершён простой: {reason_label}',
                    'meta': driver_format_duration_label(duration_seconds),
                })
        driver_shift_downtime_rows = [
            {
                'reason': reason,
                'seconds': seconds,
                'duration': driver_report_duration_label(seconds),
            }
            for reason, seconds in downtime_totals.items()
        ]

    for index, trip in enumerate(shift_trips, start=1):
        driver_shift_timeline.append({
            'at': trip.created_at,
            'time': timezone.localtime(trip.created_at).strftime('%H:%M'),
            'kind': 'trip',
            'title': f'Рейс {index:02d} · {trip.driver_excavator_label} → {trip.driver_dump_point_label}',
            'meta': trip.driver_time_range_label,
        })
    driver_shift_timeline.sort(key=lambda item: item['at'])
    shift_progress = calculate_truck_shift_progress(current_truck, reference_shift=open_shift)
    shift_plan = shift_plan_display_context(shift_progress)
    shift_plan_percent = shift_plan['percent']
    active_tab = request.GET.get('tab', 'work' if open_shift else 'shift')
    if active_tab not in {'work', 'shift', 'downtimes', 'manifest'}:
        active_tab = 'work'
    driver_status = 'ПУСТОЙ'
    driver_status_class = 'is-empty'
    driver_target_label = '—'
    driver_trip_context_source = active_trip
    if active_trip:
        driver_status = 'ЗАГРУЖЕН'
        driver_status_class = 'is-loaded'
        driver_target_label = active_trip.actual_dump_point or active_trip.dump_point
    elif active_downtime:
        driver_status = 'ПРОСТОЙ'
        driver_status_class = 'is-downtime'

    driver_work_excavator = active_trip.excavator if active_trip else (current_assignment.excavator if current_assignment else None)
    driver_work_context_placement = None
    if driver_work_excavator:
        driver_work_context_placement = (
            ExcavatorPlacement.objects
            .select_related('work_rock_type', 'work_dump_point')
            .filter(excavator=driver_work_excavator)
            .first()
        )
    if not driver_trip_context_source and driver_work_context_placement:
        has_work_context = any([
            driver_work_context_placement.loading_horizon,
            driver_work_context_placement.loading_block,
            driver_work_context_placement.work_rock_type_id,
        ])
        if has_work_context:
            driver_trip_context_source = driver_work_context_placement
    if not driver_trip_context_source and current_assignment:
        driver_trip_context_source = (
            Trip.objects
            .filter(excavator=current_assignment.excavator)
            .select_related('rock_type', 'dump_point', 'actual_dump_point', 'assigned_dump_point')
            .order_by('-created_at')
            .first()
        )
    driver_header_label = (
        f'Самосвал {driver_equipment_number(header_truck)} · {driver_employee_short_name(access.employee)}'
        if header_truck
        else f'Самосвал · {driver_employee_short_name(access.employee)}'
    )
    driver_context_rock = (
        getattr(driver_trip_context_source, 'rock_type', None)
        or getattr(driver_trip_context_source, 'work_rock_type', None)
    )
    driver_excavator_label = driver_excavator_short_label(driver_work_excavator)
    driver_complex_label = driver_complex_label_for_excavator(driver_work_excavator)
    driver_geology_parts = [
        driver_prefixed_context_value('Горизонт', getattr(driver_trip_context_source, 'loading_horizon', '')),
        driver_prefixed_context_value('Блок', getattr(driver_trip_context_source, 'loading_block', '')),
        str(driver_context_rock or '—'),
    ]
    driver_context_parts = [driver_complex_label, *driver_geology_parts]
    driver_context_label = ' · '.join(driver_context_parts)
    driver_dial_label = str(driver_target_label) if active_trip else driver_excavator_short_label(driver_work_excavator)
    driver_dial_note = 'ТОЧКА РАЗГРУЗКИ' if active_trip else 'НА ЗАГРУЗКУ'
    driver_new_assignment_label = ''
    driver_assignment_action_label = ''
    driver_assignment_effective_at = ''
    driver_assignment_countdown = '05:00'
    if pending_assignment_action:
        if pending_assignment_action.action == HaulAssignmentAction.RELEASE:
            action_label = 'НАЗНАЧЕНИЕ СНЯТО'
        else:
            action_label = f'ВЫ НАЗНАЧЕНЫ НА {driver_excavator_short_label(pending_assignment_action.excavator)}'
        driver_assignment_action_label = action_label
        driver_assignment_countdown = driver_assignment_countdown_label(pending_assignment_action)
        driver_new_assignment_label = (
            f'{action_label} · ПРИНЯТЬ · {driver_assignment_countdown}'
        )
        if pending_assignment_action.effective_at:
            driver_assignment_effective_at = pending_assignment_action.effective_at.isoformat()

    downtime_equipment_type = current_truck.equipment_type if current_truck else None
    downtime_reasons = DowntimeReason.for_workplace('truck_driver', downtime_equipment_type)
    unload_points = DumpPoint.objects.filter(is_active=True).order_by('name')[:10]
    active_trip_assigned_dump_point = None
    active_trip_actual_dump_point_id = None
    if active_trip:
        active_trip_assigned_dump_point = active_trip.assigned_dump_point or active_trip.dump_point
        active_trip_actual_dump_point_id = (active_trip.actual_dump_point_id or active_trip.dump_point_id)
    active_downtime_elapsed_seconds = 0
    active_downtime_elapsed_label = '00:00:00'
    shift_downtime_total_seconds = driver_shift_downtime_seconds(
        open_shift.equipment if open_shift else current_truck,
        open_shift,
    )
    shift_downtime_total_label = driver_format_duration_label(shift_downtime_total_seconds)
    shift_downtime_report_total_label = driver_report_duration_label(shift_downtime_total_seconds, total=True)
    active_downtime_started_at = ''
    active_downtime_status_key = 'yellow'
    if active_downtime and active_downtime.started_at:
        active_downtime_elapsed_seconds = max(0, int((timezone.now() - active_downtime.started_at).total_seconds()))
        active_downtime_elapsed_label = driver_format_duration_label(active_downtime_elapsed_seconds)
        active_downtime_started_at = active_downtime.started_at.isoformat()
        active_downtime_status_key = driver_downtime_reason_status_key(active_downtime.reason)

    last_closed_shift = None
    if assigned_truck:
        last_closed_shift = EmployeeShift.objects.filter(
            equipment=assigned_truck,
            closed_at__isnull=False,
        ).order_by('-closed_at').first()

    if request.method == 'POST' and not open_shift:
        form = DriverOpenShiftForm(request.POST, employee=access.employee, work_assignment=work_assignment) if assignment_state == 'assigned' else None
        if form and form.is_valid():
            current_work_assignment = get_active_equipment_assignment(access.employee, 'driver')
            if work_assignment_state(access.employee, current_work_assignment) != 'assigned':
                form.add_error(None, 'Назначение изменилось. Обновите экран перед началом смены.')
            else:
                try:
                    open_driver_shift(
                        employee=access.employee,
                        work_assignment=current_work_assignment,
                        readings={
                            'start_fuel': form.cleaned_data['start_fuel'],
                            'start_mileage': form.cleaned_data['start_mileage'],
                            'start_engine_hours': form.cleaned_data['start_engine_hours'],
                        },
                        client_action_id=form.cleaned_data.get('client_action_id') or secrets.token_urlsafe(24),
                    )
                except ValidationError as error:
                    form.add_error(None, error)
                else:
                    messages.success(request, 'Смена открыта.')
                    return redirect('driver_work')
    else:
        form_initial = {}
        if last_closed_shift:
            form_initial = {
                'start_fuel': last_closed_shift.end_fuel,
                'start_mileage': last_closed_shift.end_mileage,
                'start_engine_hours': last_closed_shift.end_engine_hours,
            }
        form_initial['client_action_id'] = secrets.token_urlsafe(24)
        form = (
            DriverOpenShiftForm(initial=form_initial, employee=access.employee, work_assignment=work_assignment)
            if not open_shift and assignment_state == 'assigned'
            else None
        )

    close_form = getattr(request, '_driver_close_form', None)
    if close_form is None and open_shift:
        close_form = DriverCloseShiftForm(
            instance=open_shift,
            initial={'client_action_id': secrets.token_urlsafe(24)},
        )

    response = render(
        request,
        'users/driver_shift.html',
        {
            'access': access,
            'registration': registration,
            'current_truck': current_truck,
            'header_truck': header_truck,
            'open_shift': open_shift,
            'work_assignment': work_assignment,
            'work_assignment_state': assignment_state,
            'work_assignment_shift_label': work_assignment.work_shift_label if work_assignment else '',
            'work_assignment_equipment': assigned_truck,
            'current_assignment': current_assignment,
            'active_trip': active_trip,
            'form': form,
            'close_form': close_form,
            'close_review': getattr(request, '_driver_close_review', None),
            'last_closed_shift': last_closed_shift,
            'active_tab': active_tab,
            'active_downtime': active_downtime,
            'active_downtime_started_at': active_downtime_started_at,
            'active_downtime_elapsed_seconds': active_downtime_elapsed_seconds,
            'active_downtime_elapsed_label': active_downtime_elapsed_label,
            'shift_downtime_total_seconds': shift_downtime_total_seconds,
            'shift_downtime_total_label': shift_downtime_total_label,
            'shift_downtime_report_total_label': shift_downtime_report_total_label,
            'active_downtime_status_key': active_downtime_status_key,
            'downtime_reasons': downtime_reasons,
            'shift_trips': shift_trips,
            'shift_trip_count': shift_trip_count,
            'driver_shift_report_trip_rows': driver_shift_report_trip_rows,
            'driver_shift_downtime_rows': driver_shift_downtime_rows,
            'driver_shift_timeline': driver_shift_timeline,
            'driver_shift_report_date': timezone.localtime(open_shift.opened_at).strftime('%d.%m.%Y') if open_shift else '—',
            'driver_shift_report_shift': open_shift.get_shift_type_display() if open_shift else 'Смена не открыта',
            'driver_shift_report_driver': driver_employee_short_name(access.employee),
            'driver_shift_report_truck': driver_equipment_number(current_truck) if current_truck else '—',
            'shift_plan_percent': shift_plan_percent,
            'shift_plan_status': shift_plan['status'],
            'shift_plan_status_label': shift_plan['status_label'],
            'shift_plan_short_label': shift_plan['short_label'],
            'shift_plan_has_plan': shift_plan['has_plan'],
            'shift_plan_value': shift_plan['value'],
            'shift_plan_unit': shift_plan['unit'],
            'shift_plan_group_name': shift_plan['group_name'],
            'shift_plan_visual': shift_plan['visual'],
            'driver_status': driver_status,
            'driver_status_class': driver_status_class,
            'driver_target_label': driver_target_label,
            'driver_header_label': driver_header_label,
            'driver_excavator_label': driver_excavator_label,
            'driver_complex_label': driver_complex_label,
            'driver_geology_parts': driver_geology_parts,
            'driver_context_parts': driver_context_parts,
            'driver_context_label': driver_context_label,
            'driver_dial_label': driver_dial_label,
            'driver_dial_note': driver_dial_note,
            'driver_new_assignment_label': driver_new_assignment_label,
            'driver_assignment_action_label': driver_assignment_action_label,
            'driver_assignment_effective_at': driver_assignment_effective_at,
            'driver_assignment_countdown': driver_assignment_countdown,
            'pending_assignment_action': pending_assignment_action,
            'unload_points': unload_points,
            'active_trip_assigned_dump_point': active_trip_assigned_dump_point,
            'active_trip_actual_dump_point_id': active_trip_actual_dump_point_id,
            'trip_status_loaded': TripStatus.LOADED_WAITING_UNLOAD,
            'driver_shell_version': DRIVER_SHELL_VERSION,
        },
    )
    response['Cache-Control'] = 'no-cache'
    return response


@require_POST
def driver_accept_assignment_view(request, assignment_id):
    access_id = request.session.get('employee_access_id')
    access = (
        EmployeeAccess.objects.select_related('employee', 'role')
        .filter(id=access_id, is_active=True, role__code='driver')
        .first()
    )
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к приложению водителя.'}, status=403)
    open_shift = (
        EmployeeShift.objects
        .filter(employee=access.employee, closed_at__isnull=True)
        .select_related('equipment')
        .order_by('-opened_at')
        .first()
    )
    if not open_shift or not open_shift.equipment_id:
        return JsonResponse({'ok': False, 'error': 'Открытая смена водителя не найдена.'}, status=409)
    assignment = get_object_or_404(
        HaulAssignment,
        id=assignment_id,
        truck_id=open_shift.equipment_id,
        status=AssignmentStatus.PENDING,
        ended_at__isnull=True,
    )
    applied = apply_pending_haul_assignment(assignment.id)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': bool(applied), 'action': assignment.action})
    return redirect('driver_work')


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
        return redirect('driver_work')

    form = DriverCloseShiftForm(request.POST, instance=open_shift)
    request._driver_close_form = form
    request._driver_close_review = None
    action = request.POST.get('shift_action', 'review')
    if form.is_valid():
        readings = {
            'end_fuel': form.cleaned_data['end_fuel'],
            'end_mileage': form.cleaned_data['end_mileage'],
            'end_engine_hours': form.cleaned_data['end_engine_hours'],
        }
        review_key = f'driver_shift_close_review_{open_shift.pk}'
        normalized = {field: str(value) for field, value in readings.items()}
        if action == 'review':
            request.session[review_key] = normalized
            request._driver_close_review = {
                'start_fuel': open_shift.start_fuel,
                'end_fuel': readings['end_fuel'],
                'start_mileage': open_shift.start_mileage,
                'end_mileage': readings['end_mileage'],
                'mileage_delta': readings['end_mileage'] - open_shift.start_mileage,
                'start_engine_hours': open_shift.start_engine_hours,
                'end_engine_hours': readings['end_engine_hours'],
                'engine_hours_delta': readings['end_engine_hours'] - open_shift.start_engine_hours,
            }
        elif request.session.get(review_key) != normalized:
            form.add_error(None, 'Показания изменились после проверки. Проверьте их повторно.')
        else:
            try:
                close_driver_shift(
                    shift=open_shift,
                    employee=access.employee,
                    readings=readings,
                    client_action_id=form.cleaned_data.get('client_action_id') or secrets.token_urlsafe(24),
                )
            except ValidationError as error:
                form.add_error(None, error)
            else:
                request.session.pop(review_key, None)
                messages.success(request, 'Смена закрыта.')
                return redirect('driver_work')
    request.GET = request.GET.copy()
    request.GET['tab'] = 'shift'
    return driver_shift_view(request)


def driver_downtime_action_view(request):
    wants_json = driver_wants_json(request)
    access_id = request.session.get('employee_access_id')
    if not access_id:
        if wants_json:
            return JsonResponse({'ok': False, 'error': 'Нет доступа к экрану водителя.'}, status=403)
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code != 'driver':
        if wants_json:
            return JsonResponse({'ok': False, 'error': 'Нет доступа к экрану водителя.'}, status=403)
        return redirect('role_home')
    if not getattr(access.employee, 'driver_registration', None):
        if wants_json:
            return JsonResponse({'ok': False, 'error': 'Водитель не зарегистрирован.'}, status=403)
        return redirect('driver_registration')

    open_shift = (
        EmployeeShift.objects
        .filter(employee=access.employee, closed_at__isnull=True)
        .select_related('equipment')
        .order_by('-opened_at')
        .first()
    )
    if not open_shift or not open_shift.equipment:
        if wants_json:
            return JsonResponse({'ok': False, 'error': 'Нельзя зафиксировать простой: открытая смена с самосвалом не найдена.'}, status=409)
        messages.error(request, 'Нельзя зафиксировать простой: открытая смена с самосвалом не найдена.')
        return redirect(f'{reverse("driver_work")}?tab=downtimes')

    if request.method != 'POST':
        if wants_json:
            return JsonResponse({'ok': False, 'error': 'Некорректный метод действия простоя.'}, status=405)
        return redirect(f'{reverse("driver_work")}?tab=downtimes')

    payload = driver_json_payload(request)
    action = (payload.get('action') or '').strip()
    active_event = (
        DowntimeEvent.objects
        .select_related('reason', 'reason__equipment_state')
        .filter(equipment=open_shift.equipment, employee=access.employee, ended_at__isnull=True)
        .order_by('-started_at')
        .first()
    )
    if action == 'close':
        if active_event:
            active_event.ended_at = timezone.now()
            active_event.save(update_fields=['ended_at'])
            if wants_json:
                return JsonResponse(driver_downtime_event_payload(active_event, action='downtime_closed', closed=True, shift=open_shift))
        else:
            if wants_json:
                return JsonResponse({
                    'ok': True,
                    'active': False,
                    'closed': False,
                    'elapsed_seconds': 0,
                    'elapsed_label': '00:00:00',
                    'shift_total_seconds': driver_shift_downtime_seconds(open_shift.equipment, open_shift),
                    'shift_total_label': driver_format_duration_label(driver_shift_downtime_seconds(open_shift.equipment, open_shift)),
                })
            messages.error(request, 'Активный простой не найден.')
        return redirect(f'{reverse("driver_work")}?tab=downtimes')

    reason_id = payload.get('reason_id')
    reason = DowntimeReason.for_workplace('truck_driver', open_shift.equipment.equipment_type).filter(id=reason_id).first()
    if not reason:
        if wants_json:
            return JsonResponse({'ok': False, 'error': 'Причина простоя не найдена.'}, status=400)
        messages.error(request, 'Причина простоя не найдена.')
        return redirect(f'{reverse("driver_work")}?tab=downtimes')
    if active_event:
        active_event.reason = reason
        active_event.save(update_fields=['reason'])
        event = active_event
        action_label = 'downtime_updated'
    else:
        event = DowntimeEvent.objects.create(
            equipment=open_shift.equipment,
            employee=access.employee,
            reason=reason,
            started_at=timezone.now(),
            comment='Зафиксировано водителем самосвала',
        )
        action_label = 'downtime_started'
    if wants_json:
        return JsonResponse(driver_downtime_event_payload(event, action=action_label, shift=open_shift))
    return redirect(f'{reverse("driver_work")}?tab=downtimes')

# Create your views here.
