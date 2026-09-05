from datetime import timezone as datetime_timezone

from django.utils import timezone


PRIVACY_POLICY_VERSION = '2026-09-04'
PRIVACY_POLICY_EFFECTIVE_DATE = '4 сентября 2026 года'
PRIVACY_CONSENT_FIELD = 'privacy_consent'
PRIVACY_CONSENT_SESSION_KEY = 'mobile_privacy_consent'
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
