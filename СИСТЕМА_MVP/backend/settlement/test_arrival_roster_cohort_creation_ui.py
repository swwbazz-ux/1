import re
from dataclasses import FrozenInstanceError
from datetime import date

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from rotations.models import (
    ArrivalRosterRoutingBatch,
    ArrivalRosterRoutingEvent,
    ArrivalRosterRoutingRow,
    ArrivalRosterVersion,
)
from shifts.models import WatchPeriod, WatchPeriodBrigadePhaseVersion
from users.models import EmployeeAccess

from .cohorts import create_approved_arrival_roster_cohort
from .models import SettlementCohort, SettlementCohortMember
from . import test_arrival_roster_cohort_creation as cohort_fixtures


class ArrivalRosterCohortCreationUiTests(TestCase):
    """Read-only clerk UI for starting exact routing cohort creation."""

    def setUp(self):
        cohort_fixtures.ArrivalRosterCohortCreationTests.setUp(self)

    _insert = cohort_fixtures.ArrivalRosterCohortCreationTests._insert
    _employee = cohort_fixtures.ArrivalRosterCohortCreationTests._employee
    _confirmed_batch = cohort_fixtures.ArrivalRosterCohortCreationTests._confirmed_batch
    _routing_row = cohort_fixtures.ArrivalRosterCohortCreationTests._routing_row
    _production_employee = cohort_fixtures.ArrivalRosterCohortCreationTests._production_employee
    _publish_event = cohort_fixtures.ArrivalRosterCohortCreationTests._publish_event
    _confirm_calendar = cohort_fixtures.ArrivalRosterCohortCreationTests._confirm_calendar
    _event = cohort_fixtures.ArrivalRosterCohortCreationTests._event
    _direct_row = cohort_fixtures.ArrivalRosterCohortCreationTests._direct_row
    _internal_employee = cohort_fixtures.ArrivalRosterCohortCreationTests._internal_employee

    def _queue_url(self):
        return reverse('settlement_arrival_roster_routing')

    def _create_url(self, batch=None):
        return reverse(
            'settlement_arrival_roster_create_cohort',
            kwargs={'batch_id': (batch or self.batch).pk},
        )

    def _login(self, client=None, access=None):
        client = client or self.client
        session = client.session
        session['employee_access_id'] = (access or self.clerk_access).pk
        session.save()
        return client

    def _html(self, client=None):
        client = self._login(client)
        response = client.get(self._queue_url())
        self.assertEqual(response.status_code, 200, response.content)
        return response, response.content.decode('utf-8')

    def _make_ready(self, name='Готовый участник T3 UI'):
        self._confirm_calendar()
        return self._direct_row(employee=self._internal_employee(name))

    def test_ready_batch_has_exact_post_form_csrf_counts_and_no_hidden_domain_fields(self):
        self._make_ready()

        response, html = self._html()
        action = self._create_url()
        form = re.search(
            rf'<form method="post" action="{re.escape(action)}">(.*?)</form>',
            html,
            re.DOTALL,
        )

        self.assertIsNotNone(form)
        self.assertContains(response, 'Сформировать состав расселения')
        self.assertContains(response, 'Готово: 1 чел.')
        self.assertContains(response, 'Не заезжают: 0')
        self.assertIn('name="csrfmiddlewaretoken"', form.group(1))
        hidden_names = set(re.findall(r'name="([^"]+)"', form.group(1)))
        self.assertEqual(hidden_names, {'csrfmiddlewaretoken'})
        for forbidden in (
            'actor', 'access', 'employee', 'batch', 'resident', 'role',
            'shift', 'assignment', 'phase', 'sha', 'status', 'time',
        ):
            self.assertNotIn(f'name="{forbidden}"', form.group(1))
        overview = response.context['cohort_overview']
        self.assertIsInstance(overview.batches, tuple)
        with self.assertRaises(FrozenInstanceError):
            overview.batches[0].can_create = False

    def test_approved_cohort_shows_status_time_and_members_without_button(self):
        self._make_ready()
        cohort = create_approved_arrival_roster_cohort(
            batch_id=self.batch.pk,
            actor_access_id=self.clerk_access.pk,
        )

        response, html = self._html()

        self.assertContains(response, 'Состав расселения сформирован')
        self.assertContains(response, '1 чел.')
        self.assertContains(
            response,
            timezone.localtime(cohort.approved_at).strftime('%d.%m.%Y'),
        )
        self.assertNotIn(self._create_url(), html)
        self.assertNotContains(response, 'Сформировать состав расселения')

    def test_corrupted_linked_cohort_is_fail_closed_without_button(self):
        self._make_ready()
        cohort = create_approved_arrival_roster_cohort(
            batch_id=self.batch.pk,
            actor_access_id=self.clerk_access.pk,
        )
        SettlementCohort._base_manager.filter(pk=cohort.pk).update(
            source_type='damaged',
        )

        response, html = self._html()

        self.assertContains(response, 'Требуется проверка')
        self.assertContains(
            response,
            'Связанный состав расселения повреждён или имеет неожиданное состояние.',
        )
        self.assertNotIn(self._create_url(), html)

    def test_pending_review_and_stale_rows_never_offer_button(self):
        pending = self._routing_row(employee=self._internal_employee(
            'Ожидает назначения T3 UI', production=True,
        ))
        review = self._direct_row(employee=self._internal_employee('Review T3 UI'))
        stale = self._direct_row(employee=self._internal_employee('Stale T3 UI'))
        self._event(review, ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW)
        self._event(stale, ArrivalRosterRoutingEvent.EventType.STALE)

        response, html = self._html()

        self.assertContains(response, 'Требуется проверка')
        self.assertContains(response, 'Официальное назначение техники и смены ещё не опубликовано.')
        self.assertContains(response, 'Строка требует проверки табельщиком.')
        self.assertContains(response, 'Передача строки устарела.')
        self.assertNotIn(self._create_url(), html)
        self.assertNotIn(str(pending.pk), response.context['cohort_overview'].batches[0].blocker_messages)

    def test_off_phase_blocks_creation(self):
        self._confirm_calendar()
        self._direct_row(employee=self._internal_employee(
            'Межвахта T3 UI', brigade=3,
        ))

        response, html = self._html()

        self.assertContains(
            response,
            'Сотрудник отмечен прибывающим, но его бригада находится на межвахте.',
        )
        self.assertNotIn(self._create_url(), html)

    def test_missing_calendar_blocks_creation(self):
        self._direct_row(employee=self._internal_employee('Нет календаря T3 UI'))

        response, html = self._html()

        self.assertContains(
            response,
            'Для графика и периода нет утверждённого календаря фаз.',
        )
        self.assertNotIn(self._create_url(), html)

    def test_external_blocker_has_no_button_and_masks_private_values(self):
        phone = '+79995550778'
        self._direct_row(employee=None, phone=phone)

        response, html = self._html()

        self.assertContains(response, 'Для внешнего жильца смена не определяется автоматически.')
        self.assertNotIn(self._create_url(), html)
        self.assertNotIn(phone, html)
        self.assertNotIn(self.batch.confirmation_sha256, html)
        for forbidden in (
            'external_shift_unresolved', 'fingerprint', 'snapshot',
            'employee_access_id', 'routing_row_id', 'routing_event_id',
            'resident_id', 'equipment_assignment_id', 'crew_plan_slot_id',
            'brigade_phase_row_id', 'traceback', 'name="pin"',
        ):
            self.assertNotIn(forbidden, html.casefold())

    def test_wrong_inactive_and_blocked_access_do_not_receive_page(self):
        wrong = EmployeeAccess.objects.create(
            employee=self.clerk,
            role=self.timekeeper_role,
            access_code='t3-ui-wrong',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.assertNotEqual(self._login(Client(), wrong).get(self._queue_url()).status_code, 200)

        EmployeeAccess.objects.filter(pk=self.clerk_access.pk).update(is_active=False)
        self.assertNotEqual(self._login(Client()).get(self._queue_url()).status_code, 200)
        EmployeeAccess.objects.filter(pk=self.clerk_access.pk).update(
            is_active=True,
            status=EmployeeAccess.Status.BLOCKED,
        )
        self.assertNotEqual(self._login(Client()).get(self._queue_url()).status_code, 200)

    def test_current_batches_are_unique_stably_ordered_and_historical_is_hidden(self):
        later = WatchPeriod.objects.create(
            name='Поздний период T3 UI',
            watch_composition=self.composition,
            starts_on=date(2026, 10, 1),
            ends_on=date(2026, 10, 31),
            is_active=True,
        )
        _later_version, later_batch = self._confirmed_batch(later)
        historical = WatchPeriod.objects.create(
            name='Исторический период T3 UI',
            watch_composition=self.composition,
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            is_active=False,
        )
        _old_version, old_batch = self._confirmed_batch(
            historical,
            status=ArrivalRosterVersion.Status.SUPERSEDED,
        )

        response, html = self._html()
        batch_ids = [item.batch_id for item in response.context['cohort_overview'].batches]

        self.assertEqual(batch_ids, [self.batch.pk, later_batch.pk])
        self.assertEqual(len(batch_ids), len(set(batch_ids)))
        self.assertNotIn(old_batch.pk, batch_ids)
        self.assertEqual(html.count(self.period.name), 1)
        self.assertEqual(html.count(later.name), 1)
        self.assertNotIn(historical.name, html)

    def test_get_and_repeat_are_read_only_and_keep_t26_queue_visible(self):
        row = self._make_ready('Очередь T2.6 сохранена T3 UI')
        models = (
            ArrivalRosterVersion,
            ArrivalRosterRoutingBatch,
            ArrivalRosterRoutingRow,
            ArrivalRosterRoutingEvent,
            WatchPeriodBrigadePhaseVersion,
            SettlementCohort,
            SettlementCohortMember,
        )
        before = {model: model._base_manager.count() for model in models}

        first, first_html = self._html()
        second, second_html = self._html()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIn(row.employee.full_name, first_html)
        self.assertIn(row.employee.full_name, second_html)
        self.assertEqual(before, {
            model: model._base_manager.count()
            for model in models
        })
