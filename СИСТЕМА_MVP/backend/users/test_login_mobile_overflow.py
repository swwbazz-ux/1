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

    def test_combined_driver_and_excavator_phone_step_has_no_consent_control(self):
        for host in ('driver.localhost', 'excavator.localhost'):
            with self.subTest(host=host):
                response = Client().get('/', HTTP_HOST=host)

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'data-login-combined="true"')
                self.assertContains(response, 'data-login-step="phone"', count=1)
                self.assertContains(response, 'name="action" value="continue"', count=1)
                self.assertNotContains(response, 'id="login-privacy-consent"')
                self.assertNotContains(response, 'name="privacy_consent"')
                self.assertNotContains(response, 'class="mobile-role-login__consent-panel"')
                self.assertNotContains(
                    response,
                    '<dialog class="mobile-role-login__privacy-dialog"',
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
            HTTP_HOST='mining-master.localhost',
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
            "{% if login_step == 'pin' %} is-pin-step{% elif login_step == 'consent' %} is-consent-step{% elif combined_mobile_login %} is-phone-step{% endif %}",
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

    def test_combined_mobile_login_owns_visual_viewport_and_keyboard_contract(self):
        backend_root = Path(settings.BASE_DIR)
        template = (
            backend_root / 'templates' / 'users' / 'login.html'
        ).read_text(encoding='utf-8')
        stylesheet = (
            backend_root / 'static' / 'css' / 'mobile-role-login-v1.css'
        ).read_text(encoding='utf-8')

        self.assertIn('data-login-combined="true"', template)
        self.assertNotIn('registerButton', template)
        self.assertIn("enterkeyhint=\"{% if login_step == 'pin' %}done{% else %}next{% endif %}\"", template)
        self.assertIn('enterkeyhint="go"', template)
        self.assertIn('visualViewport.addEventListener(', template)
        self.assertIn('"scroll",', template)
        self.assertIn('--login-vv-height', template)
        self.assertIn('--login-vv-top', template)
        self.assertIn('position: fixed', stylesheet)
        self.assertIn('height: var(--login-vv-height, 100dvh)', stylesheet)
        self.assertIn('.is-login-keyboard-active .mobile-role-login__hero', stylesheet)
        self.assertIn('.is-login-keyboard-active .max-support-link', stylesheet)
        self.assertIn('overflow: hidden', stylesheet)
        self.assertIn(
            '@media (orientation: landscape) and (max-width: 1023px)',
            stylesheet,
        )
        self.assertIn(
            '(orientation: landscape) and (min-width: 1024px) and (max-height: 679px)',
            stylesheet,
        )
        self.assertIn(
            '@media (orientation: landscape) and (max-height: 360px)',
            stylesheet,
        )
        self.assertIn(
            'grid-template-columns: repeat(2, minmax(0, 1fr)) !important',
            stylesheet,
        )
        self.assertIn('grid-column: 1 / -1 !important', stylesheet)
        self.assertIn(
            'grid-template-rows: minmax(142px, min(32dvh, 340px)) minmax(max-content, 1fr) max-content;',
            stylesheet,
        )
        self.assertIn(
            'grid-template-rows: 128px minmax(max-content, 1fr) 38px;',
            stylesheet,
        )
        self.assertIn(
            '.unified-login-form.is-combined-login .mobile-role-login__lead',
            stylesheet,
        )
        self.assertIn(
            '@media (orientation: landscape) and (max-height: 320px)',
            stylesheet,
        )
        self.assertIn('max-height: 100%', stylesheet)
        self.assertIn(
            '@media (orientation: portrait) and (max-width: 1023px)',
            stylesheet,
        )
        self.assertIn('--mobile-login-control: 40px', stylesheet)
        self.assertIn(
            'is-login-a11y-overflow .unified-login-dialog.mobile-role-login',
            stylesheet,
        )
        self.assertIn('.login-combined.login-role-driver {', stylesheet)
        self.assertIn('--mobile-login-accent: var(--login-accent, #ffd200)', stylesheet)
        self.assertIn('--mobile-login-button-top: #60e3d6', stylesheet)
        self.assertIn(
            'grid-template-columns: minmax(0, .65fr) minmax(0, 1.35fr)',
            stylesheet,
        )
        self.assertNotIn('minmax(320px, 1.28fr)', stylesheet)
        self.assertNotIn('excavator-login', stylesheet)

    def test_consent_step_separates_controls_and_opens_a_returnable_dialog(self):
        backend_root = Path(settings.BASE_DIR)
        template = (
            backend_root / 'templates' / 'users' / 'login.html'
        ).read_text(encoding='utf-8')
        stylesheet = (
            backend_root / 'static' / 'css' / 'mobile-role-login-v1.css'
        ).read_text(encoding='utf-8')

        self.assertIn("{% if combined_mobile_login and login_step == 'consent' %}", template)
        self.assertIn('class="mobile-role-login__consent-panel"', template)
        self.assertIn('name="privacy_consent"', template)
        self.assertIn('value="{{ privacy_policy_version }}"', template)
        self.assertIn('data-privacy-consent', template)
        self.assertIn(
            '</label>\n            <a class="mobile-role-login__privacy-link"',
            template,
        )
        self.assertIn("{% url 'portal:public_privacy' %}?from=role-login", template)
        self.assertIn('data-login-privacy-dialog', template)
        self.assertIn('data-login-privacy-close', template)
        self.assertIn('dialog.showModal()', template)
        self.assertIn('dialog.addEventListener("cancel"', template)
        self.assertIn('closeDialog()', template)
        self.assertIn('consentField.focus({preventScroll: true})', template)

        self.assertIn(
            'body.login-page.login-fullscreen.login-combined.login-step-consent .mobile-role-login__consent {\n'
            '    min-height: 48px;',
            stylesheet,
        )
        self.assertIn(
            'body.login-page.login-fullscreen.login-combined.login-step-consent .mobile-role-login__privacy-link {\n'
            '    min-height: 44px;',
            stylesheet,
        )
        self.assertIn(
            '.mobile-role-login__privacy-dialog {\n'
            '    inset: 0;\n'
            '    width: 100%;\n'
            '    max-width: none;\n'
            '    height: 100dvh;',
            stylesheet,
        )
        self.assertIn(
            '.mobile-role-login__privacy-toolbar button,\n'
            '.mobile-role-login__privacy-return {\n'
            '    min-height: 44px;',
            stylesheet,
        )
        self.assertIn('.mobile-role-login__privacy-document {', stylesheet)
        self.assertIn('overflow-y: auto', stylesheet)

    def test_phone_and_consent_steps_use_distinct_continue_actions(self):
        template = (
            Path(settings.BASE_DIR) / 'templates' / 'users' / 'login.html'
        ).read_text(encoding='utf-8')

        self.assertIn(
            "value=\"{% if login_step == 'pin' %}login{% elif login_step == 'consent' %}consent{% else %}continue{% endif %}\"",
            template,
        )
        self.assertIn(
            'if (normalized.action !== "continue" && normalized.action !== "consent") return;',
            template,
        )
        self.assertIn(
            'if (requestedAction === "continue" || requestedAction === "consent")',
            template,
        )

    def test_combined_mobile_login_never_persists_pin(self):
        template = (
            Path(settings.BASE_DIR) / 'templates' / 'users' / 'login.html'
        ).read_text(encoding='utf-8')

        self.assertIn('JSON.stringify({phone: phoneDigits || ""})', template)
        self.assertIn('Object.prototype.hasOwnProperty.call(remembered, "pin")', template)
        self.assertIn('window.localStorage.removeItem(LOGIN_MEMORY_KEY)', template)
        self.assertIn('requestedAction === "continue"', template)
        self.assertNotIn('unified-login-register', template)
        self.assertNotIn('pin: pinDigits', template)
        self.assertNotIn('memory.pin', template)

    def test_mobile_first_entry_fits_standard_viewport_without_clipping_contract(self):
        backend_root = Path(settings.BASE_DIR)
        template = (
            backend_root / 'templates' / 'users' / 'activate_access.html'
        ).read_text(encoding='utf-8')
        stylesheet = (
            backend_root / 'static' / 'css' / 'mobile-role-activation-v1.css'
        ).read_text(encoding='utf-8')
        login_stylesheet = (
            backend_root / 'static' / 'css' / 'mobile-role-login-v1.css'
        ).read_text(encoding='utf-8')

        self.assertIn('data-mobile-role-activation', template)
        self.assertIn('mobile-role-login-v1.css', template)
        self.assertIn('mobile-role-activation-v1.css', template)
        self.assertIn('<h1>Придумайте PIN</h1>', template)
        self.assertIn('Создать PIN и войти', template)
        self.assertIn('Это не я — ввести другой номер', template)
        self.assertNotIn(' autofocus', template)

        self.assertIn(
            'body.login-page.login-fullscreen.login-combined.login-activation {\n'
            '    height: var(--login-vv-height, 100svh);\n'
            '    min-height: 0;\n'
            '    overflow-x: hidden;\n'
            '    overflow-y: auto;',
            stylesheet,
        )
        self.assertIn(
            'body.login-page.login-fullscreen.login-combined.login-activation .unified-login-dialog.mobile-role-activation {\n'
            '    position: relative;',
            stylesheet,
        )
        self.assertIn(
            'grid-template-rows: clamp(112px, 16svh, 145px) minmax(max-content, 1fr) 48px;\n'
            '    width: 100%;\n'
            '    min-height: 0;\n'
            '    height: var(--login-vv-height, 100svh);\n'
            '    padding-bottom: max(12px, env(safe-area-inset-bottom, 0px));\n'
            '    overflow: visible;',
            stylesheet,
        )
        self.assertNotIn('height: auto', stylesheet)
        self.assertNotIn('overflow-y: hidden', stylesheet)

        self.assertIn(
            '.shared-shift-login-field input {\n'
            '    min-height: 50px;',
            stylesheet,
        )
        self.assertIn(
            '.activation-pin-toggle {\n'
            '    display: inline-grid;',
            stylesheet,
        )
        self.assertIn('    min-height: 44px;', stylesheet)
        self.assertIn(
            '.unified-login-submit {\n'
            '    min-height: 52px;',
            stylesheet,
        )
        self.assertIn(
            '.unified-login-change-phone {\n'
            '    min-height: 44px;',
            stylesheet,
        )
        self.assertIn(
            '.max-support-link {\n'
            '    height: 48px;\n'
            '    min-height: 48px;',
            stylesheet,
        )

        self.assertIn('@media (max-width: 360px) and (max-height: 640px)', stylesheet)
        self.assertIn('grid-template-rows: 84px minmax(max-content, 1fr) 48px;', stylesheet)
        self.assertIn('padding: 7px 10px;', stylesheet)
        self.assertIn(
            '.shared-shift-login-field input {\n'
            '        min-height: 44px;',
            stylesheet,
        )
        self.assertIn(
            '.unified-login-submit {\n'
            '        min-height: 48px;',
            stylesheet,
        )
        self.assertIn('min-height: clamp(52px, 7.2dvh, 64px)', login_stylesheet)
