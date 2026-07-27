import json
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, EquipmentAssignment, WorkShiftType
from core.models import OperationalStateEvent
from references.models import (
    Dormitory,
    DormitoryBlock,
    DormitorySection,
    Equipment,
    EquipmentModel,
    EquipmentType,
)
from users.models import DriverPrimaryRegistration, Employee, EmployeeAccess, Role

from .models import EmployeeShift, ShiftClientAction, ShiftReadingCorrection
from .services import ExcavatorShiftError, open_driver_shift, open_excavator_shift


class ShiftStartConflictMessageTests(TestCase):
    def setUp(self):
        self.driver_role = Role.objects.create(code='driver', name='Водитель')
        self.operator_role = Role.objects.create(
            code='excavator_operator',
            name='Машинист экскаватора',
        )
        self.truck_type = EquipmentType.objects.create(name='Самосвал')
        self.excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck_model = EquipmentModel.objects.create(
            equipment_type=self.truck_type,
            name='БелАЗ P04',
            fuel_capacity_limit_l=2000,
        )
        self.excavator_model = EquipmentModel.objects.create(
            equipment_type=self.excavator_type,
            name='ЭКГ P04',
            fuel_capacity_limit_l=2000,
        )
        self.truck = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number='TRUCK-P04-006',
        )
        self.excavator = Equipment.objects.create(
            equipment_type=self.excavator_type,
            model=self.excavator_model,
            garage_number='EXC-P04-006',
        )
        self.busy_driver = self.create_employee('Занятый водитель Петров П.П.', self.driver_role)
        self.busy_operator = self.create_employee(
            'Занятый машинист Иванов И.И.',
            self.operator_role,
        )
        self.new_driver = self.create_employee('Новый водитель Сидоров С.С.', self.driver_role)
        self.new_operator = self.create_employee(
            'Новый машинист Кузнецов К.К.',
            self.operator_role,
        )
        dormitory = Dormitory.objects.create(number='P04')
        dormitory_block = DormitoryBlock.objects.create(
            dormitory=dormitory,
            name='P04',
        )
        dormitory_section = DormitorySection.objects.create(
            block=dormitory_block,
            name='P04',
        )
        DriverPrimaryRegistration.objects.create(
            employee=self.new_driver,
            dormitory_section=dormitory_section,
        )
        self.busy_driver_shift = EmployeeShift.objects.create(
            employee=self.busy_driver,
            equipment=self.truck,
            shift_type=WorkShiftType.SHIFT_1,
            workplace_code='driver',
            start_fuel=Decimal('1000'),
            start_mileage=Decimal('10000'),
            start_engine_hours=Decimal('500'),
            opened_at=timezone.now(),
            opened_by=self.busy_driver,
        )
        self.busy_operator_shift = EmployeeShift.objects.create(
            employee=self.busy_operator,
            equipment=self.excavator,
            shift_type=WorkShiftType.SHIFT_1,
            workplace_code='excavator_operator',
            start_fuel=Decimal('1000'),
            start_engine_hours=Decimal('500'),
            opened_at=timezone.now(),
            opened_by=self.busy_operator,
        )
        self.driver_assignment = self.create_assignment(
            self.new_driver,
            self.driver_role,
            self.truck,
        )
        self.operator_assignment = self.create_assignment(
            self.new_operator,
            self.operator_role,
            self.excavator,
        )

    @staticmethod
    def create_employee(name, role):
        employee = Employee.objects.create(
            full_name=name,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code=f'P04-{employee.pk:04d}',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        return employee

    @staticmethod
    def create_assignment(employee, role, equipment):
        return EquipmentAssignment.objects.create(
            employee=employee,
            role=role,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )

    def activate(self, employee, role):
        access = EmployeeAccess.objects.get(employee=employee, role=role)
        access.last_login_at = timezone.now()
        access.save(update_fields=['last_login_at'])
        session = self.client.session
        session['employee_access_id'] = access.pk
        session['active_role_access_id'] = access.pk
        session['active_role_login_at'] = access.last_login_at.isoformat()
        session['active_role_code'] = role.code
        session.save()

    @staticmethod
    def driver_readings():
        return {
            'start_fuel': Decimal('900'),
            'start_mileage': Decimal('10001'),
            'start_engine_hours': Decimal('501'),
        }

    def test_driver_conflict_names_employee_and_truck_in_service_and_screen(self):
        expected = (
            f'Смена на технике {self.truck} уже открыта сотрудником '
            f'{self.busy_driver.full_name}'
        )
        with self.assertRaisesMessage(ValidationError, expected):
            open_driver_shift(
                employee=self.new_driver,
                work_assignment=self.driver_assignment,
                readings=self.driver_readings(),
                client_action_id='p04-driver-service-conflict',
            )

        self.activate(self.new_driver, self.driver_role)
        response = self.client.post(
            reverse('driver_work'),
            {
                **{key: str(value) for key, value in self.driver_readings().items()},
                'client_action_id': 'p04-driver-screen-conflict',
            },
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, expected)
        self.assertEqual(
            EmployeeShift.objects.filter(employee=self.new_driver, closed_at__isnull=True).count(),
            0,
        )

    def test_excavator_conflict_names_employee_and_equipment_and_returns_409(self):
        expected = (
            f'Смена на технике {self.excavator} уже открыта сотрудником '
            f'{self.busy_operator.full_name}'
        )
        with self.assertRaises(ExcavatorShiftError) as captured:
            open_excavator_shift(
                employee=self.new_operator,
                equipment=self.excavator,
                shift_type=WorkShiftType.SHIFT_1,
                fuel_value='900',
                engine_hours_value='501',
                client_action_id='p04-exc-service-conflict',
            )
        self.assertEqual(captured.exception.status, 409)
        self.assertEqual(captured.exception.message, expected)

        self.activate(self.new_operator, self.operator_role)
        response = self.client.post(
            reverse('excavator_shift_action'),
            data=json.dumps({
                'action': 'open',
                'fuel': '900',
                'engine_hours': '501',
                'client_action_id': 'p04-exc-screen-conflict',
            }),
            content_type='application/json',
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], expected)
        self.assertEqual(
            EmployeeShift.objects.filter(employee=self.new_operator, closed_at__isnull=True).count(),
            0,
        )

    def test_free_truck_and_excavator_starts_do_not_regress(self):
        now = timezone.now()
        for assignment in (self.driver_assignment, self.operator_assignment):
            assignment.status = AssignmentStatus.CANCELLED
            assignment.ended_at = now
            assignment.save(update_fields=['status', 'ended_at'])
        free_truck = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number='TRUCK-P04-FREE',
        )
        free_excavator = Equipment.objects.create(
            equipment_type=self.excavator_type,
            model=self.excavator_model,
            garage_number='EXC-P04-FREE',
        )
        driver_assignment = self.create_assignment(
            self.new_driver,
            self.driver_role,
            free_truck,
        )
        operator_assignment = self.create_assignment(
            self.new_operator,
            self.operator_role,
            free_excavator,
        )

        driver_shift, driver_created = open_driver_shift(
            employee=self.new_driver,
            work_assignment=driver_assignment,
            readings=self.driver_readings(),
            client_action_id='p04-driver-free-start',
        )
        operator_response = open_excavator_shift(
            employee=self.new_operator,
            equipment=operator_assignment.equipment,
            shift_type=operator_assignment.shift_type,
            fuel_value='900',
            engine_hours_value='501',
            client_action_id='p04-exc-free-start',
        )

        self.assertTrue(driver_created)
        self.assertEqual(driver_shift.equipment, free_truck)
        self.assertTrue(operator_response['ok'])
        self.assertEqual(operator_response['equipment_id'], free_excavator.pk)


class ExcavatorShiftLateIntegrityErrorTests(TestCase):
    def setUp(self):
        self.operator_role = Role.objects.create(
            code='excavator_operator',
            name='Машинист экскаватора',
        )
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        excavator_model = EquipmentModel.objects.create(
            equipment_type=excavator_type,
            name='ЭКГ P04-009',
            fuel_capacity_limit_l=2000,
        )
        self.excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            model=excavator_model,
            garage_number='EXC-P04-009',
        )
        self.operator = Employee.objects.create(
            full_name='Машинист P04-009',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.operator_access = EmployeeAccess.objects.create(
            employee=self.operator,
            role=self.operator_role,
            access_code='P04-009',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            last_login_at=timezone.now(),
        )
        self.assignment = EquipmentAssignment.objects.create(
            employee=self.operator,
            role=self.operator_role,
            equipment=self.excavator,
            shift_type=WorkShiftType.SHIFT_1,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        session = self.client.session
        session['employee_access_id'] = self.operator_access.pk
        session['active_role_access_id'] = self.operator_access.pk
        session['active_role_login_at'] = self.operator_access.last_login_at.isoformat()
        session['active_role_code'] = self.operator_role.code
        session.save()

    def post_open(self, client_action_id):
        return self.client.post(
            reverse('excavator_shift_action'),
            data=json.dumps({
                'action': 'open',
                'fuel': '900',
                'engine_hours': '501',
                'client_action_id': client_action_id,
            }),
            content_type='application/json',
            HTTP_HOST='localhost',
        )

    def test_unrelated_late_integrity_error_is_not_masked_as_equipment_conflict(self):
        database_error = IntegrityError('simulated unrelated database failure')
        with patch.object(
            ShiftClientAction.objects,
            'create',
            side_effect=database_error,
        ):
            with self.assertRaises(IntegrityError) as captured:
                self.post_open('p04-009-unrelated-integrity')

        self.assertIs(captured.exception, database_error)
        self.assertFalse(
            EmployeeShift.objects.filter(employee=self.operator, closed_at__isnull=True).exists()
        )
        self.assertFalse(
            ShiftClientAction.objects.filter(
                client_action_id='p04-009-unrelated-integrity',
            ).exists()
        )
        self.assertFalse(ShiftReadingCorrection.objects.exists())
        self.assertFalse(
            OperationalStateEvent.objects.filter(
                event_type='excavator_shift_opened',
            ).exists()
        )

    def test_late_integrity_error_becomes_409_when_open_equipment_shift_is_confirmed(self):
        winner = Employee.objects.create(
            full_name='Победивший машинист P04-009',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        winner_shift = EmployeeShift.objects.create(
            employee=winner,
            equipment=self.excavator,
            shift_type=WorkShiftType.SHIFT_1,
            workplace_code='excavator_operator',
            start_fuel=Decimal('1000'),
            start_engine_hours=Decimal('500'),
            opened_at=timezone.now(),
            opened_by=winner,
        )
        database_error = IntegrityError('simulated concurrent unique conflict')

        with patch(
            'shifts.services._open_excavator_shift_atomic',
            side_effect=database_error,
        ):
            with self.assertRaises(ExcavatorShiftError) as captured:
                open_excavator_shift(
                    employee=self.operator,
                    equipment=self.excavator,
                    shift_type=WorkShiftType.SHIFT_1,
                    fuel_value='900',
                    engine_hours_value='501',
                    client_action_id='p04-009-confirmed-conflict',
                )

        self.assertEqual(captured.exception.status, 409)
        self.assertEqual(captured.exception.code, 'equipment_shift_already_open')
        self.assertEqual(
            captured.exception.message,
            (
                f'Смена на технике {self.excavator} уже открыта сотрудником '
                f'{winner.full_name}.'
            ),
        )
        self.assertEqual(
            EmployeeShift.objects.filter(
                equipment=self.excavator,
                closed_at__isnull=True,
            ).get(),
            winner_shift,
        )

    def test_nonconcurrent_excavator_start_still_succeeds_once(self):
        response = self.post_open('p04-009-normal-start')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertEqual(
            EmployeeShift.objects.filter(employee=self.operator, closed_at__isnull=True).count(),
            1,
        )
        self.assertEqual(
            ShiftClientAction.objects.filter(
                action_type='excavator_shift_opened',
                client_action_id='p04-009-normal-start',
            ).count(),
            1,
        )
