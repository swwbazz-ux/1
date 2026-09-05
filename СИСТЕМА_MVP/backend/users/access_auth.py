from .forms import is_valid_russian_mobile_phone, normalize_phone
from .models import EmployeeAccess


def find_employee_access_by_credentials(
    phone,
    access_code,
    role_code=None,
    role_codes=None,
):
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
    elif role_codes:
        access_candidates = access_candidates.filter(role__code__in=tuple(role_codes))

    matches = []
    for candidate in access_candidates.order_by('employee_id', 'id'):
        employee_phone = normalize_phone(candidate.employee.phone)
        if employee_phone and is_valid_russian_mobile_phone(phone) and len(access_code) == 6 and normalized_phone == employee_phone:
            matches.append(candidate)
        elif not employee_phone and not normalized_phone:
            matches.append(candidate)

    # Several roles of one employee may intentionally share one PIN. But the
    # same normalized phone/PIN across different employees is ambiguous and
    # must never authenticate whichever row the database happened to return.
    employee_ids = {candidate.employee_id for candidate in matches}
    if len(employee_ids) != 1:
        return None
    if role_code and len(matches) != 1:
        return None
    return matches[0] if matches else None


def find_unactivated_accesses_by_phone(phone, role_codes=None):
    """Доступы, которые человек может забрать себе сам при первом входе.

    Раньше на экран создания пинкода можно было попасть, только зная выданный
    временный код, и его приходилось раздавать вручную каждому. Ключ здесь —
    номер телефона: при полусотне водителей однофамильцы почти неизбежны, а
    ФИО человек подтверждает уже на следующем экране, глазами.
    """
    normalized_phone = normalize_phone(phone)
    if not normalized_phone or not is_valid_russian_mobile_phone(phone):
        return []

    candidates = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(is_active=True, employee__is_active=True, role__is_active=True)
    )
    if role_codes:
        candidates = candidates.filter(role__code__in=tuple(role_codes))
    return [
        candidate
        for candidate in candidates
        if normalize_phone(candidate.employee.phone) == normalized_phone
    ]


def format_phone_for_display(value):
    """Номер для показа человеку: +7 999 111-22-33.

    В форму номер приходит уже с кодом страны или без него, и подставлять
    «+7» вслепую нельзя — получалось «+7 79991112233».
    """
    digits = normalize_phone(value)
    if len(digits) != 11:
        return value
    return f'+{digits[0]} {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:]}'
