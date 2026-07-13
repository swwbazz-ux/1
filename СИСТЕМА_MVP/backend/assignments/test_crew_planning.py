from datetime import datetime, timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from core.models import OperationalStateEvent
from references.models import Equipment, EquipmentType
from users.models import Employee, EmployeeAccess, Role

from .models import (
    AssignmentStatus,
    CrewPlan,
    CrewPlanStatus,
    EquipmentAssignment,
    WorkShiftType,
)
from .services import (
    get_or_create_crew_draft,
    production_work_date,
    publish_crew_plan,
    set_active_equipment_assignment,
    update_crew_draft_slot,
)


class CrewPlanningServiceTests(TestCase):
    def setUp(self):
        self.driver_role = Role.objects.create(code='driver', name='Водитель самосвала')
        self.excavator_role = Role.objects.create(code='excavator_operator', name='Машинист экскаватора')
        self.actor = Employee.objects.create(
            full_name='Заместитель начальника участка',
            status=Employee.Status.ACTIVE,
        )
        self.driver_1 = self.create_employee_with_access('Водитель 1', self.driver_role, '210001')
        self.driver_2 = self.create_employee_with_access('Водитель 2', self.driver_role, '210002')
        self.free_driver = self.create_employee_with_access('Водитель 3', self.driver_role, '210003')
        self.truck_type = EquipmentType.objects.create(name='Самосвал')
        self.excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck_1 = Equipment.objects.create(equipment_type=self.truck_type, garage_number='Т-01')
        self.truck_2 = Equipment.objects.create(equipment_type=self.truck_type, garage_number='Т-02')
        self.excavator = Equipment.objects.create(equipment_type=self.excavator_type, garage_number='Э-01')
        self.assignment_1 = self.assign(
            self.driver_1,
            self.driver_role,
            self.truck_1,
            WorkShiftType.SHIFT_1,
        )
        self.assignment_2 = self.assign(
            self.driver_2,
            self.driver_role,
            self.truck_2,
            WorkShiftType.SHIFT_1,
        )

    def create_employee_with_access(self, name, role, code):
        employee = Employee.objects.create(full_name=name, status=Employee.Status.ACTIVE)
        EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code=code,
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        return employee

    def assign(self, employee, role, equipment, shift_type):
        assignment, _created = set_active_equipment_assignment(
            employee=employee,
            role=role,
            equipment=equipment,
            shift_type=shift_type,
            assigned_by=self.actor,
        )
        return assignment

    def validation_code(self, error):
        return error.exception.error_list[0].code

    def test_production_work_date_changes_at_seven_am(self):
        before_boundary = timezone.make_aware(datetime(2026, 7, 13, 6, 59))
        at_boundary = timezone.make_aware(datetime(2026, 7, 13, 7, 0))

        self.assertEqual(production_work_date(before_boundary).isoformat(), '2026-07-12')
        self.assertEqual(production_work_date(at_boundary).isoformat(), '2026-07-13')

    def test_draft_contains_explicit_day_night_slots_and_baseline(self):
        plan, created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)

        self.assertTrue(created)
        self.assertEqual(plan.slots.count(), 4)
        day_slot = plan.slots.get(equipment=self.truck_1, shift_type=WorkShiftType.SHIFT_1)
        night_slot = plan.slots.get(equipment=self.truck_1, shift_type=WorkShiftType.SHIFT_2)
        self.assertEqual(day_slot.employee, self.driver_1)
        self.assertEqual(day_slot.baseline_employee, self.driver_1)
        self.assertIsNone(night_slot.employee)
        self.assertIsNone(night_slot.baseline_employee)

        same_plan, second_created = get_or_create_crew_draft(role='driver', actor=self.actor)
        self.assertFalse(second_created)
        self.assertEqual(same_plan.id, plan.id)

    def test_draft_update_moves_and_swaps_without_changing_baseline(self):
        plan, _created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)
        target = plan.slots.get(equipment=self.truck_2, shift_type=WorkShiftType.SHIFT_1)

        updated = update_crew_draft_slot(
            plan=plan,
            equipment=self.truck_2,
            shift_type=WorkShiftType.SHIFT_1,
            employee=self.driver_1,
            expected_version=plan.version,
            actor=self.actor,
        )

        source = updated.slots.get(equipment=self.truck_1, shift_type=WorkShiftType.SHIFT_1)
        target.refresh_from_db()
        self.assertEqual(source.employee, self.driver_2)
        self.assertEqual(target.employee, self.driver_1)
        self.assertEqual(source.baseline_employee, self.driver_1)
        self.assertEqual(target.baseline_employee, self.driver_2)
        self.assertEqual(updated.version, 2)

        with self.assertRaises(ValidationError) as error:
            update_crew_draft_slot(
                plan=updated,
                equipment=self.truck_1,
                shift_type=WorkShiftType.SHIFT_2,
                employee=self.free_driver,
                expected_version=1,
                actor=self.actor,
            )
        self.assertEqual(self.validation_code(error), 'stale_version')

    def test_draft_rejects_employee_without_matching_work_category_or_activated_access(self):
        employee = Employee.objects.create(full_name='Без доступа', status=Employee.Status.ACTIVE)
        EmployeeAccess.objects.create(
            employee=employee,
            role=self.driver_role,
            access_code='219999',
            status=EmployeeAccess.Status.NOT_ACTIVATED,
            is_active=True,
        )
        plan, _created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)

        with self.assertRaises(ValidationError) as error:
            update_crew_draft_slot(
                plan=plan,
                equipment=self.truck_1,
                shift_type=WorkShiftType.SHIFT_2,
                employee=employee,
                expected_version=plan.version,
                actor=self.actor,
            )

        self.assertEqual(self.validation_code(error), 'invalid_work_category')

    def test_draft_accepts_employee_work_category_without_access(self):
        employee = Employee.objects.create(
            full_name='Новый водитель ОУП',
            status=Employee.Status.ACTIVE,
            is_active=True,
            work_category=Employee.WorkCategory.DRIVER,
        )
        plan, _created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)

        updated = update_crew_draft_slot(
            plan=plan,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_2,
            employee=employee,
            expected_version=plan.version,
            actor=self.actor,
        )

        slot = updated.slots.get(equipment=self.truck_1, shift_type=WorkShiftType.SHIFT_2)
        self.assertEqual(slot.employee, employee)

    def test_explicit_work_category_overrides_legacy_activated_access(self):
        employee = Employee.objects.create(
            full_name='Переведенный машинист',
            status=Employee.Status.ACTIVE,
            is_active=True,
            work_category=Employee.WorkCategory.EXCAVATOR_OPERATOR,
        )
        EmployeeAccess.objects.create(
            employee=employee,
            role=self.driver_role,
            access_code='218888',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        plan, _created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)

        with self.assertRaises(ValidationError) as error:
            update_crew_draft_slot(
                plan=plan,
                equipment=self.truck_1,
                shift_type=WorkShiftType.SHIFT_2,
                employee=employee,
                expected_version=plan.version,
                actor=self.actor,
            )

        self.assertEqual(self.validation_code(error), 'invalid_work_category')

    def test_closed_production_day_cannot_be_edited_or_published(self):
        plan, _created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)
        CrewPlan.objects.filter(pk=plan.pk).update(
            work_date=production_work_date() - timedelta(days=1),
        )
        plan.refresh_from_db()

        with self.assertRaises(ValidationError) as update_error:
            update_crew_draft_slot(
                plan=plan,
                equipment=self.truck_1,
                shift_type=WorkShiftType.SHIFT_2,
                employee=self.free_driver,
                expected_version=plan.version,
                actor=self.actor,
            )
        self.assertEqual(self.validation_code(update_error), 'plan_work_date_closed')

        with self.assertRaises(ValidationError) as publish_error:
            publish_crew_plan(
                plan=plan,
                expected_version=plan.version,
                actor=self.actor,
            )
        self.assertEqual(self.validation_code(publish_error), 'plan_work_date_closed')

        plan.refresh_from_db()
        self.assertEqual(plan.status, CrewPlanStatus.DRAFT)
        self.assignment_1.refresh_from_db()
        self.assertIsNone(self.assignment_1.ended_at)

    def test_publish_allows_empty_slots_and_preserves_unchanged_assignment(self):
        plan, _created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)
        updated = update_crew_draft_slot(
            plan=plan,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
            employee=None,
            expected_version=plan.version,
            actor=self.actor,
        )
        event_count = OperationalStateEvent.objects.filter(
            event_type='personnel_assignment_changed',
            payload__action='crew_plan_published',
        ).count()

        published = publish_crew_plan(
            plan=updated,
            expected_version=updated.version,
            actor=self.actor,
        )

        self.assertEqual(published.status, CrewPlanStatus.PUBLISHED)
        self.assignment_1.refresh_from_db()
        self.assignment_2.refresh_from_db()
        self.assertIsNotNone(self.assignment_1.ended_at)
        self.assertEqual(self.assignment_1.ended_by, self.actor)
        self.assertIsNone(self.assignment_2.ended_at)
        self.assertEqual(
            EquipmentAssignment.objects.filter(
                role=self.driver_role,
                status=AssignmentStatus.ACCEPTED,
                ended_at__isnull=True,
                shift__isnull=True,
            ).count(),
            1,
        )
        self.assertEqual(
            OperationalStateEvent.objects.filter(
                event_type='personnel_assignment_changed',
                payload__action='crew_plan_published',
            ).count(),
            event_count + 1,
        )

    def test_publish_rejects_changed_baseline(self):
        plan, _created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)
        self.assign(
            self.driver_1,
            self.driver_role,
            self.truck_2,
            WorkShiftType.SHIFT_2,
        )

        with self.assertRaises(ValidationError) as error:
            publish_crew_plan(
                plan=plan,
                expected_version=plan.version,
                actor=self.actor,
            )

        self.assertEqual(self.validation_code(error), 'stale_baseline')
        plan.refresh_from_db()
        self.assertEqual(plan.status, CrewPlanStatus.DRAFT)

    def test_publish_integrity_conflict_rolls_back_closed_assignments(self):
        plan, _created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)
        updated = update_crew_draft_slot(
            plan=plan,
            equipment=self.truck_2,
            shift_type=WorkShiftType.SHIFT_1,
            employee=self.driver_1,
            expected_version=plan.version,
            actor=self.actor,
        )

        with patch(
            'assignments.services.EquipmentAssignment.objects.bulk_create',
            side_effect=IntegrityError('forced conflict'),
        ):
            with self.assertRaises(ValidationError) as error:
                publish_crew_plan(
                    plan=updated,
                    expected_version=updated.version,
                    actor=self.actor,
                )

        self.assertEqual(self.validation_code(error), 'assignment_conflict')
        self.assignment_1.refresh_from_db()
        self.assignment_2.refresh_from_db()
        updated.refresh_from_db()
        self.assertIsNone(self.assignment_1.ended_at)
        self.assertIsNone(self.assignment_2.ended_at)
        self.assertEqual(updated.status, CrewPlanStatus.DRAFT)

    def test_publish_rejects_target_employee_active_in_other_role(self):
        dual_role_employee = self.create_employee_with_access(
            'Совмещающий сотрудник',
            self.excavator_role,
            '310001',
        )
        EmployeeAccess.objects.create(
            employee=dual_role_employee,
            role=self.driver_role,
            access_code='210004',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        plan, _created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)
        updated = update_crew_draft_slot(
            plan=plan,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_2,
            employee=dual_role_employee,
            expected_version=plan.version,
            actor=self.actor,
        )
        excavator_assignment = self.assign(
            dual_role_employee,
            self.excavator_role,
            self.excavator,
            WorkShiftType.SHIFT_2,
        )

        with self.assertRaises(ValidationError) as error:
            publish_crew_plan(
                plan=updated,
                expected_version=updated.version,
                actor=self.actor,
            )

        self.assertEqual(self.validation_code(error), 'assignment_conflict')
        excavator_assignment.refresh_from_db()
        self.assertIsNone(excavator_assignment.ended_at)

    def test_draft_rejects_employee_active_in_other_role(self):
        dual_role_employee = self.create_employee_with_access(
            'Совмещающий сотрудник',
            self.excavator_role,
            '310002',
        )
        EmployeeAccess.objects.create(
            employee=dual_role_employee,
            role=self.driver_role,
            access_code='210005',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        excavator_assignment = self.assign(
            dual_role_employee,
            self.excavator_role,
            self.excavator,
            WorkShiftType.SHIFT_2,
        )
        plan, _created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)

        with self.assertRaises(ValidationError) as error:
            update_crew_draft_slot(
                plan=plan,
                equipment=self.truck_1,
                shift_type=WorkShiftType.SHIFT_2,
                employee=dual_role_employee,
                expected_version=plan.version,
                actor=self.actor,
            )

        self.assertEqual(self.validation_code(error), 'assignment_conflict')
        excavator_assignment.refresh_from_db()
        self.assertIsNone(excavator_assignment.ended_at)

    def test_new_publication_supersedes_previous_snapshot(self):
        first, _created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)
        first = publish_crew_plan(
            plan=first,
            expected_version=first.version,
            actor=self.actor,
        )
        second, created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)

        self.assertTrue(created)
        self.assertEqual(second.revision, 2)
        second = publish_crew_plan(
            plan=second,
            expected_version=second.version,
            actor=self.actor,
        )

        first.refresh_from_db()
        self.assertEqual(first.status, CrewPlanStatus.SUPERSEDED)
        self.assertEqual(second.status, CrewPlanStatus.PUBLISHED)
        self.assertEqual(
            CrewPlan.objects.filter(
                work_date=second.work_date,
                role=self.driver_role,
                status=CrewPlanStatus.PUBLISHED,
            ).count(),
            1,
        )
