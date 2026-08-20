from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse

from shifts.brigade_phase_calendar import (
    BrigadePhaseCalendarError,
    create_watch_period_brigade_phase_draft,
)
from shifts.models import (
    WatchPeriod,
    WatchPeriodBrigadePhaseRow,
    WatchPeriodBrigadePhaseVersion,
)
from users.models import Employee, EmployeeAccess, Role, WorkSchedule


class TimekeeperBrigadePhaseCalendarUiTests(TestCase):
    order_sha = 'a' * 64
    schedule_sha = 'b' * 64

    def setUp(self):
        self.timekeeper_role = Role.objects.get(code='timekeeper')
        self.other_role, _created = Role.objects.get_or_create(
            code='brigade-calendar-ui-other',
            defaults={'name': 'Другая роль календаря'},
        )
        self.admin_role, _created = Role.objects.get_or_create(
            code='admin',
            defaults={'name': 'Администратор'},
        )
        self.employee = Employee.objects.create(
            full_name='Табельщик календаря',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.timekeeper_role,
            access_code='PHASE-CALENDAR-UI',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.other_employee = Employee.objects.create(
            full_name='Сотрудник другой роли',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.other_access = EmployeeAccess.objects.create(
            employee=self.other_employee,
            role=self.other_role,
            access_code='PHASE-CALENDAR-OTHER',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.other_employee,
            role=self.admin_role,
            access_code='PHASE-CALENDAR-ADMIN',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.schedule_11, _created = WorkSchedule.objects.update_or_create(
            code='schedule_11',
            defaults={
                'name': 'Официальный график 11',
                'brigade_count': 2,
                'is_active': True,
            },
        )
        self.schedule_12, _created = WorkSchedule.objects.update_or_create(
            code='schedule_12',
            defaults={
                'name': 'Официальный график 12',
                'brigade_count': 4,
                'is_active': True,
            },
        )
        self.unsupported_schedule = WorkSchedule.objects.create(
            code='calendar-ui-unsupported',
            name='Неподдерживаемый график',
            brigade_count=3,
            is_active=True,
        )
        self.period = WatchPeriod.objects.create(
            name='Вахта календаря 1',
            starts_on=date(2032, 8, 14),
            ends_on=date(2032, 9, 13),
        )
        self.later_period = WatchPeriod.objects.create(
            name='Вахта календаря 2',
            starts_on=date(2032, 9, 14),
            ends_on=date(2032, 10, 13),
        )
        self.page_url = reverse('timekeeper_brigade_phase_calendar')
        self.create_url = reverse('timekeeper_brigade_phase_calendar_create')
        self._login()

    def _login(self, access=None, client=None):
        client = client or self.client
        session = client.session
        session['employee_access_id'] = (access or self.access).pk
        session.save()
        return client

    def _selection_url(self, schedule=None, period=None):
        schedule = schedule or self.schedule_12
        period = period or self.period
        return f'{self.page_url}?watch_period={period.pk}&work_schedule={schedule.pk}'

    def _payload(self, schedule=None, period=None, **overrides):
        schedule = schedule or self.schedule_12
        period = period or self.period
        payload = {
            'watch_period': period.pk,
            'work_schedule': schedule.pk,
            'order_number': 'Приказ № 17',
            'order_date': '2032-08-01',
            'effective_from': '2032-08-01',
            'order_checksum': self.order_sha,
            'schedule_designation': (
                'График № 11/1' if schedule.code == 'schedule_11' else 'График № 12/1'
            ),
            'schedule_checksum': self.schedule_sha,
        }
        if schedule.code == 'schedule_11':
            payload.update({'brigade_1_phase': 'day', 'brigade_2_phase': 'off'})
        else:
            payload.update({
                'brigade_1_phase': 'night',
                'brigade_2_phase': 'day',
                'brigade_3_phase': 'off',
                'brigade_4_phase': 'off',
            })
        payload.update(overrides)
        return payload

    def _create_draft(self, **overrides):
        payload = self._payload(**overrides)
        return create_watch_period_brigade_phase_draft(
            watch_period_id=payload['watch_period'],
            work_schedule_id=payload['work_schedule'],
            actor_access_id=self.access.pk,
            order_number=payload['order_number'],
            order_date=payload['order_date'],
            effective_from=payload['effective_from'],
            order_document_sha256=payload['order_checksum'],
            schedule_designation=payload['schedule_designation'],
            schedule_document_sha256=payload['schedule_checksum'],
            brigade_phases=[
                {'brigade_number': number, 'phase': payload[f'brigade_{number}_phase']}
                for number in range(1, 5)
            ],
        )

    def test_exact_active_timekeeper_opens_read_only_page(self):
        before = (
            WatchPeriodBrigadePhaseVersion._base_manager.count(),
            WatchPeriodBrigadePhaseRow._base_manager.count(),
        )
        response = self.client.get(self._selection_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Календарь фаз бригад')
        self.assertEqual(before, (
            WatchPeriodBrigadePhaseVersion._base_manager.count(),
            WatchPeriodBrigadePhaseRow._base_manager.count(),
        ))
        self.client.get(self._selection_url())
        self.assertEqual(before, (
            WatchPeriodBrigadePhaseVersion._base_manager.count(),
            WatchPeriodBrigadePhaseRow._base_manager.count(),
        ))

    def test_missing_wrong_inactive_blocked_and_admin_access_are_closed(self):
        cases = []
        cases.append(Client())
        cases.append(self._login(self.other_access, Client()))
        cases.append(self._login(self.admin_access, Client()))
        inactive_access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.timekeeper_role,
            access_code='PHASE-CALENDAR-INACTIVE',
            status=EmployeeAccess.Status.DEACTIVATED,
            is_active=False,
        )
        cases.append(self._login(inactive_access, Client()))
        blocked_access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.timekeeper_role,
            access_code='PHASE-CALENDAR-BLOCKED',
            status=EmployeeAccess.Status.BLOCKED,
            is_active=True,
        )
        cases.append(self._login(blocked_access, Client()))
        for client in cases:
            with self.subTest(session=client.session.session_key):
                response = client.get(self.page_url)
                self.assertEqual(response.status_code, 302)
        self.assertEqual(WatchPeriodBrigadePhaseVersion._base_manager.count(), 0)

    def test_schedule_12_renders_four_brigades_and_all_phases(self):
        response = self.client.get(self._selection_url(self.schedule_12))
        self.assertEqual(response.status_code, 200)
        for number in range(1, 5):
            self.assertContains(response, f'name="brigade_{number}_phase"')
        self.assertContains(response, 'value="day"', count=4)
        self.assertContains(response, 'value="night"', count=4)
        self.assertContains(response, 'value="off"', count=4)

    def test_schedule_11_renders_two_brigades_without_night(self):
        response = self.client.get(self._selection_url(self.schedule_11))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="brigade_1_phase"')
        self.assertContains(response, 'name="brigade_2_phase"')
        self.assertNotContains(response, 'name="brigade_3_phase"')
        self.assertNotContains(response, 'value="night"')

    @patch('rotations.views.create_watch_period_brigade_phase_draft')
    def test_create_calls_existing_service_with_exact_session_access(self, command):
        response = self.client.post(self.create_url, self._payload(
            actor_access_id=self.other_access.pk,
            employee_access_id=self.other_access.pk,
            status='confirmed',
            version_number='99',
            based_on_version='88',
            source_snapshot='forged',
            source_fingerprint='c' * 64,
            created_by_access='77',
            confirmed_at='2000-01-01T00:00:00Z',
        ))
        self.assertEqual(response.status_code, 302)
        command.assert_called_once_with(
            watch_period_id=self.period.pk,
            work_schedule_id=self.schedule_12.pk,
            actor_access_id=self.access.pk,
            order_number='Приказ № 17',
            order_date='2032-08-01',
            effective_from='2032-08-01',
            order_document_sha256=self.order_sha,
            schedule_designation='График № 12/1',
            schedule_document_sha256=self.schedule_sha,
            brigade_phases=[
                {'brigade_number': 1, 'phase': 'night'},
                {'brigade_number': 2, 'phase': 'day'},
                {'brigade_number': 3, 'phase': 'off'},
                {'brigade_number': 4, 'phase': 'off'},
            ],
        )
        self.assertIn(f'watch_period={self.period.pk}', response.url)
        self.assertIn(f'work_schedule={self.schedule_12.pk}', response.url)

    def test_valid_post_creates_server_owned_draft(self):
        response = self.client.post(self.create_url, self._payload())
        self.assertEqual(response.status_code, 302)
        version = WatchPeriodBrigadePhaseVersion._base_manager.get()
        self.assertEqual(version.status, WatchPeriodBrigadePhaseVersion.Status.DRAFT)
        self.assertEqual(version.version_number, 1)
        self.assertIsNone(version.based_on_version_id)
        self.assertEqual(version.created_by_access_id, self.access.pk)
        self.assertIsNone(version.confirmed_at)
        self.assertEqual(version.source_snapshot['order']['document_sha256'], self.order_sha)
        self.assertEqual(version.rows.count(), 4)

    def test_incomplete_duplicate_and_wrong_policy_are_controlled(self):
        invalid_payloads = [
            self._payload(brigade_4_phase=''),
            self._payload(brigade_1_phase='day', brigade_2_phase='day'),
        ]
        duplicate = self._payload()
        duplicate['brigade_1_phase'] = ['night', 'day']
        invalid_payloads.append(duplicate)
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.post(self.create_url, payload, follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'фаз')
                self.assertEqual(WatchPeriodBrigadePhaseVersion._base_manager.count(), 0)

    def test_unsupported_or_inconsistent_schedule_is_rejected(self):
        unsupported = self.client.post(
            self.create_url,
            self._payload(schedule=self.unsupported_schedule),
            follow=True,
        )
        self.assertContains(unsupported, 'поддерживаемый график')
        self.schedule_12.brigade_count = 3
        self.schedule_12.save(update_fields=['brigade_count'])
        inconsistent = self.client.post(self.create_url, self._payload(), follow=True)
        self.assertContains(inconsistent, 'поддерживаемый график')
        self.assertEqual(WatchPeriodBrigadePhaseVersion._base_manager.count(), 0)

    def test_confirm_post_confirms_exact_draft_and_redirects_to_pair(self):
        draft = self._create_draft()
        response = self.client.post(
            reverse('timekeeper_brigade_phase_calendar_confirm', args=[draft.pk]),
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f'watch_period={self.period.pk}', response.url)
        self.assertIn(f'work_schedule={self.schedule_12.pk}', response.url)
        draft.refresh_from_db()
        self.assertEqual(draft.status, WatchPeriodBrigadePhaseVersion.Status.CONFIRMED)
        self.assertEqual(draft.confirmed_by_access_id, self.access.pk)

    def test_confirm_ignores_client_actor_status_and_time(self):
        draft = self._create_draft()
        with patch('rotations.views.confirm_watch_period_brigade_phase_version') as command:
            command.return_value = SimpleNamespace(
                watch_period_id=self.period.pk,
                work_schedule_id=self.schedule_12.pk,
            )
            response = self.client.post(
                reverse('timekeeper_brigade_phase_calendar_confirm', args=[draft.pk]),
                {
                    'actor_access_id': self.other_access.pk,
                    'employee_access_id': self.other_access.pk,
                    'status': 'superseded',
                    'confirmed_at': '2000-01-01T00:00:00Z',
                    'source_fingerprint': 'c' * 64,
                    'version_id': 999999,
                },
            )
        self.assertEqual(response.status_code, 302)
        command.assert_called_once_with(version_id=draft.pk, actor_access_id=self.access.pk)

    def test_next_confirmation_supersedes_previous_confirmed(self):
        first = self._create_draft()
        self.client.post(reverse(
            'timekeeper_brigade_phase_calendar_confirm', args=[first.pk],
        ))
        second = self._create_draft(order_number='Приказ № 18')
        self.client.post(reverse(
            'timekeeper_brigade_phase_calendar_confirm', args=[second.pk],
        ))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, WatchPeriodBrigadePhaseVersion.Status.SUPERSEDED)
        self.assertEqual(second.status, WatchPeriodBrigadePhaseVersion.Status.CONFIRMED)
        self.assertEqual(second.based_on_version_id, first.pk)

    def test_repeat_confirmation_is_idempotent(self):
        draft = self._create_draft()
        url = reverse('timekeeper_brigade_phase_calendar_confirm', args=[draft.pk])
        self.client.post(url)
        draft.refresh_from_db()
        confirmed_at = draft.confirmed_at
        self.client.post(url)
        draft.refresh_from_db()
        self.assertEqual(draft.status, WatchPeriodBrigadePhaseVersion.Status.CONFIRMED)
        self.assertEqual(draft.confirmed_at, confirmed_at)
        self.assertEqual(
            WatchPeriodBrigadePhaseVersion._base_manager.filter(
                watch_period=self.period,
                work_schedule=self.schedule_12,
            ).count(),
            1,
        )

    def test_command_endpoints_are_post_only(self):
        draft = self._create_draft()
        self.assertEqual(self.client.get(self.create_url).status_code, 405)
        self.assertEqual(self.client.get(reverse(
            'timekeeper_brigade_phase_calendar_confirm', args=[draft.pk],
        )).status_code, 405)

    def test_command_endpoints_require_csrf(self):
        client = self._login(client=Client(enforce_csrf_checks=True))
        self.assertEqual(client.post(self.create_url, self._payload()).status_code, 403)
        draft = self._create_draft()
        confirm_url = reverse(
            'timekeeper_brigade_phase_calendar_confirm', args=[draft.pk],
        )
        self.assertEqual(client.post(confirm_url).status_code, 403)
        page = client.get(self._selection_url())
        token = page.cookies['csrftoken'].value
        self.assertEqual(client.post(
            confirm_url,
            {'csrfmiddlewaretoken': token},
        ).status_code, 302)

    def test_controlled_errors_are_safe_and_atomic(self):
        error = BrigadePhaseCalendarError(
            'Внутренний секрет traceback',
            code='shifts.brigade_phase.internal_secret',
        )
        with patch(
            'rotations.views.create_watch_period_brigade_phase_draft',
            side_effect=error,
        ):
            response = self.client.post(self.create_url, self._payload(), follow=True)
        self.assertContains(response, 'Не удалось создать версию календаря')
        self.assertNotContains(response, 'internal_secret')
        self.assertNotContains(response, 'Внутренний секрет')
        self.assertEqual(WatchPeriodBrigadePhaseVersion._base_manager.count(), 0)

    def test_unexpected_programming_error_is_not_masked(self):
        with patch(
            'rotations.views.create_watch_period_brigade_phase_draft',
            side_effect=RuntimeError('programming failure'),
        ):
            with self.assertRaisesMessage(RuntimeError, 'programming failure'):
                self.client.post(self.create_url, self._payload())

    def test_html_shows_safe_source_and_no_secret_provenance(self):
        draft = self._create_draft()
        response = self.client.get(self._selection_url())
        self.assertContains(response, f'№ {draft.version_number}')
        self.assertContains(response, 'Приказ № 17')
        self.assertContains(response, 'График № 12/1')
        html = response.content.decode('utf-8')
        for forbidden in (
            self.order_sha,
            self.schedule_sha,
            'actor_access',
            'employee_access',
            'source_snapshot',
            'source_fingerprint',
            'order_document_sha256',
            'schedule_document_sha256',
            'shifts.brigade_phase.',
        ):
            self.assertNotIn(forbidden, html)

    def test_versions_render_with_confirm_only_for_draft(self):
        draft = self._create_draft()
        response = self.client.get(self._selection_url())
        confirm_url = reverse(
            'timekeeper_brigade_phase_calendar_confirm', args=[draft.pk],
        )
        self.assertContains(response, confirm_url)
        self.client.post(confirm_url)
        confirmed = self.client.get(self._selection_url())
        self.assertNotContains(confirmed, confirm_url)
        self.assertContains(confirmed, 'Действующая версия')
        self.assertContains(confirmed, 'Утверждена')

    def test_periods_are_stably_ordered_and_unsupported_schedule_hidden(self):
        response = self.client.get(self._selection_url())
        content = response.content.decode('utf-8')
        self.assertLess(content.index(self.period.name), content.index(self.later_period.name))
        self.assertNotContains(response, self.unsupported_schedule.name)

    def test_existing_arrival_roster_page_remains_available(self):
        response = self.client.get(reverse('arrival_roster_index'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Перевахта и состав заезда')
