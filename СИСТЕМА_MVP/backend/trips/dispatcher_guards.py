"""Доступ, безопасные переходы и ответы команд Диспетчерского пульта."""

import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from users.models import Employee, EmployeeAccess

from .dispatcher_header import get_active_dispatcher_shift


DISPATCHER_FILTER_KEYS = (
    'truck',
    'excavator',
    'show_active_trips',
    'show_pending_assignments',
    'show_accepted_assignments',
)

DISPATCHER_INTERNAL_QUERY_KEYS = {
    '_operational_fragment',
    '_operational_version',
    '_driver_refresh',
    '_eo_refresh',
    '_mm_refresh',
}

DISPATCHER_SHIFT_CLOSED_ERROR = (
    'Смена горного диспетчера закрыта. Изменения на пульте недоступны.'
)


def get_dispatcher_control_url(request):
    query_parts = []
    for key in DISPATCHER_FILTER_KEYS:
        value = request.POST.get(key, '').strip()
        if value == '':
            value = request.GET.get(key, '').strip()
        if value != '':
            query_parts.append((key, value))
    base_url = reverse('dispatcher_control')
    if not query_parts:
        return base_url
    return f'{base_url}?{urlencode(query_parts)}'


def get_dispatcher_action_redirect_url(request):
    """Return a safe full-page target after a dispatcher form action."""
    for candidate in (
        request.POST.get('next', ''),
        request.META.get('HTTP_REFERER', ''),
    ):
        candidate = str(candidate or '').strip()
        if (
            not candidate
            or re.search(r'[\x00-\x1f\x7f]', candidate)
            or not url_has_allowed_host_and_scheme(
                candidate,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            )
        ):
            continue
        parts = urlsplit(candidate)
        query = urlencode(
            [
                (key, value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
                if key not in DISPATCHER_INTERNAL_QUERY_KEYS
            ],
            doseq=True,
        )
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    return get_dispatcher_control_url(request)


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


def lock_dispatcher_mutation_access(
    request,
    access,
    *,
    role_session_state_getter,
):
    """Serialize role activation and re-check a fresh dispatcher generation."""
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
        or locked_access.role.code not in {'dispatcher', 'admin', 'manager'}
        or not role_session_state_getter(request, locked_access)['is_active']
    ):
        return None
    return locked_access


def dispatcher_shift_required_response(access):
    if get_active_dispatcher_shift(access):
        return None
    return JsonResponse(
        {'ok': False, 'error': DISPATCHER_SHIFT_CLOSED_ERROR},
        status=409,
    )


def dispatcher_shift_required_redirect(request, access, redirect_url):
    if get_active_dispatcher_shift(access):
        return None
    messages.error(request, DISPATCHER_SHIFT_CLOSED_ERROR)
    return redirect(redirect_url)


def dispatcher_json_payload(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return {}


def dispatcher_client_action_error(payload, error, *, code='stale_client'):
    return JsonResponse(
        {
            'ok': False,
            'error': '; '.join(error.messages) if isinstance(error, ValidationError) else str(error),
            'code': code,
            'conflict': True,
            'client_action_id': str((payload or {}).get('client_action_id') or ''),
        },
        status=409,
    )


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
