from unittest import mock

from django.contrib.messages import get_messages
from django.test import Client, TestCase
from django.urls import reverse

from rotations.models import (
    ArrivalRosterRoutingBatch,
    ArrivalRosterRoutingEvent,
)
from users.models import EmployeeAccess

from .cohorts import ArrivalRosterCohortCreationError
from .models import SettlementCohort, SettlementCohortMember
from . import test_arrival_roster_cohort_creation as cohort_fixtures


class ArrivalRosterCohortCreationViewTests(TestCase):
    """HTTP adapter for the closed T3 routing-to-cohort writer."""

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

    def _url(self, batch=None):
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

    def _ready_batch(self):
        self._confirm_calendar()
        self._direct_row(employee=self._internal_employee('HTTP T3 готов'))

    @staticmethod
    def _message_texts(response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    def test_post_passes_only_url_batch_and_exact_session_access_to_writer(self):
        self._login()
        poison = {
            'actor': '999',
            'access': '999',
            'employee': '999',
            'batch': '999',
            'resident': '999',
            'role': 'admin',
            'shift': 'night',
            'assignment': '999',
            'phase': '999',
            'dates': '2099-01-01',
            'sha': 'private-sha',
            'status': 'approved',
            'time': '2099-01-01T00:00:00Z',
        }
        sentinel = object()
        with mock.patch(
            'settlement.views.create_approved_arrival_roster_cohort',
            return_value=sentinel,
        ) as writer:
            response = self.client.post(self._url(), poison)

        self.assertRedirects(
            response,
            reverse('settlement_arrival_roster_routing'),
            fetch_redirect_response=False,
        )
        writer.assert_called_once_with(
            batch_id=self.batch.pk,
            actor_access_id=self.clerk_access.pk,
        )
        self.assertNotIn('private-sha', response.content.decode('utf-8'))

    def test_success_creates_cohort_redirects_and_repeat_is_idempotent(self):
        self._ready_batch()
        self._login()

        first = self.client.post(self._url())
        cohort = SettlementCohort.objects.get()
        before = {
            'cohort_count': SettlementCohort.objects.count(),
            'member_count': SettlementCohortMember.objects.count(),
            'cohort_id': cohort.pk,
            'approved_at': cohort.approved_at,
        }
        second = self.client.post(self._url())
        cohort.refresh_from_db()

        for response in (first, second):
            self.assertRedirects(
                response,
                reverse('settlement_arrival_roster_routing'),
                fetch_redirect_response=False,
            )
        self.assertEqual(before, {
            'cohort_count': SettlementCohort.objects.count(),
            'member_count': SettlementCohortMember.objects.count(),
            'cohort_id': cohort.pk,
            'approved_at': cohort.approved_at,
        })
        self.assertIn('Утверждённый состав заезда создан.', self._message_texts(second))

    def test_missing_wrong_inactive_and_blocked_access_are_denied_without_writes(self):
        wrong_access = EmployeeAccess.objects.create(
            employee=self.clerk,
            role=self.timekeeper_role,
            access_code='t3-http-wrong-role',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cases = (
            ('missing', None),
            ('wrong_role', wrong_access),
        )
        for label, access in cases:
            with self.subTest(label=label):
                client = Client()
                if access is not None:
                    self._login(client, access)
                response = client.post(self._url())
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse('clerk_login'), response.url)
                self.assertEqual(SettlementCohort.objects.count(), 0)
                self.assertEqual(SettlementCohortMember.objects.count(), 0)

        EmployeeAccess.objects.filter(pk=self.clerk_access.pk).update(is_active=False)
        response = self._login(Client()).post(self._url())
        self.assertIn(response.status_code, {302, 409})
        EmployeeAccess.objects.filter(pk=self.clerk_access.pk).update(
            is_active=True,
            status=EmployeeAccess.Status.BLOCKED,
        )
        response = self._login(Client()).post(self._url())
        self.assertIn(response.status_code, {302, 409})
        self.assertEqual(SettlementCohort.objects.count(), 0)
        self.assertEqual(SettlementCohortMember.objects.count(), 0)

    def test_get_is_method_not_allowed(self):
        response = self._login().get(self._url())
        self.assertEqual(response.status_code, 405)
        self.assertEqual(SettlementCohort.objects.count(), 0)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        self._login(client)

        rejected = client.post(self._url())
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(SettlementCohort.objects.count(), 0)

        csrf_secret = 'a' * 32
        client.cookies['csrftoken'] = csrf_secret
        accepted = client.post(self._url(), HTTP_X_CSRFTOKEN=csrf_secret)
        self.assertEqual(accepted.status_code, 302)

    def test_controlled_writer_error_is_safe_and_has_no_partial_writes(self):
        self._login()
        private_code = 'settlement.private.secret_code'
        private_text = 'traceback batch=123 sha=secret-snapshot'
        error = ArrivalRosterCohortCreationError(
            private_text,
            code=private_code,
            blocker_codes=('private',),
        )
        before = {
            'batch': ArrivalRosterRoutingBatch._base_manager.count(),
            'events': ArrivalRosterRoutingEvent._base_manager.count(),
        }
        with mock.patch(
            'settlement.views.create_approved_arrival_roster_cohort',
            side_effect=error,
        ):
            response = self.client.post(self._url())

        self.assertRedirects(
            response,
            reverse('settlement_arrival_roster_routing'),
            fetch_redirect_response=False,
        )
        messages = self._message_texts(response)
        self.assertEqual(
            messages,
            ['Состав заезда не создан. Проверьте готовность данных и повторите попытку.'],
        )
        serialized = ' '.join(messages) + response.content.decode('utf-8')
        self.assertNotIn(private_code, serialized)
        self.assertNotIn(private_text, serialized)
        self.assertNotIn('traceback', serialized.casefold())
        self.assertEqual(SettlementCohort.objects.count(), 0)
        self.assertEqual(SettlementCohortMember.objects.count(), 0)
        self.assertEqual(before, {
            'batch': ArrivalRosterRoutingBatch._base_manager.count(),
            'events': ArrivalRosterRoutingEvent._base_manager.count(),
        })

    def test_unexpected_programming_error_is_not_masked(self):
        self._login()
        with mock.patch(
            'settlement.views.create_approved_arrival_roster_cohort',
            side_effect=RuntimeError('programming defect'),
        ):
            with self.assertRaisesMessage(RuntimeError, 'programming defect'):
                self.client.post(self._url())

        self.assertEqual(SettlementCohort.objects.count(), 0)
        self.assertEqual(SettlementCohortMember.objects.count(), 0)
