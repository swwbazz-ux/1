"""Общий вход /start/: что человек видит после ввода номера.

Две вещи, на которых страница врала совмещающему роли: обещала завести пинкод
тому, у кого он уже есть, и вываливала все приложения одним списком.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Employee, EmployeeAccess, Role


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
        self.add_apk('apk/driver-5.apk')
        self.add_apk('apk/excavator-7.apk')
        self.add_access('driver', 'Водитель самосвала')
        self.add_access('excavator_operator', 'Машинист экскаватора')

        response = self.post(user_agent=ANDROID_USER_AGENT)

        self.assertContains(response, '/media/apk/driver-5.apk')
        self.assertContains(response, '/media/apk/excavator-7.apk')
        self.assertContains(response, 'Установить приложение')
        self.assertContains(response, 'Работает, когда приложение свёрнуто — связь со сменой не рвётся')
        self.assertContains(response, 'Уведомления приходят со звуком')
        self.assertContains(response, 'Обновляется само, ничего переустанавливать не нужно')
        self.assertContains(response, 'Не получается установить? Открыть в браузере')
        self.assertContains(response, 'Так тоже можно работать, но связь оборвётся, когда свернёте окно')
        self.assertNotContains(response, 'class="start-screen__app-main" href=')

    def test_android_does_not_see_button_when_configured_apk_file_is_missing(self):
        self.add_access('driver', 'Водитель самосвала')

        response = self.post(user_agent=ANDROID_USER_AGENT)

        self.assertIsNone(response.context['apps'][0]['apk'])
        self.assertNotContains(response, 'href="/media/apk/driver-5.apk"')

    def test_android_does_not_see_apk_button_for_unsupported_role(self):
        self.add_access('mining_master', 'Горный мастер')

        response = self.post(user_agent=ANDROID_USER_AGENT)

        self.assertTrue(all(item['apk'] is None for item in response.context['apps']))
        self.assertNotContains(response, 'href="/media/apk/')

    def test_iphone_does_not_see_apk_button(self):
        self.add_apk('apk/driver-5.apk')
        self.add_access('driver', 'Водитель самосвала')

        response = self.post(user_agent=IPHONE_USER_AGENT)

        self.assertIsNone(response.context['apps'][0]['apk'])
        self.assertNotContains(response, 'href="/media/apk/driver-5.apk"')
        self.assertContains(response, 'Открыть в браузере')
        self.assertContains(response, 'После перехода нужно будет добавить значок на экран')
        self.assertNotContains(response, 'Не получается установить? Открыть в браузере')

    def test_iphone_shows_only_pwa_action_without_android_block(self):
        self.add_apk('apk/driver-5.apk')
        self.add_access('driver', 'Водитель самосвала')

        response = self.post(user_agent=IPHONE_USER_AGENT)

        self.assertEqual(response.context['apps'][0]['app'].name, 'Водитель самосвала')
        # На iPhone только действие «Открыть в браузере», без действий Android.
        self.assertContains(response, 'Открыть в браузере')
        self.assertNotContains(response, 'href="/media/apk/driver-5.apk"')
        self.assertNotContains(response, 'Не получается установить?')
        self.assertNotContains(response, 'class="start-screen__app-main" href=')

    def test_desktop_does_not_see_apk_button(self):
        self.add_apk('apk/excavator-7.apk')
        self.add_access('excavator_operator', 'Машинист экскаватора')

        response = self.post(user_agent=DESKTOP_USER_AGENT)

        self.assertIsNone(response.context['apps'][0]['apk'])
        self.assertNotContains(response, 'href="/media/apk/excavator-7.apk"')
        self.assertContains(response, 'Открыть в браузере')
        self.assertNotContains(response, 'После перехода нужно будет добавить значок на экран')
