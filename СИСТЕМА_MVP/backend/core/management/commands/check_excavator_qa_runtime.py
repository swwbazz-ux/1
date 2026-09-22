from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from django.conf import settings
from django.core.cache import caches
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from core.qa_environment import require_excavator_qa_environment
from users.models import Employee, EmployeeAccess


QA_HOST_ROLES = {
    'qa-admin.driverform.ru': 'admin',
    'qa-driver.driverform.ru': 'driver',
    'qa-excavator.driverform.ru': 'excavator_operator',
}
QA_ORIGINS = {f'https://{host}' for host in QA_HOST_ROLES}


def _qa_cache_settings() -> dict:
    cache_settings = settings.CACHES.get('default', {})
    backend = str(cache_settings.get('BACKEND') or '')
    location = str(cache_settings.get('LOCATION') or '')
    prefix = str(cache_settings.get('KEY_PREFIX') or '')
    if backend != 'django.core.cache.backends.redis.RedisCache':
        raise CommandError('QA runtime requires the shared Django Redis cache backend.')
    if not location.startswith(('redis://', 'rediss://')):
        raise CommandError('QA runtime requires an explicit Redis URL.')
    expected_db = str(getattr(settings, 'EXCAVATOR_QA_REDIS_DB', '') or '').strip()
    actual_db = urlparse(location).path.lstrip('/')
    if not expected_db.isdigit() or int(expected_db) <= 0:
        raise CommandError('EXCAVATOR_QA_REDIS_DB must be an explicit non-zero Redis DB index.')
    if actual_db != expected_db:
        raise CommandError('QA Redis URL does not use the guarded QA DB index.')
    if not prefix or prefix == 'accounting-mvp' or 'qa' not in prefix.lower():
        raise CommandError('QA Redis KEY_PREFIX must be non-production and contain qa.')
    return cache_settings


def _validate_hosts() -> None:
    if set(settings.ALLOWED_HOSTS) != set(QA_HOST_ROLES):
        raise CommandError('DJANGO_ALLOWED_HOSTS must contain exactly the three QA hosts.')
    if set(settings.CSRF_TRUSTED_ORIGINS) != QA_ORIGINS:
        raise CommandError('DJANGO_CSRF_TRUSTED_ORIGINS must contain exactly the three QA origins.')
    if settings.ROLE_APP_HOST_ALIASES != QA_HOST_ROLES:
        raise CommandError('DJANGO_ROLE_APP_HOST_ALIASES does not match the QA host-role contract.')
    if settings.SESSION_COOKIE_DOMAIN is not None or settings.CSRF_COOKIE_DOMAIN is not None:
        raise CommandError('QA role hosts require host-only session and CSRF cookies.')
    if settings.DEBUG or settings.SECRET_KEY == 'django-insecure-local-dev-key':
        raise CommandError('Public QA runtime cannot use DEBUG or the local development secret key.')
    if not settings.SESSION_COOKIE_SECURE or not settings.CSRF_COOKIE_SECURE:
        raise CommandError('Public QA runtime requires secure session and CSRF cookies.')
    if settings.SECURE_PROXY_SSL_HEADER != ('HTTP_X_FORWARDED_PROTO', 'https'):
        raise CommandError('Public QA runtime requires the trusted HTTPS proxy header.')
    if not settings.SECURE_SSL_REDIRECT:
        raise CommandError('Public QA runtime requires HTTPS redirect enforcement.')


def _validate_fcm() -> None:
    expected_project = str(settings.FCM_PROJECT_ID or '').strip()
    guarded_project = str(
        getattr(settings, 'EXCAVATOR_QA_FIREBASE_PROJECT_ID', '') or ''
    ).strip()
    credentials_path = Path(str(settings.FCM_SERVICE_ACCOUNT_FILE or '').strip())
    if (
        not expected_project
        or expected_project != guarded_project
        or not credentials_path.is_file()
    ):
        raise CommandError('QA FCM project and service-account file are required.')
    try:
        payload = json.loads(credentials_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommandError('QA FCM service-account file is invalid.') from exc
    if (
        payload.get('type') != 'service_account'
        or payload.get('project_id') != expected_project
        or not payload.get('client_email')
        or not payload.get('private_key_id')
        or not payload.get('private_key')
        or not payload.get('token_uri')
    ):
        raise CommandError('QA FCM service account does not match the configured QA project.')


def _validate_migrations() -> None:
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if plan:
        raise CommandError('QA database has pending migrations; deploy must stop before runtime update.')


def _validate_admin_access() -> None:
    count = EmployeeAccess.objects.filter(
        employee__personnel_number='RUSTORE-QA-ADMIN',
        employee__status=Employee.Status.ACTIVE,
        employee__is_active=True,
        role__code='admin',
        role__is_active=True,
        status=EmployeeAccess.Status.ACTIVATED,
        is_active=True,
    ).count()
    if count != 1:
        raise CommandError('QA runtime requires exactly one activated QA administrator access.')


class Command(BaseCommand):
    help = 'Non-persistent connectivity preflight for the isolated QA active-probe runtime.'

    def handle(self, *args, **options):
        environment = require_excavator_qa_environment()
        _qa_cache_settings()
        _validate_hosts()
        _validate_fcm()
        _validate_migrations()
        _validate_admin_access()

        cache = caches['default']
        probe_key = f'qa-runtime-check:{uuid4().hex}'
        try:
            cache.set(probe_key, 'ok', timeout=15)
            if cache.get(probe_key) != 'ok':
                raise CommandError('QA Redis round-trip did not return the expected value.')
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError('QA Redis round-trip failed.') from exc
        finally:
            try:
                cache.delete(probe_key)
            except Exception:
                pass

        self.stdout.write(self.style.SUCCESS(
            f'QA_RUNTIME_OK database={environment.database_name} '
            f'hosts={len(QA_HOST_ROLES)} redis=shared migrations=applied '
            'admin=ready fcm=ready'
        ))
