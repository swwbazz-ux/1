"""Общий вход /start/: что человек видит после ввода номера.

Две вещи, на которых страница врала совмещающему роли: обещала завести пинкод
тому, у кого он уже есть, и вываливала все приложения одним списком.
"""

import re
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase, override_settings
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

    def post(self, *, user_agent=''):
        return self.client.post(
            reverse('universal_start'),
            {'phone': self.phone},
            HTTP_USER_AGENT=user_agent,
        )

    def test_browser_bar_matches_the_shared_dark_background(self):
        response = self.client.get(reverse('universal_start'))

        self.assertContains(
            response,
            f'<meta name="theme-color" content="{ENTRY_SCREEN_BROWSER_BAR}">',
            count=1,
        )

    def test_unknown_phone_keeps_the_shared_dark_browser_bar(self):
        response = self.client.post(
            reverse('universal_start'),
            {'phone': '+79990000999'},
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
        self.assertContains(response, 'data-start-native-open', count=2)
        self.assertContains(response, 'data-start-install-option="browser"', count=2)
        self.assertContains(response, '<b>Приложение <i>стабильное</i></b>', count=2)
        self.assertContains(response, '<b>Браузер <i>нестабильно</i></b>', count=2)
        self.assertContains(response, '1. Скачать APK · версия 0.1.12', count=1)
        self.assertContains(response, '1. Скачать APK · версия 0.1.8', count=1)
        self.assertContains(response, '2. Открыть приложение', count=2)
        self.assertContains(response, 'PWA · ярлык на экран · открыть', count=2)
        self.assertContains(response, 'install=1', count=2)
        self.assertNotContains(response, 'class="start-screen__app-main" href=')
        # Атрибут download заставлял Chrome ругаться «файл может быть опасным».
        self.assertNotContains(response, ' download')
        html = response.content.decode('utf-8')
        handoff_links = re.findall(
            r'href="(https://(?:driver|excavator)\.driverform\.ru/native-handoff/open/#token=[A-Za-z0-9_-]{43})"',
            html,
        )
        self.assertEqual(len(handoff_links), 2)
        self.assertTrue(all('phone=' not in link for link in handoff_links))
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertContains(response, 'phone=79990000071', count=2)

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

        self.assertContains(response, 'viewport.height < window.innerHeight - 120')
        self.assertContains(response, 'is-start-keyboard-active')

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
