from datetime import timedelta
from unittest.mock import patch

from django.db import connection, transaction
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from .active_role import ACTIVE_ROLE_GENERATION_SESSION_KEY
from .mining_master_tablet_auth import SHIFT_AUTH_KEY, SHIFT_AUTH_TTL, issue_tablet_shift_auth
from .mining_master_tablet_shift_auth import _lock_access, verify_shift_start
from .models import Employee, EmployeeAccess, Role
from .test_mining_master_tablet_auth import urlpatterns  # isolated auth-only URL wiring


@override_settings(ROOT_URLCONF=__name__, ALLOWED_HOSTS=['testserver', 'localhost'])
class MiningMasterTabletShiftAuthTests(TestCase):
    def setUp(self):
        self.role, _ = Role.objects.get_or_create(code='mining_master', defaults={'name': 'Горный мастер'})
        self.employee = Employee.objects.create(full_name='Иванов Иван Иванович', phone='+79001234567', status=Employee.Status.ACTIVE)
        self.access = EmployeeAccess.objects.create(
            employee=self.employee, role=self.role, access_code='246824', status=EmployeeAccess.Status.ACTIVATED,
        )
        response = self.client.post('/mining-master/tablet/login/', {'phone': self.employee.phone, 'access_code': '246824'})
        self.assertEqual(response.status_code, 302)

    def request(self, **data):
        request = RequestFactory().post('/mining-master/tablet/', {'action': 'start_shift', **data})
        request.session = self.client.session
        return request

    def credentials(self, **extra):
        return self.request(reauth_phone=self.employee.phone, reauth_access_code='24-68-24', **extra)

    def test_fresh_proof_is_consumed_once_inside_transaction(self):
        request = self.request()
        with patch('users.mining_master_tablet_shift_auth._lock_access', wraps=_lock_access) as lock:
            with transaction.atomic():
                access, error = verify_shift_start(request)
                self.assertTrue(connection.in_atomic_block)
        self.assertEqual(access.pk, self.access.pk)
        self.assertEqual(error, '')
        self.assertEqual(lock.call_count, 1)
        self.assertNotIn(SHIFT_AUTH_KEY, request.session)
        self.assertIsNone(verify_shift_start(request)[0])

    def test_revocation_between_lookup_and_lock_invalidates_proof(self):
        def revoke_before_lock(candidate):
            EmployeeAccess.objects.filter(pk=candidate.pk).update(is_active=False)
            return _lock_access(candidate)
        with patch('users.mining_master_tablet_shift_auth._lock_access', side_effect=revoke_before_lock):
            access, error = verify_shift_start(self.request())
        self.assertIsNone(access)
        self.assertTrue(error)

    def test_new_login_between_lookup_and_lock_invalidates_generation(self):
        def new_generation_before_lock(candidate):
            EmployeeAccess.objects.filter(pk=candidate.pk).update(last_login_at=timezone.now() + timedelta(seconds=1))
            return _lock_access(candidate)
        with patch('users.mining_master_tablet_shift_auth._lock_access', side_effect=new_generation_before_lock):
            self.assertIsNone(verify_shift_start(self.request())[0])

    def test_inflight_pin_reissue_rejects_previously_matched_old_pin(self):
        request = self.credentials()
        old_generation = request.session[ACTIVE_ROLE_GENERATION_SESSION_KEY]
        def reissue_before_lock(candidate):
            EmployeeAccess.objects.filter(pk=candidate.pk).update(access_code='568927')
            return _lock_access(candidate)
        with patch('users.mining_master_tablet_shift_auth._lock_access', side_effect=reissue_before_lock):
            access, error = verify_shift_start(request)
        self.assertIsNone(access)
        self.assertTrue(error)
        self.assertEqual(request.session[ACTIVE_ROLE_GENERATION_SESSION_KEY], old_generation)
        self.assertEqual(request.session['employee_access_id'], self.access.pk)

    def test_inflight_phone_change_rejects_previously_matched_phone(self):
        def change_phone_before_lock(candidate):
            Employee.objects.filter(pk=candidate.employee_id).update(phone='+79009876543')
            return _lock_access(candidate)
        with patch('users.mining_master_tablet_shift_auth._lock_access', side_effect=change_phone_before_lock):
            self.assertIsNone(verify_shift_start(self.credentials())[0])

    def test_valid_credentials_override_proof_and_force_shared(self):
        other = Employee.objects.create(full_name='Другой мастер', phone='+79009876543', status=Employee.Status.ACTIVE)
        other_access = EmployeeAccess.objects.create(employee=other, role=self.role, access_code='568927', status=EmployeeAccess.Status.ACTIVATED)
        request = self.request(reauth_phone=other.phone, reauth_access_code='56-89-27', device_kind='personal')
        access, error = verify_shift_start(request)
        self.assertEqual(access.pk, other_access.pk)
        self.assertEqual(error, '')
        self.assertEqual(request.session['employee_access_id'], other_access.pk)
        self.assertEqual(request.session['device_kind'], 'shared')
        self.assertNotIn(SHIFT_AUTH_KEY, request.session)

    def test_wrong_or_partial_credentials_never_fall_back_to_proof(self):
        for data in ({'reauth_phone': self.employee.phone}, {'reauth_access_code': '246824'},
                     {'reauth_phone': self.employee.phone, 'reauth_access_code': '999999'}):
            with self.subTest(data=data):
                request = self.request(**data)
                self.assertIsNone(verify_shift_start(request)[0])
                self.assertEqual(request.session['employee_access_id'], self.access.pk)

    def test_stale_and_tampered_proofs_do_not_authorize(self):
        request = self.request()
        proof = request.session[SHIFT_AUTH_KEY]
        proof['created_at'] -= SHIFT_AUTH_TTL + 1
        request.session[SHIFT_AUTH_KEY] = proof
        self.assertIsNone(verify_shift_start(request)[0])
        request = self.request()
        request.session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = 'wrong-generation'
        self.assertIsNone(verify_shift_start(request)[0])

    def test_duplicate_matching_employee_after_lookup_is_rejected(self):
        def duplicate_before_lock(candidate):
            employee = Employee.objects.create(full_name='Дубликат номера', phone=self.employee.phone, status=Employee.Status.ACTIVE)
            EmployeeAccess.objects.create(employee=employee, role=self.role, access_code='246824', status=EmployeeAccess.Status.ACTIVATED)
            return _lock_access(candidate)
        with patch('users.mining_master_tablet_shift_auth._lock_access', side_effect=duplicate_before_lock):
            self.assertIsNone(verify_shift_start(self.credentials())[0])

    def test_dispatcher_credentials_are_not_master_credentials(self):
        role, _ = Role.objects.get_or_create(code='dispatcher', defaults={'name': 'Диспетчер'})
        employee = Employee.objects.create(full_name='Диспетчер', phone='+79009999999', status=Employee.Status.ACTIVE)
        EmployeeAccess.objects.create(employee=employee, role=role, access_code='568927', status=EmployeeAccess.Status.ACTIVATED)
        request = self.request(reauth_phone=employee.phone, reauth_access_code='568927')
        self.assertIsNone(verify_shift_start(request)[0])

    def test_explicit_pin_allows_only_six_ascii_digits_with_keypad_separators(self):
        for code in ('24682x4', '٢٤٦٨٢٤', '24682', '2468240'):
            with self.subTest(code=code):
                self.assertIsNone(verify_shift_start(self.request(reauth_phone=self.employee.phone, reauth_access_code=code))[0])

    def test_public_issue_helper_supports_next_bound_confirmation(self):
        request = self.credentials()
        access, error = verify_shift_start(request)
        self.assertEqual(error, '')
        issue_tablet_shift_auth(request, access)
        request.POST = request.POST.copy()
        request.POST.pop('reauth_phone')
        request.POST.pop('reauth_access_code')
        self.assertEqual(verify_shift_start(request)[0].pk, self.access.pk)
