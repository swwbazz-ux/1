from datetime import date

from django.test import Client, TestCase
from django.urls import reverse

from assignments.models import (
    CrewPlan,
    CrewPlanSlot,
    EquipmentAssignment,
)
from assignments.test_deputy_arrival_roster_routing import (
    DeputyArrivalRosterRoutingQueueTests,
)
from rotations.models import (
    ArrivalRosterRoutingBatch,
    ArrivalRosterRoutingEvent,
    ArrivalRosterRoutingRow,
    ArrivalRosterVersion,
)
from settlement.models import SettlementCohort, SettlementCohortMember
from shifts.models import WatchPeriod
from users.models import Employee, EmployeeAccess


class SettlementArrivalRosterRoutingQueueTests(TestCase):
    """T2.6 HTTP contract for the clerk's immutable routing projection."""

    def setUp(self):
        DeputyArrivalRosterRoutingQueueTests.setUp(self)
        self.clerk = self._employee('Делопроизводитель T2.6')
        self.clerk_access = EmployeeAccess.objects.create(
            employee=self.clerk,
            role=self.clerk_role,
            access_code='t26-clerk',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    _insert = DeputyArrivalRosterRoutingQueueTests._insert
    _employee = DeputyArrivalRosterRoutingQueueTests._employee
    _confirmed_batch = DeputyArrivalRosterRoutingQueueTests._confirmed_batch
    _routing_row = DeputyArrivalRosterRoutingQueueTests._routing_row
    _production_employee = DeputyArrivalRosterRoutingQueueTests._production_employee
    _publish_event = DeputyArrivalRosterRoutingQueueTests._publish_event

    def _url(self):
        return reverse('settlement_arrival_roster_routing')

    def _clerk_login(self, client=None, access=None):
        client = client or self.client
        session = client.session
        session['employee_access_id'] = (access or self.clerk_access).pk
        session.save()
        return client

    def _direct_row(self, *, employee=None, batch=None, phone='+79995550071'):
        row = self._routing_row(
            employee=employee,
            batch=batch,
            route_state=ArrivalRosterRoutingRow.RouteState.TO_CLERK,
            role_code=None,
            phone=phone,
        )
        ArrivalRosterRoutingRow._base_manager.filter(pk=row.pk).update(
            role_snapshot={
                'role_code': None,
                'qualification_state': 'not_production',
            },
        )
        row.refresh_from_db()
        self._insert(ArrivalRosterRoutingEvent(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.SENT_TO_CLERK,
            actor_access=self.timekeeper_access,
        ))
        return row

    def _requires_review(self, row):
        self._insert(ArrivalRosterRoutingEvent(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW,
            actor_access=self.timekeeper_access,
        ))

    def _html(self, client=None):
        response = self._clerk_login(client).get(self._url())
        self.assertEqual(response.status_code, 200, response.content)
        return response.content.decode('utf-8')

    def test_exact_active_clerk_opens_get_only_queue_without_domain_writes(self):
        employee = self._employee('Прямой сотрудник T2.6')
        self._direct_row(employee=employee)
        before = {
            model: model._base_manager.count()
            for model in (
                ArrivalRosterRoutingBatch,
                ArrivalRosterRoutingRow,
                ArrivalRosterRoutingEvent,
                CrewPlan,
                CrewPlanSlot,
                EquipmentAssignment,
                SettlementCohort,
                SettlementCohortMember,
            )
        }

        html = self._html()

        self.assertIn(employee.full_name, html)
        self.assertEqual(before, {
            model: model._base_manager.count()
            for model in before
        })

    def test_wrong_inactive_blocked_and_missing_access_are_closed(self):
        wrong_access = EmployeeAccess.objects.create(
            employee=self.clerk,
            role=self.driver_role,
            access_code='t26-wrong',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.assertEqual(self._clerk_login(Client(), wrong_access).get(self._url()).status_code, 302)
        self.assertEqual(Client().get(self._url()).status_code, 302)
        EmployeeAccess.objects.filter(pk=self.clerk_access.pk).update(is_active=False)
        self.assertEqual(self._clerk_login(Client()).get(self._url()).status_code, 302)
        EmployeeAccess.objects.filter(pk=self.clerk_access.pk).update(
            is_active=True,
            status=EmployeeAccess.Status.BLOCKED,
        )
        self.assertEqual(self._clerk_login(Client()).get(self._url()).status_code, 302)

    def test_direct_internal_and_external_rows_are_ready_without_deputy_assignment(self):
        employee = self._employee('Непроизводственный сотрудник T2.6')
        internal = self._direct_row(employee=employee)
        external = self._direct_row(employee=None, phone='+79995550072')

        html = self._html()

        self.assertIn(internal.employee.full_name, html)
        self.assertIn(external.resident.display_name, html)
        self.assertIn('Направлен непосредственно', html)
        self.assertIn('Не назначается заместителем', html)

    def test_production_row_requires_exact_official_event_and_shows_equipment_day_night(self):
        employee = self._production_employee('Водитель готов T2.6')
        row = self._routing_row(employee=employee)
        pending = self._production_employee('Водитель ожидает T2.6')
        self._routing_row(employee=pending)
        self._publish_event(row)

        html = self._html()
        event = ArrivalRosterRoutingEvent._base_manager.get(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
        )

        self.assertIn(employee.full_name, html)
        self.assertIn(str(event.equipment_assignment.equipment), html)
        self.assertIn('День', html)
        self.assertNotIn(pending.full_name, html)

    def test_requires_review_and_later_review_override_ready_without_creating_events(self):
        direct = self._direct_row(employee=self._employee('Прямой review T2.6'))
        production = self._routing_row(employee=self._production_employee('Производственный review T2.6'))
        self._publish_event(production)
        self._requires_review(direct)
        self._requires_review(production)
        before_events = ArrivalRosterRoutingEvent._base_manager.count()

        html = self._html()

        self.assertIn('Требуется проверка', html)
        self.assertIn(direct.employee.full_name, html)
        self.assertIn(production.employee.full_name, html)
        self.assertEqual(ArrivalRosterRoutingEvent._base_manager.count(), before_events)
        ready_html, review_html = html.split('<h3 id="settlement-review-title">', 1)
        self.assertNotIn(direct.employee.full_name, ready_html)
        self.assertNotIn(production.employee.full_name, ready_html)
        self.assertIn(direct.employee.full_name, review_html)
        self.assertIn(production.employee.full_name, review_html)

    def test_role_change_and_damaged_slot_assignment_are_blocked(self):
        damaged = self._production_employee('Связь нарушена T2.6')
        damaged_row = self._routing_row(employee=damaged)
        self._publish_event(damaged_row)
        event = ArrivalRosterRoutingEvent._base_manager.get(
            routing_row=damaged_row,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
        )
        EquipmentAssignment._base_manager.filter(
            pk=event.equipment_assignment_id,
        ).update(shift_type=None)
        changed = self._production_employee('Роль изменена T2.6')
        changed_row = self._routing_row(employee=changed)
        self._insert(ArrivalRosterRoutingEvent(
            routing_row=changed_row,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
            actor_access=self.deputy_access,
            crew_plan_slot=event.crew_plan_slot,
            equipment_assignment=event.equipment_assignment,
        ))
        changed.base_specialization = self.excavator_specialization
        changed.save(update_fields=['base_specialization'])

        html = self._html()
        ready_html, review_html = html.split('<h3 id="settlement-review-title">', 1)
        self.assertNotIn(changed.full_name, ready_html)
        self.assertNotIn(damaged.full_name, ready_html)
        self.assertIn('Роль изменена ОУП — требуется проверка', review_html)
        self.assertIn('Связь назначения техники и смены нарушена.', review_html)

    def test_superseded_not_arriving_and_duplicate_row_do_not_appear_or_duplicate(self):
        old_version, old_batch = self._confirmed_batch(
            self.period,
            status=ArrivalRosterVersion.Status.SUPERSEDED,
        )
        old = self._direct_row(
            employee=self._employee('Заменённый T2.6'),
            batch=old_batch,
        )
        absent = self._routing_row(
            employee=self._production_employee('Не приезжает T2.6'),
            participation='not_arriving',
            route_state=ArrivalRosterRoutingRow.RouteState.TO_CLERK,
        )
        ready = self._direct_row(employee=self._employee('Один раз T2.6'))

        html = self._html()

        self.assertNotIn(old.employee.full_name, html)
        self.assertNotIn(absent.employee.full_name, html)
        self.assertEqual(html.count(ready.employee.full_name), 1)

    def test_sorting_is_by_period_then_name_not_primary_key(self):
        later = self._direct_row(employee=self._employee('Яна позже T2.6'))
        earlier = self._direct_row(employee=self._employee('Анна раньше T2.6'))
        next_period = WatchPeriod.objects.create(
            name='Следующая вахта T2.6',
            watch_composition=self.composition,
            starts_on=date(2026, 10, 1),
            ends_on=date(2026, 10, 30),
            is_active=True,
        )
        _version, next_batch = self._confirmed_batch(next_period)
        next_row = self._direct_row(
            employee=self._employee('Борис следующая T2.6'),
            batch=next_batch,
        )

        html = self._html()

        self.assertLess(html.index(earlier.employee.full_name), html.index(later.employee.full_name))
        self.assertLess(html.index(later.employee.full_name), html.index(next_row.employee.full_name))

    def test_html_is_private_and_has_no_t3_controls_or_hidden_post_form(self):
        employee = self._employee('Конфиденциальный T2.6')
        employee.phone = '+79995550926'
        employee.save(update_fields=['phone'])
        self._direct_row(employee=employee)

        html = self._html()

        for forbidden in (
            employee.phone,
            self.version.confirmation_sha256,
            'confirmation_snapshot',
            'source_fingerprint',
            'employee_access_id',
            'routing_row_id',
            'equipment_assignment_id',
            'Назначить технику',
            'Выбрать смену',
            'Выбрать комнату',
            'Выбрать койку',
            'Авторосселение',
            '<form',
            'method="post"',
        ):
            self.assertNotIn(forbidden, html)
