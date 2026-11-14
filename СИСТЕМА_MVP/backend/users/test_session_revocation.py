from datetime import timedelta

from django.contrib.sessions.models import Session
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Employee, EmployeeAccess, Role
from .session_device import PERSONAL_SESSION_MAX_AGE_SECONDS


class PersonalSessionAgeTests(TestCase):
    """T-021: личная сессия живёт не больше 30 дней, старые cookie укорачиваются."""

    def setUp(self):
        driver_role, _created = Role.objects.update_or_create(
            code='driver',
            defaults={'name': 'Водитель самосвала', 'is_active': True},
        )
        self.driver = Employee.objects.create(
            full_name='Водитель T-021',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.driver,
            role=driver_role,
            access_code='300021',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.client = Client()
        response = self.client.post(
            '/',
            {'access_code': '300021', 'device_kind': 'personal'},
            HTTP_HOST='localhost',
        )
        self.assertEqual(response.status_code, 302)

    def test_personal_session_is_limited_to_thirty_days(self):
        session = self.client.session
        self.assertFalse(session.get_expire_at_browser_close())
        self.assertLessEqual(
            session.get_expiry_age(),
            PERSONAL_SESSION_MAX_AGE_SECONDS,
        )
        self.assertGreaterEqual(
            session.get_expiry_age(),
            PERSONAL_SESSION_MAX_AGE_SECONDS - 5,
        )

    def test_previously_issued_year_long_cookie_is_shortened_on_next_state_change(self):
        session = self.client.session
        session.set_expiry(timezone.now() + timedelta(days=365))
        session.save()
        self.assertGreater(session.get_expiry_age(), PERSONAL_SESSION_MAX_AGE_SECONDS)

        # Read-only GET не пишет сессию (CI-гейт); clamp — на изменяющем запросе.
        response = self.client.post(
            '/push/shown/',
            data='{}',
            content_type='application/json',
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            self.client.session.get_expiry_age(),
            PERSONAL_SESSION_MAX_AGE_SECONDS,
        )

    def test_legacy_session_without_device_kind_is_shortened(self):
        # Сессии, выданные до появления отметки device_kind: срок всё равно
        # должен укорачиваться на изменяющем запросе (T-021, review 2.2).
        session = self.client.session
        session.pop('device_kind', None)
        session.set_expiry(timezone.now() + timedelta(days=365))
        session.save()

        response = self.client.post(
            '/push/shown/',
            data='{}',
            content_type='application/json',
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            self.client.session.get_expiry_age(),
            PERSONAL_SESSION_MAX_AGE_SECONDS,
        )


class SessionRevocationTests(TestCase):
    """T-021: блокировка/деактивация/смена прав гасят выданный вход."""

    def setUp(self):
        self.admin_role, _created = Role.objects.update_or_create(
            code='admin',
            defaults={'name': 'Администратор', 'is_active': True},
        )
        self.driver_role, _created = Role.objects.update_or_create(
            code='driver',
            defaults={'name': 'Водитель самосвала', 'is_active': True},
        )
        self.other_role, _created = Role.objects.update_or_create(
            code='driver_other',
            defaults={'name': 'Водитель другая роль', 'is_active': True},
        )
        self.admin = Employee.objects.create(
            full_name='Администратор T-021',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.admin,
            role=self.admin_role,
            access_code='100022',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.driver = Employee.objects.create(
            full_name='Водитель смены T-021',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.driver_access = EmployeeAccess.objects.create(
            employee=self.driver,
            role=self.driver_role,
            access_code='300022',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

        self.admin_client = Client()
        self.admin_client.post('/', {'access_code': '100022'}, HTTP_HOST='localhost')
        self.driver_client = Client()
        self.driver_client.post('/', {'access_code': '300022'}, HTTP_HOST='localhost')
        self.driver_session_key = self.driver_client.session.session_key
        self.assertIsNotNone(self.driver_session_key)

    def driver_realtime(self):
        return self.driver_client.get(
            '/realtime/state/',
            {'include_events': '0'},
            HTTP_HOST='localhost',
        )

    def assert_driver_session_is_gone(self):
        self.assertFalse(
            Session.objects.filter(session_key=self.driver_session_key).exists()
        )
        self.assertEqual(self.driver_realtime().status_code, 401)

    def test_block_revokes_session_and_unblock_does_not_restore_it(self):
        response = self.admin_client.post(
            reverse('system_admin_access_action', args=[self.driver_access.pk, 'block']),
            {'reason': 'Проверка T-021'},
            HTTP_HOST='localhost',
        )
        self.assertEqual(response.status_code, 302)
        self.assert_driver_session_is_gone()

        unblock_response = self.admin_client.post(
            reverse('system_admin_access_action', args=[self.driver_access.pk, 'unblock']),
            HTTP_HOST='localhost',
        )
        self.assertEqual(unblock_response.status_code, 302)
        self.driver_access.refresh_from_db()
        self.assertEqual(self.driver_access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertTrue(self.driver_access.is_active)

        # Возврат статуса не возвращает старую cookie: строки сессии уже нет.
        self.assertEqual(self.driver_realtime().status_code, 401)
        reclaim_response = self.driver_client.post(
            reverse('reclaim_role_session'),
            HTTP_HOST='localhost',
        )
        self.assertIn(reclaim_response.status_code, {401, 403})

    def test_deactivate_revokes_session(self):
        response = self.admin_client.post(
            reverse('system_admin_access_action', args=[self.driver_access.pk, 'deactivate']),
            HTTP_HOST='localhost',
        )
        self.assertEqual(response.status_code, 302)
        self.assert_driver_session_is_gone()
        reclaim_response = self.driver_client.post(
            reverse('reclaim_role_session'),
            HTTP_HOST='localhost',
        )
        self.assertIn(reclaim_response.status_code, {401, 403})

    def test_role_change_revokes_old_session(self):
        previous_generation = self.driver_access.last_login_at
        response = self.admin_client.post(
            reverse('system_admin_change_access_role', args=[self.driver_access.pk]),
            {'role': self.other_role.id},
            HTTP_HOST='localhost',
        )
        self.assertEqual(response.status_code, 302)
        self.driver_access.refresh_from_db()
        self.assertEqual(self.driver_access.role_id, self.other_role.pk)
        self.assertEqual(self.driver_access.access_code, '300022')
        # Смена прав = новый вход: поколение сдвинуто, прежняя сессия удалена.
        self.assertIsNotNone(self.driver_access.last_login_at)
        if previous_generation is not None:
            self.assertGreater(self.driver_access.last_login_at, previous_generation)
        self.assert_driver_session_is_gone()

    def test_unblock_of_legacy_blocked_access_revokes_stale_session(self):
        # Блокировка, сделанная до T-021: строка сессии не удалялась.
        EmployeeAccess.objects.filter(pk=self.driver_access.pk).update(
            status=EmployeeAccess.Status.BLOCKED,
            is_active=False,
            blocked_at=timezone.now(),
            block_reason='Историческая блокировка',
        )
        self.assertTrue(
            Session.objects.filter(session_key=self.driver_session_key).exists()
        )

        response = self.admin_client.post(
            reverse('system_admin_access_action', args=[self.driver_access.pk, 'unblock']),
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 302)
        self.driver_access.refresh_from_db()
        self.assertEqual(self.driver_access.status, EmployeeAccess.Status.ACTIVATED)
        self.assert_driver_session_is_gone()

    def test_employee_archive_revokes_sessions(self):
        response = self.admin_client.post(
            reverse('system_admin_employee_status_action', args=[self.driver.pk, 'archive']),
            HTTP_HOST='localhost',
        )
        self.assertEqual(response.status_code, 302)
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.status, Employee.Status.ARCHIVED)
        self.assert_driver_session_is_gone()
