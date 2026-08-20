import re
from unittest import mock

from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from rotations.models import ArrivalRosterRoutingEvent
from users.models import EmployeeAccess

from . import test_arrival_roster_cohort_creation as cohort_fixtures
from .cohorts import create_approved_arrival_roster_cohort
from .models import (
    EmployeeBedOccupancy,
    SettlementCohort,
    SettlementPreviewApplication,
    SettlementPreviewRun,
)


class ArrivalRosterCohortPreviewUiTests(TestCase):
    """Protected HTML entrypoint from exact routing cohort to saved M7 preview."""

    def setUp(self):
        cohort_fixtures.ArrivalRosterCohortCreationTests.setUp(self)

    _insert = cohort_fixtures.ArrivalRosterCohortCreationTests._insert
    _employee = cohort_fixtures.ArrivalRosterCohortCreationTests._employee
    _confirmed_batch = cohort_fixtures.ArrivalRosterCohortCreationTests._confirmed_batch
    _routing_row = cohort_fixtures.ArrivalRosterCohortCreationTests._routing_row
    _production_employee = (
        cohort_fixtures.ArrivalRosterCohortCreationTests._production_employee
    )
    _publish_event = cohort_fixtures.ArrivalRosterCohortCreationTests._publish_event
    _confirm_calendar = cohort_fixtures.ArrivalRosterCohortCreationTests._confirm_calendar
    _event = cohort_fixtures.ArrivalRosterCohortCreationTests._event
    _direct_row = cohort_fixtures.ArrivalRosterCohortCreationTests._direct_row
    _internal_employee = cohort_fixtures.ArrivalRosterCohortCreationTests._internal_employee

    def _queue_url(self):
        return reverse('settlement_arrival_roster_routing')

    def _preview_url(self, cohort):
        return reverse(
            'settlement_arrival_roster_create_preview',
            kwargs={'cohort_id': cohort.pk},
        )

    def _login(self, client=None, access=None):
        client = client or self.client
        session = client.session
        session['employee_access_id'] = (access or self.clerk_access).pk
        session.save()
        return client

    def _acquire(self, client=None):
        client = self._login(client)
        response = client.post(reverse('settlement_control_acquire'))
        self.assertEqual(response.status_code, 200, response.content)
        return client

    def _cohort(self):
        self._confirm_calendar()
        self._direct_row(employee=self._internal_employee('Preview UI participant'))
        return create_approved_arrival_roster_cohort(
            batch_id=self.batch.pk,
            actor_access_id=self.clerk_access.pk,
        )

    @staticmethod
    def _message_texts(response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    def test_approved_cohort_shows_exact_csrf_only_preview_form(self):
        cohort = self._cohort()

        response = self._login().get(self._queue_url())
        html = response.content.decode('utf-8')
        action = self._preview_url(cohort)
        form = re.search(
            rf'<form method="post" action="{re.escape(action)}">(.*?)</form>',
            html,
            re.DOTALL,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(form)
        self.assertContains(response, 'Подготовить предварительное расселение')
        self.assertEqual(
            set(re.findall(r'name="([^"]+)"', form.group(1))),
            {'csrfmiddlewaretoken'},
        )
        for forbidden in (
            'actor', 'access', 'cohort', 'batch', 'employee', 'resident',
            'shift', 'assignment', 'phase', 'date', 'sha', 'status',
        ):
            self.assertNotIn(f'name="{forbidden}"', form.group(1))

    def test_post_uses_url_cohort_and_exact_server_session_context(self):
        cohort = self._cohort()
        self._acquire()
        poison = {
            'actor': '999',
            'access': '999',
            'cohort': '999',
            'batch': '999',
            'employee': '999',
            'resident': '999',
            'shift': 'night',
            'assignment': '999',
            'phase': '999',
            'date': '2099-01-01',
            'sha': 'private-sha',
            'status': 'confirmed',
        }
        sentinel = object()

        with mock.patch(
            'settlement.views.create_settlement_preview_run',
            return_value=sentinel,
        ) as writer:
            response = self.client.post(self._preview_url(cohort), poison)

        self.assertRedirects(
            response,
            reverse('settlement_map'),
            fetch_redirect_response=False,
        )
        writer.assert_called_once()
        kwargs = writer.call_args.kwargs
        self.assertEqual(kwargs['cohort_id'], cohort.pk)
        context = kwargs['control_context']
        self.assertEqual(context.owner_access_id, self.clerk_access.pk)
        self.assertEqual(context.raw_session_key, self.client.session.session_key)
        self.assertNotIn('private-sha', response.content.decode('utf-8'))

    def test_success_creates_saved_rows_only_and_redirects_to_settlement_map(self):
        cohort = self._cohort()
        self._acquire()

        response = self.client.post(self._preview_url(cohort))

        self.assertRedirects(
            response,
            reverse('settlement_map'),
            fetch_redirect_response=False,
        )
        run = SettlementPreviewRun.objects.get()
        self.assertEqual(run.cohort_id, cohort.pk)
        self.assertEqual(run.status, SettlementPreviewRun.Status.DRAFT)
        self.assertEqual(
            run.placements.count() + run.unresolved_rows.count(),
            cohort.members.count(),
        )
        self.assertEqual(SettlementPreviewApplication.objects.count(), 0)
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 0)

    def test_repeat_post_uses_existing_saved_preview_versioning_policy(self):
        cohort = self._cohort()
        self._acquire()

        first = self.client.post(self._preview_url(cohort))
        second = self.client.post(self._preview_url(cohort))

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(
            list(SettlementPreviewRun.objects.order_by('version').values_list(
                'version', flat=True,
            )),
            [1, 2],
        )
        self.assertEqual(SettlementPreviewApplication.objects.count(), 0)
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 0)

    def test_missing_wrong_inactive_and_blocked_access_are_denied(self):
        cohort = self._cohort()
        wrong = EmployeeAccess.objects.create(
            employee=self.clerk,
            role=self.timekeeper_role,
            access_code='preview-ui-wrong',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        for label, access in (('missing', None), ('wrong', wrong)):
            with self.subTest(label=label):
                client = Client()
                if access is not None:
                    self._login(client, access)
                response = client.post(self._preview_url(cohort))
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse('clerk_login'), response.url)
                self.assertEqual(SettlementPreviewRun.objects.count(), 0)

        EmployeeAccess.objects.filter(pk=self.clerk_access.pk).update(is_active=False)
        inactive = self._login(Client()).post(self._preview_url(cohort))
        self.assertNotEqual(inactive.status_code, 200)
        EmployeeAccess.objects.filter(pk=self.clerk_access.pk).update(
            is_active=True,
            status=EmployeeAccess.Status.BLOCKED,
        )
        blocked = self._login(Client()).post(self._preview_url(cohort))
        self.assertNotEqual(blocked.status_code, 200)
        self.assertEqual(SettlementPreviewRun.objects.count(), 0)

    def test_command_is_post_only_and_csrf_protected(self):
        cohort = self._cohort()
        self.assertEqual(self._login().get(self._preview_url(cohort)).status_code, 405)

        client = Client(enforce_csrf_checks=True)
        self._login(client)
        response = client.post(self._preview_url(cohort))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(SettlementPreviewRun.objects.count(), 0)

    def _assert_invalid_cohort_has_no_button_or_preview(self, **mutation):
        cohort = self._cohort()
        SettlementCohort._base_manager.filter(pk=cohort.pk).update(**mutation)
        response = self._login().get(self._queue_url())
        self.assertNotIn(self._preview_url(cohort), response.content.decode('utf-8'))
        self._acquire()
        posted = self.client.post(self._preview_url(cohort))
        self.assertRedirects(
            posted,
            self._queue_url(),
            fetch_redirect_response=False,
        )
        self.assertEqual(SettlementPreviewRun.objects.count(), 0)

    def test_draft_cohort_has_no_button_or_preview(self):
        self._assert_invalid_cohort_has_no_button_or_preview(
            status=SettlementCohort.Status.DRAFT,
            approved_by=None,
            approved_at=None,
        )

    def test_superseded_cohort_has_no_button_or_preview(self):
        self._assert_invalid_cohort_has_no_button_or_preview(
            status=SettlementCohort.Status.SUPERSEDED,
            superseded_at=timezone.now(),
        )

    def test_corrupted_cohort_has_no_button_or_preview(self):
        self._assert_invalid_cohort_has_no_button_or_preview(source_type='damaged')

    def test_late_routing_drift_hides_button_and_returns_safe_error(self):
        cohort = self._cohort()
        row = cohort.members.get().routing_row
        self._event(row, ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW)

        page = self._login().get(self._queue_url())
        self.assertNotIn(self._preview_url(cohort), page.content.decode('utf-8'))
        self._acquire()
        response = self.client.post(self._preview_url(cohort))

        self.assertRedirects(
            response,
            self._queue_url(),
            fetch_redirect_response=False,
        )
        messages = self._message_texts(response)
        self.assertEqual(messages, [
            'Предварительное расселение не подготовлено. '
            'Проверьте исходные данные и повторите попытку.',
        ])
        serialized = ' '.join(messages) + response.content.decode('utf-8')
        for forbidden in ('settlement.', 'traceback', 'routing_row_id', 'snapshot', 'sha'):
            self.assertNotIn(forbidden, serialized.casefold())
        self.assertEqual(SettlementPreviewRun.objects.count(), 0)

    def test_controlled_error_is_safe_and_unexpected_error_is_not_masked(self):
        cohort = self._cohort()
        self._acquire()
        secret = 'traceback id=99 sha=private-snapshot'
        with mock.patch(
            'settlement.views.create_settlement_preview_run',
            side_effect=ValidationError(secret, code='settlement.preview.private'),
        ):
            response = self.client.post(self._preview_url(cohort))
        serialized = ' '.join(self._message_texts(response)) + response.content.decode('utf-8')
        self.assertNotIn(secret, serialized)
        self.assertNotIn('private-snapshot', serialized)

        with mock.patch(
            'settlement.views.create_settlement_preview_run',
            side_effect=RuntimeError('programming defect'),
        ):
            with self.assertRaisesMessage(RuntimeError, 'programming defect'):
                self.client.post(self._preview_url(cohort))

    def test_existing_routing_queue_remains_visible_and_get_is_read_only(self):
        cohort = self._cohort()
        row = cohort.members.get().routing_row
        before = {
            'cohort': SettlementCohort._base_manager.count(),
            'preview': SettlementPreviewRun._base_manager.count(),
        }

        response = self._login().get(self._queue_url())

        self.assertContains(response, row.employee.full_name)
        self.assertContains(response, 'Состав расселения сформирован')
        self.assertEqual(before, {
            'cohort': SettlementCohort._base_manager.count(),
            'preview': SettlementPreviewRun._base_manager.count(),
        })
