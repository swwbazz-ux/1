from __future__ import annotations

from functools import wraps
from urllib.parse import urlencode

from django.shortcuts import redirect
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.utils.http import url_has_allowed_host_and_scheme

from users.models import EmployeeAccess
from users.views import get_current_access

from .models import PortalStaffPermission


PORTAL_EMPLOYEE_SESSION_KEY = 'portal_employee_id'
PORTAL_SOURCE_ACCESS_SESSION_KEY = 'portal_source_access_id'
PORTAL_MANUAL_LOGOUT_SESSION_KEY = 'portal_manual_logout'


def start_portal_session(request, access):
    request.session.cycle_key()
    request.session[PORTAL_EMPLOYEE_SESSION_KEY] = access.employee_id
    request.session[PORTAL_SOURCE_ACCESS_SESSION_KEY] = access.id
    request.session.pop(PORTAL_MANUAL_LOGOUT_SESSION_KEY, None)


def end_portal_session(request):
    request.session.pop(PORTAL_EMPLOYEE_SESSION_KEY, None)
    request.session.pop(PORTAL_SOURCE_ACCESS_SESSION_KEY, None)


def get_portal_employee(request):
    employee_id = request.session.get(PORTAL_EMPLOYEE_SESSION_KEY)
    if not employee_id:
        return None
    # Локальный импорт не создаёт цикл auth -> services -> models при загрузке app.
    from .services import active_employees

    employee = active_employees().filter(pk=employee_id).first()
    if not employee:
        end_portal_session(request)
        return None
    has_live_access = EmployeeAccess.objects.filter(
        employee=employee,
        is_active=True,
        status=EmployeeAccess.Status.ACTIVATED,
        role__is_active=True,
    ).exists()
    if not has_live_access:
        end_portal_session(request)
        return None
    return employee


def adopt_existing_employee_session(request):
    if request.session.get(PORTAL_MANUAL_LOGOUT_SESSION_KEY):
        return None
    access = get_current_access(request)
    if not access:
        return None
    start_portal_session(request, access)
    return access.employee


def portal_login_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        employee = get_portal_employee(request)
        if not employee:
            login_url = reverse('portal:login')
            next_url = request.get_full_path()
            if next_url:
                login_url = f'{login_url}?{urlencode({"next": next_url})}'
            return redirect(login_url)
        request.portal_employee = employee
        response = view_func(request, *args, **kwargs)
        if not getattr(response, 'streaming', False):
            add_never_cache_headers(response)
        return response

    return wrapped


def safe_next_url(request, fallback):
    next_url = request.POST.get('next') or request.GET.get('next') or ''
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url
    return fallback


def is_system_admin(employee):
    return EmployeeAccess.objects.filter(
        employee=employee,
        role__code='admin',
        role__is_active=True,
        status=EmployeeAccess.Status.ACTIVATED,
        is_active=True,
    ).exists()


def staff_capabilities(employee):
    if is_system_admin(employee):
        permission = PortalStaffPermission.objects.filter(employee=employee, is_active=True).first()
        return {
            'is_staff': True,
            'is_owner': True,
            'can_author': True,
            'can_edit': True,
            'can_publish': True,
            'can_manage_editorial': True,
            # Обращения отделены от общих админских прав: получателей назначают явно.
            'receives_feedback': bool(permission and permission.receives_feedback),
        }
    permission = PortalStaffPermission.objects.filter(employee=employee, is_active=True).first()
    if not permission:
        return {
            'is_staff': False,
            'is_owner': False,
            'can_author': False,
            'can_edit': False,
            'can_publish': False,
            'can_manage_editorial': False,
            'receives_feedback': False,
        }
    can_manage_editorial = any((permission.can_author, permission.can_edit, permission.can_publish))
    return {
        'is_staff': can_manage_editorial or permission.receives_feedback,
        'is_owner': False,
        'can_author': permission.can_author,
        'can_edit': permission.can_edit,
        'can_publish': permission.can_publish,
        'can_manage_editorial': can_manage_editorial,
        'receives_feedback': permission.receives_feedback,
    }


def portal_staff_required(capability=None):
    def decorator(view_func):
        @portal_login_required
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            capabilities = staff_capabilities(request.portal_employee)
            allowed = capabilities['is_staff'] if capability is None else capabilities.get(capability, False)
            if not allowed:
                return redirect('portal:dashboard')
            request.portal_capabilities = capabilities
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator
