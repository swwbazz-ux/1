from pathlib import Path

from django.conf import settings
from django.test import Client, SimpleTestCase, override_settings

from .role_apps import ENTRY_SCREEN_BROWSER_BAR, ROLE_APPS


@override_settings(ALLOWED_HOSTS=['localhost', '.localhost'])
class LoginMobileOverflowContractTests(SimpleTestCase):
    def test_all_role_origins_render_the_shared_mobile_login_contract(self):
        self.assertEqual(len(ROLE_APPS), 12)

        for app in ROLE_APPS:
            with self.subTest(role=app.role_code, host=app.subdomain):
                response = Client().get(
                    '/',
                    HTTP_HOST=f'{app.subdomain}.localhost',
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, app.name)
                self.assertContains(
                    response,
                    '<main class="shared-shift-dialog unified-login-dialog">',
                )
                self.assertContains(response, 'class="app-confirm-content unified-login-form"')
                self.assertContains(response, 'data-phone-input')
                self.assertContains(response, 'autofocus')
                self.assertContains(
                    response,
                    f'<meta name="theme-color" content="{ENTRY_SCREEN_BROWSER_BAR}">',
                    count=1,
                )

    def test_login_assets_keep_focus_scrolling_vertical_only(self):
        backend_root = Path(settings.BASE_DIR)
        template = (
            backend_root / 'templates' / 'users' / 'login.html'
        ).read_text(encoding='utf-8')
        stylesheet = (
            backend_root / 'static' / 'css' / 'app.css'
        ).read_text(encoding='utf-8')

        self.assertIn('function resetLoginHorizontalScroll()', template)
        self.assertIn('loginDialog.scrollLeft = 0', template)
        self.assertIn('window.setTimeout(resetLoginHorizontalScroll, 350)', template)
        self.assertNotIn('input.scrollIntoView', template)
        self.assertIn(
            '.login-page.unified-login-screen .unified-login-form > *',
            stylesheet,
        )
        self.assertIn('overflow-wrap: anywhere', stylesheet)
        self.assertIn('white-space: normal', stylesheet)
