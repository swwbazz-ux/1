import json
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from references.models import Dormitory, DormitoryBlock, DormitorySection, Equipment, EquipmentModel, EquipmentType
from shifts.models import EmployeeShift, EquipmentPlanGroup, PlanCalculationMode
from users.models import DriverPrimaryRegistration, Employee, EmployeeAccess, Role
from users.forms import AdminEmployeeEditForm

from .models import AssignmentStatus, EquipmentAssignment, WorkShiftType
from .services import (
    clear_active_equipment_assignment,
    equipment_queryset_for_work_role,
    get_active_equipment_assignment,
    set_active_equipment_assignment,
    work_assignment_state,
)


class WorkAssignmentFixtureMixin:
    def setUp(self):
        self.driver_role = Role.objects.create(code='driver', name='Водитель самосвала')
        self.excavator_role = Role.objects.create(
            code='excavator_operator',
            name='Машинист экскаватора',
        )
        self.admin = Employee.objects.create(
            full_name='Администратор',
            status=Employee.Status.ACTIVE,
        )
        self.driver = self.create_employee_with_access('Водитель 1', self.driver_role, '210001')
        self.other_driver = self.create_employee_with_access('Водитель 2', self.driver_role, '210002')
        self.operator = self.create_employee_with_access('Машинист 1', self.excavator_role, '310001')

        self.truck_type = EquipmentType.objects.create(name='Самосвал')
        self.excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.excavator_model = EquipmentModel.objects.create(
            equipment_type=self.excavator_type,
            name='Экскаватор 4000 тест',
            fuel_capacity_limit_l=7000,
        )
        self.truck_model = EquipmentModel.objects.create(
            equipment_type=self.truck_type,
            name='БелАЗ тестовый',
            fuel_capacity_limit_l=2000,
        )
        self.truck_1 = Equipment.objects.create(equipment_type=self.truck_type, model=self.truck_model, garage_number='Т-01')
        self.truck_2 = Equipment.objects.create(equipment_type=self.truck_type, model=self.truck_model, garage_number='Т-02')
        self.excavator_1 = Equipment.objects.create(equipment_type=self.excavator_type, model=self.excavator_model, garage_number='Э-01')
        self.excavator_2 = Equipment.objects.create(equipment_type=self.excavator_type, model=self.excavator_model, garage_number='Э-02')

        truck_group = EquipmentPlanGroup.objects.create(
            code='test-trucks',
            name='Тестовые самосвалы',
            calculation_mode=PlanCalculationMode.TRIPS,
            plan_value='10.00',
        )
        truck_group.equipment.add(self.truck_1, self.truck_2)
        excavator_group = EquipmentPlanGroup.objects.create(
            code='test-excavators',
            name='Тестовые экскаваторы',
            calculation_mode=PlanCalculationMode.VOLUME,
            plan_value='1000.00',
        )
        excavator_group.equipment.add(self.excavator_1, self.excavator_2)

        dormitory = Dormitory.objects.create(number='Тест')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Блок')
        section = DormitorySection.objects.create(block=block, name='Секция')
        DriverPrimaryRegistration.objects.create(employee=self.driver, dormitory_section=section)

    def create_employee_with_access(self, name, role, access_code):
        employee = Employee.objects.create(full_name=name, status=Employee.Status.ACTIVE)
        EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code=access_code,
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        return employee

    def authenticated_client(self, employee, role):
        access = EmployeeAccess.objects.get(employee=employee, role=role, is_active=True)
        client = Client()
        session = client.session
        session['employee_access_id'] = access.id
        session.save()
        return client

    def assign(self, employee, role, equipment, shift_type):
        assignment, created = set_active_equipment_assignment(
            employee=employee,
            role=role,
            equipment=equipment,
            shift_type=shift_type,
            assigned_by=self.admin,
        )
        self.assertTrue(created)
        return assignment


class WorkAssignmentServiceTests(WorkAssignmentFixtureMixin, TestCase):
    def test_equipment_role_uses_reference_type_and_does_not_require_plan_group(self):
        truck_without_group = Equipment.objects.create(
            equipment_type=self.truck_type,
            garage_number='Т-03',
        )
        wrong_group = EquipmentPlanGroup.objects.create(
            code='wrong-plan-mode',
            name='Ошибочно настроенная группа',
            calculation_mode=PlanCalculationMode.TRIPS,
            plan_value='5.00',
        )
        wrong_group.equipment.add(self.excavator_1)

        driver_equipment = equipment_queryset_for_work_role('driver')
        self.assertIn(truck_without_group, driver_equipment)
        self.assertNotIn(self.excavator_1, driver_equipment)

    def test_legacy_assignment_is_not_treated_as_permanent_work_assignment(self):
        EquipmentAssignment.objects.create(
            employee=self.driver,
            equipment=self.truck_1,
            assigned_by=self.admin,
            status=AssignmentStatus.ACCEPTED,
        )

        self.assertIsNone(get_active_equipment_assignment(self.driver))
        form = AdminEmployeeEditForm(instance=self.driver)
        self.assertEqual(form.initial['assignment_shift_type'], '')
        self.assertIsNone(form.initial['assignment_equipment'])

    def test_multi_role_form_contains_and_marks_equipment_for_both_roles(self):
        EmployeeAccess.objects.create(
            employee=self.driver,
            role=self.excavator_role,
            access_code='310003',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

        form = AdminEmployeeEditForm(instance=self.driver)
        self.assertIn(self.truck_1, form.fields['assignment_equipment'].queryset)
        self.assertIn(self.excavator_1, form.fields['assignment_equipment'].queryset)
        rendered = str(form['assignment_equipment'])
        self.assertIn('data-work-role="driver"', rendered)
        self.assertIn('data-work-role="excavator_operator"', rendered)

    def test_employee_card_marks_busy_equipment_for_the_occupied_shift(self):
        self.assign(
            self.other_driver,
            self.driver_role,
            self.truck_1,
            WorkShiftType.SHIFT_1,
        )

        form = AdminEmployeeEditForm(instance=self.driver)
        rendered = str(form['assignment_equipment'])

        self.assertIn('data-busy-day="Водитель 2"', rendered)
        self.assertNotIn('data-busy-night="Водитель 2"', rendered)

    def test_employee_card_does_not_mark_own_active_assignment_as_busy(self):
        self.assign(
            self.driver,
            self.driver_role,
            self.truck_1,
            WorkShiftType.SHIFT_1,
        )

        form = AdminEmployeeEditForm(instance=self.driver)

        self.assertNotIn('data-busy-day=', str(form['assignment_equipment']))

    def test_employee_card_conflict_uses_full_width_assignment_warning(self):
        admin_role = Role.objects.create(code='admin', name='Администратор')
        EmployeeAccess.objects.create(
            employee=self.admin,
            role=admin_role,
            access_code='110001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.assign(
            self.other_driver,
            self.driver_role,
            self.truck_1,
            WorkShiftType.SHIFT_1,
        )
        client = self.authenticated_client(self.admin, admin_role)

        response = client.post(
            reverse('system_admin_employee_detail', args=[self.driver.id]),
            {
                'full_name': self.driver.full_name,
                'status': self.driver.status,
                'assignment_role': self.driver_role.id,
                'assignment_shift_type': WorkShiftType.SHIFT_1,
                'assignment_equipment': self.truck_1.id,
            },
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'employee-work-assignment-meta is-warning')
        self.assertContains(response, 'Назначение не сохранено')
        self.assertContains(response, 'Эта техника уже назначена другому сотруднику')

    def test_inactive_access_makes_assignment_unavailable(self):
        assignment = self.assign(
            self.driver,
            self.driver_role,
            self.truck_1,
            WorkShiftType.SHIFT_1,
        )
        access = self.driver.accesses.get(role=self.driver_role)
        access.status = EmployeeAccess.Status.DEACTIVATED
        access.is_active = False
        access.save(update_fields=['status', 'is_active'])

        self.assertEqual(work_assignment_state(self.driver, assignment), 'access_inactive')

    def test_database_rejects_two_open_shifts_for_same_equipment(self):
        EmployeeShift.objects.create(
            employee=self.driver,
            equipment=self.truck_1,
            shift_type='day',
            opened_at=timezone.now(),
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            EmployeeShift.objects.create(
                employee=self.other_driver,
                equipment=self.truck_1,
                shift_type='night',
                opened_at=timezone.now(),
            )

    def test_employee_history_includes_assignment_ended_by(self):
        assignment = self.assign(
            self.driver,
            self.driver_role,
            self.truck_1,
            WorkShiftType.SHIFT_1,
        )
        clear_active_equipment_assignment(employee=self.driver, assigned_by=self.admin)
        assignment.refresh_from_db()

        self.assertEqual(assignment.ended_by, self.admin)
        self.assertTrue(self.admin.has_production_history())

    def test_employee_card_form_saves_assignment_through_shared_service(self):
        form = AdminEmployeeEditForm(
            data={
                'full_name': self.driver.full_name,
                'status': self.driver.status,
                'assignment_role': self.driver_role.id,
                'assignment_shift_type': WorkShiftType.SHIFT_1,
                'assignment_equipment': self.truck_1.id,
            },
            instance=self.driver,
        )

        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        assignment = form.save_work_assignment(assigned_by=self.admin)

        self.assertEqual(assignment.employee, self.driver)
        self.assertEqual(assignment.role, self.driver_role)
        self.assertEqual(assignment.equipment, self.truck_1)
        self.assertEqual(assignment.shift_type, WorkShiftType.SHIFT_1)

    def test_set_replace_clear_preserves_history_and_day_night_values(self):
        assigned_at = timezone.now()
        first, created = set_active_equipment_assignment(
            employee=self.driver,
            role=self.driver_role,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
            assigned_by=self.admin,
            now=assigned_at,
        )
        self.assertTrue(created)
        self.assertEqual(first.shift_type, 'day')
        self.assertEqual(first.status, AssignmentStatus.ACCEPTED)

        same, created = set_active_equipment_assignment(
            employee=self.driver,
            role=self.driver_role,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
            assigned_by=self.admin,
            now=assigned_at + timedelta(minutes=1),
        )
        self.assertFalse(created)
        self.assertEqual(same.id, first.id)

        replaced_at = assigned_at + timedelta(hours=1)
        second, created = set_active_equipment_assignment(
            employee=self.driver,
            role=self.driver_role,
            equipment=self.truck_2,
            shift_type=WorkShiftType.SHIFT_2,
            assigned_by=self.admin,
            now=replaced_at,
        )
        self.assertTrue(created)
        first.refresh_from_db()
        self.assertEqual(first.status, AssignmentStatus.ACCEPTED)
        self.assertEqual(first.ended_at, replaced_at)
        self.assertEqual(first.ended_by, self.admin)
        self.assertEqual(second.shift_type, 'night')
        self.assertEqual(get_active_equipment_assignment(self.driver), second)

        cleared_at = replaced_at + timedelta(hours=1)
        self.assertEqual(
            clear_active_equipment_assignment(
                employee=self.driver,
                assigned_by=self.admin,
                now=cleared_at,
            ),
            1,
        )
        second.refresh_from_db()
        self.assertEqual(second.status, AssignmentStatus.ACCEPTED)
        self.assertEqual(second.ended_at, cleared_at)
        self.assertEqual(second.ended_by, self.admin)
        self.assertIsNone(get_active_equipment_assignment(self.driver))
        self.assertEqual(EquipmentAssignment.objects.filter(employee=self.driver).count(), 2)

    def test_same_equipment_and_shift_conflicts_but_other_shift_is_allowed(self):
        self.assign(self.driver, self.driver_role, self.truck_1, WorkShiftType.SHIFT_1)

        with self.assertRaises(ValidationError):
            set_active_equipment_assignment(
                employee=self.other_driver,
                role=self.driver_role,
                equipment=self.truck_1,
                shift_type=WorkShiftType.SHIFT_1,
                assigned_by=self.admin,
            )

        other_shift, created = set_active_equipment_assignment(
            employee=self.other_driver,
            role=self.driver_role,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_2,
            assigned_by=self.admin,
        )
        self.assertTrue(created)
        self.assertEqual(other_shift.shift_type, 'night')

    def test_open_shift_on_assigned_equipment_produces_conflict_state(self):
        assignment = self.assign(
            self.driver,
            self.driver_role,
            self.truck_1,
            WorkShiftType.SHIFT_1,
        )
        EmployeeShift.objects.create(
            employee=self.other_driver,
            equipment=self.truck_1,
            shift_type='day',
            opened_at=timezone.now(),
            opened_by=self.other_driver,
        )

        self.assertEqual(work_assignment_state(self.driver, assignment), 'assignment_conflict')

    def test_missing_assignment_for_requested_role_is_not_replaced_by_other_role(self):
        EmployeeAccess.objects.create(
            employee=self.driver,
            role=self.excavator_role,
            access_code='310002',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.assign(
            self.driver,
            self.excavator_role,
            self.excavator_1,
            WorkShiftType.SHIFT_1,
        )

        driver_assignment = get_active_equipment_assignment(self.driver, 'driver')
        self.assertIsNone(driver_assignment)
        self.assertEqual(work_assignment_state(self.driver, driver_assignment), 'no_active_assignment')


class WorkAssignmentShiftStartTests(WorkAssignmentFixtureMixin, TestCase):
    def test_driver_start_uses_server_assignment_and_open_shift_keeps_snapshot(self):
        assignment = self.assign(
            self.driver,
            self.driver_role,
            self.truck_1,
            WorkShiftType.SHIFT_1,
        )
        client = self.authenticated_client(self.driver, self.driver_role)

        response = client.post(
            reverse('driver_shift'),
            {
                'shift_type': 'night',
                'truck': self.truck_2.id,
                'start_fuel': '125.50',
                'start_mileage': '1000.00',
                'start_engine_hours': '50.25',
            },
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 302)
        shift = EmployeeShift.objects.get(employee=self.driver, closed_at__isnull=True)
        self.assertEqual(shift.equipment, assignment.equipment)
        self.assertEqual(shift.shift_type, 'day')

        replacement, created = set_active_equipment_assignment(
            employee=self.driver,
            role=self.driver_role,
            equipment=self.truck_2,
            shift_type=WorkShiftType.SHIFT_2,
            assigned_by=self.admin,
        )
        self.assertTrue(created)
        self.assertEqual(replacement.shift_type, 'night')
        shift.refresh_from_db()
        self.assertEqual(shift.equipment, self.truck_1)
        self.assertEqual(shift.shift_type, 'day')

    def test_driver_without_assignment_gets_explicit_state_and_cannot_start(self):
        client = self.authenticated_client(self.driver, self.driver_role)

        get_response = client.get(reverse('driver_shift'), HTTP_HOST='localhost')
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.context['work_assignment_state'], 'no_active_assignment')
        self.assertContains(get_response, 'Смена и самосвал еще не назначены')
        self.assertContains(get_response, 'Нет плана')
        self.assertContains(get_response, 'Начать смену')

        post_response = client.post(
            reverse('driver_shift'),
            {
                'shift_type': 'day',
                'truck': self.truck_1.id,
                'start_fuel': '10',
                'start_mileage': '20',
                'start_engine_hours': '30',
            },
            HTTP_HOST='localhost',
        )
        self.assertEqual(post_response.status_code, 200)
        self.assertEqual(post_response.context['work_assignment_state'], 'no_active_assignment')
        self.assertFalse(EmployeeShift.objects.filter(employee=self.driver).exists())

    def test_excavator_start_uses_server_assignment_for_night_shift(self):
        assignment = self.assign(
            self.operator,
            self.excavator_role,
            self.excavator_1,
            WorkShiftType.SHIFT_2,
        )
        client = self.authenticated_client(self.operator, self.excavator_role)

        response = client.post(
            reverse('excavator_shift_action'),
            data=json.dumps({
                'action': 'open',
                'client_action_id': 'test-excavator-open',
                'excavator_id': self.excavator_2.id,
                'shift_type': 'day',
                'fuel': '400',
                'mileage': '1500',
                'engine_hours': '80',
            }),
            content_type='application/json',
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['equipment_id'], assignment.equipment_id)
        shift = EmployeeShift.objects.get(pk=payload['shift_id'])
        self.assertEqual(shift.employee, self.operator)
        self.assertEqual(shift.equipment, self.excavator_1)
        self.assertEqual(shift.shift_type, 'night')

    def test_excavator_without_assignment_returns_explicit_409(self):
        client = self.authenticated_client(self.operator, self.excavator_role)

        response = client.post(
            reverse('excavator_shift_action'),
            data=json.dumps({
                'action': 'open',
                'client_action_id': 'test-no-assignment',
                'excavator_id': self.excavator_1.id,
                'shift_type': 'day',
                'fuel': '1',
                'mileage': '2',
                'engine_hours': '3',
            }),
            content_type='application/json',
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['assignment_state'], 'no_active_assignment')
        self.assertIn('не назначены', response.json()['error'])
        self.assertFalse(EmployeeShift.objects.filter(employee=self.operator).exists())
