"""Серверные источники хронологии горного диспетчера."""

from contextlib import contextmanager
from uuid import UUID

from django.db import connection, transaction
from django.db.models import F, Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.http import require_POST

from users.active_role import role_session_state
from users.live_monitor import BACKGROUND_ONLINE_WINDOW, ONLINE_WINDOW

from .dispatcher_header import dispatcher_shift_queryset
from .models import DispatcherActionLog, DispatcherDutyTracking, DispatcherDutyWindow


WindowState = DispatcherDutyWindow.State
WINDOW_VALIDITY = {WindowState.VISIBLE: ONLINE_WINDOW, WindowState.HIDDEN: BACKGROUND_ONLINE_WINDOW}
SESSION_LOGOUT = UUID(int=0)
FOREGROUND_HEARTBEAT = UUID(int=1)
DUTY_PAGE_PREFIXES = ('/dispatcher/', '/reports/')


def recorder_shift_id(request, state, *, page_path=None):
    if (not state.get('authenticated') or not state.get('is_active')
            or state.get('active_role_code') != 'dispatcher' or getattr(request, 'observer_mode', False)
            or not (request.path if page_path is None else page_path).startswith(DUTY_PAGE_PREFIXES)):
        return None
    return dispatcher_shift_queryset().filter(employee_id=state['access'].employee_id).values_list('pk', flat=True).first()


def session_digest(request):
    key = request.session.session_key
    return salted_hmac('dispatcher-duty-session-v1', key, algorithm='sha256').hexdigest() if key else ''


@contextmanager
def duty_write_transaction():
    with transaction.atomic():
        if connection.vendor == 'sqlite':
            # SQLite ignores SELECT FOR UPDATE. Acquire its writer lock before
            # reading so simultaneous beacons wait instead of racing to upgrade.
            DispatcherDutyTracking.objects.filter(pk=1).update(started_at=F('started_at'))
        yield


def record_window(request, state):
    if getattr(request, 'observer_mode', False):
        return 403
    auth = role_session_state(request)
    if not auth['authenticated'] or not auth['is_active'] or auth['access'].role.code != 'dispatcher':
        return 403
    try:
        window_id = UUID(request.POST.get('duty_window', ''))
        shift_id = int(request.POST.get('duty_shift', ''))
        sequence = int(request.POST.get('duty_sequence', ''))
    except (ValueError, TypeError, AttributeError):
        return 400
    digest = session_digest(request)
    if not digest or window_id in (SESSION_LOGOUT, FOREGROUND_HEARTBEAT) or not 0 < sequence < 2**53 or not 0 < shift_id < 2**63:
        return 400
    return save_window(auth['access'].employee_id, shift_id, digest, window_id, sequence, state)


def record_foreground_heartbeat(request):
    path = request.POST.get('path', '')
    if not path.startswith(DUTY_PAGE_PREFIXES):
        return
    if request.POST.get('duty_shift'):
        return record_window(request, WindowState.VISIBLE)
    # Older open pages already send this heartbeat only while visible. Use it
    # until this session supplies its first detailed window observation.
    auth = getattr(request, 'role_session_state', None) or role_session_state(request)
    shift_id = recorder_shift_id(request, auth, page_path=path)
    if not shift_id:
        return
    digest = session_digest(request)
    if digest:
        save_window(auth['access'].employee_id, shift_id, digest, FOREGROUND_HEARTBEAT, None, WindowState.VISIBLE)


@duty_write_transaction()
def save_window(employee_id, shift_id, digest, window_id, sequence, state):
    shift = (dispatcher_shift_queryset().select_for_update(of=('self',))
             .filter(pk=shift_id, employee_id=employee_id).first())
    if not shift:
        return 409
    # Serialize tabs and logout against the same duty. A late beacon cannot
    # reopen a retired session or overwrite a newer visibility transition.
    session_periods = DispatcherDutyWindow.objects.filter(shift=shift, session_digest=digest)
    if window_id == FOREGROUND_HEARTBEAT:
        if session_periods.exclude(window_id=FOREGROUND_HEARTBEAT).exists():
            return 204
    elif session_periods.filter(window_id=SESSION_LOGOUT).exists():
        return 409
    periods = session_periods.filter(window_id=window_id)
    previous = periods.order_by('-pk').first()
    if window_id == FOREGROUND_HEARTBEAT:
        sequence = previous.last_sequence + 1 if previous else 1
    if previous and (previous.last_sequence >= sequence or previous.state == WindowState.SIGNED_OUT):
        return 204
    now = timezone.now()
    if (previous and previous.state == state and
            (state not in WINDOW_VALIDITY or now - previous.last_seen_at <= WINDOW_VALIDITY[state])):
        previous.last_seen_at = now
        previous.last_sequence = sequence
        previous.save(update_fields=['last_seen_at', 'last_sequence'])
    else:
        periods.create(shift=shift, session_digest=digest, window_id=window_id, state=state,
                       started_at=now, last_seen_at=now, last_sequence=sequence)
    return 204


@require_POST
def background_presence_view(request):
    state = request.POST.get('duty_state')
    if state not in (WindowState.HIDDEN, WindowState.CLOSED):
        return HttpResponse(status=400)
    return HttpResponse(status=record_window(request, state))


def record_dispatcher_logout(request):
    if not request.session.get('employee_access_id') or getattr(request, 'observer_mode', False):
        return
    auth = getattr(request, 'role_session_state', None) or role_session_state(request)
    if not auth.get('authenticated') or not auth.get('is_active') or auth.get('active_role_code') != 'dispatcher':
        return
    digest = session_digest(request)
    shifts = dispatcher_shift_queryset().filter(employee_id=auth['access'].employee_id)
    with duty_write_transaction():
        for shift in shifts.select_for_update(of=('self',)):
            now = timezone.now()
            DispatcherDutyWindow.objects.get_or_create(
                shift=shift, session_digest=digest, window_id=SESSION_LOGOUT,
                defaults=dict(state=WindowState.SIGNED_OUT, started_at=now, last_seen_at=now, last_sequence=0),
            )


def log_dispatcher_action(*, actor, action_type, target_summary, trip=None, shift=None, haul_assignment=None,
                          reason='', request=None, access=None, duty_detail=''):
    role = access.role.code if access else ''
    if request is not None and getattr(request, 'observer_mode', False):
        role = 'observer'
    duty = None
    if role == 'dispatcher':
        now = timezone.now()
        duty = (dispatcher_shift_queryset(open_only=False).filter(employee=actor, opened_at__lte=now)
                .filter(Q(closed_at__isnull=True) | Q(closed_at__gte=now)).order_by('-opened_at').first())
    return DispatcherActionLog.objects.create(
        actor=actor, action_type=action_type, target_summary=target_summary, trip=trip, shift=shift,
        haul_assignment=haul_assignment, reason=str(reason or '').strip(), actor_role_code=role,
        duty_shift=duty, duty_detail=duty_detail if role == 'dispatcher' else '',
    )
