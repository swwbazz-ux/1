from datetime import timedelta

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .active_role import (
    ACTIVE_ROLE_CODE_SESSION_KEY,
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    ACTIVE_ROLE_SESSION_KEY,
)
from .models import ActiveApplicationSession, Employee, EmployeeAccess, Role


@override_settings(ALLOWED_HOSTS=['testserver', '.localhost'])
class LiveMonitorPresenceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.driver_access = self._make_access(
            'Тестовый водитель',
            '+79990000301',
            'driver',
        )
        self.admin_access = self._make_access(
            'Тестовый администратор',
            '+79990000302',
            'admin',
        )
        self.excavator_access = self._make_access(
            'Тестовый экскаваторщик',
            '+79990000303',
            'excavator_operator',
        )
        self.driver = Client()
        self.excavator = Client()
        self.admin = Client()
        self._authorize(self.driver, self.driver_access)
        self._authorize(self.excavator, self.excavator_access)
        self._authorize(self.admin, self.admin_access)

    def tearDown(self):
        cache.clear()

    @staticmethod
    def _make_access(full_name, phone, role_code):
        employee = Employee.objects.create(
            full_name=full_name,
            phone=phone,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        role, _ = Role.objects.get_or_create(
            code=role_code,
            defaults={'name': role_code, 'is_active': True},
        )
        logged_in_at = timezone.now()
        return EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code='301301',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            activated_at=logged_in_at,
            last_login_at=logged_in_at,
        )

    @staticmethod
    def _authorize(client, access):
        session = client.session
        session['employee_access_id'] = access.pk
        session[ACTIVE_ROLE_SESSION_KEY] = access.pk
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = access.last_login_at.isoformat()
        session[ACTIVE_ROLE_CODE_SESSION_KEY] = access.role.code
        session['device_kind'] = 'personal'
        session.save()

    def _monitor(self):
        return self.admin.get(reverse('system_admin_live_monitor'))

    def test_native_realtime_heartbeat_becomes_blue_background_presence(self):
        response = self.driver.get(
            reverse('operational_state_version'),
            {'include_events': '0', 'role_app_code': 'driver'},
            HTTP_HOST='driver.localhost',
            HTTP_USER_AGENT='CopperResourcesNative/driver/0.1.18',
        )
        self.assertEqual(response.status_code, 200)

        presence = ActiveApplicationSession.objects.get(access=self.driver_access)
        self.assertEqual(presence.client_kind, ActiveApplicationSession.ClientKind.ANDROID_APK)
        self.assertEqual(presence.client_version, '0.1.18')
        self.assertIsNotNone(presence.background_seen_at)
        self.assertIsNone(presence.foreground_seen_at)

        monitor = self._monitor()
        self.assertContains(monitor, 'Связь в фоне')
        self.assertContains(monitor, 'APK Android · 0.1.18')
        driver_card = next(
            card for card in monitor.context['app_cards']
            if card['app'].role_code == 'driver'
        )
        row = next(item for item in driver_card['rows'] if item['access'] == self.driver_access)
        self.assertFalse(row['is_online'])
        self.assertTrue(row['is_background'])
        self.assertEqual(driver_card['online_count'], 0)
        self.assertEqual(driver_card['background_count'], 1)

    def test_foreground_heartbeat_has_priority_over_background(self):
        self.driver.get(
            reverse('operational_state_version'),
            {'include_events': '0', 'role_app_code': 'driver'},
            HTTP_HOST='driver.localhost',
            HTTP_USER_AGENT='CopperResourcesNative/driver/0.1.18',
        )
        response = self.driver.post(
            reverse('application_session_heartbeat'),
            {
                'path': '/driver/',
                'client_kind': 'android_apk',
                'client_version': '0.1.18',
            },
            HTTP_HOST='driver.localhost',
        )
        self.assertEqual(response.status_code, 204)

        monitor = self._monitor()
        driver_card = next(
            card for card in monitor.context['app_cards']
            if card['app'].role_code == 'driver'
        )
        row = next(item for item in driver_card['rows'] if item['access'] == self.driver_access)
        self.assertTrue(row['is_online'])
        self.assertFalse(row['is_background'])
        self.assertContains(monitor, '<b class="is-online">Онлайн</b>', html=True)

    def test_excavator_apk_uses_the_same_background_presence_contract(self):
        response = self.excavator.get(
            reverse('operational_state_version'),
            {'include_events': '0', 'role_app_code': 'excavator'},
            HTTP_HOST='excavator.localhost',
            HTTP_USER_AGENT='CopperResourcesNative/excavator/0.1.18',
        )
        self.assertEqual(response.status_code, 200)
        presence = ActiveApplicationSession.objects.get(access=self.excavator_access)
        self.assertEqual(presence.app_code, 'excavator_operator')
        self.assertEqual(presence.client_kind, ActiveApplicationSession.ClientKind.ANDROID_APK)
        self.assertIsNotNone(presence.background_seen_at)

    def test_web_heartbeat_persists_reported_pwa_or_safari_kind(self):
        for client_kind, expected_label in (
            ('android_pwa', 'PWA Android'),
            ('ios_pwa', 'PWA iPhone/iPad'),
            ('safari', 'Safari'),
        ):
            with self.subTest(client_kind=client_kind):
                cache.clear()
                ActiveApplicationSession.objects.all().delete()
                response = self.driver.post(
                    reverse('application_session_heartbeat'),
                    {'path': '/driver/', 'client_kind': client_kind},
                    HTTP_HOST='driver.localhost',
                )
                self.assertEqual(response.status_code, 204)
                presence = ActiveApplicationSession.objects.get(access=self.driver_access)
                self.assertEqual(presence.client_kind, client_kind)
                self.assertEqual(presence.get_client_kind_display(), expected_label)

    def test_legacy_session_without_presence_fields_remains_online_during_transition(self):
        session_key = self.driver.session.session_key
        ActiveApplicationSession.objects.create(
            session_key=session_key,
            access=self.driver_access,
            role_code='driver',
            app_code='driver',
            path='/driver/',
            last_seen_at=timezone.now() - timedelta(seconds=20),
        )
        monitor = self._monitor()
        driver_card = next(
            card for card in monitor.context['app_cards']
            if card['app'].role_code == 'driver'
        )
        row = next(item for item in driver_card['rows'] if item['access'] == self.driver_access)
        self.assertTrue(row['is_online'])
        self.assertFalse(row['is_background'])

    def test_ordinary_realtime_poll_does_not_claim_background_presence(self):
        response = self.driver.get(
            reverse('operational_state_version'),
            {'include_events': '0', 'role_app_code': 'driver'},
            HTTP_HOST='driver.localhost',
            HTTP_USER_AGENT='Mozilla/5.0 (Linux; Android 14) Chrome/140 Mobile',
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ActiveApplicationSession.objects.exists())
