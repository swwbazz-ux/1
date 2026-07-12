from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from core.models import OperationalStateEvent
from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import DumpPoint, Equipment, EquipmentModel, EquipmentType, RockType
from trips.models import Trip, TripStatus
from users.models import Employee

from .models import DriverShiftAction, EmployeeShift, EquipmentPlanGroup, EquipmentShiftPlan, PlanAssignmentStatus, PlanCalculationMode, ShiftPlan, ShiftReadingCorrection
from .equipment_plan_groups import (
    reconcile_default_equipment_plan_groups,
    validate_equipment_plan_group_membership,
)
from .services import (
    assign_shift_plan_snapshot,
    calculate_equipment_shift_progress,
    calculate_open_shift_progress,
    close_driver_shift,
    open_driver_shift,
    shift_plan_totals,
    validate_driver_close_readings,
    validate_driver_fuel_reading,
)


class DriverShiftLifecycleTests(TestCase):
    def setUp(self):
        self.truck_type = EquipmentType.objects.create(name='Самосвал')
        self.belaz = EquipmentModel.objects.create(
            equipment_type=self.truck_type, name='БелАЗ 75131', fuel_capacity_limit_l=Decimal('2000'),
        )
        self.nhl = EquipmentModel.objects.create(
            equipment_type=self.truck_type, name='NHL', fuel_capacity_limit_l=Decimal('3000'),
        )
        self.truck = Equipment.objects.create(equipment_type=self.truck_type, model=self.belaz, garage_number='54')
        self.driver = Employee.objects.create(full_name='Петров П.П.')
        self.assignment = SimpleNamespace(equipment_id=self.truck.pk, shift_type='day')

    def readings(self, fuel='1000', mileage='10000', hours='1000'):
        return {
            'start_fuel': Decimal(fuel),
            'start_mileage': Decimal(mileage),
            'start_engine_hours': Decimal(hours),
        }

    def open_shift(self, action='open-1', **overrides):
        readings = self.readings(**overrides)
        return open_driver_shift(
            employee=self.driver, work_assignment=self.assignment, readings=readings, client_action_id=action,
        )[0]

    def close_readings(self, fuel='900', mileage='10100', hours='1010'):
        return {
            'end_fuel': Decimal(fuel),
            'end_mileage': Decimal(mileage),
            'end_engine_hours': Decimal(hours),
        }

    def test_first_shift_saves_independent_start_snapshot(self):
        shift = self.open_shift()
        self.assertEqual(shift.start_mileage, Decimal('10000'))
        self.assertFalse(ShiftReadingCorrection.objects.exists())

    def test_open_requires_non_negative_fuel(self):
        with self.assertRaises(ValidationError):
            self.open_shift(fuel='-1')

    def test_model_without_fuel_limit_is_blocked(self):
        model = EquipmentModel.objects.create(equipment_type=self.truck_type, name='Без лимита')
        self.truck.model = model
        self.truck.save(update_fields=['model'])
        with self.assertRaisesMessage(ValidationError, 'не настроен'):
            self.open_shift()

    def test_belaz_fuel_limit_allows_2000(self):
        validate_driver_fuel_reading(self.truck, Decimal('2000'))

    def test_belaz_fuel_limit_blocks_above_2000(self):
        with self.assertRaises(ValidationError):
            validate_driver_fuel_reading(self.truck, Decimal('2000.01'))

    def test_nhl_fuel_limit_allows_3000(self):
        self.truck.model = self.nhl
        self.truck.save(update_fields=['model'])
        validate_driver_fuel_reading(self.truck, Decimal('3000'))

    def test_nhl_fuel_limit_blocks_above_3000(self):
        self.truck.model = self.nhl
        self.truck.save(update_fields=['model'])
        with self.assertRaises(ValidationError):
            validate_driver_fuel_reading(self.truck, Decimal('3000.01'))

    def test_previous_end_readings_can_be_opened_unchanged(self):
        previous = self.open_shift()
        close_driver_shift(
            shift=previous, employee=self.driver, readings=self.close_readings(), client_action_id='close-1',
        )
        shift = self.open_shift(action='open-2', fuel='900', mileage='10100', hours='1010')
        self.assertFalse(shift.reading_corrections.exists())

    def test_changed_inherited_reading_creates_audit(self):
        previous = self.open_shift()
        close_driver_shift(
            shift=previous, employee=self.driver, readings=self.close_readings(), client_action_id='close-1',
        )
        shift = self.open_shift(action='open-2', fuel='850', mileage='10100', hours='1010')
        correction = shift.reading_corrections.get()
        self.assertEqual(correction.inherited_value, Decimal('900'))
        self.assertEqual(correction.corrected_value, Decimal('850'))

    def test_second_open_shift_for_driver_is_blocked(self):
        self.open_shift()
        other = Equipment.objects.create(equipment_type=self.truck_type, model=self.belaz, garage_number='55')
        with self.assertRaises(ValidationError):
            open_driver_shift(
                employee=self.driver,
                work_assignment=SimpleNamespace(equipment_id=other.pk, shift_type='day'),
                readings=self.readings(),
                client_action_id='open-2',
            )

    def test_second_open_shift_for_truck_is_blocked(self):
        self.open_shift()
        other_driver = Employee.objects.create(full_name='Иванов И.И.')
        with self.assertRaisesMessage(ValidationError, 'уже открыта другим водителем'):
            open_driver_shift(
                employee=other_driver, work_assignment=self.assignment, readings=self.readings(), client_action_id='other-open',
            )

    def test_database_constraint_blocks_concurrent_duplicate_truck_shift(self):
        self.open_shift()
        other_driver = Employee.objects.create(full_name='Сидоров С.С.')
        with self.assertRaises(IntegrityError), transaction.atomic():
            EmployeeShift.objects.create(
                employee=other_driver, equipment=self.truck, shift_type='night', opened_at=timezone.now(),
            )

    def test_duplicate_open_client_action_is_idempotent(self):
        first, created = open_driver_shift(
            employee=self.driver, work_assignment=self.assignment, readings=self.readings(), client_action_id='same-open',
        )
        second, created_again = open_driver_shift(
            employee=self.driver, work_assignment=self.assignment, readings=self.readings(), client_action_id='same-open',
        )
        self.assertEqual(first.pk, second.pk)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(DriverShiftAction.objects.filter(action_type='driver_shift_opened').count(), 1)

    def test_end_fuel_may_exceed_start_fuel(self):
        shift = self.open_shift(fuel='500')
        validate_driver_close_readings(shift, **self.close_readings(fuel='700'))

    def test_mileage_may_increase_by_250(self):
        shift = self.open_shift()
        validate_driver_close_readings(shift, **self.close_readings(mileage='10250'))

    def test_mileage_above_250_is_blocked(self):
        shift = self.open_shift()
        with self.assertRaises(ValidationError):
            validate_driver_close_readings(shift, **self.close_readings(mileage='10250.01'))

    def test_mileage_decrease_is_blocked(self):
        shift = self.open_shift()
        with self.assertRaises(ValidationError):
            validate_driver_close_readings(shift, **self.close_readings(mileage='9999'))

    def test_engine_hours_may_increase_by_12(self):
        shift = self.open_shift()
        validate_driver_close_readings(shift, **self.close_readings(hours='1012'))

    def test_engine_hours_above_12_is_blocked(self):
        shift = self.open_shift()
        with self.assertRaisesMessage(ValidationError, 'не могут увеличиться более чем на 12'):
            validate_driver_close_readings(shift, **self.close_readings(hours='1012.01'))

    def test_engine_hours_decrease_is_blocked(self):
        shift = self.open_shift()
        with self.assertRaises(ValidationError):
            validate_driver_close_readings(shift, **self.close_readings(hours='999'))

    def test_close_with_active_trip_is_blocked(self):
        shift = self.open_shift()
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='ЭКС-1')
        rock = RockType.objects.create(name='Руда', density='2.50')
        point = DumpPoint.objects.create(name='ККД')
        Trip.objects.create(truck=self.truck, excavator=excavator, driver=self.driver, rock_type=rock, dump_point=point)
        with self.assertRaisesMessage(ValidationError, 'активный рейс'):
            close_driver_shift(
                shift=shift, employee=self.driver, readings=self.close_readings(), client_action_id='blocked-trip',
            )

    def test_close_with_active_downtime_is_blocked(self):
        shift = self.open_shift()
        reason, _ = DowntimeReason.objects.get_or_create(name='Ремонт', defaults={'equipment_type': self.truck_type})
        DowntimeEvent.objects.create(equipment=self.truck, employee=self.driver, reason=reason, started_at=timezone.now())
        with self.assertRaisesMessage(ValidationError, 'активный простой'):
            close_driver_shift(
                shift=shift, employee=self.driver, readings=self.close_readings(), client_action_id='blocked-downtime',
            )

    def test_duplicate_close_client_action_is_idempotent(self):
        shift = self.open_shift()
        first, created = close_driver_shift(
            shift=shift, employee=self.driver, readings=self.close_readings(), client_action_id='same-close',
        )
        second, created_again = close_driver_shift(
            shift=shift, employee=self.driver, readings=self.close_readings(), client_action_id='same-close',
        )
        self.assertEqual(first.pk, second.pk)
        self.assertTrue(created)
        self.assertFalse(created_again)

    def test_open_and_close_emit_realtime_events(self):
        shift = self.open_shift()
        close_driver_shift(
            shift=shift, employee=self.driver, readings=self.close_readings(), client_action_id='close-event',
        )
        self.assertTrue(OperationalStateEvent.objects.filter(event_type='driver_shift_opened').exists())
        self.assertTrue(OperationalStateEvent.objects.filter(event_type='driver_shift_closed').exists())


class ShiftPlanServiceTests(TestCase):
    def create_shift_with_snapshot(self, equipment, *, shift_type='day', employee_name='Сотрудник', opened_at=None):
        employee = Employee.objects.create(full_name=employee_name)
        shift = EmployeeShift.objects.create(
            employee=employee,
            shift_type=shift_type,
            equipment=equipment,
            opened_at=opened_at or timezone.now(),
            opened_by=employee,
        )
        assign_shift_plan_snapshot(shift)
        shift.refresh_from_db()
        return shift

    def create_plan_group(self, *, name, code, mode, value, equipment):
        group = EquipmentPlanGroup.objects.create(
            name=name,
            code=code,
            calculation_mode=mode,
            plan_value=value,
            is_active=True,
        )
        group.equipment.add(equipment)
        return group

    def test_reconcile_default_groups_keeps_each_equipment_in_expected_group_only(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        belaz_model = EquipmentModel.objects.create(equipment_type=truck_type, name='БелАЗ 7513D')
        nhl_model = EquipmentModel.objects.create(equipment_type=truck_type, name='NHL NTE 200')
        belaz = Equipment.objects.create(equipment_type=truck_type, model=belaz_model, garage_number='25')
        nhl = Equipment.objects.create(equipment_type=truck_type, model=nhl_model, garage_number='54')
        excavator_1 = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        excavator_8 = Equipment.objects.create(equipment_type=excavator_type, garage_number='8')
        excavator_2 = Equipment.objects.create(equipment_type=excavator_type, garage_number='2')
        excavator_3 = Equipment.objects.create(equipment_type=excavator_type, garage_number='3')
        excavators_3000 = EquipmentPlanGroup.objects.get(code='excavators_3000')
        excavators_3000.equipment.add(belaz, nhl, excavator_1, excavator_2)
        belaz_group = EquipmentPlanGroup.objects.get(code='belaz_trucks')
        belaz_group.equipment.add(excavator_8)

        report = reconcile_default_equipment_plan_groups()

        self.assertGreaterEqual(report['removed_total'], 4)
        self.assertEqual(
            set(EquipmentPlanGroup.objects.get(code='belaz_trucks').equipment.values_list('id', flat=True)),
            {belaz.id},
        )
        self.assertEqual(
            set(EquipmentPlanGroup.objects.get(code='nhl_trucks').equipment.values_list('id', flat=True)),
            {nhl.id},
        )
        self.assertEqual(
            set(EquipmentPlanGroup.objects.get(code='excavators_4000').equipment.values_list('id', flat=True)),
            {excavator_1.id, excavator_8.id},
        )
        self.assertEqual(
            set(EquipmentPlanGroup.objects.get(code='excavators_3000').equipment.values_list('id', flat=True)),
            {excavator_2.id, excavator_3.id},
        )
        default_ids = []
        for code in ['belaz_trucks', 'nhl_trucks', 'excavators_4000', 'excavators_3000']:
            default_ids.extend(EquipmentPlanGroup.objects.get(code=code).equipment.values_list('id', flat=True))
        self.assertEqual(len(default_ids), len(set(default_ids)))

    def test_default_group_validation_rejects_incompatible_equipment(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        belaz_model = EquipmentModel.objects.create(equipment_type=truck_type, name='БелАЗ 7513D')
        truck = Equipment.objects.create(equipment_type=truck_type, model=belaz_model, garage_number='25')
        group = EquipmentPlanGroup(
            name='Экскаваторы 3000',
            code='excavators_3000',
            calculation_mode=PlanCalculationMode.VOLUME,
            is_active=True,
        )

        with self.assertRaises(ValidationError):
            validate_equipment_plan_group_membership(
                group,
                [truck],
                group_code='excavators_3000',
                is_active=True,
            )

    def test_equipment_cannot_be_in_two_active_plan_groups(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='25')
        first_group = EquipmentPlanGroup.objects.create(
            name='Активная группа 1',
            code='active-group-1',
            calculation_mode=PlanCalculationMode.TRIPS,
            plan_value='10.00',
            is_active=True,
        )
        first_group.equipment.add(truck)
        second_group = EquipmentPlanGroup(
            name='Активная группа 2',
            code='active-group-2',
            calculation_mode=PlanCalculationMode.TRIPS,
            plan_value='11.00',
            is_active=True,
        )

        with self.assertRaises(ValidationError):
            validate_equipment_plan_group_membership(
                second_group,
                [truck],
                group_code='active-group-2',
                is_active=True,
            )

    def test_reconcile_does_not_recalculate_closed_shift_snapshot(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        belaz_model = EquipmentModel.objects.create(equipment_type=truck_type, name='БелАЗ 7513D')
        truck = Equipment.objects.create(equipment_type=truck_type, model=belaz_model, garage_number='25')
        group = EquipmentPlanGroup.objects.get(code='belaz_trucks')
        group.plan_value = '12.00'
        group.calculation_mode = PlanCalculationMode.TRIPS
        group.is_active = True
        group.save(update_fields=['plan_value', 'calculation_mode', 'is_active'])
        group.equipment.set([truck])
        shift = self.create_shift_with_snapshot(truck)
        shift.closed_at = timezone.now()
        shift.save(update_fields=['closed_at'])

        reconcile_default_equipment_plan_groups()
        shift.refresh_from_db()

        self.assertEqual(shift.plan_status, PlanAssignmentStatus.ASSIGNED)
        self.assertEqual(shift.plan_group_name, 'Самосвалы БелАЗ')
        self.assertEqual(shift.plan_calculation_mode, PlanCalculationMode.TRIPS)
        self.assertEqual(shift.plan_value, 12)

    def test_group_composition_change_applies_only_to_new_shifts(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='25')
        old_group = self.create_plan_group(
            name='Старая активная группа',
            code='old-active-group',
            mode=PlanCalculationMode.TRIPS,
            value='10.00',
            equipment=truck,
        )
        old_shift = self.create_shift_with_snapshot(truck, employee_name='Водитель старая смена')
        new_group = EquipmentPlanGroup.objects.create(
            name='Новая активная группа',
            code='new-active-group',
            calculation_mode=PlanCalculationMode.TRIPS,
            plan_value='15.00',
            is_active=True,
        )
        old_group.equipment.remove(truck)
        new_group.equipment.add(truck)
        old_shift.closed_at = timezone.now()
        old_shift.save(update_fields=['closed_at'])

        new_shift = self.create_shift_with_snapshot(truck, employee_name='Водитель новая смена')
        old_shift.refresh_from_db()

        self.assertEqual(old_shift.plan_group_name, 'Старая активная группа')
        self.assertEqual(old_shift.plan_value, 10)
        self.assertEqual(new_shift.plan_group_name, 'Новая активная группа')
        self.assertEqual(new_shift.plan_value, 15)

    def test_belaz_gets_trip_plan_for_day_and_night_without_daily_shift_plan(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        belaz_model = EquipmentModel.objects.create(equipment_type=truck_type, name='БелАЗ 7513D')
        truck = Equipment.objects.create(equipment_type=truck_type, model=belaz_model, garage_number='25')
        self.create_plan_group(
            name='Самосвалы БелАЗ тест',
            code='belaz-trucks-test',
            mode=PlanCalculationMode.TRIPS,
            value='12.00',
            equipment=truck,
        )

        day_shift = self.create_shift_with_snapshot(truck, shift_type='day', employee_name='Водитель день')
        day_shift.closed_at = timezone.now()
        day_shift.save(update_fields=['closed_at'])
        night_shift = self.create_shift_with_snapshot(
            truck,
            shift_type='night',
            employee_name='Водитель ночь',
            opened_at=timezone.now() + timedelta(days=1),
        )

        self.assertEqual(day_shift.plan_status, PlanAssignmentStatus.ASSIGNED)
        self.assertEqual(day_shift.plan_calculation_mode, PlanCalculationMode.TRIPS)
        self.assertEqual(day_shift.plan_value, 12)
        self.assertEqual(night_shift.plan_status, PlanAssignmentStatus.ASSIGNED)
        self.assertEqual(night_shift.plan_calculation_mode, PlanCalculationMode.TRIPS)
        self.assertEqual(night_shift.plan_value, 12)
        self.assertFalse(ShiftPlan.objects.exists())

    def test_nhl_gets_trip_plan_without_daily_shift_plan(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        nhl_model = EquipmentModel.objects.create(equipment_type=truck_type, name='NHL NTE 200')
        truck = Equipment.objects.create(equipment_type=truck_type, model=nhl_model, garage_number='31')
        self.create_plan_group(
            name='Самосвалы NHL тест',
            code='nhl-trucks-test',
            mode=PlanCalculationMode.TRIPS,
            value='15.00',
            equipment=truck,
        )

        shift = self.create_shift_with_snapshot(truck)

        self.assertEqual(shift.plan_status, PlanAssignmentStatus.ASSIGNED)
        self.assertEqual(shift.plan_calculation_mode, PlanCalculationMode.TRIPS)
        self.assertEqual(shift.plan_value, 15)
        self.assertFalse(ShiftPlan.objects.exists())

    def test_excavator_1_gets_4000_volume_plan(self):
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        self.create_plan_group(
            name='Экскаваторы 4000 тест 1',
            code='excavators-4000-test-1',
            mode=PlanCalculationMode.VOLUME,
            value='4200.00',
            equipment=excavator,
        )

        shift = self.create_shift_with_snapshot(excavator, employee_name='Машинист 1')

        self.assertEqual(shift.plan_group_name, 'Экскаваторы 4000 тест 1')
        self.assertEqual(shift.plan_calculation_mode, PlanCalculationMode.VOLUME)
        self.assertEqual(shift.plan_value, 4200)

    def test_excavator_8_gets_4000_volume_plan(self):
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='8')
        self.create_plan_group(
            name='Экскаваторы 4000 тест 8',
            code='excavators-4000-test-8',
            mode=PlanCalculationMode.VOLUME,
            value='4200.00',
            equipment=excavator,
        )

        shift = self.create_shift_with_snapshot(excavator, employee_name='Машинист 8')

        self.assertEqual(shift.plan_group_name, 'Экскаваторы 4000 тест 8')
        self.assertEqual(shift.plan_calculation_mode, PlanCalculationMode.VOLUME)
        self.assertEqual(shift.plan_value, 4200)

    def test_other_excavator_gets_3000_volume_plan(self):
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='2')
        self.create_plan_group(
            name='Экскаваторы 3000 тест',
            code='excavators-3000-test',
            mode=PlanCalculationMode.VOLUME,
            value='3000.00',
            equipment=excavator,
        )

        shift = self.create_shift_with_snapshot(excavator, employee_name='Машинист 2')

        self.assertEqual(shift.plan_group_name, 'Экскаваторы 3000 тест')
        self.assertEqual(shift.plan_calculation_mode, PlanCalculationMode.VOLUME)
        self.assertEqual(shift.plan_value, 3000)

    def test_group_plan_change_does_not_rewrite_existing_shift_snapshot(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='25')
        group = self.create_plan_group(
            name='Самосвалы БелАЗ snapshot',
            code='belaz-snapshot-test',
            mode=PlanCalculationMode.TRIPS,
            value='10.00',
            equipment=truck,
        )
        driver = Employee.objects.create(full_name='Водитель')
        operator = Employee.objects.create(full_name='Машинист')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        dump_point = DumpPoint.objects.create(name='ККД')
        rock_type = RockType.objects.create(name='Руда')
        shift = self.create_shift_with_snapshot(truck, employee_name='Водитель snapshot')
        loading_shift = EmployeeShift.objects.create(
            employee=operator,
            shift_type='day',
            equipment=excavator,
            opened_at=shift.opened_at,
            opened_by=operator,
        )
        for index in range(5):
            Trip.objects.create(
                excavator=excavator,
                truck=truck,
                excavator_operator=operator,
                driver=driver,
                loading_shift=loading_shift,
                unloading_shift=shift,
                rock_type=rock_type,
                dump_point=dump_point,
                volume_m3='49.40',
                status=TripStatus.COMPLETED,
                completed_at=shift.opened_at + timedelta(minutes=index + 1),
            )
        group.plan_value = '20.00'
        group.is_active = False
        group.save(update_fields=['plan_value', 'is_active'])

        progress = calculate_open_shift_progress(shift)
        shift.refresh_from_db()

        self.assertEqual(shift.plan_status, PlanAssignmentStatus.ASSIGNED)
        self.assertEqual(shift.plan_value, 10)
        self.assertEqual(progress['progress_percent'], 50)

    def test_no_plan_group_returns_status_without_fake_zero_percent(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='25')

        shift = self.create_shift_with_snapshot(truck)
        progress = calculate_open_shift_progress(shift)

        self.assertEqual(shift.plan_status, PlanAssignmentStatus.NO_PLAN_GROUP)
        self.assertEqual(progress['plan_status'], PlanAssignmentStatus.NO_PLAN_GROUP)
        self.assertIsNone(progress['progress_percent'])

    def test_no_active_plan_returns_status_without_fake_zero_percent(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='25')
        group = EquipmentPlanGroup.objects.create(
            name='Самосвалы БелАЗ inactive',
            code='belaz-inactive-test',
            calculation_mode=PlanCalculationMode.TRIPS,
            plan_value='10.00',
            is_active=False,
        )
        group.equipment.add(truck)

        shift = self.create_shift_with_snapshot(truck)
        progress = calculate_open_shift_progress(shift)

        self.assertEqual(shift.plan_status, PlanAssignmentStatus.NO_ACTIVE_PLAN)
        self.assertEqual(progress['plan_status'], PlanAssignmentStatus.NO_ACTIVE_PLAN)
        self.assertIsNone(progress['progress_percent'])

    def test_future_active_from_returns_no_active_plan(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='25')
        group = EquipmentPlanGroup.objects.create(
            name='Самосвалы БелАЗ future',
            code='belaz-future-test',
            calculation_mode=PlanCalculationMode.TRIPS,
            plan_value='10.00',
            is_active=True,
            active_from=timezone.localdate() + timedelta(days=1),
        )
        group.equipment.add(truck)

        shift = self.create_shift_with_snapshot(truck)
        progress = calculate_open_shift_progress(shift)

        self.assertEqual(shift.plan_status, PlanAssignmentStatus.NO_ACTIVE_PLAN)
        self.assertEqual(progress['plan_status'], PlanAssignmentStatus.NO_ACTIVE_PLAN)
        self.assertIsNone(progress['progress_percent'])

    def test_shift_plan_totals_use_equipment_plans_when_present(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='25')
        plan = ShiftPlan.objects.create(
            date=timezone.localdate(),
            shift_type='day',
            name='План дневной смены',
            plan_volume_m3=1000,
            is_active=True,
        )
        EquipmentShiftPlan.objects.create(
            shift_plan=plan,
            equipment=truck,
            plan_volume_m3=250,
            plan_tonnage=600,
            plan_trips=10,
            calculation_mode=PlanCalculationMode.TRIPS,
            is_active=True,
        )

        totals = shift_plan_totals(timezone.localdate())

        self.assertEqual(totals['volume_m3'], 250)
        self.assertEqual(totals['tonnage'], 600)
        self.assertEqual(totals['trips'], 10)

    def test_equipment_shift_progress_counts_completed_truck_trips(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='25')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='4')
        driver = Employee.objects.create(full_name='Водитель')
        operator = Employee.objects.create(full_name='Машинист')
        rock_type = RockType.objects.create(name='Руда', density='2.4000')
        dump_point = DumpPoint.objects.create(name='ККД')
        opened_at = timezone.now()
        shift = EmployeeShift.objects.create(
            employee=driver,
            shift_type='day',
            equipment=truck,
            opened_at=opened_at,
            opened_by=driver,
        )
        loading_shift = EmployeeShift.objects.create(
            employee=operator,
            shift_type='day',
            equipment=excavator,
            opened_at=opened_at,
            opened_by=operator,
        )
        plan = ShiftPlan.objects.create(
            date=timezone.localtime(opened_at).date(),
            shift_type='day',
            name='План дневной смены',
            is_active=True,
        )
        EquipmentShiftPlan.objects.create(
            shift_plan=plan,
            equipment=truck,
            plan_trips=4,
            calculation_mode=PlanCalculationMode.TRIPS,
            is_active=True,
        )
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            excavator_operator=operator,
            driver=driver,
            loading_shift=loading_shift,
            unloading_shift=shift,
            rock_type=rock_type,
            dump_point=dump_point,
            volume_m3='38.00',
            tonnage='91.20',
            status=TripStatus.COMPLETED,
            completed_at=opened_at,
        )

        progress = calculate_equipment_shift_progress(truck, timezone.localtime(opened_at).date(), 'day')

        self.assertEqual(progress['trip_count'], 1)
        self.assertEqual(progress['progress_percent'], 25)
