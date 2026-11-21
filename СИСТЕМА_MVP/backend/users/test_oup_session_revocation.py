from django.contrib.sessions.models import Session
from django.test import Client, TestCase
from django.utils import timezone

from .models import AdminActionLog, Employee, EmployeeAccess, Role
from .oup_services import (
    deactivate_employee_access,
    dismiss_employee,
    open_oup_shift,
)
from .oup_undo import undo_oup_action


class OupSessionRevocationTests(TestCase):
    """T-021: ОУП-отключение доступа и увольнение гасят выданный вход.

    Отмена ОУП возвращает доступ/карточку, но **не** оживляет удалённую сессию:
    вход выполняется заново по PIN.
    """

    PIN = '300027'

    def setUp(self):
        self.admin_role, _created = Role.objects.update_or_create(
            code='admin',
            defaults={'name': 'Администратор', 'is_active': True},
        )
        self.driver_role, _created = Role.objects.update_or_create(
            code='driver',
            defaults={'name': 'Водитель самосвала', 'is_active': True},
        )
        self.oup_role, _created = Role.objects.update_or_create(
            code='oup',
            defaults={'name': 'Специалист ОУП', 'is_active': True},
        )
        self.admin = Employee.objects.create(
            full_name='Администратор ОУП T-021',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.admin,
            role=self.admin_role,
            access_code='100027',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.oup_actor = Employee.objects.create(
            full_name='Специалист ОУП T-021',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        oup_access = EmployeeAccess.objects.create(
            employee=self.oup_actor,
            role=self.oup_role,
            access_code='200027',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        open_oup_shift(actor_access_id=oup_access.pk)

        self.driver = Employee.objects.create(
            full_name='Водитель ОУП T-021',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.driver_access = EmployeeAccess.objects.create(
            employee=self.driver,
            role=self.driver_role,
            access_code=self.PIN,
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.driver_client = Client()
        login = self.driver_client.post(
            '/',
            {'access_code': self.PIN},
            HTTP_HOST='localhost',
        )
        self.assertEqual(login.status_code, 302)
        self.driver_session_key = self.driver_client.session.session_key
        self.assertIsNotNone(self.driver_session_key)

    def driver_realtime(self, client=None):
        return (client or self.driver_client).get(
            '/realtime/state/',
            {'include_events': '0'},
            HTTP_HOST='localhost',
        )

    def assert_old_session_is_dead(self):
        self.assertFalse(
            Session.objects.filter(session_key=self.driver_session_key).exists()
        )
        self.assertEqual(self.driver_realtime().status_code, 401)

    def assert_new_login_works(self):
        fresh_client = Client()
        response = fresh_client.post(
            '/',
            {'access_code': self.PIN},
            HTTP_HOST='localhost',
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            fresh_client.session.get('employee_access_id'),
            self.driver_access.pk,
        )

    def test_oup_deactivation_revokes_session_and_undo_requires_new_login(self):
        deactivate_employee_access(
            employee_access=self.driver_access,
            actor=self.oup_actor,
        )
        self.driver_access.refresh_from_db()
        self.assertEqual(
            self.driver_access.status,
            EmployeeAccess.Status.DEACTIVATED,
        )
        self.assert_old_session_is_dead()

        log = AdminActionLog.objects.get(
            action='ОУП: отключён доступ сотрудника',
            object_id=str(self.driver_access.pk),
        )
        undo_oup_action(
            log_id=log.id,
            actor_access_id=self.admin_access.pk,
        )

        self.driver_access.refresh_from_db()
        self.assertEqual(
            self.driver_access.status,
            EmployeeAccess.Status.ACTIVATED,
        )
        self.assertTrue(self.driver_access.is_active)
        # Отмена действия не возвращает старую cookie: строка сессии удалена.
        self.assert_old_session_is_dead()
        self.assert_new_login_works()

    def test_oup_dismissal_revokes_session_and_undo_requires_new_login(self):
        dismiss_employee(
            employee=self.driver,
            actor=self.oup_actor,
            dismissed_at=timezone.localdate(),
            reason='Проверка T-021',
        )
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.status, Employee.Status.DISMISSED)
        self.assert_old_session_is_dead()

        log = AdminActionLog.objects.get(
            action='ОУП: уволен сотрудник',
            object_id=str(self.driver.pk),
        )
        undo_oup_action(
            log_id=log.id,
            actor_access_id=self.admin_access.pk,
        )

        self.driver.refresh_from_db()
        self.driver_access.refresh_from_db()
        self.assertEqual(self.driver.status, Employee.Status.ACTIVE)
        self.assertTrue(self.driver.is_active)
        self.assertEqual(
            self.driver_access.status,
            EmployeeAccess.Status.ACTIVATED,
        )
        self.assert_old_session_is_dead()
        self.assert_new_login_works()
