import asyncio
import hashlib
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import close_old_connections, connection
from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone

from shifts.models import EmployeeShift
from users.active_role import role_session_state
from users.models import EmployeeAccess
from users.role_apps import get_role_app, get_role_app_for_request

from .models import OperationalStateEvent, OperationalStateVersion
from .realtime import (
    collapse_duplicate_business_events,
    event_is_relevant,
    selected_equipment_ids_for_access,
    serialize_event,
    worker_equipment_ids_for_access,
)
from .sse_wakeup import RedisWakeSubscription


SSE_EVENT_KEY = 'production'

# Fail-closed defaults mirroring the pilot settings block. ``config/settings.py``
# is not part of this transfer, so every SSE tunable must have a safe value and
# stay overridable through ``override_settings`` in tests.
SSE_DEFAULTS = {
    'SSE_ALLOWED_ROLE_CODES': frozenset({'driver', 'excavator_operator'}),
    'SSE_HEARTBEAT_SECONDS': 10,
    'SSE_SAFETY_SCAN_SECONDS': 5,
    'SSE_REAUTH_SECONDS': 10,
    'SSE_STREAM_MAX_SECONDS': 900,
    'SSE_BATCH_SIZE': 200,
}


def sse_setting(name):
    return getattr(settings, name, SSE_DEFAULTS[name])


class StreamRejected(Exception):
    def __init__(self, code, *, status=409, payload=None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.payload = payload or {}


def build_cursor_context(
    *, access, role_app_code, worker_equipment_ids, active_shift_id, session_revision,
    cursor_equipment_ids=None,
):
    worker_equipment_ids = sorted(int(value) for value in worker_equipment_ids)
    cursor_equipment_ids = sorted(
        int(value)
        for value in (
            worker_equipment_ids
            if cursor_equipment_ids is None
            else cursor_equipment_ids
        )
    )
    scope_source = {
        'access_id': access.id,
        'employee_id': access.employee_id,
        'role_app_code': role_app_code,
        'cursor_equipment_ids': cursor_equipment_ids,
        'session_revision': str(session_revision or ''),
    }
    return {
        'access_id': access.id,
        'employee_id': access.employee_id,
        'role_app_code': role_app_code,
        'worker_equipment_ids': worker_equipment_ids,
        'cursor_equipment_ids': cursor_equipment_ids,
        'session_revision': str(session_revision or ''),
        'active_shift_id': str(active_shift_id or ''),
        'cursor_scope': hashlib.sha256(
            json.dumps(scope_source, sort_keys=True, separators=(',', ':')).encode('utf-8')
        ).hexdigest(),
    }


def _parse_cursor_value(value, source):
    if value in (None, ''):
        return 0
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise StreamRejected('invalid_cursor', status=400, payload={'cursor_source': source}) from exc
    if parsed < 0:
        raise StreamRejected('invalid_cursor', status=400, payload={'cursor_source': source})
    return parsed


def applied_cursor_from_request(request):
    """Explicit processed cursor always wins over EventSource's received id."""
    if 'applied' in request.GET:
        return _parse_cursor_value(request.GET.get('applied'), 'applied'), 'applied'
    if 'after' in request.GET:
        return _parse_cursor_value(request.GET.get('after'), 'after'), 'after'
    return (
        _parse_cursor_value(request.META.get('HTTP_LAST_EVENT_ID'), 'last_event_id'),
        'last_event_id',
    )


def encode_sse(event_name, payload, *, event_id=None, retry=None):
    lines = []
    if event_id is not None:
        lines.append(f'id: {int(event_id)}')
    if retry is not None:
        lines.append(f'retry: {int(retry)}')
    lines.append(f'event: {event_name}')
    data = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), default=str)
    for line in data.splitlines() or ['']:
        lines.append(f'data: {line}')
    return ('\n'.join(lines) + '\n\n').encode('utf-8')


def _fresh_session_data(request):
    session_key = request.session.session_key
    if not session_key:
        return {}
    store = request.session.__class__(session_key=session_key)
    return store.load()


def refresh_stream_database_snapshot():
    """Make a long-lived stream observe commits made by other processes.

    PostgreSQL autocommit gives every ORM statement a fresh READ COMMITTED
    snapshot, so normal Django connection hygiene is sufficient there. A
    long-lived SQLite connection can retain an old read view across ASGI
    generator resumes; reopen it at the scan boundary. The stream never calls
    this inside an application transaction.
    """
    if connection.in_atomic_block:
        raise RuntimeError('SSE database refresh cannot run inside atomic block')
    close_old_connections()
    if connection.vendor == 'sqlite':
        connection.close()


def _resolve_stream_context(request):
    session_data = _fresh_session_data(request)
    access_id = session_data.get('employee_access_id')
    if not access_id:
        raise StreamRejected('authentication_required', status=401)

    access = (
        EmployeeAccess.objects
        .select_related('employee', 'role', 'employee__contractor_organization')
        .filter(pk=access_id)
        .first()
    )
    request_proxy = SimpleNamespace(
        session=session_data,
        observer_mode=False,
        observer_access=None,
    )
    role_state = role_session_state(request_proxy, access)
    if not role_state.get('authenticated') or not access:
        raise StreamRejected('authentication_required', status=401)

    host_role_app = get_role_app_for_request(request)
    requested_role_app = get_role_app(request.GET.get('role_app_code', '').strip())
    role_app = host_role_app or requested_role_app or get_role_app(access.role.code)
    allowed_roles = set(sse_setting('SSE_ALLOWED_ROLE_CODES'))
    if role_app is None or role_app.role_code not in allowed_roles:
        raise StreamRejected('role_not_in_pilot', status=404)

    session_role_code = role_state.get('session_role_code', access.role.code)
    role_active = bool(
        role_state.get('is_active')
        and session_role_code == role_app.role_code
        and access.role.code == role_app.role_code
    )
    if not role_active:
        raise StreamRejected(
            'role_context_inactive',
            payload={
                'authenticated': True,
                'role_active': False,
                'role_app_code': role_app.role_code,
                'session_revision': role_state.get('session_revision', ''),
            },
        )

    active_shifts = list(
        EmployeeShift.objects.filter(
            employee_id=access.employee_id,
            workplace_code=role_app.role_code,
            closed_at__isnull=True,
        )
        .order_by('-opened_at', '-id')
        .only('id', 'equipment_id')
    )
    own_equipment_ids = sorted(selected_equipment_ids_for_access(access))
    worker_equipment_ids = sorted(worker_equipment_ids_for_access(access))
    active_shift = active_shifts[0] if active_shifts else None
    session_revision = str(role_state.get('session_revision', ''))
    cursor_context = build_cursor_context(
        access=access,
        role_app_code=role_app.role_code,
        worker_equipment_ids=worker_equipment_ids,
        cursor_equipment_ids=own_equipment_ids,
        active_shift_id=active_shift.id if active_shift else '',
        session_revision=session_revision,
    )
    snapshot_query = {
        '_operational_fragment': (
            'driver' if role_app.role_code == 'driver' else 'excavator'
        ),
        '_operational_snapshot': '1',
    }
    return {
        'access': access,
        'access_id': access.id,
        'employee_id': access.employee_id,
        'role_app_code': role_app.role_code,
        'role_active': True,
        'session_revision': session_revision,
        'active_shift_id': str(active_shift.id) if active_shift else '',
        'has_active_shift': bool(active_shift),
        'background_connection_required': bool(active_shift),
        'own_equipment_ids': own_equipment_ids,
        'worker_equipment_ids': worker_equipment_ids,
        'cursor_scope': cursor_context['cursor_scope'],
        'snapshot_url': f'{role_app.start_url}?{urlencode(snapshot_query)}',
    }


def operational_journal_status():
    state = OperationalStateVersion.objects.filter(key=SSE_EVENT_KEY).first()
    high_water = state.version if state else 0
    bounds = list(
        OperationalStateEvent.objects.filter(
            key=SSE_EVENT_KEY,
            version__lte=high_water,
        )
        .order_by('version')
        .values_list('version', flat=True)[:1]
    )
    reverse_bounds = list(
        OperationalStateEvent.objects.filter(
            key=SSE_EVENT_KEY,
            version__lte=high_water,
        )
        .order_by('-version')
        .values_list('version', flat=True)[:1]
    )
    return {
        'epoch': str(state.journal_epoch) if state else '',
        'min_version': bounds[0] if bounds else None,
        'max_version': high_water,
        'event_max_version': reverse_bounds[0] if reverse_bounds else None,
        'reason': state.reason if state else '',
        'updated_at': state.updated_at.isoformat() if state else '',
    }


def _public_context(context):
    return build_cursor_context(
        access=context['access'],
        role_app_code=context['role_app_code'],
        worker_equipment_ids=context['worker_equipment_ids'],
        cursor_equipment_ids=context['own_equipment_ids'],
        active_shift_id=context['active_shift_id'],
        session_revision=context['session_revision'],
    )


def _base_payload(context, journal, *, version=None):
    return {
        'authenticated': True,
        'role_active': context['role_active'],
        'has_active_shift': context['has_active_shift'],
        'background_connection_required': context['background_connection_required'],
        'role_app_code': context['role_app_code'],
        'worker_equipment_ids': context['worker_equipment_ids'],
        'session_revision': context['session_revision'],
        'journal_epoch': journal['epoch'],
        'context': _public_context(context),
        'journal': {
            'generation': journal['epoch'],
            'epoch': journal['epoch'],
            'min_version': journal['min_version'],
            'max_version': journal['max_version'],
        },
        'version': journal['max_version'] if version is None else int(version),
    }


def _reset_payload(context, journal, reason, *, authenticated=True, snapshot_required=True):
    if context is None:
        return {
            'authenticated': authenticated,
            'role_active': False,
            'version': journal.get('max_version', 0) if journal else 0,
            'reason': reason,
            'snapshot_required': snapshot_required,
        }
    payload = _base_payload(context, journal)
    payload.update({
        'reason': reason,
        'snapshot_required': snapshot_required,
        'snapshot_url': context['snapshot_url'],
        'events': [],
        'events_truncated': True,
        'relevant': True,
    })
    return payload


def _read_page(context, cursor, batch_size):
    journal = operational_journal_status()
    high_water = int(journal['max_version'])
    if cursor > high_water:
        return 'reset', _reset_payload(context, journal, 'cursor_ahead'), cursor
    if cursor == high_water:
        if journal['event_max_version'] not in (None, high_water):
            return 'reset', _reset_payload(context, journal, 'journal_state_mismatch'), cursor
        return 'caught_up', journal, cursor

    rows = list(
        OperationalStateEvent.objects.filter(
            key=SSE_EVENT_KEY,
            version__gt=cursor,
            version__lte=high_water,
        ).order_by('version')[:batch_size]
    )
    if not rows or rows[0].version != cursor + 1:
        return 'reset', _reset_payload(context, journal, 'journal_gap'), cursor
    expected = cursor + 1
    for row in rows:
        if row.version != expected:
            return 'reset', _reset_payload(context, journal, 'journal_gap'), cursor
        expected += 1

    page_end = rows[-1].version
    collapsed = collapse_duplicate_business_events(rows)
    equipment_ids = set(context['worker_equipment_ids'])
    driver_shift_ids = (
        {int(context['active_shift_id'])}
        if context['role_app_code'] == 'driver' and context['active_shift_id']
        else None
    )
    relevant_rows = [
        event for event in collapsed
        if event_is_relevant(
            event,
            context['access'],
            equipment_ids=equipment_ids,
            driver_shift_ids=driver_shift_ids,
        )
    ]
    payload = _base_payload(context, journal, version=page_end)
    payload.update({
        'events': [serialize_event(event) for event in relevant_rows],
        'events_truncated': False,
        'relevant': bool(relevant_rows),
        'page_final': page_end == high_water,
        'caught_up': page_end == high_water,
        'high_water_version': high_water,
        'reason': journal['reason'],
        'updated_at': journal['updated_at'],
    })
    return 'page', payload, page_end


async def operational_event_stream(
    request,
    *,
    initial_context,
    cursor,
    subscriber_factory=RedisWakeSubscription,
):
    subscription = subscriber_factory()
    started = time.monotonic()
    heartbeat_seconds = sse_setting('SSE_HEARTBEAT_SECONDS')
    safety_seconds = sse_setting('SSE_SAFETY_SCAN_SECONDS')
    reauth_seconds = sse_setting('SSE_REAUTH_SECONDS')
    next_heartbeat = started
    next_safety = started
    next_reauth = started
    context = initial_context
    initial_scope = initial_context['cursor_scope']
    initial_epoch = None

    try:
        # Critical ordering: Redis acknowledgement first, DB replay second. An
        # event committed anywhere in this interval is either in replay or in
        # the already-active subscription (usually both, then deduped by version).
        await subscription.connect()

        while time.monotonic() - started < sse_setting('SSE_STREAM_MAX_SECONDS'):
            now = time.monotonic()
            await sync_to_async(
                refresh_stream_database_snapshot,
                thread_sensitive=True,
            )()
            # Re-authorize before every post-wakeup scan, not only on a timer.
            # An assignment can change the effective technique scope in the
            # same commit that woke this stream.
            try:
                context = await sync_to_async(_resolve_stream_context, thread_sensitive=True)(request)
            except StreamRejected as rejected:
                yield encode_sse(
                    'reset',
                    _reset_payload(
                        None,
                        {'max_version': cursor},
                        rejected.code,
                        authenticated=rejected.status != 401,
                        snapshot_required=False,
                    ),
                )
                return
            if context['cursor_scope'] != initial_scope:
                journal = await sync_to_async(operational_journal_status, thread_sensitive=True)()
                yield encode_sse(
                    'reset',
                    _reset_payload(context, journal, 'role_context_changed'),
                )
                return
            next_reauth = now + reauth_seconds

            journal = await sync_to_async(operational_journal_status, thread_sensitive=True)()
            if initial_epoch is None:
                initial_epoch = journal['epoch']
            elif journal['epoch'] != initial_epoch:
                yield encode_sse(
                    'reset',
                    _reset_payload(context, journal, 'journal_epoch_changed'),
                )
                return

            # Drain all currently committed pages before waiting for another
            # wakeup. Each page advances through irrelevant global events too.
            while True:
                await sync_to_async(
                    refresh_stream_database_snapshot,
                    thread_sensitive=True,
                )()
                action, payload, new_cursor = await sync_to_async(
                    _read_page,
                    thread_sensitive=True,
                )(context, cursor, sse_setting('SSE_BATCH_SIZE'))
                if action == 'reset':
                    yield encode_sse('reset', payload)
                    return
                if action == 'caught_up':
                    journal = payload
                    break
                cursor = new_cursor
                yield encode_sse('operational', payload, event_id=cursor)

                # A large replay can stay inside this inner loop for much
                # longer than the normal wake/wait cadence. Re-check every
                # security and lifetime boundary before reading the next page;
                # ``cursor`` already points at the page successfully emitted,
                # so a reset/reconnect resumes without losing that progress.
                now = time.monotonic()
                if now - started >= sse_setting('SSE_STREAM_MAX_SECONDS'):
                    return
                await sync_to_async(
                    refresh_stream_database_snapshot,
                    thread_sensitive=True,
                )()
                try:
                    context = await sync_to_async(
                        _resolve_stream_context,
                        thread_sensitive=True,
                    )(request)
                except StreamRejected as rejected:
                    yield encode_sse(
                        'reset',
                        _reset_payload(
                            None,
                            {'max_version': cursor},
                            rejected.code,
                            authenticated=rejected.status != 401,
                            snapshot_required=False,
                        ),
                    )
                    return
                if context['cursor_scope'] != initial_scope:
                    journal = await sync_to_async(
                        operational_journal_status,
                        thread_sensitive=True,
                    )()
                    yield encode_sse(
                        'reset',
                        _reset_payload(context, journal, 'role_context_changed'),
                    )
                    return
                next_reauth = now + reauth_seconds

                journal = await sync_to_async(
                    operational_journal_status,
                    thread_sensitive=True,
                )()
                if journal['epoch'] != initial_epoch:
                    yield encode_sse(
                        'reset',
                        _reset_payload(context, journal, 'journal_epoch_changed'),
                    )
                    return

            now = time.monotonic()
            if now >= next_heartbeat:
                heartbeat = _base_payload(context, journal)
                heartbeat.update({
                    'events': [],
                    'events_truncated': False,
                    'relevant': False,
                    'server_time': timezone.now().isoformat(),
                })
                # A named event with data is visible to EventSource JavaScript;
                # an SSE comment would not satisfy the liveness contract.
                yield encode_sse('heartbeat', heartbeat, retry=3000)
                next_heartbeat = now + heartbeat_seconds

            timeout = max(
                0.05,
                min(next_safety, next_heartbeat, next_reauth) - time.monotonic(),
            )
            await subscription.wait(timeout)
            now = time.monotonic()
            if now >= next_safety:
                next_safety = now + safety_seconds
    except (asyncio.CancelledError, GeneratorExit, ConnectionError):
        return
    finally:
        await subscription.close()


async def operational_state_stream_view(request):
    if request.method != 'GET':
        return JsonResponse({'ok': False, 'error': 'method_not_allowed'}, status=405)
    if not getattr(settings, 'SSE_PILOT_ENABLED', False):
        return JsonResponse(
            {'ok': False, 'sse_enabled': False, 'error': 'sse_pilot_disabled'},
            status=404,
        )
    try:
        cursor, cursor_source = applied_cursor_from_request(request)
        context = await sync_to_async(_resolve_stream_context, thread_sensitive=True)(request)
    except StreamRejected as rejected:
        return JsonResponse(
            {'ok': False, 'error': rejected.code, **rejected.payload},
            status=rejected.status,
        )

    response = StreamingHttpResponse(
        operational_event_stream(
            request,
            initial_context=context,
            cursor=cursor,
        ),
        content_type='text/event-stream; charset=utf-8',
    )
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response['X-Accel-Buffering'] = 'no'
    response['X-SSE-Cursor-Source'] = cursor_source
    response['X-Content-Type-Options'] = 'nosniff'
    return response
