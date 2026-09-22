from __future__ import annotations

import re
import secrets
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone


CONNECTION_EVIDENCE_CACHE_PREFIX = 'application-connection-evidence-v1'
CONNECTION_PROBE_CACHE_PREFIX = 'application-connection-probe-v1'
CONNECTION_PROBE_ACK_CACHE_PREFIX = 'application-connection-probe-ack-v1'
CONNECTION_EVIDENCE_TTL_SECONDS = 15 * 60
CONNECTION_PROBE_CACHE_TTL_SECONDS = 10 * 60
CONNECTION_PROBE_COOLDOWN_SECONDS = 30
CONNECTION_PROBE_TIMEOUT = timedelta(seconds=120)
INSTALLATION_ID_PATTERN = re.compile(r'^[A-Za-z0-9._:-]{8,96}$')
PROBE_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{16,96}$')
CONNECTION_STATES = frozenset({'unknown', 'ok', 'weak', 'lost', 'recovering'})
CHANNEL_LABELS = {
    'foreground': 'Экран',
    'background': 'Фоновый APK',
}


def _evidence_cache_key(session_key):
    return f'{CONNECTION_EVIDENCE_CACHE_PREFIX}:{session_key}'


def _probe_cache_key(access_id, app_code):
    return f'{CONNECTION_PROBE_CACHE_PREFIX}:{int(access_id)}:{app_code}'


def _probe_ack_cache_key(probe_id):
    return f'{CONNECTION_PROBE_ACK_CACHE_PREFIX}:{probe_id}'


def connection_evidence_by_session_keys(session_keys):
    session_keys = {str(item or '') for item in session_keys if item}
    key_by_session = {
        session_key: _evidence_cache_key(session_key)
        for session_key in session_keys
    }
    cached = cache.get_many(key_by_session.values()) if key_by_session else {}
    return {
        session_key: cached[key]
        for session_key, key in key_by_session.items()
        if isinstance(cached.get(key), dict)
    }


def _clean_identifier(value, pattern):
    value = str(value or '').strip()
    return value if pattern.fullmatch(value) else ''


def _bounded_int(value, *, maximum):
    if value is None or str(value).strip() == '':
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if 0 <= parsed <= maximum else None


def connection_evidence_from_request(request, *, presence_kind):
    post = request.POST if request.method == 'POST' else {}

    def value(post_name, header_name):
        return post.get(post_name, '') or request.headers.get(header_name, '')

    raw = {
        'installation_id': value('installation_id', 'X-App-Installation-Id'),
        'connection_state': value('connection_state', 'X-App-Connection-State'),
        'applied_version': value('applied_version', 'X-App-Applied-Version'),
        'observed_version': value('observed_version', 'X-App-Observed-Version'),
        'pending_version': value('pending_version', 'X-App-Pending-Version'),
        'rtt_ms': value('rtt_ms', 'X-App-Heartbeat-Rtt-Ms'),
        'probe_id': value('probe_id', 'X-App-Presence-Probe'),
        'probe_capable': value('probe_capable', 'X-App-Presence-Probe-Capable'),
    }
    connection_state = str(raw['connection_state'] or '').strip()
    if connection_state not in CONNECTION_STATES:
        connection_state = 'unknown'
    return {
        'present': any(str(item or '').strip() for item in raw.values()),
        'installation_id': _clean_identifier(
            raw['installation_id'],
            INSTALLATION_ID_PATTERN,
        ),
        'channel': presence_kind,
        'connection_state': connection_state,
        'applied_version': _bounded_int(
            raw['applied_version'],
            maximum=9_223_372_036_854_775_807,
        ),
        'observed_version': _bounded_int(
            raw['observed_version'],
            maximum=9_223_372_036_854_775_807,
        ),
        'pending_version': _bounded_int(
            raw['pending_version'],
            maximum=9_223_372_036_854_775_807,
        ),
        'rtt_ms': _bounded_int(
            raw['rtt_ms'],
            maximum=120_000,
        ),
        'probe_capable': str(raw['probe_capable'] or '').strip().lower() in {'1', 'true'},
        'probe_id': _clean_identifier(
            raw['probe_id'],
            PROBE_ID_PATTERN,
        ),
    }


def record_application_connection_evidence(
    *,
    session_key,
    access_id,
    app_code,
    evidence,
    now=None,
):
    if (
        not session_key
        or not access_id
        or not app_code
        or not evidence.get('present')
    ):
        return
    now = now or timezone.now()
    payload = {
        'installation_id': evidence.get('installation_id', ''),
        'channel': evidence.get('channel', ''),
        'connection_state': evidence.get('connection_state', 'unknown'),
        'applied_version': evidence.get('applied_version', 0),
        'observed_version': evidence.get('observed_version', 0),
        'pending_version': evidence.get('pending_version', 0),
        'rtt_ms': evidence.get('rtt_ms', 0),
        'probe_capable': bool(evidence.get('probe_capable')),
        'server_received_at': now,
    }
    cache.set(
        _evidence_cache_key(session_key),
        payload,
        CONNECTION_EVIDENCE_TTL_SECONDS,
    )

    probe_id = evidence.get('probe_id', '')
    if not probe_id:
        return
    probe_key = _probe_cache_key(access_id, app_code)
    probe = cache.get(probe_key)
    if not isinstance(probe, dict) or probe.get('probe_id') != probe_id:
        return
    requested_at = probe.get('requested_at')
    roundtrip_ms = 0
    if requested_at:
        roundtrip_ms = max(0, int((now - requested_at).total_seconds() * 1000))
    acknowledgement = {
        'probe_id': probe_id,
        'status': 'acknowledged',
        'acknowledged_at': now,
        'roundtrip_ms': roundtrip_ms,
        'installation_id': payload['installation_id'],
        'channel': payload['channel'],
    }
    cache.set(
        _probe_ack_cache_key(probe_id),
        acknowledgement,
        CONNECTION_PROBE_CACHE_TTL_SECONDS,
    )


def _connection_probe_summary_from_value(probe, acknowledgement=None, *, now):
    empty = {
        'status': 'none',
        'label': 'Не запускалась',
        'requested_at': None,
        'acknowledged_at': None,
        'roundtrip_ms': 0,
    }
    if not isinstance(probe, dict):
        return empty
    if (
        isinstance(acknowledgement, dict)
        and acknowledgement.get('probe_id') == probe.get('probe_id')
    ):
        probe = {**probe, **acknowledgement}
    status = probe.get('status', 'none')
    if status == 'sent' and probe.get('requested_at'):
        if now - probe['requested_at'] > CONNECTION_PROBE_TIMEOUT:
            status = 'no_response'
    labels = {
        'sending': 'Отправляется',
        'sent': 'Ждём ответ APK',
        'acknowledged': 'APK ответил',
        'unavailable': 'Push недоступен',
        'no_response': 'APK не ответил',
    }
    return {
        **empty,
        **probe,
        'status': status,
        'label': labels.get(status, 'Не запускалась'),
    }


def connection_probe_summaries(access_app_pairs, *, now=None):
    now = now or timezone.now()
    pairs = {
        (int(access_id), str(app_code))
        for access_id, app_code in access_app_pairs
        if access_id and app_code
    }
    key_by_pair = {
        pair: _probe_cache_key(*pair)
        for pair in pairs
    }
    cached = cache.get_many(key_by_pair.values()) if key_by_pair else {}
    acknowledgement_key_by_pair = {
        pair: _probe_ack_cache_key(cached[key].get('probe_id'))
        for pair, key in key_by_pair.items()
        if isinstance(cached.get(key), dict) and cached[key].get('probe_id')
    }
    acknowledgements = (
        cache.get_many(acknowledgement_key_by_pair.values())
        if acknowledgement_key_by_pair else {}
    )
    return {
        pair: _connection_probe_summary_from_value(
            cached.get(key),
            acknowledgements.get(acknowledgement_key_by_pair.get(pair)),
            now=now,
        )
        for pair, key in key_by_pair.items()
    }


def connection_probe_summary(*, access_id, app_code, now=None):
    if not access_id or not app_code:
        return _connection_probe_summary_from_value(None, now=now or timezone.now())
    pair = (int(access_id), str(app_code))
    return connection_probe_summaries([pair], now=now).get(
        pair,
        _connection_probe_summary_from_value(None, now=now or timezone.now()),
    )


def application_connection_summary(
    sessions,
    *,
    access_id,
    app_code,
    presence_status,
    now=None,
    evidence_by_session_key=None,
    probe_summary=None,
):
    now = now or timezone.now()
    evidence = []
    for session in sessions:
        item = (
            evidence_by_session_key.get(session.session_key)
            if evidence_by_session_key is not None
            else cache.get(_evidence_cache_key(session.session_key))
        )
        if isinstance(item, dict) and item.get('server_received_at'):
            evidence.append(item)
    latest = max(evidence, key=lambda item: item['server_received_at']) if evidence else {}
    probe_capable = any(bool(item.get('probe_capable')) for item in evidence)
    connection_state = latest.get('connection_state', 'unknown')
    applied_version = latest.get('applied_version')
    observed_version = latest.get('observed_version')
    pending_version = latest.get('pending_version')

    if presence_status in {'offline', 'not_registered'}:
        health_code, health_label = 'offline', 'Нет подтверждённой связи'
    elif presence_status == 'recent':
        health_code, health_label = 'delayed', 'Давно не отвечало'
    elif connection_state == 'lost':
        health_code, health_label = 'degraded', 'Канал сообщает о потере'
    elif connection_state in {'weak', 'recovering'}:
        health_code, health_label = 'recovering', 'Связь восстанавливается'
    elif (
        pending_version is not None
        and applied_version is not None
        and pending_version > applied_version
    ):
        health_code, health_label = 'syncing', 'Применяет обновление'
    elif connection_state == 'ok':
        health_code, health_label = 'healthy', 'Обмен подтверждён'
    elif latest:
        health_code, health_label = 'unknown', 'Сервер ответил, данных мало'
    else:
        health_code, health_label = 'unknown', 'Нет технической телеметрии'

    installation_id = latest.get('installation_id', '')
    if applied_version is not None:
        version_label = f'v{applied_version}'
    elif observed_version is not None:
        version_label = f'видел v{observed_version}'
    else:
        version_label = '—'
    if pending_version is None:
        pending_label = 'нет данных об очереди'
    elif pending_version > 0:
        pending_label = f'ожидает v{pending_version}'
    else:
        pending_label = 'очередь пуста'
    return {
        'health_code': health_code,
        'health_label': health_label,
        'channel': latest.get('channel', ''),
        'channel_label': CHANNEL_LABELS.get(latest.get('channel', ''), '—'),
        'connection_state': connection_state,
        'installation_id': installation_id,
        'installation_short': installation_id[-8:] if installation_id else '—',
        'applied_version': applied_version,
        'observed_version': observed_version,
        'pending_version': pending_version,
        'version_label': version_label,
        'pending_label': pending_label,
        'rtt_ms': latest.get('rtt_ms'),
        'probe_capable': probe_capable,
        'received_at': latest.get('server_received_at'),
        'probe': probe_summary or connection_probe_summary(
            access_id=access_id, app_code=app_code, now=now,
        ),
    }


def begin_application_connection_probe(*, access, app_code, actor_access):
    from .native_push import native_app_ids_for_role, notify_employee_devices

    probe_id = secrets.token_urlsafe(24)
    now = timezone.now()
    probe = {
        'probe_id': probe_id,
        'status': 'sending',
        'requested_at': now,
        'acknowledged_at': None,
        'roundtrip_ms': 0,
        'requested_by_access_id': actor_access.pk,
    }
    key = _probe_cache_key(access.pk, app_code)
    cooldown_key = f'{key}:cooldown'
    if not cache.add(
        cooldown_key,
        probe_id,
        CONNECTION_PROBE_COOLDOWN_SECONDS,
    ):
        current = connection_probe_summary(
            access_id=access.pk,
            app_code=app_code,
            now=now,
        )
        if current['status'] != 'none':
            return current
    cache.set(key, probe, CONNECTION_PROBE_CACHE_TTL_SECONDS)
    probe['status'] = 'sent'
    cache.set(key, probe, CONNECTION_PROBE_CACHE_TTL_SECONDS)
    delivered = notify_employee_devices(
        access.employee,
        kind='presence_probe',
        app_ids=native_app_ids_for_role(app_code),
        extra_data={'probe_id': probe_id},
        max_devices=2,
        request_timeout_seconds=3,
        stop_after_first_success=True,
        penalize_transient_failures=False,
    )
    if not delivered:
        probe['status'] = 'unavailable'
        cache.set(key, probe, CONNECTION_PROBE_CACHE_TTL_SECONDS)
    return connection_probe_summary(access_id=access.pk, app_code=app_code, now=now)
