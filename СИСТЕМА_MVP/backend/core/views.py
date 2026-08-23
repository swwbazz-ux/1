from django.http import JsonResponse
from django.views.decorators.http import require_GET

from users.models import EmployeeAccess
from users.active_role import role_session_state
from users.role_apps import (
    APP_CONTRACT_VERSION,
    get_role_app,
    get_role_app_for_request,
)

from .models import OperationalStateEvent, OperationalStateVersion
from .realtime import relevant_event_delta


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

    if role_is_active_for_app:
        from assignments.services import reconcile_due_haul_assignments_throttled

        reconcile_due_haul_assignments_throttled()

    state = OperationalStateVersion.objects.filter(key='production').first()
    after = parse_positive_int(request.GET.get('after'), 0)
    limit = parse_positive_int(request.GET.get('limit'), 50, maximum=200)
    include_events = parse_bool_param(request.GET.get('include_events'), True)
    events = []
    events_truncated = False
    state_version = state.version if state else 0
    if include_events and state_version > after:
        events_queryset = OperationalStateEvent.objects.filter(
            key='production',
            version__lte=state_version,
        )
        if after:
            events_queryset = events_queryset.filter(version__gt=after)
        events, events_truncated = relevant_event_delta(
            events_queryset.order_by('version'),
            access,
            limit=limit,
        )
    relevant = bool(events) or events_truncated
    payload = {
        'authenticated': True,
        'role_active': role_is_active_for_app,
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
        'key': 'production',
        'version': state_version,
        'events': events,
        'events_truncated': events_truncated,
        'relevant': relevant if include_events else None,
    }
    if state_version > after:
        payload.update({
            'reason': state.reason if state else '',
            'updated_at': state.updated_at.isoformat() if state else '',
        })
    return JsonResponse(payload)
