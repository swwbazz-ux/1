from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase
from django.utils import timezone

from .access_auth import find_employee_access_by_credentials
from .active_role import activate_role_session, role_session_state
from .models import Employee, EmployeeAccess, Role


class IndependentRoleSessionTests(TestCase):
    def setUp(self):
        self.employee = Employee.objects.create(
            full_name='Многоролевой сотрудник',
            phone='+79990009991',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.driver_role = Role.objects.create(code='driver', name='Водитель')
        self.dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.driver_access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.driver_role,
            access_code='999991',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            activated_at=timezone.now(),
        )
        self.dispatcher_access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.dispatcher_role,
            access_code='999991',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            activated_at=timezone.now(),
        )
        self.factory = RequestFactory()

    def request_with_session(self):
        request = self.factory.get('/')
        SessionMiddleware(lambda _request: None).process_request(request)
        request.session.save()
        return request

    def test_logging_into_second_role_keeps_first_role_session_active(self):
        driver_request = self.request_with_session()
        activate_role_session(driver_request, self.driver_access)
        driver_request.session.save()

        dispatcher_request = self.request_with_session()
        activate_role_session(dispatcher_request, self.dispatcher_access)
        dispatcher_request.session.save()

        self.driver_access.refresh_from_db()
        self.dispatcher_access.refresh_from_db()
        self.assertTrue(role_session_state(driver_request, self.driver_access)['is_active'])
        self.assertTrue(role_session_state(dispatcher_request, self.dispatcher_access)['is_active'])

    def test_second_login_to_same_role_invalidates_old_generation(self):
        first_request = self.request_with_session()
        activate_role_session(first_request, self.driver_access)
        first_request.session.save()

        second_request = self.request_with_session()
        activate_role_session(second_request, self.driver_access)
        second_request.session.save()

        self.driver_access.refresh_from_db()
        self.assertFalse(role_session_state(first_request, self.driver_access)['is_active'])
        self.assertTrue(role_session_state(second_request, self.driver_access)['is_active'])

    def test_same_phone_and_pin_selects_the_requested_role_of_one_employee(self):
        access = find_employee_access_by_credentials(
            self.employee.phone,
            '999991',
            role_code='dispatcher',
        )
        self.assertEqual(access, self.dispatcher_access)

    def test_same_phone_and_pin_for_different_employees_fails_closed(self):
        second_employee = Employee.objects.create(
            full_name='Дубликат номера',
            phone=self.employee.phone,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=second_employee,
            role=self.dispatcher_role,
            access_code='999991',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            activated_at=timezone.now(),
        )

        self.assertIsNone(
            find_employee_access_by_credentials(
                self.employee.phone,
                '999991',
                role_code='dispatcher',
            )
        )
