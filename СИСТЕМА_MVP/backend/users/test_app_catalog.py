import re
from pathlib import Path

from django.conf import settings
from django.test import Client, SimpleTestCase, override_settings
from PIL import Image

from .app_catalog import (
    APP_CATALOG_QR_SIZE,
    APP_CATALOG_ROLE_CODES,
    render_role_app_qr_png,
    role_app_public_url,
    role_app_qr_asset_path,
    role_app_qr_target_url,
)


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
        html = response.content.decode('utf-8')
        self.assertEqual(len(re.findall(r'<button\b[^>]*\bdata-app-card\b', html, re.S)), 8)
        self.assertEqual(len(re.findall(r'<a\b[^>]*\bdata-app-card\b', html, re.S)), 0)
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
        self.assertContains(response, '/static/css/app-catalog-v1.css?v=4')
        self.assertContains(response, '/static/js/app-catalog-v1.js?v=4')
        self.assertContains(response, 'data-share-link')
        self.assertContains(response, 'Отправить ссылку')
        self.assertContains(response, 'Подключить', count=8)

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
                self.assertEqual(
                    items[role_code]['qr_target_url'],
                    f'https://{subdomain}.driverform.ru/',
                )
                self.assertEqual(
                    items[role_code]['qr_asset_url'],
                    f'/static/img/pwa/qr/{role_code}.png',
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
                self.assertEqual(
                    items[role_code]['qr_target_url'],
                    f'https://{subdomain}.driverform.ru/',
                )

    def test_qr_compatibility_endpoint_redirects_to_static_asset(self):
        client = Client()
        for role_code in APP_CATALOG_ROLE_CODES:
            with self.subTest(role=role_code):
                response = client.get(
                    f'/apps/qr/{role_code}/',
                    HTTP_HOST='driverform.ru',
                    secure=True,
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(
                    response.url,
                    f'/static/img/pwa/qr/{role_code}.png',
                )
                self.assertEqual(response['X-Content-Type-Options'], 'nosniff')

        for role_code in ('settlement_clerk', 'timekeeper', 'site_manager', 'mechanic'):
            with self.subTest(excluded_role=role_code):
                self.assertEqual(
                    client.get(
                        f'/apps/qr/{role_code}/',
                        HTTP_HOST='driverform.ru',
                    ).status_code,
                    404,
                )

    def test_committed_qr_assets_are_exact_integer_grid_pngs(self):
        static_root = Path(settings.BASE_DIR) / 'static'
        for role_code in APP_CATALOG_ROLE_CODES:
            with self.subTest(role=role_code):
                target_url = role_app_qr_target_url(role_code)
                asset_path = static_root / role_app_qr_asset_path(role_code)
                asset_bytes = asset_path.read_bytes()
                self.assertEqual(asset_bytes, render_role_app_qr_png(target_url))

                with Image.open(asset_path) as image:
                    self.assertEqual(image.format, 'PNG')
                    self.assertEqual(image.mode, 'RGB')
                    self.assertEqual(image.size, (APP_CATALOG_QR_SIZE, APP_CATALOG_QR_SIZE))
                    self.assertLessEqual(
                        set(image.getdata()),
                        {(0, 0, 0), (255, 255, 255)},
                    )
                    black_bounds = image.point(lambda value: 255 - value).getbbox()
                    self.assertIsNotNone(black_bounds)
                    self.assertGreaterEqual(min(black_bounds[:2]), 32)
                    self.assertLessEqual(max(black_bounds[2:]), APP_CATALOG_QR_SIZE - 32)

    def test_role_login_exposes_install_control_but_shared_login_does_not(self):
        role_response = Client().get('/', HTTP_HOST='driver.localhost')
        shared_response = Client().get('/', HTTP_HOST='localhost')

        self.assertContains(role_response, 'data-role-app-install')
        self.assertContains(role_response, '/static/css/role-app-install-v1.css?v=2')
        self.assertContains(role_response, '/static/js/role-app-install-v1.js?v=2')
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
