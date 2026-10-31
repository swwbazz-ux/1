"""Personal entry for the shared mining-master tablet, isolated from role logins."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.middleware.csrf import rotate_token
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from .access_auth import find_employee_access_by_credentials
from .active_role import (
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    activate_role_session,
    role_session_state,
)
from .forms import AccessActivationForm, is_valid_russian_mobile_phone, normalize_phone
from .models import Employee, EmployeeAccess
from .session_device import set_session_device_kind
from .work_profiles import employee_has_effective_access_role


ROLE = 'mining_master'
PENDING_KEY = 'mining_master_tablet_activation'
SHIFT_AUTH_KEY = 'mining_master_tablet_shift_auth'
ACTIVATION_TTL = 10 * 60
SHIFT_AUTH_TTL = 5 * 60
LOGIN_ERROR = 'Проверьте телефон и PIN. Для входа нужен действующий доступ горного мастера.'


class TabletAccessActivationForm(AccessActivationForm):
    def clean_new_access_code(self):
        code = super().clean_new_access_code()
        if not code.isascii():
            raise ValidationError('PIN должен состоять из цифр от 0 до 9.')
        return code


def _eligible(access, *, pending=False):
    allowed_statuses = (Employee.Status.ACTIVE, Employee.Status.NOT_ACTIVATED) if pending else (Employee.Status.ACTIVE,)
    return bool(
        access
        and access.role.code == ROLE
        and access.role.is_active
        and access.is_active
        and access.employee.is_active
        and access.employee.status in allowed_statuses
        and access.status == (EmployeeAccess.Status.NOT_ACTIVATED if pending else EmployeeAccess.Status.ACTIVATED)
        and access.employee.contractor_access_is_valid()
        and employee_has_effective_access_role(access.employee, ROLE, allow_pending_access=pending)
    )


def _version(access):
    # Invalidates an in-progress activation after reissue without storing the PIN.
    issued = access.primary_code_issued_at.isoformat() if access.primary_code_issued_at else ''
    return salted_hmac(
        'mining-master-tablet-activation',
        f'{access.pk}:{access.employee_id}:{access.role_id}:{issued}:{access.access_code}:{normalize_phone(access.employee.phone)}',
        algorithm='sha256',
    ).hexdigest()


def _fresh(payload, ttl):
    if not isinstance(payload, dict):
        return False
    try:
        age = timezone.now().timestamp() - float(payload['created_at'])
        return 0 <= age < ttl
    except (KeyError, TypeError, ValueError):
        return False


def _issue_shift_auth(request, access):
    request.session[SHIFT_AUTH_KEY] = {
        'access_id': access.pk,
        'generation': request.session[ACTIVE_ROLE_GENERATION_SESSION_KEY],
        'created_at': timezone.now().timestamp(),
    }


def get_tablet_shift_auth(request):
    """Return a still-valid one-use proof; never grants a shift by itself."""
    proof = request.session.get(SHIFT_AUTH_KEY)
    if not _fresh(proof, SHIFT_AUTH_TTL):
        return None
    if (
        proof.get('access_id') != request.session.get('employee_access_id')
        or proof.get('generation') != request.session.get(ACTIVE_ROLE_GENERATION_SESSION_KEY)
        or not proof.get('generation')
    ):
        return None
    state = role_session_state(request)
    access = state['access']
    if not state['authenticated'] or not state['is_active'] or not _eligible(access):
        return None
    return access


def consume_tablet_shift_auth(request):
    """Consume before starting a shift; all normal shift conflict guards still apply."""
    access = get_tablet_shift_auth(request)
    request.session.pop(SHIFT_AUTH_KEY, None)
    return access


def _pending_access(request, *, lock=False):
    pending = request.session.get(PENDING_KEY)
    if not _fresh(pending, ACTIVATION_TTL) or pending.get('role') != ROLE:
        return None
    if not isinstance(pending.get('access_id'), int) or not isinstance(pending.get('employee_id'), int):
        return None
    if lock:
        # Lock in the same order as ordinary activation/session code.
        Employee.objects.select_for_update().get(pk=pending['employee_id'])
    queryset = EmployeeAccess.objects.select_related('employee', 'role', 'employee__contractor_organization')
    if lock:
        queryset = queryset.select_for_update(of=('self',))
    access = queryset.filter(pk=pending['access_id'], employee_id=pending['employee_id']).first()
    if not _eligible(access, pending=True) or not access.primary_code_issued_at:
        return None
    if not constant_time_compare(str(pending.get('version', '')), _version(access)):
        return None
    return access


def _finish_login(request, access):
    request.session.pop(PENDING_KEY, None)
    request.session.cycle_key()
    rotate_token(request)
    set_session_device_kind(request, 'shared')
    _issue_shift_auth(request, access)
    return redirect('mining_master_tablet')


@sensitive_post_parameters('access_code')
@never_cache
@csrf_protect
@require_http_methods(['GET', 'POST'])
def login_view(request):
    phone = ''
    error = ''
    if request.method == 'POST':
        request.session.pop(PENDING_KEY, None)
        request.session.pop(SHIFT_AUTH_KEY, None)
        phone = request.POST.get('phone', '').strip()
        code = request.POST.get('access_code', '').strip()
        access = None
        if is_valid_russian_mobile_phone(phone) and len(code) == 6 and code.isascii() and code.isdigit():
            access = find_employee_access_by_credentials(phone, code, role_code=ROLE)
        if _eligible(access, pending=True) and access.primary_code_issued_at:
            # Do not let a duplicate pending record reset an already activated role.
            if not EmployeeAccess.objects.filter(
                employee_id=access.employee_id, role__code=ROLE,
                status=EmployeeAccess.Status.ACTIVATED, is_active=True,
            ).exists():
                request.session.cycle_key()
                request.session[PENDING_KEY] = {
                    'access_id': access.pk,
                    'employee_id': access.employee_id,
                    'role': ROLE,
                    'version': _version(access),
                    'created_at': timezone.now().timestamp(),
                }
                return redirect('mining_master_tablet_activate')
        elif _eligible(access):
            try:
                with transaction.atomic():
                    Employee.objects.select_for_update().get(pk=access.employee_id)
                    access = EmployeeAccess.objects.select_for_update(of=('self',)).select_related(
                        'employee', 'role', 'employee__contractor_organization',
                    ).get(pk=access.pk)
                    if not _eligible(access) or not constant_time_compare(access.access_code, code):
                        raise ValidationError(LOGIN_ERROR)
                    access = activate_role_session(request, access)
                return _finish_login(request, access)
            except (ValidationError, Employee.DoesNotExist, EmployeeAccess.DoesNotExist):
                pass
        error = LOGIN_ERROR
    return render(request, 'users/mining_master_tablet_login.html', {'phone': phone, 'error': error})


@sensitive_post_parameters('new_access_code', 'confirm_access_code')
@never_cache
@csrf_protect
@require_http_methods(['GET', 'POST'])
def activate_view(request):
    if request.method == 'POST' and request.POST.get('action') == 'cancel':
        request.session.pop(PENDING_KEY, None)
        request.session.pop(SHIFT_AUTH_KEY, None)
        return redirect('mining_master_tablet_login')
    access = _pending_access(request)
    if access is None:
        request.session.pop(PENDING_KEY, None)
        return redirect('mining_master_tablet_login')
    form = TabletAccessActivationForm(request.POST if request.method == 'POST' else None, access=access)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                locked = _pending_access(request, lock=True)
                if locked is None:
                    raise EmployeeAccess.DoesNotExist
                if EmployeeAccess.objects.filter(
                    employee_id=locked.employee_id, role__code=ROLE,
                    status=EmployeeAccess.Status.ACTIVATED, is_active=True,
                ).exclude(pk=locked.pk).exists():
                    raise EmployeeAccess.DoesNotExist
                # Re-run rules while the employee/access is locked (other roles may have changed).
                locked_form = TabletAccessActivationForm(request.POST, access=locked)
                if not locked_form.is_valid():
                    form = locked_form
                else:
                    locked.access_code = locked_form.cleaned_data['new_access_code']
                    locked.status = EmployeeAccess.Status.ACTIVATED
                    locked.activated_at = timezone.now()
                    locked.save(update_fields=['access_code', 'status', 'activated_at'])
                    if locked.employee.status == Employee.Status.NOT_ACTIVATED:
                        locked.employee.status = Employee.Status.ACTIVE
                        locked.employee.save(update_fields=['status', 'updated_at'])
                    access = activate_role_session(request, locked)
                    return _finish_login(request, access)
        except (Employee.DoesNotExist, EmployeeAccess.DoesNotExist):
            request.session.pop(PENDING_KEY, None)
            return redirect('mining_master_tablet_login')
        except ValidationError:
            form.add_error(None, 'Доступ изменился. Вернитесь ко входу и проверьте данные.')
    return render(request, 'users/mining_master_tablet_activate.html', {'access': access, 'form': form})
