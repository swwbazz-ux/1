from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import connection


PWA_PERFORMANCE_QA_SCHEMA = 'copper-pwa-performance-preflight'
PWA_PERFORMANCE_QA_SCHEMA_VERSION = 1
PWA_PERFORMANCE_QA_DB_ENGINE = 'django.db.backends.postgresql'
PWA_PERFORMANCE_QA_DB_NAME = 'copper_pwa_performance_qa_20260823'
PWA_PERFORMANCE_QA_DB_USER = 'copper_pwa_performance_qa_runner'
PWA_PERFORMANCE_QA_DB_HOST = '127.0.0.1'
PWA_PERFORMANCE_QA_DB_PORT = '55437'
PWA_PERFORMANCE_QA_HOST = 'dispatcher.localhost'
PWA_PERFORMANCE_QA_RUN_ID_RE = re.compile(r'^[A-Z0-9][A-Z0-9_-]{7,63}$')


class PwaPerformanceQaError(RuntimeError):
    """Fail-closed stop for the isolated PWA performance environment."""


@dataclass(frozen=True)
class PwaPerformanceQaIdentity:
    engine: str
    name: str
    user: str
    host: str
    port: str

    def canonical_fields(self) -> tuple[str, ...]:
        return (self.engine, self.name, self.user, self.host, self.port)


EXPECTED_PWA_PERFORMANCE_QA_IDENTITY = PwaPerformanceQaIdentity(
    engine=PWA_PERFORMANCE_QA_DB_ENGINE,
    name=PWA_PERFORMANCE_QA_DB_NAME,
    user=PWA_PERFORMANCE_QA_DB_USER,
    host=PWA_PERFORMANCE_QA_DB_HOST,
    port=PWA_PERFORMANCE_QA_DB_PORT,
)


def validate_pwa_performance_qa_run_id(run_id: str) -> str:
    normalized = str(run_id or '').strip()
    if not PWA_PERFORMANCE_QA_RUN_ID_RE.fullmatch(normalized):
        raise PwaPerformanceQaError(
            'QA run id должен содержать 8–64 символа A-Z, 0-9, _ или -.'
        )
    return normalized


def configured_pwa_performance_qa_identity(
    database: dict[str, Any] | None = None,
) -> PwaPerformanceQaIdentity:
    selected = settings.DATABASES['default'] if database is None else database
    return PwaPerformanceQaIdentity(
        engine=str(selected.get('ENGINE') or ''),
        name=str(selected.get('NAME') or ''),
        user=str(selected.get('USER') or ''),
        host=str(selected.get('HOST') or ''),
        port=str(selected.get('PORT') or ''),
    )


def validate_configured_pwa_performance_qa_database(
    database: dict[str, Any] | None = None,
) -> PwaPerformanceQaIdentity:
    selected = settings.DATABASES['default'] if database is None else database
    identity = configured_pwa_performance_qa_identity(selected)
    if identity != EXPECTED_PWA_PERFORMANCE_QA_IDENTITY:
        raise PwaPerformanceQaError(
            'Защитная остановка: разрешена только отдельная локальная '
            'PostgreSQL QA-БД для performance-аудита.'
        )
    if str(selected.get('PASSWORD') or ''):
        raise PwaPerformanceQaError(
            'Защитная остановка: локальная QA-роль должна использовать '
            'только loopback trust без пароля.'
        )
    return identity


def _normalized_actual_host(value: Any) -> str:
    raw = str(value or '').strip()
    if not raw:
        return raw
    try:
        return str(ipaddress.ip_interface(raw).ip)
    except ValueError:
        try:
            return str(ipaddress.ip_address(raw))
        except ValueError:
            return raw


def pwa_performance_qa_fingerprint(
    run_id: str,
    identity: PwaPerformanceQaIdentity,
) -> str:
    normalized_run_id = validate_pwa_performance_qa_run_id(run_id)
    encoded = json.dumps(
        {
            'schema': PWA_PERFORMANCE_QA_SCHEMA,
            'schema_version': PWA_PERFORMANCE_QA_SCHEMA_VERSION,
            'run_id': normalized_run_id,
            'identity': identity.canonical_fields(),
        },
        ensure_ascii=True,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('ascii')
    return hashlib.sha256(encoded).hexdigest().upper()


def verify_pwa_performance_qa_database(run_id: str) -> dict[str, Any]:
    """Verify configured and actual DB identity without returning secrets."""

    normalized_run_id = validate_pwa_performance_qa_run_id(run_id)
    configured = validate_configured_pwa_performance_qa_database()
    if connection.vendor != 'postgresql':
        raise PwaPerformanceQaError(
            'Защитная остановка: фактический backend не PostgreSQL.'
        )
    with connection.cursor() as cursor:
        cursor.execute(
            'select current_database(), current_user, '
            'inet_server_addr()::text, inet_server_port()::text'
        )
        actual_name, actual_user, actual_host, actual_port = cursor.fetchone()
    actual = PwaPerformanceQaIdentity(
        engine=PWA_PERFORMANCE_QA_DB_ENGINE,
        name=str(actual_name or ''),
        user=str(actual_user or ''),
        host=_normalized_actual_host(actual_host),
        port=str(actual_port or ''),
    )
    if actual != EXPECTED_PWA_PERFORMANCE_QA_IDENTITY:
        raise PwaPerformanceQaError(
            'Защитная остановка: фактическое соединение не соответствует '
            'изолированной performance QA-БД.'
        )
    if configured != actual:
        raise PwaPerformanceQaError(
            'Защитная остановка: configured и actual identity различаются.'
        )
    return {
        'schema': PWA_PERFORMANCE_QA_SCHEMA,
        'schema_version': PWA_PERFORMANCE_QA_SCHEMA_VERSION,
        'fingerprint': pwa_performance_qa_fingerprint(
            normalized_run_id,
            actual,
        ),
    }


def is_direct_loopback_request(request) -> bool:
    remote_addr = str(request.META.get('REMOTE_ADDR') or '').strip()
    try:
        return ipaddress.ip_address(remote_addr).is_loopback
    except ValueError:
        return False


def pwa_performance_qa_request_gate(request) -> bool:
    expected_run_id = str(
        getattr(settings, 'PWA_TRAFFIC_QA_RUN_ID', '') or ''
    ).strip()
    supplied_run_id = str(
        request.META.get('HTTP_X_COPPER_QA_RUN_ID') or ''
    ).strip()
    try:
        validate_pwa_performance_qa_run_id(expected_run_id)
    except PwaPerformanceQaError:
        return False
    return bool(
        settings.DEBUG
        and getattr(settings, 'PWA_TRAFFIC_QA_PREFLIGHT_ENABLED', False)
        and request.method == 'GET'
        and is_direct_loopback_request(request)
        and request.get_host().partition(':')[0].lower()
        == PWA_PERFORMANCE_QA_HOST
        and supplied_run_id == expected_run_id
    )
