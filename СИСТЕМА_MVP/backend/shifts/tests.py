from datetime import timedelta
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from references.models import DumpPoint, Equipment, EquipmentModel, EquipmentType, RockType
from trips.models import Trip, TripStatus
from users.models import Employee

from .models import EmployeeShift, EquipmentPlanGroup, EquipmentShiftPlan, PlanAssignmentStatus, PlanCalculationMode, ShiftPlan
from .equipment_plan_groups import (
    reconcile_default_equipment_plan_groups,
    validate_equipment_plan_group_membership,
)
from .services import assign_shift_plan_snapshot, calculate_equipment_shift_progress, calculate_open_shift_progress, shift_plan_totals


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
