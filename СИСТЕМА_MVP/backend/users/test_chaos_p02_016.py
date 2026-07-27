from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, EquipmentAssignment, WorkShiftType
from core.models import OperationalStateEvent
from references.models import Equipment, EquipmentType

from .models import AdminActionLog, Employee, EmployeeAccess, Role


class ArchivedEmployeeUnblockTests(TestCase):
    def setUp(self):
        self.admin_role = Role.objects.create(code='admin', name='Администратор')
        self.driver_role = Role.objects.create(code='driver', name='Водитель')
        self.admin = Employee.objects.create(
            full_name='Администратор P02-016',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.admin,
            role=self.admin_role,
            access_code='100000',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        login_response = self.client.post(
            '/',
            {'access_code': '100000'},
            HTTP_HOST='localhost',
        )
        self.assertEqual(login_response.status_code, 302)
        self.assertEqual(
            self.client.session.get('employee_access_id'),
            self.admin_access.pk,
        )

        truck_type = EquipmentType.objects.create(name='Самосвал')
        self.truck = Equipment.objects.create(
            equipment_type=truck_type,
            garage_number='TRUCK-P02-016',
        )

    def create_employee_access(self, *, employee_status, access_status):
        employee = Employee.objects.create(
            full_name=f'Сотрудник {employee_status} P02-016',
            status=employee_status,
            is_active=employee_status == Employee.Status.ACTIVE,
        )
        access = EmployeeAccess.objects.create(
            employee=employee,
            role=self.driver_role,
            access_code=f'P02-{employee.pk:04d}',
            status=access_status,
            is_active=False,
            blocked_at=timezone.now(),
            block_reason='Временная блокировка',
        )
        return employee, access

    def post_unblock(self, access):
        return self.client.post(
            reverse('system_admin_access_action', args=[access.pk, 'unblock']),
            HTTP_HOST='localhost',
        )

    def test_archived_blocked_direct_unblock_changes_nothing_and_keeps_assignment(self):
        employee, access = self.create_employee_access(
            employee_status=Employee.Status.ARCHIVED,
            access_status=EmployeeAccess.Status.BLOCKED,
        )
        assignment = EquipmentAssignment.objects.create(
            employee=employee,
            role=self.driver_role,
            equipment=self.truck,
            shift_type=WorkShiftType.SHIFT_1,
            assigned_by=self.admin,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        assignment_before = (
            assignment.status,
            assignment.ended_at,
            assignment.ended_by_id,
            assignment.equipment_id,
        )
        employee_before = (employee.status, employee.is_active)
        access_before = (
            access.status,
            access.is_active,
            access.blocked_at,
            access.block_reason,
            access.deactivated_at,
        )
        admin_log_count = AdminActionLog.objects.count()
        operational_event_count = OperationalStateEvent.objects.count()

        response = self.post_unblock(access)

        employee.refresh_from_db()
        access.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual((employee.status, employee.is_active), employee_before)
        self.assertEqual(
            (
                access.status,
                access.is_active,
                access.blocked_at,
                access.block_reason,
                access.deactivated_at,
            ),
            access_before,
        )
        self.assertEqual(
            (
                assignment.status,
                assignment.ended_at,
                assignment.ended_by_id,
                assignment.equipment_id,
            ),
            assignment_before,
        )
        self.assertEqual(AdminActionLog.objects.count(), admin_log_count)
        self.assertEqual(OperationalStateEvent.objects.count(), operational_event_count)

    def test_archived_employee_uses_restore_then_separate_unblock(self):
        employee, access = self.create_employee_access(
            employee_status=Employee.Status.ARCHIVED,
            access_status=EmployeeAccess.Status.BLOCKED,
        )

        restore_response = self.client.post(
            reverse('system_admin_employee_status_action', args=[employee.pk, 'restore']),
            {'access_id': access.pk},
            HTTP_HOST='localhost',
        )
        employee.refresh_from_db()
        access.refresh_from_db()

        self.assertEqual(restore_response.status_code, 302)
        self.assertEqual(employee.status, Employee.Status.ACTIVE)
        self.assertTrue(employee.is_active)
        self.assertEqual(access.status, EmployeeAccess.Status.BLOCKED)
        self.assertFalse(access.is_active)

        unblock_response = self.post_unblock(access)
        access.refresh_from_db()
        self.assertEqual(unblock_response.status_code, 302)
        self.assertEqual(access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertTrue(access.is_active)

    def test_temporarily_blocked_active_employee_unblocks_normally(self):
        employee, access = self.create_employee_access(
            employee_status=Employee.Status.ACTIVE,
            access_status=EmployeeAccess.Status.BLOCKED,
        )
        assignment = EquipmentAssignment.objects.create(
            employee=employee,
            role=self.driver_role,
            equipment=self.truck,
            shift_type=WorkShiftType.SHIFT_1,
            assigned_by=self.admin,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        assignment_before = (
            assignment.status,
            assignment.ended_at,
            assignment.ended_by_id,
            assignment.equipment_id,
        )

        response = self.post_unblock(access)

        employee.refresh_from_db()
        access.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(employee.status, Employee.Status.ACTIVE)
        self.assertTrue(employee.is_active)
        self.assertEqual(access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertTrue(access.is_active)
        self.assertIsNone(access.blocked_at)
        self.assertEqual(access.block_reason, '')
        self.assertEqual(
            (
                assignment.status,
                assignment.ended_at,
                assignment.ended_by_id,
                assignment.equipment_id,
            ),
            assignment_before,
        )

    def test_dismissed_and_deleted_employees_do_not_unblock(self):
        for employee_status in (Employee.Status.DISMISSED, Employee.Status.DELETED):
            with self.subTest(employee_status=employee_status):
                employee, access = self.create_employee_access(
                    employee_status=employee_status,
                    access_status=EmployeeAccess.Status.BLOCKED,
                )

                response = self.post_unblock(access)

                employee.refresh_from_db()
                access.refresh_from_db()
                self.assertEqual(response.status_code, 302)
                self.assertEqual(employee.status, employee_status)
                self.assertFalse(employee.is_active)
                self.assertEqual(access.status, EmployeeAccess.Status.BLOCKED)
                self.assertFalse(access.is_active)
                list_response = self.client.get(
                    reverse('system_admin_employees'),
                    HTTP_HOST='localhost',
                )
                self.assertEqual(list_response.status_code, 200)
                self.assertNotContains(
                    list_response,
                    reverse('system_admin_access_action', args=[access.pk, 'unblock']),
                )

    def test_archived_employee_card_offers_restore_not_ordinary_unblock(self):
        employee, access = self.create_employee_access(
            employee_status=Employee.Status.ARCHIVED,
            access_status=EmployeeAccess.Status.BLOCKED,
        )

        response = self.client.get(
            reverse('system_admin_employee_detail', args=[employee.pk]),
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Восстановить сотрудника')
        self.assertNotContains(
            response,
            reverse('system_admin_access_action', args=[access.pk, 'unblock']),
        )
        list_response = self.client.get(
            reverse('system_admin_employees'),
            HTTP_HOST='localhost',
        )
        self.assertEqual(list_response.status_code, 200)
        self.assertContains(
            list_response,
            f'data-employee-status-code="{Employee.Status.ARCHIVED}"',
        )
        self.assertContains(
            list_response,
            'data-dnd-unavailable-hint="Сначала восстановите сотрудника"',
        )
        self.assertNotContains(
            list_response,
            reverse('system_admin_access_action', args=[access.pk, 'unblock']),
        )
