from django.test import Client, SimpleTestCase, override_settings

from .app_catalog import APP_CATALOG_ROLE_CODES, role_app_public_url


CATALOG_HOST_SETTINGS = override_settings(
    ALLOWED_HOSTS=['driverform.ru', '.driverform.ru', 'localhost', '.localhost']
)


@CATALOG_HOST_SETTINGS
class AppCatalogTests(SimpleTestCase):
    expected_labels = (
        'Горный диспетчер',
        'Машинист экскаватора',
        'Водитель самосвала',
        'Горный мастер',
        'Заместитель начальника участка',
        'ОУП',
        'Руководство',
        'Системный администратор',
    )

    def test_public_catalog_contains_exactly_eight_approved_apps(self):
        response = Client().get('/apps/', HTTP_HOST='driverform.ru', secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['catalog_apps']), 8)
        self.assertEqual(response.content.count(b'data-app-card'), 8)
        for label in self.expected_labels:
            self.assertContains(response, label)
        for excluded in (
            'Делопроизводитель',
            'Табельщик',
            'Начальник участка',
            'Механическая служба',
        ):
            self.assertNotContains(response, excluded)

        self.assertNotContains(response, 'демо-код')
        self.assertNotContains(response, 'rel="manifest"')
        self.assertNotContains(response, 'serviceWorker.register')

    def test_catalog_production_links_use_exact_role_subdomains(self):
        response = Client().get('/apps/', HTTP_HOST='driverform.ru', secure=True)
        expected_hosts = {
            'dispatcher': 'dispatcher',
            'excavator_operator': 'excavator',
            'driver': 'driver',
            'mining_master': 'mining-master',
            'deputy_mining_manager': 'deputy',
            'oup': 'oup',
            'manager': 'management',
            'admin': 'admin',
        }

        items = {item['role_code']: item for item in response.context['catalog_apps']}
        self.assertEqual(tuple(items), APP_CATALOG_ROLE_CODES)
        for role_code, subdomain in expected_hosts.items():
            with self.subTest(role=role_code):
                self.assertEqual(
                    items[role_code]['target_url'],
                    f'https://{subdomain}.driverform.ru/',
                )

    def test_local_catalog_preserves_port_and_uses_local_role_origins(self):
        response = Client().get('/apps/', HTTP_HOST='localhost:8000')
        items = {item['role_code']: item for item in response.context['catalog_apps']}
        expected_hosts = {
            'dispatcher': 'dispatcher',
            'excavator_operator': 'excavator',
            'driver': 'driver',
            'mining_master': 'mining-master',
            'deputy_mining_manager': 'deputy',
            'oup': 'oup',
            'manager': 'management',
            'admin': 'admin',
        }

        for role_code, subdomain in expected_hosts.items():
            with self.subTest(role=role_code):
                self.assertEqual(
                    items[role_code]['target_url'],
                    f'http://{subdomain}.localhost:8000/',
                )

    def test_qr_is_generated_locally_only_for_approved_apps(self):
        client = Client()
        for role_code in APP_CATALOG_ROLE_CODES:
            with self.subTest(role=role_code):
                response = client.get(
                    f'/apps/qr/{role_code}/',
                    HTTP_HOST='driverform.ru',
                    secure=True,
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response['Content-Type'].startswith('image/svg+xml'))
                self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
                self.assertIn('Host', response['Vary'])
                self.assertIn(b'<svg', response.content)

        for role_code in ('settlement_clerk', 'timekeeper', 'site_manager', 'mechanic'):
            with self.subTest(excluded_role=role_code):
                self.assertEqual(
                    client.get(
                        f'/apps/qr/{role_code}/',
                        HTTP_HOST='driverform.ru',
                    ).status_code,
                    404,
                )

    def test_role_login_exposes_install_control_but_shared_login_does_not(self):
        role_response = Client().get('/', HTTP_HOST='driver.localhost')
        shared_response = Client().get('/', HTTP_HOST='localhost')

        self.assertContains(role_response, 'data-role-app-install')
        self.assertContains(role_response, '/static/js/role-app-install-v1.js')
        self.assertContains(role_response, 'Установить приложение')
        self.assertNotContains(shared_response, 'data-role-app-install')
        self.assertContains(role_response, 'href="http://localhost/apps/"')
        self.assertContains(shared_response, 'href="http://localhost/apps/"')

    def test_catalog_redirects_out_of_every_role_pwa_root_scope(self):
        for subdomain in (
            'dispatcher',
            'excavator',
            'driver',
            'mining-master',
            'deputy',
            'oup',
            'management',
            'admin',
        ):
            with self.subTest(subdomain=subdomain):
                response = Client().get('/apps/', HTTP_HOST=f'{subdomain}.localhost:8000')
                self.assertRedirects(
                    response,
                    'http://localhost:8000/apps/',
                    fetch_redirect_response=False,
                )

    def test_unknown_host_falls_back_to_secure_production_origin(self):
        request = type('Request', (), {'get_host': lambda self: 'example.invalid'})()
        self.assertEqual(
            role_app_public_url(request, 'driver'),
            'https://driver.driverform.ru/',
        )
