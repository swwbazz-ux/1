from django.conf import settings
from django.http import HttpResponse, JsonResponse

from .active_role import SAFE_ROLE_SWITCH_METHODS, role_session_state


class PersonalSessionRenewalMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (
            request.session.get('device_kind') == 'personal'
            and request.session.get('employee_access_id')
        ):
            request.session.set_expiry(settings.ROLE_APP_PERSONAL_SESSION_AGE)
        return response


class ActiveRoleSessionMiddleware:
    allowed_unsafe_paths = {'/', '/activate-access/', '/logout/'}

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
