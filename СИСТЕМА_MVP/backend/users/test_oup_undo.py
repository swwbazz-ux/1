from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import AdminActionLog, Employee, EmployeeAccess, Role
from .oup_services import issue_employee_access


class OupActionUndoTests(TestCase):
    def setUp(self):
        self.admin_role = Role.objects.create(code='admin', name='Администратор')
        self.driver_role = Role.objects.create(code='driver', name='Водитель')
        self.admin_employee = Employee.objects.create(
            full_name='Администратор Системы',
            phone='+79000000001',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.admin_employee,
            role=self.admin_role,
            access_code='100001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.employee = Employee.objects.create(
            full_name='Новый Водитель',
            personnel_number='UNDO-1',
            phone='+79000000002',
            hired_at=timezone.localdate(),
            work_category=Employee.WorkCategory.DRIVER,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.employee_access, _code, _created = issue_employee_access(
            employee=self.employee,
            role=self.driver_role,
            actor=None,
        )
        self.log = AdminActionLog.objects.get(object_type='EmployeeAccess', object_id=str(self.employee_access.id))
        session = self.client.session
        session['employee_access_id'] = self.admin_access.id
        session.save()

    def test_admin_log_exposes_undo_and_can_reverse_access_issue(self):
        response = self.client.get(reverse('system_admin_logs'))
        self.assertContains(response, 'Отменить выдачу PIN')

        response = self.client.post(
            reverse('system_admin_undo_oup_action', args=[self.log.id]),
            {'next': reverse('system_admin_logs')},
        )

        self.assertRedirects(response, reverse('system_admin_logs'))
        self.assertFalse(EmployeeAccess.objects.filter(pk=self.employee_access.pk).exists())
        self.assertTrue(AdminActionLog.objects.filter(reversal_of=self.log).exists())

    def test_oup_access_issue_route_is_registered(self):
        url = reverse('oup_employee_access_issue', args=[self.employee.id])
        self.assertEqual(url, f'/oup/employees/{self.employee.id}/access/issue/')
