import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
import subprocess
import sys

from django.conf import settings
from django.contrib.sessions.models import Session
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
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
                self.assertIn(
                    '/static/js/role-readonly.js',
                    worker_response.content.decode('utf-8'),
                )

            with self.subTest(role=app.role_code, mode='isolated-origin'):
                role_host = f'{app.subdomain}.localhost'
                manifest_response = client.get(app.manifest_url, HTTP_HOST=role_host)
                manifest = json.loads(manifest_response.content.decode('utf-8'))
                expected_scope = '/' if app.isolated_root_scope else app.legacy_scope
                self.assertEqual(manifest['scope'], expected_scope)

                worker_response = client.get(app.service_worker_url, HTTP_HOST=role_host)
                self.assertEqual(
                    worker_response['Service-Worker-Allowed'],
                    expected_scope,
                )

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

        rejected = self.client.post(
            '/',
            {**self._credentials(self.driver_access), 'action': 'login'},
            HTTP_HOST='excavator.localhost',
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertContains(rejected, 'Телефон или пинкод указаны неверно')
        self.assertNotIn('employee_access_id', self.client.session)
        self.assertContains(rejected, 'для приложения «Экскаватор»')
        self.assertNotIn('employee_access_id', self.client.session)

        accepted = self.client.post(
            '/',
            self._credentials(self.driver_access),
            HTTP_HOST='driver.localhost',
        )
        self.assertRedirects(accepted, '/driver/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['employee_access_id'], self.driver_access.id)

    def test_shared_login_keeps_the_existing_multi_role_entry_point(self):
        response = self.client.post(
            '/',
            self._credentials(self.excavator_access),
            HTTP_HOST='localhost',
        )
        self.assertRedirects(response, '/excavator/work/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['employee_access_id'], self.excavator_access.id)

    def test_phone_step_is_returned_in_place_and_pin_uses_one_final_navigation(self):
        phone_step = self.client.post(
            '/',
            {
                'phone': self.driver_access.employee.phone,
                'action': 'continue',
                'device_kind': 'personal',
            },
            HTTP_HOST='driver.localhost',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(phone_step.status_code, 200)
        self.assertContains(phone_step, 'data-pin-input')
        self.assertNotIn('employee_access_id', self.client.session)

        pin_step = self.client.post(
            '/',
            self._credentials(self.driver_access),
            HTTP_HOST='driver.localhost',
        )

        self.assertRedirects(pin_step, '/driver/', fetch_redirect_response=False)

    def test_fetch_login_contract_returns_the_real_workplace_not_redirect_hub(self):
        response = self.client.post(
            '/',
            self._credentials(self.excavator_access),
            HTTP_HOST='excavator.localhost',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {'ok': True, 'redirect_url': '/excavator/work/'},
        )

    def test_authenticated_app_root_skips_the_redirect_only_home_route(self):
        self.client.post(
            '/',
            self._credentials(self.driver_access),
            HTTP_HOST='driver.localhost',
        )

        response = self.client.get('/', HTTP_HOST='driver.localhost')

        self.assertRedirects(response, '/driver/', fetch_redirect_response=False)

    def test_login_script_updates_only_phone_step_and_keeps_pin_cookie_navigation(self):
        response = self.client.get('/', HTTP_HOST='driver.localhost')

        self.assertContains(response, 'window.CopperUnifiedLogin')
        self.assertContains(response, 'X-Requested-With')
        self.assertContains(response, 'normalized.action !== "continue"', html=False)
        self.assertContains(response, 'form.getAttribute("action")', html=False)
        self.assertNotContains(response, 'window.fetch(form.action', html=False)
        self.assertContains(response, 'target.closest("[data-login-retry]")', html=False)
        self.assertContains(response, 'history.replaceState', html=False)
        self.assertContains(
            response,
            '&& !isEntryScreen()',
            html=False,
        )
        self.assertContains(response, '&& !isNativeApp();', html=False)
        self.assertNotContains(response, ' autofocus')

    def test_excavator_host_renders_one_combined_login_while_driver_stays_two_step(self):
        excavator_response = self.client.get('/?form=1', HTTP_HOST='excavator.localhost')
        driver_response = self.client.get('/?form=1', HTTP_HOST='driver.localhost')

        self.assertContains(
            excavator_response,
            '<form method="post" data-validated-login data-login-combined="true"',
        )
        self.assertContains(excavator_response, 'name="phone"')
        self.assertContains(excavator_response, 'name="access_code"')
        self.assertContains(excavator_response, 'value="login">Войти')
        self.assertContains(excavator_response, 'Первый вход — создать пинкод')
        self.assertNotContains(excavator_response, 'value="continue">Далее')
        self.assertContains(excavator_response, 'excavator-login-v1.css')
        self.assertContains(excavator_response, 'start-hero-v1.webp')
        self.assertNotContains(excavator_response, 'data-login-install-gate')

        self.assertNotContains(
            driver_response,
            '<form method="post" data-validated-login data-login-combined="true"',
        )
        self.assertNotContains(driver_response, 'name="access_code"')
        self.assertContains(driver_response, 'value="continue">Далее')
        self.assertNotContains(driver_response, 'excavator-login-v1.css')
        self.assertContains(driver_response, 'data-login-install-gate')

    def test_excavator_combined_post_keeps_existing_session_and_redirect(self):
        response = self.client.post(
            '/',
            {**self._credentials(self.excavator_access), 'action': 'login'},
            HTTP_HOST='excavator.localhost',
        )

        self.assertRedirects(response, '/excavator/work/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['employee_access_id'], self.excavator_access.id)
        self.assertEqual(self.client.session['active_role_access_id'], self.excavator_access.id)
        self.assertEqual(self.client.session['active_role_code'], 'excavator_operator')

    def test_excavator_combined_error_keeps_phone_and_never_reflects_pin(self):
        response = self.client.post(
            '/',
            {
                'phone': self.excavator_access.employee.phone,
                'access_code': '999999',
                'action': 'login',
                'device_kind': 'personal',
            },
            HTTP_HOST='excavator.localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.excavator_access.employee.phone)
        self.assertContains(response, 'data-login-feedback')
        self.assertContains(response, 'Телефон или пинкод указаны неверно')
        self.assertNotContains(response, 'value="999999"')
        self.assertNotIn('employee_access_id', self.client.session)

    def test_old_excavator_continue_post_remains_compatible(self):
        response = self.client.post(
            '/',
            {
                'phone': self.excavator_access.employee.phone,
                'action': 'continue',
                'device_kind': 'personal',
            },
            HTTP_HOST='excavator.localhost',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(
            response,
            '<form method="post" data-validated-login data-login-combined="true"',
        )
        self.assertContains(response, 'class="app-confirm-content unified-login-form is-pin-step"')
        self.assertContains(response, 'data-phone-input')
        self.assertContains(response, ' readonly')
        self.assertContains(response, 'data-pin-input')
        self.assertNotIn('employee_access_id', self.client.session)

    def test_excavator_first_entry_still_reaches_activation_without_pin(self):
        pending = self._create_access(
            role_code='excavator_operator',
            role_name='Машинист экскаватора',
            full_name='Новый машинист',
            phone='+79990000123',
            access_code='',
        )
        pending.status = EmployeeAccess.Status.NOT_ACTIVATED
        pending.activated_at = None
        pending.save(update_fields=['status', 'activated_at'])

        response = self.client.post(
            '/',
            {
                'phone': pending.employee.phone,
                'action': 'register',
                'device_kind': 'personal',
            },
            HTTP_HOST='excavator.localhost',
        )

        self.assertRedirects(response, '/activate-access/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['pending_activation_access_id'], pending.id)

    def test_excavator_combined_login_remains_csrf_protected(self):
        csrf_client = Client(enforce_csrf_checks=True)
        get_response = csrf_client.get('/?form=1', HTTP_HOST='excavator.localhost')
        token = get_response.cookies['csrftoken'].value

        rejected = csrf_client.post(
            '/',
            {**self._credentials(self.excavator_access), 'action': 'login'},
            HTTP_HOST='excavator.localhost',
        )
        accepted = csrf_client.post(
            '/',
            {
                **self._credentials(self.excavator_access),
                'action': 'login',
                'csrfmiddlewaretoken': token,
            },
            HTTP_HOST='excavator.localhost',
            HTTP_X_CSRFTOKEN=token,
        )

        self.assertEqual(rejected.status_code, 403)
        self.assertRedirects(accepted, '/excavator/work/', fetch_redirect_response=False)

    def test_unknown_phone_partial_has_in_place_retry_hook(self):
        response = self.client.post(
            '/',
            {
                'phone': '+79990000999',
                'action': 'continue',
                'device_kind': 'personal',
            },
            HTTP_HOST='driver.localhost',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="phone-not-found"')
        self.assertContains(response, 'data-login-retry')

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
        session.set_expiry(timezone.now() + timedelta(seconds=30))
        session.save()

        with CaptureQueriesContext(connection) as first_captured:
            response = self.client.get('/interfaces/', HTTP_HOST='driver.localhost')
        with CaptureQueriesContext(connection) as second_captured:
            second_response = self.client.get('/interfaces/', HTTP_HOST='driver.localhost')
        self.assertGreaterEqual(
            self.client.session.get_expiry_age(),
            settings.ROLE_APP_PERSONAL_SESSION_AGE - 5,
        )
        self.assertIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertNotIn(settings.SESSION_COOKIE_NAME, second_response.cookies)
        self.assertEqual(
            sum('UPDATE "django_session"' in query['sql'] for query in first_captured.captured_queries),
            1,
        )
        self.assertEqual(
            sum('UPDATE "django_session"' in query['sql'] for query in second_captured.captured_queries),
            0,
        )

    def test_legacy_relative_personal_expiry_is_converted_once_on_ordinary_page(self):
        self.client.post(
            '/',
            self._credentials(self.driver_access),
            HTTP_HOST='driver.localhost',
        )
        session = self.client.session
        session.set_expiry(settings.ROLE_APP_PERSONAL_SESSION_AGE)
        session.save()
        self.assertIsInstance(session.get('_session_expiry'), int)

        with CaptureQueriesContext(connection) as first_captured:
            response = self.client.get('/interfaces/', HTTP_HOST='driver.localhost')
        with CaptureQueriesContext(connection) as second_captured:
            second_response = self.client.get('/interfaces/', HTTP_HOST='driver.localhost')

        converted_session = self.client.session
        self.assertNotIsInstance(converted_session.get('_session_expiry'), int)
        self.assertGreaterEqual(
            converted_session.get_expiry_age(),
            settings.ROLE_APP_PERSONAL_SESSION_AGE - 5,
        )
        self.assertIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertNotIn(settings.SESSION_COOKIE_NAME, second_response.cookies)
        self.assertEqual(
            sum('UPDATE "django_session"' in query['sql'] for query in first_captured.captured_queries),
            1,
        )
        self.assertEqual(
            sum('UPDATE "django_session"' in query['sql'] for query in second_captured.captured_queries),
            0,
        )

    def test_one_hundred_personal_realtime_reads_do_not_rewrite_session(self):
        self.client.post(
            '/',
            self._credentials(self.driver_access),
            HTTP_HOST='driver.localhost',
        )
        session = self.client.session
        session_key = session.session_key
        expire_date_before = Session.objects.get(session_key=session_key).expire_date

        with CaptureQueriesContext(connection) as captured:
            responses = [
                self.client.get(
                    '/realtime/state/',
                    {'include_events': '0'},
                    HTTP_HOST='driver.localhost',
                )
                for _ in range(100)
            ]

        session_updates = [
            query['sql']
            for query in captured.captured_queries
            if 'UPDATE "django_session"' in query['sql']
        ]
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(session_updates, [])
        self.assertTrue(
            all(settings.SESSION_COOKIE_NAME not in response.cookies for response in responses)
        )
        self.assertEqual(
            Session.objects.get(session_key=session_key).expire_date,
            expire_date_before,
        )
        self.assertGreaterEqual(
            self.client.session.get_expiry_age(),
            settings.ROLE_APP_PERSONAL_SESSION_AGE - 10,
        )

    def test_shared_realtime_reads_remain_browser_close_and_do_not_rewrite_session(self):
        credentials = self._credentials(self.driver_access)
        credentials['device_kind'] = 'shared'
        self.client.post('/', credentials, HTTP_HOST='driver.localhost')

        with CaptureQueriesContext(connection) as captured:
            responses = [
                self.client.get(
                    '/realtime/state/',
                    {'include_events': '0'},
                    HTTP_HOST='driver.localhost',
                )
                for _ in range(20)
            ]

        self.assertTrue(self.client.session.get_expire_at_browser_close())
        self.assertEqual(
            sum('UPDATE "django_session"' in query['sql'] for query in captured.captured_queries),
            0,
        )
        self.assertTrue(
            all(settings.SESSION_COOKIE_NAME not in response.cookies for response in responses)
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
