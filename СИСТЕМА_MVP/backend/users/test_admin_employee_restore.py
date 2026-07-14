from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import OperationalStateEvent

from .models import AdminActionLog, Employee, EmployeeAccess, Role


class AdminEmployeeRestoreTests(TestCase):
    def setUp(self):
        admin_role = Role.objects.create(code='admin', name='Администратор')
        self.dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.driver_role = Role.objects.create(code='driver', name='Водитель самосвала')
        self.admin = Employee.objects.create(
            full_name='Тестовый администратор',
            status=Employee.Status.ACTIVE,
        )
        EmployeeAccess.objects.create(
            employee=self.admin,
            role=admin_role,
            access_code='100000',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.client.post('/', {'access_code': '100000'}, HTTP_HOST='localhost')

    def restore(self, employee, employee_access=None, *, follow=True):
        data = {'access_id': employee_access.id} if employee_access else {}
        return self.client.post(
            reverse('system_admin_employee_status_action', args=[employee.id, 'restore']),
            data,
            follow=follow,
            HTTP_HOST='localhost',
        )

    def test_admin_restores_deactivated_employee_and_preserves_permanent_pin(self):
        employee = Employee.objects.create(
            full_name='Деактивированный диспетчер',
            status=Employee.Status.DEACTIVATED,
            is_active=False,
            dismissed_at=timezone.localdate(),
        )
        employee_access = EmployeeAccess.objects.create(
            employee=employee,
            role=self.dispatcher_role,
            access_code='654321',
            status=EmployeeAccess.Status.DEACTIVATED,
            activated_at=timezone.now() - timedelta(days=10),
            deactivated_at=timezone.now(),
            is_active=False,
        )

        detail = self.client.get(
            reverse('system_admin_employee_detail', args=[employee.id]),
            HTTP_HOST='localhost',
        )
        self.assertContains(detail, 'Восстановить сотрудника')
        self.assertContains(detail, f'name="access_id" value="{employee_access.id}"', html=False)

        response = self.restore(employee, employee_access)
        employee.refresh_from_db()
        employee_access.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(employee.status, Employee.Status.ACTIVE)
        self.assertTrue(employee.is_active)
        self.assertIsNone(employee.dismissed_at)
        self.assertEqual(employee_access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertTrue(employee_access.is_active)
        self.assertIsNone(employee_access.deactivated_at)
        self.assertEqual(employee_access.access_code, '654321')
        self.assertContains(response, 'действующий PIN/пароль сохранен')
        self.assertTrue(
            AdminActionLog.objects.filter(
                actor=self.admin,
                action='Сотрудник восстановлен администратором',
                object_id=str(employee.id),
            ).exists()
        )
        event = OperationalStateEvent.objects.filter(
            event_type='personnel_changed',
            object_id=str(employee.id),
        ).latest('id')
        self.assertEqual(event.payload['action'], 'admin_restored')

    def test_admin_restores_dismissed_employee_with_unused_primary_pin(self):
        employee = Employee.objects.create(
            full_name='Уволенный сотрудник',
            status=Employee.Status.DISMISSED,
            is_active=False,
            dismissed_at=timezone.localdate(),
        )
        issued_at = timezone.now() - timedelta(days=2)
        employee_access = EmployeeAccess.objects.create(
            employee=employee,
            role=self.driver_role,
            access_code='123456',
            status=EmployeeAccess.Status.DEACTIVATED,
            primary_code_issued_at=issued_at,
            deactivated_at=timezone.now(),
            is_active=False,
        )

        response = self.restore(employee, employee_access)
        employee.refresh_from_db()
        employee_access.refresh_from_db()

        self.assertEqual(employee.status, Employee.Status.ACTIVE)
        self.assertIsNone(employee.dismissed_at)
        self.assertEqual(employee_access.status, EmployeeAccess.Status.NOT_ACTIVATED)
        self.assertTrue(employee_access.is_active)
        self.assertEqual(employee_access.access_code, '123456')
        self.assertEqual(employee_access.primary_code_issued_at, issued_at)
        self.assertContains(response, 'первичный PIN сохранен')

    def test_restore_does_not_silently_unblock_access(self):
        employee = Employee.objects.create(
            full_name='Архивный сотрудник',
            status=Employee.Status.ARCHIVED,
            is_active=False,
        )
        blocked_at = timezone.now() - timedelta(days=1)
        employee_access = EmployeeAccess.objects.create(
            employee=employee,
            role=self.dispatcher_role,
            access_code='222222',
            status=EmployeeAccess.Status.DEACTIVATED,
            blocked_at=blocked_at,
            block_reason='Ручная блокировка администратора',
            deactivated_at=timezone.now(),
            is_active=False,
        )

        response = self.restore(employee, employee_access)
        employee.refresh_from_db()
        employee_access.refresh_from_db()

        self.assertEqual(employee.status, Employee.Status.ACTIVE)
        self.assertTrue(employee.is_active)
        self.assertEqual(employee_access.status, EmployeeAccess.Status.BLOCKED)
        self.assertFalse(employee_access.is_active)
        self.assertEqual(employee_access.blocked_at, blocked_at)
        self.assertEqual(employee_access.block_reason, 'Ручная блокировка администратора')
        self.assertContains(response, 'остался заблокированным')

    def test_admin_can_restore_soft_deleted_employee_without_access(self):
        employee = Employee.objects.create(
            full_name='Удаленная карточка',
            status=Employee.Status.DELETED,
            is_active=False,
            dismissed_at=timezone.localdate(),
        )

        response = self.restore(employee)
        employee.refresh_from_db()

        self.assertEqual(employee.status, Employee.Status.ACTIVE)
        self.assertTrue(employee.is_active)
        self.assertIsNone(employee.dismissed_at)
        self.assertContains(response, 'Доступ не найден')

    def test_only_selected_deactivated_access_is_restored(self):
        employee = Employee.objects.create(
            full_name='Сотрудник с двумя доступами',
            status=Employee.Status.DEACTIVATED,
            is_active=False,
        )
        old_access = EmployeeAccess.objects.create(
            employee=employee,
            role=self.driver_role,
            access_code='111111',
            status=EmployeeAccess.Status.DEACTIVATED,
            activated_at=timezone.now() - timedelta(days=20),
            is_active=False,
        )
        selected_access = EmployeeAccess.objects.create(
            employee=employee,
            role=self.dispatcher_role,
            access_code='333333',
            status=EmployeeAccess.Status.DEACTIVATED,
            activated_at=timezone.now() - timedelta(days=5),
            is_active=False,
        )

        self.restore(employee, selected_access, follow=False)
        old_access.refresh_from_db()
        selected_access.refresh_from_db()

        self.assertEqual(old_access.status, EmployeeAccess.Status.DEACTIVATED)
        self.assertFalse(old_access.is_active)
        self.assertEqual(selected_access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertTrue(selected_access.is_active)
