"""Граница и поведение команды собственной смены Диспетчера."""

import inspect
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import resolve, reverse

from trips import views as trips_views
from shifts.models import EmployeeShift
from users.models import Employee, EmployeeAccess, Role


class DispatcherShiftCommandBoundaryTests(SimpleTestCase):
    def test_toggle_url_keeps_public_views_facade(self):
        match = resolve(reverse('dispatcher_toggle_shift'))

        self.assertIs(match.func, trips_views.dispatcher_toggle_shift_view)

    def test_public_toggle_view_is_thin_shift_command_facade(self):
        source = inspect.getsource(trips_views.dispatcher_toggle_shift_view)

        self.assertIn('_execute_dispatcher_toggle_shift(', source)
        self.assertIn(
            'lock_mutation_access=lock_dispatcher_mutation_access',
            source,
        )
        self.assertIn(
            'shared_start_authenticator='
            'authenticate_dispatcher_shared_shift_start',
            source,
        )
        self.assertNotIn('EmployeeAccess.objects', source)
        self.assertNotIn('EmployeeShift.objects', source)
        self.assertNotIn('open_dispatcher_shift', source)
        self.assertNotIn('close_dispatcher_shift', source)


class DispatcherShiftFacadeDelegationTests(TestCase):
    def test_toggle_facade_injects_views_active_role_seam(self):
        request = RequestFactory().post('/dispatcher/shift/toggle/')
        expected = HttpResponse(status=302)
        with patch.object(
            trips_views,
            '_execute_dispatcher_toggle_shift',
            return_value=expected,
        ) as execute:
            response = trips_views.dispatcher_toggle_shift_view(request)

        self.assertIs(response, expected)
        execute.assert_called_once_with(
            request,
            lock_mutation_access=trips_views.lock_dispatcher_mutation_access,
            shared_start_authenticator=(
                trips_views.authenticate_dispatcher_shared_shift_start
            ),
        )


class DispatcherShiftCommandBehaviorTests(TestCase):
    def setUp(self):
        self.dispatcher_role = Role.objects.create(
            code='dispatcher',
            name='Диспетчер',
        )
        self.dispatcher = Employee.objects.create(
            full_name='Диспетчер смены',
            phone='79000000827',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=self.dispatcher_role,
            access_code='827827',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        session = self.client.session
        session['employee_access_id'] = self.access.id
        session['device_kind'] = 'personal'
        session.save()
        self.url = reverse('dispatcher_toggle_shift')

    @staticmethod
    def messages_text(response):
        return ' | '.join(
            str(message)
            for message in get_messages(response.wsgi_request)
        )

    def post_action(self, action, **extra):
        return self.client.post(
            self.url,
            {'shift_action': action, **extra},
        )

    def test_start_duplicate_end_and_missing_shift_keep_messages(self):
        response = self.post_action('start')
        self.assertIn('Смена горного диспетчера открыта.', self.messages_text(response))
        shift = EmployeeShift.objects.get(employee=self.dispatcher)
        self.assertEqual(shift.workplace_code, 'dispatcher')
        self.assertEqual(shift.opened_by, self.dispatcher)

        response = self.post_action('start')
        self.assertIn(
            'Смена горного диспетчера уже открыта.',
            self.messages_text(response),
        )
        self.assertEqual(EmployeeShift.objects.filter(employee=self.dispatcher).count(), 1)

        response = self.post_action('end')
        self.assertIn(
            'Смена горного диспетчера завершена.',
            self.messages_text(response),
        )
        shift.refresh_from_db()
        self.assertIsNotNone(shift.closed_at)
        self.assertEqual(shift.closed_by, self.dispatcher)

        response = self.post_action('end')
        self.assertIn(
            'Открытая смена горного диспетчера не найдена.',
            self.messages_text(response),
        )

    def test_get_preserves_filter_and_unknown_action_does_not_mutate(self):
        response = self.client.get(self.url, {'truck': '17'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response['Location'],
            f"{reverse('dispatcher_control')}?truck=17",
        )
        self.assertFalse(EmployeeShift.objects.exists())

        response = self.post_action('unknown')
        self.assertIn(
            'Неизвестное действие со сменой диспетчера.',
            self.messages_text(response),
        )
        self.assertFalse(EmployeeShift.objects.exists())

    def test_shared_start_requires_valid_dispatcher_credentials(self):
        session = self.client.session
        session['device_kind'] = 'shared'
        session.save()

        response = self.post_action('start')
        self.assertIn(
            'Для начала смены на общем компьютере введите телефон '
            'и код горного диспетчера.',
            self.messages_text(response),
        )
        self.assertFalse(EmployeeShift.objects.exists())

        response = self.post_action(
            'start',
            reauth_phone='900-000-08-27',
            reauth_access_code='00-00-00',
            device_kind='shared',
        )
        self.assertIn(
            'Телефон или код горного диспетчера указаны неверно.',
            self.messages_text(response),
        )
        self.assertFalse(EmployeeShift.objects.exists())

    def test_manager_role_cannot_toggle_dispatcher_shift(self):
        manager_role = Role.objects.create(code='manager', name='Руководитель')
        self.access.role = manager_role
        self.access.save(update_fields=['role'])

        response = self.post_action('start')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('role_home'))
        self.assertFalse(EmployeeShift.objects.exists())
