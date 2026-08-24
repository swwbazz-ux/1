import re
from pathlib import Path

from django.conf import settings
from django.test import Client, SimpleTestCase, override_settings

from .role_apps import READY_TRAFFIC_ROLE_CODES, ROLE_APPS_BY_CODE


READY_ROLE_CODES = (
    'admin',
    'oup',
    'deputy_mining_manager',
    'dispatcher',
    'mining_master',
    'excavator_operator',
    'driver',
    'manager',
)
EXPECTED_RELEASE = 'ready-core-traffic-v8'


@override_settings(ALLOWED_HOSTS=['localhost', '.localhost'])
class StableStaticReleaseTrafficRegressionTests(SimpleTestCase):
    def test_base_uses_one_stable_release_url_across_repeated_rendering(self):
        first = Client().get('/', HTTP_HOST='driver.localhost')
        second = Client().get('/', HTTP_HOST='driver.localhost')

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_html = first.content.decode('utf-8')
        second_html = second.content.decode('utf-8')
        asset_pattern = re.compile(
            r'(?:css/app\.css|js/realtime-client\.js)\?v=[^"\']+'
        )
        first_urls = asset_pattern.findall(first_html)
        second_urls = asset_pattern.findall(second_html)
        self.assertEqual(first_urls, second_urls)
        self.assertEqual(len(first_urls), 2)
        self.assertTrue(
            all(url.endswith(f'?v={EXPECTED_RELEASE}') for url in first_urls)
        )

        base_source = (
            Path(settings.BASE_DIR) / 'templates' / 'base.html'
        ).read_text(encoding='utf-8')
        self.assertNotIn("{% now 'U' %}", base_source)

    def test_all_eight_ready_workers_cache_only_exact_release_assets_first(self):
        self.assertEqual(set(READY_ROLE_CODES), set(READY_TRAFFIC_ROLE_CODES))
        for role_code in READY_ROLE_CODES:
            app = ROLE_APPS_BY_CODE[role_code]
            with self.subTest(role=role_code):
                response = Client().get(
                    app.service_worker_url,
                    HTTP_HOST=f'{app.subdomain}.localhost',
                )
                script = response.content.decode('utf-8')
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    f'const STATIC_ASSET_RELEASE = "{EXPECTED_RELEASE}";',
                    script,
                )
                self.assertIn('async function cacheFirstReleaseStatic(request)', script)
                self.assertIn(
                    'isReleaseStaticRequest(url) ? cacheFirstReleaseStatic(request)',
                    script,
                )
                core_assets = re.search(
                    r'const CORE_ASSETS = (\[[\s\S]*?\]);',
                    script,
                )
                self.assertIsNotNone(core_assets)
                self.assertNotIn('/static/css/app.css', core_assets.group(1))
                self.assertNotIn('/static/js/realtime-client.js', core_assets.group(1))
                self.assertIn('cache.delete(path)', script)
                self.assertIn('request.mode === "navigate"', script)
                self.assertIn('networkFirstStatic(request)', script)

    def test_unfinished_role_workers_do_not_receive_ready_core_cache_contract(self):
        for role_code in sorted(set(ROLE_APPS_BY_CODE) - set(READY_ROLE_CODES)):
            app = ROLE_APPS_BY_CODE[role_code]
            with self.subTest(role=role_code):
                response = Client().get(
                    app.service_worker_url,
                    HTTP_HOST=f'{app.subdomain}.localhost',
                )
                script = response.content.decode('utf-8')
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('STATIC_ASSET_RELEASE', script)
                self.assertNotIn('cacheFirstReleaseStatic', script)
