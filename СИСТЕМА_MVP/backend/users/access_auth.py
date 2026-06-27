from .forms import is_valid_russian_mobile_phone, normalize_phone
from .models import EmployeeAccess


def find_employee_access_by_credentials(phone, access_code, role_code=None):
    phone = (phone or '').strip()
    normalized_phone = normalize_phone(phone)
    access_code = (access_code or '').strip()
    if not access_code.isdigit():
        return None

    access_candidates = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(access_code=access_code, is_active=True, employee__is_active=True, role__is_active=True)
    )
    if role_code:
        access_candidates = access_candidates.filter(role__code=role_code)

    for candidate in access_candidates:
        employee_phone = normalize_phone(candidate.employee.phone)
        if employee_phone and is_valid_russian_mobile_phone(phone) and len(access_code) == 6 and normalized_phone == employee_phone:
            return candidate
        if not employee_phone and not normalized_phone:
            return candidate
    return None
