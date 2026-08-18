from datetime import datetime, timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
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
    _bulk_create_published_plan_assignments,
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

    def test_assignment_reloads_locked_employee_before_validation(self):
        stale_employee = self.free_driver
        Employee.objects.filter(pk=stale_employee.pk).update(
            status=Employee.Status.DISMISSED,
            is_active=False,
        )

        with self.assertRaises(ValidationError):
            set_active_equipment_assignment(
                employee=stale_employee,
                role=self.driver_role,
                equipment=self.truck_1,
                shift_type=WorkShiftType.SHIFT_1,
                assigned_by=self.actor,
            )

        self.assertFalse(EquipmentAssignment.objects.filter(employee=stale_employee).exists())

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

    def test_publish_replaces_unverified_assignment_with_official_provenance(self):
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
        self.assertIsNotNone(self.assignment_2.ended_at)
        self.assertEqual(self.assignment_2.ended_by, self.actor)
        official_assignment = EquipmentAssignment.objects.get(
            role=self.driver_role,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
            shift__isnull=True,
        )
        source_slot = published.slots.get(
            equipment=self.truck_2,
            shift_type=WorkShiftType.SHIFT_1,
        )
        self.assertEqual(
            official_assignment.source_kind,
            EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN,
        )
        self.assertEqual(official_assignment.source_crew_plan_slot, source_slot)
        self.assertEqual(official_assignment.employee_id, source_slot.employee_id)
        self.assertEqual(official_assignment.equipment_id, source_slot.equipment_id)
        self.assertEqual(official_assignment.role_id, source_slot.plan.role_id)
        self.assertEqual(official_assignment.shift_type, source_slot.shift_type)
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
            'assignments.services._bulk_create_published_plan_assignments',
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
        first_assignment = EquipmentAssignment.objects.get(
            employee=self.driver_1,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
        )
        first_source_slot_id = first_assignment.source_crew_plan_slot_id
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
        first_assignment.refresh_from_db()
        second_assignment = EquipmentAssignment.objects.get(
            employee=self.driver_1,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
        )
        second_source_slot = second.slots.get(
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
        )
        self.assertIsNotNone(first_assignment.ended_at)
        self.assertEqual(first_assignment.source_crew_plan_slot_id, first_source_slot_id)
        first_assignment.full_clean()
        self.assertNotEqual(second_assignment.pk, first_assignment.pk)
        self.assertEqual(
            second_assignment.source_kind,
            EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN,
        )
        self.assertEqual(second_assignment.source_crew_plan_slot, second_source_slot)
        self.assertEqual(
            CrewPlan.objects.filter(
                work_date=second.work_date,
                role=self.driver_role,
                status=CrewPlanStatus.PUBLISHED,
            ).count(),
            1,
        )

    def test_manual_assignment_remains_unverified(self):
        self.assertEqual(
            self.assignment_1.source_kind,
            EquipmentAssignment.SourceKind.UNVERIFIED,
        )
        self.assertIsNone(self.assignment_1.source_crew_plan_slot_id)

    def test_assignment_source_shape_and_slot_mapping_are_validated(self):
        plan, _created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)
        published = publish_crew_plan(
            plan=plan,
            expected_version=plan.version,
            actor=self.actor,
        )
        slot = published.slots.get(
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
        )
        ended_at = timezone.now()

        official_without_slot = EquipmentAssignment(
            employee=self.driver_1,
            role=self.driver_role,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
            status=AssignmentStatus.CANCELLED,
            ended_at=ended_at,
            source_kind=EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN,
        )
        with self.assertRaises(ValidationError):
            official_without_slot.full_clean()

        unverified_with_slot = EquipmentAssignment(
            employee=self.driver_1,
            role=self.driver_role,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
            status=AssignmentStatus.CANCELLED,
            ended_at=ended_at,
            source_kind=EquipmentAssignment.SourceKind.UNVERIFIED,
            source_crew_plan_slot=slot,
        )
        with self.assertRaises(ValidationError):
            unverified_with_slot.full_clean()

        mismatches = (
            ('employee', self.driver_2, 'employee'),
            ('equipment', self.truck_2, 'equipment'),
            ('role', self.excavator_role, 'role'),
            ('shift_type', WorkShiftType.SHIFT_2, 'shift_type'),
        )
        for field, value, error_field in mismatches:
            candidate = EquipmentAssignment(
                employee=self.driver_1,
                role=self.driver_role,
                equipment=self.truck_1,
                shift_type=WorkShiftType.SHIFT_1,
                status=AssignmentStatus.CANCELLED,
                ended_at=ended_at,
                source_kind=EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN,
                source_crew_plan_slot=slot,
            )
            setattr(candidate, field, value)
            with self.subTest(field=field), self.assertRaises(ValidationError) as error:
                candidate.full_clean()
            self.assertIn(error_field, error.exception.error_dict)

    def test_draft_plan_slot_cannot_be_official_source(self):
        plan, _created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)
        slot = plan.slots.get(
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
        )
        assignment = EquipmentAssignment(
            employee=self.driver_1,
            role=self.driver_role,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
            status=AssignmentStatus.CANCELLED,
            ended_at=timezone.now(),
            source_kind=EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN,
            source_crew_plan_slot=slot,
        )

        with self.assertRaises(ValidationError) as error:
            assignment.full_clean()

        self.assertIn('source_crew_plan_slot', error.exception.error_dict)

    def _official_assignment_candidate(self, slot, **overrides):
        values = {
            'employee': slot.employee,
            'role': slot.plan.role,
            'equipment': slot.equipment,
            'shift_type': slot.shift_type,
            'status': AssignmentStatus.CANCELLED,
            'ended_at': timezone.now(),
            'source_kind': EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN,
            'source_crew_plan_slot': slot,
        }
        values.update(overrides)
        return EquipmentAssignment(**values)

    def _published_slot(self):
        plan, _created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)
        published = publish_crew_plan(
            plan=plan,
            expected_version=plan.version,
            actor=self.actor,
        )
        return published.slots.get(
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
        )

    def test_new_official_assignment_save_is_rejected_before_write(self):
        slot = self._published_slot()
        assignment = self._official_assignment_candidate(slot)
        before = EquipmentAssignment.objects.count()

        with self.assertRaises(ValidationError) as error:
            assignment.save()

        self.assertEqual(
            error.exception.code,
            'official_assignment_requires_published_plan_service',
        )
        self.assertIsNone(assignment.pk)
        self.assertEqual(EquipmentAssignment.objects.count(), before)

    def test_public_create_rejects_official_assignment_before_write(self):
        slot = self._published_slot()
        before = EquipmentAssignment.objects.count()

        with self.assertRaises(ValidationError) as error:
            EquipmentAssignment.objects.create(
                employee=slot.employee,
                role=slot.plan.role,
                equipment=slot.equipment,
                shift_type=slot.shift_type,
                status=AssignmentStatus.CANCELLED,
                ended_at=timezone.now(),
                source_kind=EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN,
                source_crew_plan_slot=slot,
            )

        self.assertEqual(
            error.exception.code,
            'official_assignment_requires_published_plan_service',
        )
        self.assertEqual(EquipmentAssignment.objects.count(), before)

    def test_public_bulk_create_rejects_official_assignment_before_write(self):
        slot = self._published_slot()
        before = EquipmentAssignment.objects.count()

        with self.assertRaises(ValidationError) as error:
            EquipmentAssignment.objects.bulk_create([
                self._official_assignment_candidate(slot),
            ])

        self.assertEqual(
            error.exception.code,
            'official_assignment_requires_published_plan_service',
        )
        self.assertEqual(EquipmentAssignment.objects.count(), before)

    def test_public_bulk_create_rejects_unverified_assignment_with_source_slot(self):
        slot = self._published_slot()
        before = EquipmentAssignment.objects.count()
        assignment = self._official_assignment_candidate(
            slot,
            source_kind=EquipmentAssignment.SourceKind.UNVERIFIED,
        )

        with self.assertRaises(ValidationError) as error:
            EquipmentAssignment.objects.bulk_create([assignment])

        self.assertEqual(
            error.exception.code,
            'official_assignment_requires_published_plan_service',
        )
        self.assertEqual(EquipmentAssignment.objects.count(), before)

    def test_public_bulk_create_allows_unverified_assignment(self):
        before = EquipmentAssignment.objects.count()
        assignment = EquipmentAssignment(
            employee=self.free_driver,
            role=self.driver_role,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_2,
            status=AssignmentStatus.CANCELLED,
            ended_at=timezone.now(),
        )

        EquipmentAssignment.objects.bulk_create([assignment])

        self.assertIsNotNone(assignment.pk)
        self.assertEqual(EquipmentAssignment.objects.count(), before + 1)
        self.assertEqual(
            assignment.source_kind,
            EquipmentAssignment.SourceKind.UNVERIFIED,
        )
        self.assertIsNone(assignment.source_crew_plan_slot_id)

    def test_trusted_batch_validation_rolls_back_every_assignment(self):
        slot = self._published_slot()
        valid = self._official_assignment_candidate(slot)
        invalid = self._official_assignment_candidate(
            slot,
            employee=self.driver_2,
        )
        before = EquipmentAssignment._base_manager.count()

        with self.assertRaises(ValidationError):
            _bulk_create_published_plan_assignments([valid, invalid])

        self.assertEqual(EquipmentAssignment._base_manager.count(), before)
        self.assertIsNone(valid.pk)
        self.assertIsNone(invalid.pk)

    def test_official_assignment_provenance_is_immutable(self):
        plan, _created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)
        published = publish_crew_plan(
            plan=plan,
            expected_version=plan.version,
            actor=self.actor,
        )
        assignment = EquipmentAssignment.objects.get(
            employee=self.driver_1,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
        )
        other_slot = published.slots.get(
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_2,
        )

        assignment.source_crew_plan_slot = other_slot
        with self.assertRaises(ValidationError) as save_error:
            assignment.save()
        self.assertEqual(save_error.exception.code, 'immutable_assignment_provenance')

        with self.assertRaises(ValidationError) as update_error:
            EquipmentAssignment.objects.filter(pk=assignment.pk).update(
                source_kind=EquipmentAssignment.SourceKind.UNVERIFIED,
                source_crew_plan_slot=None,
            )
        self.assertEqual(update_error.exception.code, 'immutable_assignment_provenance')

    def test_official_assignment_lifecycle_fields_remain_writable(self):
        slot = self._published_slot()
        assignment = EquipmentAssignment.objects.get(
            source_crew_plan_slot=slot,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
        )
        source_kind = assignment.source_kind
        source_slot_id = assignment.source_crew_plan_slot_id

        assignment.status = AssignmentStatus.CANCELLED
        assignment.ended_at = timezone.now()
        assignment.ended_by = self.actor
        assignment.save(update_fields=['status', 'ended_at', 'ended_by'])

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.CANCELLED)
        self.assertEqual(assignment.ended_by, self.actor)
        self.assertEqual(assignment.source_kind, source_kind)
        self.assertEqual(assignment.source_crew_plan_slot_id, source_slot_id)

    def test_official_source_slot_is_protected(self):
        plan, _created = get_or_create_crew_draft(role=self.driver_role, actor=self.actor)
        published = publish_crew_plan(
            plan=plan,
            expected_version=plan.version,
            actor=self.actor,
        )
        slot = published.slots.get(
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
        )

        with self.assertRaises(ProtectedError):
            slot.delete()


class EquipmentAssignmentProvenanceMigrationTests(TransactionTestCase):
    migrate_from = ('assignments', '0006_crewplan_crewplanslot_and_more')
    migrate_to = ('assignments', '0007_equipment_assignment_provenance')

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        other_leaf_targets = [
            target
            for target in executor.loader.graph.leaf_nodes()
            if target[0] != 'assignments'
        ]
        self.before_targets = [*other_leaf_targets, self.migrate_from]
        self.after_targets = [*other_leaf_targets, self.migrate_to]
        executor.migrate(self.before_targets)
        old_apps = executor.loader.project_state(self.before_targets).apps
        Employee = old_apps.get_model('users', 'Employee')
        Role = old_apps.get_model('users', 'Role')
        EquipmentType = old_apps.get_model('references', 'EquipmentType')
        Equipment = old_apps.get_model('references', 'Equipment')
        HistoricalAssignment = old_apps.get_model('assignments', 'EquipmentAssignment')
        role = Role.objects.create(code='migration_driver', name='Migration Driver')
        employee = Employee.objects.create(
            full_name='Legacy Assignment',
            status='active',
        )
        equipment_type = EquipmentType.objects.create(name='Migration Truck')
        equipment = Equipment.objects.create(
            equipment_type=equipment_type,
            garage_number='MIG-01',
        )
        self.assignment_pk = HistoricalAssignment.objects.create(
            employee=employee,
            role=role,
            equipment=equipment,
            shift_type='day',
            status='accepted',
            accepted_at=timezone.now(),
        ).pk

        executor = MigrationExecutor(connection)
        executor.migrate(self.after_targets)
        self.migrated_apps = executor.loader.project_state(self.after_targets).apps

    def tearDown(self):
        MigrationExecutor(connection).migrate(self.after_targets)
        super().tearDown()

    def test_existing_assignment_gets_no_invented_published_source(self):
        MigratedAssignment = self.migrated_apps.get_model(
            'assignments',
            'EquipmentAssignment',
        )

        assignment = MigratedAssignment.objects.get(pk=self.assignment_pk)

        self.assertEqual(assignment.source_kind, 'unverified')
        self.assertIsNone(assignment.source_crew_plan_slot_id)
