"""Dedicated shared tablet: the dispatch board with the master's own identity."""
from functools import wraps
import secrets

from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import get_urlconf, reverse, set_urlconf
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from users.active_role import role_session_state
from users.mining_master_tablet_auth import get_tablet_shift_auth, consume_tablet_shift_auth, issue_tablet_shift_auth
from users.mining_master_tablet_shift_auth import verify_shift_start
from users.session_device import set_session_device_kind
from trips.dispatcher_guards import lock_dispatcher_mutation_access

from .views import (
    build_mining_master_dispatcher_header, find_other_role_open_shift, other_role_shift_flag,
    get_shift_state_for_access, handle_shift_action, render_dispatcher_control_view,
)


def tablet_access(request):
    state = role_session_state(request)
    access = state.get('access')
    if (state.get('authenticated') and state.get('is_active') and access
            and access.role.code == 'mining_master'
            and not getattr(request, 'observer_mode', False)):
        return access
    return None


def tablet_workplace(view):
    """Select tablet routes only for this request; never change the employee role."""
    @wraps(view)
    @never_cache
    def wrapped(request, *args, **kwargs):
        access = tablet_access(request)
        if access is None:
            if request.method not in {'GET', 'HEAD'}:
                return JsonResponse({'ok': False, 'error': 'Требуется вход горного мастера.',
                                     'code': 'authentication_required'}, status=403)
            return redirect('mining_master_tablet_login')
        request.mining_master_tablet_control = True
        request.session['mining_master_tablet_device'] = True
        set_session_device_kind(request, 'shared')
        old_urlconf = get_urlconf()
        old_request_urlconf = getattr(request, 'urlconf', None)
        request.urlconf = 'assignments.tablet_urlconf'
        set_urlconf(request.urlconf)
        try:
            response = view(request, *args, **kwargs)
            # TemplateResponse must resolve its links before restoring the context.
            if hasattr(response, 'render') and not response.is_rendered:
                response.render()
            return response
        finally:
            set_urlconf(old_urlconf)
            if old_request_urlconf is None:
                del request.urlconf
            else:
                request.urlconf = old_request_urlconf
    return wrapped


def build_tablet_header(request, access=None):
    access = access or tablet_access(request)
    current_shift, blocking_shift = get_shift_state_for_access(access)
    header = build_mining_master_dispatcher_header(
        request, access, current_shift, blocking_shift)['header']
    # Another master's shift is visible but cannot enable this tablet's controls.
    header['active_shift'] = current_shift
    header['shift_form_action'] = reverse('mining_master_tablet')
    header['requires_shift_reauth'] = bool(header['can_toggle_shift'] and not current_shift
                                          and not get_tablet_shift_auth(request))
    header['session_device_kind'] = 'shared'
    header['shift_reauth_title'] = 'Начало смены'
    header['shift_reauth_description'] = 'Введите телефон и личный PIN горного мастера.'
    header['shift_reauth_code_label'] = 'Личный PIN'
    other = find_other_role_open_shift(access.employee, workplace_code='mining_master', for_update=False)
    if other and get_tablet_shift_auth(request):
        challenge = request.session.get('tablet_master_handover', {})
        if challenge.get('employee_id') != access.employee_id or challenge.get('shift_id') != other.pk:
            challenge = {'employee_id': access.employee_id, 'shift_id': other.pk, 'token': secrets.token_urlsafe(24)}
            request.session['tablet_master_handover'] = challenge
        header['tablet_handover_token'] = challenge['token']
    return header


def build_tablet_context(request, access=None):
    access = access or tablet_access(request)
    current_shift, blocking_shift = get_shift_state_for_access(access)
    context = build_mining_master_dispatcher_header(
        request, access, current_shift, blocking_shift)['context']
    context.update({
        'mining_master_tablet': True,
        'mining_master_mobile_enabled': False,
        'dispatcher_nav_items': None,
        'dispatcher_move_excavator_url': reverse('mining_master_tablet_dispatcher_move_excavator'),
        'dispatcher_assign_truck_url': reverse('mining_master_tablet_dispatcher_assign_truck'),
    })
    return context


@tablet_workplace
@sensitive_post_parameters('reauth_access_code')
def mining_master_tablet_view(request):
    access = tablet_access(request)
    if request.method == 'POST':
        action = request.POST.get('action', '')
        if action == 'start_shift':
            with transaction.atomic():
                access, error = verify_shift_start(request)
                if error:
                    messages.error(request, error)
                    return redirect('mining_master_tablet')
                other = find_other_role_open_shift(access.employee, workplace_code='mining_master', for_update=True)
                challenge = request.session.pop('tablet_master_handover', {})
                confirmed = (other and other_role_shift_flag(request.POST)
                             and challenge.get('employee_id') == access.employee_id
                             and challenge.get('shift_id') == other.pk
                             and secrets.compare_digest(str(challenge.get('token', '')),
                                                        request.POST.get('tablet_handover_token', ''))
                             and bool(challenge.get('token')))
                if other and not confirmed:
                    # Ask about the newly verified person's shift, never the previous user's.
                    issue_tablet_shift_auth(request, access)
                    messages.warning(request, 'У вас открыта смена другой роли. Нажмите «Начать смену» и подтвердите её завершение.')
                    return redirect('mining_master_tablet')
                current_shift, blocking_shift = get_shift_state_for_access(access)
                handle_shift_action(request, action, access, current_shift, blocking_shift)
        elif action == 'end_shift':
            consume_tablet_shift_auth(request)
            with transaction.atomic():
                access = lock_dispatcher_mutation_access(request, access, role_session_state_getter=role_session_state)
                if access is None:
                    messages.error(request, 'Нет действующего доступа к собственной открытой смене.')
                    return redirect('mining_master_tablet')
                current_shift, blocking_shift = get_shift_state_for_access(access)
                handle_shift_action(request, action, access, current_shift, blocking_shift)
        else:
            messages.error(request, 'Неизвестное действие смены.')
        return redirect('mining_master_tablet')
    return render_dispatcher_control_view(
        request, access_override=access, enforce_dispatcher_access=False,
        dispatcher_header_override=build_tablet_header(request, access),
        context_overrides=build_tablet_context(request, access),
    )


@never_cache
@require_POST
def tablet_logout_view(request):
    # Logging out does not close or transfer the employee's production shift.
    request.session.flush()
    return redirect('mining_master_tablet_login')
