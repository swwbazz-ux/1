import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from django.conf import settings
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from PIL import Image

from .models import Employee, EmployeeAccess, Role
from .role_apps import ROLE_APPS, get_role_app_for_host


ROLE_HOST_SETTINGS = override_settings(ALLOWED_HOSTS=['localhost', '.localhost'])
SECURITY_ENV_NAMES = (
    'DJANGO_SECURE_PROXY_SSL_HEADER',
    'DJANGO_SECURE_SSL_REDIRECT',
    'DJANGO_SESSION_COOKIE_SECURE',
    'DJANGO_CSRF_COOKIE_SECURE',
)


class ProductionSecuritySettingsTests(SimpleTestCase):
    probe_script = (
        'import json; from config import settings as s; '
        'print(json.dumps({'
        '"proxy": s.SECURE_PROXY_SSL_HEADER, '
        '"redirect": s.SECURE_SSL_REDIRECT, '
        '"session": s.SESSION_COOKIE_SECURE, '
        '"csrf": s.CSRF_COOKIE_SECURE'
        '}))'
    )

    def run_probe(self, **overrides):
        environment = os.environ.copy()
        for name in SECURITY_ENV_NAMES:
            environment.pop(name, None)
        environment.update({name: 'False' for name in SECURITY_ENV_NAMES})
        environment.update(overrides)
        return subprocess.run(
            [sys.executable, '-c', self.probe_script],
            cwd=settings.BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_https_security_settings_can_be_enabled_for_reverse_proxy(self):
        result = self.run_probe(**{name: 'True' for name in SECURITY_ENV_NAMES})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                'proxy': ['HTTP_X_FORWARDED_PROTO', 'https'],
                'redirect': True,
                'session': True,
                'csrf': True,
            },
        )

    def test_https_security_settings_can_stay_disabled_for_local_http(self):
        result = self.run_probe()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {'proxy': None, 'redirect': False, 'session': False, 'csrf': False},
        )

    def test_invalid_https_security_flag_fails_fast(self):
        result = self.run_probe(DJANGO_SESSION_COOKIE_SECURE='sometimes')

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('DJANGO_SESSION_COOKIE_SECURE must be a boolean value', result.stderr)


class RoleAppRegistryTests(SimpleTestCase):
    def test_all_role_subdomains_resolve_and_shared_hosts_stay_generic(self):
        self.assertIsNone(get_role_app_for_host('driverform.ru'))
        self.assertIsNone(get_role_app_for_host('localhost:8000'))
        self.assertIsNone(get_role_app_for_host('www.driverform.ru'))
        self.assertIsNone(get_role_app_for_host('nested.driver.driverform.ru'))

        for app in ROLE_APPS:
            with self.subTest(role=app.role_code):
                self.assertEqual(
                    get_role_app_for_host(f'{app.subdomain}.driverform.ru').role_code,
                    app.role_code,
                )
                self.assertEqual(
                    get_role_app_for_host(f'{app.subdomain}.localhost:8000').role_code,
                    app.role_code,
                )

    def test_session_and_csrf_cookies_remain_host_only(self):
        self.assertIsNone(settings.SESSION_COOKIE_DOMAIN)
        self.assertIsNone(settings.CSRF_COOKIE_DOMAIN)

    def test_role_icons_have_expected_sizes_and_unique_visuals(self):
        icon_dir = Path(settings.BASE_DIR) / 'static' / 'img' / 'pwa'
        role_digests = set()

        for app in ROLE_APPS:
            expected = {
                f'{app.icon_slug}-180.png': (180, 180),
                f'{app.icon_slug}-192.png': (192, 192),
                f'{app.icon_slug}-512.png': (512, 512),
                f'{app.icon_slug}-maskable-512.png': (512, 512),
            }
            for filename, expected_size in expected.items():
                with self.subTest(role=app.role_code, icon=filename):
                    path = icon_dir / filename
                    self.assertTrue(path.is_file())
                    with Image.open(path) as image:
                        self.assertEqual(image.size, expected_size)
                        self.assertEqual(image.mode, 'RGB')

            role_digests.add(hashlib.sha256((icon_dir / f'{app.icon_slug}-512.png').read_bytes()).hexdigest())

        self.assertEqual(len(role_digests), len(ROLE_APPS))


@ROLE_HOST_SETTINGS
class RoleAppManifestTests(SimpleTestCase):
    def test_manifest_and_worker_scopes_follow_the_current_origin(self):
        client = Client()
        icon_sources = set()

        for app in ROLE_APPS:
            with self.subTest(role=app.role_code, mode='shared-origin'):
                manifest_response = client.get(app.manifest_url, HTTP_HOST='localhost')
                self.assertEqual(manifest_response.status_code, 200)
                manifest = json.loads(manifest_response.content.decode('utf-8'))
                self.assertEqual(manifest['start_url'], app.start_url)
                self.assertEqual(manifest['scope'], app.legacy_scope)
                self.assertTrue(manifest['start_url'].startswith(manifest['scope']))
                self.assertIn(app.icon_192_url, {icon['src'] for icon in manifest['icons']})
                self.assertTrue(any(icon.get('purpose') == 'maskable' for icon in manifest['icons']))
                icon_sources.add(app.icon_512_url)

                worker_response = client.get(app.service_worker_url, HTTP_HOST='localhost')
                self.assertEqual(worker_response.status_code, 200)
                self.assertEqual(worker_response['Service-Worker-Allowed'], app.legacy_scope)

            with self.subTest(role=app.role_code, mode='isolated-origin'):
                role_host = f'{app.subdomain}.localhost'
                manifest_response = client.get(app.manifest_url, HTTP_HOST=role_host)
                manifest = json.loads(manifest_response.content.decode('utf-8'))
                self.assertEqual(manifest['scope'], '/')

                worker_response = client.get(app.service_worker_url, HTTP_HOST=role_host)
                self.assertEqual(worker_response['Service-Worker-Allowed'], '/')

        self.assertEqual(len(icon_sources), len(ROLE_APPS))

    def test_manifest_for_another_role_does_not_gain_root_scope(self):
        response = Client().get('/excavator.webmanifest', HTTP_HOST='driver.localhost')
        manifest = json.loads(response.content.decode('utf-8'))
        self.assertEqual(manifest['scope'], '/excavator/')

    def test_shared_login_has_no_role_pwa_and_role_login_has_matching_identity(self):
        client = Client()
        shared_response = client.get('/', HTTP_HOST='localhost')
        self.assertNotContains(shared_response, 'rel="manifest"')
        self.assertNotContains(shared_response, 'navigator.serviceWorker.register("/mining-master-sw.js"')

        role_response = client.get('/', HTTP_HOST='driver.localhost')
        self.assertContains(role_response, '/driver.webmanifest')
        self.assertContains(role_response, '/driver-sw.js')
        self.assertContains(role_response, '/static/img/pwa/driver-180.png')
        self.assertContains(role_response, 'Водитель самосвала')

    def test_existing_workers_delete_only_their_own_cache_family(self):
        for worker_url, cache_prefix in (
            ('/dispatcher-sw.js', 'dispatcher-desktop-shell-'),
            ('/excavator-sw.js', 'excavator-mobile-shell-'),
        ):
            with self.subTest(worker=worker_url):
                script = Client().get(worker_url, HTTP_HOST='localhost').content.decode('utf-8')
                self.assertIn(f'const CACHE_PREFIX = "{cache_prefix}";', script)
                self.assertIn('key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME', script)
                self.assertNotIn('keys.filter(key => key !== CACHE_NAME)', script)
                self.assertIn('new URL(request.url).pathname === fallbackUrl', script)


@ROLE_HOST_SETTINGS
class RoleAppLoginTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.driver_access = cls._create_access(
            role_code='driver',
            role_name='Водитель самосвала',
            full_name='Тестовый водитель',
            phone='+79990000001',
            access_code='110001',
        )
        cls.excavator_access = cls._create_access(
            role_code='excavator_operator',
            role_name='Машинист экскаватора',
            full_name='Тестовый машинист',
            phone='+79990000002',
            access_code='110002',
        )
        cls.oup_access = cls._create_access(
            role_code='oup',
            role_name='ОУП',
            full_name='Тестовый специалист ОУП',
            phone='+79990000003',
            access_code='110003',
        )
        cls.mechanic_access = cls._create_access(
            role_code='mechanic',
            role_name='Механик',
            full_name='Тестовый механик',
            phone='+79990000004',
            access_code='110004',
        )
        cls.manager_access = cls._create_access(
            role_code='manager',
            role_name='Руководство',
            full_name='Тестовый руководитель',
            phone='+79990000005',
            access_code='110005',
        )
        cls.admin_access = cls._create_access(
            role_code='admin',
            role_name='Администратор',
            full_name='Тестовый администратор',
            phone='+79990000006',
            access_code='110006',
        )

    @classmethod
    def _create_access(cls, *, role_code, role_name, full_name, phone, access_code):
        role, _ = Role.objects.get_or_create(code=role_code, defaults={'name': role_name})
        employee = Employee.objects.create(
            full_name=full_name,
            phone=phone,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        return EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code=access_code,
            status=EmployeeAccess.Status.ACTIVATED,
            activated_at=timezone.now(),
            is_active=True,
        )

    def _credentials(self, access):
        return {
            'phone': access.employee.phone,
            'access_code': access.access_code,
            'device_kind': 'personal',
        }

    def test_role_host_accepts_only_its_own_role(self):
        rejected = self.client.post(
            '/',
            self._credentials(self.excavator_access),
            HTTP_HOST='driver.localhost',
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertContains(rejected, 'для приложения «Водитель»')
        self.assertNotIn('employee_access_id', self.client.session)

        accepted = self.client.post(
            '/',
            self._credentials(self.driver_access),
            HTTP_HOST='driver.localhost',
        )
        self.assertRedirects(accepted, '/home/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['employee_access_id'], self.driver_access.id)

    def test_shared_login_keeps_the_existing_multi_role_entry_point(self):
        response = self.client.post(
            '/',
            self._credentials(self.excavator_access),
            HTTP_HOST='localhost',
        )
        self.assertRedirects(response, '/home/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['employee_access_id'], self.excavator_access.id)

    def test_mismatched_stale_session_is_flushed_on_role_host(self):
        session = self.client.session
        session['employee_access_id'] = self.excavator_access.id
        session.save()

        response = self.client.get('/home/', HTTP_HOST='driver.localhost')
        self.assertRedirects(response, '/', fetch_redirect_response=False)
        self.assertNotIn('employee_access_id', self.client.session)

    def test_role_hosts_issue_independent_host_only_session_cookies(self):
        driver_client = Client()
        excavator_client = Client()

        driver_response = driver_client.post(
            '/',
            self._credentials(self.driver_access),
            HTTP_HOST='driver.localhost',
        )
        excavator_response = excavator_client.post(
            '/',
            self._credentials(self.excavator_access),
            HTTP_HOST='excavator.localhost',
        )

        cookie_name = settings.SESSION_COOKIE_NAME
        self.assertEqual(driver_response.cookies[cookie_name]['domain'], '')
        self.assertEqual(excavator_response.cookies[cookie_name]['domain'], '')
        self.assertNotEqual(
            driver_response.cookies[cookie_name].value,
            excavator_response.cookies[cookie_name].value,
        )
        self.assertEqual(driver_client.session['employee_access_id'], self.driver_access.id)
        self.assertEqual(excavator_client.session['employee_access_id'], self.excavator_access.id)

    def test_personal_session_is_long_lived_and_shared_session_closes_with_browser(self):
        personal_client = Client()
        personal_client.post(
            '/',
            self._credentials(self.driver_access),
            HTTP_HOST='driver.localhost',
        )
        personal_session = personal_client.session
        self.assertFalse(personal_session.get_expire_at_browser_close())
        self.assertGreaterEqual(
            personal_session.get_expiry_age(),
            settings.ROLE_APP_PERSONAL_SESSION_AGE - 5,
        )

        shared_client = Client()
        credentials = self._credentials(self.driver_access)
        credentials['device_kind'] = 'shared'
        shared_client.post('/', credentials, HTTP_HOST='driver.localhost')
        self.assertTrue(shared_client.session.get_expire_at_browser_close())

    def test_personal_session_expiry_is_renewed_on_use(self):
        self.client.post(
            '/',
            self._credentials(self.driver_access),
            HTTP_HOST='driver.localhost',
        )
        session = self.client.session
        session.set_expiry(30)
        session.save()

        self.client.get('/interfaces/', HTTP_HOST='driver.localhost')
        self.assertGreaterEqual(
            self.client.session.get_expiry_age(),
            settings.ROLE_APP_PERSONAL_SESSION_AGE - 5,
        )

    def test_new_role_pwa_metadata_is_rendered_on_each_primary_workplace(self):
        cases = (
            (self.oup_access, 'oup.localhost', '/oup/employees/', '/oup.webmanifest', 'oup-180.png'),
            (self.mechanic_access, 'mechanic.localhost', '/mechanic/downtimes/', '/mechanic.webmanifest', 'mechanic-180.png'),
            (self.manager_access, 'management.localhost', '/reports/management/', '/management.webmanifest', 'management-180.png'),
            (self.admin_access, 'admin.localhost', '/system-admin/', '/system-admin.webmanifest', 'admin-180.png'),
        )
        for access, host, path, manifest_url, icon_name in cases:
            with self.subTest(role=access.role.code):
                client = Client()
                session = client.session
                session['employee_access_id'] = access.id
                session['device_kind'] = 'personal'
                session.save()

                response = client.get(path, HTTP_HOST=host)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, manifest_url)
                self.assertContains(response, icon_name)
                self.assertContains(response, 'name="role-app-scope" content="/"')
                self.assertContains(response, '/static/js/role-app-pwa-v1.js')
