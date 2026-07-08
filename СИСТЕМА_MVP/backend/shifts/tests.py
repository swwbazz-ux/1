from django.test import TestCase
from django.utils import timezone

from references.models import DumpPoint, Equipment, EquipmentType, RockType
from trips.models import Trip, TripStatus
from users.models import Employee

from .models import EmployeeShift, EquipmentShiftPlan, PlanCalculationMode, ShiftPlan
from .services import calculate_equipment_shift_progress, shift_plan_totals


class ShiftPlanServiceTests(TestCase):
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
