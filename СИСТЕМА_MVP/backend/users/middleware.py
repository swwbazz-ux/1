from urllib.parse import parse_qsl, urlencode

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
    # Адреса, ведущие из приложения наружу: там пропуск не действует и вызвал бы
    # отказ вместо страницы входа.
    exit_paths = {'/', '/login/', '/logout/', '/activate-access/', '/home/'}

    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _log_control_action(request, response):
        """Записываем, кто действовал на самом деле.

        Действие в режиме управления уходит в систему от имени сотрудника. Без
        этой записи в журнале осталось бы, что смену закрыл водитель, и на спор
        «я этого не делал» ответить было бы нечем. А по этим данным людям платят.
        """
        if request.method not in ObserverModeMiddleware.unsafe_methods:
            return
        if not getattr(request, 'observer_control', False):
            return
        if response.status_code >= 400:
            return
        from .models import AdminActionLog

        target = request.observer_access
        actor = request.observer_actor_access
        AdminActionLog.objects.create(
            actor=actor.employee,
            action='Действие от имени сотрудника',
            action_code='admin_control_action',
            object_type='EmployeeAccess',
            object_id=str(target.pk),
            object_repr=f'{target.employee} / {target.role.name}',
            new_value=f'{request.method} {request.path}',
            comment='Выполнено администратором в режиме управления',
        )

    @staticmethod
    def _keep_token_on_redirect(request, response, token):
        """Перенаправление внутри приложения не должно ронять пропуск.

        Пропуск живёт в адресе, а не в сессии: настоящую сессию сотрудника мы
        намеренно не трогаем. Но сервер часто отвечает перенаправлением — и
        адрес в Location собран без пропуска, поэтому следующий запрос приходит
        уже без него, а администратора выбрасывает из приложения на середине
        работы. Дописываем пропуск, пока не ушли на другой хост.
        """
        location = response.headers.get('Location', '') if hasattr(response, 'headers') else ''
        if not location or location.startswith(('http://', 'https://', '//')):
            return
        path, _, fragment = location.partition('#')
        path, _, query = path.partition('?')
        if path in self.exit_paths:
            return
        params = parse_qsl(query, keep_blank_values=True)
        if any(key == 'observe' for key, _ in params):
            return
        params.append(('observe', token))
        rebuilt = f'{path}?{urlencode(params)}'
        if fragment:
            rebuilt = f'{rebuilt}#{fragment}'
        response.headers['Location'] = rebuilt

    def __call__(self, request):
        token = (
            request.GET.get('observe', '').strip()
            or request.headers.get('X-Observer-Token', '').strip()
        )
        if not token:
            request.observer_mode = False
            return self.get_response(request)
        # Разбираем пропуск раньше запрета: только из него видно, наблюдение это
        # или управление. Раньше запрет стоял первым и резал любое действие.
        try:
            apply_observer_mode(request, token)
        except ValidationError as error:
            return HttpResponseForbidden('; '.join(error.messages))
        if request.method in self.unsafe_methods and not getattr(request, 'observer_control', False):
            return HttpResponseForbidden('Режим наблюдения не разрешает изменяющие действия.')
        response = self.get_response(request)
        self._log_control_action(request, response)
        if 300 <= response.status_code < 400:
            self._keep_token_on_redirect(request, response, token)
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
