import re
import json
from decimal import Decimal

from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from references.models import Equipment
from shifts.models import EmployeeShift, ShiftType
from trips.views import dispatcher_control_view as render_dispatcher_control_view
from users.access_auth import find_employee_access_by_credentials
from users.models import EmployeeAccess
from users.session_device import get_session_device_kind, set_session_device_kind

from .models import AssignmentStatus, ExcavatorPlacement, HaulAssignment
from .services import schedule_haul_assignment, schedule_haul_release


MINING_MASTER_MANIFEST = {
    'id': '/mining-master/assignments/',
    'name': 'Горный мастер',
    'short_name': 'Горный мастер',
    'description': 'Мобильный пульт Горного мастера для управления активной сменой.',
    'start_url': '/mining-master/assignments/',
    'scope': '/',
    'display': 'standalone',
    'display_override': ['standalone', 'fullscreen'],
    'orientation': 'portrait',
    'background_color': '#041017',
    'theme_color': '#041017',
    'categories': ['business', 'productivity'],
    'icons': [
        {
            'src': '/static/img/pwa/mining-master-192.png',
            'sizes': '192x192',
            'type': 'image/png',
            'purpose': 'any',
        },
        {
            'src': '/static/img/pwa/mining-master-512.png',
            'sizes': '512x512',
            'type': 'image/png',
            'purpose': 'any',
        },
        {
            'src': '/static/img/pwa/mining-master-maskable-512.png',
            'sizes': '512x512',
            'type': 'image/png',
            'purpose': 'maskable',
        },
    ],
    'shortcuts': [
        {
            'name': 'Пульт смены',
            'short_name': 'Пульт',
            'url': '/mining-master/assignments/',
            'description': 'Открыть мобильный пульт Горного мастера.',
        },
    ],
}


MINING_MASTER_SERVICE_WORKER_JS = r"""
const CACHE_NAME = "mining-master-mobile-shell-v105";
const APP_SHELL_URL = "/mining-master/assignments/";
const LOGIN_URL = "/";
const MANIFEST_URL = "/mining-master-manifest.webmanifest";
const NETWORK_FIRST_TIMEOUT_MS = 2500;
const CORE_ASSETS = [
  LOGIN_URL,
  APP_SHELL_URL,
  MANIFEST_URL,
  "/static/js/realtime-client.js",
  "/static/css/app.css",
  "/static/favicon.ico",
  "/static/img/pwa/mining-master-180.png",
  "/static/img/pwa/mining-master-192.png",
  "/static/img/pwa/mining-master-512.png",
  "/static/img/pwa/mining-master-maskable-512.png",
  "/static/img/equipment/excavator-gray.png",
  "/static/img/equipment/excavator-blue.png",
  "/static/img/equipment/excavator-green.png",
  "/static/img/equipment/excavator-yellow.png",
  "/static/img/equipment/excavator-red.png",
  "/static/img/equipment/truck-gray.png",
  "/static/img/equipment/truck-blue.png",
  "/static/img/equipment/truck-green.png",
  "/static/img/equipment/truck-yellow.png",
  "/static/img/equipment/truck-red.png"
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(CORE_ASSETS.map(url => new Request(url, { cache: "reload" }))).catch(() => undefined))
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

function networkDelay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function networkFirst(request, fallbackUrl, event) {
  const cache = await caches.open(CACHE_NAME);
  const cached = (await cache.match(request)) ||
    (fallbackUrl ? await cache.match(fallbackUrl) : null);
  const networkRequest = fetch(request)
    .then(response => {
      if (response && response.ok) {
        cache.put(request, response.clone()).catch(() => undefined);
        if (fallbackUrl) {
          cache.put(fallbackUrl, response.clone()).catch(() => undefined);
        }
      }
      return response;
    });
  networkRequest.catch(() => undefined);
  if (event && event.waitUntil) {
    event.waitUntil(networkRequest.then(() => undefined).catch(() => undefined));
  }
  if (cached) {
    try {
      return await Promise.race([
        networkRequest,
        networkDelay(NETWORK_FIRST_TIMEOUT_MS).then(() => cached)
      ]);
    } catch (error) {
      return cached;
    }
  }
  try {
    return await networkRequest;
  } catch (error) {
    return new Response("Оффлайн: экран еще не сохранен на этом устройстве.", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8" }
    });
  }
}

async function networkOnly(request) {
  try {
    return await fetch(request);
  } catch (error) {
    return new Response("Сеть недоступна: свежий фрагмент экрана не получен.", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8" }
    });
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request, { ignoreSearch: true });
  if (cached) return cached;
  const response = await fetch(request);
  if (response && response.ok) {
    cache.put(request, response.clone()).catch(() => undefined);
  }
  return response;
}

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.headers.get("X-Requested-With") === "XMLHttpRequest") {
    event.respondWith(networkOnly(request));
    return;
  }
  if (request.mode === "navigate" || url.pathname === APP_SHELL_URL || url.pathname === LOGIN_URL) {
    event.respondWith(networkFirst(request, url.pathname === LOGIN_URL ? LOGIN_URL : APP_SHELL_URL, event));
    return;
  }
  if (url.pathname === MANIFEST_URL) {
    event.respondWith(networkFirst(request, MANIFEST_URL, event));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(cacheFirst(request));
  }
});

self.addEventListener("message", event => {
  const data = event.data || {};
  if (data.type === "SKIP_WAITING") {
    self.skipWaiting();
    return;
  }
  if (data.type === "GET_VERSION") {
    const message = {
      type: "MINING_MASTER_SW_VERSION",
      version: CACHE_NAME
    };
    if (event.ports && event.ports[0]) {
      event.ports[0].postMessage(message);
    } else if (event.source) {
      event.source.postMessage(message);
    }
  }
});
"""


TRUCK_ICON_BY_STATUS = {
    'green': 'img/equipment/truck-green.png',
    'yellow': 'img/equipment/truck-yellow.png',
    'blue': 'img/equipment/truck-blue.png',
    'orange': 'img/equipment/truck-yellow.png',
    'red': 'img/equipment/truck-red.png',
    'gray': 'img/equipment/truck-gray.png',
}

EXCAVATOR_ICON_BY_STATUS = {
    'green': 'img/equipment/excavator-green.png',
    'yellow': 'img/equipment/excavator-yellow.png',
    'blue': 'img/equipment/excavator-blue.png',
    'orange': 'img/equipment/excavator-yellow.png',
    'red': 'img/equipment/excavator-red.png',
    'gray': 'img/equipment/excavator-gray.png',
}


def get_equipment_number(equipment):
    garage_number = (equipment.garage_number or '').strip()
    if not garage_number:
        return ''
    return garage_number


def get_truck_label(equipment):
    model_name = equipment.model.name if equipment.model else ''
    prefix = 'NHL' if 'NHL' in model_name.upper() or 'NHL' in equipment.garage_number.upper() else 'Б'
    return f'{prefix}-{get_equipment_number(equipment)}'


def get_excavator_label(equipment):
    return f'Экс {get_equipment_number(equipment)}'


def get_shift_state(employee):
    current_shift = (
        EmployeeShift.objects
        .select_related('employee')
        .filter(closed_at__isnull=True, employee=employee)
        .order_by('-opened_at')
        .first()
    )
    open_master_shifts = (
        EmployeeShift.objects
        .select_related('employee')
        .filter(
            closed_at__isnull=True,
            employee__accesses__role__code='mining_master',
            employee__accesses__is_active=True,
        )
        .distinct()
    )
    blocking_shift = open_master_shifts.exclude(employee=employee).first()
    return current_shift, blocking_shift


def get_shift_state_for_access(access):
    if access.role.code == 'mining_master':
        return get_shift_state(access.employee)
    blocking_shift = (
        EmployeeShift.objects
        .select_related('employee')
        .filter(
            closed_at__isnull=True,
            employee__accesses__role__code='mining_master',
            employee__accesses__is_active=True,
        )
        .distinct()
        .order_by('-opened_at')
        .first()
    )
    return None, blocking_shift


def get_shift_type_for_now(now):
    return ShiftType.DAY if 8 <= timezone.localtime(now).hour < 20 else ShiftType.NIGHT


def mining_master_service_worker_view(request):
    response = HttpResponse(MINING_MASTER_SERVICE_WORKER_JS, content_type='application/javascript; charset=utf-8')
    response['Cache-Control'] = 'no-cache'
    response['Service-Worker-Allowed'] = '/'
    return response


def mining_master_manifest_view(request):
    response = JsonResponse(MINING_MASTER_MANIFEST, json_dumps_params={'ensure_ascii': False})
    response['Content-Type'] = 'application/manifest+json; charset=utf-8'
    response['Cache-Control'] = 'no-cache'
    return response


def build_truck_tile(equipment, status_key, status_label, assignment=None, active_trip=None):
    label = get_truck_label(equipment)
    number = get_equipment_number(equipment)
    return {
        'id': equipment.id,
        'assignment_id': assignment.id if assignment else '',
        'number': number,
        'label': label,
        'garage_number': equipment.garage_number,
        'status_key': status_key,
        'status_label': status_label,
        'icon': TRUCK_ICON_BY_STATUS.get(status_key, TRUCK_ICON_BY_STATUS['gray']),
        'model': equipment.model.name if equipment.model else 'модель не указана',
        'search': f'{number} {label} {equipment.garage_number}'.lower(),
        'is_active_trip': bool(active_trip),
    }


def build_excavator_tile(equipment, status_key, status_label):
    number = get_equipment_number(equipment)
    label = get_excavator_label(equipment)
    return {
        'id': equipment.id,
        'number': number,
        'label': label,
        'garage_number': equipment.garage_number,
        'status_key': status_key,
        'status_label': status_label,
        'percent': 100,
        'icon': EXCAVATOR_ICON_BY_STATUS.get(status_key, EXCAVATOR_ICON_BY_STATUS['gray']),
        'model': equipment.model.name if equipment.model else 'модель не указана',
        'search': f'{number} {label} {equipment.garage_number}'.lower(),
    }


def format_datetime_short(value):
    if not value:
        return ''
    return timezone.localtime(value).strftime('%d.%m %H:%M')


def format_decimal_short(value):
    if value is None:
        return ''
    return f'{value:g}'


def build_employee_badge(employee):
    if not employee:
        return None
    photo_url = ''
    if employee.photo:
        try:
            photo_url = employee.photo.url
        except ValueError:
            photo_url = ''
    initials = ''.join(part[0] for part in (employee.full_name or '').split()[:2]).upper()
    return {
        'name': employee.full_name or '',
        'phone': employee.phone or '',
        'position': employee.position or '',
        'photo': photo_url,
        'initials': initials or '??',
    }


def build_employee_initials(employee):
    if not employee:
        return '--'
    initials = ''.join(part[0] for part in (employee.full_name or '').split()[:2]).upper()
    return initials or '--'


def build_employee_short_name(employee):
    if not employee:
        return ''
    parts = (employee.full_name or '').split()
    if not parts:
        return ''
    surname = parts[0]
    initials = ''.join(f'{part[0].upper()}.' for part in parts[1:3] if part)
    return f'{surname} {initials}'.strip()


def get_complex_truck_scale_class(truck_count):
    if truck_count <= 6:
        return 'truck-fill-1'
    if truck_count <= 12:
        return 'truck-fill-2'
    if truck_count <= 18:
        return 'truck-fill-3'
    return 'truck-fill-4'


def build_equipment_card_data(
    equipment,
    tile,
    zone_label,
    status_label,
    active_assignment=None,
    active_trip=None,
    downtime=None,
    shift_stats=None,
    truck_count=None,
    latest_trip=None,
    current_employee=None,
):
    model = equipment.model
    details = []
    details.append({'label': 'Гаражный N', 'value': equipment.garage_number})
    if equipment.vin:
        details.append({'label': 'VIN/серийный N', 'value': equipment.vin})
    if model:
        details.append({'label': 'Модель', 'value': model.name})
        if model.payload_tons:
            details.append({'label': 'ГП, т', 'value': format_decimal_short(model.payload_tons)})
        if model.body_volume_m3:
            details.append({'label': 'Кузов/ковш, м3', 'value': format_decimal_short(model.body_volume_m3)})
    else:
        details.append({'label': 'Модель', 'value': 'не указана'})
    if equipment.equipment_type.name == 'Самосвал':
        if active_assignment:
            details.append({'label': 'Назначен', 'value': format_datetime_short(active_assignment.assigned_at)})
        if active_trip:
            details.append({'label': 'Рейс', 'value': 'активный'})
            details.append({'label': 'Экскаватор рейса', 'value': get_excavator_label(active_trip.excavator)})
            details.append({'label': 'Разгрузка', 'value': str(active_trip.dump_point)})
            details.append({'label': 'Порода', 'value': str(active_trip.rock_type)})
            if active_trip.volume_m3:
                details.append({'label': 'Объем, м3', 'value': format_decimal_short(active_trip.volume_m3)})
        elif not active_assignment:
            details.append({'label': 'Назначение', 'value': 'ожидает'})
    else:
        stats = shift_stats or {}
        details.append({'label': 'Самосвалы', 'value': str(truck_count or 0)})
        details.append({'label': 'Рейсы', 'value': str(stats.get('total') or 0)})
        details.append({'label': 'Объем, м3', 'value': format_decimal_short(stats.get('volume') or Decimal('0'))})
        source_trip = active_trip or latest_trip
        details.append({'label': 'Горизонт', 'value': (source_trip.loading_horizon if source_trip else '') or 'не указан'})
        details.append({'label': 'Блок', 'value': (source_trip.loading_block if source_trip else '') or 'не указан'})

    if downtime:
        details.append({'label': 'Простой', 'value': str(downtime.reason)})
        details.append({'label': 'С начала', 'value': format_datetime_short(downtime.started_at)})

    return {
        'id': equipment.id,
        'type': equipment.equipment_type.name,
        'label': tile['label'],
        'number': tile['number'],
        'icon': tile['icon'],
        'status_key': tile['status_key'],
        'status_label': status_label,
        'zone': zone_label,
        'employee': build_employee_badge(current_employee),
        'details': details,
    }


def close_active_assignment(assignment, now):
    assignment.status = AssignmentStatus.CANCELLED
    assignment.ended_at = now
    assignment.save(update_fields=['status', 'ended_at'])


def close_active_assignments(assignments, now):
    for assignment in assignments:
        close_active_assignment(assignment, now)


def handle_shift_action(request, action, access, current_shift, blocking_shift):
    now = timezone.now()
    if action == 'start_shift':
        if current_shift:
            messages.info(request, 'Ваша смена уже открыта.')
            return
        if blocking_shift:
            messages.error(request, 'Предыдущий горный мастер еще не закрыл смену.')
            return
        EmployeeShift.objects.create(
            employee=access.employee,
            shift_type=get_shift_type_for_now(now),
            opened_at=now,
            opened_by=access.employee,
        )
        messages.success(request, 'Смена горного мастера открыта. Расстановка унаследована.')
        return

    if action == 'end_shift':
        if not current_shift:
            messages.error(request, 'Нет открытой смены для завершения.')
            return
        current_shift.closed_at = now
        current_shift.closed_by = access.employee
        current_shift.save(update_fields=['closed_at', 'closed_by'])
        messages.success(request, 'Смена горного мастера завершена.')


def authenticate_shared_shift_start(request):
    phone = request.POST.get('reauth_phone', '').strip()
    access_code = re.sub(r'\D', '', request.POST.get('reauth_access_code', ''))
    device_kind = request.POST.get('device_kind', '').strip()
    if not phone or not access_code:
        return None, 'Для начала смены на общем компьютере введите телефон и код горного мастера.'
    if phone and not phone.startswith(('+', '7', '8')):
        phone = f'+7 {phone}'

    access = find_employee_access_by_credentials(phone, access_code, role_code='mining_master')
    if not access:
        return None, 'Телефон или код горного мастера указаны неверно.'

    request.session['employee_access_id'] = access.id
    set_session_device_kind(request, device_kind)
    access.last_login_at = timezone.now()
    access.save(update_fields=['last_login_at'])
    return access, ''


def handle_assignment_action(request, action, access, current_shift):
    if not current_shift:
        messages.error(request, 'Сначала откройте смену горного мастера.')
        return

    now = timezone.now()
    truck_id = request.POST.get('truck')
    if not truck_id:
        messages.error(request, 'Самосвал не выбран.')
        return

    truck = get_object_or_404(
        Equipment.objects.select_related('equipment_type', 'model'),
        id=truck_id,
        equipment_type__name='Самосвал',
        is_active=True,
    )
    active_assignments = list(
        HaulAssignment.objects
        .filter(truck=truck, ended_at__isnull=True)
        .exclude(status=AssignmentStatus.CANCELLED)
        .select_related('excavator')
        .order_by('-assigned_at')
    )
    active_assignment = next(
        (assignment for assignment in active_assignments if assignment.status == AssignmentStatus.ACCEPTED),
        active_assignments[0] if active_assignments else None,
    )

    if action == 'release':
        if not active_assignment:
            messages.info(request, f'{get_truck_label(truck)} уже находится в гараже.')
            return
        schedule_haul_release(truck=truck, assigned_by=access.employee, now=now)
        messages.success(request, f'{get_truck_label(truck)} получит снятие назначения через 5 минут.')
        return

    excavator_id = request.POST.get('excavator')
    if not excavator_id:
        messages.error(request, 'Экскаватор не выбран.')
        return
    excavator = get_object_or_404(
        Equipment.objects.select_related('equipment_type', 'model'),
        id=excavator_id,
        equipment_type__name='Экскаватор',
        is_active=True,
    )

    schedule_haul_assignment(
        truck=truck,
        excavator=excavator,
        assigned_by=access.employee,
        now=now,
    )
    messages.success(request, f'{get_truck_label(truck)} назначен на {get_excavator_label(excavator)}.')


def get_active_assignments_queryset():
    return (
        HaulAssignment.objects
        .filter(ended_at__isnull=True)
        .exclude(status=AssignmentStatus.CANCELLED)
        .select_related('truck', 'excavator')
    )


def mining_master_access_from_request(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return None
    access = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(id=access_id, is_active=True)
        .first()
    )
    if not access or access.role.code != 'mining_master':
        return None
    return access


def mining_master_json_payload(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return {}


def mining_master_json_ok(payload=None, **extra):
    response = {'ok': True}
    response.update(extra)
    client_action_id = (payload or {}).get('client_action_id')
    if client_action_id:
        response['client_action_id'] = client_action_id
    return JsonResponse(response)


def mining_master_open_shift_or_error(access):
    if access.role.code != 'mining_master':
        return None, 'Изменять пульт Горного мастера может только Горный мастер.'
    current_shift, blocking_shift = get_shift_state_for_access(access)
    if not current_shift:
        if blocking_shift:
            return None, 'Предыдущий горный мастер еще не закрыл смену.'
        return None, 'Сначала откройте смену горного мастера.'
    return current_shift, ''


def build_mining_master_dispatcher_header(request, access, current_shift, blocking_shift):
    active_person = access.employee if current_shift else (blocking_shift.employee if blocking_shift else None)
    active_shift_opened_at = ''
    active_shift_date = ''
    if current_shift:
        active_shift_date = timezone.localtime(current_shift.opened_at).strftime('%d.%m.%Y')
        active_shift_opened_at = timezone.localtime(current_shift.opened_at).strftime('%H:%M')
    elif blocking_shift:
        active_shift_date = timezone.localtime(blocking_shift.opened_at).strftime('%d.%m.%Y')
        active_shift_opened_at = timezone.localtime(blocking_shift.opened_at).strftime('%H:%M')

    photo_url = ''
    if active_person and getattr(active_person, 'photo', None):
        try:
            photo_url = active_person.photo.url
        except ValueError:
            photo_url = ''

    can_start_shift = not current_shift and not blocking_shift
    requires_shift_reauth = can_start_shift and get_session_device_kind(request) == 'shared'
    current_time = timezone.localtime().strftime('%H:%M')
    current_date = timezone.localdate().strftime('%d.%m.%Y')
    if current_shift:
        shift_label = 'Смена открыта'
        time_range = f'с {active_shift_opened_at}'
        clock_caption = 'в работе'
        shift_status_variant = 'open'
    elif blocking_shift:
        shift_label = 'Режим наблюдателя'
        time_range = f'с {active_shift_opened_at}' if active_shift_opened_at else 'ожидание закрытия'
        clock_caption = 'наблюдение'
        shift_status_variant = 'blocked'
    else:
        shift_label = ''
        time_range = ''
        clock_caption = ''
        shift_status_variant = 'closed'

    return {
        'header': {
            'active_shift': current_shift or blocking_shift,
            'own_shift': current_shift,
            'active_dispatcher': active_person,
            'active_dispatcher_name': active_person.full_name if active_person else '',
            'active_dispatcher_short_name': build_employee_short_name(active_person),
            'active_dispatcher_photo': photo_url,
            'active_dispatcher_initials': build_employee_initials(active_person),
            'active_shift_date': active_shift_date,
            'active_shift_opened_at': active_shift_opened_at,
            'can_toggle_shift': bool(current_shift or can_start_shift),
            'shift_is_open': bool(current_shift),
            'shift_status_variant': shift_status_variant,
            'active_role_label': 'горный мастер',
            'active_shift_title': 'Активная смена горного мастера',
            'inactive_shift_title': 'Смена горного мастера не открыта',
            'inactive_name': 'смена не открыта',
            'shift_form_action': request.get_full_path(),
            'shift_action_field_name': 'action',
            'shift_start_value': 'start_shift',
            'shift_end_value': 'end_shift',
            'shift_start_label': 'Начать смену',
            'shift_end_label': 'Завершить смену',
            'shift_start_confirm': 'Начать смену горного мастера?',
            'shift_end_confirm': 'Завершить смену горного мастера?',
            'shift_end_confirm_title': 'Завершение смены',
            'shift_end_confirm_description': 'Вы уверены, что хотите завершить текущую смену? После завершения смены будут сохранены результаты работы.',
            'shift_end_confirm_role': 'Горный мастер',
            'shift_button_marker': True,
            'requires_shift_reauth': requires_shift_reauth,
            'session_device_kind': get_session_device_kind(request),
        },
        'context': {
            'dispatcher_page_title': 'Горный мастер',
            'dispatcher_compat_title': 'Пульт горного мастера',
            'dispatcher_board_label': 'Горный мастер',
            'dispatcher_identity_label': 'Активный горный мастер',
            'dispatcher_main_label': 'Пульт горного мастера',
            'dispatcher_actions_label': 'Действия смены горного мастера',
            'dispatcher_clock_label': 'Текущее время смены горного мастера',
            'dispatcher_nav_label': 'Навигация горного мастера',
            'dispatcher_header_screen': 'ГОРНЫЙ МАСТЕР',
            'dispatcher_header_shift_label': shift_label,
            'dispatcher_header_time_range': time_range,
            'dispatcher_clock_caption': clock_caption,
            'current_time': current_time,
            'current_date': current_date,
            'mining_master_mobile_enabled': True,
            'dispatcher_move_excavator_url': reverse('mining_master_move_excavator'),
            'dispatcher_assign_truck_url': reverse('mining_master_assign_truck'),
            'dispatcher_nav_items': [
                {'label': 'ПУЛЬТ', 'href': '#', 'active': True},
                {'label': 'ТЕХНИКА', 'href': '#', 'active': False},
                {'label': 'ОТЧЕТЫ', 'href': '#', 'active': False},
                {'label': 'ЖУРНАЛ', 'href': '#', 'active': False},
            ],
        },
    }


@require_POST
def mining_master_move_excavator_view(request):
    access = mining_master_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к экрану горного мастера.'}, status=403)
    current_shift, error = mining_master_open_shift_or_error(access)
    if error:
        return JsonResponse({'ok': False, 'error': error}, status=400)

    payload = mining_master_json_payload(request)
    excavator = get_object_or_404(
        Equipment.objects.select_related('equipment_type', 'model'),
        id=payload.get('excavator_id'),
        equipment_type__name='Экскаватор',
        is_active=True,
    )
    zone = payload.get('zone')
    if zone not in {ExcavatorPlacement.Zone.ACTIVE, ExcavatorPlacement.Zone.INACTIVE}:
        return JsonResponse({'ok': False, 'error': 'Некорректная зона экскаватора.'}, status=400)

    placement, _ = ExcavatorPlacement.objects.get_or_create(excavator=excavator)
    placement.zone = zone
    placement.changed_by = access.employee
    placement.save(update_fields=['zone', 'changed_by', 'changed_at'])

    closed_count = 0
    if zone == ExcavatorPlacement.Zone.INACTIVE:
        active_assignments = list(get_active_assignments_queryset().filter(excavator=excavator))
        closed_count = len(active_assignments)
        close_active_assignments(active_assignments, timezone.now())

    return mining_master_json_ok(payload, closed=closed_count)


@require_POST
def mining_master_assign_truck_view(request):
    access = mining_master_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к экрану горного мастера.'}, status=403)
    current_shift, error = mining_master_open_shift_or_error(access)
    if error:
        return JsonResponse({'ok': False, 'error': error}, status=400)

    payload = mining_master_json_payload(request)
    action = payload.get('action')
    now = timezone.now()

    if action == 'release_complex':
        excavator = get_object_or_404(
            Equipment.objects.select_related('equipment_type', 'model'),
            id=payload.get('excavator_id'),
            equipment_type__name='Экскаватор',
            is_active=True,
        )
        assignments = list(get_active_assignments_queryset().filter(excavator=excavator))
        trucks = {assignment.truck_id: assignment.truck for assignment in assignments}
        scheduled = sum(
            bool(schedule_haul_release(truck=truck, assigned_by=access.employee, now=now)[0])
            for truck in trucks.values()
        )
        return mining_master_json_ok(payload, scheduled=scheduled)

    truck = get_object_or_404(
        Equipment.objects.select_related('equipment_type', 'model'),
        id=payload.get('truck_id'),
        equipment_type__name='Самосвал',
        is_active=True,
    )
    active_assignments = list(
        HaulAssignment.objects
        .filter(truck=truck, ended_at__isnull=True)
        .exclude(status=AssignmentStatus.CANCELLED)
        .select_related('excavator')
        .order_by('-assigned_at')
    )
    active_assignment = next(
        (assignment for assignment in active_assignments if assignment.status == AssignmentStatus.ACCEPTED),
        active_assignments[0] if active_assignments else None,
    )
    expected_source_excavator_id = str(payload.get('expected_source_excavator_id') or '').strip()
    if expected_source_excavator_id and active_assignment and str(active_assignment.excavator_id) != expected_source_excavator_id:
        return JsonResponse({
            'ok': False,
            'error': 'Данные по самосвалу изменились в системе. Обновите пульт и повторите действие.',
            'conflict': True,
            'client_action_id': payload.get('client_action_id') or '',
        }, status=409)

    if action == 'release':
        assignment, created = schedule_haul_release(truck=truck, assigned_by=access.employee, now=now)
        return mining_master_json_ok(payload, assignment_id=assignment.id if assignment else None, created=created)

    if action != 'assign':
        return JsonResponse({'ok': False, 'error': 'Неизвестное действие.'}, status=400)

    excavator = get_object_or_404(
        Equipment.objects.select_related('equipment_type', 'model'),
        id=payload.get('excavator_id'),
        equipment_type__name='Экскаватор',
        is_active=True,
    )
    if not ExcavatorPlacement.objects.filter(excavator=excavator, zone=ExcavatorPlacement.Zone.ACTIVE).exists():
        return JsonResponse({'ok': False, 'error': 'Самосвал можно назначить только в активный комплекс.'}, status=400)

    assignment, created = schedule_haul_assignment(
        truck=truck,
        excavator=excavator,
        assigned_by=access.employee,
        now=now,
    )
    return mining_master_json_ok(payload, assignment_id=assignment.id, created=created)


def handle_bulk_release_action(request, action, current_shift, access):
    if not current_shift:
        messages.error(request, 'Сначала откройте смену горного мастера.')
        return

    now = timezone.now()
    assignments = get_active_assignments_queryset()

    if action == 'release_excavator':
        excavator_id = request.POST.get('excavator')
        if not excavator_id:
            messages.error(request, 'Экскаватор не выбран.')
            return
        excavator = get_object_or_404(
            Equipment.objects.select_related('equipment_type', 'model'),
            id=excavator_id,
            equipment_type__name='Экскаватор',
            is_active=True,
        )
        assignments = list(assignments.filter(excavator=excavator))
        if not assignments:
            messages.info(request, f'{get_excavator_label(excavator)} уже пустой.')
            return
        trucks = {assignment.truck_id: assignment.truck for assignment in assignments}
        for truck in trucks.values():
            schedule_haul_release(truck=truck, assigned_by=access.employee, now=now)
        messages.success(request, f'{get_excavator_label(excavator)} расформировывается. Водителям дано 5 минут.')
        return

    if action == 'release_all':
        assignments = list(assignments)
        active_excavator_placements = list(
            ExcavatorPlacement.objects
            .filter(zone=ExcavatorPlacement.Zone.ACTIVE, excavator__is_active=True)
            .select_related('excavator')
        )
        if not assignments and not active_excavator_placements:
            messages.info(request, 'Вся техника уже находится в неактивной смене.')
            return
        trucks = {assignment.truck_id: assignment.truck for assignment in assignments}
        for truck in trucks.values():
            schedule_haul_release(truck=truck, assigned_by=access.employee, now=now)
        for placement in active_excavator_placements:
            placement.zone = ExcavatorPlacement.Zone.INACTIVE
            placement.changed_by = access.employee
            placement.save(update_fields=['zone', 'changed_by', 'changed_at'])
        messages.success(request, 'Все комплексы расформированы. Вся техника возвращена в неактивную смену.')


def handle_excavator_placement_action(request, action, access, current_shift):
    if not current_shift:
        messages.error(request, 'Сначала откройте смену горного мастера.')
        return

    excavator_id = request.POST.get('excavator')
    if not excavator_id:
        messages.error(request, 'Экскаватор не выбран.')
        return

    excavator = get_object_or_404(
        Equipment.objects.select_related('equipment_type', 'model'),
        id=excavator_id,
        equipment_type__name='Экскаватор',
        is_active=True,
    )
    placement, _ = ExcavatorPlacement.objects.get_or_create(excavator=excavator)
    now = timezone.now()

    if action == 'activate_excavator':
        placement.zone = ExcavatorPlacement.Zone.ACTIVE
        placement.changed_by = access.employee
        placement.save(update_fields=['zone', 'changed_by', 'changed_at'])
        messages.success(request, f'{get_excavator_label(excavator)} добавлен в активную смену.')
        return

    if action == 'deactivate_excavator':
        active_assignments = list(get_active_assignments_queryset().filter(excavator=excavator))
        trucks = {assignment.truck_id: assignment.truck for assignment in active_assignments}
        for truck in trucks.values():
            schedule_haul_release(truck=truck, assigned_by=access.employee, now=now)
        placement.zone = ExcavatorPlacement.Zone.INACTIVE
        placement.changed_by = access.employee
        placement.save(update_fields=['zone', 'changed_by', 'changed_at'])
        messages.success(request, f'{get_excavator_label(excavator)} перенесен в неактивную смену.')


def mining_master_assignments_view(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code != 'mining_master':
        return redirect('role_home')

    current_shift, blocking_shift = get_shift_state_for_access(access)

    if request.method == 'POST':
        action = request.POST.get('action') or 'assign'
        if action in {'start_shift', 'end_shift'}:
            if action == 'start_shift':
                has_reauth_credentials = bool(request.POST.get('reauth_phone') and request.POST.get('reauth_access_code'))
                requested_device_kind = request.POST.get('device_kind', '').strip()
                starts_as_personal_device = requested_device_kind == 'personal' and not has_reauth_credentials
                if starts_as_personal_device:
                    set_session_device_kind(request, 'personal')
                elif get_session_device_kind(request) == 'shared' or has_reauth_credentials:
                    reauth_access, reauth_error = authenticate_shared_shift_start(request)
                    if reauth_error:
                        messages.error(request, reauth_error)
                        return redirect('mining_master_assignments')
                    access = reauth_access
                    current_shift, blocking_shift = get_shift_state_for_access(access)
            handle_shift_action(request, action, access, current_shift, blocking_shift)
        elif action in {'assign', 'release'}:
            handle_assignment_action(request, action, access, current_shift)
        elif action in {'release_excavator', 'release_all'}:
            handle_bulk_release_action(request, action, current_shift, access)
        elif action in {'activate_excavator', 'deactivate_excavator'}:
            handle_excavator_placement_action(request, action, access, current_shift)
        else:
            messages.error(request, 'Неизвестное действие.')
        return redirect('mining_master_assignments')

    master_dispatcher = build_mining_master_dispatcher_header(request, access, current_shift, blocking_shift)
    return render_dispatcher_control_view(
        request,
        access_override=access,
        enforce_dispatcher_access=False,
        dispatcher_header_override=master_dispatcher['header'],
        context_overrides=master_dispatcher['context'],
    )
