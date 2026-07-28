from datetime import timedelta

from django.conf import settings
from django.utils import timezone


MOBILE_USER_AGENT_TOKENS = (
    'android',
    'iphone',
    'ipod',
    'mobile',
    'windows phone',
)
PERSONAL_SESSION_MAX_RENEW_INTERVAL_SECONDS = 60 * 60 * 24 * 30


def personal_session_renew_interval_seconds():
    session_age = max(1, int(settings.ROLE_APP_PERSONAL_SESSION_AGE))
    return max(
        1,
        min(PERSONAL_SESSION_MAX_RENEW_INTERVAL_SECONDS, session_age // 2),
    )


def personal_session_expiry(*, now=None):
    now = now or timezone.now()
    return now + timedelta(seconds=int(settings.ROLE_APP_PERSONAL_SESSION_AGE))


def is_mobile_request(request):
    client_hint = request.META.get('HTTP_SEC_CH_UA_MOBILE', '').strip()
    if client_hint == '?1':
        return True
    user_agent = request.META.get('HTTP_USER_AGENT', '').lower()
    return any(token in user_agent for token in MOBILE_USER_AGENT_TOKENS)


def detect_session_device_kind(request):
    return 'personal' if is_mobile_request(request) else 'shared'


def mark_session_device_kind(request):
    return set_session_device_kind(request, detect_session_device_kind(request))


def set_session_device_kind(request, device_kind):
    if device_kind not in {'personal', 'shared'}:
        device_kind = detect_session_device_kind(request)
    request.session['device_kind'] = device_kind
    if device_kind == 'personal':
        request.session.set_expiry(personal_session_expiry())
    else:
        request.session.set_expiry(0)
    return request.session['device_kind']


def get_session_device_kind(request):
    return request.session.get('device_kind') or detect_session_device_kind(request)


def is_shared_session(request):
    return get_session_device_kind(request) == 'shared'
