from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db.models.query import QuerySet
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, EquipmentAssignment, WorkShiftType
from references.models import Equipment, EquipmentModel, EquipmentType

from . import employee_access_locks, oup_undo as oup_undo_module
from .models import (
    AdminActionLog,
    Employee,
    EmployeeAccess,
    Role,
    WatchComposition,
)
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
    undo_oup_action,
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

    def create_second_admin_access(self):
        return EmployeeAccess.objects.create(
            employee=self.admin_employee,
            role=self.admin_role,
            access_code='100002',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

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

    def test_undo_view_passes_exact_session_access_id(self):
        with patch('users.views.undo_oup_action', return_value=('Готово', None)) as undo_mock:
            response = self.client.post(
                reverse('system_admin_undo_oup_action', args=[self.log.pk]),
                {
                    'comment': '  Проверка session access  ',
                    'next': reverse('system_admin_logs'),
                },
            )

        self.assertRedirects(response, reverse('system_admin_logs'))
        undo_mock.assert_called_once_with(
            log_id=self.log.pk,
            actor_access_id=self.admin_access.pk,
            comment='Проверка session access',
        )

    def test_access_undo_uses_complete_employee_access_lock_plan(self):
        unrelated_admin_access = self.create_second_admin_access()
        captured_plans = []
        lock_calls = []
        original_build_plan = employee_access_locks.build_employee_access_lock_plan
        original_select_for_update = QuerySet.select_for_update

        def capture_plan(**kwargs):
            plan = original_build_plan(**kwargs)
            captured_plans.append(plan)
            return plan

        def capture_select_for_update(queryset, *args, **kwargs):
            if queryset.model in {Employee, EmployeeAccess}:
                lock_calls.append((queryset.model, kwargs.get('of')))
            return original_select_for_update(queryset, *args, **kwargs)

        with (
            patch.object(
                oup_undo_module,
                'build_employee_access_lock_plan',
                side_effect=capture_plan,
            ),
            patch.object(
                QuerySet,
                'select_for_update',
                new=capture_select_for_update,
            ),
        ):
            result, reversal = undo_oup_action(
                log_id=self.log.pk,
                actor_access_id=self.admin_access.pk,
            )

        self.assertEqual(result, 'Выдача первичного PIN отменена; запись доступа удалена.')
        self.assertEqual(reversal.reversal_of_id, self.log.pk)
        self.assertEqual(len(captured_plans), 1)
        plan = captured_plans[0]
        self.assertEqual(
            plan.employee_ids,
            tuple(sorted({self.admin_employee.pk, self.employee.pk})),
        )
        self.assertEqual(
            plan.access_ids,
            tuple(sorted({self.admin_access.pk, self.employee_access.pk})),
        )
        self.assertNotIn(unrelated_admin_access.pk, plan.access_ids)
        self.assertEqual(
            lock_calls,
            [
                (Employee, ('self',)),
                (EmployeeAccess, ('self',)),
            ],
        )

    def test_access_undo_rejects_stale_relation_without_partial_write(self):
        replacement_employee = Employee.objects.create(
            full_name='Сотрудник с изменённой связью',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        original_employee_id = self.employee_access.employee_id
        original_lock_employees = employee_access_locks._lock_employees

        def reassign_after_employee_locks(plan):
            employees = original_lock_employees(plan)
            EmployeeAccess.objects.filter(pk=self.employee_access.pk).update(
                employee=replacement_employee,
            )
            return employees

        with (
            patch.object(
                employee_access_locks,
                '_lock_employees',
                side_effect=reassign_after_employee_locks,
            ),
            self.assertRaisesMessage(
                ValidationError,
                'Доступ уже изменен или первичный PIN уже использован.',
            ),
        ):
            undo_oup_action(
                log_id=self.log.pk,
                actor_access_id=self.admin_access.pk,
            )

        self.employee_access.refresh_from_db()
        self.assertEqual(self.employee_access.employee_id, original_employee_id)
        self.assertFalse(AdminActionLog.objects.filter(reversal_of=self.log).exists())

    def test_access_undo_rejects_missing_target_without_partial_write(self):
        target_access_id = self.employee_access.pk
        self.employee_access.delete()

        with self.assertRaisesMessage(ValidationError, 'Объект действия больше не найден.'):
            undo_oup_action(
                log_id=self.log.pk,
                actor_access_id=self.admin_access.pk,
            )

        self.assertFalse(EmployeeAccess.objects.filter(pk=target_access_id).exists())
        self.assertFalse(AdminActionLog.objects.filter(reversal_of=self.log).exists())

    def test_access_undo_revalidates_actor_access_after_locking(self):
        self.create_second_admin_access()
        original_lock_employees = employee_access_locks._lock_employees

        def deactivate_actor_after_employee_locks(plan):
            employees = original_lock_employees(plan)
            EmployeeAccess.objects.filter(pk=self.admin_access.pk).update(is_active=False)
            return employees

        with (
            patch.object(
                employee_access_locks,
                '_lock_employees',
                side_effect=deactivate_actor_after_employee_locks,
            ),
            self.assertRaisesMessage(
                ValidationError,
                'Отменять действия ОУП может только системный администратор.',
            ),
        ):
            undo_oup_action(
                log_id=self.log.pk,
                actor_access_id=self.admin_access.pk,
            )

        self.admin_access.refresh_from_db()
        self.assertTrue(self.admin_access.is_active)
        self.assertTrue(EmployeeAccess.objects.filter(pk=self.employee_access.pk).exists())
        self.assertFalse(AdminActionLog.objects.filter(reversal_of=self.log).exists())

    def test_second_admin_access_cannot_replace_inactive_session_access(self):
        second_admin_access = self.create_second_admin_access()
        self.admin_access.is_active = False
        self.admin_access.save(update_fields=['is_active'])

        with self.assertRaisesMessage(
            ValidationError,
            'Отменять действия ОУП может только системный администратор.',
        ):
            undo_oup_action(
                log_id=self.log.pk,
                actor_access_id=self.admin_access.pk,
            )

        second_admin_access.refresh_from_db()
        self.assertTrue(second_admin_access.is_active)
        self.assertTrue(EmployeeAccess.objects.filter(pk=self.employee_access.pk).exists())
        self.assertFalse(AdminActionLog.objects.filter(reversal_of=self.log).exists())

    def test_access_undo_revalidates_actor_employee_activity_after_locking(self):
        original_lock_employees = employee_access_locks._lock_employees

        def deactivate_employee_after_employee_locks(plan):
            employees = original_lock_employees(plan)
            Employee.objects.filter(pk=self.admin_employee.pk).update(is_active=False)
            return employees

        with (
            patch.object(
                employee_access_locks,
                '_lock_employees',
                side_effect=deactivate_employee_after_employee_locks,
            ),
            self.assertRaisesMessage(
                ValidationError,
                'Отменять действия ОУП может только системный администратор.',
            ),
        ):
            undo_oup_action(
                log_id=self.log.pk,
                actor_access_id=self.admin_access.pk,
            )

        self.admin_employee.refresh_from_db()
        self.assertTrue(self.admin_employee.is_active)
        self.assertTrue(EmployeeAccess.objects.filter(pk=self.employee_access.pk).exists())
        self.assertFalse(AdminActionLog.objects.filter(reversal_of=self.log).exists())

    def test_access_undo_revalidates_actor_employee_status_after_locking(self):
        original_lock_employees = employee_access_locks._lock_employees

        def dismiss_employee_after_employee_locks(plan):
            employees = original_lock_employees(plan)
            Employee.objects.filter(pk=self.admin_employee.pk).update(
                status=Employee.Status.DISMISSED,
            )
            return employees

        with (
            patch.object(
                employee_access_locks,
                '_lock_employees',
                side_effect=dismiss_employee_after_employee_locks,
            ),
            self.assertRaisesMessage(
                ValidationError,
                'Отменять действия ОУП может только системный администратор.',
            ),
        ):
            undo_oup_action(
                log_id=self.log.pk,
                actor_access_id=self.admin_access.pk,
            )

        self.admin_employee.refresh_from_db()
        self.assertEqual(self.admin_employee.status, Employee.Status.ACTIVE)
        self.assertTrue(EmployeeAccess.objects.filter(pk=self.employee_access.pk).exists())
        self.assertFalse(AdminActionLog.objects.filter(reversal_of=self.log).exists())

    def test_access_undo_rolls_back_when_notification_fails_after_locks(self):
        with (
            patch.object(
                oup_undo_module,
                '_notify_employee_changed',
                side_effect=RuntimeError('notification failed'),
            ),
            self.assertRaisesMessage(RuntimeError, 'notification failed'),
        ):
            undo_oup_action(
                log_id=self.log.pk,
                actor_access_id=self.admin_access.pk,
            )

        self.assertTrue(EmployeeAccess.objects.filter(pk=self.employee_access.pk).exists())
        self.assertFalse(AdminActionLog.objects.filter(reversal_of=self.log).exists())

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
        previous_composition = WatchComposition.objects.create(
            code='test-undo-watch-composition-previous',
            name='ТЕСТ_ОТМЕНА_Предыдущий состав',
        )
        next_composition = WatchComposition.objects.create(
            code='test-undo-watch-composition-next',
            name='ТЕСТ_ОТМЕНА_Новый состав',
        )
        self.employee.watch_composition = previous_composition
        self.employee.sex = Employee.Sex.MALE
        self.employee.save(update_fields=['watch_composition', 'sex', 'updated_at'])
        before = employee_card_undo_state(self.employee)
        self.employee.full_name = 'Измененное Имя ОУП'
        self.employee.watch_composition = next_composition
        self.employee.sex = Employee.Sex.FEMALE
        self.employee.save(update_fields=[
            'full_name',
            'watch_composition',
            'sex',
            'updated_at',
        ])
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
        self.assertEqual(self.employee.sex, Employee.Sex.MALE)
        self.assertEqual(
            self.employee.watch_composition,
            previous_composition,
        )

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
        self.employee_access.status = EmployeeAccess.Status.ACTIVATED
        self.employee_access.save(update_fields=['status'])
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
