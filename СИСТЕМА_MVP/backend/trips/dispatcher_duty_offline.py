"""Synchronize timestamped observations from a dispatcher's own offline journal."""
import json
from datetime import datetime, timedelta, timezone as datetime_timezone
from uuid import UUID

from django.core import signing
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from users.active_role import role_session_state

from .dispatcher_duty import (FOREGROUND_HEARTBEAT, SESSION_LOGOUT, WINDOW_VALIDITY, WindowState, duty_write_transaction,
                              recorder_shift_id, session_digest)
from .dispatcher_header import dispatcher_shift_queryset
from .models import DispatcherDutyWindow


GRANT_SALT = 'dispatcher-duty-observations-v1'
MAX_BATCH = 64
EPOCH = datetime(1970, 1, 1, tzinfo=datetime_timezone.utc)
OBSERVED_STATES = {WindowState.VISIBLE, WindowState.HIDDEN, WindowState.CLOSED}


def is_dispatcher(request, state):
    return (state.get('authenticated') and state.get('is_active')
            and state.get('active_role_code') == 'dispatcher' and not getattr(request, 'observer_mode', False))


def recorder_context(request, state):
    if not is_dispatcher(request, state):
        return None
    employee = state['access'].employee_id
    context = {'owner': str(employee)}
    shift = recorder_shift_id(request, state)
    session = session_digest(request)
    if shift and session:
        issued = int(timezone.now().timestamp() * 1000)
        context.update(server_ms=str(issued), ticket=signing.dumps([employee, shift, session, issued], salt=GRANT_SALT))
    return context


def parse_observations(raw, issued, now):
    events = json.loads(raw)
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_BATCH:
        raise ValueError
    latest_ms = int(now.timestamp() * 1000)
    samples = []
    previous_ms = issued
    for entry in events:
        if not isinstance(entry, list) or len(entry) not in (3, 4):
            raise ValueError
        seq, ms, state = entry[:3]
        if (type(seq) is not int or not 0 < seq < 2**53 or type(ms) is not int
                or not issued <= ms <= latest_ms + 5000 or state not in OBSERVED_STATES):
            raise ValueError
        if ms < previous_ms or (samples and seq != samples[-1][0] + 1):
            raise ValueError
        previous_ms = ms
        if len(entry) == 4:
            wall_ms = entry[3]
            if type(wall_ms) is not int:
                raise ValueError
            if ms <= wall_ms <= latest_ms + 5000:
                ms = wall_ms
        samples.append((seq, EPOCH + timedelta(milliseconds=min(ms, latest_ms)), state))
    return samples


@duty_write_transaction()
def accept_observations(employee, shift_id, digest, window, samples, now, login_cutoff):
    # All writers for this shift (including logout) acquire the same lock.
    shift = (dispatcher_shift_queryset(open_only=False).select_for_update(of=('self',))
             .filter(pk=shift_id, employee_id=employee).first())
    if not shift:
        return HttpResponse(status=403)
    session = DispatcherDutyWindow.objects.filter(shift=shift, session_digest=digest)
    logout = session.filter(window_id=SESSION_LOGOUT).values_list('started_at', flat=True).first()
    cutoff = min((at for at in (shift.closed_at, logout, login_cutoff) if at is not None), default=None)
    retired = cutoff is not None and cutoff <= now
    previous = session.filter(window_id=window).order_by('-pk').first()
    ack = previous.last_sequence if previous else 0
    current = previous
    created = []
    updated = False
    for seq, at, state in samples:
        if seq <= ack:
            continue
        # The original shift or session may have ended while the queue was offline.
        if retired and at >= cutoff:
            ack = seq
            continue
        if seq != ack + 1:
            break
        at = max(at, shift.opened_at)
        if current:
            # Keep ordering on equal millisecond stamps and on a small clock correction.
            at = max(at, current.last_seen_at)
        if (current and current.state == state
                and (state == WindowState.CLOSED or at - current.last_seen_at <= WINDOW_VALIDITY[state])):
            current.last_seen_at = at
            current.last_sequence = seq
            if current is previous:
                updated = True
        else:
            current = DispatcherDutyWindow(shift=shift, session_digest=digest, window_id=window,
                                          state=state, started_at=at, last_seen_at=at, last_sequence=seq)
            created.append(current)
        ack = seq
    if updated:
        previous.save(update_fields=['last_seen_at', 'last_sequence'])
    if created:
        DispatcherDutyWindow.objects.bulk_create(created)
    return JsonResponse({'window': str(window), 'ack': ack, 'retired': retired})


@never_cache
@require_POST
def observations_view(request):
    auth = role_session_state(request)
    if not is_dispatcher(request, auth):
        return HttpResponse(status=403)
    now = timezone.now()
    try:
        employee, shift, digest, issued = signing.loads(request.POST['ticket'], salt=GRANT_SALT)
        if employee != auth['access'].employee_id:
            return HttpResponse(status=403)
        window = UUID(request.POST['window'])
        raw = request.POST['events']
        if window in (SESSION_LOGOUT, FOREGROUND_HEARTBEAT) or len(raw) > 8192:
            raise ValueError
        samples = parse_observations(raw, issued, now)
    except signing.BadSignature:
        return HttpResponse(status=403)
    except (KeyError, ValueError, TypeError, OverflowError, RecursionError):
        return HttpResponse(status=400)
    login = auth['access'].last_login_at
    login_cutoff = (login if digest != session_digest(request) and login
                    and int(login.timestamp() * 1000) > issued else None)
    return accept_observations(employee, shift, digest, window, samples, now, login_cutoff)
