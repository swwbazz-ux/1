from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import skipUnless

from django.conf import settings
from django.contrib.sessions.models import Session
from django.db import close_old_connections, connection, connections
from django.test import Client, TransactionTestCase
from django.urls import reverse

from .models import Employee, EmployeeAccess, Role


@skipUnless(
    connection.vendor == 'postgresql',
    'Требуется тестовая PostgreSQL: проверяется блокировка строки доступа.',
)
class BlockDuringRealtimePostgreSQLTests(TransactionTestCase):
    """T-021: блокировка с force_end не встаёт насмерть с живым опросом."""

    def setUp(self):
        admin_role, _created = Role.objects.update_or_create(
            code='admin',
            defaults={'name': 'Администратор PG T-021', 'is_active': True},
        )
        driver_role, _created = Role.objects.update_or_create(
            code='driver',
            defaults={'name': 'Водитель PG T-021', 'is_active': True},
        )
        admin = Employee.objects.create(
            full_name='Администратор PG T-021',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=admin,
            role=admin_role,
            access_code='100025',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        driver = Employee.objects.create(
            full_name='Водитель PG T-021',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.driver_access = EmployeeAccess.objects.create(
            employee=driver,
            role=driver_role,
            access_code='300025',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.admin_client = Client()
        self.admin_client.post('/', {'access_code': '100025'}, HTTP_HOST='localhost')
        self.driver_client = Client()
        self.driver_client.post('/', {'access_code': '300025'}, HTTP_HOST='localhost')
        self.driver_session_key = self.driver_client.session.session_key
        self.driver_cookie = self.driver_client.cookies[settings.SESSION_COOKIE_NAME].value

    def tearDown(self):
        # CONN_MAX_AGE не закрывает соединения сам: без явного close_all в
        # teardown DROP DATABASE тестовой БД падает с ObjectInUse.
        connections.close_all()
        super().tearDown()

    @classmethod
    def tearDownClass(cls):
        connections.close_all()
        super().tearDownClass()

    def test_block_completes_while_realtime_polls(self):
        access_id = self.driver_access.pk
        locked = Event()
        release = Event()
        polls = []

        def block_access():
            close_old_connections()

            def pause_after_access_lock(execute, sql, params, many, context):
                result = execute(sql, params, many, context)
                if 'FROM "users_employeeaccess"' in sql and 'FOR UPDATE' in sql:
                    locked.set()
                    if not release.wait(timeout=20):
                        raise TimeoutError('Не получено разрешение завершить блокировку.')
                return result

            try:
                with connection.execute_wrapper(pause_after_access_lock):
                    return self.admin_client.post(
                        reverse('system_admin_access_action', args=[access_id, 'block']),
                        {'reason': 'Проверка T-021 на PostgreSQL'},
                        HTTP_HOST='localhost',
                    )
            finally:
                # Соединение этого потока закрывается явно: при CONN_MAX_AGE=60
                # close_old_connections() его не закрывает, и DROP DATABASE
                # тестовой БД получает ObjectInUse.
                connections.close_all()

        def poll_realtime():
            close_old_connections()
            client = Client()
            client.cookies[settings.SESSION_COOKIE_NAME] = self.driver_cookie
            try:
                for _index in range(5):
                    polls.append(client.get('/realtime/state/', HTTP_HOST='localhost'))
                return client
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            block_future = pool.submit(block_access)
            self.assertTrue(locked.wait(timeout=20), 'Блокировка не дошла до строки доступа.')
            realtime_future = pool.submit(poll_realtime)
            realtime_client = realtime_future.result(timeout=20)
            release.set()
            block_response = block_future.result(timeout=30)

        self.assertEqual(block_response.status_code, 302)
        self.assertEqual(len(polls), 5)
        # Пока транзакция блокировки не зафиксирована, старый вход ещё виден.
        self.assertEqual(
            [response.status_code for response in polls],
            [200] * len(polls),
        )
        self.assertFalse(
            Session.objects.filter(session_key=self.driver_session_key).exists()
        )
        self.assertEqual(
            realtime_client.get('/realtime/state/', HTTP_HOST='localhost').status_code,
            401,
        )
