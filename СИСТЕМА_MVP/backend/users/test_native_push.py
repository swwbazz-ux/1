import json
import urllib.error
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import ANY, patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    Employee,
    EmployeeAccess,
    NativePushDevice,
    PushNotification,
    Role,
)
from .native_push import notify_employee_devices
from .webpush import notify_employee


class NativePushRegistrationTests(TestCase):
    def setUp(self):
        self.driver_role = Role.objects.create(code='driver', name='Водитель')
        self.excavator_role = Role.objects.create(code='excavator_operator', name='Машинист экскаватора')
        self.dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.employee = Employee.objects.create(
            full_name='Тестовый водитель',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.driver_role,
            access_code='921001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            activated_at=timezone.now(),
        )
        session = self.client.session
        session['employee_access_id'] = self.access.pk
        session.save()
        self.url = reverse('driver_native_push_register')

    def post(self, **overrides):
        payload = {
            'provider': 'fcm',
            'token': 'fcm-token-1',
            'platform': 'android',
            'app_id': 'ru.copperresources.driver',
            **overrides,
        }
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type='application/json',
        )

    def test_driver_registers_native_token_without_echoing_secret(self):
        response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['provider'], 'fcm')
        self.assertNotIn('token', response.json())
        device = NativePushDevice.objects.get()
        self.assertEqual(device.employee, self.employee)
        self.assertEqual(device.app_id, 'ru.copperresources.driver')
        self.assertTrue(device.is_active)

    def test_same_provider_token_is_reassigned_to_current_driver(self):
        self.post()
        other = Employee.objects.create(
            full_name='Второй водитель',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        other_access = EmployeeAccess.objects.create(
            employee=other,
            role=self.driver_role,
            access_code='921002',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            activated_at=timezone.now(),
        )
        session = self.client.session
        session['employee_access_id'] = other_access.pk
        session.save()

        response = self.post(app_id='ru.copperresources.driver.qa')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(NativePushDevice.objects.count(), 1)
        device = NativePushDevice.objects.get()
        self.assertEqual(device.employee, other)
        self.assertEqual(device.app_id, 'ru.copperresources.driver.qa')

    def test_excavator_registers_native_token_through_role_endpoint(self):
        self.access.role = self.excavator_role
        self.access.save(update_fields=['role'])
        self.url = reverse('excavator_native_push_register')

        response = self.post(app_id='ru.copperresources.excavator')

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('token', response.json())
        device = NativePushDevice.objects.get()
        self.assertEqual(device.employee, self.employee)
        self.assertEqual(device.app_id, 'ru.copperresources.excavator')
        self.assertTrue(device.is_active)

    def test_unrelated_role_cannot_register_native_push_token(self):
        self.access.role = self.dispatcher_role
        self.access.save(update_fields=['role'])

        response = self.post()

        self.assertEqual(response.status_code, 403)
        self.assertFalse(NativePushDevice.objects.exists())

    def test_invalid_provider_is_rejected(self):
        response = self.post(provider='unknown')

        self.assertEqual(response.status_code, 400)
        self.assertFalse(NativePushDevice.objects.exists())


class _SuccessfulResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class NativePushDeliveryTests(TestCase):
    def setUp(self):
        self.employee = Employee.objects.create(
            full_name='Получатель push',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.device = NativePushDevice.objects.create(
            employee=self.employee,
            provider=NativePushDevice.Provider.FCM,
            token='delivery-token',
            platform=NativePushDevice.Platform.ANDROID,
            app_id='ru.copperresources.driver',
        )
        self.tempdir = TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.credentials = Path(self.tempdir.name) / 'firebase-service-account.json'
        self.credentials.write_text(json.dumps({
            'project_id': 'copper-test',
            'client_email': 'push@copper-test.iam.gserviceaccount.com',
            'private_key': 'unused-in-mocked-test',
        }), encoding='utf-8')
        self.settings = override_settings(
            FCM_SERVICE_ACCOUNT_FILE=str(self.credentials),
            FCM_PROJECT_ID='',
            WEBPUSH_VAPID_PRIVATE_KEY='',
            WEBPUSH_VAPID_PUBLIC_KEY='',
        )
        self.settings.enable()
        self.addCleanup(self.settings.disable)

    @patch('users.native_push._oauth_access_token', return_value='oauth-token')
    @patch('users.native_push.urllib.request.urlopen', return_value=_SuccessfulResponse())
    def test_fcm_http_v1_sends_only_wakeup_data_and_marks_success(self, urlopen, _oauth):
        delivered = notify_employee_devices(
            self.employee,
            kind='driver_trip_loaded',
            state_version='173',
        )

        self.assertEqual(delivered, 1)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode('utf-8'))
        self.assertEqual(payload, {
            'message': {
                'token': 'delivery-token',
                'data': {'kind': 'driver_trip_loaded', 'version': '173'},
                'android': {'priority': 'high'},
            },
        })
        self.assertNotIn('notification', payload['message'])
        self.assertEqual(request.get_header('Authorization'), 'Bearer oauth-token')
        self.device.refresh_from_db()
        self.assertEqual(self.device.failure_count, 0)
        self.assertIsNotNone(self.device.last_success_at)

    @patch('users.native_push._oauth_access_token', return_value='oauth-token')
    @patch('users.native_push.urllib.request.urlopen', return_value=_SuccessfulResponse())
    def test_presence_probe_targets_role_app_and_carries_only_bounded_nonce(self, urlopen, _oauth):
        NativePushDevice.objects.create(
            employee=self.employee,
            provider=NativePushDevice.Provider.FCM,
            token='other-app-token',
            platform=NativePushDevice.Platform.ANDROID,
            app_id='ru.copperresources.excavator',
        )

        delivered = notify_employee_devices(
            self.employee,
            kind='presence_probe',
            app_ids={'ru.copperresources.driver'},
            extra_data={'probe_id': 'safe_probe_1234567890'},
        )

        self.assertEqual(delivered, 1)
        self.assertEqual(urlopen.call_count, 1)
        payload = json.loads(urlopen.call_args.args[0].data.decode('utf-8'))
        self.assertEqual(payload['message']['token'], 'delivery-token')
        self.assertEqual(payload['message']['data'], {
            'kind': 'presence_probe',
            'version': '',
            'probe_id': 'safe_probe_1234567890',
        })
        self.assertEqual(payload['message']['android'], {
            'priority': 'high',
            'ttl': '90s',
            'collapse_key': 'presence_probe',
        })

    @patch('users.native_push._oauth_access_token', return_value='oauth-token')
    @patch('users.native_push.urllib.request.urlopen', return_value=_SuccessfulResponse())
    def test_explicit_empty_app_filter_never_broadcasts_probe(self, urlopen, oauth):
        delivered = notify_employee_devices(
            self.employee,
            kind='presence_probe',
            app_ids=set(),
            extra_data={'probe_id': 'safe_probe_1234567890'},
        )

        self.assertEqual(delivered, 0)
        oauth.assert_not_called()
        urlopen.assert_not_called()

    @patch('users.native_push._oauth_access_token', return_value='oauth-token')
    @patch('users.native_push.urllib.request.urlopen', return_value=_SuccessfulResponse())
    def test_probe_can_bound_device_fanout_and_provider_timeout(self, urlopen, oauth):
        NativePushDevice.objects.create(
            employee=self.employee,
            provider=NativePushDevice.Provider.FCM,
            token='older-delivery-token',
            platform=NativePushDevice.Platform.ANDROID,
            app_id='ru.copperresources.driver',
        )

        delivered = notify_employee_devices(
            self.employee,
            kind='presence_probe',
            app_ids={'ru.copperresources.driver'},
            extra_data={'probe_id': 'safe_probe_1234567890'},
            max_devices=1,
            request_timeout_seconds=3,
        )

        self.assertEqual(delivered, 1)
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(urlopen.call_args.kwargs['timeout'], 3)
        oauth.assert_called_once_with(ANY, timeout_seconds=3)

    @patch('users.native_push._deliver_fcm', return_value=(False, False))
    def test_diagnostic_probe_does_not_penalize_token_for_transient_provider_failure(
        self,
        deliver,
    ):
        self.device.failure_count = 4
        self.device.save(update_fields=['failure_count'])

        delivered = notify_employee_devices(
            self.employee,
            kind='presence_probe',
            app_ids={'ru.copperresources.driver'},
            max_devices=2,
            stop_after_first_success=True,
            penalize_transient_failures=False,
        )

        self.assertEqual(delivered, 0)
        deliver.assert_called_once()
        self.device.refresh_from_db()
        self.assertTrue(self.device.is_active)
        self.assertEqual(self.device.failure_count, 4)

    @patch('users.native_push._oauth_access_token', return_value='oauth-token')
    @patch('users.native_push.urllib.request.urlopen')
    def test_unregistered_fcm_token_is_deactivated(self, urlopen, _oauth):
        urlopen.side_effect = urllib.error.HTTPError(
            url='https://fcm.googleapis.com/v1/projects/copper-test/messages:send',
            code=404,
            msg='Not Found',
            hdrs=None,
            fp=BytesIO(json.dumps({
                'error': {'details': [{'errorCode': 'UNREGISTERED'}]},
            }).encode('utf-8')),
        )

        delivered = notify_employee_devices(self.employee, kind='test')

        self.assertEqual(delivered, 0)
        self.device.refresh_from_db()
        self.assertFalse(self.device.is_active)

    @patch('users.native_push.notify_employee_devices', side_effect=RuntimeError('provider down'))
    def test_native_provider_failure_does_not_break_working_notification(self, _native):
        with self.assertLogs('users.webpush', level='ERROR'):
            delivered = notify_employee(
                self.employee,
                title='Погрузка',
                body='Рабочее событие сохранено',
                kind='driver_trip_loaded',
            )

        self.assertEqual(delivered, 0)
        self.assertTrue(PushNotification.objects.filter(employee=self.employee).exists())
