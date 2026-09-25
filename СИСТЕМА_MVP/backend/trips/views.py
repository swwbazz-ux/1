import json
import logging
import math
import re
import secrets
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Prefetch, Q, Sum
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from assignments.models import (
    AssignmentStatus,
    EquipmentAssignment,
    ExcavatorDumpPointSetting,
    ExcavatorPlacement,
    HaulAssignment,
    HaulAssignmentAction,
    HaulAssignmentHandoff,
)
from assignments.services import (
    active_haul_handoffs,
    excavator_load_assignment_queryset,
    get_active_equipment_assignment,
    reconcile_due_haul_assignments,
    resolve_excavator_load_authority,
    work_assignment_state,
)
from core.db_locks import lock_idempotency_key
from core.models import OperationalStateVersion, bump_operational_state, lock_production_state
from core.operational_fragments import operational_fragment_response
from core.production_time import (
    production_shift_bounds,
    production_shift_context,
    production_shift_type,
    production_work_date_for_shift,
)
from downtimes.driver_workflow import (
    DRIVER_DOWNTIME_FLOW_WAITING_LOADING,
    close_truck_unloading_wait_downtimes,
    close_truck_waiting_loading_downtimes,
    driver_downtime_flow,
)
from downtimes.models import DowntimeEvent, DowntimeReason
from references.equipment_states import DEFAULT_EQUIPMENT_STATES
from references.models import DumpPoint, Equipment, EquipmentState, RockType, TruckCapacityRule
from references.rock_catalog import CANONICAL_ROCK_NAMES
from shifts.models import EmployeeShift, ShiftClientAction
from shifts.models import PlanAssignmentStatus, PlanCalculationMode
from shifts.services import (
    ExcavatorShiftCloseConfirmationRequired,
    ExcavatorShiftError,
    aggregate_completed_trip_facts_by_shift,
    calculate_open_shift_progress,
    calculate_progress_from_snapshot_facts,
    calculate_truck_shift_progress,
    close_excavator_shift,
    equipment_is_truck,
    excavator_fuel_capacity_l,
    excavator_fuel_liters_from_percent,
    find_other_role_open_shift,
    format_progress_percent,
    other_role_shift_flag,
    other_role_shift_prompt,
    plan_status_label,
    progress_cycle_visual_context,
    plan_unit_label,
    open_excavator_shift,
    validate_driver_close_readings,
    validate_excavator_shift_readings,
)
from users.models import Employee, EmployeeAccess
from .manual_loading import (
    manual_dump_card_expires_at,
    manual_dump_card_is_visible,
    manual_dump_card_visibility_filter,
    manual_loading_enabled,
    may_replace_open_trip,
    reconcile_expired_manual_trips,
    trip_driver_control_filter,
    truck_driver_participation,
)
from .free_bucket import (
    active_free_bucket_acceptance_filter,
    free_bucket_acceptance_expires_at,
    free_bucket_dump_card_expires_at,
    free_bucket_dump_card_is_visible,
    free_bucket_dump_card_visibility_filter,
    reconcile_expired_free_bucket_acceptances,
)
from users.active_role import role_session_state
from users.role_apps import (
    role_app_manifest_response,
    role_app_service_worker_response,
)
from .excavator_hourly_report import build_excavator_hourly_report
from .dispatcher_header import build_dispatcher_header_context, get_active_dispatcher_shift
from .dispatcher_assignment_commands import (
    execute_dispatcher_cancel_assignment as _execute_dispatcher_cancel_assignment,
)
from .dispatcher_dashboard_projection import (
    dispatcher_complex_label,
    dispatcher_complex_number_int,
    dispatcher_complex_state_code,
    dispatcher_complex_face_label,
    dispatcher_complex_location_parts,
    dispatcher_complex_shift_report as _build_dispatcher_complex_shift_report,
    dispatcher_complex_truck_rows,
    dispatcher_downtime_reason_label,
    dispatcher_employee_for_equipment,
    dispatcher_equipment_card_requested,
    dispatcher_equipment_presence_fields,
    dispatcher_equipment_state_tuple,
    dispatcher_excavator_state_code,
    dispatcher_garage_number_int,
    dispatcher_plan_details,
    dispatcher_shift_details,
    dispatcher_status_label,
    dispatcher_tons_from_label,
    dispatcher_truck_state_code,
)
from .dispatcher_downtime_projection import (
    dispatcher_alert_status_for_color_group,
    dispatcher_alert_status_for_downtime,
    dispatcher_downtime_card_payload,
    dispatcher_downtime_count_label,
    dispatcher_downtime_report_extras,
    dispatcher_downtime_row_meta,
    dispatcher_report_with_downtimes,
    dispatcher_shift_downtime_rows,
    downtime_reason_color_group,
    format_dispatcher_datetime,
    format_dispatcher_downtime_duration,
    format_duration_label,
    normalize_status_color_group,
)
from .dispatcher_downtime_commands import (
    execute_dispatcher_close_downtime as _execute_dispatcher_close_downtime,
)
from .dispatcher_equipment_commands import (
    execute_dispatcher_equipment_settings as _execute_dispatcher_equipment_settings,
)
from .dispatcher_guards import (
    dispatcher_access_from_request,
    dispatcher_client_action_error,
    dispatcher_json_payload,
    dispatcher_shift_required_redirect,
    dispatcher_shift_required_response,
    get_dispatcher_action_redirect_url,
    get_dispatcher_control_url,
    lock_dispatcher_mutation_access as _lock_dispatcher_mutation_access,
)
from .dispatcher_read_model import build_dispatcher_control_read_model
from .dispatcher_shift_commands import (
    SERVICE_CLOSE_AUTO_EXPIRED,
    SERVICE_CLOSE_AUTO_NOTE,
    SERVICE_CLOSE_COORDINATED,
    SERVICE_CLOSE_KIND_LABELS,
    SERVICE_CLOSE_NEGLECTED,
    SERVICE_CLOSE_NEGLECTED_NOTE,
    authenticate_dispatcher_shared_shift_start,
    execute_dispatcher_service_close_shift as _execute_dispatcher_service_close_shift,
    execute_dispatcher_toggle_shift as _execute_dispatcher_toggle_shift,
    finish_service_closed_shift,
    normalize_service_close_kind,
)
from .dispatcher_trip_commands import (
    DISPATCHER_MANUAL_TRIP_MAX_COUNT,
    execute_dispatcher_cancel_trip as _execute_dispatcher_cancel_trip,
    execute_dispatcher_complete_trip as _execute_dispatcher_complete_trip,
    execute_dispatcher_manual_trip as _execute_dispatcher_manual_trip,
    parse_dispatcher_manual_trip_time,
)
from .dispatcher_topology_commands import (
    execute_dispatcher_assign_truck as _execute_dispatcher_assign_truck,
    execute_dispatcher_move_excavator as _execute_dispatcher_move_excavator,
    required_assignment_state_id,
    required_projected_assignment_states,
)
from .forms import TripCreateForm
from .models import DispatcherActionLog, DispatcherActionType, OPEN_TRIP_STATUSES, Trip, TripClientAction, TripStatus
from .trip_creation import (
    calculate_trip_volume_and_tonnage,
    create_loaded_waiting_unload_trip,
    lock_trip_participant_equipment,
)

logger = logging.getLogger(__name__)


TRUCK_POST_UNLOAD_COOLDOWN = timedelta(
    seconds=getattr(settings, 'TRUCK_POST_UNLOAD_COOLDOWN_SECONDS', 600)
)
TRUCK_POST_UNLOAD_ZERO_COOLDOWN_GARAGE_NUMBERS = frozenset({'ТЕСТ-1'})


def truck_waiting_loading_downtime(event):
    reason = getattr(event, 'reason', None)
    return driver_downtime_flow(reason) == DRIVER_DOWNTIME_FLOW_WAITING_LOADING


def truck_post_unload_cooldown(truck, *, completed_at=None, now=None):
    if not truck:
        return None
    garage_number = str(getattr(truck, 'garage_number', '') or '').strip().upper()
    if garage_number in TRUCK_POST_UNLOAD_ZERO_COOLDOWN_GARAGE_NUMBERS:
        return None
    if completed_at is None:
        completed_at = (
            Trip.objects
            .filter(
                truck=truck,
                status=TripStatus.COMPLETED,
                completed_at__isnull=False,
            )
            .order_by('-completed_at')
            .values_list('completed_at', flat=True)
            .first()
        )
    if not completed_at:
        return None
    now = now or timezone.now()
    remaining_seconds = int(
        (completed_at + TRUCK_POST_UNLOAD_COOLDOWN - now).total_seconds()
    )
    if remaining_seconds <= 0:
        return None
    remaining_minutes = max(1, math.ceil(remaining_seconds / 60))
    return {
        'code': 'post_unload_cooldown',
        'label': f'Возвращается к экскаватору · {remaining_minutes} мин.',
        'remaining_seconds': remaining_seconds,
    }


DISPATCHER_PLAN_TOTAL_TONS = Decimal('420000')
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


def equipment_state_icon_color(color_group):
    if color_group == 'orange':
        return 'yellow'
    if color_group in {'green', 'yellow', 'red', 'gray', 'blue'}:
        return color_group
    return 'gray'


def downtime_reason_equipment_state_code(reason):
    if not reason:
        return 'waiting'
    return reason.effective_equipment_state_code


def downtime_reason_state_ui(states, reason):
    state_ui = dict(equipment_state_ui(states, downtime_reason_equipment_state_code(reason)))
    state_ui['color_group'] = downtime_reason_color_group(reason)
    return state_ui


def downtime_equipment_state_code(downtime):
    if not downtime:
        return ''
    return downtime_reason_equipment_state_code(getattr(downtime, 'reason', None))


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
        'date': (
            production_work_date_for_shift(shift.opened_at, shift.shift_type)
            if shift and shift.opened_at
            else None
        ),
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


def equipment_shift_downtime_seconds_by_reason(equipment, shift, *, until=None):
    if not equipment or not shift or not shift.opened_at:
        return {}
    calculation_end = until or timezone.now()
    source_period_end = shift.closed_at or calculation_end
    if source_period_end <= shift.opened_at:
        return {}
    events = (
        DowntimeEvent.objects
        .filter(
            equipment=equipment,
            started_at__gte=shift.opened_at,
            started_at__lt=source_period_end,
        )
    )
    totals = {}
    for event in events.only('reason_id', 'started_at', 'ended_at'):
        event_end = min(event.ended_at or calculation_end, calculation_end)
        elapsed_seconds = max(0, int((event_end - event.started_at).total_seconds()))
        totals[event.reason_id] = totals.get(event.reason_id, 0) + elapsed_seconds
    return totals


def equipment_shift_downtime_seconds(equipment, shift, *, until=None):
    return sum(equipment_shift_downtime_seconds_by_reason(equipment, shift, until=until).values())


def downtime_event_counts_towards_shift(event, shift):
    if not event or not shift or not shift.opened_at or not event.started_at:
        return False
    return event.started_at >= shift.opened_at and (
        not shift.closed_at or event.started_at < shift.closed_at
    )


def excavator_downtime_totals_payload(excavator, shift, *, calculated_at=None):
    calculated_at = calculated_at or timezone.now()
    reason_totals = equipment_shift_downtime_seconds_by_reason(
        excavator,
        shift,
        until=calculated_at,
    )
    shift_total_seconds = sum(reason_totals.values())
    return {
        'calculated_at': calculated_at.isoformat(),
        'shift_total_seconds': shift_total_seconds,
        'shift_total_label': format_duration_label(shift_total_seconds),
        'reason_totals': reason_totals,
    }


def excavator_downtime_status_payload(excavator, shift):
    calculated_at = timezone.now()
    totals_payload = excavator_downtime_totals_payload(
        excavator,
        shift,
        calculated_at=calculated_at,
    )
    active_event = (
        DowntimeEvent.objects
        .filter(equipment=excavator, ended_at__isnull=True)
        .select_related('reason', 'reason__equipment_state')
        .order_by('-started_at')
        .first()
    ) if excavator else None
    if active_event:
        payload = downtime_event_payload(active_event)
        payload['active_counts_towards_shift'] = downtime_event_counts_towards_shift(
            active_event,
            shift,
        )
    else:
        payload = {
            'ok': True,
            'active': False,
            'closed': True,
            'event_id': None,
            'reason_id': None,
            'reason': '',
            'started_at': '',
            'ended_at': '',
            'elapsed_seconds': 0,
            'elapsed_label': '00:00:00',
            'equipment_state_code': '',
            'status_key': 'gray',
            'status_label': '',
            'active_counts_towards_shift': False,
        }
    payload.update(totals_payload)
    return payload


def get_operational_state_version():
    state = (
        OperationalStateVersion.objects
        .filter(key='production')
        .only('version')
        .first()
    )
    return state.version if state else 0

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
            'src': '/static/img/pwa/excavator-192.png',
            'sizes': '192x192',
            'type': 'image/png',
            'purpose': 'any',
        },
        {
            'src': '/static/img/pwa/excavator-512.png',
            'sizes': '512x512',
            'type': 'image/png',
            'purpose': 'any',
        },
        {
            'src': '/static/img/pwa/excavator-maskable-512.png',
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
const APP_CONTRACT_VERSION = "pwa-contract-v1";
const ROLE_CODE = "excavator_operator";
const CACHE_PREFIX = "excavator-mobile-shell-";
const CACHE_NAME = "excavator-mobile-shell-v261";
const APP_SHELL_URL = "/excavator/work/";
const MANIFEST_URL = "/excavator.webmanifest";
const PRIVACY_POLICY_PATH = "/company/privacy/";
const PRIVACY_POLICY_URL = "/company/privacy/?from=role-login";
const CORE_ASSETS = [
  MANIFEST_URL,
  PRIVACY_POLICY_URL,
  "/static/portal/css/portal-shell-v5.css?v=7",
  "/static/portal/js/portal-shell-v5.js",
  "/static/js/realtime-client.js?v=__STATIC_ASSET_RELEASE__",
  "/static/js/role-readonly.js",
  "/static/css/app.css?v=__STATIC_ASSET_RELEASE__",
  "/static/css/excavator-manual-loading-v1.css?v=4",
  "/static/css/excavator-work-v55.css?v=excavator-mobile-shell-v261",
  "/static/css/excavator-work-v55-final.css?v=excavator-mobile-shell-v261",
  "/static/css/excavator-work-v55-shift.css?v=excavator-mobile-shell-v261",
  "/static/css/mobile-shift-unified-v1.css?v=excavator-mobile-shell-v261",
  "/static/css/mobile-face-unified-v1.css?v=excavator-mobile-shell-v261",
  "/static/css/mobile-downtime-unified-v1.css?v=excavator-mobile-shell-v261",
  "/static/css/excavator-hourly-report-v1.css?v=excavator-mobile-shell-v261",
  "/static/css/mobile-role-login-v1.css",
  "/static/js/mobile-shift-unified-v1.js?v=excavator-mobile-shell-v261",
  "/static/js/mobile-operational-sounds-v1.js?v=excavator-mobile-shell-v261",
  "/static/js/excavator-haptics-v1.js?v=excavator-mobile-shell-v261",
  "/static/js/excavator-native-push-v1.js?v=excavator-mobile-shell-v261",
  "/static/js/excavator-hourly-report-v1.js?v=excavator-mobile-shell-v261",
  "/static/js/excavator-field-outbox-v1.js?v=excavator-mobile-shell-v261",
  "/static/js/excavator-free-bucket-v1.js?v=excavator-mobile-shell-v261",
  "/static/js/equipment-label-fit-v1.js?v=excavator-mobile-shell-v261",
  "/static/js/excavator-truck-number-fit-v1.js?v=excavator-mobile-shell-v261",
  "/static/js/excavator-dashboard-drag-v1.js?v=excavator-mobile-shell-v261",
  "/static/js/excavator-dump-return-swipe-v1.js?v=excavator-mobile-shell-v261",
  "/static/css/excavator-free-bucket-v1.css?v=excavator-mobile-shell-v261",
  "/static/css/excavator-offline-v1.css?v=1",
  "/static/css/native-app-update-v1.css",
  "/static/favicon.ico",
  "/static/img/pwa/excavator-180.png",
  "/static/img/pwa/excavator-192.png",
  "/static/img/pwa/excavator-512.png",
  "/static/img/pwa/excavator-maskable-512.png",
  "/static/img/start/start-hero-v1.webp",
  "/static/img/start/start-hero-v1.jpg",
  "/static/img/equipment/excavator-gray.png",
  "/static/img/equipment/excavator-green.png",
  "/static/img/equipment/excavator-yellow.png",
  "/static/img/equipment/excavator-red.png",
  "/static/img/equipment/truck-gray.png",
  "/static/img/equipment/truck-green.png",
  "/static/img/equipment/truck-yellow.png",
  "/static/img/equipment/truck-red.png",
  "/static/audio/excavator/excavator_truck_assigned.wav",
  "/static/audio/excavator/excavator_action_ok.wav",
  "/static/audio/excavator/excavator_action_error.wav",
  "/static/audio/excavator/excavator_connection_lost.wav",
  "/static/audio/excavator/excavator_connection_restored.wav",
  "/static/audio/excavator/excavator_shift_start.wav",
  "/static/audio/excavator/excavator_shift_end.wav",
  "/static/audio/excavator/excavator_assignment_notice.wav",
  "/static/audio/excavator/excavator_action_success_notice.wav",
  "/static/audio/excavator/excavator_assignment_removed_notice.wav",
  "/static/audio/excavator/excavator_shift_notice.wav",
  "/static/audio/excavator/excavator_action_failed_notice.wav",
  "/static/audio/excavator/excavator_connection_lost_notice.wav",
  "/static/audio/excavator/excavator_connection_restored_notice.wav"
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(async cache => {
        await cache.addAll(CORE_ASSETS.map(url => new Request(url, { cache: "reload" })));
        if (await precacheAuthenticatedShell(cache)) return;
        const keys = await caches.keys();
        const previous = keys.filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME);
        if (await migratePreviousExcavatorCache(previous)) return;
        throw new Error("Authenticated excavator shell is unavailable for offline installation.");
      })
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME)
          .map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

async function isExcavatorShellResponse(response) {
  if (!response || !response.ok || !response.url) return false;
  let finalUrl;
  try {
    finalUrl = new URL(response.url, self.location.origin);
  } catch (error) {
    return false;
  }
  if (finalUrl.origin !== self.location.origin || finalUrl.pathname !== APP_SHELL_URL) {
    return false;
  }
  const contentType = String(response.headers.get("Content-Type") || "").toLowerCase();
  if (!contentType.includes("text/html")) return false;
  try {
    const html = await response.clone().text();
    return html.includes("data-eo-shell") &&
      html.includes('data-eo-role-code="' + ROLE_CODE + '"');
  } catch (error) {
    return false;
  }
}

async function precacheAuthenticatedShell(cache) {
  try {
    const request = new Request(APP_SHELL_URL, {
      cache: "reload",
      credentials: "same-origin"
    });
    const response = await fetch(request);
    if (await isExcavatorShellResponse(response)) {
      const shellHtml = await response.clone().text();
      if (!await cacheExcavatorShellDependencies(cache, shellHtml)) return false;
      await cache.put(APP_SHELL_URL, response.clone());
      return await hasCompleteExcavatorShell(cache, response);
    }
  } catch (error) {
    return false;
  }
  return false;
}

function excavatorShellStaticDependencies(html) {
  const dependencies = [];
  const pattern = /\b(?:src|href)\s*=\s*["']([^"'#]+)["']/gi;
  let match;
  while ((match = pattern.exec(String(html || ""))) !== null) {
    try {
      const url = new URL(match[1].replace(/&amp;/g, "&"), self.location.origin);
      if (url.origin === self.location.origin && url.pathname.startsWith("/static/")) {
        dependencies.push(url.pathname + url.search);
      }
    } catch (error) {}
  }
  return Array.from(new Set(dependencies));
}

async function cacheExcavatorShellDependencies(cache, html) {
  const dependencies = excavatorShellStaticDependencies(html);
  const missing = [];
  for (const path of dependencies) {
    const request = new Request(path, {cache: "reload", credentials: "same-origin"});
    const response = await cache.match(request);
    if (!await isSafeExcavatorCacheEntry(request, response)) missing.push(request);
  }
  if (missing.length) await cache.addAll(missing);
  const available = await Promise.all(dependencies.map(async path => {
    const request = new Request(path, {credentials: "same-origin"});
    return await isSafeExcavatorCacheEntry(request, await cache.match(request));
  }));
  return available.every(Boolean);
}

async function hasCompleteExcavatorShell(cache, response) {
  if (!response || !await isExcavatorShellResponse(response)) return false;
  const shellHtml = await response.clone().text();
  return await cacheExcavatorShellDependencies(cache, shellHtml);
}

async function isSafeExcavatorCacheEntry(request, response) {
  if (!request || !response || !response.ok || !response.url) return false;
  const requestUrl = new URL(request.url, self.location.origin);
  const finalUrl = new URL(response.url, self.location.origin);
  if (requestUrl.origin !== self.location.origin || finalUrl.origin !== self.location.origin) return false;
  const allowed = requestUrl.pathname.startsWith("/static/") ||
    requestUrl.pathname === MANIFEST_URL ||
    requestUrl.pathname === PRIVACY_POLICY_PATH;
  if (!allowed || requestUrl.pathname !== finalUrl.pathname) return false;
  if (
    requestUrl.pathname.startsWith("/static/")
    && requestUrl.search !== finalUrl.search
  ) return false;
  if (
    requestUrl.pathname.startsWith("/static/")
    && String(response.headers.get("Content-Type") || "").toLowerCase().includes("text/html")
  ) return false;
  return true;
}

async function migratePreviousExcavatorCache(cacheNames) {
  const current = await caches.open(CACHE_NAME);
  const existing = await current.match(APP_SHELL_URL);
  try {
    if (await hasCompleteExcavatorShell(current, existing)) return true;
  } catch (error) {}
  if (existing) await current.delete(APP_SHELL_URL);
  for (const cacheName of cacheNames.slice().reverse()) {
    const previous = await caches.open(cacheName);
    const candidate = await previous.match(APP_SHELL_URL);
    if (candidate && await isExcavatorShellResponse(candidate)) {
      const previousRequests = await previous.keys();
      for (const request of previousRequests) {
        const response = await previous.match(request);
        if (await isSafeExcavatorCacheEntry(request, response)) {
          await current.put(request, response.clone());
        }
      }
      try {
        if (!await cacheExcavatorShellDependencies(current, await candidate.clone().text())) continue;
      } catch (error) {
        continue;
      }
      await current.put(APP_SHELL_URL, candidate.clone());
      if (await hasCompleteExcavatorShell(current, candidate)) return true;
      await current.delete(APP_SHELL_URL);
    }
  }
  return false;
}

async function networkFirst(request, fallbackUrl, responseValidator) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    const canCache = response && response.ok &&
      (!responseValidator || await responseValidator(response));
    if (canCache) {
      cache.put(request, response.clone()).catch(() => undefined);
      if (fallbackUrl && new URL(request.url).pathname === fallbackUrl) {
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

async function networkFirstStatic(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request, { cache: "no-store" });
    if (response && response.ok) {
      cache.put(request, response.clone()).catch(() => undefined);
    }
    return response;
  } catch (error) {
    return (await cache.match(request)) ||
      new Response("Resource unavailable offline.", {
        status: 503,
        headers: { "Content-Type": "text/plain; charset=utf-8" }
      });
  }
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
  if (url.pathname === PRIVACY_POLICY_PATH) {
    event.respondWith(networkFirst(request, PRIVACY_POLICY_URL));
    return;
  }
  if (request.mode === "navigate" || url.pathname === APP_SHELL_URL) {
    event.respondWith(networkFirst(request, APP_SHELL_URL, isExcavatorShellResponse));
    return;
  }
  if (url.pathname === MANIFEST_URL) {
    event.respondWith(networkFirst(request, MANIFEST_URL));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(networkFirstStatic(request));
  }
});

self.addEventListener("message", event => {
  if (!event.data) return;
  if (event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
    return;
  }
  if (event.data.type === "CLEAR_AUTHENTICATED_SHELL") {
    const work = caches.keys().then(keys => Promise.all(
      keys.filter(key => key.startsWith(CACHE_PREFIX)).map(async key => {
        const cache = await caches.open(key);
        await cache.delete(APP_SHELL_URL);
      })
    ));
    event.waitUntil(work);
    const target = event.ports && event.ports[0];
    if (target) work.finally(() => target.postMessage({ok: true}));
    return;
  }
  if (event.data.type === "GET_VERSION") {
    const target = event.ports && event.ports[0];
    const payload = {
      type: "VERSION",
      version: CACHE_NAME,
      appContractVersion: APP_CONTRACT_VERSION,
      shellVersion: CACHE_NAME,
      roleCode: ROLE_CODE
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


def format_dispatcher_decimal(value):
    if value is None:
        return ''
    return f'{value:g}'


def excavator_manifest_view(request):
    return role_app_manifest_response(request, 'excavator_operator')


def excavator_service_worker_view(request):
    return role_app_service_worker_response(request, 'excavator_operator', EXCAVATOR_SERVICE_WORKER_JS)


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


def dispatcher_truck_garage_number(truck, fallback_index):
    raw_number = str(getattr(truck, 'garage_number', '') or '').strip()
    match = re.search(r'\d+', raw_number)
    if match:
        number = int(match.group(0))
        if number == 53:
            return None
        return str(number)
    return None


MINING_MASTER_EQUIPMENT_SHIFT_MAX_AGE = timedelta(hours=16)
MINING_MASTER_FRESH_SHIFT_GRACE = timedelta(hours=3)
MINING_MASTER_LIVE_PRESENCE_CODES = frozenset({'online', 'background', 'recent'})


def mining_master_equipment_shift_is_current(shift, *, now=None):
    """Отделяет текущую работу техники от старой незакрытой смены.

    У ранних и стандартных комплексов разные часы пересменки, поэтому в
    окнах 06:00-08:00 и 18:00-20:00 допустимы обе смены. Свежий heartbeat
    сохраняет смену видимой при задержке сотрудника, но запись старше 16
    часов всё равно считается служебным хвостом и не даёт плановый контур.
    """
    if not shift or not shift.equipment_id or not shift.opened_at:
        return False
    now = now or timezone.now()
    age = now - shift.opened_at
    if age > MINING_MASTER_EQUIPMENT_SHIFT_MAX_AGE:
        return False
    if age <= MINING_MASTER_FRESH_SHIFT_GRACE:
        return True
    presence = getattr(shift, 'application_presence', None) or {}
    if presence.get('status_code') in MINING_MASTER_LIVE_PRESENCE_CODES:
        return True
    local_hour = timezone.localtime(now).hour
    if 6 <= local_hour < 8 or 18 <= local_hour < 20:
        return True
    return shift.shift_type == production_shift_type(now)


def dispatcher_employee_badge(employee, *, presence_label='В смене'):
    if not employee:
        return None
    photo_url = ''
    if getattr(employee, 'photo', None):
        try:
            photo_url = employee.photo.url
        except ValueError:
            photo_url = ''
    initials = ''.join(part[0] for part in (employee.full_name or '').split()[:2]).upper()
    presence = getattr(employee, 'application_presence', None) or {}
    client_labels = []
    for badge in presence.get('client_badges') or []:
        label = badge.get('label') or ''
        version = badge.get('version') or ''
        if label:
            client_labels.append(f'{label} · {version}' if version else label)
    return {
        'name': employee.full_name or '',
        'phone': employee.phone or '',
        'position': employee.position or '',
        'photo': photo_url,
        'initials': initials or '??',
        'presence_label': presence_label,
        'presence_status': presence.get('status_code') or '',
        'last_seen_label': format_dispatcher_datetime(presence.get('last_seen_at')),
        'client_label': ', '.join(client_labels),
    }


def excavator_configured_destinations(placement):
    if not placement:
        return []
    prefetched = getattr(placement, '_prefetched_objects_cache', {}).get('dump_point_settings')
    settings_rows = (
        list(prefetched)
        if prefetched is not None
        else list(
            placement.dump_point_settings
            .filter(dump_point__is_active=True)
            .select_related('dump_point')
            .order_by('position', 'id')
        )
    )
    return [
        {
            'dump_point': row.dump_point,
            'transport_distance_km': row.transport_distance_km,
        }
        for row in settings_rows
        if row.dump_point and row.dump_point.is_active
    ]


def dispatcher_excavator_settings(equipment, placement, *, rock_types, dump_points):
    if not equipment or equipment.equipment_type.name != 'Экскаватор':
        return None
    destination_rows = excavator_configured_destinations(placement)
    if not destination_rows and placement and placement.work_dump_point_id:
        destination_rows = [{
            'dump_point': placement.work_dump_point,
            'transport_distance_km': placement.transport_distance_km,
        }]
    return {
        'editable': True,
        'title': 'Настройки работы',
        'hint': 'Общие для диспетчера и машиниста экскаватора.',
        'loading_horizon': getattr(placement, 'loading_horizon', '') or '',
        'loading_block': getattr(placement, 'loading_block', '') or '',
        'rock_type_id': getattr(placement, 'work_rock_type_id', None),
        'destinations': [
            {
                'dump_point_id': row['dump_point'].id,
                'name': str(row['dump_point']),
                'transport_distance_km': (
                    format(row['transport_distance_km'], 'f')
                    if row['transport_distance_km'] is not None
                    else ''
                ),
            }
            for row in destination_rows
        ],
        'rock_types': [{'id': item.id, 'name': str(item)} for item in rock_types],
        'dump_points': [{'id': item.id, 'name': str(item)} for item in dump_points],
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


def format_whole_input_value(value):
    if value in {None, ''}:
        return ''
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    return str(int(parsed.to_integral_value(rounding=ROUND_HALF_UP)))


def excavator_fuel_percent_from_liters(value, capacity):
    if value in {None, ''} or not capacity:
        return ''
    try:
        liters = Decimal(value)
        capacity_value = Decimal(capacity)
    except (InvalidOperation, TypeError, ValueError):
        return ''
    if capacity_value <= 0:
        return '0'
    percent = (liters * Decimal('100') / capacity_value).to_integral_value(rounding=ROUND_HALF_UP)
    return str(max(0, int(percent)))


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


def dispatcher_trip_count_label(count):
    count = int(count or 0)
    remainder_100 = count % 100
    remainder_10 = count % 10
    if 11 <= remainder_100 <= 14:
        word = 'рейсов'
    elif remainder_10 == 1:
        word = 'рейс'
    elif 2 <= remainder_10 <= 4:
        word = 'рейса'
    else:
        word = 'рейсов'
    return f'{count} {word}'


def dispatcher_shift_plan_detail(
    *,
    completed_trips,
    active_trips,
    completed_use_tonnage,
    fact_tons,
    plan_tons,
    completion_percent,
):
    by_dump_point = defaultdict(lambda: {'fact': Decimal('0'), 'trip_count': 0})
    by_route = defaultdict(lambda: {'fact': Decimal('0'), 'trip_count': 0})

    def decimal_percent(value, *, places=1):
        quantum = Decimal('1').scaleb(-places)
        rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
        return format(rounded, 'f').rstrip('0').rstrip('.') or '0'

    plan_value = Decimal(str(plan_tons or 0))
    fact_value = Decimal(str(fact_tons or 0))
    exact_completion = (
        max(Decimal('0'), min(Decimal('100'), (fact_value / plan_value) * 100))
        if plan_value
        else Decimal('0')
    )
    completion_percent_css = decimal_percent(exact_completion)

    def add_trip(trip, amount):
        amount = amount or Decimal('0')
        if amount <= 0:
            return
        dump_point = trip.actual_dump_point or trip.assigned_dump_point or trip.dump_point
        destination_label = str(dump_point) if dump_point else 'Точка не указана'
        excavator_number = str(getattr(trip.excavator, 'garage_number', '') or '').strip()
        excavator_match = re.search(r'\d+', excavator_number)
        source_label = (
            f'К-{int(excavator_match.group(0))}'
            if excavator_match
            else excavator_number or 'Комплекс не указан'
        )
        by_dump_point[destination_label]['fact'] += amount
        by_dump_point[destination_label]['trip_count'] += 1
        route = by_route[(source_label, destination_label)]
        route['source'] = source_label
        route['destination'] = destination_label
        route['fact'] += amount
        route['trip_count'] += 1

    completed_fact = sum(
        (
            (trip.tonnage if completed_use_tonnage else trip.volume_m3)
            or Decimal('0')
            for trip in completed_trips
        ),
        Decimal('0'),
    )
    active_fact = sum(
        (trip.tonnage or trip.volume_m3 or Decimal('0') for trip in active_trips),
        Decimal('0'),
    )
    for trip in completed_trips:
        add_trip(trip, trip.tonnage if completed_use_tonnage else trip.volume_m3)
    for trip in active_trips:
        add_trip(trip, trip.tonnage or trip.volume_m3)

    def allocate_contribution(rows):
        total = sum((row['fact'] for row in rows), Decimal('0'))
        allocations = []
        for index, row in enumerate(rows):
            exact_percent = (
                (row['fact'] / total) * visible_percent
                if total and visible_percent
                else Decimal('0')
            )
            whole_percent = int(exact_percent)
            exact_fact_share = (row['fact'] / total) * 100 if total else Decimal('0')
            whole_fact_share = int(exact_fact_share)
            allocations.append({
                **row,
                'index': index,
                'contribution_percent': whole_percent,
                'remainder': exact_percent - whole_percent,
                'fact_share_percent': whole_fact_share,
                'fact_share_remainder': exact_fact_share - whole_fact_share,
                'plan_percent_label': (
                    decimal_percent((row['fact'] / plan_value) * 100, places=2)
                    if plan_value
                    else '0'
                ),
            })
        unallocated = visible_percent - sum(row['contribution_percent'] for row in allocations)
        for row in sorted(allocations, key=lambda item: (-item['remainder'], item['index']))[:unallocated]:
            row['contribution_percent'] += 1
        unallocated_fact_share = (
            (100 if total else 0)
            - sum(row['fact_share_percent'] for row in allocations)
        )
        ordered_fact_shares = sorted(
            allocations,
            key=lambda item: (-item['fact_share_remainder'], item['index']),
        )
        for row in ordered_fact_shares[:unallocated_fact_share]:
            row['fact_share_percent'] += 1
        return allocations

    visible_percent = max(0, int(completion_percent or 0))
    unit_label = 'т' if completed_use_tonnage or any(trip.tonnage for trip in active_trips) else 'м³'
    points = [{'name': name, **values} for name, values in by_dump_point.items()]
    points.sort(key=lambda row: (-row['fact'], row['name']))
    if len(points) > 5:
        other_points = points[4:]
        points = points[:4] + [{
            'name': 'Другие точки',
            'fact': sum((row['fact'] for row in other_points), Decimal('0')),
            'trip_count': sum(row['trip_count'] for row in other_points),
        }]

    point_allocations = allocate_contribution(points)

    routes = list(by_route.values())
    routes.sort(key=lambda row: (-row['fact'], row['source'], row['destination']))
    if len(routes) > 6:
        other_routes = routes[5:]
        routes = routes[:5] + [{
            'source': 'Другие',
            'destination': 'разные точки',
            'fact': sum((row['fact'] for row in other_routes), Decimal('0')),
            'trip_count': sum(row['trip_count'] for row in other_routes),
        }]
    route_allocations = allocate_contribution(routes)

    return {
        'completion_percent': visible_percent,
        'completion_percent_css': completion_percent_css,
        'completion_percent_label': completion_percent_css.replace('.', ','),
        'fact_tons': format_dispatcher_number(fact_tons),
        'completed_fact_tons': format_dispatcher_number(completed_fact),
        'active_fact_tons': format_dispatcher_number(active_fact),
        'plan_tons': format_dispatcher_number(plan_tons),
        'completed_trip_count': len(completed_trips),
        'active_trip_count': len(active_trips),
        'trip_count_label': dispatcher_trip_count_label(len(completed_trips) + len(active_trips)),
        'unit_label': unit_label,
        'points': [
            {
                'name': row['name'],
                'fact_tons': format_dispatcher_number(row['fact']),
                'trip_count_label': dispatcher_trip_count_label(row['trip_count']),
                'contribution_percent': row['contribution_percent'],
                'fact_share_percent': row['fact_share_percent'],
                'plan_percent_label': row['plan_percent_label'].replace('.', ','),
            }
            for row in point_allocations
        ],
        'routes': [
            {
                'source': row['source'],
                'destination': row['destination'],
                'fact_tons': format_dispatcher_number(row['fact']),
                'trip_count_label': dispatcher_trip_count_label(row['trip_count']),
                'contribution_percent': row['contribution_percent'],
                'fact_share_percent': row['fact_share_percent'],
                'plan_percent_label': row['plan_percent_label'].replace('.', ','),
            }
            for row in route_allocations
        ],
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

def dispatcher_complex_shift_report(card):
    return _build_dispatcher_complex_shift_report(
        card,
        format_number=format_dispatcher_number,
        chart_percent=dispatcher_chart_percent,
    )


def dispatcher_shift_reading_label(value):
    """Показание на начало смены для подсказки в карточке: целое — без хвоста."""
    if value is None:
        return ''
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    if number == number.to_integral_value():
        return str(int(number))
    return format(number.normalize(), 'f').replace('.', ',')


def dispatcher_duration_label(delta):
    total_minutes = int(max(delta.total_seconds(), 0) // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f'{hours} ч {minutes} мин'
    if hours:
        return f'{hours} ч'
    return f'{minutes} мин'


def dispatcher_shift_period_fields(shift, *, now=None):
    """Чья это смена: текущего периода или хвост, который не закрыл прошлый водитель.

    Сравниваем производственный период смены (дата + первая/вторая) с
    текущим по часам предприятия; окно пересменки считаем допустимым по тем
    же правилам, что и горный мастер (mining_master_equipment_shift_is_current).
    """
    now = now or timezone.now()
    if not shift or not shift.opened_at:
        return {
            'verdict': 'unknown',
            'verdict_label': '',
            'alert': '',
            'duration_label': '',
            'period_label': '',
            'current_period_label': '',
        }
    shift_type_labels = dict(EmployeeShift._meta.get_field('shift_type').choices)
    context = production_shift_context(now)
    work_date = production_work_date_for_shift(shift.opened_at, shift.shift_type)
    period_label = f'{shift_type_labels.get(shift.shift_type, shift.shift_type)} {work_date.strftime("%d.%m")}'
    current_period_label = (
        f'{shift_type_labels.get(context.shift_type, context.shift_type)} '
        f'{context.production_date.strftime("%d.%m")}'
    )
    duration_label = dispatcher_duration_label(now - shift.opened_at)
    same_period = work_date == context.production_date and shift.shift_type == context.shift_type
    if same_period:
        verdict, verdict_label, alert = 'current', 'Текущая смена', ''
    elif mining_master_equipment_shift_is_current(shift, now=now):
        verdict, verdict_label = 'overlap', 'Открыта в пересменку'
        alert = (
            f'Смена открыта {format_dispatcher_datetime(shift.opened_at)} ({period_label.lower()}), '
            f'сейчас идёт {current_period_label.lower()}. Проверьте, что это нынешний водитель.'
        )
    else:
        verdict, verdict_label = 'stale', 'Прошлая смена — не закрыта'
        alert = (
            f'Смена открыта {format_dispatcher_datetime(shift.opened_at)} ({period_label.lower()}) '
            f'и длится уже {duration_label}. Сейчас идёт {current_period_label.lower()}: '
            f'водитель прошлой смены не закрыл её — закройте служебно.'
        )
    return {
        'verdict': verdict,
        'verdict_label': verdict_label,
        'alert': alert,
        'duration_label': duration_label,
        'period_label': period_label,
        'current_period_label': current_period_label,
    }


def dispatcher_manual_trip_payload(truck, *, excavator, placement, truck_shift, rock_types, dump_points):
    """Данные для ручного рейса в карточке самосвала на пульте.

    Рейс создаётся сразу выполненным от имени водителя открытой смены на
    экскаватор, к которому самосвал назначен; точки и порода берутся из
    настроек забоя, но диспетчер может выбрать любые активные.
    """
    if not truck:
        return None
    blocked_reason = ''
    if not excavator:
        blocked_reason = 'Самосвал не назначен в комплекс — рейс добавить нельзя.'
    elif not truck_shift:
        blocked_reason = 'У самосвала нет открытой смены водителя — рейс некому записать.'
    destination_rows = excavator_configured_destinations(placement) if placement else []
    if not destination_rows and placement and placement.work_dump_point_id:
        destination_rows = [{
            'dump_point': placement.work_dump_point,
            'transport_distance_km': placement.transport_distance_km,
        }]
    return {
        'url': reverse('dispatcher_manual_trip', args=[truck.id]),
        'can_add': not blocked_reason,
        'blocked_reason': blocked_reason,
        'excavator_id': getattr(excavator, 'id', None),
        'excavator_label': equipment_short_name(excavator) if excavator else '',
        'rock_type_id': getattr(placement, 'work_rock_type_id', None),
        'rock_types': [{'id': item.id, 'name': str(item)} for item in rock_types],
        'destinations': [
            {
                'dump_point_id': row['dump_point'].id,
                'name': str(row['dump_point']),
                'transport_distance_km': (
                    format(row['transport_distance_km'], 'f')
                    if row['transport_distance_km'] is not None
                    else ''
                ),
            }
            for row in destination_rows
        ],
        'dump_points': [{'id': item.id, 'name': str(item)} for item in dump_points],
        'max_count': DISPATCHER_MANUAL_TRIP_MAX_COUNT,
    }


def dispatcher_shift_card_payload(shift):
    """Открытая смена сотрудника на этой технике — для карточки пульта.

    Диспетчер завершает смену машиниста/водителя из карточки через уже
    существующий служебный маршрут dispatcher_service_close_shift; отсюда
    карточке нужны id смены, вид техники (какие показания спрашивать) и
    сведения о связи, чтобы не искать их в общем списке деталей.
    """
    if not shift:
        return None
    presence = getattr(shift, 'application_presence', None) or {}
    is_truck = bool(shift.equipment_id and equipment_is_truck(shift.equipment))
    return {
        'id': shift.id,
        'type_label': shift.get_shift_type_display(),
        'opened_at': shift.opened_at.isoformat() if shift.opened_at else '',
        'opened_at_label': format_dispatcher_datetime(shift.opened_at),
        'is_truck': is_truck,
        'presence_status': presence.get('status_code') or 'not_registered',
        'presence_label': presence.get('status_label') or 'Не подключался',
        'last_seen_label': format_dispatcher_datetime(presence.get('last_seen_at')),
        'plan_group_name': shift.plan_group_name or '',
        'auto_close_at_label': (
            format_dispatcher_datetime(equipment_shift_auto_close_at(shift))
            if shift.equipment_id else ''
        ),
        **dispatcher_shift_period_fields(shift),
        'start_fuel': dispatcher_shift_reading_label(shift.start_fuel),
        'start_mileage': dispatcher_shift_reading_label(shift.start_mileage),
        'start_engine_hours': dispatcher_shift_reading_label(shift.start_engine_hours),
        'service_close_url': reverse('dispatcher_service_close_shift', args=[shift.id]),
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
    employee_presence_label='В смене',
    details=None,
    shift_report=None,
    category='equipment',
    plan=None,
    settings=None,
    downtime=None,
    shift=None,
    manual_trip=None,
    include_equipment_metadata=True,
):
    card_details = []
    seen_labels = set()
    if equipment and include_equipment_metadata:
        type_name = type_name or equipment.equipment_type.name
        number = number or equipment_short_name(equipment)
        model = equipment.model
        add_dispatcher_detail(card_details, seen_labels, 'Гаражный N', equipment.garage_number)
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
        'employee': dispatcher_employee_badge(employee, presence_label=employee_presence_label),
        'details': card_details,
        'shift_report': shift_report or {},
        'category': category,
        'plan': dispatcher_plan_api_payload(plan),
        'settings': settings,
        'downtime': dispatcher_downtime_card_payload(downtime),
        'shift': dispatcher_shift_card_payload(shift),
        'manual_trip': manual_trip,
    }


def build_dispatcher_dashboard_context(
    *,
    dispatcher_shift,
    active_trips,
    pending_assignments,
    accepted_assignments,
    recent_completed_trips,
    open_shifts,
    open_mechanic_downtimes,
    trucks,
    excavators,
    recent_dispatcher_actions,
    equipment_card_ids=None,
    equipment_work_assignments=(),
    reporting_period=None,
):
    active_trips_list = list(active_trips)
    pending_assignments_list = list(pending_assignments)
    accepted_assignments_list = list(accepted_assignments)
    accepted_source_assignment_by_truck_id = {}
    for assignment in accepted_assignments_list:
        accepted_source_assignment_by_truck_id.setdefault(assignment.truck_id, assignment)
    assignment_by_truck = {}
    for assignment in accepted_assignments_list + pending_assignments_list:
        current = assignment_by_truck.get(assignment.truck_id)
        if current is None:
            assignment_by_truck[assignment.truck_id] = assignment
            continue
        current_time = current.assigned_at or current.created_at
        assignment_time = assignment.assigned_at or assignment.created_at
        if (assignment_time, assignment.id or 0) >= (current_time, current.id or 0):
            assignment_by_truck[assignment.truck_id] = assignment
    active_assignments_list = list(assignment_by_truck.values())
    pending_assignments_list = [
        assignment for assignment in active_assignments_list
        if (
            assignment.status == AssignmentStatus.PENDING
            and assignment.action != HaulAssignmentAction.RELEASE
        )
    ]
    accepted_assignments_list = [
        assignment for assignment in active_assignments_list
        if assignment.status == AssignmentStatus.ACCEPTED
    ]
    recent_completed_trips_list = list(recent_completed_trips)
    open_downtime_list = list(open_mechanic_downtimes)
    trucks_list = list(trucks)
    excavators_list = list(excavators)
    # The dispatcher keeps a truck in its primary complex.  This separate
    # annotation explains a temporary free-bucket operation without turning it
    # into a dispatcher reassignment.
    from trips.models import FreeBucketAcceptance
    free_bucket_marker_by_truck_id = {}
    free_bucket_marker_now = timezone.now()
    for acceptance in (
        FreeBucketAcceptance.objects
        .filter(
            truck_id__in=[truck.id for truck in trucks_list],
        )
        .filter(active_free_bucket_acceptance_filter(now=free_bucket_marker_now))
        .select_related('excavator', 'used_trip')
        .order_by('-occurred_at', '-id')
    ):
        free_bucket_marker_by_truck_id.setdefault(
            acceptance.truck_id,
            {
                'label': 'Свободный ковш · ' + equipment_short_name(acceptance.excavator),
                'expires_at': free_bucket_acceptance_expires_at(acceptance),
            },
        )
    shift_trip_queryset = Trip.objects.none()
    shift_trip_attribution = None
    production_shift_start = None
    production_shift_end = None
    reporting_starts_at = (reporting_period or {}).get('starts_at')
    reporting_ends_at = (reporting_period or {}).get('ends_at')
    is_mining_master_reporting_period = bool(reporting_starts_at)
    if is_mining_master_reporting_period:
        # Смена мастера — период ответственности, а не смена оборудования.
        # Выполненный рейс входит по времени выгрузки; пока рейс не завершён,
        # он отдельно отображается как «в пути» только у текущего периода.
        completed_period_filter = Q(
            status=TripStatus.COMPLETED,
            completed_at__gte=reporting_starts_at,
        )
        if reporting_ends_at:
            completed_period_filter &= Q(completed_at__lt=reporting_ends_at)
        active_period_filter = (
            Q(status__in=OPEN_TRIP_STATUSES)
            if not reporting_ends_at
            else Q(pk__in=[])
        )
        shift_trip_queryset = (
            Trip.objects
            .filter(completed_period_filter | active_period_filter)
            .select_related(
                'truck',
                'excavator',
                'rock_type',
                'dump_point',
                'assigned_dump_point',
                'actual_dump_point',
            )
            .order_by('-completed_at', '-created_at')
        )
    elif dispatcher_shift:
        production_shift_start, production_shift_end = production_shift_bounds(
            production_work_date_for_shift(
                dispatcher_shift.opened_at,
                dispatcher_shift.shift_type,
            ),
            dispatcher_shift.shift_type,
        )
        shift_trip_attribution = (
            Q(loading_shift=dispatcher_shift)
            | Q(
                loading_shift__isnull=True,
                created_at__gte=production_shift_start,
                created_at__lt=production_shift_end,
            )
        )
        shift_trip_queryset = (
            Trip.objects
            .filter(shift_trip_attribution)
            .select_related(
                'truck',
                'excavator',
                'rock_type',
                'dump_point',
                'assigned_dump_point',
                'actual_dump_point',
            )
            .order_by('-created_at')
        )
    shift_trips = list(shift_trip_queryset[:500])
    reporting_plan_facts = {
        'truck': {},
        'excavator': {},
    }
    if is_mining_master_reporting_period:
        # Контур плана на пульте мастера должен считать тот же отрезок,
        # что и его отчёт. Смены самосвалов и экскаваторов могут начаться
        # раньше или закончиться позже — они задают только сам план, но не
        # переносят свой старый факт в новую смену мастера.
        def reporting_plan_facts_by_equipment(equipment_field):
            equipment_key = f'{equipment_field}_id'
            rows = (
                shift_trip_queryset
                .filter(status=TripStatus.COMPLETED)
                .order_by()
                .values(equipment_key)
                .annotate(
                    trip_count=Count('id'),
                    volume_m3=Sum('volume_m3'),
                    tonnage=Sum('tonnage'),
                )
            )
            return {
                row[equipment_key]: {
                    'trip_count': row['trip_count'],
                    'volume_m3': row['volume_m3'],
                    'tonnage': row['tonnage'],
                }
                for row in rows
                if row[equipment_key]
            }

        reporting_plan_facts = {
            'truck': reporting_plan_facts_by_equipment('truck'),
            'excavator': reporting_plan_facts_by_equipment('excavator'),
        }
    first_open_shift_by_equipment_id = {}
    latest_open_shift_by_equipment_id = {}
    for shift in open_shifts:
        if not shift.equipment_id:
            continue
        first_open_shift_by_equipment_id.setdefault(shift.equipment_id, shift)
        current = latest_open_shift_by_equipment_id.get(shift.equipment_id)
        if current is None or (shift.opened_at, shift.id) > (current.opened_at, current.id):
            latest_open_shift_by_equipment_id[shift.equipment_id] = shift
    dashboard_now = timezone.now()
    if is_mining_master_reporting_period:
        open_shift_by_equipment_id = {
            equipment_id: shift
            for equipment_id, shift in latest_open_shift_by_equipment_id.items()
            if mining_master_equipment_shift_is_current(shift, now=dashboard_now)
        }
        stale_equipment_shifts = [
            shift
            for shift in open_shifts
            if shift.equipment_id and open_shift_by_equipment_id.get(shift.equipment_id) is not shift
        ]
    else:
        # Диспетчерский пульт сохраняет свой прежний полный список открытых
        # смен. Правило отсечения хвостов относится только к рабочему экрану
        # Горного мастера и его периоду ответственности.
        open_shift_by_equipment_id = first_open_shift_by_equipment_id
        stale_equipment_shifts = []
    work_assignment_by_equipment_id = {
        assignment.equipment_id: assignment
        for assignment in equipment_work_assignments
        if assignment.equipment_id
    }

    truck_equipment_ids = {truck.id for truck in trucks_list}
    excavator_equipment_ids = {excavator.id for excavator in excavators_list}
    selected_equipment_shifts = list(open_shift_by_equipment_id.values())
    snapshot_trip_facts = (
        {'unloading': {}, 'loading': {}}
        if is_mining_master_reporting_period
        else aggregate_completed_trip_facts_by_shift(
            unloading_shift_ids=(
                shift.id
                for shift in selected_equipment_shifts
                if shift.equipment_id in truck_equipment_ids
            ),
            loading_shift_ids=(
                shift.id
                for shift in selected_equipment_shifts
                if shift.equipment_id in excavator_equipment_ids and shift.plan_status
            ),
        )
    )
    plan_by_equipment_id = {}

    def dispatcher_plan_for_equipment(equipment):
        equipment_id = getattr(equipment, 'id', None)
        if not equipment_id:
            return plan_progress_display_context(None)
        if equipment_id not in plan_by_equipment_id:
            shift = open_shift_by_equipment_id.get(equipment_id)
            if shift and is_mining_master_reporting_period:
                equipment_kind = 'truck' if equipment_id in truck_equipment_ids else 'excavator'
                progress = calculate_progress_from_snapshot_facts(
                    shift,
                    reporting_plan_facts[equipment_kind].get(equipment_id),
                )
            elif shift and equipment_id in truck_equipment_ids:
                progress = calculate_progress_from_snapshot_facts(
                    shift,
                    snapshot_trip_facts['unloading'].get(shift.id),
                )
            elif shift and equipment_id in excavator_equipment_ids and shift.plan_status:
                progress = calculate_progress_from_snapshot_facts(
                    shift,
                    snapshot_trip_facts['loading'].get(shift.id),
                )
            else:
                progress = calculate_dispatcher_snapshot_progress(shift, equipment=equipment)
            plan_by_equipment_id[equipment_id] = plan_progress_display_context(progress)
        return plan_by_equipment_id[equipment_id]

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
    requested_equipment_card_ids = (
        None
        if equipment_card_ids is None
        else {str(card_id) for card_id in equipment_card_ids}
    )

    def dispatcher_card_shift_report(equipment, equipment_kind):
        """Отчёт карточки: рейсы и простои текущей смены самой техники."""
        report = dispatcher_shift_report_for_equipment(
            equipment,
            equipment_kind=equipment_kind,
            shift_trips=dispatcher_card_shift_trips(equipment),
        )
        return dispatcher_report_with_downtimes(
            report,
            equipment,
            open_shift_by_equipment_id.get(getattr(equipment, 'id', None)),
            now=dashboard_now,
        )

    def dispatcher_card_shift_trips(equipment):
        """Рейсы для блока «смена на текущий момент» в карточке техники.

        Сводки доски привязаны к смене диспетчера, и это правильно: доска —
        его рабочее место. Карточка же рассказывает про саму машину, и её
        шапка уже считает план по смене машины. Берём тот же источник, иначе
        рядом с «80 % плана» стоят нули: у диспетчера может не быть своей
        открытой смены, а рейсы могут быть записаны на смену сменщика.

        Карточка строится по запросу (equipment_card_ids), поэтому запрос
        здесь выполняется для одной единицы техники, а не для всей доски.
        """
        if is_mining_master_reporting_period:
            # У мастера карточка обязана совпадать с его отчётным периодом.
            return shift_trips
        if equipment is None:
            return shift_trips
        equipment_shift = open_shift_by_equipment_id.get(equipment.id)
        if equipment_shift is None or not equipment_shift.id:
            return shift_trips
        if equipment_is_truck(equipment):
            # Рейс попадает в смену водителя по выгрузке — так же считает план.
            scope = Q(unloading_shift_id=equipment_shift.id) | Q(
                truck_id=equipment.id, status__in=OPEN_TRIP_STATUSES
            )
        else:
            scope = Q(loading_shift_id=equipment_shift.id) | Q(
                excavator_id=equipment.id, status__in=OPEN_TRIP_STATUSES
            )
        return list(
            Trip.objects
            .filter(scope)
            .exclude(status=TripStatus.CANCELLED)
            .select_related('truck', 'excavator', 'rock_type', 'dump_point', 'actual_dump_point')
            .order_by('-created_at', '-id')[:200]
        )
    equipment_state_map = get_equipment_state_ui_map()

    completed_tons = Decimal('0')
    completed_use_tonnage = False
    if is_mining_master_reporting_period:
        completed_tons = (
            shift_trip_queryset
            .filter(status=TripStatus.COMPLETED)
            .aggregate(total=Sum('tonnage'))['total']
            or Decimal('0')
        )
        completed_use_tonnage = completed_tons > 0
    elif dispatcher_shift:
        completed_tons = (
            Trip.objects
            .filter(status=TripStatus.COMPLETED)
            .filter(shift_trip_attribution)
            .aggregate(total=Sum('tonnage'))['total']
            or Decimal('0')
        )
        completed_use_tonnage = completed_tons > 0
    if (is_mining_master_reporting_period or dispatcher_shift) and completed_tons == 0:
        completed_volume_queryset = (
            shift_trip_queryset
            if is_mining_master_reporting_period
            else Trip.objects.filter(shift_trip_attribution)
        )
        completed_tons = (
            completed_volume_queryset
            .filter(status=TripStatus.COMPLETED)
            .aggregate(total=Sum('volume_m3'))['total']
            or Decimal('0')
        )
    completed_shift_trips = [trip for trip in shift_trips if trip.status == TripStatus.COMPLETED]
    active_shift_trips = [trip for trip in shift_trips if trip.status in OPEN_TRIP_STATUSES]
    if is_mining_master_reporting_period:
        active_volume = sum(
            (trip.tonnage or trip.volume_m3 or Decimal('0'))
            for trip in active_shift_trips
        )
    else:
        active_volume = (
            sum(
                (trip.tonnage or trip.volume_m3 or Decimal('0'))
                for trip in active_trips_list
                if (
                    (
                        trip.loading_shift_id
                        and production_shift_start <= trip.loading_shift.opened_at < production_shift_end
                    )
                    or (
                        not trip.loading_shift_id
                        and production_shift_start <= trip.created_at < production_shift_end
                    )
                )
            )
            if dispatcher_shift
            else Decimal('0')
        )
    fact_tons = completed_tons + active_volume
    display_fact_tons = fact_tons
    forecast_tons = min(DISPATCHER_PLAN_TOTAL_TONS, display_fact_tons)
    completion_percent = int((display_fact_tons / DISPATCHER_PLAN_TOTAL_TONS) * 100) if DISPATCHER_PLAN_TOTAL_TONS else 0
    completion_percent = max(0, min(99, completion_percent))
    shift_plan_detail = dispatcher_shift_plan_detail(
        completed_trips=completed_shift_trips,
        active_trips=active_shift_trips,
        completed_use_tonnage=completed_use_tonnage,
        fact_tons=display_fact_tons,
        plan_tons=DISPATCHER_PLAN_TOTAL_TONS,
        completion_percent=completion_percent,
    )
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
    if dispatcher_shift or is_mining_master_reporting_period:
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
    placement_by_excavator_id = {
        placement.excavator_id: placement
        for placement in (
            ExcavatorPlacement.objects
            .filter(excavator__in=excavators_list)
            .select_related('work_rock_type', 'work_dump_point')
            .prefetch_related(Prefetch(
                'dump_point_settings',
                queryset=(
                    ExcavatorDumpPointSetting.objects
                    .filter(dump_point__is_active=True)
                    .select_related('dump_point')
                    .order_by('position', 'id')
                ),
            ))
        )
    }
    dispatcher_rock_types = list(RockType.objects.filter(is_active=True).order_by('name'))
    dispatcher_dump_points = list(DumpPoint.objects.filter(is_active=True).order_by('name'))
    active_excavator_ids = set(active_placement_ids)
    active_excavator_ids.update(
        assignment.excavator_id
        for assignment in pending_assignments_list + accepted_assignments_list
        if assignment.excavator_id and assignment.action != HaulAssignmentAction.RELEASE
    )
    active_excavator_ids.update(trip.excavator_id for trip in active_trips_list if trip.excavator_id)

    downtime_state_code_by_equipment_id = {
        equipment_id: downtime_equipment_state_code(downtime)
        for equipment_id, downtime in downtime_by_equipment_id.items()
    }
    truck_state_code_by_id = {}
    truck_state_by_id = {}
    for truck in trucks_list:
        active_trip = active_trip_by_truck_id.get(truck.id)
        assignment = assignment_by_truck_id.get(truck.id)
        state_code = dispatcher_truck_state_code(
            is_active=getattr(truck, 'is_active', True),
            downtime_state_code=(
                downtime_state_code_by_equipment_id.get(truck.id) or ''
            ),
            has_open_trip=bool(
                active_trip and active_trip.status in OPEN_TRIP_STATUSES
            ),
            has_pending_assignment=bool(
                assignment and assignment.status == AssignmentStatus.PENDING
            ),
            has_accepted_assignment=bool(
                assignment and assignment.status == AssignmentStatus.ACCEPTED
            ),
        )
        truck_state_code_by_id[truck.id] = state_code
        status, label, resolved_code = dispatcher_equipment_state_tuple(
            equipment_state_ui(equipment_state_map, state_code)
        )
        if downtime_state_code_by_equipment_id.get(truck.id):
            label = (
                dispatcher_downtime_reason_label(
                    downtime_by_equipment_id.get(truck.id)
                )
                or label
            )
        truck_state_by_id[truck.id] = status, label, resolved_code

    excavator_state_code_by_id = {}
    excavator_state_by_id = {}
    for excavator in excavators_list:
        state_code = dispatcher_excavator_state_code(
            is_active=getattr(excavator, 'is_active', True),
            downtime_state_code=(
                downtime_state_code_by_equipment_id.get(excavator.id) or ''
            ),
            has_active_trip=bool(
                active_trip_by_excavator_id.get(excavator.id)
            ),
            is_in_active_zone=excavator.id in active_excavator_ids,
        )
        excavator_state_code_by_id[excavator.id] = state_code
        excavator_state_by_id[excavator.id] = dispatcher_equipment_state_tuple(
            equipment_state_ui(equipment_state_map, state_code)
        )

    excavator_by_id = {excavator.id: excavator for excavator in excavators_list}
    shown_excavators = sorted(
        [excavator_by_id[equipment_id] for equipment_id in active_excavator_ids if equipment_id in excavator_by_id],
        key=dispatcher_garage_number_int,
    )

    trips_by_excavator_id = defaultdict(list)
    for trip in shift_trips:
        if trip.excavator_id:
            trips_by_excavator_id[trip.excavator_id].append(trip)

    complex_cards = []
    for excavator in shown_excavators:
        # Техника с гаражным номером «ТЕСТ...» — заглушка, а не настоящий
        # экскаватор, даже если она активна в справочнике. Номер комплекса
        # раньше брали через поиск первой цифры в названии — и «ТЕСТ-Э1»
        # получал тот же номер, что настоящий экскаватор №1: оба содержат
        # цифру «1». 28.08.2026 из-за этого на пульте одновременно показались
        # два разных «К-1». Формат «Э-1» (буква + число) — обычная запись для
        # настоящей техники, его не трогаем; фильтруем только префикс «ТЕСТ».
        if str(excavator.garage_number or '').strip().upper().startswith('ТЕСТ'):
            continue
        index = dispatcher_garage_number_int(excavator)
        complex_label = dispatcher_complex_label(excavator)
        row = by_excavator[excavator.id]
        need = max(len(row['trucks']), row['accepted'] + row['pending'], 0)
        assigned = row['accepted'] + row['active_trips']
        plan = Decimal('0')
        fact = row['volume']
        excavator_plan = dispatcher_plan_for_equipment(excavator)
        percent = excavator_plan['css_percent']
        complex_state_code = dispatcher_complex_state_code(
            is_active=bool(excavator and getattr(excavator, 'is_active', True)),
            downtime_state_code=(
                downtime_state_code_by_equipment_id.get(excavator.id) or ''
            ),
            has_pending=bool(row.get('pending')),
            has_active_trips=bool(row.get('active_trips')),
            has_accepted=bool(row.get('accepted')),
            is_in_active_zone=excavator.id in active_excavator_ids,
        )
        status_key, status_label, equipment_state_code = (
            dispatcher_equipment_state_tuple(
                equipment_state_ui(equipment_state_map, complex_state_code)
            )
        )
        status_label = dispatcher_downtime_reason_label(
            downtime_by_equipment_id.get(excavator.id)
        ) or status_label

        complex_trips = trips_by_excavator_id.get(excavator.id, [])
        placement = placement_by_excavator_id.get(excavator.id)
        current_horizon = f'Гор. {placement.loading_horizon}' if placement and placement.loading_horizon else ''
        current_block = f'Блок {placement.loading_block}' if placement and placement.loading_block else ''
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
        current_assignment_by_truck_id = {
            assignment.truck_id: assignment
            for assignment in current_assignments
        }
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
        truck_by_id = {truck.id: truck for truck in trucks_list}
        for truck_id in sorted(current_truck_ids, key=lambda item: dispatcher_garage_number_int(truck_by_id.get(item)) if item in truck_by_id else 9999):
            truck = truck_by_id.get(truck_id)
            if not truck:
                continue
            current_assignment = current_assignment_by_truck_id.get(truck_id)
            source_assignment = accepted_source_assignment_by_truck_id.get(truck_id)
            transfer_pending = bool(
                current_assignment
                and current_assignment.status == AssignmentStatus.PENDING
                and current_assignment.action == HaulAssignmentAction.ASSIGN
                and source_assignment
                and source_assignment.excavator_id != current_assignment.excavator_id
            )
            transfer_source_excavator = (
                excavator_by_id.get(source_assignment.excavator_id)
                if transfer_pending
                else None
            )
            truck_status, truck_state_label, truck_state_code = (
                truck_state_by_id[truck.id]
            )
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
                'assignment_state_id': (
                    assignment_by_truck[truck_id].id
                    if truck_id in assignment_by_truck
                    else 0
                ),
                'transfer_pending': transfer_pending,
                'transfer_source_label': (
                    dispatcher_complex_label(transfer_source_excavator)
                    if transfer_source_excavator
                    else ''
                ),
                'free_bucket_label': (
                    free_bucket_marker_by_truck_id.get(truck_id, {}).get('label', '')
                ),
                'free_bucket_expires_at': (
                    free_bucket_marker_by_truck_id.get(truck_id, {}).get('expires_at')
                ),
                **dispatcher_equipment_presence_fields(
                    truck.id,
                    open_shift_by_equipment_id,
                ),
            })
        forecast = fact
        current_rock = (
            str(placement.work_rock_type)
            if placement and placement.work_rock_type_id
            else (rock_values[0] if rock_values else '')
        )
        active_downtime = downtime_by_equipment_id.get(excavator.id)
        complex_cards.append({
            'id': complex_label,
            'zone_key': f'equipment-{excavator.id}',
            'excavator_slot': complex_label[2:],
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
            'card_id': f'complex-equipment-{excavator.id}',
            'equipment_card_id': str(excavator.id) if excavator else '',
            'truck_rows': truck_rows,
            'current_horizon': current_horizon,
            'current_block': current_block,
            'current_rock': current_rock,
            'active_downtime': dispatcher_downtime_card_payload(active_downtime),
            **dispatcher_equipment_presence_fields(
                excavator.id,
                open_shift_by_equipment_id,
            ),
        })

    excavator_tiles = []
    for index, excavator in enumerate(excavators_list[:12], start=1):
        board_number = dispatcher_garage_number_int(excavator)
        status, label, equipment_state_code = excavator_state_by_id[excavator.id]
        excavator_plan = dispatcher_plan_for_equipment(excavator)
        percent = excavator_plan['css_percent']
        excavator_tiles.append({
            'equipment': excavator,
            'name': equipment_short_name(excavator),
            'complex': dispatcher_complex_label(excavator) if excavator.id in active_excavator_ids else '',
            'complex_label': dispatcher_complex_label(excavator),
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
            **dispatcher_equipment_presence_fields(
                excavator.id,
                open_shift_by_equipment_id,
            ),
        })

    excavator_garage_tiles = []
    inactive_excavator_tiles = sorted(
        [tile for tile in excavator_tiles if tile.get('equipment') and tile['equipment'].id not in active_excavator_ids],
        key=lambda tile: (
            tile.get('board_number') or 9999,
            tile.get('complex_label') or '',
        ),
    )
    for index, tile in enumerate(inactive_excavator_tiles[:12], start=1):
        garage_tile = tile.copy()
        garage_tile['display_name'] = str(tile.get('complex_label') or f'K-{index}')[2:]
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
                'assignment_state_id': row.get('assignment_state_id') or 0,
                'transfer_pending': bool(row.get('transfer_pending')),
                'transfer_source_label': row.get('transfer_source_label') or '',
                'free_bucket_label': row.get('free_bucket_label') or '',
                'free_bucket_expires_at': row.get('free_bucket_expires_at'),
                'has_current_shift': bool(row.get('has_current_shift')),
                'presence_status': row.get('presence_status') or '',
                'presence_label': row.get('presence_label') or '',
            })
        pending_transfer_tile = next((tile for tile in truck_tiles if tile.get('transfer_pending')), None)
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
            'mobile_truck_overflow': max(len(truck_tiles) - 3, 0),
            'mobile_transfer_notice': (
                f'№{pending_transfer_tile.get("name")} из {pending_transfer_tile.get("transfer_source_label")} · до 5 мин'
                if pending_transfer_tile
                else ''
            ),
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
    complex_zones = sorted(
        complex_cards,
        key=lambda card: (
            status_order.get(card['status_key'], 3),
            dispatcher_complex_number_int(card),
            card.get('id') or '',
        ),
    )
    while len(complex_zones) < 9:
        index = len(complex_zones) + 1
        complex_zones.append({
            'id': f'K-{index}',
            'zone_key': f'empty-{index}',
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
                'zone_key': f'mobile-empty-{index}',
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
        status, label, equipment_state_code = truck_state_by_id[truck.id]
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
            'assignment_state_id': (
                assignment_by_truck[truck.id].id
                if truck.id in assignment_by_truck
                else 0
            ),
            'free_bucket_label': free_bucket_marker_by_truck_id.get(truck.id, {}).get('label', ''),
            'free_bucket_expires_at': free_bucket_marker_by_truck_id.get(truck.id, {}).get('expires_at'),
            **dispatcher_equipment_presence_fields(
                truck.id,
                open_shift_by_equipment_id,
            ),
        })
    mobile_truck_garage_tiles = []
    mobile_truck_sort_source = sorted(trucks_list, key=dispatcher_garage_number_int)
    for index, truck in enumerate(mobile_truck_sort_source, start=1):
        if len(mobile_truck_garage_tiles) >= 52:
            break
        if truck.id in assigned_truck_ids:
            continue
        truck_number = dispatcher_truck_garage_number(truck, index)
        if truck_number is None:
            continue
        status, label, equipment_state_code = truck_state_by_id[truck.id]
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
            'assignment_state_id': (
                assignment_by_truck[truck.id].id
                if truck.id in assignment_by_truck
                else 0
            ),
            'free_bucket_label': free_bucket_marker_by_truck_id.get(truck.id, {}).get('label', ''),
            'free_bucket_expires_at': free_bucket_marker_by_truck_id.get(truck.id, {}).get('expires_at'),
            **dispatcher_equipment_presence_fields(
                truck.id,
                open_shift_by_equipment_id,
            ),
        })

    completed_shift_percent = (
        int((completed_tons / DISPATCHER_PLAN_TOTAL_TONS) * 100)
        if DISPATCHER_PLAN_TOTAL_TONS
        else 0
    )
    completed_shift_detail = dispatcher_shift_plan_detail(
        completed_trips=completed_shift_trips,
        active_trips=[],
        completed_use_tonnage=completed_use_tonnage,
        fact_tons=completed_tons,
        plan_tons=DISPATCHER_PLAN_TOTAL_TONS,
        completion_percent=max(0, min(100, completed_shift_percent)),
    )
    active_shift_detail = dispatcher_shift_plan_detail(
        completed_trips=[],
        active_trips=active_shift_trips,
        completed_use_tonnage=completed_use_tonnage,
        fact_tons=active_volume,
        plan_tons=DISPATCHER_PLAN_TOTAL_TONS,
        completion_percent=completion_percent,
    )
    active_truck_equipment_ids = {
        truck.id
        for truck in trucks_list
        if getattr(truck, 'is_active', True)
    }
    report_working_truck_ids = (
        ((accepted_truck_ids | active_trip_truck_ids) & active_truck_equipment_ids)
        - downtime_truck_ids
    )
    reserve_trucks = sum(
        1
        for truck in trucks_list
        if truck.id not in assigned_truck_ids and truck_state_code_by_id[truck.id] == 'free'
    )
    reserve_excavators = sum(
        1
        for excavator in excavators_list
        if (
            excavator.id not in active_excavator_ids
            and excavator_state_code_by_id[excavator.id] == 'garage'
        )
    )
    mobile_shift_report = {
        'completed_trip_count': len(completed_shift_trips),
        'active_trip_count': len(active_shift_trips),
        'completed_fact': format_dispatcher_number(completed_tons),
        'active_fact': format_dispatcher_number(active_volume),
        'unit_label': shift_plan_detail['unit_label'],
        'working_trucks': len(report_working_truck_ids),
        'free_trucks': reserve_trucks,
        'reserve_trucks': reserve_trucks,
        'reserve_excavators': reserve_excavators,
        'reserve_total': reserve_trucks + reserve_excavators,
        'points': completed_shift_detail['points'],
        'active_points': active_shift_detail['points'],
    }

    for tile in excavator_tiles:
        equipment = tile.get('equipment')
        if (
            not equipment
            or not tile.get('card_id')
            or not dispatcher_equipment_card_requested(
                requested_equipment_card_ids,
                tile['card_id'],
            )
        ):
            continue
        downtime = downtime_by_equipment_id.get(equipment.id)
        active_trip = active_trip_by_excavator_id.get(equipment.id)
        latest_trip = latest_trip_by_equipment_id.get(equipment.id)
        details = dispatcher_shift_details(
            open_shift_by_equipment_id.get(equipment.id),
            format_datetime=format_dispatcher_datetime,
        )
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
        equipment_employee, employee_presence_label = dispatcher_employee_for_equipment(
            equipment.id,
            open_shift_by_equipment_id,
            work_assignment_by_equipment_id,
        )
        equipment_cards[str(tile['card_id'])] = build_dispatcher_equipment_card(
            card_id=tile['card_id'],
            equipment=equipment,
            number=tile.get('display_name') or tile.get('name'),
            icon=tile.get('icon'),
            status=tile.get('status'),
            status_label=dispatcher_status_label(tile.get('status'), tile.get('label')),
            zone=tile.get('complex') or 'гараж',
            percent=tile.get('percent', 0),
            employee=equipment_employee,
            employee_presence_label=employee_presence_label,
            shift=open_shift_by_equipment_id.get(equipment.id),
            details=details,
            shift_report=dispatcher_card_shift_report(equipment, 'Экскаватор'),
            plan=tile.get('plan'),
            settings=dispatcher_excavator_settings(
                equipment,
                placement_by_excavator_id.get(equipment.id),
                rock_types=dispatcher_rock_types,
                dump_points=dispatcher_dump_points,
            ),
            downtime=downtime,
        )

    for card in complex_cards:
        if not dispatcher_equipment_card_requested(
            requested_equipment_card_ids,
            card['card_id'],
        ):
            continue
        complex_report = dispatcher_complex_shift_report(card)
        complex_excavator = card.get('excavator')
        details = dispatcher_shift_details(
            open_shift_by_equipment_id.get(complex_excavator.id),
            format_datetime=format_dispatcher_datetime,
        ) + [
            {
                'label': 'Экскаватор',
                'value': ' · '.join(part for part in [
                    card.get('excavator_name'),
                    str(getattr(getattr(card.get('excavator'), 'model', None), 'name', '') or ''),
                ] if part),
            },
            {'label': 'В составе', 'value': ', '.join(complex_report.get('current_trucks') or [])},
        ]
        if card.get('status_key') == 'yellow':
            details.append({'label': 'Требует внимания', 'value': 'Дефицит транспорта — добавить самосвалы'})
        elif card.get('status_key') in {'orange', 'red'}:
            details.append({'label': 'Требует внимания', 'value': card.get('status_label') or 'Комплекс остановлен'})
        elif card.get('status_key') == 'blue':
            details.append({'label': 'Требует внимания', 'value': 'Комплекс назначен без активной операции'})
        equipment_employee, employee_presence_label = dispatcher_employee_for_equipment(
            complex_excavator.id,
            open_shift_by_equipment_id,
            work_assignment_by_equipment_id,
        )
        equipment_cards[str(card['card_id'])] = build_dispatcher_equipment_card(
            card_id=card['card_id'],
            equipment=complex_excavator,
            type_name='Комплекс',
            number=card.get('id'),
            icon=card.get('excavator_icon'),
            status=card.get('status_key'),
            status_label=card.get('status_label'),
            zone=card.get('material'),
            percent=card.get('percent', 0),
            details=details,
            shift_report=dispatcher_report_with_downtimes(
                complex_report,
                complex_excavator,
                open_shift_by_equipment_id.get(complex_excavator.id),
                now=dashboard_now,
            ),
            category='complex',
            plan=card.get('plan'),
            employee=equipment_employee,
            employee_presence_label=employee_presence_label,
            shift=open_shift_by_equipment_id.get(complex_excavator.id),
            settings=dispatcher_excavator_settings(
                complex_excavator,
                placement_by_excavator_id.get(complex_excavator.id),
                rock_types=dispatcher_rock_types,
                dump_points=dispatcher_dump_points,
            ),
            downtime=downtime_by_equipment_id.get(complex_excavator.id),
            include_equipment_metadata=False,
        )

    for complex_card in complex_cards:
        for tile in complex_card.get('active_truck_tiles', []):
            card_id = str(tile.get('card_id') or '')
            if (
                not card_id
                or card_id in equipment_cards
                or not dispatcher_equipment_card_requested(
                    requested_equipment_card_ids,
                    card_id,
                )
            ):
                continue
            equipment = truck_by_id.get(int(card_id)) if card_id.isdigit() else None
            downtime = downtime_by_equipment_id.get(equipment.id) if equipment else None
            if equipment:
                equipment_employee, employee_presence_label = (
                    dispatcher_employee_for_equipment(
                        equipment.id,
                        open_shift_by_equipment_id,
                        work_assignment_by_equipment_id,
                    )
                )
            else:
                equipment_employee = None
                employee_presence_label = 'Сотрудник не назначен'
            status_label = dispatcher_status_label(tile.get('status'), tile.get('label'))
            details = dispatcher_shift_details(
                open_shift_by_equipment_id.get(equipment.id) if equipment else None,
                format_datetime=format_dispatcher_datetime,
            ) + [
                {'label': 'Гаражный N', 'value': tile.get('name')},
                {'label': 'Комплекс', 'value': complex_card.get('id')},
                {'label': 'Состояние', 'value': tile.get('label')},
                {'label': 'Забой', 'value': complex_card.get('current_face')},
                {'label': 'Порода', 'value': complex_card.get('current_rock')},
                {'label': 'Разгрузки', 'value': ', '.join(point.get('name') for point in complex_card.get('unload_points', []) if point.get('name'))},
            ]
            if tile.get('free_bucket_label'):
                details.append({'label': 'Временная работа', 'value': tile['free_bucket_label']})
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
                employee=equipment_employee,
                shift=open_shift_by_equipment_id.get(equipment.id) if equipment else None,
                manual_trip=dispatcher_manual_trip_payload(
                    equipment,
                    excavator=complex_card.get('excavator'),
                    placement=placement_by_excavator_id.get(getattr(complex_card.get('excavator'), 'id', None)),
                    truck_shift=open_shift_by_equipment_id.get(equipment.id),
                    rock_types=dispatcher_rock_types,
                    dump_points=dispatcher_dump_points,
                ) if equipment else None,
                employee_presence_label=employee_presence_label,
                details=details,
                shift_report=dispatcher_card_shift_report(equipment, 'Самосвал'),
                plan=tile.get('plan'),
                downtime=downtime,
            )

    for tile in truck_garage_tiles + [
        mobile_tile
        for mobile_tile in mobile_truck_garage_tiles
        if mobile_tile.get('card_id') and str(mobile_tile.get('card_id')) not in equipment_cards
    ]:
        if not dispatcher_equipment_card_requested(
            requested_equipment_card_ids,
            tile.get('card_id'),
        ):
            continue
        equipment = tile.get('equipment')
        status_label = dispatcher_status_label(tile.get('status'), tile.get('label'))
        details = dispatcher_plan_details(tile.get('plan'))
        if equipment:
            downtime = downtime_by_equipment_id.get(equipment.id)
            active_trip = active_trip_by_truck_id.get(equipment.id)
            assignment = assignment_by_truck_id.get(equipment.id)
            latest_trip = latest_trip_by_equipment_id.get(equipment.id)
            details = dispatcher_shift_details(
                open_shift_by_equipment_id.get(equipment.id),
                format_datetime=format_dispatcher_datetime,
            ) + details
            if assignment:
                details.extend([
                    {'label': 'Назначение', 'value': 'принято' if assignment.status == AssignmentStatus.ACCEPTED else 'ожидает'},
                    {'label': 'Экскаватор', 'value': equipment_short_name(assignment.excavator)},
                    {'label': 'Назначен', 'value': format_dispatcher_datetime(assignment.assigned_at)},
                ])
            if tile.get('free_bucket_label'):
                details.append({'label': 'Временная работа', 'value': tile['free_bucket_label']})
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
            equipment_employee, employee_presence_label = dispatcher_employee_for_equipment(
                equipment.id,
                open_shift_by_equipment_id,
                work_assignment_by_equipment_id,
            )
            card = build_dispatcher_equipment_card(
                card_id=tile['card_id'],
                equipment=equipment,
                number=tile.get('name'),
                icon=tile.get('icon'),
                status=tile.get('status'),
                status_label=status_label,
                zone='гараж',
                percent=tile.get('percent', 0),
                employee=equipment_employee,
                employee_presence_label=employee_presence_label,
                shift=open_shift_by_equipment_id.get(equipment.id),
                manual_trip=dispatcher_manual_trip_payload(
                    equipment,
                    excavator=getattr(assignment, 'excavator', None),
                    placement=placement_by_excavator_id.get(getattr(assignment, 'excavator_id', None)),
                    truck_shift=open_shift_by_equipment_id.get(equipment.id),
                    rock_types=dispatcher_rock_types,
                    dump_points=dispatcher_dump_points,
                ),
                details=details,
                shift_report=dispatcher_card_shift_report(equipment, 'Самосвал'),
                plan=tile.get('plan'),
                downtime=downtime,
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
    if stale_equipment_shifts:
        action_items.append({
            'priority': 3,
            'status': 'warning',
            'title': f'Незакрытые старые смены: {len(stale_equipment_shifts)}',
            'meta': 'Не участвуют в текущем плане и статусе связи',
            'action': 'закрыть служебно с причиной',
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

    production_context = production_shift_context()
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
        'completion_percent': completion_percent,
        'shift_plan_detail': shift_plan_detail,
        'mobile_shift_report': mobile_shift_report,
        'current_equipment_shift_count': len(open_shift_by_equipment_id),
        'stale_equipment_shift_count': len(stale_equipment_shifts),
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
        'current_time': production_context.local_datetime.strftime('%H:%M'),
        'current_date': production_context.production_date.strftime('%d.%m.%Y'),
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


def lock_dispatcher_mutation_access(request, access):
    """Preserve the views patch seam while delegating the guard implementation."""
    return _lock_dispatcher_mutation_access(
        request,
        access,
        role_session_state_getter=role_session_state,
    )


@require_POST
@transaction.atomic
def dispatcher_move_excavator_view(request):
    return _execute_dispatcher_move_excavator(
        request,
        lock_mutation_access=lock_dispatcher_mutation_access,
        action_logger=log_dispatcher_action,
        equipment_label=equipment_short_name,
    )


@require_POST
@transaction.atomic
def dispatcher_assign_truck_view(request):
    return _execute_dispatcher_assign_truck(
        request,
        lock_mutation_access=lock_dispatcher_mutation_access,
        action_logger=log_dispatcher_action,
        equipment_label=equipment_short_name,
    )


def excavator_access_from_request(request, *, require_active_role=True):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return None
    access = (
        EmployeeAccess.objects
        .select_related('employee', 'employee__contractor_organization', 'role')
        .filter(id=access_id, is_active=True)
        .first()
    )
    if (
        not access
        or access.role.code != 'excavator_operator'
        or (
            require_active_role
            and not role_session_state(request, access)['is_active']
        )
    ):
        return None
    return access


def lock_excavator_mutation_access(request, access):
    """Serialize role activation and re-check a fresh access generation."""
    Employee.objects.select_for_update().get(pk=access.employee_id)
    locked_access = (
        EmployeeAccess.objects
        .select_for_update(of=('self',))
        .select_related('employee', 'employee__contractor_organization', 'role')
        .filter(
            id=access.id,
            employee_id=access.employee_id,
            is_active=True,
        )
        .first()
    )
    if (
        not locked_access
        or locked_access.role.code != 'excavator_operator'
        or not role_session_state(request, locked_access)['is_active']
    ):
        return None
    return locked_access


def get_excavator_open_shift(employee):
    return (
        EmployeeShift.objects
        .filter(employee=employee, closed_at__isnull=True)
        .filter(equipment__equipment_type__name='Экскаватор')
        .filter(
            Q(workplace_code='excavator_operator')
            | Q(workplace_code='')
        )
        .select_related('equipment', 'equipment__equipment_type')
        .order_by('-opened_at')
        .first()
    )


def restrict_excavator_trip_form(form, current_excavator, current_shift=None):
    if current_excavator and current_shift:
        form.fields['assignment'].queryset = excavator_load_assignment_queryset(
            current_shift
        )
    elif current_excavator:
        form.fields['assignment'].queryset = (
            HaulAssignment.objects
            .filter(
                excavator=current_excavator,
                status=AssignmentStatus.ACCEPTED,
                ended_at__isnull=True,
            )
            .select_related('truck', 'truck__model', 'excavator')
            .order_by('truck__garage_number', '-assigned_at', '-id')
        )
    else:
        form.fields['assignment'].queryset = form.fields['assignment'].queryset.none()

    rock_queryset = form.fields['rock_type'].queryset.filter(
        name__in=CANONICAL_ROCK_NAMES,
        density__isnull=False,
        loosening_factor__isnull=False,
    )
    assigned_model_rows = list(
        form.fields['assignment'].queryset
        .exclude(truck__model_id__isnull=True)
        .values_list('truck__model_id', 'truck__model__body_volume_m3')
        .distinct()
    )
    for model_id, body_volume_m3 in assigned_model_rows:
        if body_volume_m3:
            continue
        supported_rock_ids = TruckCapacityRule.objects.filter(
            equipment_model_id=model_id,
        ).values('rock_type_id')
        rock_queryset = rock_queryset.filter(pk__in=supported_rock_ids)
    form.fields['rock_type'].queryset = rock_queryset.order_by('name')
    return form


EXCAVATOR_TRUCK_LOAD_BLOCK_LABELS = {
    'driver_offline': 'Водитель без связи — удерживайте самосвал для ручной отправки',
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
            employee__is_active=True,
            employee_id__in=(
                EmployeeAccess.objects
                .filter(role__code='driver', is_active=True)
                .exclude(status=EmployeeAccess.Status.DEACTIVATED)
                .values('employee_id')
            ),
        )
        .filter(
            Q(role__code='driver')
            | Q(role__isnull=True)
        )
        .exists()
    )


def excavator_truck_load_block(
    assignment,
    *,
    current_excavator=None,
    active_trip=None,
    active_downtime=None,
    post_unload_cooldown=None,
    has_open_truck_shift=None,
    has_driver_assignment=None,
    manual_control=False,
    participation=None,
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
    participation = participation or truck_driver_participation([truck.pk])[truck.pk]
    manual_control = bool(manual_control and manual_loading_enabled() and participation['passive'])
    replace_trip = may_replace_open_trip(active_trip, participation) and (
        manual_control or not participation['passive']
    )
    if active_trip and not replace_trip:
        return excavator_truck_load_block_payload('active_trip')
    if active_downtime is None:
        active_downtime = (
            DowntimeEvent.objects
            .filter(equipment=truck, ended_at__isnull=True)
            .order_by('-started_at', '-id')
            .first()
        )
    if active_downtime and not truck_waiting_loading_downtime(active_downtime):
        return excavator_truck_load_block_payload('active_downtime')
    if post_unload_cooldown is None:
        post_unload_cooldown = truck_post_unload_cooldown(truck)
    if post_unload_cooldown and not manual_control and not replace_trip:
        return post_unload_cooldown
    if has_open_truck_shift is None:
        has_open_truck_shift = EmployeeShift.objects.filter(
            equipment=truck,
            closed_at__isnull=True,
        ).exists()
    if not has_open_truck_shift and not manual_control:
        if has_driver_assignment is None:
            has_driver_assignment = excavator_truck_has_driver_assignment(truck)
        if has_driver_assignment:
            return excavator_truck_load_block_payload('driver_shift_not_started')
        return excavator_truck_load_block_payload('no_driver')
    if manual_loading_enabled() and participation['passive'] and not manual_control:
        return excavator_truck_load_block_payload('driver_offline')
    return None


def excavator_truck_load_block_reason(
    assignment,
    *,
    current_excavator=None,
    active_trip=None,
    active_downtime=None,
    post_unload_cooldown=None,
    has_open_truck_shift=None,
    has_driver_assignment=None,
):
    block = excavator_truck_load_block(
        assignment,
        current_excavator=current_excavator,
        active_trip=active_trip,
        active_downtime=active_downtime,
        post_unload_cooldown=post_unload_cooldown,
        has_open_truck_shift=has_open_truck_shift,
        has_driver_assignment=has_driver_assignment,
    )
    return block['label'] if block else ''


EXCAVATOR_WORK_SETTINGS_SESSION_KEY = 'excavator_work_settings'
EXCAVATOR_AUTO_DOWNTIME_TRANSFER = 'Перегон экскаватора'
EXCAVATOR_AUTO_DOWNTIME_WAITING_TRUCKS = 'Ожидание самосвалов'
EXCAVATOR_AUTO_DOWNTIME_COMMENT = 'Автоматически по производственному событию'


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


def excavator_auto_downtime_reason(excavator, reason_name):
    if not excavator:
        return None
    return (
        DowntimeReason.for_workplace('excavator_operator', excavator.equipment_type)
        .filter(name=reason_name)
        .first()
    )


def start_excavator_auto_downtime(excavator, employee, reason_name, *, replace_active=False):
    reason = excavator_auto_downtime_reason(excavator, reason_name)
    if not reason:
        return None
    # Простой без смены бессмыслен: некому его учитывать и некому закрыть.
    if not EmployeeShift.objects.filter(equipment=excavator, closed_at__isnull=True).exists():
        return None
    with transaction.atomic():
        excavator = Equipment.objects.select_for_update().get(pk=excavator.pk)
        active_events = list(
            DowntimeEvent.objects
            .select_for_update()
            .filter(equipment=excavator, ended_at__isnull=True)
            .select_related('reason')
            .order_by('-started_at', '-id')
        )
        for event in active_events:
            if event.reason_id == reason.id:
                return event
        if active_events and not replace_active:
            return None
        now = timezone.now()
        for event in active_events:
            event.ended_at = now
            event.save(update_fields=['ended_at'])
        return DowntimeEvent.objects.create(
            equipment=excavator,
            employee=employee,
            reason=reason,
            started_at=now,
            comment=EXCAVATOR_AUTO_DOWNTIME_COMMENT,
        )


def close_excavator_auto_downtime(excavator, reason_name):
    if not excavator:
        return 0
    now = timezone.now()
    closed = 0
    with transaction.atomic():
        excavator = Equipment.objects.select_for_update().get(pk=excavator.pk)
        events = list(
            DowntimeEvent.objects
            .select_for_update()
            .filter(
                equipment=excavator,
                reason__name=reason_name,
                ended_at__isnull=True,
                comment=EXCAVATOR_AUTO_DOWNTIME_COMMENT,
            )
        )
        for event in events:
            event.ended_at = now
            event.save(update_fields=['ended_at'])
            closed += 1
    return closed


def close_excavator_open_downtimes(excavator):
    """Close every current excavator downtime at one production boundary."""
    if not excavator:
        return 0
    with transaction.atomic():
        excavator = Equipment.objects.select_for_update().get(pk=excavator.pk)
        events = list(
            DowntimeEvent.objects
            .select_for_update()
            .filter(equipment=excavator, ended_at__isnull=True)
            .order_by('id')
        )
        if not events:
            return 0
        ended_at = timezone.now()
        for event in events:
            event.ended_at = ended_at
            event.save(update_fields=['ended_at'])
        return len(events)


def excavator_assigned_truck_counts(excavator):
    """Сколько самосвалов назначено экскаватору и сколько из них можно грузить.

    Возвращает (всего назначено, доступно для погрузки, есть выключенный).
    Выключенной считается только неактивная единица техники. Рейс на
    разгрузке сюда не относится: это штатное состояние, при котором
    ожидание самосвалов как раз должно включаться после отправки всех
    рабочих машин.
    """
    if not excavator:
        return 0, 0, False
    assignments = list(
        HaulAssignment.objects
        .filter(
            excavator=excavator,
            ended_at__isnull=True,
            status=AssignmentStatus.ACCEPTED,
        )
        .select_related('truck', 'truck__equipment_type')
    )
    has_inactive_assigned_truck = any(
        not getattr(assignment.truck, 'is_active', True)
        for assignment in assignments
    )
    loadable = sum(
        1
        for assignment in assignments
        if not excavator_truck_load_block(assignment, current_excavator=excavator, manual_control=True)
    )
    return len(assignments), loadable, has_inactive_assigned_truck


def reconcile_excavator_waiting_for_trucks(excavator, employee=None, *, start_when_empty=False):
    if not excavator:
        return None
    with transaction.atomic():
        excavator = Equipment.objects.select_for_update().get(pk=excavator.pk)
        assigned_total, loadable, has_inactive_assigned_truck = excavator_assigned_truck_counts(excavator)
        if loadable:
            close_excavator_auto_downtime(excavator, EXCAVATOR_AUTO_DOWNTIME_WAITING_TRUCKS)
            return None
        # Ждать самосвалы можно только тогда, когда они назначены и все уже
        # отгружены. Если экскаватору не назначено ни одного самосвала, ждать
        # ему нечего — это не простой по ожиданию, и открывать его нельзя.
        if assigned_total == 0:
            close_excavator_auto_downtime(excavator, EXCAVATOR_AUTO_DOWNTIME_WAITING_TRUCKS)
            return None
        # Автоматическое «Ожидание самосвалов» отражает только ситуацию,
        # когда весь назначенный парк включён, но уже находится в рейсах.
        # Выключенный самосвал не должен сам запускать этот простой.
        if has_inactive_assigned_truck:
            close_excavator_auto_downtime(excavator, EXCAVATOR_AUTO_DOWNTIME_WAITING_TRUCKS)
            return None
        if start_when_empty:
            return start_excavator_auto_downtime(
                excavator,
                employee,
                EXCAVATOR_AUTO_DOWNTIME_WAITING_TRUCKS,
            )
        return None


def excavator_work_context_changed(
    placement,
    previous_session_settings,
    *,
    rock_type,
    dump_points,
    loading_horizon,
    loading_block,
):
    previous_session_settings = previous_session_settings or {}
    previous_dump_ids = previous_session_settings.get('dump_point_ids')
    if not isinstance(previous_dump_ids, list):
        previous_dump_ids = [placement.work_dump_point_id] if placement and placement.work_dump_point_id else []
    previous_rock_id = previous_session_settings.get('rock_type_id')
    if previous_rock_id is None and placement:
        previous_rock_id = placement.work_rock_type_id
    # Координаты забоя принадлежат постоянному размещению экскаватора. Данные
    # вкладки в session могут устареть, поэтому для горизонта и блока источником
    # истины остаётся ExcavatorPlacement.
    previous_horizon = placement.loading_horizon if placement else ''
    previous_block = placement.loading_block if placement else ''
    return any((
        str(previous_rock_id or '') != str(rock_type.id),
        [str(value) for value in previous_dump_ids] != [str(point.id) for point in dump_points],
        canonical_excavator_face_position(previous_horizon)
        != canonical_excavator_face_position(loading_horizon),
        canonical_excavator_face_position(previous_block)
        != canonical_excavator_face_position(loading_block),
    ))


_EXCAVATOR_DISTANCE_UNSET = object()
_EXCAVATOR_DESTINATIONS_UNSET = object()


def parse_excavator_destinations(payload, dump_point_queryset):
    dump_by_id = {str(point.id): point for point in dump_point_queryset}
    raw_destinations = payload.get('destinations')
    if not isinstance(raw_destinations, list):
        raw_ids = payload.get('dump_point_ids')
        if not isinstance(raw_ids, list):
            raw_ids = [payload.get('dump_point_id') or payload.get('dump_point')]
        raw_distances = payload.get('dump_point_distances')
        if not isinstance(raw_distances, dict):
            raw_distances = {}
        raw_destinations = [
            {
                'dump_point_id': raw_id,
                'transport_distance_km': (
                    raw_distances.get(str(raw_id), '')
                    if raw_distances
                    else payload.get('transport_distance_km', '')
                ),
            }
            for raw_id in raw_ids
        ]

    destinations = []
    seen_dump_ids = set()
    for raw_row in raw_destinations:
        if not isinstance(raw_row, dict):
            continue
        dump_id = str(raw_row.get('dump_point_id') or raw_row.get('id') or '')
        if dump_id not in dump_by_id or dump_id in seen_dump_ids:
            continue
        distance = parse_excavator_shift_decimal(
            raw_row.get('transport_distance_km'),
            'Плечо',
        )
        if distance is not None and distance > Decimal('999999.99'):
            raise ValueError('invalid_transport_distance')
        destinations.append({
            'dump_point': dump_by_id[dump_id],
            'transport_distance_km': distance,
        })
        seen_dump_ids.add(dump_id)
    return destinations


def parse_excavator_operator_destinations(payload, dump_point_queryset, placement):
    """Select destinations while preserving dispatcher-owned haul distances."""
    dump_by_id = {str(point.id): point for point in dump_point_queryset}
    raw_ids = payload.get('dump_point_ids')
    if not isinstance(raw_ids, list):
        raw_destinations = payload.get('destinations')
        if isinstance(raw_destinations, list):
            raw_ids = [
                row.get('dump_point_id') or row.get('id')
                for row in raw_destinations
                if isinstance(row, dict)
            ]
        else:
            raw_ids = [payload.get('dump_point_id') or payload.get('dump_point')]

    distance_by_dump_id = {
        row['dump_point'].id: row['transport_distance_km']
        for row in excavator_configured_destinations(placement)
    }
    if (
        placement
        and placement.work_dump_point_id
        and placement.work_dump_point_id not in distance_by_dump_id
    ):
        distance_by_dump_id[placement.work_dump_point_id] = placement.transport_distance_km

    destinations = []
    seen_dump_ids = set()
    for raw_id in raw_ids:
        dump_id = str(raw_id or '')
        if dump_id not in dump_by_id or dump_id in seen_dump_ids:
            continue
        dump_point = dump_by_id[dump_id]
        destinations.append({
            'dump_point': dump_point,
            'transport_distance_km': distance_by_dump_id.get(dump_point.id),
        })
        seen_dump_ids.add(dump_id)
    return destinations


def save_excavator_work_context(
    *,
    current_excavator,
    actor,
    rock_type,
    dump_points,
    loading_horizon,
    loading_block,
    transport_distance_km=_EXCAVATOR_DISTANCE_UNSET,
    destination_settings=_EXCAVATOR_DESTINATIONS_UNSET,
):
    if not current_excavator:
        return None
    placement, _ = ExcavatorPlacement.objects.get_or_create(excavator=current_excavator)
    configured_destinations_exist = bool(
        placement.pk and placement.dump_point_settings.exists()
    )
    if destination_settings is not _EXCAVATOR_DESTINATIONS_UNSET:
        dump_points = [row['dump_point'] for row in destination_settings]
        placement.work_dump_point = dump_points[0] if dump_points else None
        placement.transport_distance_km = (
            destination_settings[0]['transport_distance_km']
            if destination_settings
            else None
        )
    elif not configured_destinations_exist:
        placement.work_dump_point = dump_points[0] if dump_points else None
        if transport_distance_km is not _EXCAVATOR_DISTANCE_UNSET:
            placement.transport_distance_km = transport_distance_km
    placement.work_rock_type = rock_type
    placement.loading_horizon = loading_horizon
    placement.loading_block = loading_block
    placement.work_context_updated_at = timezone.now()
    placement.changed_by = actor
    update_fields = [
        'work_rock_type',
        'work_dump_point',
        'loading_horizon',
        'loading_block',
        'work_context_updated_at',
        'changed_by',
        'changed_at',
    ]
    if (
        transport_distance_km is not _EXCAVATOR_DISTANCE_UNSET
        or destination_settings is not _EXCAVATOR_DESTINATIONS_UNSET
    ):
        update_fields.append('transport_distance_km')
    placement.save(update_fields=update_fields)
    if destination_settings is not _EXCAVATOR_DESTINATIONS_UNSET:
        selected_dump_ids = [row['dump_point'].id for row in destination_settings]
        placement.dump_point_settings.exclude(dump_point_id__in=selected_dump_ids).delete()
        existing_rows = {
            row.dump_point_id: row
            for row in placement.dump_point_settings.select_for_update()
        }
        for position, row in enumerate(destination_settings):
            setting = existing_rows.get(row['dump_point'].id)
            if setting is None:
                setting = ExcavatorDumpPointSetting(
                    placement=placement,
                    dump_point=row['dump_point'],
                )
            setting.transport_distance_km = row['transport_distance_km']
            setting.position = position
            setting.changed_by = actor
            setting.save()
    return placement


def normalize_excavator_numeric_setting(value, *, max_length=16):
    return re.sub(r'\D+', '', str(value or ''))[:max_length]


def canonical_excavator_face_position(value):
    normalized = normalize_excavator_numeric_setting(value)
    if not normalized:
        return ''
    return normalized.lstrip('0') or '0'


def excavator_face_position_changed(placement, *, loading_horizon, loading_block):
    """Фиксирует только подтверждённый переезд между двумя известными забоями."""
    if not placement:
        return False

    previous_horizon = canonical_excavator_face_position(placement.loading_horizon)
    previous_block = canonical_excavator_face_position(placement.loading_block)
    next_horizon = canonical_excavator_face_position(loading_horizon)
    next_block = canonical_excavator_face_position(loading_block)
    return any((
        bool(previous_horizon and next_horizon and previous_horizon != next_horizon),
        bool(previous_block and next_block and previous_block != next_block),
    ))


def excavator_work_settings_from_session(request, current_excavator, form):
    session_settings = request.session.get(EXCAVATOR_WORK_SETTINGS_SESSION_KEY, {})
    raw_settings = session_settings.get(excavator_work_settings_key(current_excavator), {})
    placement = get_excavator_work_placement(current_excavator)
    rock_choices = list(form.fields['rock_type'].queryset)
    dump_point_choices = list(form.fields['dump_point'].queryset)

    rock_by_id = {str(rock.id): rock for rock in rock_choices}
    dump_by_id = {str(point.id): point for point in dump_point_choices}

    raw_rock_id = str(raw_settings.get('rock_type_id') or '')
    default_rock_id = str(form['rock_type'].value() or '')
    placement_rock_id = str(getattr(placement, 'work_rock_type_id', '') or '')
    persisted_rock_id = placement_rock_id or raw_rock_id
    persisted_rock = rock_by_id.get(persisted_rock_id)
    current_rock = (
        persisted_rock
        or rock_by_id.get(default_rock_id)
        or (rock_choices[0] if rock_choices else None)
    )

    configured_destinations = excavator_configured_destinations(placement)
    configured_dump_ids = [row['dump_point'].id for row in configured_destinations]
    destination_distance_values = {
        str(row['dump_point'].id): (
            format(row['transport_distance_km'], 'f')
            if row['transport_distance_km'] is not None
            else ''
        )
        for row in configured_destinations
    }
    raw_dump_ids = configured_dump_ids or raw_settings.get('dump_point_ids')
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

    persisted_dump_point_ids = configured_dump_ids or [point.id for point in selected_dump_points]

    form_dump_id = str(form['dump_point'].value() or '')
    if not selected_dump_points and form_dump_id in dump_by_id:
        selected_dump_points.append(dump_by_id[form_dump_id])
    if not selected_dump_points and dump_point_choices:
        selected_dump_points.append(dump_point_choices[0])

    face_horizon = normalize_excavator_numeric_setting(
        (getattr(placement, 'loading_horizon', '') if placement else '')
        or raw_settings.get('loading_horizon')
        or form['loading_horizon'].value()
    )
    face_block = normalize_excavator_numeric_setting(
        (getattr(placement, 'loading_block', '') if placement else '')
        or raw_settings.get('loading_block')
        or form['loading_block'].value()
    )

    selected_dump_ids = [point.id for point in selected_dump_points]
    has_placement_settings = bool(
        placement
        and (
            placement.work_context_updated_at
            or placement.work_rock_type_id
            or placement.work_dump_point_id
            or placement.loading_horizon
            or placement.loading_block
            or placement.transport_distance_km is not None
        )
    )
    return {
        # Настройки считаются применёнными только вместе с действующими породой
        # и точкой разгрузки. Если сохранённый справочник изменился, резервные
        # значения остаются черновиком и кнопку можно нажать снова.
        'has_applied_settings': bool(
            persisted_rock
            and persisted_dump_point_ids
            and (raw_settings or has_placement_settings)
        ),
        'rock_choices': rock_choices,
        'dump_point_choices': dump_point_choices,
        'current_rock': current_rock,
        'default_rock': current_rock.id if current_rock else '',
        'selected_dump_points': selected_dump_points,
        'selected_dump_point_ids': selected_dump_ids,
        'persisted_dump_point_ids': persisted_dump_point_ids,
        'destination_distance_values': destination_distance_values,
        'default_dump_point': selected_dump_ids[0] if selected_dump_ids else '',
        'transport_distance_km': (
            placement.transport_distance_km
            if placement and placement.transport_distance_km is not None
            else form['transport_distance_km'].value()
        ),
        'face_horizon': face_horizon,
        'face_block': face_block,
    }


from core.dump_point_names import dump_name_size_class
from core.equipment_numbers import is_plain_number


def build_excavator_dump_cards(
    points,
    *,
    selected_ids=None,
    persisted_ids=None,
    distance_values=None,
    include_all=False,
):
    selected_ids = {str(point_id) for point_id in (selected_ids or [])}
    persisted_ids = {str(point_id) for point_id in (persisted_ids or [])}
    distance_values = distance_values or {}
    cards = []
    for index, point in enumerate(points):
        is_selected = str(point.id) in selected_ids if include_all else True
        if include_all:
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
            'is_persisted': include_all and str(point.id) in persisted_ids,
            'transport_distance_km': distance_values.get(str(point.id), ''),
            'name_size_class': dump_name_size_class(str(point)),
        })
    return cards


def excavator_json_payload(request):
    if request.content_type == 'application/json':
        try:
            return json.loads(request.body.decode('utf-8') or '{}')
        except json.JSONDecodeError:
            return {}
    return request.POST


def finalize_trip_unloaded(trip, *, driver, unloading_shift, occurred_at=None, late_confirmation=False):
    if trip.status not in (*OPEN_TRIP_STATUSES, TripStatus.UNCONTROLLED):
        return False
    volume, tonnage = calculate_trip_volume_and_tonnage(
        trip.truck,
        trip.rock_type,
    )
    if trip.volume_m3 is None or trip.tonnage is None:
        trip.volume_m3 = trip.volume_m3 if trip.volume_m3 is not None else volume
        trip.tonnage = trip.tonnage if trip.tonnage is not None else tonnage
    trip.status = TripStatus.COMPLETED
    trip.driver = driver
    trip.unload_received_at = timezone.now()
    trip.completed_at = occurred_at or (None if late_confirmation else trip.unload_received_at)
    trip.unload_time_source = 'driver_device' if occurred_at else ('unknown' if late_confirmation else 'server_receipt')
    trip.unloading_shift = unloading_shift
    if trip.actual_dump_point_id is None:
        trip.actual_dump_point = trip.dump_point
    if trip.assigned_dump_point_id is None:
        trip.assigned_dump_point = trip.dump_point
    trip.is_carryover = bool(
        trip.is_carryover
        or (
            trip.loading_shift
            and unloading_shift
            and trip.loading_shift.shift_type != unloading_shift.shift_type
        )
    )
    trip.save(update_fields=[
        'volume_m3',
        'tonnage',
        'status',
        'driver',
        'completed_at',
        'unload_received_at',
        'unload_time_source',
        'unloading_shift',
        'assigned_dump_point',
        'actual_dump_point',
        'is_carryover',
    ])
    from trips.free_bucket import close_free_bucket_acceptance_for_trip
    close_free_bucket_acceptance_for_trip(trip, closed_at=trip.completed_at or trip.unload_received_at)
    if not late_confirmation:
        close_truck_unloading_wait_downtimes(
            trip.truck,
            ended_at=trip.completed_at,
        )
    reconcile_excavator_waiting_for_trucks(trip.excavator)
    return True


def trip_loaded_payload(trip, *, client_action_id=''):
    actual_status = trip.status
    refresh_required = actual_status in {
        TripStatus.COMPLETED,
        TripStatus.CANCELLED,
        TripStatus.UNCONTROLLED,
    }
    if actual_status == TripStatus.LOADED_WAITING_UNLOAD:
        status_label = equipment_state_ui(
            get_equipment_state_ui_map(),
            'loaded_waiting_unload',
        )['label']
    else:
        status_label = trip.get_status_display()
    dump_badge_auto_hide_at = (
        free_bucket_dump_card_expires_at(trip)
        or manual_dump_card_expires_at(trip)
    )
    return {
        'ok': True,
        'action': 'truck_loaded',
        'driver_participation_recorded': trip.driver_participation_recorded,
        'driver_control_shift_id': trip.driver_control_shift_id,
        'client_action_id': client_action_id,
        'trip_id': trip.id,
        'truck_id': trip.truck_id,
        'excavator_id': trip.excavator_id,
        'dump_point_id': trip.dump_point_id,
        'dump_point': str(trip.dump_point),
        'assigned_dump_point_id': trip.assigned_dump_point_id or trip.dump_point_id,
        'actual_dump_point_id': trip.actual_dump_point_id or trip.dump_point_id,
        'status': actual_status,
        'status_label': status_label,
        'refresh_required': refresh_required,
        'dump_badge_auto_hide_at': (
            dump_badge_auto_hide_at.isoformat()
            if dump_badge_auto_hide_at is not None
            else ''
        ),
        'version': get_operational_state_version(),
    }


def notify_driver_truck_loaded(trip):
    """Сообщает водителю, что его самосвал загружен и куда ехать.

    Главное в уведомлении — точка разгрузки: именно её водитель ждёт от
    экскаваторщика. Ошибки отправки намеренно проглатываются: уведомление не
    должно ломать саму погрузку.
    """
    from users.webpush import notify_employee

    if trip.driver_participation_recorded and not trip.driver_control_shift_id:
        return
    try:
        driver_shift = trip.driver_control_shift if trip.driver_participation_recorded else (
            EmployeeShift.objects
            .select_related('employee')
            .filter(equipment_id=trip.truck_id, closed_at__isnull=True)
            .filter(
                Q(workplace_code='driver')
                | Q(workplace_code='', equipment__equipment_type__name='Самосвал')
            )
            .order_by('-opened_at')
            .first()
        )
        if not driver_shift or not driver_shift.employee_id:
            return
        dump_point = trip.assigned_dump_point or trip.dump_point
        dump_name = getattr(dump_point, 'name', '') or 'не указана'
        rock_name = getattr(trip.rock_type, 'name', '') or ''
        body = f'Точка разгрузки: {dump_name}'
        if rock_name:
            body = f'{body} · {rock_name}'
        notify_employee(
            driver_shift.employee,
            title='Самосвал загружен',
            body=body,
            url='/driver/',
            tag='driver-trip-loaded',
            kind='driver_trip_loaded',
        )
    except Exception:
        logger.exception('Не удалось отправить водителю уведомление о погрузке.')


@require_POST
def excavator_truck_loaded_view(request):
    access = excavator_access_from_request(request, require_active_role=False)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к экрану Экскаваторщика.'}, status=403)
    payload = excavator_json_payload(request)
    client_action_id = str(payload.get('client_action_id') or '').strip()
    if not client_action_id or len(client_action_id) > 128:
        return JsonResponse({'ok': False, 'error': 'Не передан client_action_id.'}, status=400)

    with transaction.atomic():
        lock_idempotency_key('truck_loaded', client_action_id)
        existing_action = (
            TripClientAction.objects
            .select_related('trip', 'trip__dump_point')
            .filter(action_type='truck_loaded', client_action_id=client_action_id)
            .first()
        )
        if existing_action and existing_action.actor_id != access.employee_id:
            return JsonResponse({'ok': False, 'error': 'Действие принадлежит другому участнику.'}, status=409)
        if existing_action:
            response_payload = trip_loaded_payload(existing_action.trip, client_action_id=client_action_id)
            response_payload['deduplicated'] = True
            return JsonResponse(response_payload)

        access = lock_excavator_mutation_access(request, access)
        if not access:
            return JsonResponse(
                {'ok': False, 'error': 'Роль неактивна — доступен только просмотр', 'code': 'inactive_role'},
                status=409,
            )
        open_shift = (
            EmployeeShift.objects
            .select_for_update(of=('self',))
            .filter(employee=access.employee, closed_at__isnull=True)
            .filter(
                Q(workplace_code='excavator_operator')
                | Q(workplace_code='', equipment__equipment_type__name='Экскаватор')
            )
            .select_related('equipment', 'equipment__equipment_type')
            .order_by('-opened_at')
            .first()
        )
        current_excavator = open_shift.equipment if open_shift else None
        if not current_excavator:
            return JsonResponse({'ok': False, 'error': 'Сначала нужно открыть смену на экскаваторе.'}, status=409)
        lock_production_state()

        try:
            assignment_id = int(payload.get('assignment_id') or 0)
            truck_id = int(payload.get('truck_id') or 0)
            excavator_id = int(payload.get('excavator_id') or current_excavator.id)
            dump_point_id = int(payload.get('dump_point_id') or 0)
            rock_type_id = int(payload.get('rock_type') or payload.get('rock_type_id') or 0)
        except (TypeError, ValueError):
            return JsonResponse({'ok': False, 'error': 'Некорректные параметры действия.'}, status=400)

        if excavator_id != current_excavator.id:
            return JsonResponse({'ok': False, 'error': 'Экскаватор в действии не совпадает с текущей сменой.'}, status=409)

        try:
            current_excavator, locked_truck = lock_trip_participant_equipment(
                excavator_id=current_excavator.pk,
                truck_id=truck_id,
            )
        except ValidationError as error:
            return JsonResponse(
                {
                    'ok': False,
                    'error': '; '.join(error.messages),
                    'code': 'trip_equipment_unavailable',
                },
                status=409,
            )
        from trips.free_bucket import active_free_bucket_acceptance_for_truck
        if active_free_bucket_acceptance_for_truck(locked_truck, for_update=True):
            return JsonResponse({
                'ok': False,
                'error': 'Самосвал принят под свободный ковш; погрузка возможна только через этот временный приём.',
                'code': 'free_bucket_acceptance_required',
            }, status=409)
        open_shift.equipment = current_excavator
        assignment, handoff = resolve_excavator_load_authority(
            truck_id=locked_truck.id,
            excavator_id=current_excavator.id,
            source_shift=open_shift,
            requested_assignment_id=assignment_id or None,
        )
        if not assignment:
            return JsonResponse({
                'ok': False,
                'error': 'Назначение уже изменилось или право завершить погрузку использовано.',
            }, status=409)
        assignment.truck = locked_truck
        assignment.excavator = current_excavator
        open_trip = (
            Trip.objects
            .select_for_update()
            .filter(truck_id=locked_truck.id, status__in=OPEN_TRIP_STATUSES)
            .first()
        )
        participation = truck_driver_participation([locked_truck.pk])[locked_truck.pk]
        manual_control = payload.get('manual_control') is True
        if open_trip and may_replace_open_trip(open_trip, participation):
            if str(payload.get('expected_open_trip_id') or '') != str(open_trip.pk):
                return JsonResponse({'ok': False, 'error': 'Рейс изменился. Обновите экран.', 'code': 'trip_changed'}, status=409)
        load_block = excavator_truck_load_block(
            assignment,
            current_excavator=current_excavator,
            active_trip=open_trip or False,
            manual_control=manual_control,
            participation=participation,
        )
        if load_block:
            return JsonResponse({
                'ok': False,
                'error': load_block['label'],
                'load_block_reason_code': load_block['code'],
                'load_block_reason_label': load_block['label'],
            }, status=409)

        if manual_control and (not current_excavator.is_active or DowntimeEvent.objects.filter(
            equipment=current_excavator, ended_at__isnull=True, reason__is_critical=True,
        ).exists()):
            return JsonResponse({'ok': False, 'error': 'Экскаватор недоступен для работы.'}, status=409)

        dump_point = get_object_or_404(DumpPoint.objects.filter(is_active=True), id=dump_point_id)
        rock_type = get_object_or_404(RockType.objects.filter(is_active=True), id=rock_type_id)
        loading_horizon = normalize_excavator_numeric_setting(payload.get('loading_horizon'))
        loading_block = normalize_excavator_numeric_setting(payload.get('loading_block'))
        transport_distance_km = payload.get('transport_distance_km')
        if transport_distance_km in {None, ''}:
            transport_distance_km = (
                ExcavatorDumpPointSetting.objects
                .filter(
                    placement__excavator=current_excavator,
                    dump_point=dump_point,
                )
                .values_list('transport_distance_km', flat=True)
                .first()
            )
        try:
            trip = create_loaded_waiting_unload_trip(
                assignment=assignment,
                excavator_operator=access.employee,
                loading_shift=open_shift,
                rock_type=rock_type,
                dump_point=dump_point,
                planned_volume_m3=payload.get('planned_volume_m3') or None,
                loading_horizon=loading_horizon,
                loading_block=loading_block,
                transport_distance_km=(
                    transport_distance_km
                    if transport_distance_km not in {None, ''}
                    else None
                ),
                downtime_text=payload.get('downtime_text'),
                note=payload.get('note'),
                participation=participation,
                supersede_trip=open_trip,
            )
        except ValidationError as error:
            return JsonResponse(
                {
                    'ok': False,
                    'error': '; '.join(error.messages),
                    'code': 'trip_measurements_unresolved',
                },
                status=409,
            )
        save_excavator_work_context(
            current_excavator=current_excavator,
            actor=access.employee,
            rock_type=rock_type,
            dump_points=[dump_point],
            loading_horizon=loading_horizon,
            loading_block=loading_block,
            transport_distance_km=trip.transport_distance_km,
        )
        TripClientAction.objects.create(
            action_type='truck_loaded',
            client_action_id=client_action_id,
            trip=trip,
            actor=access.employee,
        )
        if open_trip:
            TripClientAction.objects.create(action_type='truck_load_supersede', client_action_id=client_action_id,
                                            trip=open_trip, actor=access.employee)
        close_truck_waiting_loading_downtimes(assignment.truck)
        close_excavator_open_downtimes(current_excavator)
        reconcile_excavator_waiting_for_trucks(
            current_excavator,
            access.employee,
            start_when_empty=True,
        )
        state = bump_operational_state(
            'Trip:truck_loaded',
            event_type='trip_changed',
            object_type='Trip',
            object_id=trip.id,
            payload={
                'action': 'truck_loaded',
                'driver_participation_recorded': trip.driver_participation_recorded,
                'driver_control_shift_id': trip.driver_control_shift_id,
                'trip_id': trip.id,
                'truck_id': trip.truck_id,
                'excavator_id': trip.excavator_id,
                'excavator_ids': sorted({
                    trip.excavator_id,
                    *(
                        [handoff.target_assignment.excavator_id]
                        if handoff and handoff.target_assignment.excavator_id
                        else []
                    ),
                }),
                'dump_point_id': trip.dump_point_id,
                'assigned_dump_point_id': trip.assigned_dump_point_id,
                'actual_dump_point_id': trip.actual_dump_point_id,
                'dump_point_name': str(trip.assigned_dump_point or trip.dump_point),
                'status': TripStatus.LOADED_WAITING_UNLOAD,
            },
        )

    notify_driver_truck_loaded(trip)
    response_payload = trip_loaded_payload(trip, client_action_id=client_action_id)
    response_payload['version'] = state.version
    response_payload['downtime_status'] = excavator_downtime_status_payload(current_excavator, open_shift)
    return JsonResponse(response_payload)


@require_POST
def excavator_truck_loaded_cancel_view(request):
    access = excavator_access_from_request(request, require_active_role=False)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к экрану Экскаваторщика.'}, status=403)
    payload = excavator_json_payload(request)
    client_action_id = str(payload.get('client_action_id') or '').strip()
    if not client_action_id:
        return JsonResponse({'ok': False, 'error': 'Не передан client_action_id.'}, status=400)

    with transaction.atomic():
        lock_idempotency_key('truck_loaded_cancel', client_action_id)
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

        access = lock_excavator_mutation_access(request, access)
        if not access:
            return JsonResponse(
                {'ok': False, 'error': 'Роль неактивна — доступен только просмотр', 'code': 'inactive_role'},
                status=409,
            )
        open_shift = (
            EmployeeShift.objects
            .select_for_update(of=('self',))
            .filter(employee=access.employee, closed_at__isnull=True)
            .filter(
                Q(workplace_code='excavator_operator')
                | Q(workplace_code='', equipment__equipment_type__name='Экскаватор')
            )
            .select_related('equipment', 'equipment__equipment_type')
            .order_by('-opened_at')
            .first()
        )
        current_excavator = open_shift.equipment if open_shift else None
        if not current_excavator:
            return JsonResponse({'ok': False, 'error': 'Сначала нужно открыть смену на экскаваторе.'}, status=409)

        try:
            trip_id = int(payload.get('trip_id') or 0)
            truck_id = int(payload.get('truck_id') or 0)
            dump_point_id = int(payload.get('dump_point_id') or 0)
        except (TypeError, ValueError):
            return JsonResponse({'ok': False, 'error': 'Некорректные параметры действия.'}, status=400)

        lock_production_state()
        Equipment.objects.select_for_update().filter(pk=truck_id).first()
        trip = (
            Trip.objects
            .select_for_update(of=('self',))
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
            return JsonResponse({'ok': False, 'error': 'Незакрытый рейс для отмены не найден.'}, status=409)
        current_dump_point_id = trip.assigned_dump_point_id or trip.actual_dump_point_id or trip.dump_point_id
        if dump_point_id and dump_point_id != current_dump_point_id:
            return JsonResponse({'ok': False, 'error': 'Точка разгрузки в действии не совпадает с рейсом.'}, status=409)

        trip.status = TripStatus.CANCELLED
        trip.cancelled_at = timezone.now()
        trip.save(update_fields=['status', 'cancelled_at'])
        from trips.free_bucket import close_free_bucket_acceptance_for_trip
        close_free_bucket_acceptance_for_trip(trip, closed_at=trip.cancelled_at)
        previous = Trip.objects.select_for_update().filter(superseded_by=trip, status=TripStatus.UNCONTROLLED).first()
        if previous:
            previous.status = TripStatus.LOADED_WAITING_UNLOAD
            previous.operationally_closed_at = None
            previous.closure_recorded_by = None
            previous.superseded_by = None
            previous.save(update_fields=['status', 'operationally_closed_at', 'closure_recorded_by', 'superseded_by'])
        reconcile_excavator_waiting_for_trucks(current_excavator)
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
@transaction.atomic
def excavator_work_settings_view(request):
    access = excavator_access_from_request(request, require_active_role=False)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к экрану Экскаваторщика.'}, status=403)
    access = lock_excavator_mutation_access(request, access)
    if not access:
        return JsonResponse(
            {
                'ok': False,
                'error': 'Роль неактивна — доступен только просмотр',
                'code': 'inactive_role',
            },
            status=409,
        )
    open_shift = (
        EmployeeShift.objects
        .select_for_update(of=('self',))
        .filter(employee=access.employee, closed_at__isnull=True)
        .filter(
            Q(workplace_code='excavator_operator')
            | Q(
                workplace_code='',
                equipment__equipment_type__name='Экскаватор',
            )
        )
        .select_related('equipment', 'equipment__equipment_type')
        .order_by('-opened_at')
        .first()
    )
    current_excavator = open_shift.equipment if open_shift else None
    if not current_excavator:
        return JsonResponse({'ok': False, 'error': 'Сначала нужно открыть смену на экскаваторе.'}, status=409)

    current_excavator = (
        Equipment.objects.select_for_update(of=('self',))
        .select_related('equipment_type')
        .get(pk=current_excavator.pk)
    )
    open_shift.equipment = current_excavator

    payload = excavator_json_payload(request)
    form = restrict_excavator_trip_form(
        TripCreateForm(excavator_operator=access.employee),
        current_excavator,
        open_shift,
    )
    rock_queryset = form.fields['rock_type'].queryset
    dump_point_queryset = form.fields['dump_point'].queryset

    rock_type_id = payload.get('rock_type_id') or payload.get('rock_type')
    rock_type = rock_queryset.filter(id=rock_type_id).first()
    if not rock_type:
        if RockType.objects.filter(id=rock_type_id, is_active=True).exists():
            return JsonResponse(
                {
                    'ok': False,
                    'error': (
                        'Для выбранной породы не настроены плотность, коэффициент '
                        'разрыхления или кубатура назначенных самосвалов.'
                    ),
                    'code': 'rock_reference_incomplete',
                },
                status=409,
            )
        return JsonResponse({'ok': False, 'error': 'Порода недоступна в справочнике.'}, status=400)

    placement = get_excavator_work_placement(current_excavator)
    destinations = parse_excavator_operator_destinations(
        payload,
        dump_point_queryset,
        placement,
    )
    if not destinations:
        return JsonResponse({'ok': False, 'error': 'Выберите хотя бы одну точку разгрузки из справочника.'}, status=400)
    dump_points = [row['dump_point'] for row in destinations]

    loading_horizon = normalize_excavator_numeric_setting(payload.get('loading_horizon'))
    loading_block = normalize_excavator_numeric_setting(payload.get('loading_block'))
    session_settings = request.session.get(EXCAVATOR_WORK_SETTINGS_SESSION_KEY, {})
    setting_key = excavator_work_settings_key(current_excavator)
    work_context_changed = excavator_work_context_changed(
        placement,
        session_settings.get(setting_key),
        rock_type=rock_type,
        dump_points=dump_points,
        loading_horizon=loading_horizon,
        loading_block=loading_block,
    )
    face_position_changed = excavator_face_position_changed(
        placement,
        loading_horizon=loading_horizon,
        loading_block=loading_block,
    )
    session_settings[setting_key] = {
        'client_action_id': str(payload.get('client_action_id') or ''),
        'rock_type_id': rock_type.id,
        'dump_point_ids': [point.id for point in dump_points],
        'destinations': [
            {
                'dump_point_id': row['dump_point'].id,
                'transport_distance_km': (
                    str(row['transport_distance_km'])
                    if row['transport_distance_km'] is not None
                    else ''
                ),
            }
            for row in destinations
        ],
        'loading_horizon': loading_horizon,
        'loading_block': loading_block,
        'updated_at': timezone.now().isoformat(),
    }
    request.session[EXCAVATOR_WORK_SETTINGS_SESSION_KEY] = session_settings
    request.session.modified = True
    with transaction.atomic():
        save_excavator_work_context(
            current_excavator=current_excavator,
            actor=access.employee,
            rock_type=rock_type,
            dump_points=dump_points,
            loading_horizon=loading_horizon,
            loading_block=loading_block,
            destination_settings=destinations,
        )
        active_downtime = None
        if face_position_changed:
            active_downtime = start_excavator_auto_downtime(
                current_excavator,
                access.employee,
                EXCAVATOR_AUTO_DOWNTIME_TRANSFER,
                replace_active=True,
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
            'destinations': session_settings[setting_key]['destinations'],
            'loading_horizon': loading_horizon,
            'loading_block': loading_block,
            'face_position_changed': face_position_changed,
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
        'destinations': session_settings[setting_key]['destinations'],
        'loading_horizon': loading_horizon,
        'loading_block': loading_block,
        'work_context_changed': work_context_changed,
        'face_position_changed': face_position_changed,
        'active_downtime_reason': str(active_downtime.reason) if active_downtime else '',
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
    return production_shift_type(now)


def work_assignment_error_message(state):
    if state in {'employee_inactive', 'access_inactive'}:
        return 'Рабочий доступ неактивен. Обратитесь к администратору.'
    if state == 'equipment_inactive':
        return 'Техника неактивна. Обратитесь к руководителю.'
    if state == 'assignment_conflict':
        return 'Техника занята в другой смене.'
    return 'Смена и техника не назначены.'


def get_previous_closed_equipment_shift(equipment):
    if not equipment:
        return None
    return (
        EmployeeShift.objects
        .filter(equipment=equipment, closed_at__isnull=False)
        .order_by('-closed_at', '-opened_at')
        .first()
    )


@require_POST
@transaction.atomic
def excavator_shift_action_view(request):
    access = excavator_access_from_request(request, require_active_role=False)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к экрану Экскаваторщика.'}, status=403)

    payload = excavator_json_payload(request)
    client_action_id = str(payload.get('client_action_id') or '').strip()
    if not client_action_id:
        return JsonResponse({'ok': False, 'error': 'Не передан client_action_id.'}, status=400)
    if len(client_action_id) > 128:
        return JsonResponse({'ok': False, 'error': 'client_action_id слишком длинный.'}, status=400)

    requested_action = str(payload.get('action') or payload.get('shift_action') or '').strip()
    lock_idempotency_key('excavator_shift_action', client_action_id)
    if requested_action == 'toggle':
        existing_action = (
            ShiftClientAction.objects
            .filter(
                action_type__in=('excavator_shift_opened', 'excavator_shift_closed'),
                client_action_id=client_action_id,
                employee=access.employee,
            )
            .order_by('created_at', 'id')
            .first()
        )
        if existing_action:
            response_payload = dict(existing_action.response_payload or {})
            response_payload.pop('_request_signature', None)
            response_payload['deduplicated'] = True
            return JsonResponse(response_payload)

    access = lock_excavator_mutation_access(request, access)
    if not access:
        return JsonResponse(
            {'ok': False, 'error': 'Роль неактивна — доступен только просмотр', 'code': 'inactive_role'},
            status=409,
        )
    open_shift = get_excavator_open_shift(access.employee)
    action = requested_action
    if action == 'toggle':
        action = 'close' if open_shift else 'open'
    if action not in {'open', 'close'}:
        return JsonResponse({'ok': False, 'error': 'Неизвестное действие смены.'}, status=400)

    try:
        if action == 'close':
            posted_shift_id = str(payload.get('shift_id') or '').strip()
            if not posted_shift_id:
                return JsonResponse(
                    {
                        'ok': False,
                        'error': 'Не указан ID закрываемой смены. Обновите экран и повторите действие.',
                        'code': 'shift_context_required',
                        'has_active_shift': bool(open_shift),
                    },
                    status=409,
                )
            raw_occurred_at = str(payload.get('occurred_at') or '').strip()
            occurred_at = parse_datetime(raw_occurred_at) if raw_occurred_at else None
            if raw_occurred_at and (occurred_at is None or timezone.is_naive(occurred_at)):
                return JsonResponse(
                    {
                        'ok': False,
                        'error': 'Время действия должно содержать часовой пояс.',
                        'code': 'invalid_occurred_at',
                    },
                    status=400,
                )
            response_payload = close_excavator_shift(
                employee=access.employee,
                fuel_value=payload.get('fuel'),
                engine_hours_value=payload.get('engine_hours'),
                client_action_id=client_action_id,
                submitted_fuel_percent=payload.get('fuel_percent'),
                confirmation_token=str(payload.get('confirmation_token') or '').strip(),
                expected_shift_id=posted_shift_id,
                occurred_at=occurred_at,
            )
            return JsonResponse(response_payload)

        work_assignment = get_active_equipment_assignment(access.employee, 'excavator_operator')
        assignment_state = work_assignment_state(access.employee, work_assignment)
        if assignment_state not in {'assigned', 'assignment_conflict'}:
            return JsonResponse({'ok': False, 'error': work_assignment_error_message(assignment_state), 'assignment_state': assignment_state}, status=409)
        fuel_value = payload.get('fuel')
        fuel_limit_override = None
        if 'fuel_percent' in payload:
            fuel_value, _, fuel_limit_override = excavator_fuel_liters_from_percent(
                work_assignment.equipment,
                payload.get('fuel_percent'),
            )
        response_payload = open_excavator_shift(
            employee=access.employee,
            equipment=work_assignment.equipment,
            shift_type=work_assignment.shift_type,
            fuel_value=fuel_value,
            engine_hours_value=payload.get('engine_hours'),
            client_action_id=client_action_id,
            fuel_limit_override=fuel_limit_override,
            close_other_role_shift=other_role_shift_flag(payload),
        )
        return JsonResponse(response_payload)
    except ExcavatorShiftCloseConfirmationRequired as confirmation:
        return JsonResponse(
            {
                'ok': False,
                'error': 'Проверьте подозрительные показания и подтвердите их.',
                'code': 'reading_confirmation_required',
                'confirmation_required': True,
                'confirmation_token': confirmation.confirmation_token,
                'warnings': confirmation.warnings,
                'field_errors': {},
                'client_action_id': client_action_id,
                'shift_id': open_shift.pk if open_shift else None,
                'has_active_shift': bool(open_shift),
            },
            status=422,
        )
    except ExcavatorShiftError as error:
        return JsonResponse({
            'ok': False,
            'error': error.message,
            'code': error.code,
            'field_errors': error.field_errors,
            'confirmation_required': False,
            'has_active_shift': bool(open_shift),
            **error.extra,
        }, status=error.status)


@require_GET
def excavator_hourly_report_view(request):
    access = excavator_access_from_request(request, require_active_role=False)
    if not access:
        return JsonResponse({
            'ok': False,
            'code': 'authentication_required',
            'error': 'Требуется вход Экскаваторщика.',
        }, status=403)

    open_shift = get_excavator_open_shift(access.employee)
    if not open_shift:
        return JsonResponse({
            'ok': False,
            'code': 'open_shift_required',
            'error': 'Нет открытой смены Экскаваторщика.',
        }, status=409)

    payload = build_excavator_hourly_report(open_shift.equipment)
    payload.update({
        'ok': True,
        'version': get_operational_state_version(),
    })
    response = JsonResponse(payload)
    response['Cache-Control'] = 'no-store'
    return response


def excavator_work_view(request):
    requested_fragment = request.GET.get('_operational_fragment', '').strip()
    access_id = request.session.get('employee_access_id')
    if not access_id:
        if requested_fragment == 'excavator':
            return JsonResponse({'authenticated': False}, status=401)
        return redirect('login')
    # GET must remain renderable when this same access was activated on another
    # device. The common read-only banner then offers "Продолжить здесь".
    # Rejecting the stale generation here used to create an endless
    # /excavator/work/ -> /home/ -> /excavator/work/ redirect loop.
    access = excavator_access_from_request(request, require_active_role=False)
    if not access:
        return redirect('role_home')

    reconcile_due_haul_assignments()
    reconcile_expired_free_bucket_acceptances()
    reconcile_expired_manual_trips()

    open_shift = get_excavator_open_shift(access.employee)
    work_assignment = get_active_equipment_assignment(access.employee, 'excavator_operator')
    assignment_state = work_assignment_state(access.employee, work_assignment)
    current_excavator = open_shift.equipment if open_shift else None
    reconcile_excavator_waiting_for_trucks(
        current_excavator,
        access.employee,
        start_when_empty=bool(open_shift),
    )
    shift_start_excavator = current_excavator or (work_assignment.equipment if work_assignment else None)
    equipment_open_shift = None
    if shift_start_excavator and not open_shift:
        equipment_open_shift = (
            EmployeeShift.objects
            .filter(equipment=shift_start_excavator, closed_at__isnull=True)
            .select_related('employee')
            .order_by('-opened_at')
            .first()
        )
    shift_fuel_limit = excavator_fuel_capacity_l(shift_start_excavator) if shift_start_excavator else Decimal('0')
    shift_action_block_message = ''
    other_role_shift_prompt_context = None
    if not open_shift:
        if assignment_state != 'assigned':
            shift_action_block_message = work_assignment_error_message(assignment_state)
        elif equipment_open_shift:
            shift_action_block_message = 'Техника занята в другой смене.'
        elif shift_fuel_limit <= 0:
            shift_action_block_message = (
                'Для этого экскаватора не настроена вместимость топливного бака. '
                'Обратитесь к администратору.'
            )
        else:
            other_role_shift = find_other_role_open_shift(
                access.employee,
                workplace_code='excavator_operator',
                for_update=False,
            )
            if other_role_shift:
                other_role_shift_prompt_context = other_role_shift_prompt(
                    other_role_shift,
                    target_workplace_code='excavator_operator',
                )
    previous_equipment_shift = None if open_shift or equipment_open_shift else get_previous_closed_equipment_shift(shift_start_excavator)

    legacy_trip_client_action_id = (
        str(request.POST.get('client_action_id') or '').strip()
        if request.method == 'POST'
        else secrets.token_urlsafe(24)
    )
    if request.method == 'POST':
        form = restrict_excavator_trip_form(
            TripCreateForm(request.POST, excavator_operator=access.employee),
            current_excavator,
            open_shift,
        )
        submitted_rock_type_id = request.POST.get('rock_type')
        if (
            submitted_rock_type_id
            and RockType.objects.filter(id=submitted_rock_type_id, is_active=True).exists()
            and not form.fields['rock_type'].queryset.filter(id=submitted_rock_type_id).exists()
        ):
            form.add_error(
                None,
                'Для выбранной породы не настроены плотность или кубатура '
                'назначенных самосвалов.',
            )
        if not legacy_trip_client_action_id:
            form.add_error(None, 'Не передан client_action_id. Обновите экран и повторите погрузку.')
        else:
            trip = None
            deduplicated = False
            try:
                with transaction.atomic():
                    lock_idempotency_key('truck_loaded', legacy_trip_client_action_id)
                    existing_action = (
                        TripClientAction.objects
                        .select_related('trip', 'trip__dump_point')
                        .filter(
                            action_type='truck_loaded',
                            client_action_id=legacy_trip_client_action_id,
                        )
                        .first()
                    )
                    deduplicated = bool(existing_action)
                    if existing_action:
                        trip = existing_action.trip
                    elif form.is_valid():
                        access = lock_excavator_mutation_access(request, access)
                        if not access:
                            raise ValidationError('Роль неактивна — доступен только просмотр')
                        locked_shift = (
                            EmployeeShift.objects
                            .select_for_update(of=('self',))
                            .filter(
                                employee=access.employee,
                                closed_at__isnull=True,
                            )
                            .filter(
                                Q(workplace_code='excavator_operator')
                                | Q(
                                    workplace_code='',
                                    equipment__equipment_type__name='Экскаватор',
                                )
                            )
                            .select_related('equipment', 'equipment__equipment_type')
                            .order_by('-opened_at')
                            .first()
                        )
                        if not locked_shift or not locked_shift.equipment_id:
                            raise ValidationError('Сначала нужно открыть смену на экскаваторе.')
                        lock_production_state()
                        requested_assignment = form.cleaned_data['assignment']
                        locked_excavator, locked_truck = lock_trip_participant_equipment(
                            excavator_id=locked_shift.equipment_id,
                            truck_id=requested_assignment.truck_id,
                        )
                        locked_shift.equipment = locked_excavator
                        locked_assignment, handoff = resolve_excavator_load_authority(
                            truck_id=locked_truck.id,
                            excavator_id=locked_excavator.id,
                            source_shift=locked_shift,
                            requested_assignment_id=requested_assignment.id,
                        )
                        if not locked_assignment:
                            raise ValidationError(
                                'Назначение уже изменилось или право завершить погрузку использовано.'
                            )
                        locked_assignment.truck = locked_truck
                        locked_assignment.excavator = locked_excavator
                        open_trip = (
                            Trip.objects
                            .select_for_update()
                            .filter(
                                truck_id=locked_truck.id,
                                status__in=OPEN_TRIP_STATUSES,
                            )
                            .first()
                        )
                        block_reason = excavator_truck_load_block_reason(
                            locked_assignment,
                            current_excavator=locked_excavator,
                            active_trip=open_trip or False,
                        )
                        if block_reason:
                            raise ValidationError(block_reason)
                        form.cleaned_data['assignment'] = locked_assignment
                        trip = form.create_trip(
                            excavator_operator=access.employee,
                            loading_shift=locked_shift,
                        )
                        TripClientAction.objects.create(
                            action_type='truck_loaded',
                            client_action_id=legacy_trip_client_action_id,
                            trip=trip,
                            actor=access.employee,
                        )
                        close_truck_waiting_loading_downtimes(locked_assignment.truck)
                        close_excavator_open_downtimes(locked_excavator)
                        reconcile_excavator_waiting_for_trucks(
                            locked_excavator,
                            access.employee,
                            start_when_empty=True,
                        )
                        bump_operational_state(
                            'Trip:truck_loaded',
                            event_type='trip_changed',
                            object_type='Trip',
                            object_id=trip.id,
                            payload={
                                'action': 'truck_loaded',
                                'driver_participation_recorded': trip.driver_participation_recorded,
                                'driver_control_shift_id': trip.driver_control_shift_id,
                                'trip_id': trip.id,
                                'truck_id': trip.truck_id,
                                'excavator_id': trip.excavator_id,
                                'excavator_ids': sorted({
                                    trip.excavator_id,
                                    *(
                                        [handoff.target_assignment.excavator_id]
                                        if handoff and handoff.target_assignment.excavator_id
                                        else []
                                    ),
                                }),
                                'dump_point_id': trip.dump_point_id,
                                'assigned_dump_point_id': trip.assigned_dump_point_id or trip.dump_point_id,
                                'actual_dump_point_id': trip.actual_dump_point_id or trip.dump_point_id,
                                'dump_point_name': str(trip.assigned_dump_point or trip.dump_point),
                                'status': TripStatus.LOADED_WAITING_UNLOAD,
                            },
                        )
            except ValidationError as error:
                form.add_error(None, error)
            else:
                if trip is not None:
                    response_payload = trip_loaded_payload(
                        trip,
                        client_action_id=legacy_trip_client_action_id,
                    )
                    if deduplicated:
                        response_payload['deduplicated'] = True
                    response_payload['downtime_status'] = excavator_downtime_status_payload(
                        trip.excavator,
                        trip.loading_shift,
                    )
                    response_payload['deduplicated'] = deduplicated
                    response_payload['version'] = get_operational_state_version()
                    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                        return JsonResponse(response_payload)
                    messages.success(request, 'Рейс создан. У водителя появился активный рейс.')
                    return redirect('excavator_work')
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'ok': False, 'errors': form.errors}, status=400)
    else:
        form = restrict_excavator_trip_form(
            TripCreateForm(excavator_operator=access.employee),
            current_excavator,
            open_shift,
        )

    transfer_by_assignment_id = {}
    if open_shift and current_excavator:
        open_transfers = (
            active_haul_handoffs()
            .filter(
                Q(source_shift=open_shift)
                | Q(target_assignment__excavator=current_excavator)
            )
            .select_related(
                'source_excavator',
                'source_assignment',
                'target_assignment',
                'target_assignment__excavator',
            )
            .order_by('-created_at', '-id')
        )
        for transfer in open_transfers:
            common = {
                'id': transfer.id,
                'created_at': transfer.target_assignment.assigned_at or transfer.created_at,
                'deadline': transfer.target_assignment.effective_at,
                'source_label': str(transfer.source_excavator.garage_number or transfer.source_excavator),
                'target_label': str(
                    transfer.target_assignment.excavator.garage_number
                    or transfer.target_assignment.excavator
                ),
            }
            if transfer.source_shift_id == open_shift.id:
                transfer_by_assignment_id.setdefault(
                    transfer.source_assignment_id,
                    {
                        **common,
                        'direction': 'outgoing',
                        'kind': 'inter_excavator',
                        'route_label': f"→ {common['target_label']}",
                    },
                )
            if transfer.target_assignment.excavator_id == current_excavator.id:
                transfer_by_assignment_id.setdefault(
                    transfer.target_assignment_id,
                    {
                        **common,
                        'direction': 'incoming',
                        'kind': 'inter_excavator',
                        'route_label': f"← {common['source_label']}",
                    },
                )
        direct_pending_assignments = (
            HaulAssignment.objects
            .filter(
                excavator=current_excavator,
                status=AssignmentStatus.PENDING,
                ended_at__isnull=True,
                action=HaulAssignmentAction.ASSIGN,
                effective_at__isnull=False,
            )
            .select_related('excavator')
        )
        for pending in direct_pending_assignments:
            transfer_by_assignment_id.setdefault(
                pending.id,
                {
                    'id': f'assignment-{pending.id}',
                    'created_at': pending.assigned_at or pending.created_at,
                    'deadline': pending.effective_at,
                    'source_label': '',
                    'target_label': str(current_excavator.garage_number or current_excavator),
                    'direction': 'incoming',
                    'kind': 'from_free',
                    'route_label': 'Назначается',
                },
            )
        pending_releases = list(
            HaulAssignment.objects
            .filter(
                excavator=current_excavator,
                status=AssignmentStatus.PENDING,
                ended_at__isnull=True,
                action=HaulAssignmentAction.RELEASE,
                effective_at__isnull=False,
            )
            .order_by('-assigned_at', '-id')
        )
        release_by_truck_id = {item.truck_id: item for item in pending_releases}
        accepted_sources = HaulAssignment.objects.filter(
            truck_id__in=release_by_truck_id,
            excavator=current_excavator,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
        )
        for source in accepted_sources:
            release = release_by_truck_id[source.truck_id]
            transfer_by_assignment_id.setdefault(
                source.id,
                {
                    'id': f'assignment-{release.id}',
                    'created_at': release.assigned_at or release.created_at,
                    'deadline': release.effective_at,
                    'source_label': str(current_excavator.garage_number or current_excavator),
                    'target_label': '',
                    'direction': 'outgoing',
                    'kind': 'release',
                    'route_label': 'В свободные',
                },
            )
    available_assignments = []
    visible_assignment_index_by_truck = {}
    for assignment in form.fields['assignment'].queryset:
        assignment.transfer_state = transfer_by_assignment_id.get(assignment.id)
        assignment.is_handoff_completion = False
        existing_index = visible_assignment_index_by_truck.get(assignment.truck_id)
        if existing_index is None:
            visible_assignment_index_by_truck[assignment.truck_id] = len(available_assignments)
            available_assignments.append(assignment)
            continue
        existing = available_assignments[existing_index]
        if existing.transfer_state and not assignment.transfer_state:
            available_assignments[existing_index] = assignment
    assignment_snapshot_cards = [
        {
            'assignment_id': assignment.id,
            'truck_id': assignment.truck_id,
            'number': str(assignment.truck.garage_number or assignment.truck or '-'),
        }
        for assignment in available_assignments
    ]
    outgoing_transfer_by_truck_id = {
        assignment.truck_id: assignment.transfer_state
        for assignment in available_assignments
        if (
            assignment.truck_id
            and assignment.transfer_state
            and assignment.transfer_state.get('direction') == 'outgoing'
        )
    }
    outgoing_sent_truck_ids = set()
    if outgoing_transfer_by_truck_id and open_shift and current_excavator:
        source_transition_trips = (
            Trip.objects
            .filter(
                truck_id__in=outgoing_transfer_by_truck_id,
                excavator=current_excavator,
                loading_shift=open_shift,
            )
            .exclude(status=TripStatus.CANCELLED)
            .only('truck_id', 'created_at')
        )
        for source_trip in source_transition_trips:
            transition_started_at = outgoing_transfer_by_truck_id[source_trip.truck_id].get('created_at')
            if transition_started_at and source_trip.created_at >= transition_started_at:
                outgoing_sent_truck_ids.add(source_trip.truck_id)
    assignment_truck_ids = [assignment.truck_id for assignment in available_assignments if assignment.truck_id]
    visible_assignment_truck_ids = set(assignment_truck_ids)
    dump_card_now = timezone.now()
    active_trips_queryset = (
        Trip.objects
        .filter(status__in=OPEN_TRIP_STATUSES)
        .select_related('truck', 'excavator', 'rock_type', 'dump_point', 'free_bucket_acceptance')
        .order_by('-created_at', '-id')
    )
    if current_excavator:
        active_trips_queryset = active_trips_queryset.filter(excavator=current_excavator)
    else:
        active_trips_queryset = active_trips_queryset.filter(excavator_operator=access.employee)
    active_trips = list(active_trips_queryset[:20])
    outgoing_sent_truck_ids.update(
        trip.truck_id
        for trip in active_trips
        if trip.truck_id in outgoing_transfer_by_truck_id
    )
    dump_badge_trips = list(
        active_trips_queryset.filter(
            free_bucket_dump_card_visibility_filter(now=dump_card_now)
            | (
                Q(free_bucket_acceptance__isnull=True)
                & manual_dump_card_visibility_filter(now=dump_card_now)
            )
        )[:20]
    )
    historical_outgoing_transition_by_truck_id = {}
    historical_transition_truck_ids = {
        trip.truck_id
        for trip in dump_badge_trips
        if (
            trip.truck_id not in outgoing_transfer_by_truck_id
            and trip.truck_id not in visible_assignment_truck_ids
        )
    }
    if historical_transition_truck_ids and current_excavator:
        for transfer in (
            HaulAssignmentHandoff.objects
            .filter(
                truck_id__in=historical_transition_truck_ids,
                source_excavator=current_excavator,
                target_assignment__action=HaulAssignmentAction.ASSIGN,
                target_assignment__effective_at__isnull=False,
            )
            .exclude(target_assignment__excavator=current_excavator)
            .select_related('target_assignment')
        ):
            target = transfer.target_assignment
            started_at = target.assigned_at or transfer.created_at
            deadline = target.effective_at
            if not started_at or not deadline:
                continue
            candidate = {
                'id': transfer.id,
                'created_at': started_at,
                'deadline': deadline,
                'order_id': target.id,
            }
            existing = historical_outgoing_transition_by_truck_id.get(transfer.truck_id)
            if not existing or (started_at, target.id) > (existing['created_at'], existing['order_id']):
                historical_outgoing_transition_by_truck_id[transfer.truck_id] = candidate
        for release in (
            HaulAssignment.objects
            .filter(
                truck_id__in=historical_transition_truck_ids,
                excavator=current_excavator,
                action=HaulAssignmentAction.RELEASE,
                effective_at__isnull=False,
            )
        ):
            started_at = release.assigned_at or release.created_at
            candidate = {
                'id': f'assignment-{release.id}',
                'created_at': started_at,
                'deadline': release.effective_at,
                'order_id': release.id,
            }
            existing = historical_outgoing_transition_by_truck_id.get(release.truck_id)
            if not existing or (started_at, release.id) > (existing['created_at'], existing['order_id']):
                historical_outgoing_transition_by_truck_id[release.truck_id] = candidate
    last_sent_trip = None
    if open_shift:
        last_sent_trip = (
            Trip.objects
            .filter(loading_shift=open_shift)
            .only('assigned_dump_point_id', 'dump_point_id', 'created_at')
            .order_by('-created_at', '-id')
            .first()
        )
    last_sent_dump_point_id = (
        (last_sent_trip.assigned_dump_point_id or last_sent_trip.dump_point_id)
        if last_sent_trip else None
    )
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
    post_unload_cooldown_by_truck_id = {}
    if assignment_truck_ids:
        cooldown_cutoff = timezone.now() - TRUCK_POST_UNLOAD_COOLDOWN
        for completed_trip in (
            Trip.objects
            .filter(
                truck_id__in=assignment_truck_ids,
                status=TripStatus.COMPLETED,
                completed_at__gt=cooldown_cutoff,
            )
            .select_related('truck')
            .only('truck_id', 'truck__garage_number', 'completed_at')
            .order_by('truck_id', '-completed_at')
        ):
            post_unload_cooldown_by_truck_id.setdefault(
                completed_trip.truck_id,
                truck_post_unload_cooldown(
                    completed_trip.truck,
                    completed_at=completed_trip.completed_at,
                ),
            )

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
                employee__is_active=True,
                employee_id__in=(
                    EmployeeAccess.objects
                    .filter(role__code='driver', is_active=True)
                    .exclude(status=EmployeeAccess.Status.DEACTIVATED)
                    .values('employee_id')
                ),
            )
            .filter(
                Q(role__code='driver')
                | Q(role__isnull=True)
            )
            .values_list('equipment_id', flat=True)
            .distinct()
        )

    driver_participation = truck_driver_participation(assignment_truck_ids)

    def assignment_load_block(assignment, active_trip=None, *, manual_control=False):
        known_active_trip = active_trip
        if known_active_trip is None:
            known_active_trip = active_trip_by_truck_id.get(assignment.truck_id) or False
        return excavator_truck_load_block(
            assignment,
            current_excavator=current_excavator,
            participation=driver_participation[assignment.truck_id],
            manual_control=manual_control,
            active_trip=known_active_trip,
            active_downtime=truck_downtime_by_equipment_id.get(assignment.truck_id),
            post_unload_cooldown=post_unload_cooldown_by_truck_id.get(assignment.truck_id) or False,
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
        if post_unload_cooldown_by_truck_id.get(assignment.truck_id):
            return 'waiting'
        if assignment.truck_id not in open_truck_shift_equipment_ids:
            if assignment.truck_id in driver_assignment_truck_ids:
                return 'waiting_for_shift'
            return 'no_driver'
        if (
            getattr(assignment, 'is_handoff_completion', False)
            or assignment.status in {AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED}
        ):
            return 'assigned'
        return 'free'

    # A confirmed free-bucket acceptance does not change the dispatcher
    # assignment, so the primary excavator keeps its normal card.  It must
    # nevertheless see why that card is temporarily unavailable.
    from trips.models import FreeBucketAcceptance, FreeBucketAcceptanceStatus
    foreign_free_bucket_by_truck_id = {}
    if current_excavator:
        for acceptance in (
            FreeBucketAcceptance.objects
            .filter(
                truck_id__in=[assignment.truck_id for assignment in available_assignments],
            )
            .filter(active_free_bucket_acceptance_filter(now=timezone.now()))
            .exclude(excavator=current_excavator)
            .select_related('excavator', 'used_trip')
            .order_by('-occurred_at', '-id')
        ):
            foreign_free_bucket_by_truck_id.setdefault(
                acceptance.truck_id,
                {
                    'label': str(acceptance.excavator.garage_number or acceptance.excavator),
                    'is_loaded': acceptance.status == FreeBucketAcceptanceStatus.USED,
                },
            )

    truck_cards = []
    for assignment in available_assignments:
        if assignment.truck_id in outgoing_sent_truck_ids:
            continue
        active_trip = active_trip_by_truck_id.get(assignment.truck_id)
        foreign_free_bucket = foreign_free_bucket_by_truck_id.get(assignment.truck_id)
        equipment_state_code = excavator_truck_equipment_state_code(assignment, active_trip)
        target_label = str(active_trip.dump_point) if active_trip else ''
        state_ui = equipment_state_ui(equipment_state_map, equipment_state_code)
        load_block = assignment_load_block(assignment, active_trip)
        if foreign_free_bucket:
            load_block = {
                'code': 'free_bucket_reserved_elsewhere',
                'label': 'Самосвал временно обслуживается другим экскаватором.',
            }
        block_reason = load_block['label'] if load_block else ''
        load_block_reason_code = load_block['code'] if load_block else ''
        participation = driver_participation[assignment.truck_id]
        manual_available = bool(manual_loading_enabled() and participation['passive']
                                and not assignment_load_block(assignment, active_trip, manual_control=True))
        unowned_previous = bool(active_trip and active_trip.driver_participation_recorded
                                and not active_trip.driver_control_shift_id and not participation['passive']
                                and manual_loading_enabled())
        soft_driver_block = load_block_reason_code in {'no_driver', 'driver_shift_not_started', 'driver_offline'}
        active_truck_downtime = truck_downtime_by_equipment_id.get(assignment.truck_id)
        is_waiting_for_loading = truck_waiting_loading_downtime(active_truck_downtime)
        state_allows_load = bool(
            is_waiting_for_loading
            or unowned_previous
            or (state_ui['allows_drag'] and not state_ui['blocks_operation'])
        )
        can_load = bool(not load_block and state_allows_load)
        is_locked = not can_load
        is_inactive = bool(
            (load_block and not soft_driver_block and load_block_reason_code != 'transfer_outgoing')
            or (not load_block and not state_allows_load)
        )
        status_key = state_ui['color_group']
        truck_cards.append({
            'assignment': assignment,
            'manual_available': manual_available,
            'driver_presence_code': participation['code'],
            'driver_presence_label': participation['label'],
            'open_trip_id': active_trip.pk if active_trip else '',
            'number': equipment_number(assignment.truck),
            'number_is_plain': is_plain_number(equipment_number(assignment.truck)),
            'equipment_state_code': equipment_state_code,
            'status_key': status_key,
            'status_label': (
                (
                    'На разгрузку под свободным ковшом'
                    if foreign_free_bucket and foreign_free_bucket['is_loaded']
                    else 'Под свободным ковшом'
                )
                if foreign_free_bucket
                else active_truck_downtime.reason.button_label
                if active_truck_downtime
                else (
                    block_reason
                    if load_block_reason_code == 'post_unload_cooldown'
                    else (
                        'Завершить погрузку'
                        if getattr(assignment, 'is_handoff_completion', False)
                        else state_ui['label']
                    )
                )
            ),
            'target_label': target_label,
            'is_selected': assignment.id == first_ready_assignment_id,
            'is_locked': is_locked,
            'is_inactive': is_inactive,
            'is_load_blocked': bool(load_block and soft_driver_block),
            'can_drag': can_load,
            'can_load': can_load,
            'is_waiting_for_loading': is_waiting_for_loading,
            'is_handoff_completion': False,
            'transfer': getattr(assignment, 'transfer_state', None),
            'foreign_free_bucket': foreign_free_bucket,
            'driver_shift_started': assignment.truck_id in open_truck_shift_equipment_ids,
            'block_reason': block_reason,
            'load_block_reason_code': load_block_reason_code,
            'load_block_reason_label': block_reason,
            'icon': f'img/equipment/truck-{status_key}.png',
        })

    # This directory is deliberately broader than the excavator's own cards:
    # an offline free-bucket lookup must find every truck the operator was
    # allowed to see when the shell was last synchronized.
    free_bucket_trucks = list(
        Equipment.objects.filter(equipment_type__name='Самосвал')
        .select_related('equipment_type', 'model')
        .order_by('garage_number', 'id')
    )
    free_bucket_truck_ids = [item.id for item in free_bucket_trucks]
    free_bucket_primary_by_truck_id = {}
    for assignment in (
        HaulAssignment.objects.filter(
            truck_id__in=[item.id for item in free_bucket_trucks],
            action=HaulAssignmentAction.ASSIGN,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
        )
        .select_related('excavator')
        .order_by('truck_id', '-assigned_at', '-id')
    ):
        free_bucket_primary_by_truck_id.setdefault(
            assignment.truck_id,
            excavator_operator_label(assignment.excavator),
        )
    free_bucket_active_trip_by_truck_id = {}
    free_bucket_downtime_by_truck_id = {}
    free_bucket_acceptance_by_truck_id = {}
    if free_bucket_truck_ids:
        for trip in (
            Trip.objects
            .filter(truck_id__in=free_bucket_truck_ids, status__in=OPEN_TRIP_STATUSES)
            .only('id', 'truck_id', 'status')
            .order_by('-created_at', '-id')
        ):
            free_bucket_active_trip_by_truck_id.setdefault(trip.truck_id, trip)
        for downtime in (
            DowntimeEvent.objects
            .filter(equipment_id__in=free_bucket_truck_ids, ended_at__isnull=True)
            .select_related('reason')
            .order_by('-started_at', '-id')
        ):
            free_bucket_downtime_by_truck_id.setdefault(downtime.equipment_id, downtime)
        for acceptance in (
            FreeBucketAcceptance.objects
            .filter(
                truck_id__in=free_bucket_truck_ids,
            )
            .filter(active_free_bucket_acceptance_filter(now=timezone.now()))
            .select_related('excavator', 'used_trip')
            .order_by('-occurred_at', '-id')
        ):
            free_bucket_acceptance_by_truck_id.setdefault(acceptance.truck_id, acceptance)

    free_bucket_truck_directory = {
        'updated_at': timezone.now().isoformat(),
        'version': get_operational_state_version(),
        'trucks': [],
    }
    for truck in free_bucket_trucks:
        model_name = str(getattr(truck.model, 'name', '') or '')
        model_key = model_name.casefold()
        truck_type = 'БелАЗ' if 'белаз' in model_key or 'belaz' in model_key else (
            'NHL' if 'nhl' in model_key or 'nte' in model_key else 'Тип не определён'
        )
        active_trip = free_bucket_active_trip_by_truck_id.get(truck.id)
        downtime = free_bucket_downtime_by_truck_id.get(truck.id)
        acceptance = free_bucket_acceptance_by_truck_id.get(truck.id)
        requested_for_current_excavator = bool(
            acceptance
            and current_excavator
            and acceptance.status == FreeBucketAcceptanceStatus.REQUESTED
            and acceptance.excavator_id == current_excavator.id
        )
        availability_label = (
            'Неактивен' if not truck.is_active
            else 'На разгрузку' if active_trip
            else str(downtime.reason.button_label or downtime.reason) if downtime
            else 'Запрошен водителем' if requested_for_current_excavator
            else 'Под свободным ковшом' if acceptance
            else 'Доступен'
        )
        free_bucket_truck_directory['trucks'].append({
            'id': truck.id,
            'number': equipment_number(truck),
            'truck_type': truck_type,
            'model': model_name,
            'is_active': bool(truck.is_active),
            'can_accept_free_bucket': bool(
                truck.is_active
                and not active_trip
                and (not acceptance or requested_for_current_excavator)
            ),
            'primary_assignment_label': free_bucket_primary_by_truck_id.get(truck.id, ''),
            'availability_label': availability_label,
        })
    free_bucket_cards = []
    if current_excavator:
        for acceptance in (
            FreeBucketAcceptance.objects
            .filter(
                excavator=current_excavator,
                status=FreeBucketAcceptanceStatus.ACCEPTED,
            )
            .select_related('truck', 'primary_assignment__excavator')
            .order_by('occurred_at', 'id')
        ):
            free_bucket_cards.append({
                'id': acceptance.id,
                'client_acceptance_id': acceptance.client_acceptance_id,
                'truck_id': acceptance.truck_id,
                'number': equipment_number(acceptance.truck),
                'primary_assignment_label': (
                    excavator_operator_label(acceptance.primary_assignment.excavator)
                    if acceptance.primary_assignment_id else ''
                ),
                'occurred_at': acceptance.occurred_at,
            })

    work_settings = excavator_work_settings_from_session(request, current_excavator, form)
    if not form.is_bound and work_settings['transport_distance_km'] not in {None, ''}:
        form.fields['transport_distance_km'].initial = work_settings['transport_distance_km']
    dump_points = work_settings['selected_dump_points']
    dump_cards = build_excavator_dump_cards(
        dump_points,
        distance_values=work_settings['destination_distance_values'],
    )
    dump_choice_cards = build_excavator_dump_cards(
        work_settings['dump_point_choices'],
        selected_ids=work_settings['selected_dump_point_ids'],
        persisted_ids=work_settings['persisted_dump_point_ids'],
        distance_values=work_settings['destination_distance_values'],
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

    downtime_calculated_at = timezone.now()
    downtime_reason_totals = equipment_shift_downtime_seconds_by_reason(
        current_excavator,
        open_shift,
        until=downtime_calculated_at,
    )

    def downtime_reason_card(reason):
        label = reason.button_label
        full_name = str(reason)
        reason_state_code = downtime_reason_equipment_state_code(reason)
        reason_state_ui = downtime_reason_state_ui(equipment_state_map, reason)
        total_seconds = downtime_reason_totals.get(reason.id, 0)
        return {
            'reason': reason,
            'name': label,
            'full_name': full_name,
            'equipment_state_code': reason_state_code,
            'status_key': reason_state_ui['color_group'],
            'is_selected': bool(active_downtime and active_downtime.reason_id == reason.id),
            'total_seconds': total_seconds,
            'total_label': format_duration_label(total_seconds),
            'is_used': bool(total_seconds or (active_downtime and active_downtime.reason_id == reason.id)),
        }

    downtime_reason_cards = [downtime_reason_card(reason) for reason in downtime_reasons]

    active_downtime_elapsed_seconds = 0
    active_downtime_elapsed_label = '00:00:00'
    shift_downtime_total_seconds = sum(downtime_reason_totals.values())
    shift_downtime_total_label = format_duration_label(shift_downtime_total_seconds)
    active_downtime_state = equipment_state_ui(equipment_state_map, 'waiting')
    active_downtime_started_at = ''
    if active_downtime and active_downtime.started_at:
        active_downtime_elapsed_seconds = max(0, int((timezone.now() - active_downtime.started_at).total_seconds()))
        active_downtime_elapsed_label = format_duration_label(active_downtime_elapsed_seconds)
        active_downtime_state = downtime_reason_state_ui(equipment_state_map, active_downtime.reason)
        active_downtime_started_at = active_downtime.started_at.isoformat()
    active_downtime_counts_towards_shift = downtime_event_counts_towards_shift(
        active_downtime,
        open_shift,
    )

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
            shift=truck_shift,
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
    dump_transition_by_trip_id = {}
    for trip in dump_badge_trips:
        free_bucket_expires_at = free_bucket_dump_card_expires_at(trip)
        if free_bucket_expires_at is not None:
            if not free_bucket_dump_card_is_visible(trip, now=dump_card_now):
                continue
        elif not manual_dump_card_is_visible(trip, now=dump_card_now):
            continue
        transition = outgoing_transfer_by_truck_id.get(trip.truck_id)
        historical_transition = False
        if transition is None:
            historical = historical_outgoing_transition_by_truck_id.get(trip.truck_id)
            if (
                historical
                and historical['created_at'] <= trip.created_at <= historical['deadline']
            ):
                transition = historical
                historical_transition = True
        if transition:
            dump_transition_by_trip_id[trip.id] = transition
            if historical_transition or transition['deadline'] <= dump_card_now:
                continue
        point_id = trip.assigned_dump_point_id or trip.actual_dump_point_id or trip.dump_point_id
        if point_id:
            active_trips_by_dump_id[point_id].append(trip)

    shift_trip_queryset = Trip.objects.none()
    if open_shift:
        shift_trip_queryset = Trip.objects.filter(
            loading_shift=open_shift,
        ).exclude(status=TripStatus.CANCELLED)

    completed_shift_count = shift_trip_queryset.count()
    completed_shift_volume = shift_trip_queryset.aggregate(total=Sum('volume_m3'))['total'] or Decimal('0')
    shift_fact_label = 'Факт'
    shift_fact_value = format_whole_value_with_unit(completed_shift_volume, 'м³')
    shift_fact_meta = f'{completed_shift_count} маш.'

    completed_by_dump_id = defaultdict(int)
    completed_face_queryset = shift_trip_queryset
    if face_horizon:
        completed_face_queryset = completed_face_queryset.filter(loading_horizon=face_horizon)
    if face_block:
        completed_face_queryset = completed_face_queryset.filter(loading_block=face_block)
    if current_rock:
        completed_face_queryset = completed_face_queryset.filter(rock_type=current_rock)
    for row in (
        completed_face_queryset
        .annotate(effective_dump_point_id=Coalesce('assigned_dump_point_id', 'dump_point_id'))
        .values('effective_dump_point_id')
        .annotate(total=Count('id'))
    ):
        if row['effective_dump_point_id']:
            completed_by_dump_id[row['effective_dump_point_id']] = row['total']

    for card in dump_cards:
        point_id = card['point'].id
        card['completed_count'] = completed_by_dump_id[point_id]
        card['is_last_sent'] = point_id == last_sent_dump_point_id
        pending_trips = active_trips_by_dump_id.get(point_id, [])
        card['pending_trucks'] = [
            {
                'truck_id': trip.truck_id,
                'trip_id': trip.id,
                'number': equipment_number(trip.truck),
                'status_key': 'green',
                'is_last_sent': index == 0,
                'auto_hide_at': (
                    free_bucket_dump_card_expires_at(trip)
                    or manual_dump_card_expires_at(trip)
                ),
                'auto_hide_kind': (
                    'free_bucket'
                    if free_bucket_dump_card_expires_at(trip) is not None
                    else 'manual' if manual_dump_card_expires_at(trip) is not None
                    else ''
                ),
                'transition_id': dump_transition_by_trip_id.get(trip.id, {}).get('id', ''),
                'transition_hide_at': dump_transition_by_trip_id.get(trip.id, {}).get('deadline'),
            }
            for index, trip in enumerate(pending_trips)
        ]

    def form_value_as_text(field_name):
        value = form[field_name].value()
        return '' if value is None else str(value)

    operational_state_version = get_operational_state_version()
    response = render(
        request,
        'trips/excavator_work.html',
        {
            'access': access,
            'form': form,
            'open_shift': open_shift,
            'current_excavator': current_excavator,
            'server_now': timezone.now(),
            'shift_start_excavator': shift_start_excavator,
            'available_assignments': available_assignments,
            'active_trips': active_trips,
            'available_assignments_count': len(available_assignments),
            'active_trips_count': len(active_trips),
            'completed_today_count': completed_shift_count,
            'truck_cards': truck_cards,
            'free_bucket_truck_directory': free_bucket_truck_directory,
            'free_bucket_cards': free_bucket_cards,
            'assignment_snapshot_cards': assignment_snapshot_cards,
            'first_ready_assignment_id': first_ready_assignment_id,
            'legacy_trip_client_action_id': legacy_trip_client_action_id,
            'truck_detail_cards': truck_detail_cards,
            'dump_cards': dump_cards,
            'dump_choice_cards': dump_choice_cards,
            'rock_choices': rock_choices,
            'has_applied_settings': work_settings['has_applied_settings'],
            'default_rock': default_rock,
            'default_dump_point': default_dump_point,
            'selected_dump_point_ids': work_settings['selected_dump_point_ids'],
            'face_horizon': face_horizon,
            'face_block': face_block,
            'current_rock': current_rock,
            'selected_dump_point': selected_dump_point,
            'shift_time_label': (
                '07:00-19:00'
                if (open_shift and open_shift.shift_type == 'day')
                or (not open_shift and work_assignment and work_assignment.shift_type == 'day')
                else '19:00-07:00'
                if open_shift or work_assignment
                else 'Не назначена'
            ),
            'excavator_label': excavator_operator_label(current_excavator or shift_start_excavator) if (current_excavator or shift_start_excavator) else 'Не назначен',
            'work_assignment': work_assignment,
            'work_assignment_state': assignment_state,
            'work_assignment_error': work_assignment_error_message(assignment_state),
            'work_assignment_shift_label': work_assignment.work_shift_label if work_assignment else '',
            'equipment_open_shift': equipment_open_shift,
            'shift_fuel_limit': shift_fuel_limit,
            'shift_action_block_message': shift_action_block_message,
            'other_role_shift_prompt': other_role_shift_prompt_context,
            'shift_previous_readings': bool(previous_equipment_shift),
            'shift_start_fuel_display': format_whole_input_value(open_shift.start_fuel if open_shift else None),
            'shift_start_fuel_percent_display': excavator_fuel_percent_from_liters(
                open_shift.start_fuel if open_shift else None,
                shift_fuel_limit,
            ),
            'shift_start_engine_hours_display': format_whole_input_value(open_shift.start_engine_hours if open_shift else None),
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
            'shift_fuel_display': excavator_fuel_percent_from_liters(
                open_shift.end_fuel if open_shift else getattr(previous_equipment_shift, 'end_fuel', None),
                shift_fuel_limit,
            ),
            'shift_engine_hours_display': format_whole_input_value(
                open_shift.end_engine_hours if open_shift else getattr(previous_equipment_shift, 'end_engine_hours', None)
            ),
            'active_downtime': active_downtime,
            'active_downtime_started_at': active_downtime_started_at,
            'active_downtime_elapsed_seconds': active_downtime_elapsed_seconds,
            'active_downtime_elapsed_label': active_downtime_elapsed_label,
            'active_downtime_counts_towards_shift': active_downtime_counts_towards_shift,
            'shift_downtime_total_seconds': shift_downtime_total_seconds,
            'shift_downtime_total_label': shift_downtime_total_label,
            'downtime_calculated_at': downtime_calculated_at.isoformat(),
            'active_downtime_state': active_downtime_state,
            'downtime_reason_cards': downtime_reason_cards,
            'operational_state_version': operational_state_version,
            'planned_volume_value': form_value_as_text('planned_volume_m3'),
            'transport_distance_value': form_value_as_text('transport_distance_km'),
            'downtime_text_value': form_value_as_text('downtime_text'),
            'note_value': form_value_as_text('note'),
        },
    )
    if requested_fragment == 'excavator':
        return operational_fragment_response(
            response,
            screen='excavator',
            selector='[data-eo-shell]',
            version=operational_state_version,
            extra={
                'equipment_cards': truck_detail_cards,
                # The fragment extractor intentionally strips script elements.
                # Send the JSON snapshots explicitly so a shell replacement can
                # restore the temporary cards and the offline truck directory.
                'free_bucket_truck_directory': free_bucket_truck_directory,
                'free_bucket_cards': free_bucket_cards,
            },
        )
    return response


@require_http_methods(["GET", "POST"])
@transaction.atomic
def excavator_downtime_action_view(request):
    access = excavator_access_from_request(request, require_active_role=False)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к экрану Экскаваторщика.'}, status=403)
    open_shift = get_excavator_open_shift(access.employee)
    current_excavator = open_shift.equipment if open_shift else None
    if not current_excavator:
        return JsonResponse({'ok': False, 'error': 'Сначала нужно открыть смену на экскаваторе.'}, status=409)

    if request.method == 'GET':
        return JsonResponse(excavator_downtime_status_payload(current_excavator, open_shift))

    access = lock_excavator_mutation_access(request, access)
    if not access:
        return JsonResponse(
            {
                'ok': False,
                'error': 'Роль неактивна — доступен только просмотр',
                'code': 'inactive_role',
            },
            status=409,
        )
    open_shift = (
        EmployeeShift.objects
        .select_for_update(of=('self',))
        .filter(employee=access.employee, closed_at__isnull=True)
        .filter(
            Q(workplace_code='excavator_operator')
            | Q(
                workplace_code='',
                equipment__equipment_type__name='Экскаватор',
            )
        )
        .select_related('equipment', 'equipment__equipment_type')
        .order_by('-opened_at')
        .first()
    )
    current_excavator = open_shift.equipment if open_shift else None
    if not current_excavator:
        return JsonResponse({'ok': False, 'error': 'Сначала нужно открыть смену на экскаваторе.'}, status=409)

    payload = excavator_json_payload(request)
    action = (payload.get('action') or '').strip()
    current_excavator = Equipment.objects.select_for_update(of=('self',)).get(pk=current_excavator.pk)
    active_event = (
        DowntimeEvent.objects
        .filter(equipment=current_excavator, ended_at__isnull=True)
        .select_related('reason', 'reason__equipment_state')
        .order_by('-started_at')
        .first()
    )

    if action == 'close':
        if not active_event:
            response_payload = {
                'ok': True,
                'active': False,
                'closed': False,
                'elapsed_seconds': 0,
                'elapsed_label': '00:00:00',
                'version': get_operational_state_version(),
                'active_counts_towards_shift': False,
            }
            response_payload.update(excavator_downtime_totals_payload(current_excavator, open_shift))
            return JsonResponse(response_payload)
        active_event.ended_at = timezone.now()
        active_event.save(update_fields=['ended_at'])
        response_payload = downtime_event_payload(active_event, action='downtime_closed', closed=True)
        response_payload['active_counts_towards_shift'] = False
        response_payload.update(excavator_downtime_totals_payload(current_excavator, open_shift))
        return JsonResponse(response_payload)

    if action != 'start':
        return JsonResponse({'ok': False, 'error': 'Некорректное действие простоя.'}, status=400)

    reason = (
        DowntimeReason.for_workplace('excavator_operator', current_excavator.equipment_type)
        .filter(id=payload.get('reason_id'))
        .first()
    )
    if not reason:
        return JsonResponse({'ok': False, 'error': 'Причина простоя недоступна для экскаваторщика.'}, status=400)
    action_label = 'downtime_started'
    if active_event:
        if active_event.employee_id != access.employee_id:
            return JsonResponse(
                {
                    'ok': False,
                    'error': (
                        'Этот непрерывный простой начат предыдущим машинистом. '
                        'Сменщик может завершить его, но не менять причину или автора.'
                    ),
                    'code': 'transferred_downtime_read_only',
                },
                status=409,
            )
        if active_event.reason_id == reason.id:
            active_event.comment = (payload.get('comment') or '')[:255]
            active_event.save(update_fields=['comment'])
            event = active_event
            action_label = 'downtime_updated'
        else:
            switched_at = timezone.now()
            active_event.ended_at = switched_at
            active_event.save(update_fields=['ended_at'])
            event = DowntimeEvent.objects.create(
                equipment=current_excavator,
                employee=access.employee,
                reason=reason,
                started_at=switched_at,
                comment=(payload.get('comment') or '')[:255],
            )
            action_label = 'downtime_switched'
    else:
        event = DowntimeEvent.objects.create(
            equipment=current_excavator,
            employee=access.employee,
            reason=reason,
            started_at=timezone.now(),
            comment=(payload.get('comment') or '')[:255],
        )
    response_payload = downtime_event_payload(event, action=action_label)
    response_payload['active_counts_towards_shift'] = downtime_event_counts_towards_shift(
        event,
        open_shift,
    )
    response_payload.update(excavator_downtime_totals_payload(current_excavator, open_shift))
    return JsonResponse(response_payload)

def protect_dispatcher_equipment_detail_response(response):
    response['Cache-Control'] = 'private, no-store, max-age=0'
    response['Pragma'] = 'no-cache'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Vary'] = 'Cookie'
    return response


def dispatcher_equipment_detail_error(code, *, status):
    return protect_dispatcher_equipment_detail_response(JsonResponse(
        {
            'contract': 'dispatcher-equipment-detail-v1',
            'error': code,
        },
        status=status,
    ))


def dispatcher_downtime_close_response(payload, *, status=200):
    response_payload = {
        'contract': 'dispatcher-downtime-close-v1',
        **payload,
    }
    return protect_dispatcher_equipment_detail_response(
        JsonResponse(response_payload, status=status)
    )


@require_POST
@transaction.atomic
def dispatcher_close_downtime_view(request, event_id):
    return _execute_dispatcher_close_downtime(
        request,
        event_id,
        lock_mutation_access=lock_dispatcher_mutation_access,
        response_builder=dispatcher_downtime_close_response,
        event_payload=downtime_event_payload,
    )


@require_http_methods(['GET', 'POST'])
def dispatcher_equipment_detail_view(request, category, equipment_id):
    access_id = request.session.get('employee_access_id')
    access = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(
            id=access_id,
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
            role__is_active=True,
            employee__is_active=True,
        )
        .first()
        if access_id
        else None
    )
    if not access:
        return dispatcher_equipment_detail_error('authentication_required', status=401)
    if access.role.code not in {'dispatcher', 'admin', 'manager'}:
        return dispatcher_equipment_detail_error('forbidden', status=403)
    if category not in {'equipment', 'complex'}:
        return dispatcher_equipment_detail_error('card_not_found', status=404)
    state_version = None
    if request.method == 'GET':
        try:
            state_version = int(request.GET.get('state_version', ''))
        except (TypeError, ValueError):
            state_version = -1
        if state_version < 0:
            return dispatcher_equipment_detail_error('invalid_state_version', status=400)

    equipment = (
        Equipment.objects
        .filter(
            pk=equipment_id,
            is_active=True,
            equipment_type__name__in={'Самосвал', 'Экскаватор'},
        )
        .select_related('equipment_type')
        .first()
    )
    if not equipment:
        return dispatcher_equipment_detail_error('card_not_found', status=404)
    if category == 'complex':
        if equipment.equipment_type.name != 'Экскаватор':
            return dispatcher_equipment_detail_error('card_not_found', status=404)
        card_key = f'complex-equipment-{equipment.id}'
    else:
        card_key = str(equipment.pk)

    if request.method == 'POST':
        return _execute_dispatcher_equipment_settings(
            request,
            access,
            equipment,
            active_role_state=role_session_state,
            active_shift_getter=get_active_dispatcher_shift,
            json_payload=excavator_json_payload,
            parse_destinations=parse_excavator_destinations,
            normalize_numeric_setting=normalize_excavator_numeric_setting,
            lock_mutation_access=lock_dispatcher_mutation_access,
            error_response=dispatcher_equipment_detail_error,
            save_work_context=save_excavator_work_context,
            build_settings=dispatcher_excavator_settings,
            protect_response=protect_dispatcher_equipment_detail_response,
        )

    return dispatcher_control_view(
        request,
        access_override=access,
        equipment_detail={
            'card_key': card_key,
            'state_version': state_version,
        },
    )


def dispatcher_control_view(
    request,
    *,
    access_override=None,
    enforce_dispatcher_access=True,
    dispatcher_header_override=None,
    context_overrides=None,
    equipment_detail=None,
):
    requested_fragment = request.GET.get('_operational_fragment', '').strip()
    reconcile_due_haul_assignments()
    reconcile_expired_free_bucket_acceptances()
    reconcile_expired_manual_trips()
    # Просроченные смены закрывает сервер по таймеру (close_expired_shifts),
    # а не загрузка пульта: момент закрытия не должен зависеть от того, открыл
    # ли кто-то браузер.
    if access_override is None:
        access_id = request.session.get('employee_access_id')
        if not access_id:
            if equipment_detail:
                return dispatcher_equipment_detail_error('authentication_required', status=401)
            if requested_fragment in {'dispatcher', 'mining_master'}:
                return JsonResponse({'authenticated': False}, status=401)
            return redirect('login')
        access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    else:
        access = access_override
    if not access:
        if equipment_detail:
            return dispatcher_equipment_detail_error('authentication_required', status=401)
        return redirect('role_home')
    if enforce_dispatcher_access and access.role.code not in {'dispatcher', 'admin', 'manager'}:
        if equipment_detail:
            return dispatcher_equipment_detail_error('forbidden', status=403)
        return redirect('role_home')
    if equipment_detail:
        equipment_detail['state_version_before'] = get_operational_state_version()
        if equipment_detail['state_version_before'] != equipment_detail['state_version']:
            return dispatcher_equipment_detail_error('stale_board', status=409)
    read_model = build_dispatcher_control_read_model(
        request,
        access,
        dashboard_builder=build_dispatcher_dashboard_context,
        equipment_shift_is_current=mining_master_equipment_shift_is_current,
        dispatcher_header_override=dispatcher_header_override,
        context_overrides=context_overrides,
        equipment_detail=equipment_detail,
    )
    dispatcher_header = read_model.dispatcher_header
    dispatcher_dashboard = read_model.dispatcher_dashboard
    active_trips = read_model.active_trips
    pending_assignments = read_model.pending_assignments
    accepted_assignments = read_model.accepted_assignments
    recent_completed_trips = read_model.recent_completed_trips
    open_shifts = read_model.open_shifts
    open_mechanic_downtimes = read_model.open_mechanic_downtimes
    open_mechanic_downtimes_count = read_model.open_mechanic_downtimes_count
    trucks = read_model.trucks
    excavators = read_model.excavators
    recent_dispatcher_actions = read_model.recent_dispatcher_actions
    operational_state_version = get_operational_state_version()
    if equipment_detail:
        requested_version = equipment_detail['state_version']
        if (
            operational_state_version != equipment_detail['state_version_before']
            or operational_state_version != requested_version
        ):
            return dispatcher_equipment_detail_error(
                'state_changed',
                status=409,
            )
        card = dispatcher_dashboard['equipment_cards'].get(equipment_detail['card_key'])
        if not card:
            return dispatcher_equipment_detail_error('card_not_found', status=404)
        response = JsonResponse({
            'contract': 'dispatcher-equipment-detail-v1',
            'card_key': equipment_detail['card_key'],
            'operational_state_version': operational_state_version,
            'card': card,
        })
        return protect_dispatcher_equipment_detail_response(response)
    context = {
            'access': access,
            'dispatcher_header': dispatcher_header,
            'dispatcher_dashboard': dispatcher_dashboard,
            'dispatcher_page_title': 'Горный диспетчер',
            'dispatcher_compat_title': 'Диспетчерский пульт',
            'dispatcher_board_label': 'Горный диспетчер',
            'operational_state_version': operational_state_version,
            'dispatcher_shift_return_url': get_dispatcher_control_url(request),
            'server_now': timezone.now(),
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
            'filters': read_model.filters,
            'dispatcher_filter_items': read_model.dispatcher_filter_items,
        }
    if context_overrides:
        context.update(context_overrides)

    response = render(request, 'trips/dispatcher_control.html', context)
    if requested_fragment in {'dispatcher', 'mining_master'}:
        selector = '.dispatcher-board' if requested_fragment == 'dispatcher' else '.mm-mobile-shell'
        fragment_extra = {}
        if requested_fragment == 'mining_master':
            fragment_extra['equipment_cards'] = dispatcher_dashboard['equipment_cards']
        return operational_fragment_response(
            response,
            screen=requested_fragment,
            selector=selector,
            version=operational_state_version,
            extra=fragment_extra,
        )
    return response


@transaction.atomic
def dispatcher_toggle_shift_view(request):
    return _execute_dispatcher_toggle_shift(
        request,
        lock_mutation_access=lock_dispatcher_mutation_access,
        shared_start_authenticator=authenticate_dispatcher_shared_shift_start,
    )


@transaction.atomic
def dispatcher_service_close_shift_view(request, shift_id):
    return _execute_dispatcher_service_close_shift(
        request,
        shift_id,
        lock_mutation_access=lock_dispatcher_mutation_access,
        parse_shift_decimal=parse_excavator_shift_decimal,
        close_kind_normalizer=normalize_service_close_kind,
        finish_shift=finish_service_closed_shift,
        action_logger=log_dispatcher_action,
    )


@transaction.atomic
def dispatcher_cancel_assignment_view(request, assignment_id):
    return _execute_dispatcher_cancel_assignment(
        request,
        assignment_id,
        lock_mutation_access=lock_dispatcher_mutation_access,
        action_logger=log_dispatcher_action,
    )


@transaction.atomic
def dispatcher_cancel_trip_view(request, trip_id):
    return _execute_dispatcher_cancel_trip(
        request,
        trip_id,
        lock_mutation_access=lock_dispatcher_mutation_access,
        reconcile_excavator=reconcile_excavator_waiting_for_trucks,
        action_logger=log_dispatcher_action,
    )


# Полчаса после конца производственной смены: 19:30 для первой смены и 07:30
# для второй. Ранние комплексы (06:00-18:00) попадают в ту же отсечку.
EQUIPMENT_SHIFT_AUTO_CLOSE_GRACE = timedelta(minutes=30)
# Страховка только для смен, у которых период вообще не посчитался.
EQUIPMENT_SHIFT_AUTO_CLOSE_HARD_LIMIT = timedelta(hours=16)
# Часы, в которые отрубаются незакрытые смены техники.
EQUIPMENT_SHIFT_AUTO_CLOSE_HOURS = (8, 20)


def next_shift_auto_close_cutoff(moment):
    """Ближайшие 08:00 или 20:00 начиная с этого момента (часы предприятия)."""
    from datetime import datetime as _datetime, time as _time, timedelta as _timedelta
    from core.production_time import BUSINESS_TIME_ZONE, business_localtime

    local = business_localtime(moment)
    for day_shift in (0, 1):
        for hour in EQUIPMENT_SHIFT_AUTO_CLOSE_HOURS:
            candidate = _datetime.combine(
                local.date() + _timedelta(days=day_shift),
                _time(hour, 0),
                tzinfo=BUSINESS_TIME_ZONE,
            )
            if candidate >= local:
                return candidate
    return local


# Роли, у которых своё рабочее время, не совпадающее с производственной сменой
# техники. Часы роли меняются здесь одной строкой.
WORKPLACE_SHIFT_SCHEDULE = {
    'dispatcher': (8, 20),
    'mining_master': (8, 20),
}


def workplace_shift_period_end(shift):
    """Конец смены роли со своим расписанием (диспетчер, горный мастер).

    Окно определяем по времени открытия: смена, начатая днём, кончается вечером,
    начатая вечером — утром следующего дня, начатая ночью — этим же утром.
    Для техники вернётся None: у неё производственные часы.
    """
    from datetime import datetime as _datetime, time as _time, timedelta as _timedelta
    from core.production_time import BUSINESS_TIME_ZONE, business_localtime

    schedule = WORKPLACE_SHIFT_SCHEDULE.get(shift.workplace_code or '')
    if not schedule or not shift.opened_at:
        return None
    day_hour, night_hour = schedule
    local = business_localtime(shift.opened_at)
    opened_time = local.time().replace(tzinfo=None)
    day_start = _time(day_hour, 0)
    night_start = _time(night_hour, 0)
    if day_start <= opened_time < night_start:
        end_date, end_time = local.date(), night_start
    elif opened_time >= night_start:
        end_date, end_time = local.date() + _timedelta(days=1), day_start
    else:
        end_date, end_time = local.date(), day_start
    return _datetime.combine(end_date, end_time, tzinfo=BUSINESS_TIME_ZONE)


def shift_auto_close_at(shift):
    """Когда смена закроется сама: конец своей смены плюс полчаса."""
    if not shift or not shift.opened_at:
        return None
    period_end = workplace_shift_period_end(shift)
    if period_end is not None:
        # Диспетчер и горный мастер заканчивают ровно в отсечку, поэтому им
        # полчаса на сдачу дел, иначе пульт погаснет в момент пересменки.
        close_at = period_end + EQUIPMENT_SHIFT_AUTO_CLOSE_GRACE
    else:
        work_date = production_work_date_for_shift(shift.opened_at, shift.shift_type)
        try:
            _, period_end = production_shift_bounds(work_date, shift.shift_type)
        except (TypeError, ValueError):
            return shift.opened_at + EQUIPMENT_SHIFT_AUTO_CLOSE_HARD_LIMIT
        # Смена техники доживает до ближайшей отсечки после своего конца: у
        # первой смены это двадцать часов, у второй — восемь утра. Сменщика,
        # заступившего в восемь, отсечка этого же утра не касается.
        close_at = next_shift_auto_close_cutoff(period_end)
    # Страховку по времени здесь не применяем: она обрубала бы смену раньше её
    # законной отсечки (смена второй смены, открытая днём, закрывалась ночью).
    return close_at


# Прежнее имя оставлено: карточка техники зовёт его напрямую.
equipment_shift_auto_close_at = shift_auto_close_at


def auto_close_expired_equipment_shifts(now=None):
    """13 часов с открытия — смена техники закрывается сама как незакрытая сотрудником.

    Смена длится 12 часов; лишний час — запас, чтобы сотрудник без связи успел
    попросить диспетчера закрыть смену по согласованию. Вызывается таймером
    (close_expired_shifts) и при каждой загрузке пульта.
    """
    now = now or timezone.now()
    closed = []
    with transaction.atomic():
        expired = list(
            EmployeeShift.objects
            .select_for_update(of=('self',), skip_locked=True)
            .select_related('employee', 'equipment', 'equipment__equipment_type')
            .filter(
                Q(equipment__isnull=False) | Q(workplace_code__in=WORKPLACE_SHIFT_SCHEDULE),
                closed_at__isnull=True,
                opened_at__lte=now - EQUIPMENT_SHIFT_AUTO_CLOSE_GRACE,
            )
            .order_by('opened_at', 'id')
        )
        expired = [
            shift
            for shift in expired
            if (shift_auto_close_at(shift) or now) <= now
        ]
        for shift in expired:
            finish_service_closed_shift(
                shift,
                closed_by=None,
                close_kind=SERVICE_CLOSE_AUTO_EXPIRED,
                note=SERVICE_CLOSE_AUTO_NOTE,
                now=now,
            )
            closed.append(shift)
        # Осиротевшие ожидания: техника без открытой смены, а «ожидание
        # самосвалов» всё идёт. Ремонт и прочие состояния техники живут между
        # сменами — их не трогаем, как и ручные простои диспетчера.
        from downtimes.driver_workflow import is_workflow_downtime_reason
        from downtimes.models import DowntimeEvent, DowntimeEventSource
        orphan_candidates = (
            DowntimeEvent.objects
            .filter(ended_at__isnull=True)
            .exclude(source=DowntimeEventSource.DISPATCHER_OVERRIDE)
            .exclude(
                equipment_id__in=EmployeeShift.objects
                .filter(closed_at__isnull=True, equipment__isnull=False)
                .values('equipment_id')
            )
            .select_related('reason')
        )
        orphan_ids = [
            event.id
            for event in orphan_candidates
            if is_workflow_downtime_reason(event.reason)
        ]
        orphan_count = (
            DowntimeEvent.objects.filter(id__in=orphan_ids).update(ended_at=now)
            if orphan_ids else 0
        )
        if closed or orphan_count:
            bump_operational_state(
                'Shift:auto_close_expired',
                event_type='shift_changed',
                object_type='EmployeeShift',
                object_id=closed[-1].id if closed else 0,
                payload={
                    'action': 'auto_close_expired_shifts',
                    'shift_ids': [shift.id for shift in closed],
                    'equipment_ids': [shift.equipment_id for shift in closed],
                    'orphan_downtimes_closed': orphan_count,
                },
            )
    return closed


@transaction.atomic
def dispatcher_manual_trip_view(request, equipment_id):
    return _execute_dispatcher_manual_trip(
        request,
        equipment_id,
        lock_mutation_access=lock_dispatcher_mutation_access,
        format_datetime=format_dispatcher_datetime,
        action_logger=log_dispatcher_action,
    )


@transaction.atomic
def dispatcher_complete_trip_view(request, trip_id):
    return _execute_dispatcher_complete_trip(
        request,
        trip_id,
        lock_mutation_access=lock_dispatcher_mutation_access,
        finalize_trip=finalize_trip_unloaded,
        action_logger=log_dispatcher_action,
    )


def driver_complete_trip_view(request, trip_id):
    wants_json = 'application/json' in request.headers.get('Accept', '')

    def reject(message):
        if wants_json:
            return JsonResponse({'ok': False, 'conflict': True, 'error': message}, status=409)
        messages.error(request, message)
        return redirect('driver_shift')

    def accepted(trip):
        if wants_json:
            return JsonResponse({'ok': True, 'trip_id': trip.pk, 'client_action_id': client_action_id})
        return redirect('driver_shift')

    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code != 'driver':
        return redirect('role_home')
    if request.method != 'POST':
        return redirect('driver_shift')
    client_action_id = str(request.POST.get('client_action_id') or '').strip()
    if not client_action_id:
        messages.error(request, 'Не передан идентификатор действия. Обновите экран и повторите точковку.')
        return redirect('driver_shift')
    with transaction.atomic():
        lock_idempotency_key('trip_unloaded', client_action_id)
        existing_action = TripClientAction.objects.filter(
            action_type='trip_unloaded',
            client_action_id=client_action_id,
        ).first()
        if existing_action:
            if existing_action.actor_id != access.employee_id or existing_action.trip_id != trip_id:
                return reject('Идентификатор подтверждения принадлежит другому действию.')
            return accepted(existing_action.trip)
        Employee.objects.select_for_update().get(pk=access.employee_id)
        if not role_session_state(request, access)['is_active']:
            messages.error(request, 'Роль неактивна — доступен только просмотр.')
            return redirect('driver_shift')
        reference = Trip.objects.filter(pk=trip_id).first()
        if reference and reference.driver_participation_recorded:
            shift_query = Q(pk=reference.driver_control_shift_id, employee=access.employee)
        else:
            shift_query = Q(employee=access.employee, closed_at__isnull=True)
        unloading_shift = (
            EmployeeShift.objects
            .select_for_update(of=('self',))
            .filter(shift_query)
            .filter(
                Q(workplace_code='driver')
                | Q(workplace_code='', equipment__equipment_type__name='Самосвал')
            )
            .select_related('equipment')
            .order_by('-opened_at')
            .first()
        )
        if not unloading_shift or not unloading_shift.equipment_id:
            messages.error(request, 'Нельзя завершить рейс: открытая смена с самосвалом не найдена.')
            return redirect('driver_shift')
        # Тот же порядок, что у отправки: состояние производства, техника, рейс.
        lock_production_state()
        if reference and reference.truck_id == unloading_shift.equipment_id:
            lock_trip_participant_equipment(
                excavator_id=reference.excavator_id, truck_id=reference.truck_id,
            )
        trip = (
            Trip.objects
            .select_for_update()
            .filter(trip_driver_control_filter(unloading_shift))
            .filter(id=trip_id, truck=unloading_shift.equipment, status__in=(*OPEN_TRIP_STATUSES, TripStatus.UNCONTROLLED))
            .first()
        )
        if trip:
            raw_time = str(request.POST.get('occurred_at') or '').strip()
            try:
                occurred_at = parse_datetime(raw_time) if raw_time else None
            except ValueError:
                occurred_at = None
            late_confirmation = trip.status == TripStatus.UNCONTROLLED
            latest = trip.operationally_closed_at if late_confirmation else timezone.now() + timedelta(minutes=2)
            if raw_time and (occurred_at is None or timezone.is_naive(occurred_at)
                             or occurred_at < trip.created_at or occurred_at > latest):
                return reject('Время подтверждения не соответствует рейсу. Требуется сверка.')
            finalize_trip_unloaded(trip, driver=access.employee, unloading_shift=unloading_shift,
                                   occurred_at=occurred_at, late_confirmation=late_confirmation)
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
            return reject('Рейс не назначен этой смене, отменён или уже закрыт.')
    return accepted(trip)


def driver_change_unload_point_view(request, trip_id):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code != 'driver':
        return redirect('role_home')
    if request.method != 'POST':
        return redirect('driver_shift')

    client_action_id = str(request.POST.get('client_action_id') or '').strip()
    if not client_action_id:
        messages.error(request, 'Не передан идентификатор действия. Обновите экран и выберите точку снова.')
        return redirect('driver_shift')

    with transaction.atomic():
        lock_idempotency_key('change_actual_unload_point', client_action_id)
        existing_action = TripClientAction.objects.select_related('trip').filter(
            action_type='change_actual_unload_point',
            client_action_id=client_action_id,
        ).first()
        if existing_action:
            try:
                repeated_dump_point_id = int(request.POST.get('dump_point') or 0)
            except (TypeError, ValueError):
                repeated_dump_point_id = 0
            existing_point_id = (
                existing_action.trip.actual_dump_point_id
                or existing_action.trip.dump_point_id
            )
            if (
                existing_action.actor_id == access.employee_id
                and existing_action.trip_id == trip_id
                and repeated_dump_point_id == existing_point_id
            ):
                return redirect('driver_shift')
            messages.error(
                request,
                'Идентификатор действия уже использован для другого выбора. Обновите экран и повторите действие.',
            )
            return redirect('driver_shift')
        Employee.objects.select_for_update().get(pk=access.employee_id)
        if not role_session_state(request, access)['is_active']:
            messages.error(request, 'Роль неактивна — доступен только просмотр.')
            return redirect('driver_shift')
        unloading_shift = (
            EmployeeShift.objects
            .select_for_update(of=('self',))
            .filter(employee=access.employee, closed_at__isnull=True)
            .filter(
                Q(workplace_code='driver')
                | Q(workplace_code='', equipment__equipment_type__name='Самосвал')
            )
            .select_related('equipment')
            .order_by('-opened_at')
            .first()
        )
        if not unloading_shift or not unloading_shift.equipment_id:
            messages.error(request, 'Нельзя изменить точку: открытая смена с самосвалом не найдена.')
            return redirect('driver_shift')
        try:
            dump_point_id = int(request.POST.get('dump_point') or 0)
        except (TypeError, ValueError):
            dump_point_id = 0
        trip = (
            Trip.objects
            .select_for_update()
            .filter(trip_driver_control_filter(unloading_shift))
            .filter(id=trip_id, truck=unloading_shift.equipment, status__in=OPEN_TRIP_STATUSES)
            .first()
        )
        if not trip:
            messages.error(request, 'Активный рейс не найден или уже закрыт.')
            return redirect('driver_shift')
        from trips.free_bucket import free_bucket_snapshot_dump_points_for_trip

        free_bucket_dump_points = free_bucket_snapshot_dump_points_for_trip(trip)
        if free_bucket_dump_points is None:
            dump_point = DumpPoint.objects.filter(id=dump_point_id, is_active=True).first()
        else:
            allowed_ids = {item['id'] for item in free_bucket_dump_points}
            if dump_point_id not in allowed_ids:
                messages.error(
                    request,
                    'Точка разгрузки не входила в сохранённые настройки свободного ковша.',
                )
                return redirect('driver_shift')
            dump_point = DumpPoint.objects.filter(id=dump_point_id).first()
        if not dump_point:
            messages.error(request, 'Точка разгрузки не найдена.')
            return redirect('driver_shift')
        previous_dump_point_id = trip.actual_dump_point_id or trip.dump_point_id
        if previous_dump_point_id == dump_point.id:
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
                'previous_dump_point_id': previous_dump_point_id,
                'actual_dump_point_id': trip.actual_dump_point_id,
                'actor_id': access.employee_id,
                'occurred_at': timezone.now().isoformat(),
                'status': trip.status,
            },
        )
    return redirect('driver_shift')
