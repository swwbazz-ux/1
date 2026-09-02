"""Общий вход /start/: что человек видит после ввода номера.

Две вещи, на которых страница врала совмещающему роли: обещала завести пинкод
тому, у кого он уже есть, и вываливала все приложения одним списком.
"""

import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Employee, EmployeeAccess, Role
from .role_apps import ENTRY_SCREEN_BROWSER_BAR


ANDROID_USER_AGENT = (
    'Mozilla/5.0 (Linux; Android 14; Pixel 8) '
    'AppleWebKit/537.36 Chrome/127.0 Mobile Safari/537.36'
)
IPHONE_USER_AGENT = (
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) '
    'AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1'
)
DESKTOP_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 Chrome/127.0 Safari/537.36'
)


def make_role(code, name):
    role, _ = Role.objects.get_or_create(code=code, defaults={'name': name, 'is_active': True})
    if not role.is_active:
        role.is_active = True
        role.save(update_fields=['is_active'])
    return role


@override_settings(ALLOWED_HOSTS=['testserver', '.testserver'])
class UniversalStartTests(TestCase):
    phone = '+79990000071'

    def setUp(self):
        self.media_directory = TemporaryDirectory()
        self.addCleanup(self.media_directory.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.employee = Employee.objects.create(
            full_name='Многоролев Модест Модестович',
            phone=self.phone,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )

    def add_access(self, code, name, *, status=EmployeeAccess.Status.ACTIVATED,
                   access_code='170001', last_login_at=None):
        return EmployeeAccess.objects.create(
            employee=self.employee,
            role=make_role(code, name),
            access_code=access_code,
            status=status,
            is_active=True,
            activated_at=timezone.now(),
            last_login_at=last_login_at,
        )

    def add_apk(self, relative_path):
        apk_path = Path(self.media_directory.name) / relative_path
        apk_path.parent.mkdir(parents=True, exist_ok=True)
        apk_path.write_bytes(b'test apk placeholder')

    def post(self, *, user_agent='', follow=True):
        return self.client.post(
            reverse('universal_start'),
            {'phone': self.phone},
            HTTP_USER_AGENT=user_agent,
            follow=follow,
        )

    def test_browser_bar_matches_the_shared_dark_background(self):
        response = self.client.get(reverse('universal_start'))

        self.assertContains(
            response,
            f'<meta name="theme-color" content="{ENTRY_SCREEN_BROWSER_BAR}">',
            count=1,
        )

    def test_start_form_preserves_contract_and_uses_accessible_brand_markup(self):
        response = self.client.get(reverse('universal_start'))
        html = response.content.decode('utf-8')
        form = re.search(r'<form method="post" data-start-form>(.*?)</form>', html, re.S)

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(form)
        form_html = form.group(1)
        self.assertIn('name="csrfmiddlewaretoken"', form_html)
        self.assertIn('id="start-phone" name="phone" type="text"', form_html)
        self.assertIn('inputmode="numeric"', form_html)
        self.assertIn('autocomplete="tel-national"', form_html)
        self.assertIn('enterkeyhint="go"', form_html)
        self.assertIn('aria-describedby="start-phone-hint"', form_html)
        self.assertIn('aria-invalid="false"', form_html)
        self.assertIn('type="submit"', form_html)
        self.assertNotIn(' action=', form.group(0))
        self.assertNotIn('autofocus', form_html)
        self.assertNotIn('maxlength=', form_html)
        self.assertContains(response, 'ДОСТУП К РАБОЧИМ ПРИЛОЖЕНИЯМ')
        self.assertContains(response, 'Вход в рабочее приложение')
        self.assertContains(response, 'Введите свой номер телефона — система подскажет, какое приложение вам нужно.')
        self.assertContains(response, '>ТЕЛЕФОН<')
        self.assertContains(response, 'placeholder="900-000-00-00"')
        self.assertContains(response, 'start-hero-v1.webp')
        self.assertContains(response, 'start-hero-v1.jpg')
        self.assertContains(response, 'fetchpriority="high"')
        self.assertContains(response, 'start-page-v1.css?v=20260903-3')
        self.assertContains(response, 'start-page-v1.js?v=20260903-3')
        self.assertRegex(html, r'<body[^>]+class="start-page"')
        self.assertNotRegex(html, r'<body[^>]+class="[^"]*dispatcher-shell')

    def test_start_form_keeps_same_origin_csrf_evidence(self):
        client = Client(enforce_csrf_checks=True)
        form = client.get(reverse('universal_start'), secure=True)
        html = form.content.decode('utf-8')
        csrf_token = re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"',
            html,
        ).group(1)

        self.assertEqual(form.headers['Referrer-Policy'], 'same-origin')
        self.assertIn('csrftoken', client.cookies)

        accepted = client.post(
            reverse('universal_start'),
            {'phone': '+79990000999', 'csrfmiddlewaretoken': csrf_token},
            secure=True,
            HTTP_ORIGIN='https://testserver',
        )
        rejected = client.post(
            reverse('universal_start'),
            {'phone': '+79990000999', 'csrfmiddlewaretoken': csrf_token},
            secure=True,
            HTTP_ORIGIN='null',
        )

        self.assertEqual(accepted.status_code, 303)
        self.assertNotIn('phone=', accepted.headers['Location'])
        self.assertEqual(rejected.status_code, 403)

    def test_lookup_uses_opaque_post_redirect_get_result(self):
        self.add_access('driver', 'Водитель самосвала')

        submitted = self.post(user_agent=ANDROID_USER_AGENT, follow=False)

        self.assertEqual(submitted.status_code, 303)
        location = submitted.headers['Location']
        parsed = urlparse(location)
        result_tokens = parse_qs(parsed.query).get('result', [])
        self.assertEqual(parsed.path, reverse('universal_start'))
        self.assertEqual(len(result_tokens), 1)
        self.assertRegex(result_tokens[0], r'^[A-Za-z0-9_-]{43}$')
        self.assertNotIn('phone', location)
        self.assertNotIn('79990000071', location)

        first = self.client.get(location, HTTP_USER_AGENT=ANDROID_USER_AGENT)
        reloaded = self.client.get(location, HTTP_USER_AGENT=ANDROID_USER_AGENT)

        for response in (first, reloaded):
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['found'])
            self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
            self.assertContains(response, 'Водитель самосвала')

    def test_parallel_lookup_results_do_not_overwrite_each_other(self):
        self.add_access('driver', 'Водитель самосвала')
        other = Employee.objects.create(
            full_name='Другой Водитель Водителевич',
            phone='+79990000072',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=other,
            role=make_role('driver', 'Водитель самосвала'),
            access_code='170002',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            activated_at=timezone.now(),
        )

        first = self.post(follow=False)
        second = self.client.post(
            reverse('universal_start'),
            {'phone': '+79990000072'},
        )

        self.assertNotEqual(first.headers['Location'], second.headers['Location'])
        for location, employee_name in (
            (first.headers['Location'], self.employee.full_name),
            (second.headers['Location'], other.full_name),
        ):
            response = self.client.get(location)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['found'])
            self.assertContains(response, employee_name)

    @patch('users.start_views.cache.add', side_effect=RuntimeError('cache down'))
    def test_cache_failure_redirects_to_clean_form_without_post_result(self, _add):
        self.add_access('driver', 'Водитель самосвала')

        response = self.post(follow=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['Location'], reverse('universal_start'))
        self.assertNotIn('phone', response.headers['Location'])

    def test_start_rejects_methods_other_than_get_and_post(self):
        response = self.client.put(reverse('universal_start'))

        self.assertEqual(response.status_code, 405)

    def test_missing_result_redirects_to_clean_same_origin_form(self):
        missing = self.client.get(
            f'{reverse("universal_start")}?result={"x" * 43}',
        )

        self.assertEqual(missing.status_code, 303)
        self.assertEqual(missing.headers['Location'], reverse('universal_start'))
        self.assertNotIn('phone', missing.headers['Location'])

        form = self.client.get(missing.headers['Location'])
        self.assertEqual(form.status_code, 200)
        self.assertEqual(form.headers['Referrer-Policy'], 'same-origin')
        self.assertContains(form, 'name="csrfmiddlewaretoken"')

    def test_unknown_phone_keeps_the_shared_dark_browser_bar(self):
        response = self.client.post(
            reverse('universal_start'),
            {'phone': '+79990000999'},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="phone-not-found"')
        self.assertContains(response, 'Номер не найден')
        self.assertContains(
            response,
            f'<meta name="theme-color" content="{ENTRY_SCREEN_BROWSER_BAR}">',
            count=1,
        )

    def test_person_with_a_working_code_is_not_promised_a_new_one(self):
        self.add_access('driver', 'Водитель самосвала')
        response = self.post()
        self.assertTrue(response.context['has_working_code'])
        self.assertNotContains(response, 'Пинкод придумаете при первом входе')

    def test_person_without_a_code_is_told_to_invent_one(self):
        self.add_access(
            'driver', 'Водитель самосвала',
            status=EmployeeAccess.Status.NOT_ACTIVATED,
            access_code='000000',
        )
        response = self.post()
        self.assertFalse(response.context['has_working_code'])
        self.assertContains(response, 'Пинкод придумаете при первом входе')

    def test_apps_are_shown_as_icon_tiles(self):
        self.add_access('driver', 'Водитель самосвала')
        response = self.post()
        self.assertEqual(len(response.context['apps']), 1)
        self.assertContains(response, 'start-screen__apps')
        self.assertContains(response, response.context['apps'][0]['app'].icon_192_url)

    def test_all_apps_are_shown(self):
        """Раньше список обрезался: кнопки в столбик занимали несколько
        экранов. Плитки в два столбца помещаются, прятать нечего."""
        for code, name in (
            ('driver', 'Водитель самосвала'),
            ('excavator_operator', 'Машинист экскаватора'),
            ('mining_master', 'Горный мастер'),
            ('dispatcher', 'Диспетчер'),
            ('oup', 'Специалист ОУП'),
        ):
            self.add_access(code, name)
        response = self.post()
        self.assertEqual(len(response.context['apps']), 5)

    def test_recently_used_app_comes_first(self):
        now = timezone.now()
        self.add_access('driver', 'Водитель самосвала')
        self.add_access('mining_master', 'Горный мастер', last_login_at=now)
        response = self.post()
        self.assertEqual(response.context['apps'][0]['app'].role_code, 'mining_master')

    def test_android_sees_configured_apk_buttons_for_its_roles(self):
        self.add_apk('apk/driver-10.apk')
        self.add_apk('apk/excavator-15.apk')
        self.add_access('driver', 'Водитель самосвала')
        self.add_access('excavator_operator', 'Машинист экскаватора')

        response = self.post(user_agent=ANDROID_USER_AGENT)

        self.assertContains(response, '/media/apk/driver-10.apk')
        self.assertContains(response, '/media/apk/excavator-15.apk')
        self.assertContains(response, 'data-start-install-option="native"', count=2)
        self.assertNotContains(response, 'data-start-native-open')
        self.assertContains(response, 'data-start-install-option="browser"', count=2)
        self.assertContains(response, '<b>Приложение <i>стабильное</i></b>', count=2)
        self.assertContains(response, '<b>Браузер <i>нестабильно</i></b>', count=2)
        self.assertContains(response, 'APK · версия 0.1.12 · скачать и установить', count=1)
        self.assertContains(response, 'APK · версия 0.1.8 · скачать и установить', count=1)
        self.assertNotContains(response, '2. Открыть приложение')
        self.assertNotContains(response, '/native-handoff/')
        self.assertContains(response, 'PWA · ярлык на экран · открыть', count=2)
        self.assertContains(response, 'install=1', count=2)
        self.assertNotContains(response, 'class="start-screen__app-main" href=')
        # Атрибут download заставлял Chrome ругаться «файл может быть опасным».
        self.assertNotContains(response, ' download')
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertContains(response, 'phone=79990000071', count=2)

    def test_native_phone_handoff_routes_are_removed(self):
        for path in (
            '/.well-known/assetlinks.json',
            '/native-handoff/open/',
            '/native-handoff/redeem/',
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_android_does_not_see_button_when_configured_apk_file_is_missing(self):
        self.add_access('driver', 'Водитель самосвала')

        response = self.post(user_agent=ANDROID_USER_AGENT)

        self.assertIsNone(response.context['apps'][0]['apk'])
        self.assertNotContains(response, 'href="/media/apk/driver-10.apk"')
        self.assertNotContains(response, 'data-start-install-option="native"')
        self.assertNotContains(response, 'data-start-native-open')
        self.assertContains(response, 'data-start-install-option="browser"')

    def test_android_does_not_see_apk_button_for_unsupported_role(self):
        self.add_access('mining_master', 'Горный мастер')

        response = self.post(user_agent=ANDROID_USER_AGENT)

        self.assertTrue(all(item['apk'] is None for item in response.context['apps']))
        self.assertNotContains(response, 'data-start-install-option="native"')
        self.assertContains(response, 'data-start-install-option="browser"')
        self.assertNotContains(response, 'href="/media/apk/')

    def test_iphone_does_not_see_apk_button(self):
        self.add_apk('apk/driver-10.apk')
        self.add_access('driver', 'Водитель самосвала')

        response = self.post(user_agent=IPHONE_USER_AGENT)

        self.assertIsNone(response.context['apps'][0]['apk'])
        self.assertNotContains(response, 'href="/media/apk/driver-10.apk"')
        self.assertNotContains(response, 'data-start-install-option="native"')
        self.assertNotContains(response, 'data-start-native-open')
        self.assertContains(response, 'data-start-install-option="browser"', count=1)
        self.assertContains(response, '<b>Браузер <i>нестабильно</i></b>')
        self.assertContains(response, 'install=1', count=1)
        self.assertContains(response, 'После перехода добавьте значок на экран')

    def test_iphone_shows_only_pwa_action_without_android_block(self):
        self.add_apk('apk/driver-10.apk')
        self.add_access('driver', 'Водитель самосвала')

        response = self.post(user_agent=IPHONE_USER_AGENT)

        self.assertEqual(response.context['apps'][0]['app'].name, 'Водитель самосвала')
        # На iPhone только полноценный браузерный вариант, без блока Android.
        self.assertContains(response, 'data-start-install-option="browser"', count=1)
        self.assertNotContains(response, 'data-start-install-option="native"')
        self.assertContains(response, 'install=1', count=1)
        self.assertNotContains(response, 'href="/media/apk/driver-10.apk"')
        self.assertNotContains(response, 'data-start-native-open')
        self.assertNotContains(response, 'class="start-screen__app-main" href=')

    @override_settings(
        SUPPORT_CHAT_URL='https://max.ru/u/test',
        SUPPORT_CHAT_LABEL='Написать администратору в MAX',
    )
    def test_administrator_contact_is_offered_before_and_after_the_lookup(self):
        """Застревают не только те, чей номер не нашёлся: человек может
        найтись, но не суметь поставить приложение — ему тоже нужен выход."""
        self.add_access('driver', 'Водитель самосвала')

        form = self.client.get(reverse('universal_start'))
        found = self.post()

        for response in (form, found):
            self.assertContains(response, 'https://max.ru/u/test')
            self.assertContains(response, 'Написать администратору в MAX')
            self.assertContains(response, 'data-support-channel="max"', count=1)
            self.assertContains(response, 'max-support-link-v1.css?v=6')

        unknown = self.client.post(
            reverse('universal_start'),
            {'phone': '+79990000999'},
            follow=True,
        )
        self.assertContains(unknown, 'phone-not-found__chat max-support-link')
        self.assertContains(unknown, 'data-support-channel="max"', count=1)

    @override_settings(SUPPORT_CHAT_URL='')
    def test_administrator_contact_is_hidden_when_no_address_is_configured(self):
        """Ссылка в никуда хуже её отсутствия: человек нажмёт и не поймёт."""
        self.add_access('driver', 'Водитель самосвала')

        response = self.post()

        self.assertNotContains(response, 'data-support-channel="max"')

    def test_start_support_hides_only_for_a_real_visual_keyboard(self):
        response = self.client.get(reverse('universal_start'))
        backend_root = Path(settings.BASE_DIR)
        script = (backend_root / 'static' / 'js' / 'start-page-v1.js').read_text(encoding='utf-8')
        stylesheet = (backend_root / 'static' / 'css' / 'start-page-v1.css').read_text(encoding='utf-8')

        self.assertContains(response, 'start-page-v1.js?v=20260903-3')
        self.assertIn('window.visualViewport || null', script)
        self.assertIn('Math.max(120, baselineHeight * 0.22)', script)
        self.assertIn('viewport.addEventListener("resize"', script)
        self.assertIn('viewport.addEventListener("scroll"', script)
        self.assertIn('window.addEventListener("orientationchange"', script)
        self.assertIn('form.addEventListener("focusin"', script)
        self.assertIn('form.addEventListener("focusout"', script)
        self.assertIn('body.classList.toggle("is-input-mode"', script)
        self.assertIn('body.classList.toggle("is-keyboard-open"', script)
        self.assertIn('body.classList.toggle("is-start-viewport-tight"', script)
        self.assertIn('--start-vv-height', script)
        self.assertIn('form.scrollIntoView', script)
        self.assertIn('body.start-page.is-input-mode .start-screen__inner > .max-support-link', stylesheet)
        self.assertIn('body.start-page.is-keyboard-open .start-hero', stylesheet)
        self.assertIn('aspect-ratio: 853 / 373', stylesheet)
        self.assertIn('height: var(--start-vv-height, 100dvh)', stylesheet)
        self.assertIn('grid-template-rows: auto minmax(0, 1fr)', stylesheet)
        self.assertIn('align-self: end', stylesheet)

    def test_start_phone_runtime_formats_country_prefix_and_guards_double_submit(self):
        script = (
            Path(settings.BASE_DIR) / 'static' / 'js' / 'start-page-v1.js'
        ).read_text(encoding='utf-8')

        self.assertIn('if (digits.charAt(0) === "8")', script)
        self.assertIn('if (digits.charAt(0) === "7")', script)
        self.assertIn('digits.slice(0, 10)', script)
        self.assertIn('result += "-" + digits.slice(3, 6)', script)
        self.assertIn('var showInvalid = Boolean', script)
        self.assertIn('phoneShell.classList.toggle("is-invalid", showInvalid)', script)
        self.assertIn('form.addEventListener("submit"', script)
        self.assertIn('if (submitting)', script)
        self.assertIn('submitLabel.textContent = "Проверяем…"', script)

    def test_desktop_does_not_see_apk_button(self):
        self.add_apk('apk/excavator-15.apk')
        self.add_access('excavator_operator', 'Машинист экскаватора')

        response = self.post(user_agent=DESKTOP_USER_AGENT)

        self.assertIsNone(response.context['apps'][0]['apk'])
        self.assertNotContains(response, 'href="/media/apk/excavator-15.apk"')
        self.assertNotContains(response, 'data-start-native-open')
        self.assertContains(response, 'data-start-install-option="browser"')
        self.assertContains(response, 'install=1', count=1)
        self.assertNotContains(response, 'После перехода добавьте значок на экран')
