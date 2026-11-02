from datetime import timedelta
from unittest.mock import patch

from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import include, path
from django.utils import timezone

from shifts.models import EmployeeShift

from .active_role import ACTIVE_ROLE_GENERATION_SESSION_KEY
from .live_monitor import OBSERVER_MODE_CONTROL, create_observer_token
from .mining_master_tablet_auth import (
    ACTIVATION_TTL,
    PENDING_KEY,
    SHIFT_AUTH_KEY,
    SHIFT_AUTH_TTL,
    activate_view,
    consume_tablet_shift_auth,
    get_tablet_shift_auth,
    login_view,
)
from .models import Employee, EmployeeAccess, Role


urlpatterns = [
    path('mining-master/tablet/login/', login_view, name='mining_master_tablet_login'),
    path('mining-master/tablet/activate/', activate_view, name='mining_master_tablet_activate'),
    path('mining-master/tablet/', lambda request: HttpResponse('Пульт'), name='mining_master_tablet'),
    path('', include('config.urls')),
]


@override_settings(ROOT_URLCONF=__name__, ALLOWED_HOSTS=['testserver', 'localhost'])
class MiningMasterTabletAuthTests(TestCase):
    LOGIN = '/mining-master/tablet/login/'
    ACTIVATE = '/mining-master/tablet/activate/'
    CONTROL = '/mining-master/tablet/'

    def setUp(self):
        self.role, _ = Role.objects.get_or_create(code='mining_master', defaults={'name': 'Горный мастер'})
        self.employee = Employee.objects.create(
            full_name='Полевой Мастер Иванович', phone='+79001234567',
            status=Employee.Status.NOT_ACTIVATED,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.employee, role=self.role, access_code='246824',
            status=EmployeeAccess.Status.NOT_ACTIVATED, primary_code_issued_at=timezone.now(),
        )

    def login(self, **data):
        return self.client.post(self.LOGIN, {'phone': self.employee.phone, 'access_code': '246824', **data})

    def activate(self, code='864286', confirm=None):
        return self.client.post(self.ACTIVATE, {'new_access_code': code, 'confirm_access_code': confirm or code})

    def make_active(self):
        self.employee.status = Employee.Status.ACTIVE
        self.employee.save(update_fields=['status', 'updated_at'])
        self.access.status = EmployeeAccess.Status.ACTIVATED
        self.access.save(update_fields=['status'])

    def test_phone_change_between_lookup_and_lock_rejects_old_phone(self):
        self.make_active()
        from .access_auth import find_employee_access_by_credentials
        def find_then_change_phone(*args, **kwargs):
            result = find_employee_access_by_credentials(*args, **kwargs)
            Employee.objects.filter(pk=self.employee.pk).update(phone='+79001234569')
            return result
        with patch('users.mining_master_tablet_auth.find_employee_access_by_credentials', side_effect=find_then_change_phone):
            response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('employee_access_id', self.client.session)

    def proof_request(self):
        request = RequestFactory().post(self.CONTROL)
        request.session = self.client.session
        return request

    def test_first_entry_requires_issued_pin_and_binds_employee_and_role(self):
        self.assertRedirects(self.login(), self.ACTIVATE, fetch_redirect_response=False)
        pending = self.client.session[PENDING_KEY]
        self.assertEqual(pending['access_id'], self.access.pk)
        self.assertEqual(pending['employee_id'], self.employee.pk)
        self.assertEqual(pending['role'], 'mining_master')
        self.assertNotIn('246824', str(dict(self.client.session)))
        self.assertNotIn('employee_access_id', self.client.session)
        response = self.client.get(self.ACTIVATE)
        self.assertContains(response, self.employee.full_name)
        self.assertNotContains(response, 'name="phone"')
        self.assertNotContains(response, 'name="access_code"')

    def test_activation_updates_pin_and_creates_real_shared_master_session(self):
        self.login()
        self.assertRedirects(self.activate(), self.CONTROL, fetch_redirect_response=False)
        self.access.refresh_from_db()
        self.employee.refresh_from_db()
        self.assertEqual(self.access.access_code, '864286')
        self.assertEqual(self.access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertEqual(self.employee.status, Employee.Status.ACTIVE)
        self.assertEqual(self.client.session['employee_access_id'], self.access.pk)
        self.assertEqual(self.client.session['active_role_code'], 'mining_master')
        self.assertEqual(self.client.session['device_kind'], 'shared')
        self.assertNotIn(PENDING_KEY, self.client.session)
        self.assertNotIn('864286', str(dict(self.client.session)))
        self.assertEqual(get_tablet_shift_auth(self.proof_request()).pk, self.access.pk)

    def test_first_activation_finishes_only_the_verified_shift_intent(self):
        self.login(intent='start_shift')
        self.assertTrue(self.client.get(self.ACTIVATE).context['shift_start_intent'])
        self.assertTrue(self.activate(confirm='864287').context['shift_start_intent'])
        self.assertFalse(EmployeeShift.objects.filter(employee=self.employee).exists())
        self.assertRedirects(self.activate(), self.CONTROL, fetch_redirect_response=False)
        shift = EmployeeShift.objects.get(employee=self.employee)
        self.assertEqual((shift.workplace_code, shift.opened_by_id), ('mining_master', self.employee.pk))
        self.assertIsNone(shift.closed_at)
        self.assertNotIn(SHIFT_AUTH_KEY, self.client.session)
        self.assertNotIn(PENDING_KEY, self.client.session)

    def test_activation_form_cannot_add_unverified_start_intent(self):
        self.login()
        self.client.post(self.ACTIVATE, {
            'new_access_code': '864286', 'confirm_access_code': '864286', 'intent': 'start_shift',
        })
        self.assertEqual(self.client.session['employee_access_id'], self.access.pk)
        self.assertFalse(EmployeeShift.objects.filter(employee=self.employee).exists())

    def test_observer_control_cannot_start_a_shift_with_login_or_pending_activation(self):
        admin_role, _ = Role.objects.get_or_create(code='admin', defaults={'name': 'Администратор'})
        admin = Employee.objects.create(full_name='Тестовый администратор', status=Employee.Status.ACTIVE)
        actor = EmployeeAccess.objects.create(employee=admin, role=admin_role, access_code='563294',
                                             status=EmployeeAccess.Status.ACTIVATED)
        master = Employee.objects.create(full_name='Наблюдаемый мастер', status=Employee.Status.ACTIVE)
        target = EmployeeAccess.objects.create(employee=master, role=self.role, access_code='294653',
                                              status=EmployeeAccess.Status.ACTIVATED)
        token = create_observer_token(actor_access=actor, target_access=target, mode=OBSERVER_MODE_CONTROL)
        response = self.client.post(self.LOGIN, {'phone': self.employee.phone, 'access_code': '246824',
                                    'intent': 'start_shift'}, HTTP_X_OBSERVER_TOKEN=token)
        self.assertContains(response, 'Начать личную смену', status_code=403)
        self.assertNotIn(PENDING_KEY, self.client.session)
        self.login(intent='start_shift')
        response = self.client.post(self.ACTIVATE, {'new_access_code': '864286', 'confirm_access_code': '864286'},
                                    HTTP_X_OBSERVER_TOKEN=token)
        self.assertContains(response, 'Начать личную смену', status_code=403)
        self.access.refresh_from_db()
        self.assertEqual(self.access.status, EmployeeAccess.Status.NOT_ACTIVATED)
        self.assertFalse(EmployeeShift.objects.filter(employee=self.employee).exists())

    def test_activated_employee_uses_existing_pin_without_activation(self):
        self.make_active()
        self.assertRedirects(self.login(), self.CONTROL, fetch_redirect_response=False)
        self.assertNotIn(PENDING_KEY, self.client.session)
        self.assertIsNotNone(get_tablet_shift_auth(self.proof_request()))

    def test_wrong_pin_phone_and_phone_only_do_not_authenticate(self):
        for data in ({'access_code': '112233'}, {'access_code': ''}, {'phone': ''}, {'phone': '+79009876543'}):
            with self.subTest(data=data):
                response = self.login(**data)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Проверьте телефон и PIN')
                self.assertNotIn(PENDING_KEY, self.client.session)
                self.assertNotIn('employee_access_id', self.client.session)

    def test_dispatcher_access_cannot_activate_master(self):
        role, _ = Role.objects.get_or_create(code='dispatcher', defaults={'name': 'Диспетчер'})
        self.access.role = role
        self.access.save(update_fields=['role'])
        self.assertContains(self.login(), 'Проверьте телефон и PIN')
        self.assertNotIn(PENDING_KEY, self.client.session)

    def test_national_phone_with_spaces_can_start_first_activation(self):
        response = self.login(phone='900 123 45 67')
        self.assertRedirects(response, self.ACTIVATE)
        self.assertEqual(self.client.session[PENDING_KEY]['access_id'], self.access.pk)

    def test_unissued_pending_code_is_not_accepted(self):
        self.access.primary_code_issued_at = None
        self.access.save(update_fields=['primary_code_issued_at'])
        self.assertContains(self.login(), 'Проверьте телефон и PIN')
        self.assertNotIn(PENDING_KEY, self.client.session)

    def test_revoked_employee_role_and_access_are_rejected(self):
        for model, field, value in (
            (self.employee, 'is_active', False),
            (self.role, 'is_active', False),
            (self.access, 'is_active', False),
            (self.access, 'status', EmployeeAccess.Status.BLOCKED),
        ):
            with self.subTest(field=field, model=type(model).__name__):
                old = getattr(model, field)
                setattr(model, field, value)
                model.save(update_fields=[field])
                self.assertContains(self.login(), 'Проверьте телефон и PIN')
                self.assertNotIn(PENDING_KEY, self.client.session)
                setattr(model, field, old)
                model.save(update_fields=[field])

    def test_activation_without_verified_entry_is_rejected(self):
        self.assertRedirects(self.activate(), self.LOGIN, fetch_redirect_response=False)
        self.access.refresh_from_db()
        self.assertEqual(self.access.status, EmployeeAccess.Status.NOT_ACTIVATED)

    def test_weak_same_and_mismatched_pin_are_not_saved_or_echoed(self):
        self.login()
        for code, confirm in [('123456', '123456'), ('246824', '246824'), ('864286', '864287'), ('٨٦٤٢٨٦', '٨٦٤٢٨٦')]:
            with self.subTest(code=code):
                response = self.activate(code, confirm)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, f'value="{code}"')
                self.access.refresh_from_db()
                self.assertEqual(self.access.access_code, '246824')

    def test_activation_expiry_cancel_and_reissue_invalidate_pending(self):
        self.login()
        with patch('users.mining_master_tablet_auth.timezone.now', return_value=timezone.now() + timedelta(seconds=ACTIVATION_TTL + 1)):
            self.assertRedirects(self.activate(), self.LOGIN, fetch_redirect_response=False)
        self.login()
        self.assertRedirects(self.client.post(self.ACTIVATE, {'action': 'cancel'}), self.LOGIN, fetch_redirect_response=False)
        self.assertNotIn(PENDING_KEY, self.client.session)
        self.login()
        self.access.access_code = '568927'
        self.access.save(update_fields=['access_code'])
        self.assertRedirects(self.activate(), self.LOGIN, fetch_redirect_response=False)

    def test_access_revocation_between_steps_blocks_activation(self):
        self.login()
        self.role.is_active = False
        self.role.save(update_fields=['is_active'])
        self.assertRedirects(self.activate(), self.LOGIN, fetch_redirect_response=False)
        self.access.refresh_from_db()
        self.assertEqual(self.access.access_code, '246824')

    def test_already_activated_role_cannot_be_reset_via_duplicate_pending(self):
        EmployeeAccess.objects.create(
            employee=self.employee, role=self.role, access_code='568927',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        self.assertContains(self.login(), 'Проверьте телефон и PIN')
        self.assertNotIn(PENDING_KEY, self.client.session)

    def test_pending_binds_real_employee_ignores_posted_employee_and_role(self):
        other = Employee.objects.create(full_name='Другой мастер', phone='+79009999999')
        self.login()
        self.client.post(self.ACTIVATE, {
            'new_access_code': '864286', 'confirm_access_code': '864286',
            'employee_id': other.pk, 'role': 'admin',
        })
        self.assertEqual(self.client.session['employee_access_id'], self.access.pk)
        self.assertEqual(self.client.session['active_role_code'], 'mining_master')

    def test_proof_is_consumed_once_and_bound_to_session_generation(self):
        self.make_active()
        self.login()
        request = self.proof_request()
        self.assertEqual(consume_tablet_shift_auth(request).pk, self.access.pk)
        self.assertIsNone(consume_tablet_shift_auth(request))
        request = self.proof_request()
        request.session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = 'wrong-generation'
        self.assertIsNone(get_tablet_shift_auth(request))

    def test_proof_expires_and_rechecks_revoked_access(self):
        self.make_active()
        self.login()
        with patch('users.mining_master_tablet_auth.timezone.now', return_value=timezone.now() + timedelta(seconds=SHIFT_AUTH_TTL + 1)):
            self.assertIsNone(get_tablet_shift_auth(self.proof_request()))
        self.access.is_active = False
        self.access.save(update_fields=['is_active'])
        self.assertIsNone(get_tablet_shift_auth(self.proof_request()))

    def test_new_login_invalidates_old_session_proof(self):
        self.make_active()
        self.login()
        old = self.proof_request()
        other_client = Client()
        other_client.post(self.LOGIN, {'phone': self.employee.phone, 'access_code': '246824'})
        self.assertIsNone(get_tablet_shift_auth(old))

    def test_stale_previous_session_does_not_block_login_or_activation(self):
        self.make_active()
        self.login()
        old_employee = Employee.objects.create(full_name='Уволенный сотрудник', phone='+79000000001')
        old_access = EmployeeAccess.objects.create(
            employee=old_employee, role=self.role, access_code='568927',
            status=EmployeeAccess.Status.BLOCKED,
        )
        session = self.client.session
        session['employee_access_id'] = old_access.pk
        session.save()
        self.assertRedirects(self.login(), self.CONTROL, fetch_redirect_response=False)
        self.assertEqual(self.client.session['employee_access_id'], self.access.pk)

    def test_csrf_required_and_credentials_not_reflected_or_cached(self):
        protected = Client(enforce_csrf_checks=True)
        self.assertEqual(protected.post(self.LOGIN, {'phone': self.employee.phone, 'access_code': '246824'}).status_code, 403)
        self.assertEqual(protected.post(self.ACTIVATE, {'new_access_code': '864286', 'confirm_access_code': '864286'}).status_code, 403)
        response = self.login(access_code='568927')
        self.assertNotContains(response, '568927')
        self.assertIn('no-store', response['Cache-Control'])

    def test_next_url_is_not_accepted(self):
        self.make_active()
        response = self.client.post(self.LOGIN + '?next=https://example.org/', {
            'phone': self.employee.phone, 'access_code': '246824', 'next': 'https://example.org/',
        })
        self.assertEqual(response.url, self.CONTROL)
