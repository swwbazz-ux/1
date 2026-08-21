from django.utils.crypto import constant_time_compare, salted_hmac

from users.access_auth import find_employee_access_by_credentials
from users.models import Employee, EmployeeAccess
from users.work_profiles import employee_has_effective_access_role


TIMEKEEPER_APP_ACCESS_SESSION_KEY = 'timekeeper_app_access_id'
TIMEKEEPER_APP_CREDENTIAL_REVISION_SESSION_KEY = (
    'timekeeper_app_credential_revision'
)
TIMEKEEPER_APP_ROLE_CODES = ('timekeeper', 'admin')


def _timestamp_value(value):
    return value.isoformat() if value else ''


def timekeeper_app_credential_revision(access):
    payload = '|'.join((
        str(access.pk),
        str(access.employee_id),
        str(access.role_id),
        access.role.code,
        access.status,
        '1' if access.is_active else '0',
        access.access_code,
        _timestamp_value(access.primary_code_issued_at),
        _timestamp_value(access.activated_at),
        _timestamp_value(access.blocked_at),
        _timestamp_value(access.deactivated_at),
    ))
    return salted_hmac(
        'rotations.timekeeper_app_session',
        payload,
        algorithm='sha256',
    ).hexdigest()


def clear_timekeeper_app_session(session):
    session.pop(TIMEKEEPER_APP_ACCESS_SESSION_KEY, None)
    session.pop(TIMEKEEPER_APP_CREDENTIAL_REVISION_SESSION_KEY, None)


def start_timekeeper_app_session(request, access, *, device_kind):
    from users.session_device import set_session_device_kind

    request.session.cycle_key()
    request.session[TIMEKEEPER_APP_ACCESS_SESSION_KEY] = access.pk
    request.session[TIMEKEEPER_APP_CREDENTIAL_REVISION_SESSION_KEY] = (
        timekeeper_app_credential_revision(access)
    )
    set_session_device_kind(request, device_kind)


def end_timekeeper_app_session(request):
    clear_timekeeper_app_session(request.session)


def authenticate_timekeeper_app_access(*, phone, access_code):
    access = find_employee_access_by_credentials(
        phone,
        access_code,
        role_codes=TIMEKEEPER_APP_ROLE_CODES,
    )
    if not access:
        return None
    if (
        access.status != EmployeeAccess.Status.ACTIVATED
        or not access.is_active
        or access.blocked_at is not None
        or access.deactivated_at is not None
        or access.employee.status != Employee.Status.ACTIVE
        or not access.employee.is_active
        or not access.role.is_active
        or access.role.code not in TIMEKEEPER_APP_ROLE_CODES
        or not employee_has_effective_access_role(
            access.employee,
            access.role.code,
        )
    ):
        return None
    return access


def timekeeper_app_access_from_request(request):
    access_id = request.session.get(TIMEKEEPER_APP_ACCESS_SESSION_KEY)
    credential_revision = request.session.get(
        TIMEKEEPER_APP_CREDENTIAL_REVISION_SESSION_KEY,
    )
    try:
        access_id = int(access_id)
    except (TypeError, ValueError):
        clear_timekeeper_app_session(request.session)
        return None
    if access_id <= 0 or not isinstance(credential_revision, str):
        clear_timekeeper_app_session(request.session)
        return None

    access = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(
            pk=access_id,
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
            blocked_at__isnull=True,
            deactivated_at__isnull=True,
            employee__is_active=True,
            employee__status=Employee.Status.ACTIVE,
            role__is_active=True,
            role__code__in=TIMEKEEPER_APP_ROLE_CODES,
        )
        .first()
    )
    if (
        not access
        or not employee_has_effective_access_role(
            access.employee,
            access.role.code,
        )
        or not constant_time_compare(
            credential_revision,
            timekeeper_app_credential_revision(access),
        )
    ):
        clear_timekeeper_app_session(request.session)
        return None
    return access
