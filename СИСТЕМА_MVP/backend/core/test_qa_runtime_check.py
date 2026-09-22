from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings

from users.models import Employee, EmployeeAccess, Role


QA_HOST_ROLES = {
    'qa-admin.driverform.ru': 'admin',
    'qa-driver.driverform.ru': 'driver',
    'qa-excavator.driverform.ru': 'excavator_operator',
}


class FakeCache:
    def __init__(self):
        self.values = {}

    def set(self, key, value, timeout=None):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        self.values.pop(key, None)


class QARuntimeCheckTests(TestCase):
    def setUp(self):
        role = Role.objects.create(code='admin', name='Системный администратор')
        employee = Employee.objects.create(
            personnel_number='RUSTORE-QA-ADMIN',
            full_name='Администратор QA-стенда',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code='test-only',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    def runtime_settings(self, service_account_path: str, **extra):
        values = {
            'EXCAVATOR_QA_ENABLED': True,
            'EXCAVATOR_QA_DATABASE_NAME': str(connection.settings_dict['NAME']),
            'ALLOWED_HOSTS': list(QA_HOST_ROLES),
            'CSRF_TRUSTED_ORIGINS': [f'https://{host}' for host in QA_HOST_ROLES],
            'ROLE_APP_HOST_ALIASES': QA_HOST_ROLES,
            'SESSION_COOKIE_DOMAIN': None,
            'CSRF_COOKIE_DOMAIN': None,
            'DEBUG': False,
            'SECRET_KEY': 'test-qa-secret-key-not-for-production',
            'SESSION_COOKIE_SECURE': True,
            'CSRF_COOKIE_SECURE': True,
            'SECURE_PROXY_SSL_HEADER': ('HTTP_X_FORWARDED_PROTO', 'https'),
            'SECURE_SSL_REDIRECT': True,
            'FCM_PROJECT_ID': 'copper-resources-qa',
            'EXCAVATOR_QA_FIREBASE_PROJECT_ID': 'copper-resources-qa',
            'FCM_SERVICE_ACCOUNT_FILE': service_account_path,
            'EXCAVATOR_QA_REDIS_DB': '15',
            'CACHES': {
                'default': {
                    'BACKEND': 'django.core.cache.backends.redis.RedisCache',
                    'LOCATION': 'redis://127.0.0.1:6379/15',
                    'KEY_PREFIX': 'accounting-mvp-qa',
                },
            },
        }
        values.update(extra)
        return override_settings(**values)

    def write_service_account(self, root: str) -> str:
        path = Path(root) / 'qa-service-account.json'
        path.write_text(json.dumps({
            'type': 'service_account',
            'project_id': 'copper-resources-qa',
            'client_email': 'qa@example.invalid',
            'private_key_id': 'test-key-id',
            'private_key': 'test-key',
            'token_uri': 'https://oauth2.googleapis.com/token',
        }), encoding='utf-8')
        return str(path)

    @patch('core.management.commands.check_excavator_qa_runtime.MigrationExecutor')
    @patch('core.management.commands.check_excavator_qa_runtime.caches')
    def test_accepts_isolated_runtime_without_printing_credentials(self, caches, executor):
        executor.return_value.loader.graph.leaf_nodes.return_value = []
        executor.return_value.migration_plan.return_value = []
        caches.__getitem__.return_value = FakeCache()
        with tempfile.TemporaryDirectory() as directory:
            credentials = self.write_service_account(directory)
            stdout = StringIO()
            with self.runtime_settings(credentials):
                call_command('check_excavator_qa_runtime', stdout=stdout)
        output = stdout.getvalue()
        self.assertIn('QA_RUNTIME_OK', output)
        self.assertNotIn('qa@example.invalid', output)
        self.assertNotIn('test-key', output)

    def test_rejects_process_local_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            credentials = self.write_service_account(directory)
            with self.runtime_settings(
                credentials,
                CACHES={
                    'default': {
                        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                    }
                },
            ):
                with self.assertRaisesMessage(CommandError, 'shared Django Redis'):
                    call_command('check_excavator_qa_runtime')

    def test_rejects_public_debug_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            credentials = self.write_service_account(directory)
            with self.runtime_settings(credentials, DEBUG=True):
                with self.assertRaisesMessage(CommandError, 'cannot use DEBUG'):
                    call_command('check_excavator_qa_runtime')

    def test_rejects_wrong_redis_database(self):
        with tempfile.TemporaryDirectory() as directory:
            credentials = self.write_service_account(directory)
            with self.runtime_settings(credentials, EXCAVATOR_QA_REDIS_DB='14'):
                with self.assertRaisesMessage(CommandError, 'guarded QA DB index'):
                    call_command('check_excavator_qa_runtime')
