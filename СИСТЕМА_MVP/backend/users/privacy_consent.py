from datetime import timezone as datetime_timezone

from django.utils import timezone


PRIVACY_POLICY_VERSION = '2026-09-05'
PRIVACY_POLICY_EFFECTIVE_DATE = '5 сентября 2026 года'
PRIVACY_CONSENT_FIELD = 'privacy_consent'
PRIVACY_CONSENT_SESSION_KEY = 'mobile_privacy_consent'
PRIVACY_CONSENT_COOKIE_NAME = 'cr_mobile_privacy_consent'
PRIVACY_CONSENT_COOKIE_SALT = 'users.mobile-privacy-consent.v1'
PRIVACY_CONSENT_COOKIE_MAX_AGE = 365 * 24 * 60 * 60
PRIVACY_CONSENT_REQUIRED_MESSAGE = (
    'Подтвердите согласие с Политикой обработки персональных данных.'
)


def privacy_consent_is_current(session):
    stored = session.get(PRIVACY_CONSENT_SESSION_KEY)
    return bool(
        isinstance(stored, dict)
        and stored.get('version') == PRIVACY_POLICY_VERSION
        and stored.get('accepted_at')
        and stored.get('access_id')
        and stored.get('role_code')
    )


def privacy_consent_matches_access(session, access):
    """Require the current consent to belong to this exact role access."""
    if not privacy_consent_is_current(session) or access is None:
        return False
    stored = session[PRIVACY_CONSENT_SESSION_KEY]
    return bool(
        stored.get('access_id') == access.pk
        and stored.get('role_code') == access.role.code
    )


def privacy_consent_submission_is_current(submitted_version):
    """Validate the submitted revision without changing the session."""
    return submitted_version == PRIVACY_POLICY_VERSION


def privacy_consent_cookie_matches_access(request, access):
    """Remember a consent revision outside the login session.

    Role-app logout intentionally flushes the Django session. A separate signed,
    host-only cookie lets the same employee keep the accepted policy revision
    without exposing it to JavaScript or carrying it to another role host.
    """
    if access is None:
        return False
    stored = request.get_signed_cookie(
        PRIVACY_CONSENT_COOKIE_NAME,
        default='',
        salt=PRIVACY_CONSENT_COOKIE_SALT,
        max_age=PRIVACY_CONSENT_COOKIE_MAX_AGE,
    )
    return stored == ':'.join((
        PRIVACY_POLICY_VERSION,
        str(access.pk),
        str(access.role.code),
    ))


def set_current_privacy_consent_cookie(response, request, access):
    """Persist only the current revision and exact role access on this host."""
    if access is None:
        return response
    response.set_signed_cookie(
        PRIVACY_CONSENT_COOKIE_NAME,
        ':'.join((
            PRIVACY_POLICY_VERSION,
            str(access.pk),
            str(access.role.code),
        )),
        salt=PRIVACY_CONSENT_COOKIE_SALT,
        max_age=PRIVACY_CONSENT_COOKIE_MAX_AGE,
        secure=request.is_secure(),
        httponly=True,
        samesite='Lax',
        path='/',
    )
    return response


def accept_current_privacy_policy(request, submitted_version, *, access):
    """Bind an accepted policy revision to an identified employee access."""
    if not privacy_consent_submission_is_current(submitted_version) or access is None:
        return False

    accepted_at = timezone.now().astimezone(datetime_timezone.utc)
    request.session[PRIVACY_CONSENT_SESSION_KEY] = {
        'version': PRIVACY_POLICY_VERSION,
        'accepted_at': accepted_at.isoformat().replace('+00:00', 'Z'),
        'access_id': access.pk,
        'role_code': access.role.code,
    }
    return True
