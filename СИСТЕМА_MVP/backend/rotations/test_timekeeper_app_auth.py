from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from portal.auth import (
    PORTAL_EMPLOYEE_SESSION_KEY,
    PORTAL_SOURCE_ACCESS_SESSION_KEY,
)
from users.active_role import (
    ACTIVE_ROLE_CODE_SESSION_KEY,
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    ACTIVE_ROLE_SESSION_KEY,
)
from users.models import Employee, EmployeeAccess, Role

from .timekeeper_auth import (
    TIMEKEEPER_APP_ACCESS_SESSION_KEY,
    TIMEKEEPER_APP_CREDENTIAL_REVISION_SESSION_KEY,
)


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '.localhost'])
class TimekeeperAppAuthTests(TestCase):
    def setUp(self):
        self.timekeeper_role = Role.objects.get(code='timekeeper')
        self.admin_role, _created = Role.objects.get_or_create(
            code='admin',
            defaults={'name': 'Системный администратор', 'is_active': True},
        )
        self.manager_role, _created = Role.objects.get_or_create(
            code='manager',
            defaults={'name': 'Руководитель', 'is_active': True},
        )
        self.oup_role, _created = Role.objects.get_or_create(
            code='oup',
            defaults={'name': 'ОУП', 'is_active': True},
        )
        self.dispatcher_role, _created = Role.objects.get_or_create(
            code='dispatcher',
            defaults={'name': 'Диспетчер', 'is_active': True},
        )
        self.timekeeper_access = self._access(
            role=self.timekeeper_role,
            full_name='Точный табельщик приложения',
            phone='+7 900 700-00-01',
            access_code='710001',
        )
        self.admin_access = self._access(
            role=self.admin_role,
            full_name='Точный системный администратор',
            phone='+7 900 700-00-02',
            access_code='710002',
            last_login_at=timezone.now() - timedelta(minutes=5),
        )
        self.login_url = reverse('timekeeper_login')
        self.logout_url = reverse('timekeeper_logout')
        self.dashboard_url = reverse('rotation_timekeeper_dashboard')

    def _access(
        self,
        *,
        role,
        full_name,
        phone,
        access_code,
        status=EmployeeAccess.Status.ACTIVATED,
        is_active=True,
        employee_status=Employee.Status.ACTIVE,
        employee_is_active=True,
        last_login_at=None,
    ):
        employee = Employee.objects.create(
            full_name=full_name,
            phone=phone,
            status=employee_status,
            is_active=employee_is_active,
        )
        return EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code=access_code,
            status=status,
            is_active=is_active,
            activated_at=(
                timezone.now()
                if status == EmployeeAccess.Status.ACTIVATED
                else None
            ),
            last_login_at=last_login_at,
        )

    @staticmethod
    def _credentials(access):
        return {
            'phone': access.employee.phone,
            'access_code': access.access_code,
            'device_kind': 'personal',
        }

    def _login(self, access=None, *, client=None, **extra):
        access = access or self.timekeeper_access
        client = client or self.client
        return client.post(
            self.login_url,
            self._credentials(access),
            **extra,
        )

    def test_exact_timekeeper_login_uses_only_namespaced_session(self):
        initial_session_key = self.client.session.session_key
        original_last_login_at = self.timekeeper_access.last_login_at

        response = self._login()

        self.assertRedirects(
            response,
            self.dashboard_url,
            fetch_redirect_response=False,
        )
        session = self.client.session
        self.assertNotEqual(session.session_key, initial_session_key)
        self.assertEqual(
            session[TIMEKEEPER_APP_ACCESS_SESSION_KEY],
            self.timekeeper_access.pk,
        )
        self.assertTrue(
            session[TIMEKEEPER_APP_CREDENTIAL_REVISION_SESSION_KEY],
        )
        for forbidden_key in (
            'employee_access_id',
            ACTIVE_ROLE_SESSION_KEY,
            ACTIVE_ROLE_GENERATION_SESSION_KEY,
            ACTIVE_ROLE_CODE_SESSION_KEY,
        ):
            self.assertNotIn(forbidden_key, session)
        self.timekeeper_access.refresh_from_db()
        self.assertEqual(
            self.timekeeper_access.last_login_at,
            original_last_login_at,
        )

    def test_exact_admin_login_uses_actual_admin_access_and_label(self):
        original_last_login_at = self.admin_access.last_login_at
        response = self._login(self.admin_access)
        self.assertRedirects(
            response,
            self.dashboard_url,
            fetch_redirect_response=False,
        )
        self.assertEqual(
            self.client.session[TIMEKEEPER_APP_ACCESS_SESSION_KEY],
            self.admin_access.pk,
        )
        dashboard = self.client.get(self.dashboard_url)
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, self.admin_access.employee.full_name)
        self.assertContains(dashboard, 'Системный администратор')
        self.assertContains(dashboard, 'Календарь фаз бригад')
        self.assertContains(dashboard, 'Графики и вахты сотрудников')
        self.admin_access.refresh_from_db()
        self.assertEqual(self.admin_access.last_login_at, original_last_login_at)

    def test_wrong_inactive_blocked_unactivated_and_inactive_employee_are_closed(self):
        inactive_role = Role.objects.create(
            code='timekeeper-inactive-role',
            name='Неактивная роль Табельщика',
            is_active=False,
        )
        cases = (
            self._access(
                role=self.manager_role,
                full_name='Руководитель без доступа Табельщика',
                phone='+7 900 700-00-03',
                access_code='710003',
            ),
            self._access(
                role=self.timekeeper_role,
                full_name='Неактивный доступ Табельщика',
                phone='+7 900 700-00-04',
                access_code='710004',
                status=EmployeeAccess.Status.DEACTIVATED,
                is_active=False,
            ),
            self._access(
                role=self.timekeeper_role,
                full_name='Заблокированный доступ Табельщика',
                phone='+7 900 700-00-05',
                access_code='710005',
                status=EmployeeAccess.Status.BLOCKED,
            ),
            self._access(
                role=self.timekeeper_role,
                full_name='Неактивированный доступ Табельщика',
                phone='+7 900 700-00-06',
                access_code='710006',
                status=EmployeeAccess.Status.NOT_ACTIVATED,
            ),
            self._access(
                role=self.timekeeper_role,
                full_name='Неактивный сотрудник Табельщика',
                phone='+7 900 700-00-07',
                access_code='710007',
                employee_status=Employee.Status.DEACTIVATED,
                employee_is_active=False,
            ),
            self._access(
                role=inactive_role,
                full_name='Сотрудник неактивной роли Табельщика',
                phone='+7 900 700-00-11',
                access_code='710011',
            ),
        )
        for access in cases:
            with self.subTest(access=access.pk):
                client = Client()
                response = self._login(access, client=client)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'для приложения «Табельщик»')
                self.assertNotIn(
                    TIMEKEEPER_APP_ACCESS_SESSION_KEY,
                    client.session,
                )

        Role.objects.filter(pk=self.timekeeper_role.pk).update(is_active=False)
        inactive_role_client = Client()
        response = self._login(
            self.timekeeper_access,
            client=inactive_role_client,
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            TIMEKEEPER_APP_ACCESS_SESSION_KEY,
            inactive_role_client.session,
        )

    def test_manager_oup_and_dispatcher_cannot_enter(self):
        cases = (
            (self.manager_role, '+7 900 700-00-08', '710008'),
            (self.oup_role, '+7 900 700-00-09', '710009'),
            (self.dispatcher_role, '+7 900 700-00-10', '710010'),
        )
        for index, (role, phone, access_code) in enumerate(cases, start=1):
            access = self._access(
                role=role,
                full_name=f'Запрещённая роль приложения {index}',
                phone=phone,
                access_code=access_code,
            )
            client = Client()
            response = self._login(access, client=client)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(TIMEKEEPER_APP_ACCESS_SESSION_KEY, client.session)

    def test_legacy_employee_access_id_does_not_authorize_timekeeper_views(self):
        session = self.client.session
        session['employee_access_id'] = self.timekeeper_access.pk
        session.save()
        response = self.client.get(self.dashboard_url)
        self.assertRedirects(
            response,
            self.login_url,
            fetch_redirect_response=False,
        )

    def test_login_get_and_existing_session_redirects_are_stable(self):
        self.assertEqual(self.login_url, '/timekeeper/login/')
        self.assertEqual(self.logout_url, '/timekeeper/logout/')
        self.assertEqual(self.dashboard_url, '/timekeeper/')
        response = self.client.get(self.login_url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'users/login.html')
        self.assertContains(response, 'Табельщик')
        self._login()
        self.assertRedirects(
            self.client.get(self.login_url),
            self.dashboard_url,
            fetch_redirect_response=False,
        )

    def test_pin_reset_block_and_deactivation_invalidate_app_session(self):
        mutations = (
            {
                'access_code': '719901',
                'status': EmployeeAccess.Status.NOT_ACTIVATED,
                'primary_code_issued_at': timezone.now(),
                'activated_at': None,
            },
            {
                'status': EmployeeAccess.Status.BLOCKED,
                'is_active': False,
                'blocked_at': timezone.now(),
            },
            {
                'status': EmployeeAccess.Status.DEACTIVATED,
                'is_active': False,
                'deactivated_at': timezone.now(),
            },
        )
        for mutation in mutations:
            with self.subTest(mutation=tuple(sorted(mutation))):
                access = self._access(
                    role=self.timekeeper_role,
                    full_name=f'Инвалидация сессии {len(Employee.objects.all())}',
                    phone=f'+7 900 701-{Employee.objects.count():02d}-00',
                    access_code=f'72{Employee.objects.count():04d}',
                )
                client = Client()
                self._login(access, client=client)
                EmployeeAccess.objects.filter(pk=access.pk).update(**mutation)
                response = client.get(self.dashboard_url)
                self.assertRedirects(
                    response,
                    self.login_url,
                    fetch_redirect_response=False,
                )
                self.assertNotIn(
                    TIMEKEEPER_APP_ACCESS_SESSION_KEY,
                    client.session,
                )
                self.assertNotIn(
                    TIMEKEEPER_APP_CREDENTIAL_REVISION_SESSION_KEY,
                    client.session,
                )

    def test_admin_and_timekeeper_app_sessions_work_simultaneously(self):
        admin_client = Client()
        admin_session = admin_client.session
        admin_session['employee_access_id'] = self.admin_access.pk
        admin_session[ACTIVE_ROLE_SESSION_KEY] = self.admin_access.pk
        admin_session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = (
            self.admin_access.last_login_at.isoformat()
        )
        admin_session[ACTIVE_ROLE_CODE_SESSION_KEY] = 'admin'
        admin_session.save()

        timekeeper_client = Client()
        self._login(
            self.admin_access,
            client=timekeeper_client,
            HTTP_HOST='timekeeper.localhost',
        )
        second_timekeeper_client = Client()
        self._login(
            self.admin_access,
            client=second_timekeeper_client,
            HTTP_HOST='timekeeper.localhost',
        )

        self.assertEqual(
            admin_client.get(
                reverse('system_admin_dashboard'),
                HTTP_HOST='admin.localhost',
            ).status_code,
            200,
        )
        self.assertEqual(
            timekeeper_client.get(
                self.dashboard_url,
                HTTP_HOST='timekeeper.localhost',
            ).status_code,
            200,
        )
        self.assertEqual(
            second_timekeeper_client.get(
                self.dashboard_url,
                HTTP_HOST='timekeeper.localhost',
            ).status_code,
            200,
        )
        self.admin_access.refresh_from_db()
        self.assertEqual(
            admin_client.session[ACTIVE_ROLE_GENERATION_SESSION_KEY],
            self.admin_access.last_login_at.isoformat(),
        )

    def test_logout_is_post_only_and_removes_only_timekeeper_namespace(self):
        session = self.client.session
        session['employee_access_id'] = self.admin_access.pk
        session[ACTIVE_ROLE_SESSION_KEY] = self.admin_access.pk
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = (
            self.admin_access.last_login_at.isoformat()
        )
        session[ACTIVE_ROLE_CODE_SESSION_KEY] = 'admin'
        session[PORTAL_EMPLOYEE_SESSION_KEY] = self.admin_access.employee_id
        session[PORTAL_SOURCE_ACCESS_SESSION_KEY] = self.admin_access.pk
        session.save()
        self._login(self.admin_access)

        self.assertEqual(
            self.client.session['employee_access_id'],
            self.admin_access.pk,
        )
        self.assertEqual(
            self.client.session[PORTAL_EMPLOYEE_SESSION_KEY],
            self.admin_access.employee_id,
        )

        self.assertEqual(self.client.get(self.logout_url).status_code, 405)
        response = self.client.post(self.logout_url)
        self.assertRedirects(
            response,
            self.login_url,
            fetch_redirect_response=False,
        )
        session = self.client.session
        self.assertNotIn(TIMEKEEPER_APP_ACCESS_SESSION_KEY, session)
        self.assertNotIn(
            TIMEKEEPER_APP_CREDENTIAL_REVISION_SESSION_KEY,
            session,
        )
        self.assertEqual(session['employee_access_id'], self.admin_access.pk)
        self.assertEqual(session[ACTIVE_ROLE_SESSION_KEY], self.admin_access.pk)
        self.assertEqual(
            session[PORTAL_EMPLOYEE_SESSION_KEY],
            self.admin_access.employee_id,
        )
        self.assertEqual(
            session[PORTAL_SOURCE_ACCESS_SESSION_KEY],
            self.admin_access.pk,
        )
        self.assertEqual(
            self.client.get(
                reverse('system_admin_dashboard'),
                HTTP_HOST='admin.localhost',
            ).status_code,
            200,
        )

    def test_login_and_logout_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(
            client.post(
                self.login_url,
                self._credentials(self.timekeeper_access),
            ).status_code,
            403,
        )
        login_page = client.get(self.login_url)
        token = login_page.cookies['csrftoken'].value
        payload = self._credentials(self.timekeeper_access)
        payload['csrfmiddlewaretoken'] = token
        self.assertEqual(client.post(self.login_url, payload).status_code, 302)
        self.assertEqual(client.post(self.logout_url).status_code, 403)
        self.assertEqual(client.post(
            self.logout_url,
            {'csrfmiddlewaretoken': token},
        ).status_code, 302)

    def test_old_global_role_session_does_not_block_app_login_or_logout(self):
        session = self.client.session
        session['employee_access_id'] = self.admin_access.pk
        session[ACTIVE_ROLE_SESSION_KEY] = self.admin_access.pk
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = '2000-01-01T00:00:00+00:00'
        session[ACTIVE_ROLE_CODE_SESSION_KEY] = 'admin'
        session.save()
        self.assertEqual(self._login(self.admin_access).status_code, 302)
        self.assertEqual(self.client.post(self.logout_url).status_code, 302)

    def test_admin_gets_dashboard_calendar_and_profiles(self):
        self._login(self.admin_access)
        for url in (
            self.dashboard_url,
            reverse('timekeeper_brigade_phase_calendar'),
            reverse('timekeeper_employee_watch_profiles'),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(
                    response,
                    f'action="{self.logout_url}"',
                )

    def test_manifest_keeps_existing_start_url_and_scope(self):
        response = self.client.get(
            reverse('timekeeper_manifest'),
            HTTP_HOST='timekeeper.localhost',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['start_url'], '/timekeeper/')
        self.assertEqual(payload['scope'], '/')
