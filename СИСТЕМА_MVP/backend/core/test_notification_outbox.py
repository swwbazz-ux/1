from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.apps import apps
from django.core.management.color import no_style
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from core.models import NotificationDelivery, NotificationIntent
from core.notification_outbox import (
    claim_delivery, deliver_claimed, enqueue_notification, finish_delivery, run_notification_outbox,
)
from users.models import Employee, NativePushDevice, PushNotification, WebPushSubscription


class NotificationFixture:
    def setUp(self):
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                for sql in connection.ops.sequence_reset_sql(no_style(), apps.get_models()):
                    cursor.execute(sql)
        self.employee = Employee.objects.create(full_name='Получатель F3', is_active=True)
        self.other = Employee.objects.create(full_name='Другой получатель F3', is_active=True)
        self.subscription = WebPushSubscription.objects.create(
            employee=self.employee, endpoint='https://push.example.test/f3', role_code='driver',
            p256dh='generation-1', auth='key-1',
        )

    def enqueue(self, **changes):
        payload = dict(source_key='offline:load-one', employee_id=self.employee.pk, role_code='driver',
                       title='Погрузка', body='Точка 1', kind='driver_trip_loaded', version=22)
        payload.update(changes)
        return enqueue_notification(**payload)

    def native(self):
        return NativePushDevice.objects.create(employee=self.employee, token='token-f3',
                                               app_id='ru.copperresources.driver')


class NotificationOutboxTests(NotificationFixture, TransactionTestCase):
    reset_sequences = True

    @patch('users.native_push._deliver_fcm')
    @patch('users.webpush._deliver')
    def test_rollback_removes_text_intent_delivery_and_no_network_escapes(self, web, native):
        self.native()
        with self.assertRaisesRegex(RuntimeError, 'domain rollback'):
            with transaction.atomic():
                self.enqueue()
                self.assertEqual(NotificationIntent.objects.count(), 1)
                self.assertEqual(NotificationDelivery.objects.count(), 2)
                raise RuntimeError('domain rollback')
        self.assertEqual(NotificationIntent.objects.count(), 0)
        self.assertEqual(NotificationDelivery.objects.count(), 0)
        self.assertEqual(PushNotification.objects.count(), 0)
        web.assert_not_called()
        native.assert_not_called()

    @patch('users.webpush.push_is_configured', return_value=True)
    def test_committed_intent_survives_lost_callback_and_is_sent_outside_atomic_once(self, configured):
        with patch('users.webpush._deliver') as deliver:
            with transaction.atomic():
                intent = self.enqueue()
                self.enqueue(title='Изменившийся текущий текст', version=999)
                deliver.assert_not_called()
        intent.refresh_from_db()
        self.assertEqual(intent.payload['title'], 'Погрузка')
        self.assertEqual(intent.payload['version'], '22')
        self.assertEqual(PushNotification.objects.count(), 1)

        def send(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            return True, 201

        with patch('users.webpush._deliver', side_effect=send) as deliver:
            self.assertEqual(run_notification_outbox(), {'claimed': 1, 'delivered': 1})
            self.assertEqual(run_notification_outbox(), {'claimed': 0, 'delivered': 0})
        deliver.assert_called_once()
        self.assertIsNotNone(NotificationDelivery.objects.get().delivered_at)

    @patch('users.webpush._deliver')
    def test_worker_refuses_outer_atomic_even_if_inner_claim_would_commit(self, deliver):
        self.enqueue()
        with transaction.atomic():
            with self.assertRaisesRegex(RuntimeError, 'outside every transaction'):
                run_notification_outbox()
        deliver.assert_not_called()
        self.assertEqual(NotificationDelivery.objects.get().attempts, 0)

    @patch('users.webpush.push_is_configured', return_value=True)
    @patch('users.native_push.native_push_is_configured', return_value=True)
    @patch('users.webpush._deliver', return_value=(True, 201))
    @patch('users.native_push._deliver_fcm', side_effect=[(False, False), (True, False)])
    def test_transient_retry_only_retries_failed_target_with_same_effect_id(self, native, web, *configured):
        self.native()
        self.enqueue()
        self.assertEqual(run_notification_outbox(), {'claimed': 2, 'delivered': 1})
        self.assertEqual(run_notification_outbox()['claimed'], 0)
        NotificationDelivery.objects.filter(delivered_at__isnull=True).update(available_at=timezone.now())
        self.assertEqual(run_notification_outbox(), {'claimed': 1, 'delivered': 1})
        self.assertEqual(web.call_count, 1)
        self.assertEqual(native.call_count, 2)
        self.assertEqual(native.call_args_list[0].kwargs['extra_data'], native.call_args_list[1].kwargs['extra_data'])
        self.assertEqual(native.call_args_list[1].kwargs['state_version'], '22')
        self.assertEqual(PushNotification.objects.count(), 1)

    @patch('users.webpush.push_is_configured', return_value=True)
    @patch('users.webpush._deliver', return_value=(False, 410))
    def test_dead_target_is_terminal_but_provider_outage_is_not(self, deliver, configured):
        self.enqueue()
        run_notification_outbox()
        row = NotificationDelivery.objects.get()
        self.assertEqual(row.terminal_reason, 'target_gone')
        self.assertIsNone(row.delivered_at)
        self.assertEqual(run_notification_outbox()['claimed'], 0)
        deliver.assert_called_once()

    @patch('users.webpush.push_is_configured', return_value=False)
    @patch('users.webpush._deliver')
    def test_unconfigured_provider_preserves_pending_delivery(self, deliver, configured):
        self.enqueue()
        run_notification_outbox()
        row = NotificationDelivery.objects.get()
        self.assertEqual(row.last_error, 'provider_not_configured')
        self.assertEqual(row.terminal_reason, '')
        self.assertGreater(row.available_at, timezone.now())
        deliver.assert_not_called()

    @patch('users.webpush.push_is_configured', return_value=True)
    @patch('users.webpush._deliver', return_value=(True, 201))
    def test_expired_lease_recovery_fences_old_send_and_old_ack(self, deliver, configured):
        self.enqueue()
        old = claim_delivery()
        NotificationDelivery.objects.filter(pk=old.pk).update(leased_until=timezone.now() - timedelta(seconds=1))
        new = claim_delivery()
        self.assertEqual(old.pk, new.pk)
        self.assertNotEqual(old.lease_token, new.lease_token)
        self.assertEqual(finish_delivery(old, delivered=True), 0)
        self.assertFalse(deliver_claimed(old))
        deliver.assert_not_called()
        self.assertTrue(deliver_claimed(new))
        self.assertEqual(finish_delivery(old, error='late failure'), 0)
        row = NotificationDelivery.objects.get()
        self.assertIsNotNone(row.delivered_at)
        self.assertEqual(row.last_error, '')
        self.assertEqual(row.attempts, 2)

    @patch('users.webpush._deliver')
    @patch('users.native_push._deliver_fcm')
    def test_reassigned_web_and_native_target_never_receive_old_intent(self, native_send, web_send):
        device = self.native()
        self.enqueue()
        WebPushSubscription.objects.filter(pk=self.subscription.pk).update(employee=self.other)
        NativePushDevice.objects.filter(pk=device.pk).update(employee=self.other)
        self.assertEqual(run_notification_outbox(), {'claimed': 2, 'delivered': 0})
        self.assertEqual(list(NotificationDelivery.objects.values_list('terminal_reason', flat=True)),
                         ['target_changed', 'target_changed'])
        web_send.assert_not_called()
        native_send.assert_not_called()
        self.assertEqual(PushNotification.objects.get().employee_id, self.employee.pk)

    @patch('users.webpush._deliver')
    def test_changed_role_or_credential_generation_invalidates_old_target(self, deliver):
        self.enqueue()
        WebPushSubscription.objects.filter(pk=self.subscription.pk).update(role_code='dispatcher', auth='generation-2')
        run_notification_outbox()
        self.assertEqual(NotificationDelivery.objects.get().terminal_reason, 'target_changed')
        deliver.assert_not_called()

    def test_distinct_source_revisions_keep_two_legitimate_notifications(self):
        first = self.enqueue(source_key='trip:7:dump-point-change:action-A')
        second = self.enqueue(source_key='trip:7:dump-point-change:action-B', body='Точка 2', version=23)
        self.assertNotEqual(first.effect_key, second.effect_key)
        self.assertEqual(PushNotification.objects.count(), 2)

    @patch('users.native_push.native_push_is_configured', return_value=True)
    @patch('users.native_push._deliver_fcm', return_value=(True, False))
    def test_crash_after_send_before_ack_repeats_same_wake_and_keeps_one_text(self, native, configured):
        self.subscription.delete()
        self.native()
        self.enqueue()
        with patch('core.notification_outbox.finish_delivery', side_effect=RuntimeError('worker stopped')):
            with self.assertRaisesRegex(RuntimeError, 'worker stopped'):
                run_notification_outbox(limit=1)
        row = NotificationDelivery.objects.get()
        self.assertIsNone(row.delivered_at)
        self.assertTrue(row.lease_token)
        NotificationDelivery.objects.update(leased_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(run_notification_outbox(), {'claimed': 1, 'delivered': 1})
        self.assertEqual(native.call_count, 2)
        self.assertEqual(native.call_args_list[0].kwargs['extra_data'], native.call_args_list[1].kwargs['extra_data'])
        self.assertEqual(PushNotification.objects.count(), 1)


@skipUnless(connection.vendor == 'postgresql', 'Real PostgreSQL connections and row locks required')
class NotificationOutboxPostgresTests(NotificationFixture, TransactionTestCase):
    reset_sequences = True

    def race(self, functions):
        barrier = Barrier(2)

        def worker(function):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('lock_timeout', '8s', false)")
                    cursor.execute('SELECT pg_backend_pid()')
                    pid = cursor.fetchone()[0]
                barrier.wait(timeout=10)
                return pid, function()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, functions))
        self.assertEqual(len({pid for pid, _ in results}), 2)
        return [value for _, value in results]

    def test_two_connections_enqueue_same_effect_one_text_and_one_target(self):
        ids = self.race([lambda: self.enqueue().pk, lambda: self.enqueue().pk])
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(NotificationIntent.objects.count(), 1)
        self.assertEqual(PushNotification.objects.count(), 1)
        self.assertEqual(NotificationDelivery.objects.count(), 1)

    @patch('users.webpush.push_is_configured', return_value=True)
    @patch('users.webpush._deliver', return_value=(True, 201))
    def test_worker_cannot_observe_or_send_an_uncommitted_intent(self, deliver, configured):
        written = Barrier(2)
        checked = Barrier(2)

        def domain_writer():
            with transaction.atomic():
                intent = self.enqueue()
                written.wait(timeout=10)
                checked.wait(timeout=10)
                return intent.pk

        def worker_before_commit():
            written.wait(timeout=10)
            try:
                return run_notification_outbox()
            finally:
                checked.wait(timeout=10)

        results = self.race([domain_writer, worker_before_commit])
        self.assertEqual(results[1], {'claimed': 0, 'delivered': 0})
        deliver.assert_not_called()
        self.assertEqual(run_notification_outbox(), {'claimed': 1, 'delivered': 1})
        deliver.assert_called_once()

    @patch('users.webpush.push_is_configured', return_value=True)
    @patch('users.webpush._deliver', return_value=(True, 201))
    def test_two_workers_claim_one_committed_delivery_without_double_send(self, deliver, configured):
        self.enqueue()
        results = self.race([lambda: run_notification_outbox(limit=1), lambda: run_notification_outbox(limit=1)])
        self.assertEqual(sum(item['claimed'] for item in results), 1)
        self.assertEqual(sum(item['delivered'] for item in results), 1)
        deliver.assert_called_once()
        self.assertEqual(NotificationDelivery.objects.get().attempts, 1)
