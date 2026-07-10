import json
import math
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from assignments.models import AssignmentStatus, EquipmentAssignment, ExcavatorPlacement, HaulAssignment
from core.models import OperationalStateVersion, bump_operational_state
from downtimes.models import DowntimeEvent, DowntimeReason
from references.equipment_states import DEFAULT_EQUIPMENT_STATES
from references.models import DumpPoint, Equipment, EquipmentState, RockType, TruckCapacityRule
from shifts.models import EmployeeShift
from shifts.models import PlanAssignmentStatus, PlanCalculationMode
from shifts.services import (
    assign_shift_plan_snapshot,
    calculate_open_shift_progress,
    calculate_truck_shift_progress,
    equipment_is_truck,
    format_progress_percent,
    plan_status_label,
    progress_cycle_visual_context,
    plan_unit_label,
)
from users.access_auth import find_employee_access_by_credentials
from users.models import EmployeeAccess
from users.session_device import get_session_device_kind, set_session_device_kind

from .forms import TripCreateForm
from .dispatcher_header import build_dispatcher_header_context, close_dispatcher_shift, get_active_dispatcher_shift, open_dispatcher_shift
from .models import DispatcherActionLog, DispatcherActionType, OPEN_TRIP_STATUSES, Trip, TripClientAction, TripStatus


DISPATCHER_FILTER_KEYS = (
    'truck',
    'excavator',
    'show_active_trips',
    'show_pending_assignments',
    'show_accepted_assignments',
)

DISPATCHER_PLAN_TOTAL_TONS = Decimal('420000')
EQUIPMENT_STATUS_COLOR_GROUPS = {'gray', 'yellow', 'green', 'blue', 'orange', 'red'}
DISPATCHER_PLAN_NOT_ASSIGNED = 'plan_not_assigned'

def get_equipment_state_ui_map():
    states = {}
    for fallback in DEFAULT_EQUIPMENT_STATES:
        code = fallback['code']
        states[code] = {
            'code': code,
            'label': fallback.get('short_label') or fallback.get('name') or code,
            'color_group': fallback.get('color_group') or 'gray',
            'allows_assignment': bool(fallback.get('allows_assignment', False)),
            'allows_drag': bool(fallback.get('allows_drag', False)),
            'blocks_operation': bool(fallback.get('blocks_operation', False)),
        }
    for state in EquipmentState.objects.filter(code__in=states.keys(), is_active=True):
        states[state.code].update({
            'label': state.label,
            'color_group': state.color_group,
            'allows_assignment': state.allows_assignment,
            'allows_drag': state.allows_drag,
            'blocks_operation': state.blocks_operation,
        })
    return states


def equipment_state_ui(states, code):
    return states.get(code) or states['inactive']


def normalize_status_color_group(color_group, *, fallback='yellow'):
    if color_group in EQUIPMENT_STATUS_COLOR_GROUPS:
        return color_group
    return fallback


def equipment_state_icon_color(color_group):
    if color_group == 'orange':
        return 'yellow'
    if color_group in {'green', 'yellow', 'red', 'gray', 'blue'}:
        return color_group
    return 'gray'


def trip_equipment_state_code(trip):
    if not trip:
        return ''
    if trip.status in OPEN_TRIP_STATUSES:
        return 'loaded_waiting_unload'
    return ''


def downtime_reason_equipment_state_code(reason):
    if not reason:
        return 'waiting'
    return reason.effective_equipment_state_code


def downtime_reason_color_group(reason):
    if not reason:
        return 'yellow'
    return normalize_status_color_group(reason.effective_color_group, fallback='yellow')


def dispatcher_alert_status_for_color_group(color_group):
    color_group = normalize_status_color_group(color_group, fallback='yellow')
    if color_group == 'red':
        return 'danger'
    if color_group in {'yellow', 'orange'}:
        return 'warning'
    if color_group == 'blue':
        return 'info'
    return 'ok'


def dispatcher_alert_status_for_downtime(downtime):
    return dispatcher_alert_status_for_color_group(
        downtime_reason_color_group(getattr(downtime, 'reason', None))
    )


def downtime_reason_state_ui(states, reason):
    state_ui = dict(equipment_state_ui(states, downtime_reason_equipment_state_code(reason)))
    state_ui['color_group'] = downtime_reason_color_group(reason)
    return state_ui


def downtime_equipment_state_code(downtime):
    if not downtime:
        return ''
    return downtime_reason_equipment_state_code(getattr(downtime, 'reason', None))


def format_duration_label(seconds):
    seconds = max(0, int(seconds or 0))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f'{hours:02d}:{minutes:02d}:{seconds % 60:02d}'


def plan_progress_status_key(percent, plan_status=''):
    if plan_status in {PlanAssignmentStatus.NO_PLAN_GROUP, PlanAssignmentStatus.NO_ACTIVE_PLAN, DISPATCHER_PLAN_NOT_ASSIGNED}:
        return plan_status
    try:
        value = int(percent)
    except (TypeError, ValueError):
        return 'empty'
    if value <= 0:
        return 'empty'
    if value < 50:
        return 'low'
    if value < 80:
        return 'warning'
    return 'good'


def dispatcher_empty_snapshot_progress(shift=None, equipment=None):
    equipment = equipment or getattr(shift, 'equipment', None)
    return {
        'equipment': equipment,
        'date': timezone.localtime(shift.opened_at).date() if shift and shift.opened_at else None,
        'shift_type': shift.shift_type if shift else None,
        'shift': shift,
        'plan': None,
        'plan_group': getattr(shift, 'plan_group', None) if shift else None,
        'plan_group_name': getattr(shift, 'plan_group_name', '') if shift else '',
        'plan_status': DISPATCHER_PLAN_NOT_ASSIGNED,
        'calculation_mode': getattr(shift, 'plan_calculation_mode', '') if shift else '',
        'plan_value': getattr(shift, 'plan_value', None) if shift else None,
        'trip_count': 0,
        'volume_m3': Decimal('0'),
        'tonnage': Decimal('0'),
        'progress_percent': None,
    }


def calculate_dispatcher_snapshot_progress(shift, equipment=None):
    if not shift:
        return dispatcher_empty_snapshot_progress(equipment=equipment)
    if not shift.plan_status:
        return dispatcher_empty_snapshot_progress(shift=shift, equipment=equipment)
    return calculate_open_shift_progress(shift)


def format_dispatcher_plan_number(value):
    if value in {None, ''}:
        return ''
    try:
        value = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    if value == value.to_integral_value():
        return format_dispatcher_number(value)
    return format_dispatcher_decimal(value.normalize())


def dispatcher_plan_fact_value(progress, calculation_mode):
    if not progress:
        return None
    if calculation_mode == PlanCalculationMode.TRIPS:
        return progress.get('trip_count') or 0
    if calculation_mode == PlanCalculationMode.TONNAGE:
        return progress.get('tonnage') or Decimal('0')
    if calculation_mode == PlanCalculationMode.VOLUME:
        return progress.get('volume_m3') or Decimal('0')
    return progress.get('volume_m3') or Decimal('0')


def dispatcher_plan_unit_label(calculation_mode):
    if calculation_mode == PlanCalculationMode.TRIPS:
        return 'рейса'
    return plan_unit_label(calculation_mode)


def plan_progress_display_context(progress):
    status = progress.get('plan_status') if progress else DISPATCHER_PLAN_NOT_ASSIGNED
    percent_value = format_progress_percent(progress.get('progress_percent') if progress else None)
    calculation_mode = progress.get('calculation_mode') if progress else ''
    fact_value = dispatcher_plan_fact_value(progress, calculation_mode)
    plan_value = progress.get('plan_value') if progress else None
    unit = dispatcher_plan_unit_label(calculation_mode)
    fact_display = format_dispatcher_plan_number(fact_value) if fact_value is not None else ''
    plan_display = format_dispatcher_plan_number(plan_value) if plan_value is not None else ''
    status_label = plan_status_label(status)
    short_label = (
        'Нет группы'
        if status == PlanAssignmentStatus.NO_PLAN_GROUP
        else 'Нет плана'
        if status == PlanAssignmentStatus.NO_ACTIVE_PLAN
        else 'Не назначен'
        if status == DISPATCHER_PLAN_NOT_ASSIGNED
        else status_label
    )
    has_plan = percent_value is not None
    fact_plan_label = f'{fact_display} / {plan_display} {unit}'.strip() if has_plan else status_label
    percent_label = f'{percent_value}%' if has_plan else short_label
    visual = progress_cycle_visual_context(percent_value if has_plan else 0)
    return {
        'percent': percent_value,
        'css_percent': percent_value if percent_value is not None else 0,
        'status': status,
        'status_key': plan_progress_status_key(percent_value, status),
        'status_label': status_label,
        'short_label': short_label,
        'has_plan': has_plan,
        'value': plan_value,
        'value_display': plan_display,
        'fact_value': fact_value,
        'fact_display': fact_display,
        'fact_plan_label': fact_plan_label,
        'percent_label': percent_label,
        'unit': unit,
        'calculation_mode': calculation_mode,
        'group_name': progress.get('plan_group_name') if progress else '',
        'visual': visual,
        'loop_progress': visual['loop_progress'],
        'completed_loops': visual['completed_loops'],
        'progress_phase': visual['phase'],
        'has_completed_loops': visual['has_completed_loops'],
    }


def dispatcher_plan_api_payload(plan):
    plan = plan or plan_progress_display_context(None)
    visual = plan.get('visual') or progress_cycle_visual_context(plan['percent'] if plan['has_plan'] else 0)
    return {
        'plan_status': plan['status'],
        'plan_status_label': plan['status_label'],
        'plan_group_name': plan['group_name'],
        'plan_calculation_mode': plan['calculation_mode'],
        'plan_value': plan['value_display'],
        'completed_value': plan['fact_display'],
        'progress_percent': plan['percent'] if plan['has_plan'] else None,
        'progress_loop_percent': visual['loop_progress'] if plan['has_plan'] else None,
        'progress_completed_loops': visual['completed_loops'],
        'progress_phase': visual['phase'] if plan['has_plan'] else '',
        'unit': plan['unit'],
        'fact_plan_label': plan['fact_plan_label'],
    }


def downtime_event_payload(event, *, action='', closed=False):
    now = timezone.now()
    started_at = event.started_at or now
    ended_at = event.ended_at
    elapsed_until = ended_at or now
    elapsed_seconds = max(0, int((elapsed_until - started_at).total_seconds()))
    equipment_state_map = get_equipment_state_ui_map()
    equipment_state_code = downtime_equipment_state_code(event)
    state_ui = downtime_reason_state_ui(equipment_state_map, getattr(event, 'reason', None))
    return {
        'ok': True,
        'action': action,
        'active': not bool(ended_at),
        'closed': bool(closed),
        'event_id': event.id,
        'reason_id': event.reason_id,
        'reason': str(event.reason) if event.reason_id else '',
        'started_at': started_at.isoformat(),
        'ended_at': ended_at.isoformat() if ended_at else '',
        'elapsed_seconds': elapsed_seconds,
        'elapsed_label': format_duration_label(elapsed_seconds),
        'equipment_state_code': equipment_state_code,
        'status_key': state_ui['color_group'],
        'status_label': state_ui['label'],
        'version': get_operational_state_version(),
    }


def get_operational_state_version():
    state = (
        OperationalStateVersion.objects
        .filter(key='production')
        .only('version')
        .first()
    )
    return state.version if state else 0

DISPATCHER_MANIFEST = {
    'id': '/dispatcher/control/',
    'name': 'Горный диспетчер',
    'short_name': 'Диспетчер',
    'description': 'Рабочий экран Горного диспетчера для управления активной сменой, комплексами и техникой.',
    'start_url': '/dispatcher/control/',
    'scope': '/dispatcher/',
    'display': 'standalone',
    'display_override': ['standalone', 'fullscreen'],
    'orientation': 'landscape',
    'background_color': '#07131f',
    'theme_color': '#07131f',
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
            'name': 'Пульт диспетчера',
            'short_name': 'Пульт',
            'url': '/dispatcher/control/',
            'description': 'Открыть рабочий экран Горного диспетчера.',
        },
    ],
}

DISPATCHER_SERVICE_WORKER_JS = r"""
const CACHE_NAME = "dispatcher-desktop-shell-v28";
const APP_SHELL_URL = "/dispatcher/control/";
const MANIFEST_URL = "/dispatcher.webmanifest";
const CORE_ASSETS = [
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
  "/static/img/equipment/excavator-green.png",
  "/static/img/equipment/excavator-yellow.png",
  "/static/img/equipment/excavator-blue.png",
  "/static/img/equipment/excavator-red.png",
  "/static/img/equipment/truck-gray.png",
  "/static/img/equipment/truck-green.png",
  "/static/img/equipment/truck-yellow.png",
  "/static/img/equipment/truck-blue.png",
  "/static/img/equipment/truck-red.png"
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(CORE_ASSETS.map(url => new Request(url, { cache: "reload" }))).catch(() => undefined))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

async function networkFirst(request, fallbackUrl) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      cache.put(request, response.clone()).catch(() => undefined);
      if (fallbackUrl) {
        cache.put(fallbackUrl, response.clone()).catch(() => undefined);
      }
    }
    return response;
  } catch (error) {
    return (await cache.match(request)) ||
      (fallbackUrl ? await cache.match(fallbackUrl) : null) ||
      new Response("Оффлайн: экран диспетчера еще не сохранен на этом устройстве.", {
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
  if (request.mode === "navigate" || url.pathname === APP_SHELL_URL) {
    event.respondWith(networkFirst(request, APP_SHELL_URL));
    return;
  }
  if (url.pathname === MANIFEST_URL) {
    event.respondWith(networkFirst(request, MANIFEST_URL));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(cacheFirst(request));
  }
});

self.addEventListener("message", event => {
  if (!event.data) return;
  if (event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
    return;
  }
  if (event.data.type === "GET_VERSION") {
    event.source && event.source.postMessage({
      type: "VERSION",
      version: CACHE_NAME
    });
  }
});
"""

EXCAVATOR_MANIFEST = {
    'id': '/excavator/work/',
    'name': 'Экскаваторщик',
    'short_name': 'Погрузка',
    'description': 'Мобильное рабочее место экскаваторщика для погрузки, забоя, смены и событий.',
    'start_url': '/excavator/work/',
    'scope': '/excavator/',
    'display': 'standalone',
    'display_override': ['standalone', 'fullscreen'],
    'orientation': 'portrait',
    'background_color': '#030708',
    'theme_color': '#030708',
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
            'name': 'Погрузка',
            'short_name': 'Погрузка',
            'url': '/excavator/work/',
            'description': 'Открыть основной рабочий экран экскаваторщика.',
        },
    ],
}

EXCAVATOR_SERVICE_WORKER_JS = r"""
const CACHE_NAME = "excavator-mobile-shell-v90";
const APP_SHELL_URL = "/excavator/work/";
const MANIFEST_URL = "/excavator.webmanifest";
const CORE_ASSETS = [
  APP_SHELL_URL,
  MANIFEST_URL,
  "/static/js/realtime-client.js",
  "/static/css/app.css",
  "/static/css/excavator-work-v55.css",
  "/static/css/excavator-work-v55-final.css",
  "/static/css/excavator-work-v55-shift.css",
  "/static/favicon.ico",
  "/static/img/pwa/mining-master-180.png",
  "/static/img/pwa/mining-master-192.png",
  "/static/img/pwa/mining-master-512.png",
  "/static/img/pwa/mining-master-maskable-512.png",
  "/static/img/equipment/excavator-gray.png",
  "/static/img/equipment/excavator-green.png",
  "/static/img/equipment/excavator-yellow.png",
  "/static/img/equipment/excavator-red.png",
  "/static/img/equipment/truck-gray.png",
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

async function networkFirst(request, fallbackUrl) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      cache.put(request, response.clone()).catch(() => undefined);
      if (fallbackUrl) {
        cache.put(fallbackUrl, response.clone()).catch(() => undefined);
      }
    }
    return response;
  } catch (error) {
    return (await cache.match(request)) ||
      (fallbackUrl ? await cache.match(fallbackUrl) : null) ||
      new Response("Offline: excavator shell is not cached on this device yet.", {
        status: 503,
        headers: { "Content-Type": "text/plain; charset=utf-8" }
      });
  }
}

async function networkOnly(request) {
  try {
    return await fetch(request);
  } catch (error) {
    return new Response("Network unavailable: fresh excavator data was not received.", {
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
  if (request.mode === "navigate" || url.pathname === APP_SHELL_URL) {
    event.respondWith(networkFirst(request, APP_SHELL_URL));
    return;
  }
  if (url.pathname === MANIFEST_URL) {
    event.respondWith(networkFirst(request, MANIFEST_URL));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(cacheFirst(request));
  }
});

self.addEventListener("message", event => {
  if (!event.data) return;
  if (event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
    return;
  }
  if (event.data.type === "GET_VERSION") {
    const target = event.ports && event.ports[0];
    const payload = {
      type: "VERSION",
      version: CACHE_NAME
    };
    if (target) {
      target.postMessage(payload);
      return;
    }
    event.source && event.source.postMessage(payload);
  }
});
"""


def format_dispatcher_number(value):
    value = int(value or 0)
    return f'{value:,}'.replace(',', ' ')


def format_dispatcher_datetime(value):
    if not value:
        return ''
    return timezone.localtime(value).strftime('%d.%m %H:%M')


def format_dispatcher_decimal(value):
    if value is None:
        return ''
    return f'{value:g}'


def dispatcher_manifest_view(request):
    response = JsonResponse(DISPATCHER_MANIFEST, json_dumps_params={'ensure_ascii': False})
    response['Content-Type'] = 'application/manifest+json; charset=utf-8'
    response['Cache-Control'] = 'no-cache'
    return response


def dispatcher_service_worker_view(request):
    response = HttpResponse(DISPATCHER_SERVICE_WORKER_JS, content_type='application/javascript; charset=utf-8')
    response['Cache-Control'] = 'no-cache'
    response['Service-Worker-Allowed'] = '/dispatcher/'
    return response


def excavator_manifest_view(request):
    response = JsonResponse(EXCAVATOR_MANIFEST, json_dumps_params={'ensure_ascii': False})
    response['Content-Type'] = 'application/manifest+json; charset=utf-8'
    response['Cache-Control'] = 'no-cache'
    return response


def excavator_service_worker_view(request):
    response = HttpResponse(EXCAVATOR_SERVICE_WORKER_JS, content_type='application/javascript; charset=utf-8')
    response['Cache-Control'] = 'no-cache'
    response['Service-Worker-Allowed'] = '/excavator/'
    return response


def equipment_short_name(equipment):
    if not equipment:
        return '-'
    return str(equipment.garage_number or equipment).replace('Экс ', 'EX-').replace('Экс', 'EX-')


def equipment_icon_key(equipment, status='green'):
    type_name = (getattr(getattr(equipment, 'equipment_type', None), 'name', '') or '').lower()
    prefix = 'excavator' if 'экскаватор' in type_name else 'truck'
    if status not in {'green', 'yellow', 'red', 'gray', 'blue', 'orange'}:
        status = 'gray'
    if status == 'orange':
        status = 'yellow'
    return f'img/equipment/{prefix}-{status}.png'


def authenticate_dispatcher_shared_shift_start(request):
    phone = request.POST.get('reauth_phone', '').strip()
    access_code = re.sub(r'\D', '', request.POST.get('reauth_access_code', ''))
    device_kind = request.POST.get('device_kind', '').strip()
    if not phone or not access_code:
        return None, 'Для начала смены на общем компьютере введите телефон и код горного диспетчера.'
    if phone and not phone.startswith(('+', '7', '8')):
        phone = f'+7 {phone}'

    access = find_employee_access_by_credentials(phone, access_code, role_code='dispatcher')
    if not access:
        return None, 'Телефон или код горного диспетчера указаны неверно.'

    request.session['employee_access_id'] = access.id
    set_session_device_kind(request, device_kind)
    access.last_login_at = timezone.now()
    access.save(update_fields=['last_login_at'])
    return access, ''


def dispatcher_truck_garage_number(truck, fallback_index):
    raw_number = str(getattr(truck, 'garage_number', '') or '').strip()
    match = re.search(r'\d+', raw_number)
    if match:
        number = int(match.group(0))
        if number == 53:
            return None
        return str(number)
    return None


def dispatcher_employee_badge(employee):
    if not employee:
        return None
    photo_url = ''
    if getattr(employee, 'photo', None):
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


def add_dispatcher_detail(details, seen_labels, label, value):
    if value in {None, ''} or label in seen_labels:
        return
    seen_labels.add(label)
    details.append({'label': label, 'value': str(value)})


def dispatcher_trip_amount(trip):
    return trip.tonnage or trip.volume_m3 or Decimal('0')


def format_whole_number(value):
    if value in {None, ''}:
        return ''
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    rounded = int(parsed.to_integral_value(rounding=ROUND_HALF_UP))
    return f'{rounded:,}'.replace(',', ' ')


def format_whole_value_with_unit(value, unit):
    formatted = format_whole_number(value)
    return f'{formatted} {unit}' if formatted and unit else formatted


def dispatcher_chart_percent(value, max_value):
    if not max_value:
        return 0
    return max(4, min(100, int((value / max_value) * 100)))


def dispatcher_summary_chart_rows(group_items, label_index, *, meta_index=None, max_rows=6):
    accents = ('green', 'blue', 'yellow', 'red')
    grouped_rows = defaultdict(lambda: {'volume': Decimal('0'), 'meta': set()})
    for key, row in group_items:
        label = key[label_index] or 'не указано'
        grouped_rows[label]['volume'] += row['volume']
        if meta_index is not None and key[meta_index]:
            grouped_rows[label]['meta'].add(key[meta_index])
    sorted_rows = sorted(grouped_rows.items(), key=lambda item: item[1]['volume'], reverse=True)[:max_rows]
    max_volume = max([row['volume'] for _, row in sorted_rows] or [Decimal('0')])
    return [
        {
            'label': label,
            'value': f'{format_dispatcher_number(row["volume"])} т',
            'percent': dispatcher_chart_percent(row['volume'], max_volume),
            'accent': accents[index % len(accents)],
            'meta': ', '.join(sorted(row['meta'])[:2]),
        }
        for index, (label, row) in enumerate(sorted_rows)
    ]


def dispatcher_trip_equipment_summary_rows(trips, *, equipment_attr='truck', max_rows=6):
    accents = ('green', 'blue', 'yellow', 'red')
    grouped_rows = defaultdict(lambda: Decimal('0'))
    for trip in trips:
        label = equipment_short_name(getattr(trip, equipment_attr, None))
        grouped_rows[label] += dispatcher_trip_amount(trip)
    sorted_rows = sorted(grouped_rows.items(), key=lambda item: item[1], reverse=True)[:max_rows]
    max_volume = max([volume for _, volume in sorted_rows] or [Decimal('0')])
    return [
        {
            'label': label,
            'value': f'{format_dispatcher_number(volume)} т',
            'percent': dispatcher_chart_percent(volume, max_volume),
            'accent': accents[index % len(accents)],
            'meta': 'текущая смена',
        }
        for index, (label, volume) in enumerate(sorted_rows)
    ]


def dispatcher_empty_shift_report(*, is_truck=False):
    counterpart_label = 'Экскаваторы' if is_truck else 'Самосвалы'
    return {
        'metrics': [
            {'label': 'Рейсы', 'value': '0'},
            {'label': 'Объем', 'value': '0 т'},
            {'label': 'Активные', 'value': '0'},
            {'label': 'Завершены', 'value': '0'},
            {'label': counterpart_label, 'value': '0'},
            {'label': 'Разгрузки', 'value': '0'},
        ],
        'charts': [
            {'type': 'route' if is_truck else 'matrix', 'title': 'Текущая смена', 'rows': []},
            {'type': 'donut-list', 'title': 'По разгрузке', 'rows': []},
            {'type': 'donut-list', 'title': 'По породе', 'rows': []},
            {'type': 'donut-list', 'title': 'По комплексам' if is_truck else 'По самосвалам', 'rows': []},
        ],
        'tables': [],
    }


def dispatcher_shift_report_for_equipment(equipment, *, equipment_kind='', shift_trips=None):
    equipment_type = (equipment_kind or getattr(getattr(equipment, 'equipment_type', None), 'name', '') or '').lower()
    is_truck = 'самосвал' in equipment_type
    is_excavator = 'экскаватор' in equipment_type
    trips = []
    if equipment and shift_trips:
        if is_truck:
            trips = [trip for trip in shift_trips if trip.truck_id == equipment.id]
        elif is_excavator:
            trips = [trip for trip in shift_trips if trip.excavator_id == equipment.id]
    if not trips:
        return dispatcher_empty_shift_report(is_truck=bool(is_truck))

    total_volume = sum((dispatcher_trip_amount(trip) for trip in trips), Decimal('0'))
    completed_count = sum(1 for trip in trips if trip.status == TripStatus.COMPLETED)
    active_count = sum(1 for trip in trips if trip.status in OPEN_TRIP_STATUSES)
    dump_points = {str(trip.dump_point) for trip in trips}
    counterpart_ids = {trip.excavator_id if is_truck else trip.truck_id for trip in trips}
    metrics = [
        {'label': 'Рейсы', 'value': str(len(trips))},
        {'label': 'Объем', 'value': f'{format_dispatcher_number(total_volume)} т'},
        {'label': 'Активные', 'value': str(active_count)},
        {'label': 'Завершены', 'value': str(completed_count)},
    ]
    if is_truck:
        metrics.append({'label': 'Экскаваторы', 'value': str(len(counterpart_ids))})
    else:
        metrics.append({'label': 'Самосвалы', 'value': str(len(counterpart_ids))})
    metrics.append({'label': 'Разгрузки', 'value': str(len(dump_points))})

    grouped = {}
    for trip in trips:
        if is_truck:
            key = (equipment_short_name(trip.excavator), str(trip.dump_point), str(trip.rock_type))
        else:
            face = ' / '.join(part for part in [trip.loading_horizon, trip.loading_block] if part) or 'не указан'
            key = (face, str(trip.dump_point), str(trip.rock_type))
        row = grouped.setdefault(key, {'count': 0, 'volume': Decimal('0'), 'last': None, 'trucks': set()})
        row['count'] += 1
        row['volume'] += dispatcher_trip_amount(trip)
        row['last'] = max(row['last'] or trip.created_at, trip.completed_at or trip.created_at)
        if is_excavator:
            row['trucks'].add(equipment_short_name(trip.truck))

    sorted_groups = sorted(grouped.items(), key=lambda item: item[1]['volume'], reverse=True)[:6]
    max_volume = max([row['volume'] for _, row in sorted_groups] or [Decimal('0')])
    rows = []
    chart_rows = []
    for key, row in sorted_groups:
        if is_truck:
            rows.append([key[0], key[1], key[2], str(row['count']), f'{format_dispatcher_number(row["volume"])} т', format_dispatcher_datetime(row['last'])])
            chart_rows.append({
                'source': key[0],
                'target': key[1],
                'meta': key[2],
                'value': f'{format_dispatcher_number(row["volume"])} т',
                'percent': dispatcher_chart_percent(row['volume'], max_volume),
                'accent': 'green' if len(chart_rows) == 0 else 'blue' if len(chart_rows) == 1 else 'yellow',
            })
        else:
            rows.append([key[0], key[1], key[2], str(len(row['trucks'])), str(row['count']), f'{format_dispatcher_number(row["volume"])} т'])
            chart_rows.append({
                'label': key[0],
                'target': key[1],
                'meta': key[2],
                'value': f'{format_dispatcher_number(row["volume"])} т',
                'percent': dispatcher_chart_percent(row['volume'], max_volume),
                'accent': 'green' if len(chart_rows) == 0 else 'yellow' if len(chart_rows) == 1 else 'blue',
            })

    if is_truck:
        return {
            'metrics': metrics[:6],
            'charts': [
                {
                    'type': 'route',
                    'title': 'Маршруты',
                    'rows': chart_rows,
                },
                {
                    'type': 'donut-list',
                    'title': 'По разгрузке',
                    'rows': dispatcher_summary_chart_rows(grouped.items(), 1, meta_index=2),
                },
                {
                    'type': 'donut-list',
                    'title': 'По породе',
                    'rows': dispatcher_summary_chart_rows(grouped.items(), 2, meta_index=1),
                },
                {
                    'type': 'donut-list',
                    'title': 'По комплексам',
                    'rows': dispatcher_summary_chart_rows(grouped.items(), 0, meta_index=1),
                },
            ],
            'tables': [],
        }
    return {
        'metrics': metrics[:6],
        'charts': [
            {
                'type': 'matrix',
                'title': 'По забоям',
                'rows': chart_rows,
            },
            {
                'type': 'donut-list',
                'title': 'По разгрузке',
                'rows': dispatcher_summary_chart_rows(grouped.items(), 1, meta_index=2),
            },
            {
                'type': 'donut-list',
                'title': 'По породе',
                'rows': dispatcher_summary_chart_rows(grouped.items(), 2, meta_index=1),
            },
            {
                'type': 'donut-list',
                'title': 'По самосвалам',
                'rows': dispatcher_trip_equipment_summary_rows(trips, equipment_attr='truck'),
            },
        ],
        'tables': [],
    }

def dispatcher_complex_truck_rows(card):
    return list(card.get('truck_rows') or [])


def dispatcher_tons_from_label(value):
    if not value:
        return Decimal('0')
    digits = ''.join(char for char in str(value) if char.isdigit())
    return Decimal(digits or '0')


def dispatcher_complex_face_label(card):
    horizon = card.get('current_horizon') or ''
    block = card.get('current_block') or ''
    label = ' / '.join(part for part in [horizon, block] if part and '-' not in part)
    return label or 'Забой не указан'


def dispatcher_complex_location_parts(card):
    return (card.get('current_horizon') or 'Гор. -', card.get('current_block') or 'Блок -')


def dispatcher_complex_shift_report(card):
    status_key = card.get('status_key') or 'green'
    assigned = int(card.get('assigned') or 0)
    need = int(card.get('need') or 0)
    balance = assigned - need
    percent = int(card.get('percent') or 0)
    truck_rows = dispatcher_complex_truck_rows(card)
    current_truck_rows = [row for row in truck_rows if row['state_key'] == 'current']
    removed_truck_rows = [row for row in truck_rows if row['state_key'] == 'removed']
    plan_context = card.get('plan') or {}
    plan_unit = plan_context.get('unit') or 'т'
    plan_value = f'{plan_context.get("value_display")} {plan_unit}'.strip() if plan_context.get('value_display') else f'{card.get("plan_tons", "0")} т'
    fact_value = plan_context.get('fact_plan_label') or f'{card.get("fact_tons", "0")} т'
    forecast_value = f'{card.get("forecast_tons", "0")} т'
    if status_key == 'red':
        problem = 'работа заблокирована'
        action = 'ремонт / перераспределить самосвалы'
    elif status_key == 'orange':
        problem = 'техническое ограничение'
        action = 'контроль ремонта или ТО'
    elif status_key == 'yellow':
        problem = 'ожидает действия'
        action = 'добавить транспорт'
    elif status_key == 'blue':
        problem = 'назначен'
        action = 'дождаться активной операции'
    else:
        problem = 'без отклонений'
        action = 'контроль нормы'

    def grouped_chart_rows(source_rows, field, meta_field):
        totals = defaultdict(Decimal)
        meta = defaultdict(set)
        for row in source_rows:
            label = row.get(field) or 'не указано'
            totals[label] += dispatcher_tons_from_label(row.get('value'))
            if row.get(meta_field):
                meta[label].add(row.get(meta_field))
        sorted_rows = sorted(totals.items(), key=lambda item: item[1], reverse=True)
        max_value = max((value for _, value in sorted_rows), default=Decimal('0'))
        accents = ('green', 'blue', 'yellow', 'red')
        return [
            {
                'label': label,
                'meta': ', '.join(sorted(meta[label])[:3]),
                'value': f'{format_dispatcher_number(value)} т',
                'percent': dispatcher_chart_percent(value, max_value) if max_value else 0,
                'accent': accents[index % len(accents)],
            }
            for index, (label, value) in enumerate(sorted_rows)
        ]

    material_rows = grouped_chart_rows(truck_rows, 'rock', 'target')
    unload_rows = grouped_chart_rows(truck_rows, 'target', 'rock')
    return {
        'metrics': [
            {'label': 'План', 'value': plan_value},
            {'label': 'Факт', 'value': fact_value},
            {'label': 'Самосвалы', 'value': f'{assigned} / {need}'},
            {'label': 'Работали', 'value': str(len(truck_rows))},
            {'label': 'Выведены', 'value': str(len(removed_truck_rows))},
        ],
        'charts': [
            {
                'type': 'bar',
                'title': 'План / факт',
                'rows': [
                    {'label': 'Факт / план', 'meta': plan_context.get('group_name') or 'snapshot смены', 'value': fact_value, 'percent': max(4, percent), 'accent': status_key if status_key in {'green', 'yellow', 'blue', 'orange', 'red', 'gray'} else 'green'},
                    {'label': 'Прогноз', 'meta': 'ожидаемый итог', 'value': forecast_value, 'percent': min(100, max(4, percent + 8)), 'accent': 'blue'},
                    {'label': 'План', 'meta': 'сменное задание', 'value': plan_value, 'percent': 100, 'accent': 'green'},
                ],
            },
            {
                'type': 'donut-list',
                'title': 'Порода',
                'rows': material_rows,
            },
            {
                'type': 'donut-list',
                'title': 'Разгрузка',
                'rows': unload_rows,
            },
            {
                'type': 'truck-ledger',
                'title': 'Самосвалы',
                'rows': truck_rows,
            },
            {
                'type': 'bar',
                'title': 'Баланс',
                'rows': [
                    {'label': 'Назначено', 'meta': 'самосвалы в комплексе', 'value': str(assigned), 'percent': dispatcher_chart_percent(Decimal(assigned), Decimal(max(need, assigned, 1))), 'accent': 'green' if assigned >= need else 'yellow'},
                    {'label': 'Нужно', 'meta': 'расчетная потребность', 'value': str(need), 'percent': 100, 'accent': 'blue'},
                    {'label': 'Баланс', 'meta': action, 'value': f'+{balance}' if balance > 0 else str(balance), 'percent': dispatcher_chart_percent(Decimal(abs(balance)), Decimal(max(need, 1))), 'accent': 'red' if balance < 0 else 'green'},
                ],
            },
        ],
        'tables': [],
        'problem': problem,
        'truck_rows': truck_rows,
        'current_trucks': [row['truck'] for row in current_truck_rows],
        'removed_trucks': [row['truck'] for row in removed_truck_rows],
    }


def build_dispatcher_equipment_card(
    *,
    card_id,
    equipment=None,
    type_name='',
    number='',
    icon='',
    status='gray',
    status_label='',
    zone='',
    percent=0,
    employee=None,
    details=None,
    shift_report=None,
    category='equipment',
    plan=None,
):
    card_details = []
    seen_labels = set()
    if equipment:
        type_name = type_name or equipment.equipment_type.name
        number = number or equipment_short_name(equipment)
        model = equipment.model
        add_dispatcher_detail(card_details, seen_labels, 'Гаражный N', equipment.garage_number)
        if equipment.vin:
            add_dispatcher_detail(card_details, seen_labels, 'VIN/серийный N', equipment.vin)
        add_dispatcher_detail(card_details, seen_labels, 'Модель', model.name if model else 'не указана')
        if model and model.payload_tons:
            add_dispatcher_detail(card_details, seen_labels, 'ГП, т', format_dispatcher_decimal(model.payload_tons))
        if model and model.body_volume_m3:
            add_dispatcher_detail(card_details, seen_labels, 'Кузов/ковш, м3', format_dispatcher_decimal(model.body_volume_m3))
    for row in details or []:
        add_dispatcher_detail(card_details, seen_labels, row.get('label'), row.get('value'))
    return {
        'id': str(card_id),
        'type': type_name,
        'label': number,
        'number': number,
        'icon': icon,
        'status_key': status,
        'status_label': status_label,
        'zone': zone,
        'percent': percent,
        'employee': dispatcher_employee_badge(employee),
        'details': card_details,
        'shift_report': shift_report or {},
        'category': category,
        'plan': dispatcher_plan_api_payload(plan),
    }


def build_dispatcher_dashboard_context(*, dispatcher_shift, active_trips, pending_assignments, accepted_assignments, recent_completed_trips, open_shifts, open_mechanic_downtimes, trucks, excavators, recent_dispatcher_actions):
    active_trips_list = list(active_trips)
    pending_assignments_list = list(pending_assignments)
    accepted_assignments_list = list(accepted_assignments)
    assignment_by_truck = {}
    for assignment in accepted_assignments_list + pending_assignments_list:
        current = assignment_by_truck.get(assignment.truck_id)
        if current is None:
            assignment_by_truck[assignment.truck_id] = assignment
            continue
        current_time = current.accepted_at or current.assigned_at or current.created_at
        assignment_time = assignment.accepted_at or assignment.assigned_at or assignment.created_at
        if (assignment_time, assignment.id or 0) >= (current_time, current.id or 0):
            assignment_by_truck[assignment.truck_id] = assignment
    active_assignments_list = list(assignment_by_truck.values())
    pending_assignments_list = [
        assignment for assignment in active_assignments_list
        if assignment.status == AssignmentStatus.PENDING
    ]
    accepted_assignments_list = [
        assignment for assignment in active_assignments_list
        if assignment.status == AssignmentStatus.ACCEPTED
    ]
    recent_completed_trips_list = list(recent_completed_trips)
    open_downtime_list = list(open_mechanic_downtimes)
    trucks_list = list(trucks)
    excavators_list = list(excavators)
    shift_trip_queryset = Trip.objects.none()
    if dispatcher_shift:
        shift_trip_queryset = (
            Trip.objects
            .filter(created_at__gte=dispatcher_shift.opened_at)
            .select_related('truck', 'excavator', 'rock_type', 'dump_point')
            .order_by('-created_at')
        )
    shift_trips = list(shift_trip_queryset[:500])
    open_shift_by_equipment_id = {}
    for shift in open_shifts:
        if shift.equipment_id and shift.equipment_id not in open_shift_by_equipment_id:
            open_shift_by_equipment_id[shift.equipment_id] = shift
    plan_by_equipment_id = {}

    def dispatcher_plan_for_equipment(equipment):
        equipment_id = getattr(equipment, 'id', None)
        if not equipment_id:
            return plan_progress_display_context(None)
        if equipment_id not in plan_by_equipment_id:
            shift = open_shift_by_equipment_id.get(equipment_id)
            if shift and equipment_is_truck(shift.equipment):
                progress = calculate_truck_shift_progress(equipment, reference_shift=shift)
            else:
                progress = calculate_dispatcher_snapshot_progress(shift, equipment=equipment)
            plan_by_equipment_id[equipment_id] = plan_progress_display_context(progress)
        return plan_by_equipment_id[equipment_id]

    def dispatcher_plan_details(plan):
        if not plan:
            return []
        rows = [
            {'label': 'Статус плана', 'value': plan.get('status_label')},
            {'label': 'Факт / план', 'value': plan.get('fact_plan_label')},
        ]
        if plan.get('has_plan'):
            rows.insert(1, {'label': 'Выполнение плана', 'value': plan.get('percent_label')})
        if plan.get('group_name'):
            rows.append({'label': 'Группа плана', 'value': plan.get('group_name')})
        return rows

    downtime_by_equipment_id = {}
    for downtime in open_downtime_list:
        downtime_by_equipment_id.setdefault(downtime.equipment_id, downtime)
    active_trip_by_truck_id = {}
    active_trip_by_excavator_id = {}
    for trip in active_trips_list:
        active_trip_by_truck_id.setdefault(trip.truck_id, trip)
        active_trip_by_excavator_id.setdefault(trip.excavator_id, trip)
    latest_trip_by_equipment_id = {}
    for trip in recent_completed_trips_list:
        latest_trip_by_equipment_id.setdefault(trip.truck_id, trip)
        latest_trip_by_equipment_id.setdefault(trip.excavator_id, trip)
    assignment_by_truck_id = {}
    for assignment in accepted_assignments_list + pending_assignments_list:
        assignment_by_truck_id.setdefault(assignment.truck_id, assignment)
    equipment_cards = {}
    equipment_state_map = get_equipment_state_ui_map()

    def equipment_state_for(code):
        state = equipment_state_ui(equipment_state_map, code)
        return state['color_group'], state['label'], state['code']

    def complex_equipment_state(excavator, row):
        if not excavator or not getattr(excavator, 'is_active', True):
            return equipment_state_for('inactive')
        downtime_state_code = downtime_state_code_for(excavator.id)
        if downtime_state_code:
            return equipment_state_for(downtime_state_code)
        if row.get('pending'):
            return equipment_state_for('waiting')
        if row.get('active_trips'):
            return equipment_state_for('working')
        if row.get('accepted') or excavator.id in active_excavator_ids:
            return equipment_state_for('assigned')
        return equipment_state_for('garage')

    def status_label_for(status, label=''):
        return label or ''

    def downtime_state_code_for(equipment_id):
        downtime = downtime_by_equipment_id.get(equipment_id)
        if not downtime:
            return None
        return downtime_equipment_state_code(downtime)

    def downtime_reason_label_for(equipment_id):
        downtime = downtime_by_equipment_id.get(equipment_id)
        if not downtime or not getattr(downtime, 'reason', None):
            return ''
        return downtime.reason.button_label or downtime.reason.name or str(downtime.reason)

    def excavator_current_state(excavator):
        if not getattr(excavator, 'is_active', True):
            return equipment_state_for('inactive')
        downtime_state_code = downtime_state_code_for(excavator.id)
        if downtime_state_code:
            return equipment_state_for(downtime_state_code)
        if active_trip_by_excavator_id.get(excavator.id):
            return equipment_state_for('working')
        if excavator.id in active_excavator_ids:
            return equipment_state_for('assigned')
        return equipment_state_for('garage')

    def truck_current_state(truck):
        if not getattr(truck, 'is_active', True):
            return equipment_state_for('inactive')
        downtime_state_code = downtime_state_code_for(truck.id)
        if downtime_state_code:
            return equipment_state_for(downtime_state_code)
        active_trip = active_trip_by_truck_id.get(truck.id)
        if active_trip:
            if active_trip.status in OPEN_TRIP_STATUSES:
                return equipment_state_for('loaded_waiting_unload')
        assignment = assignment_by_truck_id.get(truck.id)
        if assignment and assignment.status == AssignmentStatus.PENDING:
            return equipment_state_for('waiting')
        if assignment and assignment.status == AssignmentStatus.ACCEPTED:
            return equipment_state_for('assigned')
        return equipment_state_for('free')

    def shift_details(equipment):
        shift = open_shift_by_equipment_id.get(equipment.id) if equipment else None
        if not shift:
            return []
        return [
            {'label': 'Смена', 'value': shift.get_shift_type_display()},
            {'label': 'Смена открыта', 'value': format_dispatcher_datetime(shift.opened_at)},
        ]

    completed_tons = Decimal('0')
    if dispatcher_shift:
        completed_tons = (
            Trip.objects
            .filter(status=TripStatus.COMPLETED, completed_at__gte=dispatcher_shift.opened_at)
            .aggregate(total=Sum('tonnage'))['total']
            or Decimal('0')
        )
    if dispatcher_shift and completed_tons == 0:
        completed_tons = (
            Trip.objects
            .filter(status=TripStatus.COMPLETED, completed_at__gte=dispatcher_shift.opened_at)
            .aggregate(total=Sum('volume_m3'))['total']
            or Decimal('0')
        )
    active_volume = sum((trip.tonnage or trip.volume_m3 or Decimal('0')) for trip in active_trips_list) if dispatcher_shift else Decimal('0')
    fact_tons = completed_tons + active_volume
    display_fact_tons = fact_tons
    forecast_tons = min(DISPATCHER_PLAN_TOTAL_TONS, display_fact_tons)
    completion_percent = int((display_fact_tons / DISPATCHER_PLAN_TOTAL_TONS) * 100) if DISPATCHER_PLAN_TOTAL_TONS else 0
    completion_percent = max(0, min(99, completion_percent))
    deficit_tons = forecast_tons - DISPATCHER_PLAN_TOTAL_TONS

    by_excavator = defaultdict(lambda: {
        'pending': 0,
        'accepted': 0,
        'active_trips': 0,
        'volume': Decimal('0'),
        'trucks': set(),
    })
    for assignment in pending_assignments_list:
        row = by_excavator[assignment.excavator_id]
        row['pending'] += 1
        row['trucks'].add(assignment.truck_id)
    for assignment in accepted_assignments_list:
        row = by_excavator[assignment.excavator_id]
        row['accepted'] += 1
        row['trucks'].add(assignment.truck_id)
    if dispatcher_shift:
        for trip in active_trips_list:
            row = by_excavator[trip.excavator_id]
            row['active_trips'] += 1
            row['volume'] += trip.tonnage or trip.volume_m3 or Decimal('0')
            row['trucks'].add(trip.truck_id)

    active_downtime_ids = {downtime.equipment_id for downtime in open_downtime_list}
    active_trip_truck_ids = {trip.truck_id for trip in active_trips_list}
    active_placement_ids = set(
        ExcavatorPlacement.objects
        .filter(zone=ExcavatorPlacement.Zone.ACTIVE, excavator__in=excavators_list)
        .values_list('excavator_id', flat=True)
    )
    active_excavator_ids = set(active_placement_ids)
    active_excavator_ids.update(assignment.excavator_id for assignment in pending_assignments_list + accepted_assignments_list if assignment.excavator_id)
    active_excavator_ids.update(trip.excavator_id for trip in active_trips_list if trip.excavator_id)

    def garage_number_int(equipment):
        match = re.search(r'\d+', str(getattr(equipment, 'garage_number', '') or ''))
        return int(match.group(0)) if match else 9999

    def complex_number_int(card):
        match = re.search(r'\d+', str(card.get('id', '') or ''))
        return int(match.group(0)) if match else 9999

    excavator_by_id = {excavator.id: excavator for excavator in excavators_list}
    shown_excavators = sorted(
        [excavator_by_id[equipment_id] for equipment_id in active_excavator_ids if equipment_id in excavator_by_id],
        key=garage_number_int,
    )

    trips_by_excavator_id = defaultdict(list)
    for trip in shift_trips:
        if trip.excavator_id:
            trips_by_excavator_id[trip.excavator_id].append(trip)

    complex_cards = []
    for excavator in shown_excavators:
        index = garage_number_int(excavator)
        row = by_excavator[excavator.id]
        need = max(len(row['trucks']), row['accepted'] + row['pending'], 0)
        assigned = row['accepted'] + row['active_trips']
        plan = Decimal('0')
        fact = row['volume']
        excavator_plan = dispatcher_plan_for_equipment(excavator)
        percent = excavator_plan['css_percent']
        status_key, status_label, equipment_state_code = complex_equipment_state(excavator, row)
        status_label = downtime_reason_label_for(excavator.id) or status_label

        complex_trips = trips_by_excavator_id.get(excavator.id, [])
        current_horizon = ''
        current_block = ''
        rock_values = []
        unload_totals = defaultdict(Decimal)
        truck_rows = []
        latest_trip = None
        for trip in complex_trips:
            if not latest_trip or (trip.completed_at or trip.created_at) > (latest_trip.completed_at or latest_trip.created_at):
                latest_trip = trip
            if trip.loading_horizon and not current_horizon:
                current_horizon = f'Гор. {trip.loading_horizon}'
            if trip.loading_block and not current_block:
                current_block = f'Блок {trip.loading_block}'
            if trip.rock_type:
                rock_values.append(str(trip.rock_type))
            if trip.dump_point:
                unload_totals[str(trip.dump_point)] += dispatcher_trip_amount(trip)

        current_assignments = [assignment for assignment in accepted_assignments_list + pending_assignments_list if assignment.excavator_id == excavator.id]
        current_truck_ids = {assignment.truck_id for assignment in current_assignments}
        volume_by_truck = defaultdict(Decimal)
        target_by_truck = {}
        rock_by_truck = {}
        for trip in complex_trips:
            if not trip.truck_id:
                continue
            volume_by_truck[trip.truck_id] += dispatcher_trip_amount(trip)
            if trip.dump_point:
                target_by_truck[trip.truck_id] = str(trip.dump_point)
            if trip.rock_type:
                rock_by_truck[trip.truck_id] = str(trip.rock_type)
        max_truck_volume = max(volume_by_truck.values(), default=Decimal('0'))
        truck_by_id = {truck.id: truck for truck in trucks_list}
        for truck_id in sorted(current_truck_ids, key=lambda item: garage_number_int(truck_by_id.get(item)) if item in truck_by_id else 9999):
            truck = truck_by_id.get(truck_id)
            if not truck:
                continue
            truck_status, truck_state_label, truck_state_code = truck_current_state(truck)
            truck_volume = volume_by_truck.get(truck_id, Decimal('0'))
            truck_plan = dispatcher_plan_for_equipment(truck)
            truck_rows.append({
                'truck': dispatcher_truck_garage_number(truck, 0) or equipment_short_name(truck),
                'truck_id': truck_id,
                'state_key': 'current',
                'state': truck_state_label,
                'equipment_state_code': truck_state_code,
                'target': target_by_truck.get(truck_id, ''),
                'rock': rock_by_truck.get(truck_id, ''),
                'value': f'{format_dispatcher_number(truck_volume)} т',
                'percent': truck_plan['css_percent'],
                'plan_visual': truck_plan['visual'],
                'accent': truck_status,
                'label': dispatcher_truck_garage_number(truck, 0) or equipment_short_name(truck),
                'meta': '',
                'plan': truck_plan,
                'plan_status': truck_plan['status'],
                'plan_status_label': truck_plan['status_label'],
                'plan_group_name': truck_plan['group_name'],
                'plan_calculation_mode': truck_plan['calculation_mode'],
                'plan_value': truck_plan['value_display'],
                'plan_fact_value': truck_plan['fact_display'],
                'plan_fact_label': truck_plan['fact_plan_label'],
                'plan_percent_label': truck_plan['percent_label'],
                'plan_unit': truck_plan['unit'],
                'plan_has_plan': truck_plan['has_plan'],
            })
        forecast = fact
        current_rock = rock_values[0] if rock_values else ''
        complex_cards.append({
            'id': f'K-{index}',
            'excavator_slot': index,
            'material': current_rock,
            'status_key': status_key,
            'status_label': status_label,
            'equipment_state_code': equipment_state_code,
            'percent': percent,
            'plan_visual': excavator_plan['visual'],
            'plan': excavator_plan,
            'plan_status': excavator_plan['status'],
            'plan_status_label': excavator_plan['status_label'],
            'plan_group_name': excavator_plan['group_name'],
            'plan_calculation_mode': excavator_plan['calculation_mode'],
            'plan_value': excavator_plan['value_display'],
            'plan_fact_value': excavator_plan['fact_display'],
            'plan_fact_label': excavator_plan['fact_plan_label'],
            'plan_percent_label': excavator_plan['percent_label'],
            'plan_unit': excavator_plan['unit'],
            'plan_has_plan': excavator_plan['has_plan'],
            'excavator': excavator,
            'excavator_name': equipment_short_name(excavator),
            'excavator_icon': equipment_icon_key(excavator, equipment_state_icon_color(status_key)),
            'truck_icon': f'img/equipment/truck-{equipment_state_icon_color(status_key)}.png',
            'assigned': assigned,
            'need': need,
            'plan_tons': format_dispatcher_number(plan),
            'fact_tons': format_dispatcher_number(fact),
            'forecast_tons': format_dispatcher_number(forecast),
            'card_id': f'complex-K-{index}',
            'equipment_card_id': str(excavator.id) if excavator else '',
            'truck_rows': truck_rows,
            'current_horizon': current_horizon,
            'current_block': current_block,
            'current_rock': current_rock,
        })

    excavator_tiles = []
    for index, excavator in enumerate(excavators_list[:12], start=1):
        board_number = garage_number_int(excavator)
        status, label, equipment_state_code = excavator_current_state(excavator)
        excavator_plan = dispatcher_plan_for_equipment(excavator)
        percent = excavator_plan['css_percent']
        excavator_tiles.append({
            'equipment': excavator,
            'name': equipment_short_name(excavator),
            'complex': f'K-{board_number}' if excavator.id in active_excavator_ids else '',
            'status': status,
            'label': label,
            'equipment_state_code': equipment_state_code,
            'percent': percent,
            'plan_visual': excavator_plan['visual'],
            'plan': excavator_plan,
            'plan_status': excavator_plan['status'],
            'plan_status_label': excavator_plan['status_label'],
            'plan_group_name': excavator_plan['group_name'],
            'plan_calculation_mode': excavator_plan['calculation_mode'],
            'plan_value': excavator_plan['value_display'],
            'plan_fact_value': excavator_plan['fact_display'],
            'plan_fact_label': excavator_plan['fact_plan_label'],
            'plan_percent_label': excavator_plan['percent_label'],
            'plan_unit': excavator_plan['unit'],
            'plan_has_plan': excavator_plan['has_plan'],
            'icon': equipment_icon_key(excavator, status),
            'card_id': str(excavator.id) if excavator else '',
            'board_number': board_number,
        })

    excavator_garage_tiles = []
    inactive_excavator_tiles = sorted(
        [tile for tile in excavator_tiles if tile.get('equipment') and tile['equipment'].id not in active_excavator_ids],
        key=lambda tile: tile.get('board_number') or 9999,
    )
    for index, tile in enumerate(inactive_excavator_tiles[:12], start=1):
        garage_tile = tile.copy()
        garage_tile['display_name'] = str(tile.get('board_number') or index)
        garage_tile['is_placeholder'] = False
        excavator_garage_tiles.append(garage_tile)
    while len(excavator_garage_tiles) < 12:
        index = len(excavator_garage_tiles) + 1
        excavator_garage_tiles.append({
            'equipment': None,
            'name': 'Будущий экскаватор',
            'status': 'empty',
            'label': 'резерв',
            'icon': 'img/equipment/excavator-gray.png',
            'board_number': index,
            'display_name': '',
            'percent': 0,
            'is_placeholder': True,
        })
    mobile_excavator_garage_tiles = [
        tile
        for tile in excavator_garage_tiles
        if not tile.get('is_placeholder')
    ]
    while len(mobile_excavator_garage_tiles) < 6 or len(mobile_excavator_garage_tiles) % 2:
        index = len(mobile_excavator_garage_tiles) + 1
        mobile_excavator_garage_tiles.append({
            'equipment': None,
            'name': 'Будущий экскаватор',
            'status': 'empty',
            'label': 'резерв',
            'icon': 'img/equipment/excavator-gray.png',
            'board_number': index,
            'display_name': '',
            'percent': 0,
            'is_placeholder': True,
        })
    mobile_excavator_garage_row_count = max(3, math.ceil(len(mobile_excavator_garage_tiles) / 2))

    total_trucks = len(trucks_list)
    accepted_truck_ids = {assignment.truck_id for assignment in accepted_assignments_list}
    pending_truck_ids = {assignment.truck_id for assignment in pending_assignments_list}
    downtime_truck_ids = active_downtime_ids & {truck.id for truck in trucks_list}
    working_trucks = len(accepted_truck_ids | active_trip_truck_ids)
    waiting_trucks = len(pending_truck_ids - accepted_truck_ids)
    repair_trucks = len(downtime_truck_ids)
    loading_trucks = len(active_trip_truck_ids)

    balance_rows = []
    for card in complex_cards:
        balance = card['assigned'] - card['need']
        complex_report = dispatcher_complex_shift_report(card)
        current_trucks = complex_report.get('current_trucks') or []
        removed_trucks = complex_report.get('removed_trucks') or []
        current_truck_rows = [row for row in (complex_report.get('truck_rows') or []) if row.get('state_key') == 'current']
        truck_tiles = []
        for row in current_truck_rows:
            status = row.get('accent') if row.get('accent') in {'green', 'yellow', 'red', 'gray', 'blue', 'orange'} else 'gray'
            truck = truck_by_id.get(row.get('truck_id'))
            row_state_code = row.get('equipment_state_code') or ''
            row_state = equipment_state_ui(equipment_state_map, row_state_code) if row_state_code else None
            row_plan = row.get('plan') or plan_progress_display_context(None)
            truck_tiles.append({
                'name': row.get('truck'),
                'status': status,
                'label': row.get('state') or (row_state['label'] if row_state else ''),
                'equipment_state_code': row_state_code,
                'icon': equipment_icon_key(truck, status),
                'percent': row.get('percent') or 0,
                'card_id': str(row.get('truck_id') or ''),
                'plan': row_plan,
                'plan_visual': row_plan['visual'],
                'plan_status': row.get('plan_status') or DISPATCHER_PLAN_NOT_ASSIGNED,
                'plan_status_label': row.get('plan_status_label') or plan_status_label(DISPATCHER_PLAN_NOT_ASSIGNED),
                'plan_group_name': row.get('plan_group_name') or '',
                'plan_calculation_mode': row.get('plan_calculation_mode') or '',
                'plan_value': row.get('plan_value') or '',
                'plan_fact_value': row.get('plan_fact_value') or '',
                'plan_fact_label': row.get('plan_fact_label') or plan_status_label(DISPATCHER_PLAN_NOT_ASSIGNED),
                'plan_percent_label': row.get('plan_percent_label') or 'Не назначен',
                'plan_unit': row.get('plan_unit') or '',
                'plan_has_plan': bool(row.get('plan_has_plan')),
            })
        unload_totals = {}
        for row in current_truck_rows:
            target = row.get('target')
            tons = dispatcher_tons_from_label(row.get('value'))
            if target and tons > 0:
                unload_totals[target] = unload_totals.get(target, Decimal('0')) + tons
        total_unload_tons = sum(unload_totals.values(), Decimal('0'))
        unload_points = []
        for target, tons in unload_totals.items():
            if total_unload_tons <= 0:
                continue
            unload_points.append({
                'name': target,
                'percent': int((tons * Decimal('100') / total_unload_tons).quantize(Decimal('1'))),
            })
        rock_values = [row.get('rock') for row in current_truck_rows if row.get('rock')]
        current_rock = rock_values[0] if rock_values else (card.get('material') or '')
        if card['status_key'] == 'red':
            attention_label = 'Комплекс остановлен, состав под контролем'
        elif card['status_key'] == 'orange':
            attention_label = 'Техника на ремонте или обслуживании'
        elif card['status_key'] == 'yellow':
            attention_label = 'Нужна проверка транспорта и маршрута'
        elif card['status_key'] == 'blue':
            attention_label = 'Комплекс назначен, активной операции нет'
        else:
            attention_label = 'Работает по плану'
        current_horizon, current_block = dispatcher_complex_location_parts(card)
        card.update({
            'balance': balance,
            'balance_label': f'+{balance}' if balance > 0 else str(balance),
            'balance_status': 'plus' if balance > 0 else 'minus' if balance < 0 else 'zero',
            'current_trucks': current_trucks,
            'removed_trucks': removed_trucks,
            'active_truck_tiles': truck_tiles,
            'truck_scale_class': 'truck-fill-1' if len(truck_tiles) <= 6 else 'truck-fill-2' if len(truck_tiles) <= 12 else 'truck-fill-3' if len(truck_tiles) <= 18 else 'truck-fill-4',
            'truck_column_count': 6,
            'truck_preview': current_trucks[:6],
            'truck_overflow': max(len(current_trucks) - 6, 0),
            'mobile_truck_overflow': max(len(current_trucks) - 16, 0),
            'current_face': dispatcher_complex_face_label(card),
            'current_horizon': current_horizon,
            'current_block': current_block,
            'current_rock': current_rock,
            'unload_points': unload_points,
            'attention_label': attention_label,
        })
        balance_rows.append({
            'complex': card['id'],
            'assigned': card['assigned'],
            'need': card['need'],
            'balance': balance,
            'balance_label': f'+{balance}' if balance > 0 else str(balance),
            'status': 'plus' if balance > 0 else 'minus' if balance < 0 else 'zero',
        })

    status_order = {
        'red': 0,
        'danger': 0,
        'orange': 1,
        'yellow': 2,
        'risk': 2,
        'blue': 3,
        'green': 4,
        'normal': 4,
        'gray': 5,
    }
    complex_zones = sorted(complex_cards, key=lambda card: (status_order.get(card['status_key'], 3), complex_number_int(card)))
    while len(complex_zones) < 9:
        index = len(complex_zones) + 1
        complex_zones.append({
            'id': f'K-{index}',
            'is_empty': True,
            'status_key': 'empty',
            'status_label': 'СВОБОДНАЯ ЗОНА',
            'equipment_state_code': 'inactive',
            'percent': 0,
            'material': '',
            'excavator_name': '',
            'excavator_icon': 'img/equipment/excavator-gray.png',
            'truck_icon': 'img/equipment/truck-gray.png',
            'assigned': 0,
            'need': 0,
            'plan_tons': '0',
            'fact_tons': '0',
            'forecast_tons': '0',
            'card_id': '',
            'equipment_card_id': '',
            'balance': 0,
            'balance_label': '0',
            'balance_status': 'zero',
            'current_trucks': [],
            'removed_trucks': [],
            'active_truck_tiles': [],
            'truck_scale_class': 'truck-fill-1',
            'truck_column_count': 1,
            'truck_preview': [],
            'truck_overflow': 0,
            'mobile_truck_overflow': 0,
            'current_face': '',
            'current_rock': '',
            'unload_points': [],
            'attention_label': '',
        })
    mobile_complex_zones = [
        zone
        for zone in complex_zones
        if not zone.get('is_empty')
    ]
    mobile_empty_complex_zones = [
        zone
        for zone in complex_zones
        if zone.get('is_empty')
    ]
    while len(mobile_complex_zones) < 6 or len(mobile_complex_zones) % 2:
        if mobile_empty_complex_zones:
            mobile_complex_zones.append(mobile_empty_complex_zones.pop(0))
        else:
            index = len(mobile_complex_zones) + 1
            mobile_complex_zones.append({
                'id': f'K-{index}',
                'is_empty': True,
                'status_key': 'empty',
                'status_label': 'СВОБОДНАЯ ЗОНА',
                'equipment_state_code': 'inactive',
                'percent': 0,
                'material': '',
                'excavator_name': '',
                'excavator_icon': 'img/equipment/excavator-gray.png',
                'truck_icon': 'img/equipment/truck-gray.png',
                'assigned': 0,
                'need': 0,
                'plan_tons': '0',
                'fact_tons': '0',
                'forecast_tons': '0',
                'card_id': '',
                'equipment_card_id': '',
                'balance': 0,
                'balance_label': '0',
                'balance_status': 'zero',
                'current_trucks': [],
                'removed_trucks': [],
                'active_truck_tiles': [],
                'truck_scale_class': 'truck-fill-1',
                'truck_column_count': 1,
                'truck_preview': [],
                'truck_overflow': 0,
                'mobile_truck_overflow': 0,
                'current_face': '',
                'current_rock': '',
                'unload_points': [],
                'attention_label': '',
            })

    assigned_truck_ids = accepted_truck_ids | pending_truck_ids
    active_complex_truck_names = {
        str(truck.get('name'))
        for card in complex_cards
        for truck in card.get('active_truck_tiles', [])
        if truck.get('name')
    }
    truck_garage_tiles = []
    for index, truck in enumerate([truck for truck in trucks_list if truck.id not in assigned_truck_ids], start=1):
        if len(truck_garage_tiles) >= 52:
            break
        truck_number = dispatcher_truck_garage_number(truck, len(truck_garage_tiles) + 1)
        if truck_number is None:
            continue
        if str(truck_number) in active_complex_truck_names:
            continue
        status, label, equipment_state_code = truck_current_state(truck)
        truck_plan = dispatcher_plan_for_equipment(truck)
        truck_garage_tiles.append({
            'equipment': truck,
            'name': truck_number,
            'status': status,
            'label': label,
            'equipment_state_code': equipment_state_code,
            'icon': equipment_icon_key(truck, status),
            'percent': truck_plan['css_percent'],
            'plan_visual': truck_plan['visual'],
            'plan': truck_plan,
            'plan_status': truck_plan['status'],
            'plan_status_label': truck_plan['status_label'],
            'plan_group_name': truck_plan['group_name'],
            'plan_calculation_mode': truck_plan['calculation_mode'],
            'plan_value': truck_plan['value_display'],
            'plan_fact_value': truck_plan['fact_display'],
            'plan_fact_label': truck_plan['fact_plan_label'],
            'plan_percent_label': truck_plan['percent_label'],
            'plan_unit': truck_plan['unit'],
            'plan_has_plan': truck_plan['has_plan'],
            'card_id': str(truck.id),
        })
    mobile_truck_garage_tiles = []
    mobile_truck_sort_source = sorted(trucks_list, key=garage_number_int)
    for index, truck in enumerate(mobile_truck_sort_source, start=1):
        if len(mobile_truck_garage_tiles) >= 52:
            break
        if truck.id in assigned_truck_ids:
            continue
        truck_number = dispatcher_truck_garage_number(truck, index)
        if truck_number is None:
            continue
        status, label, equipment_state_code = truck_current_state(truck)
        truck_plan = dispatcher_plan_for_equipment(truck)
        mobile_truck_garage_tiles.append({
            'equipment': truck,
            'name': truck_number,
            'status': status,
            'label': label,
            'equipment_state_code': equipment_state_code,
            'icon': equipment_icon_key(truck, status),
            'percent': truck_plan['css_percent'],
            'plan_visual': truck_plan['visual'],
            'plan': truck_plan,
            'plan_status': truck_plan['status'],
            'plan_status_label': truck_plan['status_label'],
            'plan_group_name': truck_plan['group_name'],
            'plan_calculation_mode': truck_plan['calculation_mode'],
            'plan_value': truck_plan['value_display'],
            'plan_fact_value': truck_plan['fact_display'],
            'plan_fact_label': truck_plan['fact_plan_label'],
            'plan_percent_label': truck_plan['percent_label'],
            'plan_unit': truck_plan['unit'],
            'plan_has_plan': truck_plan['has_plan'],
            'card_id': str(truck.id),
        })

    for tile in excavator_tiles:
        equipment = tile.get('equipment')
        if not equipment or not tile.get('card_id'):
            continue
        downtime = downtime_by_equipment_id.get(equipment.id)
        active_trip = active_trip_by_excavator_id.get(equipment.id)
        latest_trip = latest_trip_by_equipment_id.get(equipment.id)
        details = shift_details(equipment)
        details.extend([
            {'label': 'Комплекс', 'value': tile.get('complex')},
        ])
        details.extend(dispatcher_plan_details(tile.get('plan')))
        if active_trip:
            details.extend([
                {'label': 'Рейс', 'value': 'активный'},
                {'label': 'Самосвал рейса', 'value': equipment_short_name(active_trip.truck)},
                {'label': 'Разгрузка', 'value': active_trip.dump_point},
                {'label': 'Порода', 'value': active_trip.rock_type},
            ])
        if latest_trip:
            details.append({'label': 'Последний рейс', 'value': format_dispatcher_datetime(latest_trip.completed_at)})
        if downtime:
            details.extend([
                {'label': 'Простой', 'value': downtime.reason},
                {'label': 'С начала', 'value': format_dispatcher_datetime(downtime.started_at)},
            ])
        equipment_cards[str(tile['card_id'])] = build_dispatcher_equipment_card(
            card_id=tile['card_id'],
            equipment=equipment,
            number=tile.get('display_name') or tile.get('name'),
            icon=tile.get('icon'),
            status=tile.get('status'),
            status_label=status_label_for(tile.get('status'), tile.get('label')),
            zone=tile.get('complex') or 'гараж',
            percent=tile.get('percent', 0),
            employee=getattr(open_shift_by_equipment_id.get(equipment.id), 'employee', None),
            details=details,
            shift_report=dispatcher_shift_report_for_equipment(
                equipment,
                equipment_kind='Экскаватор',
                shift_trips=shift_trips,
            ),
            plan=tile.get('plan'),
        )

    for card in complex_cards:
        complex_report = dispatcher_complex_shift_report(card)
        details = [
            {'label': 'Экскаватор', 'value': card.get('excavator_name')},
            {'label': 'Текущий состав', 'value': ', '.join(complex_report.get('current_trucks') or [])},
            {'label': 'Выведены из состава', 'value': ', '.join(complex_report.get('removed_trucks') or [])},
            {'label': 'Порода', 'value': card.get('material')},
            {'label': 'Самосвалы', 'value': f'{card.get("assigned", 0)} / {card.get("need", 0)}'},
            {'label': 'Баланс транспорта', 'value': f'+{card["assigned"] - card["need"]}' if card['assigned'] > card['need'] else str(card['assigned'] - card['need'])},
            {'label': 'Прогноз', 'value': f'{card.get("forecast_tons")} т'},
        ]
        details.extend(dispatcher_plan_details(card.get('plan')))
        if card.get('status_key') == 'yellow':
            details.append({'label': 'Причина', 'value': 'дефицит транспорта / риск выполнения'})
            details.append({'label': 'Действие', 'value': 'добавить самосвалы'})
        elif card.get('status_key') in {'orange', 'red'}:
            details.append({'label': 'Причина', 'value': card.get('status_label') or 'комплекс остановлен'})
            details.append({'label': 'Действие', 'value': 'ремонт, простой или расформирование'})
        elif card.get('status_key') == 'blue':
            details.append({'label': 'Причина', 'value': 'комплекс назначен без активной операции'})
            details.append({'label': 'Действие', 'value': 'контроль запуска работы'})
        else:
            details.append({'label': 'Причина', 'value': 'без отклонений'})
            details.append({'label': 'Действие', 'value': 'контроль нормы'})
        equipment_cards[str(card['card_id'])] = build_dispatcher_equipment_card(
            card_id=card['card_id'],
            type_name='Комплекс',
            number=card.get('id'),
            icon=card.get('excavator_icon'),
            status=card.get('status_key'),
            status_label=card.get('status_label'),
            zone=card.get('material'),
            percent=card.get('percent', 0),
            details=details,
            shift_report=complex_report,
            category='complex',
            plan=card.get('plan'),
        )

    for complex_card in complex_cards:
        for tile in complex_card.get('active_truck_tiles', []):
            card_id = str(tile.get('card_id') or '')
            if not card_id or card_id in equipment_cards:
                continue
            equipment = truck_by_id.get(int(card_id)) if card_id.isdigit() else None
            status_label = status_label_for(tile.get('status'), tile.get('label'))
            details = [
                {'label': 'Гаражный N', 'value': tile.get('name')},
                {'label': 'Комплекс', 'value': complex_card.get('id')},
                {'label': 'Состояние', 'value': tile.get('label')},
                {'label': 'Забой', 'value': complex_card.get('current_face')},
                {'label': 'Порода', 'value': complex_card.get('current_rock')},
                {'label': 'Разгрузки', 'value': ', '.join(point.get('name') for point in complex_card.get('unload_points', []) if point.get('name'))},
            ]
            details.extend(dispatcher_plan_details(tile.get('plan')))
            equipment_cards[card_id] = build_dispatcher_equipment_card(
                card_id=card_id,
                type_name='Самосвал',
                equipment=equipment,
                number=tile.get('name'),
                icon=tile.get('icon'),
                status=tile.get('status'),
                status_label=status_label,
                zone=f'{complex_card.get("id")} / в составе',
                percent=tile.get('percent', 0),
                details=details,
                shift_report=dispatcher_shift_report_for_equipment(
                    equipment,
                    equipment_kind='Самосвал',
                    shift_trips=shift_trips,
                ),
                plan=tile.get('plan'),
            )

    for tile in truck_garage_tiles + [
        mobile_tile
        for mobile_tile in mobile_truck_garage_tiles
        if mobile_tile.get('card_id') and str(mobile_tile.get('card_id')) not in equipment_cards
    ]:
        equipment = tile.get('equipment')
        status_label = status_label_for(tile.get('status'), tile.get('label'))
        details = dispatcher_plan_details(tile.get('plan'))
        if equipment:
            downtime = downtime_by_equipment_id.get(equipment.id)
            active_trip = active_trip_by_truck_id.get(equipment.id)
            assignment = assignment_by_truck_id.get(equipment.id)
            latest_trip = latest_trip_by_equipment_id.get(equipment.id)
            details = shift_details(equipment) + details
            if assignment:
                details.extend([
                    {'label': 'Назначение', 'value': 'принято' if assignment.status == AssignmentStatus.ACCEPTED else 'ожидает'},
                    {'label': 'Экскаватор', 'value': equipment_short_name(assignment.excavator)},
                    {'label': 'Назначен', 'value': format_dispatcher_datetime(assignment.assigned_at)},
                ])
            if active_trip:
                details.extend([
                    {'label': 'Рейс', 'value': 'активный'},
                    {'label': 'Экскаватор рейса', 'value': equipment_short_name(active_trip.excavator)},
                    {'label': 'Разгрузка', 'value': active_trip.dump_point},
                    {'label': 'Порода', 'value': active_trip.rock_type},
                ])
            if latest_trip:
                details.append({'label': 'Последний рейс', 'value': format_dispatcher_datetime(latest_trip.completed_at)})
            if downtime:
                details.extend([
                    {'label': 'Простой', 'value': downtime.reason},
                    {'label': 'С начала', 'value': format_dispatcher_datetime(downtime.started_at)},
                ])
            card = build_dispatcher_equipment_card(
                card_id=tile['card_id'],
                equipment=equipment,
                number=tile.get('name'),
                icon=tile.get('icon'),
                status=tile.get('status'),
                status_label=status_label,
                zone='гараж',
                percent=tile.get('percent', 0),
                employee=getattr(open_shift_by_equipment_id.get(equipment.id), 'employee', None),
                details=details,
                shift_report=dispatcher_shift_report_for_equipment(
                    equipment,
                    equipment_kind='Самосвал',
                    shift_trips=shift_trips,
                ),
                plan=tile.get('plan'),
            )
        equipment_cards[str(tile['card_id'])] = card

    action_items = []
    pending_complex = next((card for card in complex_cards if card['status_key'] == 'yellow'), None)
    if pending_complex:
        action_items.append({
            'priority': 1,
            'status': 'warning',
            'title': f'{pending_complex["id"]}: есть неподтвержденные назначения',
            'meta': 'Проверить принятие самосвалов водителями',
            'action': 'контроль назначений',
        })
    if open_downtime_list:
        first_downtime = open_downtime_list[0]
        action_items.append({'priority': 2, 'status': 'warning', 'title': f'{equipment_short_name(first_downtime.equipment)} ремонт', 'meta': str(first_downtime.reason), 'action': 'перераспределить транспорт'})
    if open_downtime_list and action_items:
        first_downtime = open_downtime_list[0]
        first_downtime_state = downtime_reason_state_ui(equipment_state_map, first_downtime.reason)
        action_items[-1].update({
            'status': dispatcher_alert_status_for_color_group(first_downtime_state['color_group']),
            'title': f'{equipment_short_name(first_downtime.equipment)} {first_downtime_state["label"]}',
            'action': 'контроль состояния',
        })
    action_items = action_items[:4]

    event_rows = []
    for downtime in open_downtime_list[:4]:
        event_rows.append({
            'time': timezone.localtime(downtime.started_at).strftime('%H:%M'),
            'object': equipment_short_name(downtime.equipment),
            'text': str(downtime.reason),
            'status': dispatcher_alert_status_for_downtime(downtime),
        })
    for action in list(recent_dispatcher_actions)[:5]:
        event_rows.append({
            'time': timezone.localtime(action.created_at).strftime('%H:%M'),
            'object': action.get_action_type_display()[:8],
            'text': action.target_summary,
            'status': 'warning',
        })
    ore_tons = Decimal('0')
    overburden_tons = Decimal('0')
    for trip in shift_trips:
        rock_name = str(trip.rock_type or '').lower()
        amount = dispatcher_trip_amount(trip)
        if 'вскрыш' in rock_name:
            overburden_tons += amount
        else:
            ore_tons += amount

    return {
        'dispatcher_kpis': {
            'plan_tons': format_dispatcher_number(DISPATCHER_PLAN_TOTAL_TONS),
            'fact_tons': format_dispatcher_number(display_fact_tons),
            'forecast_tons': format_dispatcher_number(forecast_tons),
            'deficit_tons': format_dispatcher_number(abs(deficit_tons)),
            'deficit_is_negative': deficit_tons < 0,
            'completion_percent': completion_percent,
            'ore_tons': format_dispatcher_number(ore_tons),
            'overburden_tons': format_dispatcher_number(overburden_tons),
            'excavators_working': sum(1 for tile in excavator_tiles if tile['status'] == 'green'),
            'excavators_total': len(excavator_tiles),
            'trucks_working': working_trucks,
            'trucks_total': total_trucks,
            'alerts': len([event for event in event_rows if event['status'] in {'danger', 'warning'}]),
        },
        'excavator_tiles': excavator_tiles,
        'excavator_garage_tiles': excavator_garage_tiles,
        'mobile_excavator_garage_tiles': mobile_excavator_garage_tiles,
        'mobile_excavator_garage_row_count': mobile_excavator_garage_row_count,
        'complex_cards': complex_cards,
        'complex_zones': complex_zones[:12],
        'mobile_complex_zones': mobile_complex_zones,
        'truck_garage_tiles': truck_garage_tiles,
        'mobile_truck_garage_tiles': mobile_truck_garage_tiles,
        'equipment_cards': equipment_cards,
        'equipment_state_ui': {
            code: {
                'code': state['code'],
                'label': state['label'],
                'color_group': state['color_group'],
                'allows_assignment': state['allows_assignment'],
                'allows_drag': state['allows_drag'],
                'blocks_operation': state['blocks_operation'],
            }
            for code, state in equipment_state_map.items()
        },
        'truck_balance': {
            'total': total_trucks,
            'working': working_trucks,
            'waiting': waiting_trucks,
            'loading': loading_trucks,
            'repair': repair_trucks,
            'rows': balance_rows,
        },
        'action_items': action_items,
        'event_rows': event_rows[:7],
        'loss_reasons': [
            {'label': str(downtime.reason), 'value': 1, 'status': dispatcher_alert_status_for_downtime(downtime)}
            for downtime in open_downtime_list
        ],
        'forecast_points': [],
        'current_time': timezone.localtime().strftime('%H:%M'),
        'current_date': timezone.localdate().strftime('%d.%m.%Y'),
    }


def log_dispatcher_action(*, actor, action_type, target_summary, trip=None, shift=None, haul_assignment=None, reason=''):
    DispatcherActionLog.objects.create(
        actor=actor,
        action_type=action_type,
        trip=trip,
        shift=shift,
        haul_assignment=haul_assignment,
        target_summary=target_summary,
        reason=str(reason or '').strip(),
    )


def get_dispatcher_control_url(request):
    query_parts = []
    for key in DISPATCHER_FILTER_KEYS:
        value = request.POST.get(key, '').strip()
        if value == '':
            value = request.GET.get(key, '').strip()
        if value != '':
            query_parts.append(f'{key}={value}')
    base_url = reverse('dispatcher_control')
    if not query_parts:
        return base_url
    return f"{base_url}?{'&'.join(query_parts)}"


def dispatcher_access_from_request(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return None
    return (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(id=access_id, is_active=True, role__code__in={'dispatcher', 'admin', 'manager'})
        .first()
    )


def dispatcher_shift_required_response(access):
    if get_active_dispatcher_shift(access):
        return None
    return JsonResponse(
        {'ok': False, 'error': 'Смена горного диспетчера закрыта. Изменения на пульте недоступны.'},
        status=409,
    )


def dispatcher_shift_required_redirect(request, access, redirect_url):
    if get_active_dispatcher_shift(access):
        return None
    messages.error(request, 'Смена горного диспетчера закрыта. Изменения на пульте недоступны.')
    return redirect(redirect_url)


def dispatcher_json_payload(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return {}


def close_haul_assignments(queryset, now, *, action='bulk_close_assignments', source='dispatcher'):
    assignments = list(queryset)
    for assignment in assignments:
        assignment.status = AssignmentStatus.CANCELLED
        assignment.ended_at = now
    if assignments:
        HaulAssignment.objects.bulk_update(assignments, ['status', 'ended_at'])
        bump_operational_state(
            'HaulAssignment:bulk_close',
            event_type='assignment_changed',
            object_type='HaulAssignment',
            payload={
                'action': action,
                'source': source,
                'closed_count': len(assignments),
                'excavator_ids': sorted({assignment.excavator_id for assignment in assignments}),
                'truck_ids': sorted({assignment.truck_id for assignment in assignments}),
            },
        )
    return assignments


@require_POST
def dispatcher_move_excavator_view(request):
    access = dispatcher_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к диспетчерскому пульту.'}, status=403)
    shift_error = dispatcher_shift_required_response(access)
    if shift_error:
        return shift_error
    payload = dispatcher_json_payload(request)
    excavator = get_object_or_404(
        Equipment.objects.select_related('equipment_type'),
        id=payload.get('excavator_id'),
        equipment_type__name__icontains='Экскаватор',
        is_active=True,
    )
    zone = payload.get('zone')
    if zone not in {ExcavatorPlacement.Zone.ACTIVE, ExcavatorPlacement.Zone.INACTIVE}:
        return JsonResponse({'ok': False, 'error': 'Некорректная зона экскаватора.'}, status=400)

    placement, _ = ExcavatorPlacement.objects.get_or_create(excavator=excavator)
    placement.zone = zone
    placement.changed_by = access.employee
    placement.save(update_fields=['zone', 'changed_by', 'changed_at'])

    if zone == ExcavatorPlacement.Zone.INACTIVE:
        now = timezone.now()
        closed = close_haul_assignments(
            HaulAssignment.objects
            .filter(excavator=excavator, ended_at__isnull=True)
            .exclude(status=AssignmentStatus.CANCELLED),
            now,
            action='move_excavator_inactive',
        )
        summary = f'{equipment_short_name(excavator)} возвращен в гараж, комплекс расформирован ({len(closed)} самосв.)'
    else:
        summary = f'{equipment_short_name(excavator)} переведен в активную смену'

    log_dispatcher_action(
        actor=access.employee,
        action_type=DispatcherActionType.CANCEL_ASSIGNMENT,
        target_summary=summary,
    )
    return JsonResponse({'ok': True})


@require_POST
def dispatcher_assign_truck_view(request):
    access = dispatcher_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к диспетчерскому пульту.'}, status=403)
    shift_error = dispatcher_shift_required_response(access)
    if shift_error:
        return shift_error
    payload = dispatcher_json_payload(request)
    action = payload.get('action')
    now = timezone.now()

    if action == 'release_complex':
        excavator = get_object_or_404(
            Equipment.objects.select_related('equipment_type'),
            id=payload.get('excavator_id'),
            equipment_type__name__icontains='Экскаватор',
            is_active=True,
        )
        closed = close_haul_assignments(
            HaulAssignment.objects
            .filter(excavator=excavator, ended_at__isnull=True)
            .exclude(status=AssignmentStatus.CANCELLED),
            now,
            action='release_complex',
        )
        log_dispatcher_action(
            actor=access.employee,
            action_type=DispatcherActionType.CANCEL_ASSIGNMENT,
            target_summary=f'{equipment_short_name(excavator)}: самосвалы сброшены в гараж ({len(closed)})',
        )
        return JsonResponse({'ok': True, 'closed': len(closed)})

    truck = get_object_or_404(
        Equipment.objects.select_related('equipment_type'),
        id=payload.get('truck_id'),
        equipment_type__name__icontains='Самосвал',
        is_active=True,
    )
    active_assignments = (
        HaulAssignment.objects
        .filter(truck=truck, ended_at__isnull=True)
        .exclude(status=AssignmentStatus.CANCELLED)
    )
    if action == 'release':
        closed = close_haul_assignments(active_assignments, now, action='release_truck')
        log_dispatcher_action(
            actor=access.employee,
            action_type=DispatcherActionType.CANCEL_ASSIGNMENT,
            target_summary=f'{equipment_short_name(truck)} снят с комплекса и возвращен в гараж',
        )
        return JsonResponse({'ok': True, 'closed': len(closed)})

    if action != 'assign':
        return JsonResponse({'ok': False, 'error': 'Некорректное действие с самосвалом.'}, status=400)

    excavator = get_object_or_404(
        Equipment.objects.select_related('equipment_type'),
        id=payload.get('excavator_id'),
        equipment_type__name__icontains='Экскаватор',
        is_active=True,
    )
    placement, _ = ExcavatorPlacement.objects.get_or_create(excavator=excavator)
    if placement.zone != ExcavatorPlacement.Zone.ACTIVE:
        placement.zone = ExcavatorPlacement.Zone.ACTIVE
        placement.changed_by = access.employee
        placement.save(update_fields=['zone', 'changed_by', 'changed_at'])

    close_haul_assignments(active_assignments, now, action='assign_truck_reassign')
    assignment = HaulAssignment.objects.create(
        truck=truck,
        excavator=excavator,
        assigned_by=access.employee,
        status=AssignmentStatus.PENDING,
    )
    log_dispatcher_action(
        actor=access.employee,
        action_type=DispatcherActionType.CANCEL_ASSIGNMENT,
        target_summary=f'{equipment_short_name(truck)} назначен под {equipment_short_name(excavator)}',
        haul_assignment=assignment,
    )
    return JsonResponse({'ok': True, 'assignment_id': assignment.id})


def excavator_access_from_request(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return None
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code != 'excavator_operator':
        return None
    return access


def get_excavator_open_shift(employee):
    return (
        EmployeeShift.objects
        .filter(employee=employee, closed_at__isnull=True)
        .select_related('equipment', 'equipment__equipment_type')
        .order_by('-opened_at')
        .first()
    )


def restrict_excavator_trip_form(form, current_excavator):
    if current_excavator:
        form.fields['assignment'].queryset = form.fields['assignment'].queryset.filter(excavator=current_excavator)
    else:
        form.fields['assignment'].queryset = form.fields['assignment'].queryset.none()
    return form


EXCAVATOR_TRUCK_LOAD_BLOCK_LABELS = {
    'missing_truck': 'Самосвал не назначен.',
    'wrong_excavator': 'Самосвал назначен другому экскаватору.',
    'inactive_truck': 'Самосвал неактивен.',
    'active_trip': 'Самосвал уже находится в незакрытом рейсе.',
    'active_downtime': 'Самосвал находится в активном простое.',
    'no_driver': 'Водитель не назначен',
    'driver_shift_not_started': 'Смена водителя не начата',
}


def excavator_truck_load_block_payload(code):
    return {
        'code': code,
        'label': EXCAVATOR_TRUCK_LOAD_BLOCK_LABELS.get(code, 'Самосвал недоступен для погрузки.'),
    }


def excavator_truck_has_driver_assignment(truck):
    if not truck:
        return False
    return (
        EquipmentAssignment.objects
        .filter(
            equipment=truck,
            ended_at__isnull=True,
            status__in=(AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED),
            employee__accesses__role__code='driver',
            employee__accesses__is_active=True,
        )
        .exists()
    )


def excavator_truck_load_block(
    assignment,
    *,
    current_excavator=None,
    active_trip=None,
    active_downtime=None,
    has_open_truck_shift=None,
    has_driver_assignment=None,
):
    if not assignment or not assignment.truck_id:
        return excavator_truck_load_block_payload('missing_truck')
    if current_excavator and assignment.excavator_id != current_excavator.id:
        return excavator_truck_load_block_payload('wrong_excavator')
    truck = assignment.truck
    if not getattr(truck, 'is_active', True):
        return excavator_truck_load_block_payload('inactive_truck')
    if active_trip is None:
        active_trip = (
            Trip.objects
            .filter(truck=truck, status__in=OPEN_TRIP_STATUSES)
            .order_by('-created_at')
            .first()
        )
    if active_trip:
        return excavator_truck_load_block_payload('active_trip')
    if active_downtime is None:
        active_downtime = (
            DowntimeEvent.objects
            .filter(equipment=truck, ended_at__isnull=True)
            .order_by('-started_at', '-id')
            .first()
        )
    if active_downtime:
        return excavator_truck_load_block_payload('active_downtime')
    if has_open_truck_shift is None:
        has_open_truck_shift = EmployeeShift.objects.filter(
            equipment=truck,
            closed_at__isnull=True,
        ).exists()
    if not has_open_truck_shift:
        if has_driver_assignment is None:
            has_driver_assignment = excavator_truck_has_driver_assignment(truck)
        if has_driver_assignment:
            return excavator_truck_load_block_payload('driver_shift_not_started')
        return excavator_truck_load_block_payload('no_driver')
    return None


def excavator_truck_load_block_reason(
    assignment,
    *,
    current_excavator=None,
    active_trip=None,
    active_downtime=None,
    has_open_truck_shift=None,
    has_driver_assignment=None,
):
    block = excavator_truck_load_block(
        assignment,
        current_excavator=current_excavator,
        active_trip=active_trip,
        active_downtime=active_downtime,
        has_open_truck_shift=has_open_truck_shift,
        has_driver_assignment=has_driver_assignment,
    )
    return block['label'] if block else ''


EXCAVATOR_WORK_SETTINGS_SESSION_KEY = 'excavator_work_settings'


def excavator_work_settings_key(current_excavator):
    return str(current_excavator.id) if current_excavator else 'none'


def get_excavator_work_placement(current_excavator):
    if not current_excavator:
        return None
    return (
        ExcavatorPlacement.objects
        .select_related('work_rock_type', 'work_dump_point')
        .filter(excavator=current_excavator)
        .first()
    )


def save_excavator_work_context(*, current_excavator, actor, rock_type, dump_points, loading_horizon, loading_block):
    if not current_excavator:
        return None
    placement, _ = ExcavatorPlacement.objects.get_or_create(excavator=current_excavator)
    placement.work_rock_type = rock_type
    placement.work_dump_point = dump_points[0] if dump_points else None
    placement.loading_horizon = loading_horizon
    placement.loading_block = loading_block
    placement.work_context_updated_at = timezone.now()
    placement.changed_by = actor
    placement.save(update_fields=[
        'work_rock_type',
        'work_dump_point',
        'loading_horizon',
        'loading_block',
        'work_context_updated_at',
        'changed_by',
        'changed_at',
    ])
    return placement


def normalize_excavator_numeric_setting(value, *, max_length=16):
    return re.sub(r'\D+', '', str(value or ''))[:max_length]


def excavator_work_settings_from_session(request, current_excavator, form):
    session_settings = request.session.get(EXCAVATOR_WORK_SETTINGS_SESSION_KEY, {})
    raw_settings = session_settings.get(excavator_work_settings_key(current_excavator), {})
    placement = get_excavator_work_placement(current_excavator)
    rock_choices = list(form.fields['rock_type'].queryset)
    dump_point_choices = list(form.fields['dump_point'].queryset)

    rock_by_id = {str(rock.id): rock for rock in rock_choices}
    dump_by_id = {str(point.id): point for point in dump_point_choices}

    default_rock_id = str(form['rock_type'].value() or '')
    placement_rock_id = str(getattr(placement, 'work_rock_type_id', '') or '')
    rock_id = str(raw_settings.get('rock_type_id') or placement_rock_id or default_rock_id or (rock_choices[0].id if rock_choices else ''))
    current_rock = rock_by_id.get(rock_id) or (rock_choices[0] if rock_choices else None)

    raw_dump_ids = raw_settings.get('dump_point_ids')
    if not isinstance(raw_dump_ids, list):
        raw_dump_ids = []
    if not raw_dump_ids and getattr(placement, 'work_dump_point_id', None):
        raw_dump_ids = [placement.work_dump_point_id]
    selected_dump_points = []
    seen_dump_ids = set()
    for raw_id in raw_dump_ids:
        dump_id = str(raw_id)
        if dump_id in dump_by_id and dump_id not in seen_dump_ids:
            selected_dump_points.append(dump_by_id[dump_id])
            seen_dump_ids.add(dump_id)

    form_dump_id = str(form['dump_point'].value() or '')
    if not selected_dump_points and form_dump_id in dump_by_id:
        selected_dump_points.append(dump_by_id[form_dump_id])
    if not selected_dump_points and dump_point_choices:
        selected_dump_points.append(dump_point_choices[0])

    face_horizon = normalize_excavator_numeric_setting(
        raw_settings.get('loading_horizon')
        if 'loading_horizon' in raw_settings
        else (getattr(placement, 'loading_horizon', '') or form['loading_horizon'].value())
    )
    face_block = normalize_excavator_numeric_setting(
        raw_settings.get('loading_block')
        if 'loading_block' in raw_settings
        else (getattr(placement, 'loading_block', '') or form['loading_block'].value())
    )

    selected_dump_ids = [point.id for point in selected_dump_points]
    return {
        'rock_choices': rock_choices,
        'dump_point_choices': dump_point_choices,
        'current_rock': current_rock,
        'default_rock': current_rock.id if current_rock else '',
        'selected_dump_points': selected_dump_points,
        'selected_dump_point_ids': selected_dump_ids,
        'default_dump_point': selected_dump_ids[0] if selected_dump_ids else '',
        'face_horizon': face_horizon,
        'face_block': face_block,
    }


def build_excavator_dump_cards(points, *, selected_ids=None, include_all=False):
    selected_ids = {str(point_id) for point_id in (selected_ids or [])}
    cards = []
    for index, point in enumerate(points):
        is_selected = str(point.id) in selected_ids if include_all else True
        if include_all and not is_selected:
            status_key = 'gray'
        elif index == 0:
            status_key = 'yellow'
        else:
            status_key = 'green'
        cards.append({
            'point': point,
            'name': str(point),
            'status_key': status_key,
            'is_default': index == 0 and is_selected,
            'is_selected': is_selected,
        })
    return cards


def excavator_json_payload(request):
    if request.content_type == 'application/json':
        try:
            return json.loads(request.body.decode('utf-8') or '{}')
        except json.JSONDecodeError:
            return {}
    return request.POST


def calculate_trip_volume_and_tonnage(truck, rock_type):
    volume = None
    if truck and truck.model:
        rule = TruckCapacityRule.objects.filter(equipment_model=truck.model, rock_type=rock_type).first()
        if rule:
            volume = rule.volume_m3
        elif truck.model.body_volume_m3:
            volume = truck.model.body_volume_m3
    if not volume or not rock_type or not rock_type.density:
        return volume, None
    return volume, (Decimal(volume) * Decimal(rock_type.density)).quantize(Decimal('0.01'))


def finalize_trip_unloaded(trip, *, driver, unloading_shift):
    if trip.volume_m3 is None or trip.tonnage is None:
        volume, tonnage = calculate_trip_volume_and_tonnage(trip.truck, trip.rock_type)
        trip.volume_m3 = trip.volume_m3 if trip.volume_m3 is not None else volume
        trip.tonnage = trip.tonnage if trip.tonnage is not None else tonnage
    trip.status = TripStatus.COMPLETED
    trip.driver = driver
    trip.completed_at = timezone.now()
    trip.unloading_shift = unloading_shift
    if trip.actual_dump_point_id is None:
        trip.actual_dump_point = trip.dump_point
    if trip.assigned_dump_point_id is None:
        trip.assigned_dump_point = trip.dump_point
    trip.is_carryover = bool(
        trip.loading_shift
        and unloading_shift
        and trip.loading_shift.shift_type != unloading_shift.shift_type
    )
    trip.save(update_fields=[
        'volume_m3',
        'tonnage',
        'status',
        'driver',
        'completed_at',
        'unloading_shift',
        'assigned_dump_point',
        'actual_dump_point',
        'is_carryover',
    ])


def trip_loaded_payload(trip, *, client_action_id=''):
    state_ui = equipment_state_ui(get_equipment_state_ui_map(), 'loaded_waiting_unload')
    return {
        'ok': True,
        'action': 'truck_loaded',
        'client_action_id': client_action_id,
        'trip_id': trip.id,
        'truck_id': trip.truck_id,
        'excavator_id': trip.excavator_id,
        'dump_point_id': trip.dump_point_id,
        'dump_point': str(trip.dump_point),
        'assigned_dump_point_id': trip.assigned_dump_point_id or trip.dump_point_id,
        'actual_dump_point_id': trip.actual_dump_point_id or trip.dump_point_id,
        'status': TripStatus.LOADED_WAITING_UNLOAD,
        'status_label': state_ui['label'],
        'version': get_operational_state_version(),
    }


@require_POST
def excavator_truck_loaded_view(request):
    access = excavator_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к экрану Экскаваторщика.'}, status=403)
    open_shift = get_excavator_open_shift(access.employee)
    current_excavator = open_shift.equipment if open_shift else None
    if not current_excavator:
        return JsonResponse({'ok': False, 'error': 'Сначала нужно открыть смену на экскаваторе.'}, status=409)

    payload = excavator_json_payload(request)
    client_action_id = str(payload.get('client_action_id') or '').strip()
    if not client_action_id:
        return JsonResponse({'ok': False, 'error': 'Не передан client_action_id.'}, status=400)

    with transaction.atomic():
        existing_action = (
            TripClientAction.objects
            .select_related('trip', 'trip__dump_point')
            .filter(action_type='truck_loaded', client_action_id=client_action_id)
            .first()
        )
        if existing_action:
            response_payload = trip_loaded_payload(existing_action.trip, client_action_id=client_action_id)
            response_payload['deduplicated'] = True
            return JsonResponse(response_payload)

        try:
            truck_id = int(payload.get('truck_id') or 0)
            excavator_id = int(payload.get('excavator_id') or current_excavator.id)
            dump_point_id = int(payload.get('dump_point_id') or 0)
            rock_type_id = int(payload.get('rock_type') or payload.get('rock_type_id') or 0)
        except (TypeError, ValueError):
            return JsonResponse({'ok': False, 'error': 'Некорректные параметры действия.'}, status=400)

        if excavator_id != current_excavator.id:
            return JsonResponse({'ok': False, 'error': 'Экскаватор в действии не совпадает с текущей сменой.'}, status=409)

        assignment = (
            HaulAssignment.objects
            .select_for_update(of=('self',))
            .select_related('truck', 'truck__model', 'excavator')
            .filter(
                truck_id=truck_id,
                excavator=current_excavator,
                ended_at__isnull=True,
                status__in={AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED},
            )
            .first()
        )
        if not assignment:
            return JsonResponse({'ok': False, 'error': 'Самосвал не назначен текущему экскаватору.'}, status=409)

        open_trip = (
            Trip.objects
            .select_for_update()
            .filter(truck=assignment.truck, status__in=OPEN_TRIP_STATUSES)
            .first()
        )
        if open_trip:
            return JsonResponse({'ok': False, 'error': 'Самосвал уже находится в незакрытом рейсе.', 'trip_id': open_trip.id}, status=409)

        load_block = excavator_truck_load_block(
            assignment,
            current_excavator=current_excavator,
            active_trip=open_trip or False,
        )
        if load_block:
            return JsonResponse({
                'ok': False,
                'error': load_block['label'],
                'load_block_reason_code': load_block['code'],
                'load_block_reason_label': load_block['label'],
            }, status=409)

        dump_point = get_object_or_404(DumpPoint.objects.filter(is_active=True), id=dump_point_id)
        rock_type = get_object_or_404(RockType.objects.filter(is_active=True), id=rock_type_id)
        if assignment.status != AssignmentStatus.ACCEPTED:
            assignment.status = AssignmentStatus.ACCEPTED
            assignment.accepted_at = timezone.now()
            assignment.save(update_fields=['status', 'accepted_at'])
        loading_horizon = normalize_excavator_numeric_setting(payload.get('loading_horizon'))
        loading_block = normalize_excavator_numeric_setting(payload.get('loading_block'))
        save_excavator_work_context(
            current_excavator=current_excavator,
            actor=access.employee,
            rock_type=rock_type,
            dump_points=[dump_point],
            loading_horizon=loading_horizon,
            loading_block=loading_block,
        )

        trip = Trip.objects.create(
            excavator=current_excavator,
            truck=assignment.truck,
            excavator_operator=access.employee,
            loading_shift=open_shift,
            rock_type=rock_type,
            dump_point=dump_point,
            assigned_dump_point=dump_point,
            actual_dump_point=dump_point,
            planned_volume_m3=payload.get('planned_volume_m3') or None,
            volume_m3=None,
            tonnage=None,
            loading_horizon=loading_horizon[:64],
            loading_block=loading_block[:64],
            transport_distance_km=payload.get('transport_distance_km') or None,
            downtime_text=str(payload.get('downtime_text') or '')[:255],
            note=str(payload.get('note') or '')[:1000],
            status=TripStatus.LOADED_WAITING_UNLOAD,
        )
        TripClientAction.objects.create(
            action_type='truck_loaded',
            client_action_id=client_action_id,
            trip=trip,
            actor=access.employee,
        )
        state = bump_operational_state(
            'Trip:truck_loaded',
            event_type='trip_changed',
            object_type='Trip',
            object_id=trip.id,
            payload={
                'action': 'truck_loaded',
                'trip_id': trip.id,
                'truck_id': trip.truck_id,
                'excavator_id': trip.excavator_id,
                'dump_point_id': trip.dump_point_id,
                'assigned_dump_point_id': trip.assigned_dump_point_id,
                'actual_dump_point_id': trip.actual_dump_point_id,
                'status': TripStatus.LOADED_WAITING_UNLOAD,
            },
        )

    response_payload = trip_loaded_payload(trip, client_action_id=client_action_id)
    response_payload['version'] = state.version
    return JsonResponse(response_payload)


@require_POST
def excavator_truck_loaded_cancel_view(request):
    access = excavator_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'РќРµС‚ РґРѕСЃС‚СѓРїР° Рє СЌРєСЂР°РЅСѓ Р­РєСЃРєР°РІР°С‚РѕСЂС‰РёРєР°.'}, status=403)
    open_shift = get_excavator_open_shift(access.employee)
    current_excavator = open_shift.equipment if open_shift else None
    if not current_excavator:
        return JsonResponse({'ok': False, 'error': 'РЎРЅР°С‡Р°Р»Р° РЅСѓР¶РЅРѕ РѕС‚РєСЂС‹С‚СЊ СЃРјРµРЅСѓ РЅР° СЌРєСЃРєР°РІР°С‚РѕСЂРµ.'}, status=409)

    payload = excavator_json_payload(request)
    client_action_id = str(payload.get('client_action_id') or '').strip()
    if not client_action_id:
        return JsonResponse({'ok': False, 'error': 'РќРµ РїРµСЂРµРґР°РЅ client_action_id.'}, status=400)

    with transaction.atomic():
        existing_action = (
            TripClientAction.objects
            .select_related('trip')
            .filter(action_type='truck_loaded_cancel', client_action_id=client_action_id)
            .first()
        )
        if existing_action:
            state_ui = equipment_state_ui(get_equipment_state_ui_map(), 'assigned')
            return JsonResponse({
                'ok': True,
                'deduplicated': True,
                'trip_id': existing_action.trip_id,
                'truck_id': existing_action.trip.truck_id,
                'status': existing_action.trip.status,
                'equipment_state': 'assigned',
                'status_label': state_ui['label'],
                'status_key': state_ui['color_group'],
                'version': get_operational_state_version(),
            })

        try:
            trip_id = int(payload.get('trip_id') or 0)
            truck_id = int(payload.get('truck_id') or 0)
            dump_point_id = int(payload.get('dump_point_id') or 0)
        except (TypeError, ValueError):
            return JsonResponse({'ok': False, 'error': 'РќРµРєРѕСЂСЂРµРєС‚РЅС‹Рµ РїР°СЂР°РјРµС‚СЂС‹ РґРµР№СЃС‚РІРёСЏ.'}, status=400)

        trip = (
            Trip.objects
            .select_for_update()
            .select_related('truck', 'dump_point', 'excavator')
            .filter(
                id=trip_id,
                truck_id=truck_id,
                excavator=current_excavator,
                status=TripStatus.LOADED_WAITING_UNLOAD,
            )
            .first()
        )
        if not trip:
            return JsonResponse({'ok': False, 'error': 'РќРµР·Р°РєСЂС‹С‚С‹Р№ СЂРµР№СЃ РґР»СЏ РѕС‚РјРµРЅС‹ РЅРµ РЅР°Р№РґРµРЅ.'}, status=409)
        current_dump_point_id = trip.assigned_dump_point_id or trip.actual_dump_point_id or trip.dump_point_id
        if dump_point_id and dump_point_id != current_dump_point_id:
            return JsonResponse({'ok': False, 'error': 'РўРѕС‡РєР° СЂР°Р·РіСЂСѓР·РєРё РІ РґРµР№СЃС‚РІРёРё РЅРµ СЃРѕРІРїР°РґР°РµС‚ СЃ СЂРµР№СЃРѕРј.'}, status=409)

        trip.status = TripStatus.CANCELLED
        trip.save(update_fields=['status'])
        TripClientAction.objects.create(
            action_type='truck_loaded_cancel',
            client_action_id=client_action_id,
            trip=trip,
            actor=access.employee,
        )
        state = bump_operational_state(
            'Trip:truck_loaded_cancel',
            event_type='trip_changed',
            object_type='Trip',
            object_id=trip.id,
            payload={
                'action': 'truck_loaded_cancel',
                'trip_id': trip.id,
                'truck_id': trip.truck_id,
                'excavator_id': trip.excavator_id,
                'dump_point_id': current_dump_point_id,
                'status': TripStatus.CANCELLED,
            },
        )
        state_ui = equipment_state_ui(get_equipment_state_ui_map(), 'assigned')
        return JsonResponse({
            'ok': True,
            'trip_id': trip.id,
            'truck_id': trip.truck_id,
            'dump_point_id': current_dump_point_id,
            'status': TripStatus.CANCELLED,
            'equipment_state': 'assigned',
            'status_label': state_ui['label'],
            'status_key': state_ui['color_group'],
            'version': state.version,
        })


@require_POST
def excavator_work_settings_view(request):
    access = excavator_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к экрану Экскаваторщика.'}, status=403)
    open_shift = get_excavator_open_shift(access.employee)
    current_excavator = open_shift.equipment if open_shift else None
    if not current_excavator:
        return JsonResponse({'ok': False, 'error': 'Сначала нужно открыть смену на экскаваторе.'}, status=409)

    payload = excavator_json_payload(request)
    form = restrict_excavator_trip_form(
        TripCreateForm(excavator_operator=access.employee),
        current_excavator,
    )
    rock_queryset = form.fields['rock_type'].queryset
    dump_point_queryset = form.fields['dump_point'].queryset

    rock_type_id = payload.get('rock_type_id') or payload.get('rock_type')
    rock_type = rock_queryset.filter(id=rock_type_id).first()
    if not rock_type:
        return JsonResponse({'ok': False, 'error': 'Порода недоступна в справочнике.'}, status=400)

    raw_dump_ids = payload.get('dump_point_ids')
    if not isinstance(raw_dump_ids, list):
        raw_dump_ids = [payload.get('dump_point_id') or payload.get('dump_point')]
    dump_points = []
    seen_dump_ids = set()
    dump_by_id = {str(point.id): point for point in dump_point_queryset}
    for raw_id in raw_dump_ids:
        dump_id = str(raw_id or '')
        if dump_id in dump_by_id and dump_id not in seen_dump_ids:
            dump_points.append(dump_by_id[dump_id])
            seen_dump_ids.add(dump_id)
    if not dump_points:
        return JsonResponse({'ok': False, 'error': 'Выберите хотя бы одну точку разгрузки из справочника.'}, status=400)

    loading_horizon = normalize_excavator_numeric_setting(payload.get('loading_horizon'))
    loading_block = normalize_excavator_numeric_setting(payload.get('loading_block'))
    session_settings = request.session.get(EXCAVATOR_WORK_SETTINGS_SESSION_KEY, {})
    setting_key = excavator_work_settings_key(current_excavator)
    session_settings[setting_key] = {
        'client_action_id': str(payload.get('client_action_id') or ''),
        'rock_type_id': rock_type.id,
        'dump_point_ids': [point.id for point in dump_points],
        'loading_horizon': loading_horizon,
        'loading_block': loading_block,
        'updated_at': timezone.now().isoformat(),
    }
    request.session[EXCAVATOR_WORK_SETTINGS_SESSION_KEY] = session_settings
    request.session.modified = True
    save_excavator_work_context(
        current_excavator=current_excavator,
        actor=access.employee,
        rock_type=rock_type,
        dump_points=dump_points,
        loading_horizon=loading_horizon,
        loading_block=loading_block,
    )

    state = bump_operational_state(
        'ExcavatorWorkSettings:update',
        event_type='equipment_changed',
        object_type='Equipment',
        object_id=current_excavator.id,
        payload={
            'action': 'excavator_work_settings',
            'excavator_id': current_excavator.id,
            'rock_type_id': rock_type.id,
            'dump_point_ids': [point.id for point in dump_points],
            'loading_horizon': loading_horizon,
            'loading_block': loading_block,
        },
    )
    return JsonResponse({
        'ok': True,
        'action': 'excavator_work_settings',
        'client_action_id': payload.get('client_action_id') or '',
        'rock_type_id': rock_type.id,
        'rock_type': str(rock_type),
        'dump_point_ids': [point.id for point in dump_points],
        'dump_points': [{'id': point.id, 'name': str(point)} for point in dump_points],
        'loading_horizon': loading_horizon,
        'loading_block': loading_block,
        'version': state.version,
    })


def parse_excavator_shift_decimal(value, field_label):
    raw_value = (
        str(value or '')
        .strip()
        .replace('\u00a0', '')
        .replace(' ', '')
        .replace(',', '.')
    )
    if raw_value == '':
        return None
    try:
        parsed = Decimal(raw_value)
    except (InvalidOperation, ValueError):
        raise ValueError(f'{field_label}: нужно указать число.')
    if parsed < 0:
        raise ValueError(f'{field_label}: значение не может быть меньше нуля.')
    return parsed.quantize(Decimal('0.01'))


def default_excavator_shift_type(now=None):
    now = timezone.localtime(now or timezone.now())
    return 'day' if 7 <= now.hour < 19 else 'night'


def get_excavator_for_shift_start(employee, payload):
    raw_excavator_id = payload.get('excavator_id') or payload.get('equipment_id') or ''
    if raw_excavator_id:
        try:
            excavator_id = int(raw_excavator_id)
        except (TypeError, ValueError):
            return None
        return Equipment.objects.filter(
            id=excavator_id,
            equipment_type__name__icontains='Экскаватор',
            is_active=True,
        ).first()

    last_shift = (
        EmployeeShift.objects
        .filter(employee=employee, equipment__equipment_type__name__icontains='Экскаватор')
        .select_related('equipment', 'equipment__equipment_type')
        .order_by('-opened_at')
        .first()
    )
    if last_shift and last_shift.equipment and last_shift.equipment.is_active:
        return last_shift.equipment

    busy_equipment_ids = EmployeeShift.objects.filter(closed_at__isnull=True, equipment_id__isnull=False).values('equipment_id')
    return (
        Equipment.objects
        .filter(equipment_type__name__icontains='Экскаватор', is_active=True)
        .exclude(id__in=busy_equipment_ids)
        .order_by('garage_number')
        .first()
    )


@require_POST
def excavator_shift_action_view(request):
    access = excavator_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к экрану Экскаваторщика.'}, status=403)

    payload = excavator_json_payload(request)
    client_action_id = str(payload.get('client_action_id') or '').strip()
    if not client_action_id:
        return JsonResponse({'ok': False, 'error': 'Не передан client_action_id.'}, status=400)

    action = str(payload.get('action') or payload.get('shift_action') or '').strip()
    open_shift = get_excavator_open_shift(access.employee)
    if action == 'toggle':
        action = 'close' if open_shift else 'open'
    if action not in {'open', 'close'}:
        return JsonResponse({'ok': False, 'error': 'Неизвестное действие смены.'}, status=400)

    try:
        fuel = parse_excavator_shift_decimal(payload.get('fuel'), 'Топливо')
        mileage = parse_excavator_shift_decimal(payload.get('mileage'), 'Пробег')
        engine_hours = parse_excavator_shift_decimal(payload.get('engine_hours'), 'Моточасы')
    except ValueError as error:
        return JsonResponse({'ok': False, 'error': str(error)}, status=400)

    with transaction.atomic():
        if action == 'close':
            open_shift = (
                EmployeeShift.objects
                .select_for_update(of=('self',))
                .filter(employee=access.employee, closed_at__isnull=True)
                .select_related('equipment')
                .order_by('-opened_at')
                .first()
            )
            if not open_shift:
                return JsonResponse({'ok': False, 'error': 'Открытая смена уже закрыта.'}, status=409)
            open_shift.end_fuel = fuel
            open_shift.end_mileage = mileage
            open_shift.end_engine_hours = engine_hours
            open_shift.closed_at = timezone.now()
            open_shift.closed_by = access.employee
            open_shift.save(update_fields=['end_fuel', 'end_mileage', 'end_engine_hours', 'closed_at', 'closed_by'])
            return JsonResponse({
                'ok': True,
                'action': 'shift_closed',
                'client_action_id': client_action_id,
                'shift_id': open_shift.id,
                'shift_open': False,
                'version': get_operational_state_version(),
            })

        if open_shift:
            shift_progress = calculate_open_shift_progress(open_shift)
            return JsonResponse({
                'ok': True,
                'action': 'shift_opened',
                'client_action_id': client_action_id,
                'shift_id': open_shift.id,
                'shift_open': True,
                'deduplicated': True,
                'plan_status': shift_progress.get('plan_status') if shift_progress else '',
                'plan_value': str(shift_progress.get('plan_value') or '') if shift_progress else '',
                'calculation_mode': shift_progress.get('calculation_mode') if shift_progress else '',
                'version': get_operational_state_version(),
            })

        excavator = get_excavator_for_shift_start(access.employee, payload)
        if not excavator:
            return JsonResponse({'ok': False, 'error': 'Не найден свободный экскаватор для начала смены.'}, status=409)
        if EmployeeShift.objects.filter(equipment=excavator, closed_at__isnull=True).exists():
            return JsonResponse({'ok': False, 'error': 'На этом экскаваторе уже открыта смена.'}, status=409)

        shift_type = str(payload.get('shift_type') or default_excavator_shift_type())
        if shift_type not in {'day', 'night'}:
            shift_type = default_excavator_shift_type()
        shift = EmployeeShift.objects.create(
            employee=access.employee,
            equipment=excavator,
            shift_type=shift_type,
            start_fuel=fuel,
            start_mileage=mileage,
            start_engine_hours=engine_hours,
            opened_at=timezone.now(),
            opened_by=access.employee,
        )
        shift_progress = assign_shift_plan_snapshot(shift)
        return JsonResponse({
            'ok': True,
            'action': 'shift_opened',
            'client_action_id': client_action_id,
            'shift_id': shift.id,
            'shift_open': True,
            'equipment_id': excavator.id,
            'plan_status': shift_progress.get('plan_status'),
            'plan_value': str(shift_progress.get('plan_value') or ''),
            'calculation_mode': shift_progress.get('calculation_mode') or '',
            'version': get_operational_state_version(),
        })


def excavator_work_view(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = excavator_access_from_request(request)
    if not access:
        return redirect('role_home')

    open_shift = get_excavator_open_shift(access.employee)
    current_excavator = open_shift.equipment if open_shift else None

    if request.method == 'POST':
        form = restrict_excavator_trip_form(
            TripCreateForm(request.POST, excavator_operator=access.employee),
            current_excavator,
        )
        if form.is_valid():
            block_reason = excavator_truck_load_block_reason(
                form.cleaned_data['assignment'],
                current_excavator=current_excavator,
            )
            if block_reason:
                form.add_error('assignment', block_reason)
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({'ok': False, 'errors': form.errors}, status=400)
                messages.error(request, block_reason)
                return redirect('excavator_work')
            trip = form.create_trip(excavator_operator=access.employee)
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'ok': True,
                    'trip_id': trip.id,
                    'assignment_id': trip.haul_assignment_id if hasattr(trip, 'haul_assignment_id') else form.cleaned_data['assignment'].id,
                    'version': get_operational_state_version(),
                })
            messages.success(request, 'Рейс создан. У водителя появился активный рейс.')
            return redirect('excavator_work')
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'ok': False, 'errors': form.errors}, status=400)
    else:
        form = restrict_excavator_trip_form(
            TripCreateForm(excavator_operator=access.employee),
            current_excavator,
        )

    available_assignments = list(form.fields['assignment'].queryset)
    assignment_truck_ids = [assignment.truck_id for assignment in available_assignments if assignment.truck_id]
    active_trips_queryset = (
        Trip.objects
        .filter(status__in=OPEN_TRIP_STATUSES)
        .select_related('truck', 'excavator', 'rock_type', 'dump_point')
        .order_by('-created_at')
    )
    if current_excavator:
        active_trips_queryset = active_trips_queryset.filter(excavator=current_excavator)
    else:
        active_trips_queryset = active_trips_queryset.filter(excavator_operator=access.employee)
    active_trips = list(active_trips_queryset[:20])
    blocking_trips = []
    if assignment_truck_ids:
        blocking_trips = list(
            Trip.objects
            .filter(truck_id__in=assignment_truck_ids, status__in=OPEN_TRIP_STATUSES)
            .select_related('truck', 'excavator', 'rock_type', 'dump_point')
            .order_by('-created_at')
        )
    active_truck_ids = {trip.truck_id for trip in blocking_trips}
    active_trip_by_truck_id = {trip.truck_id: trip for trip in blocking_trips}

    def equipment_number(equipment):
        return str(getattr(equipment, 'garage_number', '') or equipment or '-')

    def excavator_operator_label(equipment):
        if not equipment:
            return 'ЭКГ-12'
        number = getattr(equipment, 'garage_number', '') or ''
        if number:
            return f'ЭКС-{number}'
        return equipment_short_name(equipment)

    equipment_state_map = get_equipment_state_ui_map()
    truck_downtime_by_equipment_id = {}
    if assignment_truck_ids:
        for downtime in (
            DowntimeEvent.objects
            .filter(equipment_id__in=assignment_truck_ids, ended_at__isnull=True)
            .select_related('reason', 'reason__equipment_state')
            .order_by('-started_at', '-id')
        ):
            truck_downtime_by_equipment_id.setdefault(downtime.equipment_id, downtime)
    open_truck_shift_by_equipment_id = {}
    open_truck_shift_equipment_ids = set()
    if assignment_truck_ids:
        open_truck_shifts = list(
            EmployeeShift.objects
            .filter(equipment_id__in=assignment_truck_ids, closed_at__isnull=True)
            .select_related('employee', 'equipment', 'equipment__equipment_type', 'plan_group')
            .order_by('-opened_at')
        )
        for truck_shift in open_truck_shifts:
            if truck_shift.equipment_id and truck_shift.equipment_id not in open_truck_shift_by_equipment_id:
                open_truck_shift_by_equipment_id[truck_shift.equipment_id] = truck_shift
        open_truck_shift_equipment_ids = set(open_truck_shift_by_equipment_id.keys())

    driver_assignment_truck_ids = set()
    if assignment_truck_ids:
        driver_assignment_truck_ids = set(
            EquipmentAssignment.objects
            .filter(
                equipment_id__in=assignment_truck_ids,
                ended_at__isnull=True,
                status__in=(AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED),
                employee__accesses__role__code='driver',
                employee__accesses__is_active=True,
            )
            .values_list('equipment_id', flat=True)
            .distinct()
        )

    def assignment_load_block(assignment, active_trip=None):
        known_active_trip = active_trip
        if known_active_trip is None:
            known_active_trip = active_trip_by_truck_id.get(assignment.truck_id) or False
        return excavator_truck_load_block(
            assignment,
            current_excavator=current_excavator,
            active_trip=known_active_trip,
            active_downtime=truck_downtime_by_equipment_id.get(assignment.truck_id),
            has_open_truck_shift=assignment.truck_id in open_truck_shift_equipment_ids,
            has_driver_assignment=assignment.truck_id in driver_assignment_truck_ids,
        )

    def assignment_block_reason(assignment, active_trip=None):
        block = assignment_load_block(assignment, active_trip)
        return block['label'] if block else ''

    first_ready_assignment_id = next(
        (assignment.id for assignment in available_assignments if not assignment_block_reason(assignment)),
        None,
    )
    for assignment in available_assignments:
        assignment.has_active_trip = assignment.truck_id in active_truck_ids
        assignment.has_active_downtime = assignment.truck_id in truck_downtime_by_equipment_id
        assignment.has_open_truck_shift = assignment.truck_id in open_truck_shift_equipment_ids

    truck_detail_shift_trips = []
    if assignment_truck_ids:
        truck_shift_ids = [
            truck_shift.id
            for truck_shift in open_truck_shift_by_equipment_id.values()
            if truck_shift.id
        ]
        truck_detail_queryset = (
            Trip.objects
            .filter(truck_id__in=assignment_truck_ids)
            .select_related('truck', 'excavator', 'rock_type', 'dump_point', 'actual_dump_point')
            .order_by('-created_at', '-id')
        )
        if truck_shift_ids:
            truck_detail_queryset = truck_detail_queryset.filter(
                Q(unloading_shift_id__in=truck_shift_ids) |
                Q(status__in=OPEN_TRIP_STATUSES)
            )
        elif open_shift:
            truck_detail_queryset = truck_detail_queryset.filter(
                Q(loading_shift=open_shift) |
                Q(status__in=OPEN_TRIP_STATUSES)
            )
        else:
            truck_detail_queryset = truck_detail_queryset.none()
        truck_detail_shift_trips = list(truck_detail_queryset[:200])
    latest_trip_by_truck_id = {}
    for detail_trip in truck_detail_shift_trips:
        latest_trip_by_truck_id.setdefault(detail_trip.truck_id, detail_trip)

    def excavator_truck_equipment_state_code(assignment, active_trip):
        truck = assignment.truck
        if not getattr(truck, 'is_active', True):
            return 'inactive'
        downtime = truck_downtime_by_equipment_id.get(assignment.truck_id)
        if downtime:
            return downtime_equipment_state_code(downtime)
        if active_trip:
            return 'loaded_waiting_unload'
        if assignment.truck_id not in open_truck_shift_equipment_ids:
            if assignment.truck_id in driver_assignment_truck_ids:
                return 'waiting_for_shift'
            return 'no_driver'
        if assignment.status in {AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED}:
            return 'assigned'
        return 'free'

    truck_cards = []
    for assignment in available_assignments:
        active_trip = active_trip_by_truck_id.get(assignment.truck_id)
        equipment_state_code = excavator_truck_equipment_state_code(assignment, active_trip)
        target_label = str(active_trip.dump_point) if active_trip else ''
        state_ui = equipment_state_ui(equipment_state_map, equipment_state_code)
        load_block = assignment_load_block(assignment, active_trip)
        block_reason = load_block['label'] if load_block else ''
        load_block_reason_code = load_block['code'] if load_block else ''
        soft_driver_block = load_block_reason_code in {'no_driver', 'driver_shift_not_started'}
        state_allows_load = bool(state_ui['allows_drag'] and not state_ui['blocks_operation'])
        can_load = bool(not load_block and state_allows_load)
        is_locked = not can_load
        is_inactive = bool((load_block and not soft_driver_block) or (not load_block and not state_allows_load))
        status_key = state_ui['color_group']
        truck_cards.append({
            'assignment': assignment,
            'number': equipment_number(assignment.truck),
            'equipment_state_code': equipment_state_code,
            'status_key': status_key,
            'status_label': state_ui['label'],
            'target_label': target_label,
            'is_selected': assignment.id == first_ready_assignment_id,
            'is_locked': is_locked,
            'is_inactive': is_inactive,
            'is_load_blocked': bool(load_block and soft_driver_block),
            'can_drag': can_load,
            'can_load': can_load,
            'driver_shift_started': assignment.truck_id in open_truck_shift_equipment_ids,
            'block_reason': block_reason,
            'load_block_reason_code': load_block_reason_code,
            'load_block_reason_label': block_reason,
            'icon': f'img/equipment/truck-{status_key}.png',
        })

    work_settings = excavator_work_settings_from_session(request, current_excavator, form)
    dump_points = work_settings['selected_dump_points']
    dump_cards = build_excavator_dump_cards(dump_points)
    dump_choice_cards = build_excavator_dump_cards(
        work_settings['dump_point_choices'],
        selected_ids=work_settings['selected_dump_point_ids'],
        include_all=True,
    )
    rock_choices = work_settings['rock_choices']
    downtime_equipment_type = current_excavator.equipment_type if current_excavator else None
    downtime_reasons = list(DowntimeReason.for_workplace('excavator_operator', downtime_equipment_type))
    active_downtime = None
    if current_excavator:
        active_downtime = (
            DowntimeEvent.objects
            .filter(equipment=current_excavator, ended_at__isnull=True)
            .select_related('reason', 'reason__equipment_state')
            .order_by('-started_at')
            .first()
        )

    def downtime_reason_card(reason):
        label = reason.button_label
        full_name = str(reason)
        reason_state_code = downtime_reason_equipment_state_code(reason)
        reason_state_ui = downtime_reason_state_ui(equipment_state_map, reason)
        return {
            'reason': reason,
            'name': label,
            'full_name': full_name,
            'equipment_state_code': reason_state_code,
            'status_key': reason_state_ui['color_group'],
            'is_selected': bool(active_downtime and active_downtime.reason_id == reason.id),
        }

    downtime_reason_cards = [downtime_reason_card(reason) for reason in downtime_reasons]

    active_downtime_elapsed_seconds = 0
    active_downtime_elapsed_label = '00:00:00'
    active_downtime_state = equipment_state_ui(equipment_state_map, 'waiting')
    active_downtime_started_at = ''
    if active_downtime and active_downtime.started_at:
        active_downtime_elapsed_seconds = max(0, int((timezone.now() - active_downtime.started_at).total_seconds()))
        active_downtime_elapsed_label = format_duration_label(active_downtime_elapsed_seconds)
        active_downtime_state = downtime_reason_state_ui(equipment_state_map, active_downtime.reason)
        active_downtime_started_at = active_downtime.started_at.isoformat()

    default_rock = work_settings['default_rock']
    default_dump_point = work_settings['default_dump_point']
    face_horizon = work_settings['face_horizon']
    face_block = work_settings['face_block']
    current_rock = work_settings['current_rock']
    selected_dump_point = dump_points[0] if dump_points else None
    shift_progress = calculate_open_shift_progress(open_shift)
    shift_plan = plan_progress_display_context(shift_progress)
    shift_plan_percent = shift_plan['percent']
    shift_plan_visual = progress_cycle_visual_context(shift_plan_percent if shift_plan['has_plan'] else 0)

    for card in truck_cards:
        truck_progress = None
        if open_shift:
            truck_progress = calculate_truck_shift_progress(card['assignment'].truck, reference_shift=open_shift)
        truck_plan = plan_progress_display_context(truck_progress)
        plan_percent = truck_plan['percent']
        card['plan_percent'] = plan_percent
        card['plan_status_key'] = plan_progress_status_key(plan_percent, truck_plan['status'])
        card['plan_status'] = truck_plan['status']
        card['plan_status_label'] = truck_plan['status_label']
        card['plan_short_label'] = truck_plan['short_label']
        card['plan_has_plan'] = truck_plan['has_plan']
        card['plan_value'] = truck_plan['value']
        card['plan_unit'] = truck_plan['unit']
        card['plan_group_name'] = truck_plan['group_name']
        card['plan'] = truck_plan
        card['plan_visual'] = progress_cycle_visual_context(plan_percent if truck_plan['has_plan'] else 0)

    truck_detail_cards = {}

    def excavator_detail_plan_rows(plan):
        if not plan:
            return []
        if not plan.get('has_plan'):
            return [{'label': 'План смены', 'value': plan.get('status_label')}]
        rows = [
            {'label': 'Выполнение плана', 'value': plan.get('percent_label')},
            {'label': 'Факт / план', 'value': plan.get('fact_plan_label')},
        ]
        if plan.get('group_name'):
            rows.append({'label': 'Группа плана', 'value': plan.get('group_name')})
        return rows

    for card in truck_cards:
        assignment = card['assignment']
        truck = assignment.truck
        active_trip = active_trip_by_truck_id.get(assignment.truck_id)
        downtime = truck_downtime_by_equipment_id.get(assignment.truck_id)
        latest_trip = latest_trip_by_truck_id.get(assignment.truck_id)
        truck_shift = open_truck_shift_by_equipment_id.get(assignment.truck_id)
        truck_trips = [trip for trip in truck_detail_shift_trips if trip.truck_id == assignment.truck_id]
        completed_trips = [trip for trip in truck_trips if trip.status == TripStatus.COMPLETED]
        truck_volume = sum((trip.volume_m3 or Decimal('0')) for trip in truck_trips)
        availability_label = card['block_reason'] or (
            'Доступен для погрузки' if card['can_drag'] else f'Недоступен: {card["status_label"]}'
        )
        assignment_label = 'принято' if assignment.status == AssignmentStatus.ACCEPTED else 'ожидает'
        detail_rows = [
            {'label': 'Состояние', 'value': card['status_label']},
            {'label': 'Доступность', 'value': availability_label},
            {'label': 'Назначение', 'value': assignment_label},
            {'label': 'Экскаватор', 'value': equipment_short_name(assignment.excavator)},
            {'label': 'Назначен', 'value': format_dispatcher_datetime(assignment.assigned_at)},
            {'label': 'Рейсы смены', 'value': f'{len(completed_trips)} / {len(truck_trips)}'},
            {'label': 'Объем смены', 'value': format_whole_value_with_unit(truck_volume, 'м³')},
        ]
        detail_rows.extend(excavator_detail_plan_rows(card.get('plan')))
        if active_trip:
            detail_rows.extend([
                {'label': 'Текущий рейс', 'value': 'на разгрузке'},
                {'label': 'Точка разгрузки', 'value': str(active_trip.dump_point or '')},
                {'label': 'Порода', 'value': str(active_trip.rock_type or '')},
            ])
        elif card.get('target_label'):
            detail_rows.append({'label': 'Точка разгрузки', 'value': card.get('target_label')})
        if latest_trip:
            detail_rows.append({
                'label': 'Последнее событие',
                'value': format_dispatcher_datetime(latest_trip.completed_at or latest_trip.created_at),
            })
        if downtime:
            detail_rows.extend([
                {'label': 'Простой', 'value': str(downtime.reason or '')},
                {'label': 'С начала', 'value': format_dispatcher_datetime(downtime.started_at)},
            ])
        card['detail_card_id'] = str(truck.id)
        detail_card = build_dispatcher_equipment_card(
            card_id=truck.id,
            type_name='Самосвал',
            equipment=truck,
            number=card['number'],
            icon=card['icon'],
            status=card['status_key'],
            status_label=card['status_label'],
            zone=card.get('target_label') or equipment_short_name(assignment.excavator),
            percent=card['plan'].get('css_percent', 0),
            employee=getattr(truck_shift, 'employee', None),
            details=detail_rows,
            shift_report=dispatcher_shift_report_for_equipment(
                truck,
                equipment_kind='Самосвал',
                shift_trips=truck_detail_shift_trips,
            ),
            category='truck',
            plan=card['plan'],
        )
        detail_card.update({
            'can_load': card['can_load'],
            'can_drag': card['can_drag'],
            'driver_shift_started': card['driver_shift_started'],
            'equipment_state_code': card['equipment_state_code'],
            'css_class': f'status-{card["status_key"]}',
            'color_group': card['status_key'],
            'load_block_reason_code': card['load_block_reason_code'],
            'load_block_reason_label': card['load_block_reason_label'],
        })
        truck_detail_cards[str(truck.id)] = detail_card

    active_trips_by_dump_id = defaultdict(list)
    for trip in active_trips:
        point_id = trip.assigned_dump_point_id or trip.actual_dump_point_id or trip.dump_point_id
        if point_id:
            active_trips_by_dump_id[point_id].append(trip)

    completed_by_dump_id = defaultdict(int)
    completed_filter = {
        'excavator_operator': access.employee,
        'status': TripStatus.COMPLETED,
        'completed_at__date': timezone.localdate(),
    }
    if current_excavator:
        completed_filter['excavator'] = current_excavator
    completed_queryset = Trip.objects.filter(**completed_filter)
    if face_horizon:
        completed_queryset = completed_queryset.filter(loading_horizon=face_horizon)
    if face_block:
        completed_queryset = completed_queryset.filter(loading_block=face_block)
    if current_rock:
        completed_queryset = completed_queryset.filter(rock_type=current_rock)
    for row in completed_queryset.values('actual_dump_point_id').annotate(total=Count('id')):
        if row['actual_dump_point_id']:
            completed_by_dump_id[row['actual_dump_point_id']] = row['total']

    completed_shift_count = completed_queryset.count()
    completed_shift_volume = completed_queryset.aggregate(total=Sum('volume_m3'))['total'] or Decimal('0')
    shift_fact_label = 'Факт'
    shift_fact_value = format_whole_value_with_unit(completed_shift_volume, 'м³')
    shift_fact_meta = f'{completed_shift_count} маш.'

    for card in dump_cards:
        point_id = card['point'].id
        card['completed_count'] = completed_by_dump_id[point_id]
        card['pending_trucks'] = [
            {
                'truck_id': trip.truck_id,
                'trip_id': trip.id,
                'number': equipment_number(trip.truck),
                'status_key': 'green',
            }
            for trip in active_trips_by_dump_id.get(point_id, [])
        ]

    def form_value_as_text(field_name):
        value = form[field_name].value()
        return '' if value is None else str(value)

    return render(
        request,
        'trips/excavator_work.html',
        {
            'access': access,
            'form': form,
            'open_shift': open_shift,
            'current_excavator': current_excavator,
            'available_assignments': available_assignments,
            'active_trips': active_trips,
            'available_assignments_count': len(available_assignments),
            'active_trips_count': len(active_trips),
            'completed_today_count': completed_shift_count,
            'truck_cards': truck_cards,
            'first_ready_assignment_id': first_ready_assignment_id,
            'truck_detail_cards': truck_detail_cards,
            'dump_cards': dump_cards,
            'dump_choice_cards': dump_choice_cards,
            'rock_choices': rock_choices,
            'default_rock': default_rock,
            'default_dump_point': default_dump_point,
            'selected_dump_point_ids': work_settings['selected_dump_point_ids'],
            'face_horizon': face_horizon,
            'face_block': face_block,
            'current_rock': current_rock,
            'selected_dump_point': selected_dump_point,
            'shift_time_label': '07:00-19:00' if not open_shift or open_shift.shift_type == 'day' else '19:00-07:00',
            'excavator_label': excavator_operator_label(current_excavator),
            'shift_plan_percent': shift_plan_percent,
            'shift_plan_visual': shift_plan_visual,
            'shift_plan_status': shift_plan['status'],
            'shift_plan_status_label': shift_plan['status_label'],
            'shift_plan_short_label': shift_plan['short_label'],
            'shift_plan_has_plan': shift_plan['has_plan'],
            'shift_plan_value': shift_plan['value'],
            'shift_plan_unit': shift_plan['unit'],
            'shift_plan_group_name': shift_plan['group_name'],
            'shift_fact_label': shift_fact_label,
            'shift_fact_value': shift_fact_value,
            'shift_fact_meta': shift_fact_meta,
            'shift_fuel_display': format_whole_number(open_shift.start_fuel) if open_shift else '',
            'shift_mileage_display': format_whole_number(open_shift.start_mileage) if open_shift else '',
            'shift_engine_hours_display': format_whole_number(open_shift.start_engine_hours) if open_shift else '',
            'active_downtime': active_downtime,
            'active_downtime_started_at': active_downtime_started_at,
            'active_downtime_elapsed_seconds': active_downtime_elapsed_seconds,
            'active_downtime_elapsed_label': active_downtime_elapsed_label,
            'active_downtime_state': active_downtime_state,
            'downtime_reason_cards': downtime_reason_cards,
            'operational_state_version': get_operational_state_version(),
            'planned_volume_value': form_value_as_text('planned_volume_m3'),
            'transport_distance_value': form_value_as_text('transport_distance_km'),
            'downtime_text_value': form_value_as_text('downtime_text'),
            'note_value': form_value_as_text('note'),
        },
    )


@require_POST
def excavator_downtime_action_view(request):
    access = excavator_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к экрану Экскаваторщика.'}, status=403)
    open_shift = get_excavator_open_shift(access.employee)
    current_excavator = open_shift.equipment if open_shift else None
    if not current_excavator:
        return JsonResponse({'ok': False, 'error': 'Сначала нужно открыть смену на экскаваторе.'}, status=409)

    payload = excavator_json_payload(request)
    action = (payload.get('action') or '').strip()
    active_event = (
        DowntimeEvent.objects
        .filter(equipment=current_excavator, ended_at__isnull=True)
        .select_related('reason', 'reason__equipment_state')
        .order_by('-started_at')
        .first()
    )

    if action == 'close':
        if not active_event:
            return JsonResponse({
                'ok': True,
                'active': False,
                'closed': False,
                'elapsed_seconds': 0,
                'elapsed_label': '00:00:00',
                'version': get_operational_state_version(),
            })
        active_event.ended_at = timezone.now()
        active_event.save(update_fields=['ended_at'])
        return JsonResponse(downtime_event_payload(active_event, action='downtime_closed', closed=True))

    if action != 'start':
        return JsonResponse({'ok': False, 'error': 'Некорректное действие простоя.'}, status=400)

    reason = (
        DowntimeReason.for_workplace('excavator_operator', current_excavator.equipment_type)
        .filter(id=payload.get('reason_id'))
        .first()
    )
    if not reason:
        return JsonResponse({'ok': False, 'error': 'Причина простоя недоступна для экскаваторщика.'}, status=400)
    if active_event:
        active_event.reason = reason
        active_event.employee = access.employee
        active_event.comment = (payload.get('comment') or '')[:255]
        active_event.save(update_fields=['reason', 'employee', 'comment'])
        event = active_event
    else:
        event = DowntimeEvent.objects.create(
            equipment=current_excavator,
            employee=access.employee,
            reason=reason,
            started_at=timezone.now(),
            comment=(payload.get('comment') or '')[:255],
        )
    action_label = 'downtime_updated' if active_event else 'downtime_started'
    return JsonResponse(downtime_event_payload(event, action=action_label))

def dispatcher_control_view(request, *, access_override=None, enforce_dispatcher_access=True, dispatcher_header_override=None, context_overrides=None):
    if access_override is None:
        access_id = request.session.get('employee_access_id')
        if not access_id:
            return redirect('login')
        access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    else:
        access = access_override
    if not access:
        return redirect('role_home')
    if enforce_dispatcher_access and access.role.code not in {'dispatcher', 'admin', 'manager'}:
        return redirect('role_home')
    dispatcher_header = dispatcher_header_override or build_dispatcher_header_context(access, request)
    dispatcher_shift = dispatcher_header.get('active_shift')

    truck_id = request.GET.get('truck', '').strip()
    excavator_id = request.GET.get('excavator', '').strip()
    show_active_trips = request.GET.get('show_active_trips', '1') == '1'
    show_pending_assignments = request.GET.get('show_pending_assignments', '1') == '1'
    show_accepted_assignments = request.GET.get('show_accepted_assignments', '1') == '1'

    active_trips = (
        Trip.objects
        .filter(status__in=OPEN_TRIP_STATUSES)
        .select_related('truck', 'excavator', 'rock_type', 'dump_point', 'excavator_operator')
        .order_by('created_at')
    )
    if dispatcher_shift:
        active_trips = active_trips.filter(created_at__gte=dispatcher_shift.opened_at)
    else:
        active_trips = active_trips.none()
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
    if dispatcher_shift:
        recent_completed_trips = recent_completed_trips.filter(completed_at__gte=dispatcher_shift.opened_at)
    else:
        recent_completed_trips = recent_completed_trips.none()
    if truck_id:
        recent_completed_trips = recent_completed_trips.filter(truck_id=truck_id)
    if excavator_id:
        recent_completed_trips = recent_completed_trips.filter(excavator_id=excavator_id)

    open_shifts = (
        EmployeeShift.objects
        .filter(closed_at__isnull=True)
        .select_related('employee', 'equipment', 'equipment__equipment_type', 'plan_group', 'opened_by')
        .order_by('opened_at')
    )
    if dispatcher_shift:
        open_shifts = open_shifts.exclude(id=dispatcher_shift.id)
    if truck_id:
        open_shifts = open_shifts.filter(equipment_id=truck_id)
    if excavator_id:
        open_shifts = open_shifts.filter(equipment_id=excavator_id)
    open_shifts = list(open_shifts[:120])

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
    open_mechanic_downtimes = (
        DowntimeEvent.objects
        .filter(ended_at__isnull=True)
        .select_related('equipment', 'reason', 'employee')
        .order_by('started_at')
    )
    downtime_equipment_ids = [equipment_id for equipment_id in [truck_id, excavator_id] if equipment_id]
    if downtime_equipment_ids:
        open_mechanic_downtimes = open_mechanic_downtimes.filter(equipment_id__in=downtime_equipment_ids)
    open_mechanic_downtimes_count = open_mechanic_downtimes.count()
    dispatcher_dashboard = build_dispatcher_dashboard_context(
        dispatcher_shift=dispatcher_shift,
        active_trips=active_trips,
        pending_assignments=pending_assignments,
        accepted_assignments=accepted_assignments,
        recent_completed_trips=recent_completed_trips,
        open_shifts=open_shifts,
        open_mechanic_downtimes=open_mechanic_downtimes[:30],
        trucks=trucks,
        excavators=excavators,
        recent_dispatcher_actions=recent_dispatcher_actions,
    )

    context = {
            'access': access,
            'dispatcher_header': dispatcher_header,
            'dispatcher_dashboard': dispatcher_dashboard,
            'dispatcher_page_title': 'Горный диспетчер',
            'dispatcher_compat_title': 'Диспетчерский пульт',
            'dispatcher_board_label': 'Горный диспетчер',
            'operational_state_version': get_operational_state_version(),
            'dispatcher_move_excavator_url': reverse('dispatcher_move_excavator'),
            'dispatcher_assign_truck_url': reverse('dispatcher_assign_truck'),
            'active_trips': active_trips,
            'pending_assignments': pending_assignments,
            'accepted_assignments': accepted_assignments[:30],
            'recent_completed_trips': recent_completed_trips[:30],
            'open_shifts': open_shifts,
            'open_mechanic_downtimes': open_mechanic_downtimes[:30],
            'active_trips_count': active_trips.count(),
            'pending_assignments_count': pending_assignments.count(),
            'accepted_assignments_count': accepted_assignments.count(),
            'open_shifts_count': len(open_shifts),
            'open_mechanic_downtimes_count': open_mechanic_downtimes_count,
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
            'dispatcher_filter_items': [
                ('truck', truck_id),
                ('excavator', excavator_id),
                ('show_active_trips', '1' if show_active_trips else '0'),
                ('show_pending_assignments', '1' if show_pending_assignments else '0'),
                ('show_accepted_assignments', '1' if show_accepted_assignments else '0'),
            ],
        }
    if context_overrides:
        context.update(context_overrides)

    return render(request, 'trips/dispatcher_control.html', context)


def dispatcher_toggle_shift_view(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')

    redirect_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('dispatcher_control')
    if request.method != 'POST':
        return redirect(redirect_url)

    action = request.POST.get('shift_action')
    if action == 'start':
        has_reauth_credentials = bool(request.POST.get('reauth_phone') and request.POST.get('reauth_access_code'))
        if get_session_device_kind(request) == 'shared' or not has_reauth_credentials:
            reauth_access, reauth_error = authenticate_dispatcher_shared_shift_start(request)
            if reauth_error:
                messages.error(request, reauth_error)
                return redirect(redirect_url)
            access = reauth_access
        if EmployeeShift.objects.filter(employee=access.employee, closed_at__isnull=True).exists():
            messages.warning(request, 'Смена горного диспетчера уже открыта.')
            return redirect(redirect_url)
        open_dispatcher_shift(access)
        messages.success(request, 'Смена горного диспетчера открыта.')
        return redirect(redirect_url)

    if action == 'end':
        shift = close_dispatcher_shift(access)
        if not shift:
            messages.warning(request, 'Открытая смена горного диспетчера не найдена.')
            return redirect(redirect_url)
        messages.success(request, 'Смена горного диспетчера завершена.')
        return redirect(redirect_url)

    messages.error(request, 'Неизвестное действие со сменой диспетчера.')
    return redirect(redirect_url)


def dispatcher_service_close_shift_view(request, shift_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')
    redirect_url = get_dispatcher_control_url(request)

    if request.method != 'POST':
        return redirect(redirect_url)
    shift_error = dispatcher_shift_required_redirect(request, access, redirect_url)
    if shift_error:
        return shift_error
    reason = request.POST.get('reason', '').strip()

    shift = (
        EmployeeShift.objects
        .select_related('employee', 'equipment')
        .filter(id=shift_id, closed_at__isnull=True)
        .first()
    )
    if not shift:
        messages.error(request, 'Открытая смена для служебного закрытия не найдена.')
        return redirect(redirect_url)

    shift.closed_at = timezone.now()
    shift.closed_by = access.employee
    shift.is_service_closed = True
    shift.save(update_fields=['closed_at', 'closed_by', 'is_service_closed'])
    log_dispatcher_action(
        actor=access.employee,
        action_type=DispatcherActionType.SERVICE_CLOSE_SHIFT,
        shift=shift,
        target_summary=f'{shift.employee} / {shift.equipment or "-"} / {shift.get_shift_type_display()}',
        reason=reason,
    )
    messages.success(request, f'Смена сотрудника {shift.employee} закрыта служебно.')
    return redirect(redirect_url)


def dispatcher_cancel_assignment_view(request, assignment_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')
    redirect_url = get_dispatcher_control_url(request)

    if request.method != 'POST':
        return redirect(redirect_url)
    shift_error = dispatcher_shift_required_redirect(request, access, redirect_url)
    if shift_error:
        return shift_error
    reason = request.POST.get('reason', '').strip()

    assignment = (
        HaulAssignment.objects
        .select_related('truck', 'excavator')
        .filter(id=assignment_id, ended_at__isnull=True, status__in={AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED})
        .first()
    )
    if not assignment:
        messages.error(request, 'Активное назначение для отмены не найдено.')
        return redirect(redirect_url)

    assignment.status = AssignmentStatus.CANCELLED
    assignment.ended_at = timezone.now()
    assignment.save(update_fields=['status', 'ended_at'])
    log_dispatcher_action(
        actor=access.employee,
        action_type=DispatcherActionType.CANCEL_ASSIGNMENT,
        haul_assignment=assignment,
        target_summary=f'{assignment.truck} под {assignment.excavator}',
        reason=reason,
    )
    messages.success(request, f'Назначение {assignment.truck} под {assignment.excavator} отменено.')
    return redirect(redirect_url)


def dispatcher_cancel_trip_view(request, trip_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')
    redirect_url = get_dispatcher_control_url(request)

    if request.method != 'POST':
        return redirect(redirect_url)
    shift_error = dispatcher_shift_required_redirect(request, access, redirect_url)
    if shift_error:
        return shift_error
    reason = request.POST.get('reason', '').strip()

    trip = (
        Trip.objects
        .select_related('truck', 'excavator')
        .filter(id=trip_id, status__in=OPEN_TRIP_STATUSES)
        .first()
    )
    if not trip:
        messages.error(request, 'Активный рейс для отмены не найден.')
        return redirect(redirect_url)

    trip.status = TripStatus.CANCELLED
    trip.save(update_fields=['status'])
    log_dispatcher_action(
        actor=access.employee,
        action_type=DispatcherActionType.CANCEL_TRIP,
        trip=trip,
        target_summary=f'{trip.truck} -> {trip.dump_point}',
        reason=reason,
    )
    messages.success(request, f'Рейс {trip.truck} -> {trip.dump_point} отменен.')
    return redirect(redirect_url)


def dispatcher_complete_trip_view(request, trip_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')
    redirect_url = get_dispatcher_control_url(request)

    if request.method != 'POST':
        return redirect(redirect_url)
    shift_error = dispatcher_shift_required_redirect(request, access, redirect_url)
    if shift_error:
        return shift_error
    reason = request.POST.get('reason', '').strip()

    trip = (
        Trip.objects
        .select_related('truck', 'excavator', 'loading_shift')
        .filter(id=trip_id, status__in=OPEN_TRIP_STATUSES)
        .first()
    )
    if not trip:
        messages.error(request, 'Активный рейс для служебного завершения не найден.')
        return redirect(redirect_url)

    unloading_shift = (
        EmployeeShift.objects
        .filter(equipment=trip.truck, closed_at__isnull=True)
        .order_by('-opened_at')
        .first()
    )
    if not unloading_shift:
        messages.error(request, 'Нельзя служебно завершить рейс: не найдена открытая смена по этому самосвалу.')
        return redirect(redirect_url)

    finalize_trip_unloaded(trip, driver=unloading_shift.employee, unloading_shift=unloading_shift)
    log_dispatcher_action(
        actor=access.employee,
        action_type=DispatcherActionType.COMPLETE_TRIP,
        trip=trip,
        target_summary=f'{trip.truck} -> {trip.dump_point}',
        reason=reason,
    )
    messages.success(request, f'Рейс {trip.truck} завершен служебно.')
    return redirect(redirect_url)


def driver_complete_trip_view(request, trip_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code != 'driver':
        return redirect('role_home')
    if not hasattr(access.employee, 'driver_registration'):
        return redirect('driver_registration')
    unloading_shift = EmployeeShift.objects.filter(employee=access.employee, closed_at__isnull=True).order_by('-opened_at').first()
    if not unloading_shift or not unloading_shift.equipment:
        messages.error(request, 'Нельзя завершить рейс: открытая смена с самосвалом не найдена.')
        return redirect('driver_shift')
    if request.method != 'POST':
        return redirect('driver_shift')
    client_action_id = str(request.POST.get('client_action_id') or '').strip()
    if not client_action_id:
        messages.error(request, 'Не передан идентификатор действия. Обновите экран и повторите точковку.')
        return redirect('driver_shift')
    with transaction.atomic():
        existing_action = TripClientAction.objects.filter(
            action_type='trip_unloaded',
            client_action_id=client_action_id,
        ).first()
        if existing_action:
            return redirect('driver_shift')
        trip = (
            Trip.objects
            .select_for_update()
            .filter(id=trip_id, truck=unloading_shift.equipment, status__in=OPEN_TRIP_STATUSES)
            .first()
        )
        if trip:
            finalize_trip_unloaded(trip, driver=access.employee, unloading_shift=unloading_shift)
            TripClientAction.objects.create(
                action_type='trip_unloaded',
                client_action_id=client_action_id,
                trip=trip,
                actor=access.employee,
            )
            bump_operational_state(
                'Trip:trip_unloaded',
                event_type='trip_changed',
                object_type='Trip',
                object_id=trip.id,
                payload={
                    'action': 'trip_unloaded',
                    'trip_id': trip.id,
                    'truck_id': trip.truck_id,
                    'excavator_id': trip.excavator_id,
                    'assigned_dump_point_id': trip.assigned_dump_point_id or trip.dump_point_id,
                    'actual_dump_point_id': trip.actual_dump_point_id or trip.dump_point_id,
                    'status': TripStatus.COMPLETED,
                },
            )
        else:
            messages.error(request, 'Активный рейс не найден или уже закрыт.')
    return redirect('driver_shift')


def driver_change_unload_point_view(request, trip_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code != 'driver':
        return redirect('role_home')
    if not hasattr(access.employee, 'driver_registration'):
        return redirect('driver_registration')
    if request.method != 'POST':
        return redirect('driver_shift')

    unloading_shift = EmployeeShift.objects.filter(employee=access.employee, closed_at__isnull=True).order_by('-opened_at').first()
    if not unloading_shift or not unloading_shift.equipment:
        messages.error(request, 'Нельзя изменить точку: открытая смена с самосвалом не найдена.')
        return redirect('driver_shift')

    client_action_id = str(request.POST.get('client_action_id') or '').strip()
    if not client_action_id:
        messages.error(request, 'Не передан идентификатор действия. Обновите экран и выберите точку снова.')
        return redirect('driver_shift')

    try:
        dump_point_id = int(request.POST.get('dump_point') or 0)
    except (TypeError, ValueError):
        dump_point_id = 0
    dump_point = DumpPoint.objects.filter(id=dump_point_id, is_active=True).first()
    if not dump_point:
        messages.error(request, 'Точка разгрузки не найдена.')
        return redirect('driver_shift')

    with transaction.atomic():
        existing_action = TripClientAction.objects.filter(
            action_type='change_actual_unload_point',
            client_action_id=client_action_id,
        ).first()
        if existing_action:
            return redirect('driver_shift')
        trip = (
            Trip.objects
            .select_for_update()
            .filter(id=trip_id, truck=unloading_shift.equipment, status__in=OPEN_TRIP_STATUSES)
            .first()
        )
        if not trip:
            messages.error(request, 'Активный рейс не найден или уже закрыт.')
            return redirect('driver_shift')
        if trip.assigned_dump_point_id is None:
            trip.assigned_dump_point = trip.dump_point
        trip.actual_dump_point = dump_point
        trip.dump_point = dump_point
        trip.save(update_fields=['assigned_dump_point', 'actual_dump_point', 'dump_point'])
        TripClientAction.objects.create(
            action_type='change_actual_unload_point',
            client_action_id=client_action_id,
            trip=trip,
            actor=access.employee,
        )
        bump_operational_state(
            'Trip:change_actual_unload_point',
            event_type='trip_changed',
            object_type='Trip',
            object_id=trip.id,
            payload={
                'action': 'change_actual_unload_point',
                'trip_id': trip.id,
                'truck_id': trip.truck_id,
                'excavator_id': trip.excavator_id,
                'assigned_dump_point_id': trip.assigned_dump_point_id or trip.dump_point_id,
                'actual_dump_point_id': trip.actual_dump_point_id,
                'status': trip.status,
            },
        )
    return redirect('driver_shift')
