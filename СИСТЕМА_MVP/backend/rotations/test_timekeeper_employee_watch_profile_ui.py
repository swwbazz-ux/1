from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from shifts.models import WatchPeriod
from users.models import Employee, EmployeeAccess, Role, WatchComposition, WorkSchedule

from .employee_watch_profile_changes import (
    EmployeeWatchProfileChangeError,
    apply_employee_watch_profile_change,
    create_employee_watch_profile_change_draft,
    resolve_employee_watch_profile,
)
from .models import EmployeeWatchProfileChange


class TimekeeperEmployeeWatchProfileUiTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.timekeeper_role = Role.objects.get(code='timekeeper')
        self.other_role, _created = Role.objects.get_or_create(
            code='watch-profile-ui-other',
            defaults={'name': 'Другая роль профиля', 'is_active': True},
        )
        self.admin_role, _created = Role.objects.get_or_create(
            code='admin',
            defaults={'name': 'Администратор', 'is_active': True},
        )
        self.actor = Employee.objects.create(
            full_name='Табельщик профилей',
            phone='+7 900 000-00-01',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.actor,
            role=self.timekeeper_role,
            access_code='WATCH-PROFILE-UI-SECRET',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.other_employee = Employee.objects.create(
            full_name='Сотрудник другой роли профиля',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.other_access = EmployeeAccess.objects.create(
            employee=self.other_employee,
            role=self.other_role,
            access_code='WATCH-PROFILE-UI-OTHER',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.other_employee,
            role=self.admin_role,
            access_code='WATCH-PROFILE-UI-ADMIN',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.old_schedule = WorkSchedule.objects.create(
            code='watch-profile-ui-old',
            name='Прежний график интерфейса',
            brigade_count=2,
            is_active=True,
        )
        self.new_schedule = WorkSchedule.objects.create(
            code='watch-profile-ui-new',
            name='Новый график интерфейса',
            brigade_count=4,
            is_active=True,
        )
        self.no_brigade_schedule = WorkSchedule.objects.create(
            code='watch-profile-ui-no-brigade',
            name='График без бригад интерфейса',
            brigade_count=0,
            is_active=True,
        )
        self.old_composition = WatchComposition.objects.create(
            code='watch-profile-ui-old-composition',
            name='Прежний состав интерфейса',
            is_active=True,
        )
        self.new_composition = WatchComposition.objects.create(
            code='watch-profile-ui-new-composition',
            name='Новый состав интерфейса',
            is_active=True,
        )
        self.forged_composition = WatchComposition.objects.create(
            code='watch-profile-ui-forged-composition',
            name='Подложный состав интерфейса',
            is_active=True,
        )
        self.employee = Employee.objects.create(
            full_name='Сотрудник изменения графика',
            phone='+7 999 123-45-67',
            status=Employee.Status.ACTIVE,
            is_active=True,
            work_schedule=self.old_schedule,
            brigade_number=1,
            watch_composition=self.old_composition,
        )
        self.period = WatchPeriod.objects.create(
            name='Будущий период интерфейса',
            watch_composition=self.new_composition,
            starts_on=self.today + timedelta(days=30),
            ends_on=self.today + timedelta(days=59),
            is_active=True,
        )
        self.later_period = WatchPeriod.objects.create(
            name='Следующий будущий период интерфейса',
            watch_composition=self.new_composition,
            starts_on=self.today + timedelta(days=60),
            ends_on=self.today + timedelta(days=89),
            is_active=True,
        )
        self.current_period = WatchPeriod.objects.create(
            name='Текущий период интерфейса',
            watch_composition=self.new_composition,
            starts_on=self.today,
            ends_on=self.today + timedelta(days=29),
            is_active=True,
        )
        self.past_period = WatchPeriod.objects.create(
            name='Прошедший период интерфейса',
            watch_composition=self.new_composition,
            starts_on=self.today - timedelta(days=30),
            ends_on=self.today - timedelta(days=1),
            is_active=True,
        )
        self.page_url = reverse('timekeeper_employee_watch_profiles')
        self.create_url = reverse('timekeeper_employee_watch_profile_create')
        self._login()

    def _login(self, access=None, client=None):
        client = client or self.client
        session = client.session
        session['employee_access_id'] = (access or self.access).pk
        session.save()
        return client

    def _selection_url(self, *, employee=None, period=None):
        employee = employee or self.employee
        period = period or self.period
        return f'{self.page_url}?employee={employee.pk}&watch_period={period.pk}'

    def _payload(self, **overrides):
        payload = {
            'employee_id': self.employee.pk,
            'watch_period_id': self.period.pk,
            'new_work_schedule_id': self.new_schedule.pk,
            'new_brigade_number': 2,
            'basis_kind': EmployeeWatchProfileChange.BasisKind.EMPLOYEE_APPLICATION,
            'basis_number': 'Заявление № 15',
            'basis_date': self.today.isoformat(),
            'basis': 'Прошу изменить график работы с будущего периода.',
        }
        payload.update(overrides)
        return payload

    def _create_draft(self, **overrides):
        payload = self._payload(**overrides)
        return create_employee_watch_profile_change_draft(
            employee_id=int(payload['employee_id']),
            effective_watch_period_id=int(payload['watch_period_id']),
            new_work_schedule_id=int(payload['new_work_schedule_id']),
            new_brigade_number=(
                int(payload['new_brigade_number'])
                if payload.get('new_brigade_number') not in (None, '')
                else None
            ),
            new_watch_composition_id=self.new_composition.pk,
            basis_kind=payload['basis_kind'],
            basis_number=payload['basis_number'],
            basis_date=payload['basis_date'],
            basis=payload['basis'],
            actor_access_id=self.access.pk,
        )

    def test_exact_timekeeper_get_is_read_only_and_uses_effective_resolver(self):
        before = EmployeeWatchProfileChange._base_manager.count()
        with patch(
            'rotations.views.resolve_employee_watch_profile',
            wraps=resolve_employee_watch_profile,
        ) as resolver:
            response = self.client.get(self._selection_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Графики и вахты сотрудников')
        resolver.assert_called_once_with(
            employee_id=self.employee.pk,
            watch_period_id=self.period.pk,
        )
        self.assertEqual(EmployeeWatchProfileChange._base_manager.count(), before)
        self.client.get(self._selection_url())
        self.assertEqual(EmployeeWatchProfileChange._base_manager.count(), before)

    def test_missing_inactive_blocked_wrong_role_admin_and_inactive_employee_are_closed(self):
        cases = [Client(), self._login(self.other_access, Client()), self._login(self.admin_access, Client())]
        inactive_access = EmployeeAccess.objects.create(
            employee=self.actor,
            role=self.timekeeper_role,
            access_code='WATCH-PROFILE-UI-INACTIVE',
            status=EmployeeAccess.Status.DEACTIVATED,
            is_active=False,
        )
        blocked_access = EmployeeAccess.objects.create(
            employee=self.actor,
            role=self.timekeeper_role,
            access_code='WATCH-PROFILE-UI-BLOCKED',
            status=EmployeeAccess.Status.BLOCKED,
            is_active=True,
        )
        inactive_actor = Employee.objects.create(
            full_name='Неактивный табельщик профилей',
            status=Employee.Status.DEACTIVATED,
            is_active=False,
        )
        inactive_actor_access = EmployeeAccess.objects.create(
            employee=inactive_actor,
            role=self.timekeeper_role,
            access_code='WATCH-PROFILE-UI-INACTIVE-ACTOR',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cases.extend((
            self._login(inactive_access, Client()),
            self._login(blocked_access, Client()),
            self._login(inactive_actor_access, Client()),
        ))
        for client in cases:
            with self.subTest(session=client.session.session_key):
                self.assertEqual(client.get(self.page_url).status_code, 302)

    def test_only_future_periods_and_active_employees_are_rendered(self):
        inactive_employee = Employee.objects.create(
            full_name='Скрытый неактивный сотрудник',
            status=Employee.Status.ARCHIVED,
            is_active=False,
        )
        response = self.client.get(self._selection_url())
        self.assertContains(response, self.period.name)
        self.assertContains(response, self.later_period.name)
        self.assertNotContains(response, self.current_period.name)
        self.assertNotContains(response, self.past_period.name)
        self.assertNotContains(response, inactive_employee.full_name)

    @patch('rotations.views.create_employee_watch_profile_change_draft')
    def test_create_uses_session_actor_period_composition_and_ignores_forged_fields(self, command):
        response = self.client.post(self.create_url, self._payload(
            actor_access_id=self.other_access.pk,
            access_id=self.other_access.pk,
            status='applied',
            version_number=99,
            source_fingerprint='f' * 64,
            source_snapshot='forged',
            new_watch_composition_id=self.forged_composition.pk,
            applied_at='2000-01-01T00:00:00Z',
        ))
        self.assertEqual(response.status_code, 302)
        command.assert_called_once_with(
            employee_id=self.employee.pk,
            effective_watch_period_id=self.period.pk,
            new_work_schedule_id=self.new_schedule.pk,
            new_brigade_number=2,
            new_watch_composition_id=self.period.watch_composition_id,
            basis_kind=EmployeeWatchProfileChange.BasisKind.EMPLOYEE_APPLICATION,
            basis_number='Заявление № 15',
            basis_date=self.today,
            basis='Прошу изменить график работы с будущего периода.',
            actor_access_id=self.access.pk,
        )

    def test_create_and_identical_repeat_are_idempotent(self):
        first_response = self.client.post(self.create_url, self._payload())
        self.assertEqual(first_response.status_code, 302)
        draft = EmployeeWatchProfileChange._base_manager.get()
        created_at = draft.created_at
        second_response = self.client.post(self.create_url, self._payload())
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(EmployeeWatchProfileChange._base_manager.count(), 1)
        draft.refresh_from_db()
        self.assertEqual(draft.created_at, created_at)
        self.assertEqual(draft.created_by_access_id, self.access.pk)

    @patch('rotations.views.apply_employee_watch_profile_change')
    def test_apply_uses_only_url_change_and_session_actor(self, command):
        draft = self._create_draft()
        command.return_value = SimpleNamespace(
            employee_id=self.employee.pk,
            effective_watch_period_id=self.period.pk,
        )
        other_id = draft.pk + 1000
        response = self.client.post(
            reverse('timekeeper_employee_watch_profile_apply', args=[draft.pk]),
            {
                'change_id': other_id,
                'actor_access_id': self.other_access.pk,
                'status': 'superseded',
                'applied_at': '2000-01-01T00:00:00Z',
            },
        )
        self.assertEqual(response.status_code, 302)
        command.assert_called_once_with(
            change_id=draft.pk,
            actor_access_id=self.access.pk,
        )

    def test_apply_records_exact_access_is_idempotent_and_does_not_rewrite_employee(self):
        baseline = (
            self.employee.work_schedule_id,
            self.employee.brigade_number,
            self.employee.watch_composition_id,
        )
        draft = self._create_draft()
        apply_url = reverse('timekeeper_employee_watch_profile_apply', args=[draft.pk])
        self.assertEqual(self.client.post(apply_url).status_code, 302)
        draft.refresh_from_db()
        applied_at = draft.applied_at
        self.assertEqual(draft.status, EmployeeWatchProfileChange.Status.APPLIED)
        self.assertEqual(draft.applied_by_access_id, self.access.pk)
        self.assertEqual(self.client.post(apply_url).status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.applied_at, applied_at)
        self.employee.refresh_from_db()
        self.assertEqual((
            self.employee.work_schedule_id,
            self.employee.brigade_number,
            self.employee.watch_composition_id,
        ), baseline)

    def test_current_and_past_periods_are_blocked_with_safe_message(self):
        for period in (self.current_period, self.past_period):
            with self.subTest(period=period.pk):
                response = self.client.post(
                    self.create_url,
                    self._payload(watch_period_id=period.pk),
                    follow=True,
                )
                self.assertContains(response, 'только для будущего периода вахты')
        self.assertEqual(EmployeeWatchProfileChange._base_manager.count(), 0)

    def test_invalid_brigade_and_incomplete_basis_are_safe(self):
        invalid_brigade = self.client.post(
            self.create_url,
            self._payload(new_brigade_number=99),
            follow=True,
        )
        self.assertContains(invalid_brigade, 'Номер бригады не соответствует')
        incomplete_basis = self.client.post(
            self.create_url,
            self._payload(basis_number='', basis=''),
            follow=True,
        )
        self.assertContains(incomplete_basis, 'Заполните сотрудника, период, новый график')
        self.assertEqual(EmployeeWatchProfileChange._base_manager.count(), 0)

    def test_controlled_error_is_safe_and_unexpected_error_is_not_masked(self):
        with patch(
            'rotations.views.create_employee_watch_profile_change_draft',
            side_effect=EmployeeWatchProfileChangeError(
                'Внутренний секрет',
                code='employee_watch_profile.internal_secret',
            ),
        ):
            response = self.client.post(self.create_url, self._payload(), follow=True)
        self.assertContains(response, 'Не удалось создать черновик изменения')
        self.assertNotContains(response, 'internal_secret')
        self.assertNotContains(response, 'Внутренний секрет')
        with patch(
            'rotations.views.create_employee_watch_profile_change_draft',
            side_effect=RuntimeError('programming failure'),
        ):
            with self.assertRaisesMessage(RuntimeError, 'programming failure'):
                self.client.post(self.create_url, self._payload())

    def test_command_endpoints_reject_get_and_csrf_is_enforced(self):
        self.assertEqual(self.client.get(self.create_url).status_code, 405)
        self.assertEqual(self.client.get(
            reverse('timekeeper_employee_watch_profile_apply', args=[1]),
        ).status_code, 405)
        csrf_client = self._login(client=Client(enforce_csrf_checks=True))
        self.assertEqual(
            csrf_client.post(self.create_url, self._payload()).status_code,
            403,
        )

    def test_html_has_safe_fields_navigation_and_no_secret_provenance(self):
        draft = self._create_draft()
        response = self.client.get(self._selection_url())
        html = response.content.decode('utf-8')
        self.assertContains(response, 'Календарь фаз бригад')
        self.assertContains(response, reverse('timekeeper_brigade_phase_calendar'))
        self.assertContains(
            self.client.get(reverse('timekeeper_brigade_phase_calendar')),
            self.page_url,
        )
        for forbidden in (
            self.employee.phone,
            self.access.access_code,
            draft.source_fingerprint,
            'source_snapshot',
            'source_fingerprint',
            'actor_access_id',
            'access_id',
            'name="change_id"',
            'new_watch_composition_id',
            'version_number',
            'PIN',
        ):
            self.assertNotIn(forbidden, html)

    def test_history_renders_draft_applied_and_superseded_with_apply_only_for_draft(self):
        first = self._create_draft()
        apply_employee_watch_profile_change(
            change_id=first.pk,
            actor_access_id=self.access.pk,
        )
        replacement = self._create_draft(
            new_brigade_number=3,
            basis_number='Заявление № 16',
        )
        apply_employee_watch_profile_change(
            change_id=replacement.pk,
            actor_access_id=self.access.pk,
        )
        pending = self._create_draft(
            new_work_schedule_id=self.no_brigade_schedule.pk,
            new_brigade_number='',
            basis_number='Заявление № 17',
        )
        response = self.client.get(self._selection_url())
        self.assertContains(response, 'Черновик')
        self.assertContains(response, 'Применена')
        self.assertContains(response, 'Заменена')
        pending_url = reverse(
            'timekeeper_employee_watch_profile_apply',
            args=[pending.pk],
        )
        self.assertContains(response, pending_url)
        self.assertNotContains(response, reverse(
            'timekeeper_employee_watch_profile_apply',
            args=[first.pk],
        ))
        self.assertNotContains(response, reverse(
            'timekeeper_employee_watch_profile_apply',
            args=[replacement.pk],
        ))
