from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, EquipmentAssignment, WorkShiftType
from references.models import Equipment, EquipmentModel, EquipmentType

from .models import AdminActionLog, Employee, EmployeeAccess, Role
from .oup_services import (
    deactivate_employee_access,
    dismiss_employee,
    issue_employee_access,
    log_oup_action,
    open_oup_shift,
)
from .oup_undo import (
    OUP_ACTION_BULK_EMPLOYEE_UPDATED,
    OUP_ACTION_EMPLOYEE_CREATED,
    OUP_ACTION_EMPLOYEE_DISMISSED,
    OUP_ACTION_EMPLOYEE_UPDATED,
    OUP_ACTION_PERIOD_FINISHED,
    employee_card_undo_state,
    employee_created_undo_payload,
    get_oup_action_undo_state,
    state_change_payload,
)


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

    def create_oup_actor(self):
        oup_role, _ = Role.objects.get_or_create(
            code='oup',
            defaults={'name': 'Специалист ОУП'},
        )
        actor = Employee.objects.create(
            full_name='Специалист ОУП Тестовый',
            phone='+79000000003',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=actor,
            role=oup_role,
            access_code='300001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        open_oup_shift(employee=actor)
        return actor

    def dismiss_with_assignment(self):
        actor = self.create_oup_actor()
        self.employee_access.status = EmployeeAccess.Status.ACTIVATED
        self.employee_access.activated_at = timezone.now()
        self.employee_access.save(update_fields=['status', 'activated_at'])
        equipment_type = EquipmentType.objects.create(name='Самосвал')
        equipment_model = EquipmentModel.objects.create(
            equipment_type=equipment_type,
            name='БелАЗ для отмены',
        )
        equipment = Equipment.objects.create(
            equipment_type=equipment_type,
            model=equipment_model,
            garage_number='UNDO-101',
        )
        assignment = EquipmentAssignment.objects.create(
            employee=self.employee,
            role=self.driver_role,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
            assigned_by=actor,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        dismiss_employee(
            employee=self.employee,
            actor=actor,
            dismissed_at=timezone.localdate(),
            reason='Ошибочное увольнение',
        )
        dismissal_log = AdminActionLog.objects.get(
            action_code=OUP_ACTION_EMPLOYEE_DISMISSED,
            object_id=str(self.employee.id),
        )
        return dismissal_log, assignment, equipment, actor

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

        second_response = self.client.post(
            reverse('system_admin_undo_oup_action', args=[self.log.id]),
            {'next': reverse('system_admin_logs')},
            follow=True,
        )
        self.assertContains(second_response, 'уже отменено')
        self.assertEqual(AdminActionLog.objects.filter(reversal_of=self.log).count(), 1)

    def test_oup_access_issue_route_is_registered(self):
        url = reverse('oup_employee_access_issue', args=[self.employee.id])
        self.assertEqual(url, f'/oup/employees/{self.employee.id}/access/issue/')

    def test_admin_restores_dismissed_employee_access_and_assignment(self):
        dismissal_log, assignment, _equipment, _actor = self.dismiss_with_assignment()

        detail = self.client.get(reverse('system_admin_employee_detail', args=[self.employee.id]))
        self.assertContains(detail, 'Восстановить сотрудника')
        response = self.client.post(
            reverse('system_admin_undo_oup_action', args=[dismissal_log.id]),
            {'next': reverse('system_admin_employee_detail', args=[self.employee.id])},
        )

        self.assertRedirects(
            response,
            reverse('system_admin_employee_detail', args=[self.employee.id]),
        )
        self.employee.refresh_from_db()
        self.employee_access.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(self.employee.status, Employee.Status.ACTIVE)
        self.assertTrue(self.employee.is_active)
        self.assertIsNone(self.employee.dismissed_at)
        self.assertEqual(self.employee_access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertTrue(self.employee_access.is_active)
        self.assertIsNone(assignment.ended_at)
        self.assertTrue(AdminActionLog.objects.filter(reversal_of=dismissal_log).exists())
        restored_detail = self.client.get(
            reverse('system_admin_employee_detail', args=[self.employee.id])
        )
        self.assertNotContains(restored_detail, 'Восстановление после увольнения ОУП недоступно')

    def test_dismissal_undo_is_blocked_when_old_equipment_slot_is_busy(self):
        dismissal_log, assignment, equipment, actor = self.dismiss_with_assignment()
        replacement = Employee.objects.create(
            full_name='Сменный Водитель',
            personnel_number='UNDO-2',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EquipmentAssignment.objects.create(
            employee=replacement,
            role=self.driver_role,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
            assigned_by=actor,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )

        response = self.client.post(
            reverse('system_admin_undo_oup_action', args=[dismissal_log.id]),
            {'next': reverse('system_admin_logs')},
            follow=True,
        )

        self.assertContains(response, 'сотрудник или техника уже заняты')
        self.employee.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(self.employee.status, Employee.Status.DISMISSED)
        self.assertIsNotNone(assignment.ended_at)
        self.assertFalse(AdminActionLog.objects.filter(reversal_of=dismissal_log).exists())

    def test_legacy_dismissal_can_restore_employee_without_reactivating_access(self):
        self.employee.status = Employee.Status.DISMISSED
        self.employee.is_active = False
        self.employee.dismissed_at = timezone.localdate()
        self.employee.save(update_fields=['status', 'is_active', 'dismissed_at'])
        self.employee_access.status = EmployeeAccess.Status.DEACTIVATED
        self.employee_access.is_active = False
        self.employee_access.save(update_fields=['status', 'is_active'])
        legacy_log = AdminActionLog.objects.create(
            actor=None,
            action='ОУП: уволен сотрудник',
            object_type='Employee',
            object_id=str(self.employee.id),
            object_repr=str(self.employee),
        )

        self.client.post(
            reverse('system_admin_undo_oup_action', args=[legacy_log.id]),
            {'next': reverse('system_admin_logs')},
        )

        self.employee.refresh_from_db()
        self.employee_access.refresh_from_db()
        self.assertEqual(self.employee.status, Employee.Status.ACTIVE)
        self.assertTrue(self.employee.is_active)
        self.assertEqual(self.employee_access.status, EmployeeAccess.Status.DEACTIVATED)
        self.assertFalse(self.employee_access.is_active)

    def test_admin_can_undo_latest_employee_card_edit(self):
        actor = self.create_oup_actor()
        before = employee_card_undo_state(self.employee)
        self.employee.full_name = 'Измененное Имя ОУП'
        self.employee.save(update_fields=['full_name', 'updated_at'])
        after = employee_card_undo_state(self.employee)
        log = log_oup_action(
            actor,
            'изменена карточка сотрудника',
            self.employee,
            action_code=OUP_ACTION_EMPLOYEE_UPDATED,
            undo_payload=state_change_payload(before, after),
        )

        self.client.post(
            reverse('system_admin_undo_oup_action', args=[log.id]),
            {'next': reverse('system_admin_logs')},
        )

        self.employee.refresh_from_db()
        self.assertEqual(self.employee.full_name, 'Новый Водитель')

    def test_older_employee_edit_waits_for_later_action(self):
        actor = self.create_oup_actor()
        original = employee_card_undo_state(self.employee)
        self.employee.full_name = 'Первое Имя'
        self.employee.save(update_fields=['full_name', 'updated_at'])
        first_state = employee_card_undo_state(self.employee)
        first_log = log_oup_action(
            actor,
            'изменена карточка сотрудника',
            self.employee,
            action_code=OUP_ACTION_EMPLOYEE_UPDATED,
            undo_payload=state_change_payload(original, first_state),
        )
        self.employee.full_name = 'Второе Имя'
        self.employee.save(update_fields=['full_name', 'updated_at'])
        second_state = employee_card_undo_state(self.employee)
        log_oup_action(
            actor,
            'изменена карточка сотрудника',
            self.employee,
            action_code=OUP_ACTION_EMPLOYEE_UPDATED,
            undo_payload=state_change_payload(first_state, second_state),
        )

        undo_state = get_oup_action_undo_state(first_log)
        self.assertFalse(undo_state['available'])
        self.assertIn('более позднее действие', undo_state['reason'])
        self.client.post(
            reverse('system_admin_undo_oup_action', args=[first_log.id]),
            {'next': reverse('system_admin_logs')},
        )
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.full_name, 'Второе Имя')

    def test_admin_can_reverse_unused_employee_creation(self):
        actor = self.create_oup_actor()
        created_employee = Employee.objects.create(
            full_name='Ошибочно Созданный Сотрудник',
            personnel_number='UNDO-CREATE',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        creation_log = log_oup_action(
            actor,
            'создан сотрудник',
            created_employee,
            action_code=OUP_ACTION_EMPLOYEE_CREATED,
            undo_payload=employee_created_undo_payload(created_employee),
        )

        self.client.post(
            reverse('system_admin_undo_oup_action', args=[creation_log.id]),
            {'next': reverse('system_admin_logs')},
        )

        created_employee.refresh_from_db()
        self.assertEqual(created_employee.status, Employee.Status.DELETED)
        self.assertFalse(created_employee.is_active)

    def test_admin_can_reverse_oup_access_deactivation(self):
        actor = self.create_oup_actor()
        previous_code = self.employee_access.access_code
        deactivate_employee_access(employee_access=self.employee_access, actor=actor)
        log = AdminActionLog.objects.get(
            action='ОУП: отключён доступ сотрудника',
            object_id=str(self.employee_access.id),
        )

        self.client.post(
            reverse('system_admin_undo_oup_action', args=[log.id]),
            {'next': reverse('system_admin_logs')},
        )

        self.employee_access.refresh_from_db()
        self.assertEqual(self.employee_access.status, EmployeeAccess.Status.NOT_ACTIVATED)
        self.assertTrue(self.employee_access.is_active)
        self.assertEqual(self.employee_access.access_code, previous_code)

    def test_non_admin_cannot_undo_oup_action(self):
        session = self.client.session
        session['employee_access_id'] = self.employee_access.id
        session.save()

        response = self.client.post(
            reverse('system_admin_undo_oup_action', args=[self.log.id]),
            {'next': reverse('system_admin_logs')},
        )

        self.assertRedirects(
            response,
            reverse('role_home'),
            fetch_redirect_response=False,
        )
        self.assertTrue(EmployeeAccess.objects.filter(pk=self.employee_access.pk).exists())

    def test_work_period_history_has_explicit_non_reversible_reason(self):
        log = AdminActionLog.objects.create(
            action='ОУП: завершён рабочий период',
            action_code=OUP_ACTION_PERIOD_FINISHED,
            object_type='OupWorkShift',
            object_id='77',
            object_repr='Рабочий период 77',
        )

        undo_state = get_oup_action_undo_state(log)

        self.assertFalse(undo_state['available'])
        self.assertIn('учетной историей', undo_state['reason'])

    def test_bulk_import_has_explicit_correction_instruction(self):
        log = AdminActionLog.objects.create(
            action='ОУП: обновлена карточка массовым импортом',
            action_code=OUP_ACTION_BULK_EMPLOYEE_UPDATED,
            object_type='Employee',
            object_id=str(self.employee.id),
            object_repr=str(self.employee),
        )

        undo_state = get_oup_action_undo_state(log)

        self.assertFalse(undo_state['available'])
        self.assertIn('корректируется отдельной загрузкой', undo_state['reason'])
