from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .active_role import (
    ACTIVE_ROLE_CODE_SESSION_KEY,
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    ACTIVE_ROLE_SESSION_KEY,
)
from .application_connection import (
    connection_probe_summary,
    record_application_connection_evidence,
)
from .models import ActiveApplicationSession, Employee, EmployeeAccess, NativePushDevice, Role
from .live_monitor import (
    application_presence_by_access_ids,
    application_presence_by_employee_ids,
)
from .live_monitor_views import application_session_heartbeat_view


QA_ROLE_HOSTS = {
    'qa-admin.driverform.ru': 'admin',
    'qa-driver.driverform.ru': 'driver',
    'qa-excavator.driverform.ru': 'excavator_operator',
}


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

    def test_observer_screen_cannot_refresh_phone_presence(self):
        request = RequestFactory().post(
            reverse('application_session_heartbeat'),
            {'path': '/excavator/work/', 'client_kind': 'browser'},
        )
        request.observer_mode = True

        response = application_session_heartbeat_view(request)

        self.assertEqual(response.status_code, 403)
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

    def test_foreground_heartbeat_records_connection_evidence_for_monitor(self):
        response = self.excavator.post(
            reverse('application_session_heartbeat'),
            {
                'path': '/excavator/work/',
                'client_kind': 'android_apk',
                'client_version': '0.1.28',
                'installation_id': 'android-11111111-2222-3333-4444-555555555555',
                'connection_state': 'ok',
                'applied_version': '710',
                'pending_version': '0',
                'rtt_ms': '84',
                'probe_capable': '1',
            },
            HTTP_HOST='excavator.localhost',
        )
        self.assertEqual(response.status_code, 204)

        presence = application_presence_by_access_ids(
            [self.excavator_access.pk]
        )[self.excavator_access.pk]
        connection = presence['connection']
        self.assertEqual(connection['health_code'], 'healthy')
        self.assertEqual(connection['channel'], 'foreground')
        self.assertEqual(connection['installation_short'], '55555555')
        self.assertEqual(connection['applied_version'], 710)
        self.assertEqual(connection['rtt_ms'], 84)
        self.assertTrue(connection['probe_capable'])

        monitor = self._monitor()
        self.assertContains(monitor, 'Обмен подтверждён')
        self.assertContains(monitor, '84 мс')
        self.assertContains(monitor, 'v710')

    def test_invalid_connection_evidence_is_bounded_and_sanitized(self):
        response = self.driver.post(
            reverse('application_session_heartbeat'),
            {
                'path': '/driver/',
                'installation_id': '<script>alert(1)</script>',
                'connection_state': 'perfect',
                'applied_version': '-1',
                'rtt_ms': '999999999',
            },
            HTTP_HOST='driver.localhost',
        )
        self.assertEqual(response.status_code, 204)
        connection = application_presence_by_access_ids(
            [self.driver_access.pk]
        )[self.driver_access.pk]['connection']
        self.assertEqual(connection['installation_id'], '')
        self.assertEqual(connection['connection_state'], 'unknown')
        self.assertIsNone(connection['applied_version'])
        self.assertIsNone(connection['rtt_ms'])
        self.assertEqual(connection['health_label'], 'Сервер ответил, данных мало')
        self.assertEqual(connection['pending_label'], 'нет данных об очереди')

    def test_headerless_native_poll_does_not_erase_rich_connection_evidence(self):
        rich = self.excavator.get(
            reverse('operational_state_version'),
            {'include_events': '0', 'role_app_code': 'excavator'},
            HTTP_HOST='excavator.localhost',
            HTTP_USER_AGENT='CopperResourcesNative/excavator/0.1.29',
            HTTP_X_APP_INSTALLATION_ID='android-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            HTTP_X_APP_CONNECTION_STATE='ok',
            HTTP_X_APP_OBSERVED_VERSION='711',
            HTTP_X_APP_HEARTBEAT_RTT_MS='72',
            HTTP_X_APP_PRESENCE_PROBE_CAPABLE='1',
        )
        self.assertEqual(rich.status_code, 200)

        plain = self.excavator.get(
            reverse('operational_state_version'),
            {'include_events': '0', 'role_app_code': 'excavator'},
            HTTP_HOST='excavator.localhost',
            HTTP_USER_AGENT='CopperResourcesNative/excavator/0.1.29',
        )
        self.assertEqual(plain.status_code, 200)

        connection = application_presence_by_access_ids(
            [self.excavator_access.pk]
        )[self.excavator_access.pk]['connection']
        self.assertEqual(connection['installation_short'], 'eeeeeeee')
        self.assertEqual(connection['connection_state'], 'ok')
        self.assertEqual(connection['observed_version'], 711)
        self.assertEqual(connection['rtt_ms'], 72)
        self.assertTrue(connection['probe_capable'])

    def test_newer_browser_evidence_does_not_hide_capable_apk(self):
        now = timezone.now()
        ActiveApplicationSession.objects.create(
            session_key='capable-apk-session',
            access=self.excavator_access,
            role_code='excavator_operator',
            app_code='excavator_operator',
            path='/excavator/work/',
            last_seen_at=now - timedelta(seconds=1),
            background_seen_at=now - timedelta(seconds=1),
            client_kind=ActiveApplicationSession.ClientKind.ANDROID_APK,
        )
        ActiveApplicationSession.objects.create(
            session_key='newer-browser-session',
            access=self.excavator_access,
            role_code='excavator_operator',
            app_code='excavator_operator',
            path='/excavator/work/',
            last_seen_at=now,
            foreground_seen_at=now,
            client_kind=ActiveApplicationSession.ClientKind.BROWSER,
        )
        record_application_connection_evidence(
            session_key='capable-apk-session',
            access_id=self.excavator_access.pk,
            app_code='excavator_operator',
            evidence={
                'present': True,
                'installation_id': 'android-11111111-2222-3333-4444-555555555555',
                'channel': 'background',
                'connection_state': 'ok',
                'probe_capable': True,
            },
            now=now - timedelta(seconds=1),
        )
        record_application_connection_evidence(
            session_key='newer-browser-session',
            access_id=self.excavator_access.pk,
            app_code='excavator_operator',
            evidence={
                'present': True,
                'installation_id': 'web-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                'channel': 'foreground',
                'connection_state': 'ok',
                'probe_capable': False,
            },
            now=now,
        )

        connection = application_presence_by_access_ids(
            [self.excavator_access.pk],
            now=now,
        )[self.excavator_access.pk]['connection']

        self.assertEqual(connection['channel'], 'foreground')
        self.assertEqual(connection['installation_short'], 'eeeeeeee')
        self.assertTrue(connection['probe_capable'])

    @override_settings(
        # `testserver` нужен, потому что экран наблюдения открывается общим
        # помощником без явного адреса. Без него Django отвергает этот запрос
        # как чужой узел, и тест падает уже после подтверждения пробы.
        ALLOWED_HOSTS=[*QA_ROLE_HOSTS, 'testserver'],
        ROLE_APP_HOST_ALIASES=QA_ROLE_HOSTS,
    )
    @patch('users.native_push.notify_employee_devices', return_value=1)
    def test_admin_probe_is_acknowledged_by_matching_native_heartbeat(self, notify):
        NativePushDevice.objects.create(
            employee=self.excavator_access.employee,
            token='test-excavator-token',
            app_id='ru.copperresources.excavator',
        )
        capability = self.excavator.get(
            reverse('operational_state_version'),
            {'include_events': '0', 'role_app_code': 'excavator'},
            HTTP_HOST='qa-excavator.driverform.ru',
            HTTP_USER_AGENT='CopperResourcesNative/excavator/0.1.29',
            HTTP_X_APP_INSTALLATION_ID='android-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            HTTP_X_APP_CONNECTION_STATE='ok',
            HTTP_X_APP_PRESENCE_PROBE_CAPABLE='1',
        )
        self.assertEqual(capability.status_code, 200)
        response = self.admin.post(
            reverse('system_admin_probe_connection', args=[self.excavator_access.pk]),
            HTTP_HOST='qa-admin.driverform.ru',
        )
        self.assertRedirects(response, reverse('system_admin_live_monitor'))
        probe = connection_probe_summary(
            access_id=self.excavator_access.pk,
            app_code='excavator_operator',
        )
        self.assertEqual(probe['status'], 'sent')
        notify.assert_called_once()

        heartbeat = self.excavator.get(
            reverse('operational_state_version'),
            {'include_events': '0', 'role_app_code': 'excavator'},
            # Тест сам переводит ALLOWED_HOSTS на стендовые адреса QA, поэтому
            # и пульс должен идти с того же адреса. Со старым excavator.localhost
            # Django отвергал запрос как чужой узел, и проверка падала на 400,
            # не дойдя до самой подтверждаемой пробы.
            HTTP_HOST='qa-excavator.driverform.ru',
            HTTP_USER_AGENT='CopperResourcesNative/excavator/0.1.28',
            HTTP_X_APP_INSTALLATION_ID='android-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            HTTP_X_APP_CONNECTION_STATE='ok',
            HTTP_X_APP_OBSERVED_VERSION='710',
            HTTP_X_APP_HEARTBEAT_RTT_MS='91',
            HTTP_X_APP_PRESENCE_PROBE=probe['probe_id'],
            HTTP_X_APP_PRESENCE_PROBE_CAPABLE='1',
        )
        self.assertEqual(heartbeat.status_code, 200)

        acknowledged = connection_probe_summary(
            access_id=self.excavator_access.pk,
            app_code='excavator_operator',
        )
        self.assertEqual(acknowledged['status'], 'acknowledged')
        self.assertEqual(
            acknowledged['installation_id'],
            'android-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        )
        monitor = self._monitor()
        self.assertContains(monitor, 'APK ответил')

    @patch('users.native_push.notify_employee_devices', return_value=0)
    def test_admin_probe_reports_missing_push_channel_without_claiming_success(self, notify):
        capability = self.driver.get(
            reverse('operational_state_version'),
            {'include_events': '0', 'role_app_code': 'driver'},
            HTTP_HOST='driver.localhost',
            HTTP_USER_AGENT='CopperResourcesNative/driver/0.1.31',
            HTTP_X_APP_INSTALLATION_ID='android-bbbbbbbb-cccc-dddd-eeee-ffffffffffff',
            HTTP_X_APP_CONNECTION_STATE='ok',
            HTTP_X_APP_PRESENCE_PROBE_CAPABLE='1',
        )
        self.assertEqual(capability.status_code, 200)
        response = self.admin.post(
            reverse('system_admin_probe_connection', args=[self.driver_access.pk]),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'нет доступного push-канала')
        probe = connection_probe_summary(
            access_id=self.driver_access.pk,
            app_code='driver',
        )
        self.assertEqual(probe['status'], 'unavailable')
        notify.assert_called_once()

    @patch('users.native_push.notify_employee_devices')
    def test_fast_probe_ack_is_not_overwritten_by_sender(self, notify):
        NativePushDevice.objects.create(
            employee=self.excavator_access.employee,
            token='fast-ack-token',
            app_id='ru.copperresources.excavator',
        )
        capability = self.excavator.get(
            reverse('operational_state_version'),
            {'include_events': '0', 'role_app_code': 'excavator'},
            HTTP_HOST='excavator.localhost',
            HTTP_USER_AGENT='CopperResourcesNative/excavator/0.1.29',
            HTTP_X_APP_INSTALLATION_ID='android-cccccccc-dddd-eeee-ffff-000000000000',
            HTTP_X_APP_CONNECTION_STATE='ok',
            HTTP_X_APP_PRESENCE_PROBE_CAPABLE='1',
        )
        self.assertEqual(capability.status_code, 200)

        def acknowledge(_employee, **kwargs):
            record_application_connection_evidence(
                session_key=self.excavator.session.session_key,
                access_id=self.excavator_access.pk,
                app_code='excavator_operator',
                evidence={
                    'present': True,
                    'installation_id': 'android-cccccccc-dddd-eeee-ffff-000000000000',
                    'channel': 'background',
                    'connection_state': 'ok',
                    'probe_id': kwargs['extra_data']['probe_id'],
                    'probe_capable': True,
                },
            )
            return 1

        notify.side_effect = acknowledge
        response = self.admin.post(
            reverse('system_admin_probe_connection', args=[self.excavator_access.pk])
        )
        self.assertRedirects(response, reverse('system_admin_live_monitor'))
        probe = connection_probe_summary(
            access_id=self.excavator_access.pk,
            app_code='excavator_operator',
        )
        self.assertEqual(probe['status'], 'acknowledged')
        self.assertEqual(notify.call_args.kwargs['max_devices'], 2)
        self.assertEqual(notify.call_args.kwargs['request_timeout_seconds'], 3)
        self.assertTrue(notify.call_args.kwargs['stop_after_first_success'])
        self.assertFalse(notify.call_args.kwargs['penalize_transient_failures'])

    @patch('users.native_push.notify_employee_devices', return_value=1)
    def test_probe_cooldown_reuses_active_challenge(self, notify):
        NativePushDevice.objects.create(
            employee=self.excavator_access.employee,
            token='cooldown-token',
            app_id='ru.copperresources.excavator',
        )
        self.excavator.get(
            reverse('operational_state_version'),
            {'include_events': '0', 'role_app_code': 'excavator'},
            HTTP_HOST='excavator.localhost',
            HTTP_USER_AGENT='CopperResourcesNative/excavator/0.1.29',
            HTTP_X_APP_INSTALLATION_ID='android-dddddddd-eeee-ffff-0000-111111111111',
            HTTP_X_APP_CONNECTION_STATE='ok',
            HTTP_X_APP_PRESENCE_PROBE_CAPABLE='1',
        )
        url = reverse('system_admin_probe_connection', args=[self.excavator_access.pk])
        self.admin.post(url)
        first = connection_probe_summary(
            access_id=self.excavator_access.pk,
            app_code='excavator_operator',
        )
        self.admin.post(url)
        second = connection_probe_summary(
            access_id=self.excavator_access.pk,
            app_code='excavator_operator',
        )

        self.assertEqual(first['probe_id'], second['probe_id'])
        self.assertEqual(notify.call_count, 1)

    @patch('users.native_push.notify_employee_devices')
    def test_legacy_apk_cannot_start_probe_until_capability_is_reported(self, notify):
        NativePushDevice.objects.create(
            employee=self.excavator_access.employee,
            token='legacy-apk-token',
            app_id='ru.copperresources.excavator',
        )
        self.excavator.get(
            reverse('operational_state_version'),
            {'include_events': '0', 'role_app_code': 'excavator'},
            HTTP_HOST='excavator.localhost',
            HTTP_USER_AGENT='CopperResourcesNative/excavator/0.1.28',
        )

        monitor = self._monitor()
        self.assertContains(monitor, 'APK требует обновления')
        self.assertNotContains(monitor, '>Проверить APK<', html=True)
        response = self.admin.post(
            reverse('system_admin_probe_connection', args=[self.excavator_access.pk]),
            follow=True,
        )
        self.assertContains(response, 'ещё не поддерживает адресную проверку')
        notify.assert_not_called()

    def test_rustore_only_device_is_not_advertised_as_fcm_probe_channel(self):
        NativePushDevice.objects.create(
            employee=self.excavator_access.employee,
            provider=NativePushDevice.Provider.RUSTORE,
            token='rustore-only-token',
            app_id='ru.copperresources.excavator',
        )
        self.excavator.get(
            reverse('operational_state_version'),
            {'include_events': '0', 'role_app_code': 'excavator'},
            HTTP_HOST='excavator.localhost',
            HTTP_USER_AGENT='CopperResourcesNative/excavator/0.1.29',
            HTTP_X_APP_INSTALLATION_ID='android-eeeeeeee-ffff-0000-1111-222222222222',
            HTTP_X_APP_CONNECTION_STATE='ok',
            HTTP_X_APP_PRESENCE_PROBE_CAPABLE='1',
        )

        monitor = self._monitor()
        self.assertContains(monitor, 'push не зарегистрирован')
        self.assertNotContains(monitor, '>Проверить APK<', html=True)

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
