from django.http import JsonResponse
from django.views.decorators.http import require_GET

from users.models import EmployeeAccess

from .models import OperationalStateEvent, OperationalStateVersion


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

    access_exists = (
        EmployeeAccess.objects
        .filter(
            id=access_id,
            is_active=True,
            employee__is_active=True,
            role__is_active=True,
        )
        .exclude(status__in=[EmployeeAccess.Status.BLOCKED, EmployeeAccess.Status.DEACTIVATED])
        .exists()
    )
    if not access_exists:
        return JsonResponse({'authenticated': False}, status=401)

    state = OperationalStateVersion.objects.filter(key='production').first()
    after = parse_positive_int(request.GET.get('after'), 0)
    limit = parse_positive_int(request.GET.get('limit'), 50, maximum=200)
    include_events = parse_bool_param(request.GET.get('include_events'), True)
    events = []
    events_truncated = False
    if include_events:
        events_queryset = OperationalStateEvent.objects.filter(key='production')
        if after:
            events_queryset = events_queryset.filter(version__gt=after)
        events = [
            {
                'version': event.version,
                'type': event.event_type,
                'object_type': event.object_type,
                'object_id': event.object_id,
                'reason': event.reason,
                'payload': event.payload,
                'created_at': event.created_at.isoformat(),
            }
            for event in events_queryset.order_by('version')[:limit]
        ]
        events_truncated = events_queryset.count() > limit
    return JsonResponse({
        'authenticated': True,
        'key': 'production',
        'version': state.version if state else 0,
        'reason': state.reason if state else '',
        'updated_at': state.updated_at.isoformat() if state else '',
        'events': events,
        'events_truncated': events_truncated,
    })
