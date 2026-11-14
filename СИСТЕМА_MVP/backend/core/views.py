import json

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from shifts.models import EmployeeShift
from users.models import EmployeeAccess
from users.active_role import role_session_state
from users.context_processors import parse_native_app_marker
from users.live_monitor import PRESENCE_BACKGROUND, touch_application_session
from users.role_apps import (
    APP_CONTRACT_VERSION,
    get_role_app,
    get_role_app_for_request,
)

from .models import OperationalStateEvent
from .offline_sync import (
    OfflineEventProblem,
    SYNC_FORMAT_VERSION,
    process_offline_batch,
)
from .realtime import (
    relevant_event_page,
    selected_equipment_ids_for_access,
    worker_equipment_ids_for_access,
)


def parse_positive_int(value, default, maximum=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    if maximum is not None:
        return min(parsed, maximum)
    return parsed


def parse_bool_param(value, default=True):
    if value is None:
        return default
    return str(value).strip().lower() not in {'0', 'false', 'no', 'off'}


@require_POST
def offline_events_sync_view(request):
    """Accept a durable, partially acknowledged batch from one field device."""
    access_id = request.session.get('employee_access_id')
    access = (
        EmployeeAccess.objects
        .select_related('employee', 'role', 'employee__contractor_organization')
        .filter(pk=access_id)
        .first()
        if access_id else None
    )
    role_state = role_session_state(request, access)
    if not access or not role_state.get('authenticated') or not role_state.get('is_active'):
        return JsonResponse({
            'ok': False,
            'status': 'auth_required',
            'code': 'session_expired_or_inactive',
            'message': 'Сессия или активная роль изменилась. Требуется вход.',
            'server_received_at': timezone.now().isoformat(),
        }, status=401)
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({
            'ok': False,
            'status': 'invalid',
            'code': 'invalid_json',
            'message': 'Тело запроса должно быть корректным JSON.',
            'server_received_at': timezone.now().isoformat(),
        }, status=400)
    if not isinstance(body, dict):
        return JsonResponse({
            'ok': False,
            'status': 'invalid',
            'code': 'invalid_envelope',
            'message': 'Пакет offline-событий должен быть JSON-объектом.',
            'server_received_at': timezone.now().isoformat(),
        }, status=400)
    try:
        format_version = int(body.get('format_version', body.get('protocol_version', 0)))
    except (TypeError, ValueError):
        format_version = 0
    if format_version != SYNC_FORMAT_VERSION:
        return JsonResponse({
            'ok': False,
            'status': 'invalid',
            'code': 'unsupported_format_version',
            'message': 'Версия пакета offline-событий не поддерживается.',
            'server_received_at': timezone.now().isoformat(),
        }, status=400)
    role_code = str(body.get('role_code') or '').strip()
    claimed_actor_id = body.get('actor_id')
    claimed_access_id = body.get('access_id')
    if (
        claimed_actor_id not in (None, '', access.employee_id, str(access.employee_id))
        or claimed_access_id not in (None, '', access.id, str(access.id))
        or role_code != access.role.code
    ):
        return JsonResponse({
            'ok': False,
            'status': 'auth_required',
            'code': 'actor_or_role_changed',
            'message': 'Сотрудник, доступ или роль пакета не совпадают с активной сессией.',
            'server_received_at': timezone.now().isoformat(),
        }, status=401)
    try:
        results = process_offline_batch(
            access,
            role_code=role_code,
            device_id=body.get('device_id'),
            events=body.get('events'),
        )
    except OfflineEventProblem as problem:
        return JsonResponse({
            'ok': False,
            'status': problem.status,
            'code': problem.code,
            'message': problem.message,
            'retryable': problem.retryable,
            'server_received_at': timezone.now().isoformat(),
        }, status=400)
    return JsonResponse({
        'ok': True,
        'protocol_version': SYNC_FORMAT_VERSION,
        'format_version': SYNC_FORMAT_VERSION,
        'server_received_at': timezone.now().isoformat(),
        'results': results,
    })


@require_GET
def operational_state_version_view(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return JsonResponse({'authenticated': False}, status=401)

    access = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(
            id=access_id,
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
            employee__is_active=True,
            role__is_active=True,
        )
        .first()
    )
    if not access:
        return JsonResponse({'authenticated': False}, status=401)
    role_state = role_session_state(request, access)
    host_role_app = get_role_app_for_request(request)
    requested_role_app = get_role_app(request.GET.get('role_app_code', ''))
    role_app = host_role_app or requested_role_app or get_role_app(access.role.code)
    session_role_code = role_state.get('session_role_code', access.role.code)
    role_is_active_for_app = role_state['is_active'] and (
        role_app is None or session_role_code == role_app.role_code
    )
    background_role = role_app is not None and role_app.role_code in {
        'driver',
        'excavator_operator',
    }
    active_shift = None
    if background_role and role_is_active_for_app:
        active_shift = (
            EmployeeShift.objects
            .filter(
                employee_id=access.employee_id,
                workplace_code=role_app.role_code,
                closed_at__isnull=True,
            )
            .order_by('-opened_at', '-id')
            .only('id')
            .first()
        )
    has_active_shift = active_shift is not None
    background_connection_required = bool(
        background_role and role_is_active_for_app and has_active_shift
    )

    if role_is_active_for_app:
        native_app, native_version = parse_native_app_marker(request)
        if native_app and role_app:
            touch_application_session(
                request,
                reported_path=role_app.start_url,
                presence_kind=PRESENCE_BACKGROUND,
                client_kind='android_apk',
                client_version=native_version,
            )
        from assignments.services import reconcile_due_haul_assignments_throttled

        reconcile_due_haul_assignments_throttled()

        # Боевой случай 20.09.2026: без этого пассивный/осиротевший ручной
        # рейс гас только при ПОЛНОЙ перезагрузке страницы пульта и
        # экскаваторщика — сотрудники были бы обязаны перезагружаться
        # вручную. Опрос идёт со всех ролей, поэтому гасит сам, в фоне.
        from trips.manual_loading import reconcile_expired_manual_trips_throttled

        reconcile_expired_manual_trips_throttled()

    from .sse import build_cursor_context, operational_journal_status

    journal = operational_journal_status()
    after = parse_positive_int(request.GET.get('after'), 0)
    limit = parse_positive_int(request.GET.get('limit'), 50, maximum=200)
    include_events = parse_bool_param(request.GET.get('include_events'), True)
    events = []
    events_truncated = False
    state_version = int(journal['max_version'])
    journal_min_version = journal['min_version']
    reset_reason = ''
    if after > state_version:
        reset_reason = 'cursor_ahead'
    elif (
        after < state_version
        and (
            journal_min_version is None
            or after < int(journal_min_version) - 1
        )
    ):
        reset_reason = 'journal_gap'
    reset_required = bool(reset_reason)
    scanned_through_version = after
    if include_events and state_version > after and not reset_required:
        events_queryset = OperationalStateEvent.objects.filter(
            key='production',
            version__lte=state_version,
        )
        if after:
            events_queryset = events_queryset.filter(version__gt=after)
        events, events_truncated, scanned_version = relevant_event_page(
            events_queryset.order_by('version'),
            access,
            limit=limit,
        )
        if scanned_version is not None:
            scanned_through_version = scanned_version
    page_cursor = (
        after if reset_required
        else min(scanned_through_version, state_version)
    )
    caught_up = not reset_required and page_cursor >= state_version
    relevant = bool(events) or events_truncated
    worker_equipment_ids = (
        sorted(worker_equipment_ids_for_access(access))
        if role_is_active_for_app and access.role.code in {'driver', 'excavator_operator'}
        else []
    )
    payload = {
        'authenticated': True,
        'role_active': role_is_active_for_app,
        'has_active_shift': has_active_shift,
        'active_shift_id': str(active_shift.id) if active_shift else '',
        'background_connection_required': background_connection_required,
        'active_role_code': role_state.get('active_role_code', ''),
        'active_role_changed_at': (
            role_state['active_role_changed_at'].isoformat()
            if role_state.get('active_role_changed_at')
            else ''
        ),
        'session_role_code': session_role_code,
        'session_revision': role_state.get('session_revision', ''),
        'app_contract_version': APP_CONTRACT_VERSION,
        'role_shell_version': role_app.shell_version if role_app else '',
        'role_app_code': role_app.role_code if role_app else access.role.code,
        'worker_equipment_ids': worker_equipment_ids,
        'key': 'production',
        'version': state_version,
        'events': events,
        'events_truncated': events_truncated,
        'relevant': relevant if include_events else None,
        'page_cursor': page_cursor,
        'scanned_through_version': page_cursor,
        'high_water_version': state_version,
        'caught_up': caught_up,
        'reset_required': reset_required,
        'snapshot_required': reset_required,
        'reset_reason': reset_reason,
        'reset_to_version': state_version if reset_required else None,
    }
    if (
        reset_required
        and role_is_active_for_app
        and role_app is not None
        and role_app.role_code in {'driver', 'excavator_operator'}
    ):
        from .voice_state import build_current_voice_state

        payload['current_voice_state'] = build_current_voice_state(
            access,
            role_app.role_code if role_app else access.role.code,
            observed_at_version=state_version,
        )
    if access.role.code in {'driver', 'excavator_operator'}:
        # Additive metadata shared by foreground SSE and native heartbeat. Old
        # APKs ignore it; new clients partition durable cursors by the actual
        # user/role/technique scope and journal generation.
        cursor_context = build_cursor_context(
            access=access,
            role_app_code=role_app.role_code if role_app else access.role.code,
            worker_equipment_ids=worker_equipment_ids,
            cursor_equipment_ids=selected_equipment_ids_for_access(access),
            active_shift_id=active_shift.id if active_shift else '',
            session_revision=role_state.get('session_revision', ''),
        )
        payload.update({
            'journal_epoch': journal['epoch'],
            'context': cursor_context,
            'journal': {
                'generation': journal['epoch'],
                'epoch': journal['epoch'],
                'min_version': journal['min_version'],
                'max_version': journal['max_version'],
            },
        })
    if state_version > after:
        payload.update({
            'reason': journal['reason'],
            'updated_at': journal['updated_at'],
        })
    return JsonResponse(payload)
