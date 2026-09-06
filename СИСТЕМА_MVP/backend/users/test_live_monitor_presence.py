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
from .live_monitor import (
    application_presence_by_access_ids,
    application_presence_by_employee_ids,
)


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

    def test_background_heartbeat_has_recovery_allowance_beyond_foreground_window(self):
        now = timezone.now()
        ActiveApplicationSession.objects.create(
            session_key='background-recovery-window',
            access=self.driver_access,
            role_code='driver',
            app_code='driver',
            path='/driver/',
            last_seen_at=now - timedelta(seconds=120),
            background_seen_at=now - timedelta(seconds=120),
            client_kind=ActiveApplicationSession.ClientKind.ANDROID_APK,
            client_version='0.1.11',
        )
        presence = application_presence_by_access_ids(
            [self.driver_access.pk],
            now=now,
        )[self.driver_access.pk]
        self.assertFalse(presence['is_online'])
        self.assertTrue(presence['is_background'])
        self.assertEqual(presence['status_label'], 'Связь в фоне')

    def test_last_known_client_kind_and_version_remain_visible_after_disconnect(self):
        now = timezone.now()
        ActiveApplicationSession.objects.create(
            session_key='known-installed-client',
            access=self.excavator_access,
            role_code='excavator_operator',
            app_code='excavator_operator',
            path='/excavator/work/',
            last_seen_at=now - timedelta(days=2),
            foreground_seen_at=now - timedelta(days=2),
            client_kind=ActiveApplicationSession.ClientKind.ANDROID_APK,
            client_version='0.1.19',
        )
        presence = application_presence_by_employee_ids(
            [self.excavator_access.employee_id],
            now=now,
        )[self.excavator_access.employee_id]
        self.assertEqual(presence['status_code'], 'offline')
        self.assertEqual(presence['status_label'], 'Нет связи')
        self.assertEqual(
            presence['client_badges'],
            [{
                'kind': 'android_apk',
                'label': 'APK Android',
                'version': '0.1.19',
                'app_code': 'excavator_operator',
                'app_label': 'Экскаватор',
                'last_seen_at': now - timedelta(days=2),
            }],
        )

    def test_logged_in_employee_without_any_session_has_explicit_offline_presence(self):
        presence = application_presence_by_employee_ids(
            [self.driver_access.employee_id],
        )[self.driver_access.employee_id]
        self.assertEqual(presence['status_code'], 'offline')
        self.assertEqual(presence['status_label'], 'Нет связи')
        self.assertTrue(presence['has_logged_in'])
        self.assertEqual(presence['client_badges'], [])

    def test_employee_who_never_logged_in_is_visibly_distinct_from_offline(self):
        employee = Employee.objects.create(
            full_name='Новый сотрудник',
            phone='+79990000305',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=employee,
            role=self.driver_access.role,
            access_code='305305',
            status=EmployeeAccess.Status.NOT_ACTIVATED,
            is_active=True,
        )

        presence = application_presence_by_employee_ids([employee.pk])[employee.pk]

        self.assertEqual(presence['status_code'], 'not_registered')
        self.assertEqual(presence['status_label'], 'Не подключался')
        self.assertFalse(presence['has_logged_in'])
        self.assertIsNone(presence['last_seen_at'])
        self.assertEqual(presence['client_badges'], [])

    def test_access_presence_distinguishes_never_logged_in_from_offline(self):
        never_logged_in = EmployeeAccess.objects.create(
            employee=Employee.objects.create(
                full_name='Новый водитель',
                phone='+79990000306',
                status=Employee.Status.ACTIVE,
                is_active=True,
            ),
            role=self.driver_access.role,
            access_code='306306',
            status=EmployeeAccess.Status.NOT_ACTIVATED,
            is_active=True,
        )

        presence = application_presence_by_access_ids(
            [never_logged_in.pk, self.driver_access.pk]
        )

        self.assertEqual(presence[never_logged_in.pk]['status_code'], 'not_registered')
        self.assertEqual(presence[never_logged_in.pk]['status_label'], 'Не подключался')
        self.assertEqual(presence[self.driver_access.pk]['status_code'], 'offline')
        self.assertEqual(presence[self.driver_access.pk]['status_label'], 'Нет связи')

    def test_employee_register_and_admin_entry_use_shared_presence_component(self):
        ActiveApplicationSession.objects.create(
            session_key='shared-component-client',
            access=self.driver_access,
            role_code='driver',
            app_code='driver',
            path='/driver/',
            last_seen_at=timezone.now(),
            background_seen_at=timezone.now(),
            client_kind=ActiveApplicationSession.ClientKind.ANDROID_APK,
            client_version='0.1.11',
        )
        employees = self.admin.get(reverse('system_admin_employees'))
        enter = self.admin.get(reverse('system_admin_enter_employee'))
        employee_card = self.admin.get(
            reverse('system_admin_employee_detail', args=[self.driver_access.employee_id])
        )
        for response in (employees, enter, employee_card):
            with self.subTest(path=response.request['PATH_INFO']):
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'data-application-presence="background"')
                self.assertContains(response, 'Связь в фоне')
                self.assertContains(response, 'APK Android · 0.1.11')

    def test_oup_employee_register_uses_the_same_presence_component(self):
        oup_access = self._make_access(
            'Тестовый специалист ОУП',
            '+79990000304',
            'oup',
        )
        oup = Client()
        self._authorize(oup, oup_access)
        ActiveApplicationSession.objects.create(
            session_key='oup-visible-driver-client',
            access=self.driver_access,
            role_code='driver',
            app_code='driver',
            path='/driver/',
            last_seen_at=timezone.now(),
            foreground_seen_at=timezone.now(),
            client_kind=ActiveApplicationSession.ClientKind.ANDROID_PWA,
        )
        response = oup.get(reverse('oup_employees'), HTTP_HOST='oup.localhost')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-application-presence="online"')
        self.assertContains(response, 'PWA Android')
