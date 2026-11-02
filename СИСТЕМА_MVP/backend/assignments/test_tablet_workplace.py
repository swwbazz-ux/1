from django.test import Client, TestCase
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from shifts.models import EmployeeShift
from users.models import Employee, EmployeeAccess, Role


class TabletWorkplaceTests(TestCase):
    def setUp(self):
        role, _ = Role.objects.get_or_create(code='mining_master', defaults={'name': 'Горный мастер'})
        self.employee = Employee.objects.create(full_name='Иванов Иван Иванович',
                                               phone='79001234567', status=Employee.Status.ACTIVE)
        self.access = EmployeeAccess.objects.create(employee=self.employee, role=role,
            access_code='246824', status=EmployeeAccess.Status.ACTIVATED)
        self.url = reverse('mining_master_tablet')

    def login(self, client=None):
        return (client or self.client).post(reverse('mining_master_tablet_login'),
            {'phone': self.employee.phone, 'access_code': '246824'})

    def own_open(self):
        return EmployeeShift.objects.filter(employee=self.employee, workplace_code='mining_master',
                                            closed_at__isnull=True)

    def start(self, **data):
        return self.client.post(self.url, {'action': 'start_shift', **data})

    def test_unauthenticated_tablet_requires_personal_entry(self):
        self.assertRedirects(self.client.get(self.url), reverse('mining_master_tablet_login'),
                             fetch_redirect_response=False)

    def enter_to_start(self, client=None, **data):
        return (client or self.client).post(reverse('mining_master_tablet_login'), {
            'phone': '900 123 45 67', 'access_code': '246824', 'intent': 'start_shift', **data,
        })

    def test_landing_and_opening_credentials_do_not_open_a_shift(self):
        url = reverse('mining_master_tablet_login')
        self.assertFalse(self.client.get(url).context['show_credentials'])
        response = self.client.get(url, {'intent': 'start_shift'})
        self.assertTrue(response.context['show_credentials'])
        self.assertTrue(response.context['shift_start_intent'])
        self.assertNotIn('employee_access_id', self.client.session)
        # An old sign-in form cannot start a shift from a query string alone.
        self.client.post(url + '?intent=start_shift', {'phone': self.employee.phone, 'access_code': '246824'})
        self.assertFalse(self.own_open().exists())

    def test_shift_entry_starts_once_and_resumes_same_row_after_reboot(self):
        self.assertRedirects(self.enter_to_start(), self.url, fetch_redirect_response=False)
        original = self.own_open().get()
        self.assertEqual(original.opened_by_id, self.employee.pk)
        self.assertRedirects(self.enter_to_start(), self.url, fetch_redirect_response=False)
        self.assertRedirects(self.enter_to_start(Client()), self.url, fetch_redirect_response=False)
        actual = self.own_open().get()
        self.assertEqual((actual.pk, actual.opened_at), (original.pk, original.opened_at))
        self.assertNotIn('mining_master_tablet_shift_auth', self.client.session)

    def test_start_and_resume_do_not_announce_a_redundant_status(self):
        self.login()
        for response in (self.start(), self.enter_to_start()):
            self.assertFalse(list(get_messages(response.wsgi_request)))

    def test_unverified_pin_stays_on_start_form_and_cannot_resume_or_start(self):
        self.assertContains(self.enter_to_start(access_code='888888'), 'Проверьте телефон и PIN')
        self.assertFalse(self.own_open().exists())
        self.enter_to_start()
        other_client = Client()
        response = self.enter_to_start(other_client, access_code='888888')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['shift_start_intent'])
        self.assertNotIn('employee_access_id', other_client.session)
        self.assertEqual(self.own_open().count(), 1)

    def test_shift_entry_neither_confirms_a_different_role_nor_closes_it(self):
        other_shift = EmployeeShift.objects.create(employee=self.employee, opened_by=self.employee,
            workplace_code='dispatcher', shift_type='day', opened_at=timezone.now())
        response = self.enter_to_start(close_other_role_shift='1', tablet_handover_token='forged')
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        other_shift.refresh_from_db()
        self.assertIsNone(other_shift.closed_at)
        self.assertFalse(self.own_open().exists())
        self.assertTrue(any('другой роли' in str(m) for m in get_messages(response.wsgi_request)))
        header = self.client.get(self.url).context['dispatcher_header']
        self.start(tablet_handover_token=header['tablet_handover_token'],
                   **{header['other_role_shift_prompt']['field']: '1'})
        other_shift.refresh_from_db()
        self.assertIsNotNone(other_shift.closed_at)
        self.assertEqual(self.own_open().count(), 1)

    def test_shift_entry_reports_another_master_without_taking_over_their_shift(self):
        previous = Employee.objects.create(full_name='Предыдущий мастер', status=Employee.Status.ACTIVE)
        shift = EmployeeShift.objects.create(employee=previous, opened_by=previous, workplace_code='mining_master',
                                            shift_type='day', opened_at=timezone.now())
        response = self.enter_to_start()
        self.assertFalse(self.own_open().exists())
        self.assertTrue(any('Предыдущий горный мастер' in str(m) for m in get_messages(response.wsgi_request)))
        shift.refresh_from_db()
        self.assertIsNone(shift.closed_at)

    def test_verified_entry_starts_real_master_shift_once_without_retyping(self):
        self.login()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['dispatcher_header']['requires_shift_reauth'])
        self.start()
        shift = self.own_open().get()
        self.assertEqual(shift.opened_by_id, self.employee.pk)
        self.assertFalse(EmployeeShift.objects.filter(workplace_code='dispatcher').exists())
        self.client.post(self.url, {'action': 'end_shift'})
        self.start(device_kind='personal')
        self.assertFalse(self.own_open().exists())
        self.assertTrue(self.client.get(self.url).context['dispatcher_header']['requires_shift_reauth'])

    def test_next_shift_accepts_phone_pin_and_stays_shared(self):
        self.login(); self.start()
        self.client.post(self.url, {'action': 'end_shift'})
        self.start(reauth_phone=self.employee.phone, reauth_access_code='246824', device_kind='personal')
        self.assertEqual(self.own_open().count(), 1)
        self.assertEqual(self.client.get(self.url).context['dispatcher_header']['session_device_kind'], 'shared')

    def test_wrong_pin_cannot_use_saved_identity_after_close(self):
        self.login(); self.start()
        self.client.post(self.url, {'action': 'end_shift'})
        self.start(reauth_phone=self.employee.phone, reauth_access_code='888888')
        self.assertFalse(self.own_open().exists())

    def test_explicit_other_employee_credentials_override_first_entry_proof(self):
        other = Employee.objects.create(full_name='Петров Пётр Петрович', phone='79001234568',
                                         status=Employee.Status.ACTIVE)
        EmployeeAccess.objects.create(employee=other, role=self.access.role, access_code='864286',
                                       status=EmployeeAccess.Status.ACTIVATED)
        self.login()
        self.start(reauth_phone=other.phone, reauth_access_code='864286')
        shift = EmployeeShift.objects.get(closed_at__isnull=True)
        self.assertEqual((shift.employee_id, shift.opened_by_id), (other.pk, other.pk))

    def test_other_master_shift_is_readonly_and_cannot_be_closed(self):
        other = Employee.objects.create(full_name='Предыдущий мастер', status=Employee.Status.ACTIVE)
        shift = EmployeeShift.objects.create(employee=other, opened_by=other,
            workplace_code='mining_master', shift_type='day', opened_at=timezone.now())
        self.login()
        response = self.client.get(self.url)
        self.assertContains(response, 'data-dispatcher-shift-open="false"')
        self.assertFalse(response.context['dispatcher_header']['shift_is_open'])
        self.start()
        self.assertFalse(self.own_open().exists())
        self.client.post(self.url, {'action': 'end_shift'})
        shift.refresh_from_db(); self.assertIsNone(shift.closed_at)

    def test_scoped_urls_and_tablet_geometry_preserve_legacy_reverse(self):
        original = reverse('dispatcher_control')
        self.login()
        response = self.client.get(self.url)
        self.assertContains(response, 'data-app-role-code="mining_master"')
        self.assertNotContains(response, 'dispatcher-control-screen mining-master-mobile-screen')
        self.assertContains(response, 'href="/mining-master/tablet/dispatcher/reports/"')
        self.assertContains(response, 'data-dispatcher-move-url="/mining-master/tablet/dispatcher/control/excavator/move/"')
        self.assertEqual(reverse('dispatcher_control'), original)
        self.assertEqual(original, '/dispatcher/control/')

    def test_csrf_is_required_even_with_fresh_login_proof(self):
        self.login()
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.cookies = self.client.cookies
        self.assertEqual(csrf_client.post(self.url, {'action': 'start_shift'}).status_code, 403)
        self.assertFalse(self.own_open().exists())

    def test_closed_dispatcher_does_not_block_master_shift(self):
        self.login(); self.start()
        self.assertTrue(self.own_open().exists())
        self.assertTrue(self.client.get(self.url).context['dispatcher_header']['shift_is_open'])

    def test_disabled_access_cannot_reuse_entry_proof(self):
        self.login()
        EmployeeAccess.objects.filter(pk=self.access.pk).update(is_active=False)
        self.start()
        self.assertFalse(self.own_open().exists())

    def test_other_role_handover_needs_confirmation_for_verified_employee(self):
        other = EmployeeShift.objects.create(employee=self.employee, opened_by=self.employee,
            workplace_code='dispatcher', shift_type='day', opened_at=timezone.now())
        self.login()
        # Even valid credentials cannot silently close a shift using a forged flag.
        self.start(reauth_phone=self.employee.phone, reauth_access_code='246824', close_other_role_shift='1')
        other.refresh_from_db(); self.assertIsNone(other.closed_at)
        response = self.client.get(self.url)
        header = response.context['dispatcher_header']
        self.assertFalse(header['requires_shift_reauth'])
        token = header['tablet_handover_token']
        self.start(tablet_handover_token=token, **{header['other_role_shift_prompt']['field']: '1'})
        other.refresh_from_db(); self.assertIsNotNone(other.closed_at)
        self.assertEqual(self.own_open().count(), 1)

    def test_logout_is_post_only_and_never_closes_shift(self):
        self.login(); self.start()
        url = reverse('mining_master_tablet_logout')
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertRedirects(self.client.post(url), reverse('mining_master_tablet_login'), fetch_redirect_response=False)
        self.assertTrue(self.own_open().exists())
        self.assertNotIn('employee_access_id', self.client.session)

    def test_legacy_personal_route_cannot_bypass_next_shift_identity(self):
        self.login(); self.start()
        self.client.post(self.url, {'action': 'end_shift'})
        response = self.client.post(reverse('mining_master_assignments'),
                                    {'action': 'start_shift', 'device_kind': 'personal'})
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertFalse(self.own_open().exists())
        self.assertRedirects(self.client.get(reverse('login')), reverse('mining_master_tablet_login'),
                             fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse('activate_access')), reverse('mining_master_tablet_activate'),
                             fetch_redirect_response=False)
