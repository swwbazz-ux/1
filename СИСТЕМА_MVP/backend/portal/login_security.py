from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import time
from typing import NamedTuple

from django.conf import settings
from django.core.cache import cache

from users.forms import normalize_phone


MAX_LOGIN_FAILURES = 5
LOGIN_BLOCK_SECONDS = 15 * 60
LOGIN_FAILURE_STATE_TTL = LOGIN_BLOCK_SECONDS
_RETRY_DELAYS_SECONDS = (1, 2, 4, 8)
_CACHE_KEY_PREFIX = 'portal:login-guard:v1:'
_HMAC_CONTEXT = b'portal-login-guard-v1\0'


class LoginAllowance(NamedTuple):
    """Result suitable both for attribute access and tuple unpacking."""

    allowed: bool
    retry_after: int


def _remote_addr(request) -> str:
    raw_value = str(request.META.get('REMOTE_ADDR') or '').strip()
    if not raw_value:
        return 'unknown'
    try:
        return ipaddress.ip_address(raw_value).compressed
    except ValueError:
        # REMOTE_ADDR normally comes from the web server. Keep malformed values
        # bounded before hashing so a bad request cannot inflate memory use.
        return raw_value.lower()[:128]


def _cache_key(request, phone) -> str:
    normalized_phone = normalize_phone(phone)
    identifier = f'{normalized_phone}\0{_remote_addr(request)}'.encode('utf-8')
    secret = str(settings.SECRET_KEY).encode('utf-8')
    digest = hmac.new(secret, _HMAC_CONTEXT + identifier, hashlib.sha256).hexdigest()
    return f'{_CACHE_KEY_PREFIX}{digest}'


def _read_state(key: str) -> tuple[int, float]:
    state = cache.get(key)
    if not isinstance(state, dict):
        return 0, 0.0
    try:
        failures = int(state.get('failures', 0))
        retry_until = float(state.get('retry_until', 0.0))
    except (TypeError, ValueError, OverflowError):
        cache.delete(key)
        return 0, 0.0
    if failures < 0 or not math.isfinite(retry_until):
        cache.delete(key)
        return 0, 0.0
    return min(failures, MAX_LOGIN_FAILURES), retry_until


def _retry_after(retry_until: float, now: float) -> int:
    remaining = retry_until - now
    return max(0, math.ceil(remaining))


def check_login_allowed(request, phone) -> LoginAllowance:
    """Check the current cooldown without sleeping or verifying credentials."""

    key = _cache_key(request, phone)
    failures, retry_until = _read_state(key)
    now = time.time()
    retry_after = _retry_after(retry_until, now)
    if retry_after:
        return LoginAllowance(allowed=False, retry_after=retry_after)

    # Once the full block has elapsed, start a new failure sequence instead of
    # immediately applying another 15-minute block on the next bad attempt.
    if failures >= MAX_LOGIN_FAILURES:
        cache.delete(key)
    return LoginAllowance(allowed=True, retry_after=0)


def record_login_failure(request, phone) -> LoginAllowance:
    """Record a failed credential check and return the resulting cooldown."""

    key = _cache_key(request, phone)
    failures, _retry_until = _read_state(key)
    failures = min(failures + 1, MAX_LOGIN_FAILURES)
    now = time.time()

    if failures >= MAX_LOGIN_FAILURES:
        delay = LOGIN_BLOCK_SECONDS
    else:
        delay = _RETRY_DELAYS_SECONDS[failures - 1]

    retry_until = now + delay
    cache.set(
        key,
        {
            'failures': failures,
            'retry_until': retry_until,
        },
        timeout=LOGIN_FAILURE_STATE_TTL,
    )
    return LoginAllowance(allowed=False, retry_after=delay)


def clear_login_failures(request, phone) -> None:
    """Reset all throttling state after a successful login."""

    cache.delete(_cache_key(request, phone))
