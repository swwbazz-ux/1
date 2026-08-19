from datetime import date
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from references.models import Equipment, EquipmentModel, EquipmentType
from rotations.arrival_roster_routing import _record_published_crew_plan_routing
from rotations.models import (
    ArrivalRosterMatch,
    ArrivalRosterRoutingEvent,
    ArrivalRosterRoutingRow,
    ArrivalRosterVersion,
)
from users.models import EmployeeAccess

from .models import (
    AssignmentStatus,
    CrewPlan,
    CrewPlanSlot,
    CrewPlanStatus,
    EquipmentAssignment,
    WorkShiftType,
)
from .services import publish_crew_plan
from .test_deputy_arrival_roster_routing import (
    DeputyArrivalRosterRoutingQueueTests as _DeputyArrivalRosterRoutingQueueTests,
)


class ArrivalRosterRoutingPublicationTests(_DeputyArrivalRosterRoutingQueueTests):
    work_date = date(2026, 9, 2)

    def _plan_with_slot(self, employee=None):
        equipment_type = EquipmentType.objects.create(
            name='Самосвал', is_active=True,
        )
        equipment_model = EquipmentModel.objects.create(
            equipment_type=equipment_type, name='Самосвал T2.5',
        )
        equipment = Equipment.objects.create(
            equipment_type=equipment_type,
            model=equipment_model,
            garage_number='T25-01',
            is_active=True,
        )
        plan = CrewPlan.objects.create(
            work_date=self.work_date,
            role=self.driver_role,
            status=CrewPlanStatus.DRAFT,
            revision=1,
        )
        day_slot = CrewPlanSlot.objects.create(
            plan=plan,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
            employee=employee,
        )
        CrewPlanSlot.objects.create(
            plan=plan,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_2,
        )
        return plan, day_slot

    def _driver_access(self, employee):
        return EmployeeAccess.objects.create(
            employee=employee,
            role=self.driver_role,
            access_code=f't25-driver-{employee.pk}',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    def _publish(self, plan, *, actor_access=None):
        with patch('assignments.services.production_work_date', return_value=self.work_date):
            return publish_crew_plan(
                plan=plan,
                expected_version=plan.version,
                actor=self.deputy,
                actor_access=actor_access or self.deputy_access,
            )

    def test_exact_published_slot_assignment_and_session_access_create_one_event(self):
        employee = self._production_employee('Точная публикация T2.5')
        self._driver_access(employee)
        row = self._routing_row(employee=employee)
        ArrivalRosterMatch._base_manager.filter(pk=row.match_id).update(evidence={
            'excel_shift_hint': 'night',
            'excel_color': 'red',
        })
        plan, slot = self._plan_with_slot(employee)

        published = self._publish(plan)

        event = ArrivalRosterRoutingEvent._base_manager.get(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
        )
        assignment = event.equipment_assignment
        self.assertEqual(published.status, CrewPlanStatus.PUBLISHED)
        self.assertEqual(event.actor_access_id, self.deputy_access.pk)
        self.assertEqual(event.crew_plan_slot_id, slot.pk)
        self.assertEqual(assignment.source_crew_plan_slot_id, slot.pk)
        self.assertEqual(
            (assignment.employee_id, assignment.role_id, assignment.equipment_id, assignment.shift_type),
            (slot.employee_id, plan.role_id, slot.equipment_id, slot.shift_type),
        )
        self.assertEqual(assignment.shift_type, WorkShiftType.SHIFT_1)
        response = self._login().get(self._url())
        self.assertNotContains(response, employee.full_name)

    def test_view_uses_session_access_and_ignores_forged_post_access_fields(self):
        employee = self._production_employee('Поддельный access T2.5')
        self._driver_access(employee)
        row = self._routing_row(employee=employee)
        plan, _slot = self._plan_with_slot(employee)
        client = self._login(Client())
        with patch('assignments.services.production_work_date', return_value=self.work_date):
            response = client.post(
                reverse('deputy_mining_manager_publish'),
                data='{"plan_id": %d, "expected_version": %d, "actor_access_id": 999999, "access_id": 999999}' % (
                    plan.pk, plan.version,
                ),
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 200, response.content)
        event = ArrivalRosterRoutingEvent._base_manager.get(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
        )
        self.assertEqual(event.actor_access_id, self.deputy_access.pk)

    def test_mismatch_role_or_date_adds_only_idempotent_requires_review(self):
        employee = self._production_employee('Изменённая роль T2.5')
        self._driver_access(employee)
        row = self._routing_row(employee=employee, role_code='excavator_operator')
        plan, _slot = self._plan_with_slot(employee)

        published = self._publish(plan)

        self.assertFalse(ArrivalRosterRoutingEvent._base_manager.filter(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
        ).exists())
        self.assertEqual(ArrivalRosterRoutingEvent._base_manager.filter(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW,
        ).count(), 1)
        _record_published_crew_plan_routing(
            plan=published,
            slots=list(published.slots.all()),
            actor_access=self.deputy_access,
        )
        self.assertEqual(ArrivalRosterRoutingEvent._base_manager.filter(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW,
        ).count(), 1)

    def test_superseded_batch_and_non_deputy_rows_do_not_receive_official_event(self):
        employee = self._production_employee('Заменённая версия T2.5')
        self._driver_access(employee)
        row = self._routing_row(employee=employee)
        ArrivalRosterVersion._base_manager.filter(pk=self.version.pk).update(
            status=ArrivalRosterVersion.Status.SUPERSEDED,
            superseded_at=timezone.now(),
        )
        plan, _slot = self._plan_with_slot(employee)

        self._publish(plan)

        self.assertFalse(ArrivalRosterRoutingEvent._base_manager.filter(
            routing_row=row,
            event_type__in=(
                ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
                ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW,
            ),
        ).exists())

    def test_changed_oup_role_and_invalid_dates_require_review_without_official_event(self):
        employee = self._production_employee('OUP и даты T2.5')
        self._driver_access(employee)
        row = self._routing_row(employee=employee)
        ArrivalRosterRoutingRow._base_manager.filter(pk=row.pk).update(
            dates_snapshot={'arrival_on': '2026-09-03', 'departure_on': '2026-09-28'},
        )
        employee.base_specialization = self.excavator_specialization
        employee.save(update_fields=['base_specialization'])
        plan, _slot = self._plan_with_slot(employee)

        with self.assertRaises(ValidationError) as caught:
            self._publish(plan)

        self.assertEqual(caught.exception.code, 'invalid_work_category')

        self.assertFalse(ArrivalRosterRoutingEvent._base_manager.filter(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
        ).exists())
        self.assertEqual(ArrivalRosterRoutingEvent._base_manager.filter(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW,
        ).count(), 1)

    def test_multiple_source_assignments_require_review_instead_of_official_event(self):
        employee = self._production_employee('Несколько assignments T2.5')
        self._driver_access(employee)
        row = self._routing_row(employee=employee)
        plan, slot = self._plan_with_slot(employee)
        with patch('assignments.services.production_work_date', return_value=self.work_date):
            published = publish_crew_plan(
                plan=plan,
                expected_version=plan.version,
                actor=self.deputy,
            )
        original = EquipmentAssignment._base_manager.get(source_crew_plan_slot=slot)
        EquipmentAssignment._base_manager.bulk_create([EquipmentAssignment(
            employee=original.employee,
            role=original.role,
            equipment=original.equipment,
            shift_type=original.shift_type,
            assigned_by=self.deputy,
            status=AssignmentStatus.CANCELLED,
            source_kind=EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN,
            source_crew_plan_slot=slot,
        )])

        _record_published_crew_plan_routing(
            plan=published,
            slots=list(published.slots.all()),
            actor_access=self.deputy_access,
        )

        self.assertFalse(ArrivalRosterRoutingEvent._base_manager.filter(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
        ).exists())
        self.assertTrue(ArrivalRosterRoutingEvent._base_manager.filter(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW,
        ).exists())
        queue = self._login().get(self._url())
        self.assertContains(queue, 'Требуется проверка')
        self.assertNotContains(queue, 'Ожидает назначения техники и смены')

    def test_missing_source_assignment_adds_requires_review_without_official_event(self):
        employee = self._production_employee('Нет assignment T2.5')
        self._driver_access(employee)
        row = self._routing_row(employee=employee)
        plan, _slot = self._plan_with_slot(employee)

        with patch('assignments.services.production_work_date', return_value=self.work_date), patch(
            'assignments.services._bulk_create_published_plan_assignments',
            return_value=[],
        ):
            publish_crew_plan(
                plan=plan,
                expected_version=plan.version,
                actor=self.deputy,
                actor_access=self.deputy_access,
            )

        self.assertFalse(ArrivalRosterRoutingEvent._base_manager.filter(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
        ).exists())
        self.assertTrue(ArrivalRosterRoutingEvent._base_manager.filter(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW,
        ).exists())

    def test_non_deputy_routes_are_ignored_while_exact_deputy_route_is_fixed(self):
        employee = self._production_employee('Разные маршруты T2.5')
        self._driver_access(employee)
        deputy_row = self._routing_row(employee=employee)
        clerk_employee = self._production_employee('Делопроизводитель T2.5')
        absent_employee = self._production_employee('Не участвует T2.5')
        clerk_row = self._routing_row(
            employee=clerk_employee,
            route_state=ArrivalRosterRoutingRow.RouteState.TO_CLERK,
        )
        absent_row = self._routing_row(
            employee=absent_employee,
            route_state=ArrivalRosterRoutingRow.RouteState.NOT_PARTICIPATING,
        )
        plan, _slot = self._plan_with_slot(employee)

        self._publish(plan)

        self.assertTrue(ArrivalRosterRoutingEvent._base_manager.filter(
            routing_row=deputy_row,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
        ).exists())
        for row in (clerk_row, absent_row):
            self.assertFalse(ArrivalRosterRoutingEvent._base_manager.filter(
                routing_row=row,
                event_type__in=(
                    ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
                    ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW,
                ),
            ).exists())

    def test_unexpected_routing_failure_rolls_back_plan_assignments_and_events(self):
        employee = self._production_employee('Rollback T2.5')
        self._driver_access(employee)
        row = self._routing_row(employee=employee)
        plan, _slot = self._plan_with_slot(employee)

        with patch('assignments.services.production_work_date', return_value=self.work_date), patch(
            'rotations.arrival_roster_routing._record_published_crew_plan_routing',
            side_effect=RuntimeError('forced routing failure'),
        ):
            with self.assertRaises(RuntimeError):
                publish_crew_plan(
                    plan=plan,
                    expected_version=plan.version,
                    actor=self.deputy,
                    actor_access=self.deputy_access,
                )

        plan.refresh_from_db()
        self.assertEqual(plan.status, CrewPlanStatus.DRAFT)
        self.assertFalse(EquipmentAssignment._base_manager.exists())
        self.assertFalse(ArrivalRosterRoutingEvent._base_manager.filter(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
        ).exists())

    def test_invalid_access_aborts_before_publish_and_routing_event(self):
        employee = self._production_employee('Неактивный access T2.5')
        self._driver_access(employee)
        row = self._routing_row(employee=employee)
        plan, _slot = self._plan_with_slot(employee)
        EmployeeAccess.objects.filter(pk=self.deputy_access.pk).update(is_active=False)

        with self.assertRaises(ValidationError) as caught:
            self._publish(plan)

        self.assertEqual(caught.exception.code, 'deputy_access_required')
        plan.refresh_from_db()
        self.assertEqual(plan.status, CrewPlanStatus.DRAFT)
        self.assertFalse(ArrivalRosterRoutingEvent._base_manager.filter(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
        ).exists())

    def test_wrong_blocked_or_missing_session_access_is_closed_before_publish(self):
        employee = self._production_employee('Закрытый access T2.5')
        self._driver_access(employee)
        plan, _slot = self._plan_with_slot(employee)
        wrong_access = EmployeeAccess.objects.create(
            employee=self.deputy,
            role=self.clerk_role,
            access_code='t25-clerk-access',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        payload = '{"plan_id": %d, "expected_version": %d}' % (plan.pk, plan.version)
        with patch('assignments.services.production_work_date', return_value=self.work_date):
            self.assertEqual(self._login(Client(), wrong_access).post(
                reverse('deputy_mining_manager_publish'), payload,
                content_type='application/json',
            ).status_code, 403)
            self.assertEqual(Client().post(
                reverse('deputy_mining_manager_publish'), payload,
                content_type='application/json',
            ).status_code, 403)
            EmployeeAccess.objects.filter(pk=self.deputy_access.pk).update(
                status=EmployeeAccess.Status.BLOCKED,
            )
            self.assertIn(self._login(Client()).post(
                reverse('deputy_mining_manager_publish'), payload,
                content_type='application/json',
            ).status_code, {403, 409})
        plan.refresh_from_db()
        self.assertEqual(plan.status, CrewPlanStatus.DRAFT)

    def test_publish_without_routing_row_keeps_existing_publication_result(self):
        employee = self._production_employee('Без routing row T2.5')
        self._driver_access(employee)
        plan, slot = self._plan_with_slot(employee)

        self._publish(plan)

        plan.refresh_from_db()
        self.assertEqual(plan.status, CrewPlanStatus.PUBLISHED)
        self.assertTrue(slot.equipment_assignments.filter(
            source_kind='deputy_published_plan',
        ).exists())
        self.assertEqual(ArrivalRosterRoutingEvent._base_manager.count(), 0)
