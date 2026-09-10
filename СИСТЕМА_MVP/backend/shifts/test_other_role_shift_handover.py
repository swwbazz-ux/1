"""Одна открытая смена на сотрудника: старт смены в новой роли предлагает
завершить открытую смену другой роли — одинаково для водителя, экскаваторщика,
диспетчера, мастера и ОУП (сервисный слой водителя и экскаваторщика здесь,
диспетчер/мастер/ОУП — в тестах своих экранов)."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from assignments.models import AssignmentStatus, EquipmentAssignment
from references.models import Equipment, EquipmentModel, EquipmentType
from users.models import Employee, EmployeeAccess, Role

from .models import EmployeeShift
from .services import (
    ExcavatorShiftError,
    OtherRoleShiftOpen,
    find_other_role_open_shift,
    open_driver_shift,
    open_excavator_shift,
    other_role_shift_flag,
    other_role_shift_prompt,
)


class OtherRoleShiftHandoverTests(TestCase):
    def setUp(self):
        self.driver_role = Role.objects.create(code='driver', name='Водитель')
        self.excavator_role = Role.objects.create(code='excavator_operator', name='Машинист экскаватора')
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=EquipmentModel.objects.create(
                equipment_type=truck_type, name='БелАЗ', fuel_capacity_limit_l=Decimal('2000'),
            ),
            garage_number='54',
        )
        self.excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            model=EquipmentModel.objects.create(
                equipment_type=excavator_type, name='ЭКГ', fuel_capacity_limit_l=Decimal('7000'),
            ),
            garage_number='Э-1',
        )
        # Один человек с двумя полевыми ролями — как у пользователя при проверках.
        self.employee = Employee.objects.create(full_name='Многоролевой С.С.', status=Employee.Status.ACTIVE)
        for role, code in ((self.driver_role, '200001'), (self.excavator_role, '300001')):
            EmployeeAccess.objects.create(
                employee=self.employee, role=role, access_code=code,
                status=EmployeeAccess.Status.ACTIVATED, is_active=True,
            )
        self.driver_assignment = EquipmentAssignment.objects.create(
            employee=self.employee, role=self.driver_role, equipment=self.truck, shift_type='day',
            assigned_by=self.employee, status=AssignmentStatus.ACCEPTED, accepted_at=timezone.now(),
        )

    def open_other_role_shift(self, workplace_code='dispatcher'):
        return EmployeeShift.objects.create(
            employee=self.employee, shift_type='day', workplace_code=workplace_code,
            opened_at=timezone.now(), opened_by=self.employee,
        )

    def driver_readings(self):
        return {
            'start_fuel': Decimal('1000'),
            'start_mileage': Decimal('10000'),
            'start_engine_hours': Decimal('1000'),
        }

    def test_prompt_names_the_open_role_and_the_target_role(self):
        shift = self.open_other_role_shift('dispatcher')
        prompt = other_role_shift_prompt(shift, target_workplace_code='driver')
        opened = timezone.localtime(shift.opened_at).strftime('%H:%M')

        self.assertEqual(
            prompt['question'],
            f'У вас открыта смена «Горный диспетчер» с {opened}. Завершить её и начать смену Водителя?',
        )
        self.assertEqual(prompt['field'], 'close_other_role_shift')
        self.assertEqual(prompt['accept_label'], 'Завершить и начать')

    def test_flag_accepts_form_and_json_values(self):
        self.assertTrue(other_role_shift_flag({'close_other_role_shift': '1'}))
        self.assertTrue(other_role_shift_flag({'close_other_role_shift': True}))
        self.assertFalse(other_role_shift_flag({'close_other_role_shift': 'false'}))
        self.assertFalse(other_role_shift_flag({}))
        self.assertFalse(other_role_shift_flag(None))

    def test_legacy_shift_without_workplace_is_not_treated_as_other_role(self):
        self.open_other_role_shift('')
        self.assertIsNone(find_other_role_open_shift(self.employee, workplace_code='driver', for_update=False))

    def test_driver_start_asks_before_closing_dispatcher_shift(self):
        other = self.open_other_role_shift('dispatcher')

        with self.assertRaises(OtherRoleShiftOpen) as raised:
            open_driver_shift(
                employee=self.employee, work_assignment=self.driver_assignment,
                readings=self.driver_readings(), client_action_id='driver-ask',
            )

        self.assertEqual(raised.exception.prompt['shift_id'], other.pk)
        other.refresh_from_db()
        self.assertIsNone(other.closed_at)
        self.assertFalse(EmployeeShift.objects.filter(employee=self.employee, workplace_code='driver').exists())

    def test_driver_start_with_confirmation_closes_dispatcher_shift_as_service_close(self):
        other = self.open_other_role_shift('dispatcher')

        shift, created = open_driver_shift(
            employee=self.employee, work_assignment=self.driver_assignment,
            readings=self.driver_readings(), client_action_id='driver-confirmed',
            close_other_role_shift=True,
        )

        self.assertTrue(created)
        self.assertEqual(shift.workplace_code, 'driver')
        other.refresh_from_db()
        self.assertIsNotNone(other.closed_at)
        self.assertTrue(other.is_service_closed)
        self.assertEqual(other.closed_by, self.employee)
        self.assertEqual(EmployeeShift.objects.filter(employee=self.employee, closed_at__isnull=True).count(), 1)

    def test_excavator_start_returns_question_payload_then_hands_over(self):
        other = self.open_other_role_shift('mining_master')

        with self.assertRaises(ExcavatorShiftError) as raised:
            open_excavator_shift(
                employee=self.employee, equipment=self.excavator, shift_type='day',
                fuel_value='100', engine_hours_value='1200', client_action_id='exc-ask',
            )
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.code, 'other_role_shift_open')
        self.assertIn('Завершить её и начать смену Машиниста экскаватора?', raised.exception.extra['other_role_shift']['question'])
        other.refresh_from_db()
        self.assertIsNone(other.closed_at)

        payload = open_excavator_shift(
            employee=self.employee, equipment=self.excavator, shift_type='day',
            fuel_value='100', engine_hours_value='1200', client_action_id='exc-confirmed',
            close_other_role_shift=True,
        )

        self.assertTrue(payload.get('ok'))
        other.refresh_from_db()
        self.assertTrue(other.is_service_closed)
        self.assertTrue(
            EmployeeShift.objects.filter(
                employee=self.employee, workplace_code='excavator_operator', closed_at__isnull=True,
            ).exists()
        )

    def test_same_role_open_shift_is_still_a_plain_conflict(self):
        # Своя же смена водителя — это не «другая роль», подтверждение её не закрывает.
        EmployeeShift.objects.create(
            employee=self.employee, shift_type='day', workplace_code='driver', equipment=self.truck,
            opened_at=timezone.now(), opened_by=self.employee,
        )
        with self.assertRaises(ValidationError) as raised:
            open_driver_shift(
                employee=self.employee, work_assignment=self.driver_assignment,
                readings=self.driver_readings(), client_action_id='driver-dup',
                close_other_role_shift=True,
            )
        self.assertNotIsInstance(raised.exception, OtherRoleShiftOpen)
        self.assertEqual(EmployeeShift.objects.filter(employee=self.employee, closed_at__isnull=True).count(), 1)
