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
                self.assertContains(response, 'shared-shift-dialog unified-login-dialog')
                self.assertContains(response, 'class="app-confirm-content unified-login-form')
                self.assertContains(response, 'data-phone-input')
                self.assertNotContains(response, ' autofocus')
                self.assertContains(response, 'window.CopperUnifiedLogin')
                self.assertNotContains(response, '<span aria-hidden="true">↓</span>')
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
        self.assertIn('dialog.scrollLeft = 0', template)
        self.assertIn('window.requestAnimationFrame(resetLoginHorizontalScroll)', template)
        self.assertNotIn('window.setTimeout(resetLoginHorizontalScroll, 350)', template)
        self.assertNotIn('window.scrollTo(', template)
        self.assertNotIn("behavior: 'smooth'", template)
        self.assertNotIn('input.scrollIntoView', template)
        self.assertIn(
            '.login-page.unified-login-screen .unified-login-form > *',
            stylesheet,
        )
        self.assertIn('overflow-wrap: anywhere', stylesheet)
        self.assertIn('white-space: normal', stylesheet)

    def test_fullscreen_role_login_owns_exact_viewport_without_page_scroll(self):
        template = (
            Path(settings.BASE_DIR) / 'templates' / 'users' / 'login.html'
        ).read_text(encoding='utf-8')

        self.assertIn('body.login-page.login-fullscreen {', template)
        self.assertIn('height: 100dvh;', template)
        self.assertIn('min-height: 0;', template)
        self.assertIn('width: 100%;', template)
        self.assertIn('padding: 0;', template)
        self.assertIn('overflow: hidden;', template)
        self.assertIn('overscroll-behavior: none;', template)
        self.assertIn('transform: none;', template)
        self.assertIn('transform-origin: initial;', template)
        self.assertIn(
            'body.login-page.login-fullscreen .shared-shift-dialog.unified-login-dialog',
            template,
        )
        self.assertIn('height: 100%;', template)
        self.assertIn(
            '@media (orientation: landscape) and (max-height: 500px)',
            template,
        )
        self.assertIn(
            'grid-template-columns: minmax(132px, .75fr) minmax(0, 1.25fr);',
            template,
        )
        self.assertIn('grid-template-rows: minmax(0, 1fr) auto;', template)
        self.assertIn('grid-row: 1 / span 4;', template)

    def test_pwa_install_intent_keeps_button_and_status_copy_stable(self):
        response = Client().get(
            '/?install=1',
            HTTP_HOST='driver.localhost',
        )
        script = (
            Path(settings.BASE_DIR) / 'static' / 'js' / 'role-app-install-v1.js'
        ).read_text(encoding='utf-8')

        self.assertContains(response, 'data-install-button>Установить приложение</button>')
        self.assertContains(response, 'Нажмите кнопку, чтобы продолжить установку.')
        self.assertContains(response, 'role-app-install-v1.js?v=5')
        self.assertEqual(script.count('? installIntentStatus'), 2)

    @override_settings(
        SUPPORT_CHAT_URL='https://max.ru/u/test',
        SUPPORT_CHAT_LABEL='Написать администратору в MAX',
    )
    def test_max_support_uses_one_mobile_dock_across_login_states(self):
        response = Client().get('/', HTTP_HOST='driver.localhost')
        backend_root = Path(settings.BASE_DIR)
        template = (
            backend_root / 'templates' / 'users' / 'login.html'
        ).read_text(encoding='utf-8')
        stylesheet = (
            backend_root / 'static' / 'css' / 'max-support-link-v1.css'
        ).read_text(encoding='utf-8')

        self.assertContains(response, 'data-support-channel="max"', count=1)
        self.assertContains(response, 'max-support-link-v1.css?v=6')
        self.assertIn(
            '</section>\n    {% endif %}\n    {% comment %}\n    Общий нижний док',
            template,
        )
        self.assertIn('position: fixed', stylesheet)
        self.assertIn('bottom: calc(env(safe-area-inset-bottom, 0px) + 10px)', stylesheet)
        self.assertIn('max-width: calc(100vw - 32px)', stylesheet)
        self.assertIn('min-height: 40px', stylesheet)
        self.assertNotIn('body.is-start-keyboard-active .max-support-link', stylesheet)
        self.assertNotIn('body.is-login-keyboard-active .max-support-link', stylesheet)
        self.assertNotIn(
            'body.login-page .unified-login-form:focus-within ~ .max-support-link',
            stylesheet,
        )
        self.assertIn('function syncLoginKeyboardModeFromViewport()', template)
        self.assertIn('loginViewportBaselineHeight', template)
        self.assertIn('loginViewportBaselineHeight * 0.22', template)
        self.assertIn('&& hasFocusedLoginInput()', template)
        self.assertIn('&& keyboardShrink >= keyboardThreshold', template)
        self.assertIn('window.visualViewport.addEventListener(', template)
        self.assertNotIn('setLoginKeyboardMode(true)', template)

    def test_pin_step_moves_the_visual_focus_from_phone_to_pin(self):
        template = (
            Path(settings.BASE_DIR) / 'templates' / 'users' / 'login.html'
        ).read_text(encoding='utf-8')

        self.assertIn(
            "{% if login_step == 'pin' and not combined_excavator_login %} is-pin-step{% endif %}",
            template,
        )
        self.assertIn(
            '.unified-login-form.is-pin-step .shared-shift-phone-shell',
            template,
        )
        self.assertIn(
            '.login-pin-shell:focus-within',
            template,
        )
        self.assertIn('border-color: var(--login-accent)', template)

    def test_phone_and_pin_share_one_height_token_in_keyboard_mode(self):
        template = (
            Path(settings.BASE_DIR) / 'templates' / 'users' / 'login.html'
        ).read_text(encoding='utf-8')

        self.assertIn('--login-control-height: 58px', template)
        self.assertIn('--login-control-height: 42px', template)
        self.assertIn('--login-reveal-size: 44px', template)
        self.assertIn('--login-reveal-size: 40px', template)
        self.assertIn('width: var(--login-reveal-size)', template)
        self.assertIn('height: var(--login-reveal-size)', template)
        self.assertEqual(
            template.count('min-height: var(--login-control-height)'),
            3,
        )

    def test_excavator_combined_login_owns_visual_viewport_and_keyboard_contract(self):
        backend_root = Path(settings.BASE_DIR)
        template = (
            backend_root / 'templates' / 'users' / 'login.html'
        ).read_text(encoding='utf-8')
        stylesheet = (
            backend_root / 'static' / 'css' / 'excavator-login-v1.css'
        ).read_text(encoding='utf-8')

        self.assertIn('data-login-combined="true"', template)
        self.assertIn('enterkeyhint="next"', template)
        self.assertIn('enterkeyhint="go"', template)
        self.assertIn('visualViewport.addEventListener(', template)
        self.assertIn('"scroll",', template)
        self.assertIn('--login-vv-height', template)
        self.assertIn('--login-vv-top', template)
        self.assertIn('position: fixed', stylesheet)
        self.assertIn('height: var(--login-vv-height, 100dvh)', stylesheet)
        self.assertIn('.is-login-keyboard-active .excavator-login__hero', stylesheet)
        self.assertIn('.is-login-keyboard-active .max-support-link', stylesheet)
        self.assertIn('overflow: hidden', stylesheet)

    def test_excavator_combined_login_never_persists_pin(self):
        template = (
            Path(settings.BASE_DIR) / 'templates' / 'users' / 'login.html'
        ).read_text(encoding='utf-8')

        self.assertIn('JSON.stringify({phone: phoneDigits || ""})', template)
        self.assertIn('Object.prototype.hasOwnProperty.call(remembered, "pin")', template)
        self.assertIn('window.localStorage.removeItem(LOGIN_MEMORY_KEY)', template)
        self.assertIn('requestedAction === "register" && pinField', template)
        self.assertNotIn('pin: pinDigits', template)
        self.assertNotIn('memory.pin', template)
