import json

from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from shifts.models import EmployeeShift
from users import active_role, role_apps
from users.models import Employee, EmployeeAccess, Role
from users.privacy_consent import PRIVACY_POLICY_VERSION
from users.role_apps import ROLE_APPS


ROLE_HOST_SETTINGS = override_settings(
    ALLOWED_HOSTS=['localhost', '.localhost'],
    ROLE_APP_BASE_DOMAINS=('localhost',),
)


@ROLE_HOST_SETTINGS
class PwaContractVersionMatrixRegressionTests(TestCase):
    """QA-CHAOS-P1-P09-012: one release contract across every role PWA."""

    @classmethod
    def setUpTestData(cls):
        cls.accesses = {}
        for index, app in enumerate(ROLE_APPS, start=1):
            role, _created = Role.objects.update_or_create(
                code=app.role_code,
                defaults={
                    'name': f'P09 {app.short_name}',
                    'is_active': True,
                },
            )
            employee = Employee.objects.create(
                full_name=f'P09 contract {app.short_name}',
                phone=f'+7999100{index:04d}',
                status=Employee.Status.ACTIVE,
                is_active=True,
            )
            access = EmployeeAccess.objects.create(
                employee=employee,
                role=role,
                access_code=f'91{index:04d}',
                status=EmployeeAccess.Status.ACTIVATED,
                activated_at=timezone.now(),
                is_active=True,
            )
            cls.accesses[app.role_code] = access

    def test_global_application_contract_version_is_explicit(self):
        contract_version = getattr(role_apps, 'APP_CONTRACT_VERSION', '')

        self.assertIsInstance(contract_version, str)
        self.assertTrue(contract_version.strip())

    def test_legacy_role_paths_publish_the_same_contract_metadata(self):
        from users.context_processors import role_app as role_app_context

        request_factory = RequestFactory()
        for app in ROLE_APPS:
            with self.subTest(role=app.role_code):
                request = request_factory.get(
                    app.start_url,
                    HTTP_HOST='localhost',
                )
                request.session = {}
                context = role_app_context(request)
                self.assertIsNone(context['role_app'])
                self.assertEqual(
                    context['app_contract_version'],
                    role_apps.APP_CONTRACT_VERSION,
                )
                self.assertEqual(
                    context['app_shell_version'],
                    app.shell_version,
                )
                self.assertEqual(context['app_role_code'], app.role_code)
                self.assertEqual(
                    context['app_service_worker_url'],
                    app.service_worker_url,
                )
                self.assertEqual(
                    context['app_service_worker_scope'],
                    app.legacy_scope,
                )

    def test_manifest_worker_html_and_poll_publish_one_contract_for_all_role_apps(self):
        contract_version = getattr(
            role_apps,
            'APP_CONTRACT_VERSION',
            '__missing_app_contract_version__',
        )

        self.assertEqual(len(ROLE_APPS), 12)
        for app in ROLE_APPS:
            host = f'{app.subdomain}.localhost'
            access = self.accesses[app.role_code]
            with self.subTest(role=app.role_code, surface='manifest'):
                manifest_response = self.client.get(
                    app.manifest_url,
                    HTTP_HOST=host,
                )
                self.assertEqual(manifest_response.status_code, 200)
                manifest = json.loads(
                    manifest_response.content.decode('utf-8')
                )
                self.assertEqual(
                    manifest.get('app_contract_version'),
                    contract_version,
                )
                self.assertEqual(
                    manifest.get('shell_version'),
                    app.shell_version,
                )
                self.assertEqual(manifest.get('role_code'), app.role_code)

            with self.subTest(role=app.role_code, surface='service-worker'):
                worker_response = self.client.get(
                    app.service_worker_url,
                    HTTP_HOST=host,
                )
                self.assertEqual(worker_response.status_code, 200)
                worker = worker_response.content.decode('utf-8')
                self.assertIn(
                    f'const APP_CONTRACT_VERSION = {json.dumps(contract_version)};',
                    worker,
                )
                self.assertIn(
                    f'const ROLE_CODE = {json.dumps(app.role_code)};',
                    worker,
                )
                self.assertIn(
                    f'const CACHE_NAME = {json.dumps(app.shell_version)};',
                    worker,
                )
                self.assertNotIn('ignoreSearch: true', worker)
                self.assertIn('appContractVersion', worker)
                self.assertIn('shellVersion', worker)
                self.assertIn('roleCode', worker)

            with self.subTest(role=app.role_code, surface='html'):
                html_response = self.client.get('/', HTTP_HOST=host)
                self.assertEqual(html_response.status_code, 200)
                self.assertContains(
                    html_response,
                    f'data-app-contract-version="{contract_version}"',
                    html=False,
                )
                self.assertContains(
                    html_response,
                    f'data-app-shell-version="{app.shell_version}"',
                    html=False,
                )
                self.assertContains(
                    html_response,
                    f'data-app-role-code="{app.role_code}"',
                    html=False,
                )
                self.assertContains(
                    html_response,
                    'data-app-contract-ready="false"',
                    html=False,
                )

            with self.subTest(role=app.role_code, surface='poll'):
                role_client = Client()
                login_response = role_client.post(
                    reverse('login'),
                    {
                        'phone': access.employee.phone,
                        'access_code': access.access_code,
                        'device_kind': 'personal',
                        'privacy_consent': PRIVACY_POLICY_VERSION,
                    },
                    HTTP_HOST=host,
                )
                self.assertEqual(login_response.status_code, 302)
                poll_response = role_client.get(
                    reverse('operational_state_version'),
                    {'include_events': '0'},
                    HTTP_HOST=host,
                )
                self.assertEqual(poll_response.status_code, 200)
                payload = poll_response.json()
                self.assertEqual(
                    payload.get('app_contract_version'),
                    contract_version,
                )
                self.assertEqual(
                    payload.get('role_shell_version'),
                    app.shell_version,
                )
                self.assertEqual(
                    payload.get('role_app_code'),
                    app.role_code,
                )


@ROLE_HOST_SETTINGS
class SameEmployeeAccessRoleRevisionRegressionTests(TestCase):
    """QA-CHAOS-P2-P09-011: a role edit invalidates the already-open PWA."""

    def setUp(self):
        self.admin_role, _created = Role.objects.update_or_create(
            code='admin',
            defaults={'name': 'P09 администратор', 'is_active': True},
        )
        self.manager_role, _created = Role.objects.update_or_create(
            code='manager',
            defaults={'name': 'P09 руководство', 'is_active': True},
        )
        self.dispatcher_role, _created = Role.objects.update_or_create(
            code='dispatcher',
            defaults={'name': 'P09 диспетчер', 'is_active': True},
        )
        self.admin_employee = Employee.objects.create(
            full_name='P09 администратор сессий',
            phone='+79992000001',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.target_employee = Employee.objects.create(
            full_name='P09 сотрудник со сменой роли',
            phone='+79992000002',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.admin_employee,
            role=self.admin_role,
            access_code='920001',
            status=EmployeeAccess.Status.ACTIVATED,
            activated_at=timezone.now(),
            is_active=True,
        )
        self.target_access = EmployeeAccess.objects.create(
            employee=self.target_employee,
            role=self.manager_role,
            access_code='920002',
            status=EmployeeAccess.Status.ACTIVATED,
            activated_at=timezone.now(),
            is_active=True,
        )

    def login(self, client, *, host, access):
        return client.post(
            reverse('login'),
            {
                'phone': access.employee.phone,
                'access_code': access.access_code,
                'device_kind': 'personal',
                'privacy_consent': PRIVACY_POLICY_VERSION,
            },
            HTTP_HOST=host,
        )

    def test_same_access_role_change_invalidates_old_poll_and_old_post(self):
        old_client = Client()
        old_login = self.login(
            old_client,
            host='management.localhost',
            access=self.target_access,
        )
        self.assertEqual(old_login.status_code, 302)

        role_snapshot_key = getattr(
            active_role,
            'ACTIVE_ROLE_CODE_SESSION_KEY',
            '',
        )
        self.assertTrue(role_snapshot_key)
        old_session = old_client.session
        self.assertEqual(old_session.get(role_snapshot_key), 'manager')
        initial_revision = old_session.get(
            active_role.ACTIVE_ROLE_GENERATION_SESSION_KEY
        )
        self.assertTrue(initial_revision)

        initial_poll = old_client.get(
            reverse('operational_state_version'),
            {'include_events': '0'},
            HTTP_HOST='management.localhost',
        )
        self.assertEqual(initial_poll.status_code, 200)
        initial_payload = initial_poll.json()
        self.assertTrue(initial_payload['role_active'])
        self.assertEqual(initial_payload.get('session_role_code'), 'manager')
        self.assertEqual(
            initial_payload.get('session_revision'),
            initial_revision,
        )

        admin_client = Client()
        admin_login = self.login(
            admin_client,
            host='admin.localhost',
            access=self.admin_access,
        )
        self.assertEqual(admin_login.status_code, 302)
        change_response = admin_client.post(
            reverse(
                'system_admin_change_access_role',
                args=[self.target_access.id],
            ),
            {'role': self.dispatcher_role.id},
            HTTP_HOST='admin.localhost',
        )
        self.assertEqual(change_response.status_code, 302)
        self.target_access.refresh_from_db()
        self.assertEqual(self.target_access.role, self.dispatcher_role)

        stale_poll = old_client.get(
            reverse('operational_state_version'),
            {'include_events': '0'},
            HTTP_HOST='management.localhost',
        )
        self.assertIn(stale_poll.status_code, {200, 401})
        stale_payload = stale_poll.json()
        if stale_poll.status_code == 200:
            self.assertTrue(stale_payload['authenticated'])
            self.assertFalse(stale_payload['role_active'])
            self.assertEqual(
                stale_payload.get('session_role_code'),
                'manager',
            )
            self.assertEqual(
                stale_payload.get('session_revision'),
                initial_revision,
            )
            self.assertEqual(
                stale_payload.get('active_role_code'),
                'dispatcher',
            )
        else:
            self.assertFalse(stale_payload['authenticated'])

        shifts_before = EmployeeShift.objects.filter(
            employee=self.target_employee,
        ).count()
        stale_post = old_client.post(
            reverse('dispatcher_toggle_shift'),
            {
                'shift_action': 'start',
                'reauth_phone': self.target_employee.phone,
                'reauth_access_code': self.target_access.access_code,
            },
            HTTP_HOST='management.localhost',
            HTTP_ACCEPT='application/json',
        )
        self.assertEqual(stale_post.status_code, 409)
        self.assertEqual(
            EmployeeShift.objects.filter(
                employee=self.target_employee,
            ).count(),
            shifts_before,
        )

        new_client = Client()
        new_login = self.login(
            new_client,
            host='dispatcher.localhost',
            access=self.target_access,
        )
        self.assertEqual(new_login.status_code, 302)
        new_poll = new_client.get(
            reverse('operational_state_version'),
            {'include_events': '0'},
            HTTP_HOST='dispatcher.localhost',
        )
        self.assertEqual(new_poll.status_code, 200)
        new_payload = new_poll.json()
        self.assertTrue(new_payload['role_active'])
        self.assertEqual(
            new_payload.get('session_role_code'),
            'dispatcher',
        )
        self.assertTrue(new_payload.get('session_revision'))
