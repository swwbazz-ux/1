from django.test import TestCase
from django.urls import reverse

from shifts.models import EmployeeShift
from users.models import Employee, EmployeeAccess, Role


class DispatcherSharedShiftStartTests(TestCase):
    def setUp(self):
        self.dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.current_dispatcher = Employee.objects.create(
            full_name='Дежурный диспетчер',
            phone='79000000500',
            is_active=True,
        )
        self.current_access = EmployeeAccess.objects.create(
            employee=self.current_dispatcher,
            role=self.dispatcher_role,
            access_code='500000',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        session = self.client.session
        session['employee_access_id'] = self.current_access.id
        session['device_kind'] = 'shared'
        session.save()

    def test_shared_desktop_shows_dispatcher_reauth_popup(self):
        response = self.client.get(reverse('dispatcher_control'))

        self.assertContains(response, 'data-shared-shift-login-open')
        self.assertContains(response, 'Вход Горного диспетчера')
        self.assertContains(response, 'код диспетчера')
        self.assertContains(response, 'name="reauth_phone"')
        self.assertContains(response, 'placeholder="999-000-00-00"')
        self.assertContains(response, 'name="reauth_access_code"')
        self.assertContains(response, 'placeholder="00-00-00"')
        self.assertContains(response, 'maxlength="8"')
        self.assertContains(response, 'pattern="[0-9]{2}-[0-9]{2}-[0-9]{2}"')
        self.assertContains(response, 'name="device_kind"')
        self.assertContains(response, 'value="shared"')

    def test_dispatcher_control_uses_reauth_popup_even_if_session_was_personal(self):
        session = self.client.session
        session['device_kind'] = 'personal'
        session.save()

        response = self.client.get(reverse('dispatcher_control'))

        self.assertContains(response, 'data-shared-shift-login-open')
        self.assertNotContains(response, 'data-confirm="Начать смену горного диспетчера?"')

    def test_shared_desktop_blocks_direct_start_without_reauth(self):
        response = self.client.post(reverse('dispatcher_toggle_shift'), {'shift_action': 'start'})

        self.assertRedirects(response, reverse('dispatcher_control'))
        self.assertFalse(
            EmployeeShift.objects
            .filter(employee=self.current_dispatcher, closed_at__isnull=True)
            .exists()
        )

    def test_shared_desktop_can_start_shift_with_dispatcher_credentials(self):
        next_dispatcher = Employee.objects.create(
            full_name='Сменный диспетчер',
            phone='79000000544',
            is_active=True,
        )
        next_access = EmployeeAccess.objects.create(
            employee=next_dispatcher,
            role=self.dispatcher_role,
            access_code='544544',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )

        response = self.client.post(
            reverse('dispatcher_toggle_shift'),
            {
                'shift_action': 'start',
                'reauth_phone': '900-000-05-44',
                'reauth_access_code': '54-45-44',
                'device_kind': 'shared',
            },
        )

        self.assertRedirects(response, reverse('dispatcher_control'))
        self.assertFalse(
            EmployeeShift.objects
            .filter(employee=self.current_dispatcher, closed_at__isnull=True)
            .exists()
        )
        self.assertTrue(
            EmployeeShift.objects
            .filter(employee=next_dispatcher, closed_at__isnull=True)
            .exists()
        )
        self.assertEqual(self.client.session['employee_access_id'], next_access.id)
        self.assertEqual(self.client.session['device_kind'], 'shared')
