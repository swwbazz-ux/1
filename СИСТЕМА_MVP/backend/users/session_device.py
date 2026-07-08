MOBILE_USER_AGENT_TOKENS = (
    'android',
    'iphone',
    'ipod',
    'mobile',
    'windows phone',
)


def is_mobile_request(request):
    client_hint = request.META.get('HTTP_SEC_CH_UA_MOBILE', '').strip()
    if client_hint == '?1':
        return True
    user_agent = request.META.get('HTTP_USER_AGENT', '').lower()
    return any(token in user_agent for token in MOBILE_USER_AGENT_TOKENS)


def detect_session_device_kind(request):
    return 'personal' if is_mobile_request(request) else 'shared'


def mark_session_device_kind(request):
    request.session['device_kind'] = detect_session_device_kind(request)
    return request.session['device_kind']


def set_session_device_kind(request, device_kind):
    if device_kind not in {'personal', 'shared'}:
        device_kind = detect_session_device_kind(request)
    request.session['device_kind'] = device_kind
    return request.session['device_kind']


def get_session_device_kind(request):
    return request.session.get('device_kind') or detect_session_device_kind(request)


def is_shared_session(request):
    return get_session_device_kind(request) == 'shared'
