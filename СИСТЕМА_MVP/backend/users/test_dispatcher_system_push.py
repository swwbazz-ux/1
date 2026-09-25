from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.dispatcher_push import notification_for_event
from core.models import bump_operational_state
from users.models import (
    Employee,
    EmployeeAccess,
    PushNotification,
    Role,
    WebPushSubscription,
)
from users.webpush import notify_role_web_subscribers


class DispatcherSystemPushTests(TestCase):
    def setUp(self):
        self.dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.driver_role = Role.objects.create(code='driver', name='Водитель')
        self.dispatcher = Employee.objects.create(
            full_name='Тестовый диспетчер',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.driver = Employee.objects.create(
            full_name='Тестовый водитель',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.dispatcher_access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=self.dispatcher_role,
            access_code='931001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            activated_at=timezone.now(),
        )
        self.driver_access = EmployeeAccess.objects.create(
            employee=self.driver,
            role=self.driver_role,
            access_code='931002',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            activated_at=timezone.now(),
        )
        self.dispatcher_subscription = WebPushSubscription.objects.create(
            employee=self.dispatcher,
            endpoint='https://push.example.test/dispatcher',
            role_code='dispatcher',
        )
        self.driver_subscription = WebPushSubscription.objects.create(
            employee=self.driver,
            endpoint='https://push.example.test/driver',
            role_code='driver',
        )

    @staticmethod
    def event(event_type, action):
        return SimpleNamespace(event_type=event_type, payload={'action': action})

    def login_as(self, access):
        session = self.client.session
        session['employee_access_id'] = access.pk
        session.save()

    def test_mapper_uses_enriched_event_and_ignores_signal_save_duplicate(self):
        notification = notification_for_event(self.event('trip_changed', 'truck_loaded'))

        self.assertEqual(notification['kind'], 'dispatcher_trip')
        self.assertEqual(notification['url'], '/dispatcher/control/')
        self.assertIsNone(notification_for_event(self.event('trip_changed', 'save')))
        self.assertIsNone(notification_for_event(self.event('personnel_changed', 'restored')))

    @patch('users.webpush._deliver', return_value=(True, 201))
    @patch('users.webpush.push_is_configured', return_value=True)
    def test_role_delivery_wakes_only_dispatcher_web_subscription(self, _configured, deliver):
        delivered = notify_role_web_subscribers(
            'dispatcher',
            title='Самосвал загружен',
            body='Пульт обновлён.',
            url='/dispatcher/control/',
            tag='dispatcher-trip',
            kind='dispatcher_trip',
        )

        self.assertEqual(delivered, 1)
        deliver.assert_called_once_with(
            self.dispatcher_subscription.endpoint,
            timeout_seconds=3,
        )
        notification = PushNotification.objects.get()
        self.assertEqual(notification.employee, self.dispatcher)
        self.assertEqual(notification.kind, 'dispatcher_trip')
        self.assertFalse(PushNotification.objects.filter(employee=self.driver).exists())
        self.driver_subscription.refresh_from_db()
        self.assertIsNone(self.driver_subscription.last_success_at)

    def test_pending_queue_is_scoped_to_current_role(self):
        dispatcher_item = PushNotification.objects.create(
            employee=self.dispatcher,
            title='Для диспетчера',
            kind='dispatcher_trip',
        )
        PushNotification.objects.create(
            employee=self.dispatcher,
            title='Для другой роли',
            kind='driver_trip_loaded',
        )
        self.login_as(self.dispatcher_access)

        response = self.client.get(reverse('push_pending'))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item['id'] for item in payload['notifications']], [dispatcher_item.id])
        self.assertEqual(payload['badge'], 1)

    def test_dispatcher_cannot_mark_other_role_notification_as_shown(self):
        own = PushNotification.objects.create(
            employee=self.dispatcher,
            title='Для диспетчера',
            kind='dispatcher_trip',
        )
        foreign = PushNotification.objects.create(
            employee=self.dispatcher,
            title='Для другой роли',
            kind='driver_trip_loaded',
        )
        self.login_as(self.dispatcher_access)

        response = self.client.post(
            reverse('push_mark_shown'),
            data={'ids': [own.id, foreign.id]},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        own.refresh_from_db()
        foreign.refresh_from_db()
        self.assertIsNotNone(own.shown_at)
        self.assertIsNone(foreign.shown_at)

    @patch('core.dispatcher_push.send_dispatcher_push_for_event')
    def test_operational_event_schedules_push_only_after_commit(self, send):
        with self.captureOnCommitCallbacks(execute=True):
            bump_operational_state(
                'Trip:truck_loaded',
                event_type='trip_changed',
                object_type='Trip',
                object_id='44',
                payload={'action': 'truck_loaded', 'trip_id': 44},
            )

        send.assert_called_once()


class DispatcherServiceWorkerPushContractTests(TestCase):
    def test_dispatcher_service_worker_has_closed_window_push_contract(self):
        response = self.client.get(reverse('dispatcher_service_worker'))
        script = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        self.assertIn('dispatcher-desktop-shell-v144', script)
        self.assertIn('/static/js/dispatcher-transport-v1.js', script)
        self.assertIn('/static/js/dispatcher-detail-v1.js', script)
        self.assertIn('/static/js/dispatcher-board-v1.js', script)
        self.assertIn('/static/js/dispatcher-realtime-v1.js', script)
        self.assertIn('self.addEventListener("push"', script)
        self.assertIn('hasVisibleDispatcherWindow', script)
        self.assertIn('client.visibilityState === "visible"', script)
        self.assertIn('self.registration.showNotification', script)
        self.assertIn('self.clients.openWindow(target)', script)
