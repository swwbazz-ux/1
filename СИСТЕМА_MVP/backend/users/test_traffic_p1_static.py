import json
import re
from pathlib import Path

from django.conf import settings
from django.test import Client, SimpleTestCase, override_settings

from .role_apps import READY_TRAFFIC_ROLE_CODES, ROLE_APPS_BY_CODE, STATIC_ASSET_RELEASE


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
EXPECTED_RELEASE = STATIC_ASSET_RELEASE
DISPATCHER_STYLE_FILES = (
    'dispatcher-control-v1.css',
    'dispatcher-workspace-v1.css',
    'dispatcher-detail-v1.css',
    'dispatcher-adaptive-v1.css',
    'dispatcher-detail-overrides-v1.css',
)


@override_settings(ALLOWED_HOSTS=['localhost', '.localhost'])
class StableStaticReleaseTrafficRegressionTests(SimpleTestCase):
    def test_dispatcher_blocking_recovery_keeps_hidden_label_out_of_layout(self):
        css = ''.join(
            (Path(settings.BASE_DIR) / 'static' / 'css' / filename).read_text(
                encoding='utf-8'
            )
            for filename in DISPATCHER_STYLE_FILES
        )

        self.assertIn(
            '.dispatcher-blocking-shift-recovery .visually-hidden',
            css,
        )
        self.assertIn('position: absolute !important;', css)
        self.assertIn('clip: rect(0, 0, 0, 0) !important;', css)

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
                self.assertNotIn('"/static/css/app.css"', core_assets.group(1))
                if role_code == 'dispatcher':
                    self.assertEqual(
                        script.count('self.addEventListener("install"'),
                        1,
                    )
                    self.assertNotIn('Promise.allSettled', script)
                    self.assertIn(
                        f'/static/js/realtime-client.js?v={EXPECTED_RELEASE}',
                        core_assets.group(1),
                    )
                    for stylesheet in DISPATCHER_STYLE_FILES:
                        self.assertIn(
                            f'/static/css/{stylesheet}',
                            core_assets.group(1),
                        )
                    self.assertIn('/static/js/dispatcher-control-v1.js', core_assets.group(1))
                    self.assertIn('/static/js/dispatcher-detail-v1.js', core_assets.group(1))
                    self.assertIn('/static/js/dispatcher-board-v1.js', core_assets.group(1))
                    self.assertIn('/static/js/dispatcher-realtime-v1.js', core_assets.group(1))
                    self.assertIn('/static/js/dispatcher-sounds-v1.js', core_assets.group(1))
                else:
                    self.assertEqual(
                        script.count('self.addEventListener("install"'),
                        2,
                    )
                    self.assertNotIn('"/static/js/realtime-client.js"', core_assets.group(1))
                    self.assertIn('cache.delete(path)', script)
                declared_paths = re.search(
                    r'const RELEASE_STATIC_PATHS = new Set\((\[[^;]*\])\);', script,
                )
                self.assertIsNotNone(declared_paths)
                expected_paths = {
                    '/static/js/realtime-client.js',
                    '/static/js/connection-indicators-v1.js',
                }
                if role_code != 'dispatcher':
                    expected_paths.add('/static/css/app.css')
                if role_code in {'driver', 'excavator_operator'}:
                    expected_paths.update({
                        '/static/js/client-error-report.js',
                        '/static/js/application-session-heartbeat.js',
                        '/static/js/native-background-connection-v1.js',
                    })
                self.assertEqual(set(json.loads(declared_paths.group(1))), expected_paths)
                for core_url in re.findall(r'"([^"\n]+)"', core_assets.group(1)):
                    path = core_url.split('?', 1)[0]
                    if path in expected_paths:
                        self.assertEqual(core_url, f'{path}?v={EXPECTED_RELEASE}')
                self.assertIn('request.mode === "navigate"', script)
                self.assertIn('networkFirstStatic(request)', script)

    def test_dispatcher_precaches_every_declared_release_asset(self):
        response = Client().get('/dispatcher-sw.js', HTTP_HOST='dispatcher.localhost')
        self.assertEqual(response.status_code, 200)
        script = response.content.decode('utf-8')
        paths = re.search(r'const RELEASE_STATIC_PATHS = new Set\((\[[^;]*\])\);', script)
        core_assets = re.search(r'const CORE_ASSETS = (\[[\s\S]*?\]);', script)
        self.assertIsNotNone(paths)
        self.assertIsNotNone(core_assets)
        for path in re.findall(r'"([^"\n]+)"', paths.group(1)):
            with self.subTest(asset=path):
                self.assertIn(f'"{path}?v={EXPECTED_RELEASE}"', core_assets.group(1))

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
