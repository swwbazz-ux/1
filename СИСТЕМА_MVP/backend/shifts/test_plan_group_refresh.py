from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from core.production_time import production_work_date
from references.models import Equipment, EquipmentType
from users.models import Employee

from .models import EmployeeShift, EquipmentPlanGroup, PlanCalculationMode
from .services import assign_shift_plan_snapshot, refresh_open_shift_plan_snapshots_for_group


class PlanGroupRefreshTests(TestCase):
    def setUp(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        self.trucks = [Equipment.objects.create(equipment_type=truck_type, garage_number=f'QA-{i}')
                       for i in range(3)]
        self.group = EquipmentPlanGroup.objects.create(
            name='Проверка пересчёта', code='qa-refresh',
            calculation_mode=PlanCalculationMode.TRIPS, plan_value=10,
            is_active=True, active_from=production_work_date(),
        )
        self.group.equipment.add(*self.trucks[:2])

    def shift(self, index, *, closed=False):
        employee = Employee.objects.create(full_name=f'Проверка {index} {closed}')
        shift = EmployeeShift.objects.create(
            employee=employee, equipment=self.trucks[index], shift_type='day',
            opened_at=timezone.now(), opened_by=employee,
        )
        assign_shift_plan_snapshot(shift)
        if closed:
            shift.closed_at = timezone.now()
            shift.save(update_fields=['closed_at'])
        return shift

    def test_refresh_updates_affected_open_shifts_and_preserves_closed_and_unrelated(self):
        affected = self.shift(0)
        closed = self.shift(1, closed=True)
        unrelated = self.shift(2)
        unrelated_stamp = unrelated.plan_assigned_at
        self.group.plan_value = 20
        self.group.save(update_fields=['plan_value'])

        self.assertEqual(refresh_open_shift_plan_snapshots_for_group(self.group), 1)
        for shift in (affected, closed, unrelated):
            shift.refresh_from_db()
        self.assertEqual(affected.plan_value, Decimal('20'))
        self.assertEqual(closed.plan_value, Decimal('10'))
        self.assertEqual(unrelated.plan_assigned_at, unrelated_stamp)

    def test_refresh_includes_removed_equipment_and_new_members(self):
        removed = self.shift(0)
        added = self.shift(2)
        self.group.equipment.set([self.trucks[2]])

        self.assertEqual(refresh_open_shift_plan_snapshots_for_group(self.group), 2)
        removed.refresh_from_db()
        added.refresh_from_db()
        self.assertIsNone(removed.plan_group_id)
        self.assertIsNone(removed.plan_value)
        self.assertEqual(added.plan_group_id, self.group.pk)
        self.assertEqual(added.plan_value, Decimal('10'))

    def test_missing_or_unsaved_group_does_not_touch_shifts(self):
        self.shift(0)
        with self.assertNumQueries(0):
            self.assertEqual(refresh_open_shift_plan_snapshots_for_group(None), 0)
            self.assertEqual(refresh_open_shift_plan_snapshots_for_group(EquipmentPlanGroup()), 0)
