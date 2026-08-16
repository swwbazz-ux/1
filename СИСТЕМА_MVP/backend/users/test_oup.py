import shutil
import tempfile
from datetime import timedelta
from importlib import import_module
from io import BytesIO, StringIO
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.models import QuerySet
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from assignments.models import (
    AssignmentStatus,
    CrewPlan,
    CrewPlanSlot,
    CrewPlanStatus,
    EquipmentAssignment,
    WorkShiftType,
)
from assignments.services import publish_crew_plan
from references.models import Equipment, EquipmentModel, EquipmentType
from shifts.models import EmployeeShift, ShiftType

from .admin import AdminActionLogAdmin
from .employee_access_locks import (
    build_employee_access_lock_plan,
    lock_employee_access_plan,
)
from .forms import optimize_employee_photo
from .models import (
    AdminActionLog,
    Employee,
    EmployeeAccess,
    Role,
    WatchComposition,
)
from .oup_services import lock_oup_write_context, open_oup_shift


class OupWorkplaceTests(TestCase):
    def setUp(self):
        self.oup_role, _created = Role.objects.update_or_create(
            code='oup', defaults={'name': 'Специалист ОУП', 'is_active': True}
        )
        self.driver_role = Role.objects.create(code='driver', name='Водитель самосвала')
        self.oup_employee = Employee.objects.create(
            full_name='Иванова Анна Сергеевна',
            personnel_number='ОУП-001',
            phone='+79000000008',
            position='Специалист ОУП',
            department='ОУП',
            work_category=Employee.WorkCategory.OTHER,
            hired_at=timezone.localdate(),
            rotation='Вахта А',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.oup_access = EmployeeAccess.objects.create(
            employee=self.oup_employee,
            role=self.oup_role,
            access_code='800000',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        session = self.client.session
        session['employee_access_id'] = self.oup_access.id
        session.save()

    def employee_payload(self, **overrides):
        payload = {
            'full_name': 'Петров Петр Петрович',
            'birth_date': '1988-03-12',
            'sex': Employee.Sex.UNKNOWN,
            'personnel_number': 'CR-1001',
            'phone': '+7 900 111-22-33',
            'position': 'Водитель автомобиля',
            'department': 'Горный участок №2',
            'work_category': Employee.WorkCategory.DRIVER,
            'hired_at': timezone.localdate().isoformat(),
            'rotation': 'Вахта 1',
            'comment': 'Карточка для производственного контура',
        }
        payload.update(overrides)
        return payload

    def start_shift(self):
        response = self.client.post(reverse('oup_shift_start'), {'next': reverse('oup_employees')})
        self.assertEqual(response.status_code, 302)
        return EmployeeShift.objects.get(employee=self.oup_employee, closed_at__isnull=True)

    def create_oup_actor(self, *, suffix):
        employee = Employee.objects.create(
            full_name=f'Специалист ОУП {suffix}',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        access = EmployeeAccess.objects.create(
            employee=employee,
            role=self.oup_role,
            access_code=f'81{suffix:04d}',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        return employee, access

    def close_shift_after_outer_guard(self, *_args, **_kwargs):
        EmployeeShift.objects.filter(
            employee=self.oup_employee,
            workplace_code='oup',
            closed_at__isnull=True,
        ).update(closed_at=timezone.now(), closed_by=self.oup_employee)
        return True

    def create_access_deactivation_target(self, *, suffix='01'):
        employee = Employee.objects.create(
            full_name=f'Сотрудник для отключения {suffix}',
            phone=f'+7900001{suffix.zfill(4)}',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        access = EmployeeAccess.objects.create(
            employee=employee,
            role=self.driver_role,
            access_code=f'81{suffix.zfill(4)}',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        return employee, access

    def test_create_employee_can_issue_non_admin_primary_pin(self):
        self.start_shift()
        response = self.client.post(
            reverse('oup_employee_create'),
            self.employee_payload(issue_access='on', access_role=str(self.driver_role.id)),
        )
        employee = Employee.objects.get(personnel_number='CR-1001')
        issued_access = EmployeeAccess.objects.get(employee=employee, role=self.driver_role)
        self.assertRedirects(
            response,
            reverse('oup_employee_detail', args=[employee.id]),
            fetch_redirect_response=False,
        )
        self.assertEqual(issued_access.status, EmployeeAccess.Status.NOT_ACTIVATED)
        self.assertEqual(len(issued_access.access_code), 6)
        self.assertTrue(issued_access.primary_code_issued_at)

    def test_oup_access_form_and_server_reject_admin_role(self):
        admin_role = Role.objects.create(code='admin', name='Администратор')
        employee = Employee.objects.create(
            full_name='Защищенный Администратор',
            phone='+79000000009',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.start_shift()
        response = self.client.get(reverse('oup_employee_detail', args=[employee.id]))
        self.assertNotContains(
            response,
            f'<option value="{admin_role.id}">{admin_role.name}</option>',
            html=False,
        )
        response = self.client.post(
            reverse('oup_employee_access_issue', args=[employee.id]),
            {'role': str(admin_role.id)},
        )
        self.assertFalse(EmployeeAccess.objects.filter(employee=employee, role=admin_role).exists())
        self.assertRedirects(
            response,
            reverse('oup_employee_detail', args=[employee.id]),
            fetch_redirect_response=False,
        )

    def test_oup_can_issue_and_deactivate_working_access_with_audit(self):
        employee = Employee.objects.create(
            full_name='Новый Водитель',
            phone='+79000000010',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.start_shift()
        self.client.post(
            reverse('oup_employee_access_issue', args=[employee.id]),
            {'role': str(self.driver_role.id)},
        )
        issued_access = EmployeeAccess.objects.get(employee=employee, role=self.driver_role)
        self.assertTrue(AdminActionLog.objects.filter(actor=self.oup_employee, action__contains='PIN').exists())
        self.client.post(reverse('oup_employee_access_deactivate', args=[issued_access.id]))
        issued_access.refresh_from_db()
        self.assertFalse(issued_access.is_active)
        self.assertEqual(issued_access.status, EmployeeAccess.Status.DEACTIVATED)
        self.assertTrue(
            AdminActionLog.objects.filter(
                actor=self.oup_employee,
                object_type='EmployeeAccess',
                object_id=str(issued_access.pk),
                action_code='oup_access_deactivated',
            ).exists()
        )

    def test_access_deactivation_uses_exact_sorted_employee_access_plan(self):
        target_employee, target_access = self.create_access_deactivation_target(suffix='02')
        exact_actor_access = EmployeeAccess.objects.create(
            employee=self.oup_employee,
            role=self.oup_role,
            access_code='820002',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        session = self.client.session
        session['employee_access_id'] = exact_actor_access.pk
        session.save()
        self.start_shift()

        lock_calls = []
        original_select_for_update = QuerySet.select_for_update

        def traced_select_for_update(queryset, *args, **kwargs):
            lock_calls.append((queryset.model, kwargs.get('of')))
            return original_select_for_update(queryset, *args, **kwargs)

        with (
            patch(
                'users.oup_views.build_employee_access_lock_plan',
                wraps=build_employee_access_lock_plan,
            ) as build_plan,
            patch(
                'users.oup_views.lock_employee_access_plan',
                wraps=lock_employee_access_plan,
            ) as lock_plan,
            patch.object(QuerySet, 'select_for_update', traced_select_for_update),
        ):
            response = self.client.post(
                reverse('oup_employee_access_deactivate', args=[target_access.pk])
            )

        self.assertEqual(response.status_code, 302)
        plan = lock_plan.call_args.args[0]
        self.assertEqual(
            plan.employee_ids,
            tuple(sorted((self.oup_employee.pk, target_employee.pk))),
        )
        self.assertEqual(
            plan.access_ids,
            tuple(sorted((exact_actor_access.pk, target_access.pk))),
        )
        self.assertNotIn(self.oup_access.pk, plan.access_ids)
        self.assertEqual(
            build_plan.call_args.kwargs['access_ids'],
            tuple(sorted((exact_actor_access.pk, target_access.pk))),
        )

        locked_models = [model for model, _of in lock_calls]
        employee_lock_index = locked_models.index(Employee)
        access_lock_index = locked_models.index(EmployeeAccess)
        self.assertLess(employee_lock_index, access_lock_index)
        self.assertFalse(
            any(model is Employee for model in locked_models[access_lock_index + 1:])
        )
        self.assertEqual(lock_calls[employee_lock_index][1], ('self',))
        self.assertEqual(lock_calls[access_lock_index][1], ('self',))
        self.assertNotIn(Role, locked_models)

    def test_access_deactivation_rejects_stale_employee_mapping_without_writes(self):
        target_employee, target_access = self.create_access_deactivation_target(
            suffix='03'
        )
        replacement_employee = Employee.objects.create(
            full_name='Подменённый сотрудник доступа',
            phone='+79000010003',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.start_shift()
        audit_before = AdminActionLog.objects.count()

        def reassign_before_lock(plan):
            EmployeeAccess.objects.filter(pk=target_access.pk).update(
                employee=replacement_employee
            )
            return lock_employee_access_plan(plan)

        with patch(
            'users.oup_views.lock_employee_access_plan',
            side_effect=reassign_before_lock,
        ):
            response = self.client.post(
                reverse('oup_employee_access_deactivate', args=[target_access.pk])
            )

        self.assertEqual(response.status_code, 302)
        target_access.refresh_from_db()
        self.assertEqual(target_access.employee_id, target_employee.pk)
        self.assertTrue(target_access.is_active)
        self.assertEqual(AdminActionLog.objects.count(), audit_before)

    def test_access_deactivation_rejects_stale_role_mapping_without_writes(self):
        _target_employee, target_access = self.create_access_deactivation_target(
            suffix='04'
        )
        replacement_role = Role.objects.create(code='mechanic', name='Механик')
        self.start_shift()
        audit_before = AdminActionLog.objects.count()

        def change_role_before_lock(plan):
            EmployeeAccess.objects.filter(pk=target_access.pk).update(role=replacement_role)
            return lock_employee_access_plan(plan)

        with patch(
            'users.oup_views.lock_employee_access_plan',
            side_effect=change_role_before_lock,
        ):
            response = self.client.post(
                reverse('oup_employee_access_deactivate', args=[target_access.pk])
            )

        self.assertEqual(response.status_code, 302)
        target_access.refresh_from_db()
        self.assertEqual(target_access.role_id, self.driver_role.pk)
        self.assertTrue(target_access.is_active)
        self.assertEqual(AdminActionLog.objects.count(), audit_before)

    def test_access_deactivation_revalidates_target_access_employee_and_role(self):
        target_employee, target_access = self.create_access_deactivation_target(
            suffix='05'
        )
        admin_role = Role.objects.create(code='admin', name='Администратор')
        self.start_shift()

        invalid_states = (
            (
                'inactive_access',
                lambda: EmployeeAccess.objects.filter(pk=target_access.pk).update(is_active=False),
            ),
            (
                'inactive_employee',
                lambda: Employee.objects.filter(pk=target_employee.pk).update(
                    status=Employee.Status.DEACTIVATED,
                    is_active=False,
                ),
            ),
            (
                'inactive_role',
                lambda: Role.objects.filter(pk=self.driver_role.pk).update(is_active=False),
            ),
            (
                'protected_admin_role',
                lambda: EmployeeAccess.objects.filter(pk=target_access.pk).update(
                    role=admin_role
                ),
            ),
        )

        for state_name, invalidate in invalid_states:
            with self.subTest(state=state_name):
                EmployeeAccess.objects.filter(pk=target_access.pk).update(
                    status=EmployeeAccess.Status.ACTIVATED,
                    is_active=True,
                    role=self.driver_role,
                )
                Employee.objects.filter(pk=target_employee.pk).update(
                    status=Employee.Status.ACTIVE,
                    is_active=True,
                )
                Role.objects.filter(pk=self.driver_role.pk).update(is_active=True)
                invalidate()
                state_before = tuple(
                    EmployeeAccess.objects.filter(pk=target_access.pk).values_list(
                        'status', 'is_active', 'deactivated_at'
                    ).get()
                )
                audit_before = AdminActionLog.objects.count()

                response = self.client.post(
                    reverse('oup_employee_access_deactivate', args=[target_access.pk])
                )

                self.assertEqual(response.status_code, 302)
                self.assertEqual(
                    tuple(
                        EmployeeAccess.objects.filter(pk=target_access.pk).values_list(
                            'status', 'is_active', 'deactivated_at'
                        ).get()
                    ),
                    state_before,
                )
                self.assertEqual(AdminActionLog.objects.count(), audit_before)

    def test_access_deactivation_revalidates_exact_actor_after_preread(self):
        _target_employee, target_access = self.create_access_deactivation_target(
            suffix='07'
        )
        self.start_shift()

        invalid_states = (
            (
                'inactive_actor_access',
                lambda: EmployeeAccess.objects.filter(pk=self.oup_access.pk).update(
                    is_active=False
                ),
            ),
            (
                'inactive_actor_employee',
                lambda: Employee.objects.filter(pk=self.oup_employee.pk).update(
                    status=Employee.Status.DEACTIVATED,
                    is_active=False,
                ),
            ),
            (
                'inactive_actor_role',
                lambda: Role.objects.filter(pk=self.oup_role.pk).update(is_active=False),
            ),
        )

        for state_name, invalidate in invalid_states:
            with self.subTest(state=state_name):
                audit_before = AdminActionLog.objects.count()

                def invalidate_after_preread(**kwargs):
                    invalidate()
                    return build_employee_access_lock_plan(**kwargs)

                with patch(
                    'users.oup_views.build_employee_access_lock_plan',
                    side_effect=invalidate_after_preread,
                ):
                    response = self.client.post(
                        reverse('oup_employee_access_deactivate', args=[target_access.pk])
                    )

                self.assertEqual(response.status_code, 302)
                target_access.refresh_from_db()
                self.assertTrue(target_access.is_active)
                self.assertEqual(
                    target_access.status,
                    EmployeeAccess.Status.ACTIVATED,
                )
                self.assertEqual(AdminActionLog.objects.count(), audit_before)

    def test_access_deactivation_rolls_back_error_after_locks(self):
        _target_employee, target_access = self.create_access_deactivation_target(
            suffix='06'
        )
        self.start_shift()
        audit_before = AdminActionLog.objects.count()

        def fail_after_write(*, employee_access, actor):
            employee_access.is_active = False
            employee_access.status = EmployeeAccess.Status.DEACTIVATED
            employee_access.save(update_fields=['is_active', 'status'])
            AdminActionLog.objects.create(
                actor=actor,
                action='ОУП: тестовая частичная запись',
                object_type='EmployeeAccess',
                object_id=str(employee_access.pk),
            )
            raise ValidationError('Контрольная ошибка после блокировок.')

        with patch(
            'users.oup_views.deactivate_employee_access',
            side_effect=fail_after_write,
        ):
            response = self.client.post(
                reverse('oup_employee_access_deactivate', args=[target_access.pk])
            )

        self.assertEqual(response.status_code, 302)
        target_access.refresh_from_db()
        self.assertTrue(target_access.is_active)
        self.assertEqual(target_access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertEqual(AdminActionLog.objects.count(), audit_before)

    def test_role_home_routes_oup_to_workplace(self):
        response = self.client.get(reverse('role_home'))
        self.assertRedirects(response, reverse('oup_home'), fetch_redirect_response=False)

    def test_role_home_rejects_stale_oup_session_without_redirect_loop(self):
        invalid_states = (
            (
                'inactive_role',
                lambda: Role.objects.filter(pk=self.oup_role.pk).update(is_active=False),
            ),
            (
                'not_activated_access',
                lambda: EmployeeAccess.objects.filter(pk=self.oup_access.pk).update(
                    status=EmployeeAccess.Status.NOT_ACTIVATED,
                ),
            ),
            (
                'inactive_employee_status',
                lambda: Employee.objects.filter(pk=self.oup_employee.pk).update(
                    status=Employee.Status.DEACTIVATED,
                    is_active=True,
                ),
            ),
        )

        for state_name, invalidate in invalid_states:
            with self.subTest(state=state_name):
                Role.objects.filter(pk=self.oup_role.pk).update(is_active=True)
                EmployeeAccess.objects.filter(pk=self.oup_access.pk).update(
                    is_active=True,
                    status=EmployeeAccess.Status.ACTIVATED,
                )
                Employee.objects.filter(pk=self.oup_employee.pk).update(
                    status=Employee.Status.ACTIVE,
                    is_active=True,
                )
                session = self.client.session
                session['employee_access_id'] = self.oup_access.id
                session.save()
                invalidate()

                response = self.client.get(reverse('role_home'))

                self.assertRedirects(response, reverse('login'), fetch_redirect_response=False)
                self.assertNotIn('employee_access_id', self.client.session)

    def test_oup_pages_reject_other_role(self):
        admin_role = Role.objects.create(code='admin', name='Администратор')
        admin_employee = Employee.objects.create(full_name='Администратор', status=Employee.Status.ACTIVE)
        admin_access = EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='100000',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        session = self.client.session
        session['employee_access_id'] = admin_access.id
        session.save()
        response = self.client.get(reverse('oup_employees'))
        self.assertRedirects(response, reverse('role_home'), fetch_redirect_response=False)

    def test_free_oup_period_is_presented_as_editing_access_outside_header(self):
        response = self.client.get(reverse('oup_employees'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Кадровые изменения')
        self.assertContains(response, 'Сейчас доступен только просмотр')
        self.assertContains(response, 'Включить редактирование', count=1)
        self.assertNotContains(response, 'Рабочий период')
        self.assertNotContains(response, 'Начать работу')
        self.assertNotContains(response, 'Дневная смена')
        self.assertNotContains(response, 'Начать дневную смену')

    def test_owned_oup_period_has_one_finish_editing_action(self):
        self.start_shift()

        response = self.client.get(reverse('oup_employees'))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['owns_oup_period'])
        self.assertContains(response, 'Редактирование доступно вам')
        self.assertContains(response, 'Завершить редактирование')
        self.assertContains(response, f'action="{reverse("oup_shift_close")}"', count=1, html=False)
        self.assertNotContains(response, 'Включить редактирование')
        self.assertNotContains(response, 'Завершить работу')

    def test_oup_period_uses_internal_day_value_and_only_one_specialist_can_open_it(self):
        shift = self.start_shift()
        self.assertEqual(shift.shift_type, ShiftType.DAY)
        self.assertEqual(shift.workplace_code, 'oup')
        self.assertIsNone(shift.equipment_id)

        second_employee = Employee.objects.create(
            full_name='Сидорова Ольга Викторовна',
            phone='+79000000009',
            status=Employee.Status.ACTIVE,
        )
        second_access = EmployeeAccess.objects.create(
            employee=second_employee,
            role=self.oup_role,
            access_code='800001',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        session = self.client.session
        session['employee_access_id'] = second_access.id
        session.save()

        occupied_response = self.client.get(reverse('oup_employees'))
        self.assertEqual(occupied_response.status_code, 200)
        self.assertFalse(occupied_response.context['can_change_employees'])
        self.assertTrue(occupied_response.context['oup_period_is_occupied'])
        self.assertContains(occupied_response, 'Только просмотр')
        self.assertContains(occupied_response, self.oup_employee.full_name)
        self.assertContains(occupied_response, 'Редактирование занято')
        self.assertNotContains(occupied_response, 'Включить редактирование')
        self.assertNotContains(
            occupied_response,
            f'action="{reverse("oup_shift_start")}"',
            html=False,
        )

        response = self.client.post(reverse('oup_shift_start'), follow=True)
        self.assertContains(response, 'Редактирование кадровых данных уже выполняет')
        self.assertContains(response, self.oup_employee.full_name)
        self.assertFalse(EmployeeShift.objects.filter(employee=second_employee, closed_at__isnull=True).exists())

    def test_oup_period_remains_open_beyond_one_calendar_day(self):
        shift = self.start_shift()
        opened_at = timezone.now() - timedelta(days=31)
        EmployeeShift.objects.filter(pk=shift.pk).update(opened_at=opened_at)

        same_shift, created = open_oup_shift(actor_access_id=self.oup_access.pk)

        self.assertFalse(created)
        self.assertEqual(same_shift.pk, shift.pk)
        self.assertEqual(
            EmployeeShift.objects.filter(workplace_code='oup', closed_at__isnull=True).count(),
            1,
        )

    def test_unique_open_oup_period_constraint_blocks_second_employee(self):
        second_employee, _second_access = self.create_oup_actor(suffix=91)
        EmployeeShift.objects.create(
            employee=self.oup_employee,
            shift_type=ShiftType.DAY,
            workplace_code='oup',
            opened_at=timezone.now(),
            opened_by=self.oup_employee,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            EmployeeShift.objects.create(
                employee=second_employee,
                shift_type=ShiftType.DAY,
                workplace_code='oup',
                opened_at=timezone.now(),
                opened_by=second_employee,
            )

        self.assertEqual(
            EmployeeShift.objects.filter(workplace_code='oup', closed_at__isnull=True).count(),
            1,
        )

    def test_closed_oup_period_does_not_block_new_period(self):
        second_employee, _second_access = self.create_oup_actor(suffix=92)
        EmployeeShift.objects.create(
            employee=self.oup_employee,
            shift_type=ShiftType.DAY,
            workplace_code='oup',
            opened_at=timezone.now() - timedelta(hours=1),
            closed_at=timezone.now(),
            opened_by=self.oup_employee,
            closed_by=self.oup_employee,
        )

        shift = EmployeeShift.objects.create(
            employee=second_employee,
            shift_type=ShiftType.DAY,
            workplace_code='oup',
            opened_at=timezone.now(),
            opened_by=second_employee,
        )

        self.assertEqual(shift.employee_id, second_employee.pk)

    def test_unique_open_oup_period_does_not_affect_other_workplaces(self):
        second_employee, _second_access = self.create_oup_actor(suffix=93)

        EmployeeShift.objects.create(
            employee=self.oup_employee,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            opened_at=timezone.now(),
            opened_by=self.oup_employee,
        )
        EmployeeShift.objects.create(
            employee=second_employee,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            opened_at=timezone.now(),
            opened_by=second_employee,
        )

        self.assertEqual(
            EmployeeShift.objects.filter(workplace_code='driver', closed_at__isnull=True).count(),
            2,
        )

    def test_same_actor_start_remains_idempotent_with_one_audit(self):
        first_shift, first_created = open_oup_shift(actor_access_id=self.oup_access.pk)
        second_shift, second_created = open_oup_shift(actor_access_id=self.oup_access.pk)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(second_shift.pk, first_shift.pk)
        self.assertEqual(
            AdminActionLog.objects.filter(action_code='oup_period_started').count(),
            1,
        )

    def test_other_actor_busy_keeps_message_and_does_not_add_audit(self):
        shift, created = open_oup_shift(actor_access_id=self.oup_access.pk)
        second_employee, second_access = self.create_oup_actor(suffix=94)

        with self.assertRaisesMessage(
            ValidationError,
            f'Редактирование кадровых данных уже выполняет {self.oup_employee.full_name}',
        ):
            open_oup_shift(actor_access_id=second_access.pk)

        self.assertTrue(created)
        self.assertEqual(shift.employee_id, self.oup_employee.pk)
        self.assertFalse(
            EmployeeShift.objects.filter(employee=second_employee, closed_at__isnull=True).exists()
        )
        self.assertEqual(
            AdminActionLog.objects.filter(action_code='oup_period_started').count(),
            1,
        )

    def test_insert_conflict_with_same_actor_winner_is_idempotent(self):
        winner = EmployeeShift(
            id=901,
            employee=self.oup_employee,
            shift_type=ShiftType.DAY,
            workplace_code='oup',
            opened_at=timezone.now(),
            opened_by=self.oup_employee,
        )

        with (
            patch.object(
                EmployeeShift.objects,
                'create',
                side_effect=IntegrityError('simulated unique_open_oup_period'),
            ),
            patch('users.oup_services.get_active_oup_period', return_value=winner),
        ):
            shift, created = open_oup_shift(actor_access_id=self.oup_access.pk)

        self.assertIs(shift, winner)
        self.assertFalse(created)
        self.assertFalse(AdminActionLog.objects.exists())

    def test_insert_conflict_with_other_actor_winner_is_controlled_busy(self):
        second_employee, _second_access = self.create_oup_actor(suffix=95)
        winner = EmployeeShift(
            id=902,
            employee=second_employee,
            shift_type=ShiftType.DAY,
            workplace_code='oup',
            opened_at=timezone.now(),
            opened_by=second_employee,
        )

        with (
            patch.object(
                EmployeeShift.objects,
                'create',
                side_effect=IntegrityError('simulated unique_open_oup_period'),
            ),
            patch('users.oup_services.get_active_oup_period', return_value=winner),
            self.assertRaisesMessage(
                ValidationError,
                f'Редактирование кадровых данных уже выполняет {second_employee.full_name}',
            ),
        ):
            open_oup_shift(actor_access_id=self.oup_access.pk)

        self.assertFalse(EmployeeShift.objects.exists())
        self.assertFalse(AdminActionLog.objects.exists())

    def test_insert_integrity_error_without_winner_is_not_masked(self):
        insert_error = IntegrityError('unrelated insert failure')

        with (
            patch.object(EmployeeShift.objects, 'create', side_effect=insert_error),
            patch('users.oup_services.get_active_oup_period', return_value=None),
            self.assertRaises(IntegrityError) as caught,
        ):
            open_oup_shift(actor_access_id=self.oup_access.pk)

        self.assertIs(caught.exception, insert_error)
        self.assertFalse(EmployeeShift.objects.exists())
        self.assertFalse(AdminActionLog.objects.exists())

    def test_oup_activation_does_not_show_pin_confirmation_banner(self):
        employee = Employee.objects.create(
            full_name='Новый специалист ОУП',
            phone='+79000000010',
            status=Employee.Status.NOT_ACTIVATED,
            is_active=True,
        )
        access = EmployeeAccess.objects.create(
            employee=employee,
            role=self.oup_role,
            access_code='246824',
            status=EmployeeAccess.Status.NOT_ACTIVATED,
            primary_code_issued_at=timezone.now(),
            is_active=True,
        )
        session = self.client.session
        session.pop('employee_access_id', None)
        session['pending_activation_access_id'] = access.id
        session.save()

        response = self.client.post(
            reverse('activate_access'),
            {
                'phone': '+7 900 000-00-10',
                'new_access_code': '864286',
                'confirm_access_code': '864286',
            },
            follow=True,
        )

        access.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request['PATH_INFO'], reverse('oup_employees'))
        self.assertEqual(access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertContains(response, 'Сотрудники')
        self.assertNotContains(
            response,
            'Постоянный пинкод создан. Первичный пинкод больше не действует.',
        )

    def test_oup_shift_lookup_uses_postgresql_safe_row_lock_without_distinct(self):
        with patch.object(
            EmployeeShift.objects,
            'select_for_update',
            wraps=EmployeeShift.objects.select_for_update,
        ) as shift_lock:
            self.start_shift()

        self.assertTrue(any(
            call.kwargs.get('of') == ('self',)
            for call in shift_lock.call_args_list
        ))

    def test_shift_views_pass_exact_session_access_id_and_keep_messages(self):
        with patch(
            'users.oup_views.open_oup_shift',
            return_value=(object(), True),
        ) as open_shift:
            response = self.client.post(reverse('oup_shift_start'))

        open_shift.assert_called_once_with(actor_access_id=self.oup_access.pk)
        self.assertIn(
            'Редактирование кадровых данных включено.',
            [str(message) for message in get_messages(response.wsgi_request)],
        )

        shift = self.start_shift()
        with patch(
            'users.oup_views.close_oup_shift',
            return_value=shift,
        ) as close_shift:
            response = self.client.post(reverse('oup_shift_close'))

        close_shift.assert_called_once_with(actor_access_id=self.oup_access.pk)
        self.assertIn(
            'Редактирование кадровых данных завершено.',
            [str(message) for message in get_messages(response.wsgi_request)],
        )

    def test_exact_shift_actor_does_not_fall_back_to_second_oup_access(self):
        EmployeeAccess.objects.create(
            employee=self.oup_employee,
            role=self.oup_role,
            access_code='700002',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        EmployeeAccess.objects.filter(pk=self.oup_access.pk).update(
            status=EmployeeAccess.Status.DEACTIVATED,
            is_active=False,
        )
        audit_before = AdminActionLog.objects.count()

        with self.assertRaisesMessage(
            ValidationError,
            'Активированный доступ ОУП не найден.',
        ):
            open_oup_shift(actor_access_id=self.oup_access.pk)

        self.assertFalse(EmployeeShift.objects.exists())
        self.assertEqual(AdminActionLog.objects.count(), audit_before)

    def test_exact_shift_actor_locks_employee_then_access_then_shift(self):
        lock_order = []
        original_select_for_update = QuerySet.select_for_update

        def traced_select_for_update(queryset, *args, **kwargs):
            lock_order.append((queryset.model, kwargs.get('of')))
            return original_select_for_update(queryset, *args, **kwargs)

        with patch.object(QuerySet, 'select_for_update', traced_select_for_update):
            open_oup_shift(actor_access_id=self.oup_access.pk)

        employee_index = next(
            index for index, (model, _of) in enumerate(lock_order)
            if model is Employee
        )
        access_index = next(
            index for index, (model, _of) in enumerate(lock_order)
            if model is EmployeeAccess
        )
        shift_indexes = [
            index for index, (model, _of) in enumerate(lock_order)
            if model is EmployeeShift
        ]
        self.assertLess(employee_index, access_index)
        self.assertTrue(shift_indexes)
        self.assertTrue(all(access_index < index for index in shift_indexes))
        self.assertEqual(lock_order[employee_index][1], ('self',))
        self.assertEqual(lock_order[access_index][1], ('self',))
        self.assertFalse(any(model is Role for model, _of in lock_order))
        self.assertFalse(any(
            model is Employee
            for model, _of in lock_order[access_index + 1:]
        ))

    def test_exact_shift_actor_rejects_stale_employee_mapping(self):
        replacement = Employee.objects.create(
            full_name='Другой специалист ОУП',
            phone='+79000000031',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        audit_before = AdminActionLog.objects.count()

        def reassign_before_lock(plan):
            EmployeeAccess.objects.filter(pk=self.oup_access.pk).update(
                employee=replacement,
            )
            return lock_employee_access_plan(plan)

        with (
            patch(
                'users.oup_services.lock_employee_access_plan',
                side_effect=reassign_before_lock,
            ),
            self.assertRaisesMessage(
                ValidationError,
                'Активированный доступ ОУП не найден.',
            ),
        ):
            open_oup_shift(actor_access_id=self.oup_access.pk)

        self.oup_access.refresh_from_db()
        self.assertEqual(self.oup_access.employee_id, self.oup_employee.pk)
        self.assertFalse(EmployeeShift.objects.exists())
        self.assertEqual(AdminActionLog.objects.count(), audit_before)

    def test_exact_shift_actor_rejects_stale_role_mapping(self):
        replacement_role = Role.objects.create(code='oup-copy', name='Другой ОУП')
        audit_before = AdminActionLog.objects.count()

        def change_role_before_lock(plan):
            EmployeeAccess.objects.filter(pk=self.oup_access.pk).update(
                role=replacement_role,
            )
            return lock_employee_access_plan(plan)

        with (
            patch(
                'users.oup_services.lock_employee_access_plan',
                side_effect=change_role_before_lock,
            ),
            self.assertRaisesMessage(
                ValidationError,
                'Активированный доступ ОУП не найден.',
            ),
        ):
            open_oup_shift(actor_access_id=self.oup_access.pk)

        self.oup_access.refresh_from_db()
        self.assertEqual(self.oup_access.role_id, self.oup_role.pk)
        self.assertFalse(EmployeeShift.objects.exists())
        self.assertEqual(AdminActionLog.objects.count(), audit_before)

    def test_exact_shift_actor_preserves_inactive_employee_and_role_errors(self):
        Employee.objects.filter(pk=self.oup_employee.pk).update(
            status=Employee.Status.DISMISSED,
            is_active=False,
        )
        with self.assertRaisesMessage(
            ValidationError,
            'Сотрудник ОУП неактивен или уволен.',
        ):
            open_oup_shift(actor_access_id=self.oup_access.pk)

        Employee.objects.filter(pk=self.oup_employee.pk).update(
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        Role.objects.filter(pk=self.oup_role.pk).update(is_active=False)
        with self.assertRaisesMessage(ValidationError, 'Роль ОУП не настроена.') as caught:
            open_oup_shift(actor_access_id=self.oup_access.pk)

        self.assertEqual(caught.exception.code, 'oup_role_unavailable')
        self.assertFalse(EmployeeShift.objects.exists())

    def test_exact_shift_actor_requires_strict_oup_role_code(self):
        Role.objects.filter(pk=self.oup_role.pk).update(code='former-oup')

        with self.assertRaisesMessage(ValidationError, 'Роль ОУП не настроена.') as caught:
            open_oup_shift(actor_access_id=self.oup_access.pk)

        self.assertEqual(caught.exception.code, 'oup_role_unavailable')
        self.assertFalse(EmployeeShift.objects.exists())

    def test_shift_audit_failure_rolls_back_open_and_close(self):
        audit_before = AdminActionLog.objects.count()
        with (
            patch('users.oup_services.log_oup_action', side_effect=RuntimeError('audit failed')),
            self.assertRaisesMessage(RuntimeError, 'audit failed'),
        ):
            open_oup_shift(actor_access_id=self.oup_access.pk)

        self.assertFalse(EmployeeShift.objects.exists())
        self.assertEqual(AdminActionLog.objects.count(), audit_before)

        shift = self.start_shift()
        audit_before = AdminActionLog.objects.count()
        with (
            patch('users.oup_services.log_oup_action', side_effect=RuntimeError('audit failed')),
            self.assertRaisesMessage(RuntimeError, 'audit failed'),
        ):
            from .oup_services import close_oup_shift

            close_oup_shift(actor_access_id=self.oup_access.pk)

        shift.refresh_from_db()
        self.assertIsNone(shift.closed_at)
        self.assertIsNone(shift.closed_by_id)
        self.assertEqual(AdminActionLog.objects.count(), audit_before)

    def test_shift_start_and_close_keep_audit_contract(self):
        shift, created = open_oup_shift(actor_access_id=self.oup_access.pk)
        self.assertTrue(created)
        self.assertTrue(AdminActionLog.objects.filter(
            actor=self.oup_employee,
            object_id=str(shift.pk),
            action_code='oup_period_started',
        ).exists())

        from .oup_services import close_oup_shift

        close_oup_shift(actor_access_id=self.oup_access.pk)
        shift.refresh_from_db()
        self.assertEqual(shift.closed_by_id, self.oup_employee.pk)
        self.assertTrue(AdminActionLog.objects.filter(
            actor=self.oup_employee,
            object_id=str(shift.pk),
            action_code='oup_period_finished',
        ).exists())

    def test_open_shift_reloads_employee_and_rejects_dismissed_stale_object(self):
        stale_employee = self.oup_employee
        Employee.objects.filter(pk=stale_employee.pk).update(
            status=Employee.Status.DISMISSED,
            is_active=False,
        )

        with self.assertRaises(ValidationError):
            open_oup_shift(actor_access_id=self.oup_access.pk)

        self.assertFalse(EmployeeShift.objects.filter(employee=stale_employee).exists())

    def test_open_shift_requires_current_activated_oup_access(self):
        EmployeeAccess.objects.filter(pk=self.oup_access.pk).update(
            status=EmployeeAccess.Status.DEACTIVATED,
            is_active=False,
        )

        with self.assertRaises(ValidationError):
            open_oup_shift(actor_access_id=self.oup_access.pk)

        self.assertFalse(EmployeeShift.objects.filter(employee=self.oup_employee).exists())

    def test_oup_write_context_locks_role_actor_access_and_shift_in_shared_order(self):
        self.start_shift()
        lock_order = []
        lock_methods = (
            ('role', Role.objects, Role.objects.select_for_update),
            ('employee', Employee.objects, Employee.objects.select_for_update),
            ('access', EmployeeAccess.objects, EmployeeAccess.objects.select_for_update),
            ('shift', EmployeeShift.objects, EmployeeShift.objects.select_for_update),
        )

        def record_lock(label, select_for_update):
            def wrapper(*args, **kwargs):
                lock_order.append(label)
                return select_for_update(*args, **kwargs)

            return wrapper

        patchers = [
            patch.object(manager, 'select_for_update', side_effect=record_lock(label, method))
            for label, manager, method in lock_methods
        ]
        for patcher in patchers:
            patcher.start()
        try:
            actor, shift = lock_oup_write_context(employee=self.oup_employee)
        finally:
            for patcher in reversed(patchers):
                patcher.stop()

        self.assertEqual(actor.pk, self.oup_employee.pk)
        self.assertEqual(shift.employee_id, self.oup_employee.pk)
        self.assertEqual(lock_order, ['role', 'employee', 'access', 'shift'])

    def test_oup_does_not_treat_another_workplace_shift_as_its_own(self):
        other_shift = EmployeeShift.objects.create(
            employee=self.oup_employee,
            shift_type=ShiftType.DAY,
            opened_at=timezone.now(),
            opened_by=self.oup_employee,
        )
        response = self.client.post(reverse('oup_shift_start'))
        self.assertRedirects(response, reverse('oup_employees'), fetch_redirect_response=False)
        other_shift.refresh_from_db()
        self.assertIsNone(other_shift.closed_at)
        self.assertFalse(EmployeeShift.objects.filter(
            employee=self.oup_employee,
            workplace_code='oup',
            closed_at__isnull=True,
        ).exists())

        self.client.post(reverse('oup_shift_close'))
        other_shift.refresh_from_db()
        self.assertIsNone(other_shift.closed_at)

    def test_shift_return_target_rejects_external_url(self):
        response = self.client.post(reverse('oup_shift_start'), {'next': 'https://example.com/phishing'})
        self.assertRedirects(response, reverse('oup_employees'), fetch_redirect_response=False)

    def test_create_requires_open_shift(self):
        response = self.client.post(reverse('oup_employee_create'), self.employee_payload())
        self.assertRedirects(response, reverse('oup_employees'), fetch_redirect_response=False)
        self.assertFalse(Employee.objects.filter(personnel_number='CR-1001').exists())

    def test_create_rechecks_shift_inside_write_transaction(self):
        self.start_shift()

        with patch(
            'users.oup_views._require_open_shift',
            side_effect=self.close_shift_after_outer_guard,
        ):
            response = self.client.post(reverse('oup_employee_create'), self.employee_payload())

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Employee.objects.filter(personnel_number='CR-1001').exists())
        self.assertContains(response, 'Сначала включите редактирование кадровых данных.')

    def test_employee_card_fields_are_disabled_without_oup_shift(self):
        employee = Employee.objects.create(
            full_name='Карточка Только Для Просмотра',
            personnel_number='READ-1',
            status=Employee.Status.ACTIVE,
        )
        response = self.client.get(reverse('oup_employee_detail', args=[employee.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].fields['full_name'].disabled)
        self.assertContains(response, 'Сейчас доступен только просмотр')

    def test_employee_dates_render_in_html_date_format(self):
        employee = Employee.objects.create(
            full_name='Сотрудник С Датами Полей',
            personnel_number='DATE-HTML-1',
            birth_date=timezone.localdate().replace(year=1990),
            hired_at=timezone.localdate(),
            status=Employee.Status.ACTIVE,
        )
        response = self.client.get(reverse('oup_employee_detail', args=[employee.id]))
        self.assertContains(response, f'value="{employee.birth_date:%Y-%m-%d}"')
        self.assertContains(response, f'value="{employee.hired_at:%Y-%m-%d}"')

    def test_photo_input_has_one_explicit_accessible_name(self):
        self.start_shift()
        response = self.client.get(reverse('oup_employee_create'))
        self.assertContains(response, 'aria-label="Выбрать фото сотрудника"')

    def test_create_employee_does_not_create_access_or_assignment(self):
        self.start_shift()
        response = self.client.post(
            reverse('oup_employee_create'),
            self.employee_payload(status=Employee.Status.DISMISSED, is_active=''),
        )
        employee = Employee.objects.get(personnel_number='CR-1001')
        self.assertRedirects(response, reverse('oup_employee_detail', args=[employee.id]), fetch_redirect_response=False)
        self.assertEqual(employee.status, Employee.Status.ACTIVE)
        self.assertTrue(employee.is_active)
        self.assertEqual(employee.work_category, Employee.WorkCategory.DRIVER)
        self.assertFalse(EmployeeAccess.objects.filter(employee=employee).exists())
        self.assertFalse(EquipmentAssignment.objects.filter(employee=employee).exists())
        self.assertTrue(AdminActionLog.objects.filter(
            actor=self.oup_employee,
            action='ОУП: создан сотрудник',
            object_type='Employee',
            object_id=str(employee.id),
        ).exists())

    def test_duplicate_personnel_number_is_rejected(self):
        self.start_shift()
        Employee.objects.create(full_name='Существующий сотрудник', personnel_number='CR-1001')
        response = self.client.post(reverse('oup_employee_create'), self.employee_payload())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Сотрудник с таким архивным идентификатором уже существует')

    def test_edit_logs_changes_by_stable_employee_id(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Старое Имя Сотрудника',
            personnel_number='CR-2001',
            phone='+79001112233',
            position='Водитель',
            department='Участок',
            work_category=Employee.WorkCategory.DRIVER,
            hired_at=timezone.localdate(),
            rotation='Вахта 1',
            sex=Employee.Sex.MALE,
            status=Employee.Status.ACTIVE,
        )
        response = self.client.post(
            reverse('oup_employee_detail', args=[employee.id]),
            self.employee_payload(
                full_name='Новое Имя Сотрудника',
                personnel_number='CR-2001',
                sex=Employee.Sex.FEMALE,
            ),
        )
        self.assertRedirects(response, reverse('oup_employee_detail', args=[employee.id]), fetch_redirect_response=False)
        log = AdminActionLog.objects.get(action='ОУП: изменена карточка сотрудника')
        self.assertEqual(log.object_id, str(employee.id))
        employee.refresh_from_db()
        self.assertEqual(employee.sex, Employee.Sex.FEMALE)
        self.assertIn('Старое Имя Сотрудника', log.old_value)
        self.assertIn('Новое Имя Сотрудника', log.old_value)
        self.assertIn('Пол: Мужской → Женский', log.old_value)
        detail = self.client.get(reverse('oup_employee_detail', args=[employee.id]))
        self.assertContains(detail, 'изменена карточка сотрудника')

    def test_oup_saves_and_audits_structural_watch_composition(self):
        self.start_shift()
        previous_composition = WatchComposition.objects.create(
            code='test-oup-watch-composition-previous',
            name='ТЕСТ_ОУП_Предыдущий состав вахты',
        )
        next_composition = WatchComposition.objects.create(
            code='test-oup-watch-composition-next',
            name='ТЕСТ_ОУП_Новый состав вахты',
        )
        employee = Employee.objects.create(
            full_name='Сотрудник Состава Вахты',
            personnel_number='CR-WATCH-1',
            phone='+79001112234',
            position='Водитель',
            department='Участок',
            work_category=Employee.WorkCategory.DRIVER,
            hired_at=timezone.localdate(),
            rotation='Legacy подпись не определяет состав',
            watch_composition=previous_composition,
            status=Employee.Status.ACTIVE,
        )

        response = self.client.post(
            reverse('oup_employee_detail', args=[employee.id]),
            self.employee_payload(
                full_name=employee.full_name,
                personnel_number=employee.personnel_number,
                phone=employee.phone,
                watch_composition=str(next_composition.id),
            ),
        )

        self.assertRedirects(
            response,
            reverse('oup_employee_detail', args=[employee.id]),
            fetch_redirect_response=False,
        )
        employee.refresh_from_db()
        self.assertEqual(employee.watch_composition, next_composition)
        log = AdminActionLog.objects.get(
            action='ОУП: изменена карточка сотрудника',
            object_id=str(employee.id),
        )
        self.assertIn(previous_composition.name, log.old_value)
        self.assertIn(next_composition.name, log.old_value)

    def test_edit_rechecks_shift_inside_write_transaction(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Исходное Имя Сотрудника',
            personnel_number='CR-RACE-EDIT',
            work_category=Employee.WorkCategory.DRIVER,
            hired_at=timezone.localdate(),
            status=Employee.Status.ACTIVE,
        )

        with patch(
            'users.oup_views._require_open_shift',
            side_effect=self.close_shift_after_outer_guard,
        ):
            response = self.client.post(
                reverse('oup_employee_detail', args=[employee.id]),
                self.employee_payload(
                    full_name='Несохраненное Новое Имя',
                    personnel_number=employee.personnel_number,
                ),
            )

        self.assertEqual(response.status_code, 200)
        employee.refresh_from_db()
        self.assertEqual(employee.full_name, 'Исходное Имя Сотрудника')
        self.assertContains(response, 'Сначала включите редактирование кадровых данных.')

    def test_photo_remove_rechecks_shift_inside_write_transaction(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Сотрудник С Фото',
            personnel_number='CR-RACE-PHOTO',
            photo='employee_photos/existing.jpg',
            status=Employee.Status.ACTIVE,
        )

        with patch(
            'users.oup_views._require_open_shift',
            side_effect=self.close_shift_after_outer_guard,
        ):
            response = self.client.post(
                reverse('oup_employee_detail', args=[employee.id]),
                {'remove_photo': '1'},
            )

        self.assertRedirects(
            response,
            reverse('oup_employee_detail', args=[employee.id]),
            fetch_redirect_response=False,
        )
        employee.refresh_from_db()
        self.assertEqual(employee.photo.name, 'employee_photos/existing.jpg')

    def test_stale_dismissed_target_is_not_edited(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Активная Карточка До Гонки',
            personnel_number='CR-STALE-EDIT',
            work_category=Employee.WorkCategory.DRIVER,
            hired_at=timezone.localdate(),
            status=Employee.Status.ACTIVE,
        )

        def dismiss_target_after_outer_guard(*_args, **_kwargs):
            Employee.objects.filter(pk=employee.pk).update(
                status=Employee.Status.DISMISSED,
                is_active=False,
                dismissed_at=timezone.localdate(),
            )
            return True

        with patch('users.oup_views._require_open_shift', side_effect=dismiss_target_after_outer_guard):
            response = self.client.post(
                reverse('oup_employee_detail', args=[employee.id]),
                self.employee_payload(
                    full_name='Имя Не Должно Сохраниться',
                    personnel_number=employee.personnel_number,
                ),
            )

        self.assertEqual(response.status_code, 200)
        employee.refresh_from_db()
        self.assertEqual(employee.status, Employee.Status.DISMISSED)
        self.assertEqual(employee.full_name, 'Активная Карточка До Гонки')

    def test_stale_archived_target_photo_is_not_removed(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Карточка С Архивным Фото',
            personnel_number='CR-STALE-PHOTO',
            photo='employee_photos/archived.jpg',
            status=Employee.Status.ACTIVE,
        )

        def archive_target_after_outer_guard(*_args, **_kwargs):
            Employee.objects.filter(pk=employee.pk).update(
                status=Employee.Status.ARCHIVED,
                is_active=False,
            )
            return True

        with patch('users.oup_views._require_open_shift', side_effect=archive_target_after_outer_guard):
            response = self.client.post(
                reverse('oup_employee_detail', args=[employee.id]),
                {'remove_photo': '1'},
            )

        self.assertRedirects(
            response,
            reverse('oup_employee_detail', args=[employee.id]),
            fetch_redirect_response=False,
        )
        employee.refresh_from_db()
        self.assertEqual(employee.status, Employee.Status.ARCHIVED)
        self.assertEqual(employee.photo.name, 'employee_photos/archived.jpg')

    def test_work_category_change_is_blocked_by_active_assignment(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Категория С Назначением',
            personnel_number='CR-ROLE-1',
            phone='+79001112233',
            position='Водитель',
            department='Участок',
            work_category=Employee.WorkCategory.DRIVER,
            hired_at=timezone.localdate(),
            rotation='Вахта 1',
            status=Employee.Status.ACTIVE,
        )
        equipment_type = EquipmentType.objects.create(name='Самосвал')
        equipment_model = EquipmentModel.objects.create(equipment_type=equipment_type, name='БелАЗ')
        equipment = Equipment.objects.create(equipment_type=equipment_type, model=equipment_model, garage_number='ROLE-1')
        EquipmentAssignment.objects.create(
            employee=employee,
            role=self.driver_role,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
            assigned_by=self.oup_employee,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        response = self.client.post(
            reverse('oup_employee_detail', args=[employee.id]),
            self.employee_payload(
                full_name=employee.full_name,
                personnel_number=employee.personnel_number,
                work_category=Employee.WorkCategory.EXCAVATOR_OPERATOR,
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'есть действующее назначение на технику')
        employee.refresh_from_db()
        self.assertEqual(employee.work_category, Employee.WorkCategory.DRIVER)

    def test_dismissal_is_blocked_by_open_shift(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Работающий Водитель',
            personnel_number='CR-3001',
            status=Employee.Status.ACTIVE,
        )
        EmployeeShift.objects.create(
            employee=employee,
            shift_type=ShiftType.DAY,
            opened_at=timezone.now(),
            opened_by=employee,
        )
        response = self.client.post(
            reverse('oup_employee_dismiss', args=[employee.id]),
            {'dismissed_at': timezone.localdate().isoformat(), 'reason': ''},
        )
        self.assertEqual(response.status_code, 200)
        employee.refresh_from_db()
        self.assertTrue(employee.is_active)
        self.assertContains(response, 'Увольнение сейчас заблокировано')

    def test_dismissal_rechecks_shift_inside_write_transaction(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Сотрудник Для Проверки Гонки Увольнения',
            personnel_number='CR-RACE-DISMISS',
            hired_at=timezone.localdate(),
            status=Employee.Status.ACTIVE,
        )

        with patch(
            'users.oup_views._require_open_shift',
            side_effect=self.close_shift_after_outer_guard,
        ):
            response = self.client.post(
                reverse('oup_employee_dismiss', args=[employee.id]),
                {'dismissed_at': timezone.localdate().isoformat(), 'reason': ''},
            )

        self.assertEqual(response.status_code, 200)
        employee.refresh_from_db()
        self.assertEqual(employee.status, Employee.Status.ACTIVE)
        self.assertTrue(employee.is_active)
        self.assertContains(response, 'Сначала включите редактирование кадровых данных.')

    def test_successful_dismissal_deactivates_access_and_closes_assignment(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Водитель Для Увольнения',
            personnel_number='CR-4001',
            phone='+79002223344',
            work_category=Employee.WorkCategory.DRIVER,
            status=Employee.Status.ACTIVE,
        )
        employee_access = EmployeeAccess.objects.create(
            employee=employee,
            role=self.driver_role,
            access_code='200001',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        equipment_type = EquipmentType.objects.create(name='Самосвал')
        equipment_model = EquipmentModel.objects.create(equipment_type=equipment_type, name='БелАЗ')
        equipment = Equipment.objects.create(equipment_type=equipment_type, model=equipment_model, garage_number='101')
        assignment = EquipmentAssignment.objects.create(
            employee=employee,
            role=self.driver_role,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
            assigned_by=self.oup_employee,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        response = self.client.post(
            reverse('oup_employee_dismiss', args=[employee.id]),
            {'dismissed_at': timezone.localdate().isoformat(), 'reason': 'Получено подтверждение из 1С'},
        )
        self.assertRedirects(response, reverse('oup_dismissed_employees'), fetch_redirect_response=False)
        employee.refresh_from_db()
        employee_access.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(employee.status, Employee.Status.DISMISSED)
        self.assertFalse(employee.is_active)
        self.assertFalse(employee_access.is_active)
        self.assertEqual(employee_access.status, EmployeeAccess.Status.DEACTIVATED)
        self.assertIsNotNone(assignment.ended_at)
        self.assertTrue(AdminActionLog.objects.filter(
            action='ОУП: уволен сотрудник', object_id=str(employee.id)
        ).exists())

    def test_deactivated_employee_can_be_formally_dismissed_by_oup(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Деактивированный Сотрудник',
            personnel_number='CR-DEACT-1',
            hired_at=timezone.localdate(),
            status=Employee.Status.DEACTIVATED,
            is_active=False,
        )
        response = self.client.post(
            reverse('oup_employee_dismiss', args=[employee.id]),
            {'dismissed_at': timezone.localdate().isoformat(), 'reason': ''},
        )
        self.assertRedirects(response, reverse('oup_dismissed_employees'), fetch_redirect_response=False)
        employee.refresh_from_db()
        self.assertEqual(employee.status, Employee.Status.DISMISSED)

    def test_dismissal_date_cannot_precede_hire_date(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Сотрудник С Датами',
            personnel_number='CR-DATE-1',
            hired_at=timezone.localdate(),
            status=Employee.Status.ACTIVE,
        )
        response = self.client.post(
            reverse('oup_employee_dismiss', args=[employee.id]),
            {'dismissed_at': (timezone.localdate() - timedelta(days=1)).isoformat(), 'reason': ''},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Дата увольнения не может быть раньше даты приема')
        employee.refresh_from_db()
        self.assertEqual(employee.status, Employee.Status.ACTIVE)

    def test_dismissal_rebases_current_draft_and_keeps_planned_replacement(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Сотрудник В Текущем Черновике',
            personnel_number='CR-DRAFT-1',
            work_category=Employee.WorkCategory.DRIVER,
            hired_at=timezone.localdate() - timedelta(days=10),
            status=Employee.Status.ACTIVE,
        )
        replacement = Employee.objects.create(
            full_name='Запланированный Новый Водитель',
            personnel_number='CR-DRAFT-2',
            work_category=Employee.WorkCategory.DRIVER,
            hired_at=timezone.localdate() - timedelta(days=5),
            status=Employee.Status.ACTIVE,
        )
        equipment_type = EquipmentType.objects.create(name='Самосвал')
        equipment_model = EquipmentModel.objects.create(equipment_type=equipment_type, name='БелАЗ')
        equipment = Equipment.objects.create(equipment_type=equipment_type, model=equipment_model, garage_number='DRAFT-1')
        EquipmentAssignment.objects.create(
            employee=employee,
            equipment=equipment,
            role=self.driver_role,
            shift_type=WorkShiftType.SHIFT_1,
            status=AssignmentStatus.ACCEPTED,
            assigned_at=timezone.now(),
        )
        production_date = timezone.localdate() - timedelta(days=1)
        plan = CrewPlan.objects.create(
            work_date=production_date,
            role=self.driver_role,
            status=CrewPlanStatus.DRAFT,
            created_by=self.oup_employee,
        )
        slot = CrewPlanSlot.objects.create(
            plan=plan,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
            employee=replacement,
            baseline_employee=employee,
        )
        CrewPlanSlot.objects.create(
            plan=plan,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_2,
            employee=None,
            baseline_employee=None,
        )
        with patch('users.oup_services.production_work_date', return_value=production_date):
            response = self.client.post(
                reverse('oup_employee_dismiss', args=[employee.id]),
                {'dismissed_at': timezone.localdate().isoformat(), 'reason': ''},
            )
        self.assertRedirects(response, reverse('oup_dismissed_employees'), fetch_redirect_response=False)
        slot.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(slot.employee_id, replacement.id)
        self.assertIsNone(slot.baseline_employee_id)

        with patch('assignments.services.production_work_date', return_value=production_date):
            published = publish_crew_plan(
                plan=plan,
                expected_version=plan.version,
                actor=self.oup_employee,
            )
        self.assertEqual(published.status, CrewPlanStatus.PUBLISHED)
        self.assertTrue(EquipmentAssignment.objects.filter(
            employee=replacement,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
        ).exists())

    def test_oup_log_contains_only_current_specialist_actions(self):
        self.start_shift()
        AdminActionLog.objects.create(
            actor=self.oup_employee,
            action='ОУП: начата дневная смена',
            object_type='EmployeeShift',
            object_repr='Иванова Анна Сергеевна / Дневная / 01.07.2026',
        )
        AdminActionLog.objects.create(actor=None, action='ОУП: чужое действие', object_repr='Другой сотрудник')
        response = self.client.get(reverse('oup_logs'))
        self.assertContains(response, 'включено редактирование')
        self.assertContains(response, 'включено редактирование · 2')
        self.assertNotContains(response, 'начата дневная смена')
        self.assertNotContains(response, 'Иванова Анна Сергеевна / Дневная')
        self.assertNotContains(response, 'чужое действие')

    def test_oup_log_is_paginated_without_hiding_older_actions(self):
        AdminActionLog.objects.bulk_create([
            AdminActionLog(
                actor=self.oup_employee,
                action='ОУП: тестовое действие',
                object_repr=f'Сотрудник {index}',
            )
            for index in range(55)
        ])
        response = self.client.get(reverse('oup_logs'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].paginator.count, 55)
        self.assertEqual(len(response.context['logs']), 50)
        self.assertContains(response, 'Дальше')

        second_page = self.client.get(reverse('oup_logs'), {'page': 2})
        self.assertEqual(len(second_page.context['logs']), 5)

    def test_dismissed_registry_does_not_offer_employee_creation(self):
        self.start_shift()
        response = self.client.get(reverse('oup_dismissed_employees'))
        self.assertNotContains(response, 'Создать сотрудника')

    def test_employee_registry_uses_shared_creation_label(self):
        self.start_shift()
        response = self.client.get(reverse('oup_employees'))
        self.assertContains(response, '>Создать сотрудника<', html=False)
        self.assertNotContains(response, 'Добавить сотрудника')


class OupSeedCommandTests(TestCase):
    def test_demo_oup_employee_is_active_and_can_open_shift(self):
        call_command('seed_mvp_roles', '--with-demo-users', stdout=StringIO())

        employee = Employee.objects.get(full_name='Специалист ОУП MVP')
        access = EmployeeAccess.objects.get(employee=employee, role__code='oup')
        self.assertEqual(employee.status, Employee.Status.ACTIVE)
        self.assertTrue(employee.is_active)
        self.assertEqual(access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertTrue(access.is_active)

        shift, created = open_oup_shift(actor_access_id=access.pk)

        self.assertTrue(created)
        self.assertEqual(shift.shift_type, ShiftType.DAY)
        self.assertEqual(shift.workplace_code, 'oup')


class OupPeriodConstraintMigrationTests(TestCase):
    @staticmethod
    def migration_apps(open_period_count):
        class HistoricalQuerySet:
            def __init__(self, rows):
                self.rows = rows

            def count(self):
                return len(self.rows)

        class HistoricalManager:
            def __init__(self, rows):
                self.rows = rows
                self.filters = []

            def filter(self, **kwargs):
                self.filters.append(kwargs)
                return HistoricalQuerySet(self.rows)

        class HistoricalEmployeeShift:
            objects = HistoricalManager(list(range(open_period_count)))

        class HistoricalApps:
            def get_model(self, app_label, model_name):
                if (app_label, model_name) != ('shifts', 'EmployeeShift'):
                    raise AssertionError((app_label, model_name))
                return HistoricalEmployeeShift

        return HistoricalApps(), HistoricalEmployeeShift

    def test_migration_precondition_accepts_zero_or_one_open_period(self):
        migration = import_module('shifts.migrations.0013_unique_open_oup_period')

        for open_period_count in (0, 1):
            with self.subTest(open_period_count=open_period_count):
                apps, historical_shift = self.migration_apps(open_period_count)
                migration.assert_single_open_oup_period(apps, schema_editor=None)
                migration.noop_reverse(apps, schema_editor=None)
                self.assertEqual(
                    historical_shift.objects.filters,
                    [{'workplace_code': 'oup', 'closed_at__isnull': True}],
                )

    def test_migration_precondition_rejects_duplicates_without_cleanup(self):
        migration = import_module('shifts.migrations.0013_unique_open_oup_period')
        apps, historical_shift = self.migration_apps(3)
        rows_before = list(historical_shift.objects.rows)

        with self.assertRaisesMessage(RuntimeError, 'found 3 open OUP periods'):
            migration.assert_single_open_oup_period(apps, schema_editor=None)

        self.assertEqual(historical_shift.objects.rows, rows_before)
        self.assertEqual(
            historical_shift.objects.filters,
            [{'workplace_code': 'oup', 'closed_at__isnull': True}],
        )


class OupEmployeeStatusMigrationTests(TransactionTestCase):
    migrate_from = ('users', '0009_employee_oup_fields_and_audit_target')
    migrate_to = ('users', '0010_normalize_activated_employee_status')

    def _restore_latest_migrations(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def setUp(self):
        super().setUp()
        self.addCleanup(self._restore_latest_migrations)
        executor = MigrationExecutor(connection)
        if connection.vendor == 'postgresql':
            executor.loader.get_migration(
                'users',
                '0014_allow_work_schedules_without_brigades',
            ).atomic = False
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Employee = old_apps.get_model('users', 'Employee')
        EmployeeAccess = old_apps.get_model('users', 'EmployeeAccess')
        Role = old_apps.get_model('users', 'Role')

        role = Role.objects.create(
            code='migration_status_driver',
            name='Водитель для миграции статуса',
            is_active=True,
        )
        self.activated_employee_id = Employee.objects.create(
            full_name='Активированный Legacy Сотрудник',
            status='not_activated',
            is_active=True,
        ).pk
        self.pending_employee_id = Employee.objects.create(
            full_name='Неактивированный Legacy Сотрудник',
            status='not_activated',
            is_active=True,
        ).pk
        self.deactivated_employee_id = Employee.objects.create(
            full_name='Деактивированный Legacy Сотрудник',
            status='deactivated',
            is_active=True,
        ).pk
        EmployeeAccess.objects.create(
            employee_id=self.activated_employee_id,
            role=role,
            access_code='981001',
            status='activated',
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee_id=self.pending_employee_id,
            role=role,
            access_code='981002',
            status='not_activated',
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee_id=self.deactivated_employee_id,
            role=role,
            access_code='981003',
            status='activated',
            is_active=True,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def test_only_legacy_employee_with_activated_access_becomes_active(self):
        Employee = self.apps.get_model('users', 'Employee')

        self.assertEqual(
            Employee.objects.get(pk=self.activated_employee_id).status,
            'active',
        )
        self.assertEqual(
            Employee.objects.get(pk=self.pending_employee_id).status,
            'not_activated',
        )
        self.assertEqual(
            Employee.objects.get(pk=self.deactivated_employee_id).status,
            'deactivated',
        )


class OupAdminActionLogPermissionsTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.model_admin = AdminActionLogAdmin(AdminActionLog, admin.site)
        self.log = AdminActionLog.objects.create(action='ОУП: тест прав журнала')

    def request_for(self, user):
        request = self.factory.get('/admin/users/adminactionlog/')
        request.user = user
        return request

    def test_staff_object_view_requires_explicit_view_permission(self):
        user_model = get_user_model()
        staff_without_permission = user_model.objects.create_user(
            username='oup-log-no-view',
            password='test-password',
            is_staff=True,
        )
        staff_with_permission = user_model.objects.create_user(
            username='oup-log-view',
            password='test-password',
            is_staff=True,
        )
        view_permission = Permission.objects.get(
            content_type__app_label='users',
            content_type__model='adminactionlog',
            codename='view_adminactionlog',
        )
        staff_with_permission.user_permissions.add(view_permission)

        self.assertFalse(self.model_admin.has_view_permission(
            self.request_for(staff_without_permission),
            obj=self.log,
        ))
        self.assertTrue(self.model_admin.has_view_permission(
            self.request_for(staff_with_permission),
            obj=self.log,
        ))

    def test_change_permission_is_always_denied(self):
        user_model = get_user_model()
        staff_with_permission = user_model.objects.create_user(
            username='oup-log-change',
            password='test-password',
            is_staff=True,
        )
        change_permission = Permission.objects.get(
            content_type__app_label='users',
            content_type__model='adminactionlog',
            codename='change_adminactionlog',
        )
        staff_with_permission.user_permissions.add(change_permission)
        superuser = user_model.objects.create_superuser(
            username='oup-log-superuser',
            password='test-password',
            email='oup-log-superuser@example.test',
        )

        for user in (staff_with_permission, superuser):
            with self.subTest(username=user.username):
                self.assertFalse(self.model_admin.has_change_permission(
                    self.request_for(user),
                    obj=self.log,
                ))


class OupOpenShiftAdminSafetyTests(TestCase):
    def setUp(self):
        self.admin_role = Role.objects.create(code='admin', name='Администратор')
        self.oup_role, _created = Role.objects.update_or_create(
            code='oup',
            defaults={'name': 'Специалист ОУП', 'is_active': True},
        )
        self.admin_employee = Employee.objects.create(
            full_name='Администратор Проверки ОУП',
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
        self.oup_employee = Employee.objects.create(
            full_name='Специалист ОУП С Открытой Сменой',
            personnel_number='OUP-OPEN-SHIFT',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.oup_access = EmployeeAccess.objects.create(
            employee=self.oup_employee,
            role=self.oup_role,
            access_code='812345',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.shift, _created = open_oup_shift(actor_access_id=self.oup_access.pk)
        session = self.client.session
        session['employee_access_id'] = self.admin_access.id
        session.save()

    def assert_target_unchanged(self):
        self.oup_employee.refresh_from_db()
        self.oup_access.refresh_from_db()
        self.shift.refresh_from_db()
        self.assertEqual(self.oup_employee.status, Employee.Status.ACTIVE)
        self.assertTrue(self.oup_employee.is_active)
        self.assertEqual(self.oup_access.access_code, '812345')
        self.assertEqual(self.oup_access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertTrue(self.oup_access.is_active)
        self.assertIsNone(self.oup_access.blocked_at)
        self.assertEqual(self.oup_access.block_reason, '')
        self.assertIsNone(self.oup_access.deactivated_at)
        self.assertIsNone(self.shift.closed_at)

    def test_admin_cannot_mutate_employee_or_access_during_open_oup_shift(self):
        actions = (
            (
                'reset_pin',
                reverse('system_admin_generate_access', args=[self.oup_employee.id]),
                {'role': self.oup_role.id},
            ),
            (
                'block_access',
                reverse('system_admin_access_action', args=[self.oup_access.id, 'block']),
                {'reason': 'Проверка блокировки'},
            ),
            (
                'deactivate_access',
                reverse('system_admin_access_action', args=[self.oup_access.id, 'deactivate']),
                {},
            ),
            (
                'deactivate_employee',
                reverse('system_admin_employee_status_action', args=[self.oup_employee.id, 'deactivate']),
                {},
            ),
            (
                'archive_employee',
                reverse('system_admin_employee_status_action', args=[self.oup_employee.id, 'archive']),
                {},
            ),
        )

        for action_name, url, payload in actions:
            with self.subTest(action=action_name):
                response = self.client.post(url, payload)
                self.assertEqual(response.status_code, 302)
                self.assert_target_unchanged()

    def test_admin_employee_form_keeps_status_read_only(self):
        detail = self.client.get(
            reverse('system_admin_employee_detail', args=[self.oup_employee.id]),
        )

        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'id="employee-status-readonly"', html=False)
        self.assertContains(detail, self.oup_employee.get_status_display())
        self.assertNotContains(detail, 'name="status"', html=False)

        response = self.client.post(
            reverse('system_admin_employee_detail', args=[self.oup_employee.id]),
            {
                'full_name': self.oup_employee.full_name,
                'personnel_number': self.oup_employee.personnel_number,
                'status': Employee.Status.ARCHIVED,
                'position': '',
                'phone': '',
                'comment': '',
                'hired_at': '',
                'dismissed_at': '',
                'rotation': '',
                'residence_text': '',
                'hr_data': '',
                'assignment_role': '',
                'assignment_shift_type': '',
                'assignment_equipment': '',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assert_target_unchanged()

    def test_admin_employee_card_shows_oup_as_non_equipment_work_role(self):
        detail = self.client.get(
            reverse('system_admin_employee_detail', args=[self.oup_employee.id]),
        )

        self.assertEqual(detail.status_code, 200)
        form = detail.context['form']
        self.assertEqual(
            list(form.fields['assignment_role'].queryset),
            [self.oup_role],
        )
        self.assertEqual(int(form['assignment_role'].value()), self.oup_role.id)
        self.assertContains(detail, 'data-work-role="oup"', html=False)
        self.assertContains(detail, 'data-supports-equipment="false"', html=False)
        self.assertContains(
            detail,
            'Для этой роли постоянное назначение на смену и технику не требуется.',
        )

        response = self.client.post(
            reverse('system_admin_employee_detail', args=[self.oup_employee.id]),
            {
                'full_name': self.oup_employee.full_name,
                'personnel_number': self.oup_employee.personnel_number,
                'position': '',
                'phone': '',
                'comment': '',
                'hired_at': '',
                'dismissed_at': '',
                'rotation': '',
                'residence_text': '',
                'hr_data': '',
                'assignment_role': self.oup_role.id,
                'assignment_shift_type': '',
                'assignment_equipment': '',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(EquipmentAssignment.objects.filter(employee=self.oup_employee).exists())
        self.assert_target_unchanged()

    def test_admin_employee_form_renders_hr_dates_in_html_date_format(self):
        self.oup_employee.hired_at = timezone.localdate() - timedelta(days=10)
        self.oup_employee.dismissed_at = timezone.localdate() - timedelta(days=1)
        self.oup_employee.save(update_fields=['hired_at', 'dismissed_at'])

        detail = self.client.get(
            reverse('system_admin_employee_detail', args=[self.oup_employee.id]),
        )

        self.assertEqual(detail.status_code, 200)
        self.assertContains(
            detail,
            f'name="hired_at" value="{self.oup_employee.hired_at:%Y-%m-%d}"',
            html=False,
        )
        self.assertContains(
            detail,
            f'name="dismissed_at" value="{self.oup_employee.dismissed_at:%Y-%m-%d}"',
            html=False,
        )


class OupPhotoUploadTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix='oup-photo-tests-')
        self.override = override_settings(MEDIA_ROOT=self.media_root)
        self.override.enable()
        role, _created = Role.objects.update_or_create(
            code='oup', defaults={'name': 'Специалист ОУП', 'is_active': True}
        )
        employee = Employee.objects.create(
            full_name='Фотограф ОУП', phone='+79000000008', status=Employee.Status.ACTIVE
        )
        access = EmployeeAccess.objects.create(
            employee=employee, role=role, access_code='800000', status=EmployeeAccess.Status.ACTIVATED
        )
        session = self.client.session
        session['employee_access_id'] = access.id
        session.save()
        EmployeeShift.objects.create(
            employee=employee,
            shift_type=ShiftType.DAY,
            workplace_code='oup',
            opened_at=timezone.now(),
            opened_by=employee,
        )

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_photo_upload_is_saved_and_optimized(self):
        source = BytesIO()
        Image.new('RGB', (900, 700), color=(35, 120, 70)).save(source, format='PNG')
        photo = SimpleUploadedFile('employee.png', source.getvalue(), content_type='image/png')
        response = self.client.post(
            reverse('oup_employee_create'),
            {
                'full_name': 'Сотрудник С Фотографией',
                'personnel_number': 'PHOTO-1',
                'phone': '+79001234567',
                'position': 'Специалист',
                'department': 'ОУП',
                'work_category': Employee.WorkCategory.OTHER,
                'hired_at': timezone.localdate().isoformat(),
                'rotation': 'Вахта 1',
                'comment': '',
                'photo': photo,
            },
        )
        employee = Employee.objects.get(personnel_number='PHOTO-1')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(employee.photo.name.endswith('.jpg'))
        with Image.open(employee.photo.path) as saved:
            self.assertLessEqual(max(saved.size), 512)

    def test_photo_without_content_type_is_still_verified_and_optimized(self):
        source = BytesIO()
        Image.new('RGB', (640, 480), color=(80, 90, 100)).save(source, format='PNG')
        photo = SimpleUploadedFile('employee.bin', source.getvalue(), content_type=None)

        optimized = optimize_employee_photo(photo)

        self.assertTrue(optimized.name.endswith('.jpg'))
        with Image.open(optimized) as saved:
            self.assertEqual(saved.format, 'JPEG')

    def test_photo_without_content_type_rejects_arbitrary_file(self):
        upload = SimpleUploadedFile(
            'employee.bin',
            b'<html><script>alert(1)</script></html>',
            content_type=None,
        )

        with self.assertRaises(ValidationError):
            optimize_employee_photo(upload)
