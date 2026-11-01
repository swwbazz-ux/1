"""Locked verification for each new shift on the shared mining-master tablet.

The caller must keep an outer atomic block around verification AND the shift
command. This helper's atomic block also makes accidental standalone calls safe,
but only the caller's block keeps these locks until the shift has been created.
"""
import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.crypto import constant_time_compare
from django.views.decorators.debug import sensitive_variables

from .access_auth import find_employee_access_by_credentials
from .active_role import activate_role_session
from .forms import is_valid_russian_mobile_phone, normalize_phone
from .mining_master_tablet_auth import (
    ROLE,
    SHIFT_AUTH_KEY,
    _eligible,
    consume_tablet_shift_auth,
)
from .models import Employee, EmployeeAccess
from .session_device import set_session_device_kind


SHIFT_LOGIN_ERROR = 'Проверьте телефон и личный PIN горного мастера.'
SHIFT_ENTRY_REQUIRED = 'Для начала смены введите телефон и личный PIN горного мастера.'


def _lock_access(candidate):
    """Same Employee → EmployeeAccess order as activation and shift commands."""
    Employee.objects.select_for_update().get(pk=candidate.employee_id)
    return (
        EmployeeAccess.objects.select_for_update(of=('self',))
        .select_related('employee', 'role', 'employee__contractor_organization')
        .get(pk=candidate.pk, employee_id=candidate.employee_id)
    )


@transaction.atomic
@sensitive_variables('raw_code', 'code')
def verify_shift_start(request):
    """Return (verified access, error); never open a shift or fabricate a role.

Explicit credentials take precedence over any existing one-use confirmation.
An unsuccessful credential attempt does not switch the existing identity.
"""
    phone = request.POST.get('reauth_phone', '').strip()
    raw_code = request.POST.get('reauth_access_code', '').strip()
    has_credentials = bool(phone or raw_code)
    try:
        if not has_credentials:
            proof = request.session.get(SHIFT_AUTH_KEY)
            access_id = proof.get('access_id') if isinstance(proof, dict) else None
            if not isinstance(access_id, int):
                return None, SHIFT_ENTRY_REQUIRED
            candidate = EmployeeAccess.objects.filter(pk=access_id).first()
            if candidate is None:
                return None, SHIFT_ENTRY_REQUIRED
            locked = _lock_access(candidate)
            # Re-read session generation and current status only after acquiring
            # the exact access lock. A reissue/revocation/login cannot race the
            # caller's subsequent shift command while its transaction is open.
            verified = consume_tablet_shift_auth(request)
            if verified is None or verified.pk != locked.pk or not _eligible(locked):
                return None, SHIFT_ENTRY_REQUIRED
            set_session_device_kind(request, 'shared')
            return verified, ''

        # The established shared-shift keypad formats six digits as 00-00-00.
        code = re.sub(r'[\s-]', '', raw_code)
        if phone and not phone.startswith(('+', '7', '8')):
            phone = '+7 ' + phone
        if not (is_valid_russian_mobile_phone(phone) and len(code) == 6 and code.isascii() and code.isdigit()):
            return None, SHIFT_LOGIN_ERROR
        candidate = find_employee_access_by_credentials(phone, code, role_code=ROLE)
        if candidate is None:
            return None, SHIFT_LOGIN_ERROR
        locked = _lock_access(candidate)
        if (
            not _eligible(locked)
            or normalize_phone(locked.employee.phone) != normalize_phone(phone)
            or not constant_time_compare(locked.access_code, code)
        ):
            return None, SHIFT_LOGIN_ERROR
        # Repeat the ambiguity check after locking: never pick an arbitrary row
        # when personnel data changed between the first lookup and the lock.
        matched = find_employee_access_by_credentials(phone, code, role_code=ROLE)
        if matched is None or matched.pk != locked.pk:
            return None, SHIFT_LOGIN_ERROR
        verified = activate_role_session(request, locked)
        request.session.pop(SHIFT_AUTH_KEY, None)
        set_session_device_kind(request, 'shared')
        return verified, ''
    except (Employee.DoesNotExist, EmployeeAccess.DoesNotExist, ValidationError):
        return None, SHIFT_LOGIN_ERROR
