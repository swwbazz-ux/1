from django.core.exceptions import ValidationError
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.utils import timezone

from .active_role import SAFE_ROLE_SWITCH_METHODS, role_session_state
from .session_device import (
    personal_session_expiry,
    personal_session_renew_interval_seconds,
)
from .live_monitor import apply_observer_mode


class ObserverModeMiddleware:
    unsafe_methods = {'POST', 'PUT', 'PATCH', 'DELETE'}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = (
            request.GET.get('observe', '').strip()
            or request.headers.get('X-Observer-Token', '').strip()
        )
        if not token:
            request.observer_mode = False
            return self.get_response(request)
        if request.method in self.unsafe_methods:
            return HttpResponseForbidden('Режим наблюдения не разрешает изменяющие действия.')
        try:
            apply_observer_mode(request, token)
        except ValidationError as error:
            return HttpResponseForbidden('; '.join(error.messages))
        response = self.get_response(request)
        response['Cache-Control'] = 'private, no-store'
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response


class PersonalSessionRenewalMiddleware:
    renewal_exempt_paths = {'/realtime/state/'}
    renewal_exempt_fragments = {
        'driver',
        'excavator',
        'dispatcher',
        'mining_master',
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (
            request.session.get('device_kind') == 'personal'
            and request.session.get('employee_access_id')
            and not self._renewal_is_exempt(request)
            and self._renewal_is_due(request.session)
        ):
            request.session.set_expiry(personal_session_expiry())
        return response

    @classmethod
    def _renewal_is_exempt(cls, request):
        return (
            request.path in cls.renewal_exempt_paths
            or request.GET.get('_operational_fragment', '').strip()
            in cls.renewal_exempt_fragments
        )

    @staticmethod
    def _renewal_is_due(session):
        stored_expiry = session.get('_session_expiry')
        if isinstance(stored_expiry, int) and not isinstance(stored_expiry, bool):
            # Previous releases stored a relative integer. Convert it once on
            # an ordinary page so subsequent checks use a real absolute date.
            return True
        try:
            remaining = session.get_expiry_age(modification=timezone.now())
        except (TypeError, ValueError):
            return False
        return remaining <= personal_session_renew_interval_seconds()


class ActiveRoleSessionMiddleware:
    allowed_unsafe_paths = {
        '/',
        '/activate-access/',
        # Taking the session back is the one action an inactive role must still
        # be able to perform — otherwise there is no way out but retyping the PIN.
        '/reclaim-session/',
        # Подписка на уведомления и отметка о показе ничего не меняют в работе,
        # но должны работать и когда роль переведена в просмотр.
        '/push/subscribe/',
        '/push/unsubscribe/',
        '/push/shown/',
        '/push/test/',
        '/logout/',
        '/clerk/login/',
        '/settlement/login/',
        '/timekeeper/login/',
        '/timekeeper/logout/',
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        state = role_session_state(request)
        request.role_session_state = state
        request.role_access_is_active = state['is_active']
        if (
            request.method not in SAFE_ROLE_SWITCH_METHODS
            and request.path not in self.allowed_unsafe_paths
            and request.session.get('employee_access_id')
            and not state['is_active']
        ):
            message = 'Роль неактивна — доступен только просмотр'
            wants_json = (
                'application/json' in (request.headers.get('Accept') or '')
                or 'application/json' in (request.headers.get('Content-Type') or '')
                or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            )
            if wants_json:
                return JsonResponse(
                    {'ok': False, 'error': message, 'code': 'inactive_role'},
                    status=409,
                )
            return HttpResponse(message, status=409, content_type='text/plain; charset=utf-8')
        return self.get_response(request)
