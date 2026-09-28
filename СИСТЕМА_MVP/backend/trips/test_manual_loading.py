import json
from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.db.models.query import QuerySet
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import manual_loading as manual_loading_module
from . import tests as fixtures
from .manual_loading import manual_dump_card_expires_at, truck_driver_participation
from .models import OPEN_TRIP_STATUSES, Trip, TripStatus
from assignments.models import AssignmentStatus, HaulAssignment
from assignments.services import schedule_haul_release
from downtimes.models import DowntimeEvent, DowntimeReason
from shifts.models import EmployeeShift
from shifts.services import calculate_open_shift_progress
from users.models import ActiveApplicationSession
from core.realtime import event_is_relevant
from reports.driver_shift_timeline import _trip_spans, _trip_quality_flags, _trip_is_open_at


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class ManualLoadingTests(TestCase):
    create_registered_driver_shift = fixtures.ExcavatorWorkServerIntegrationTests.create_registered_driver_shift
    def setUp(self):
        fixtures.ExcavatorWorkServerIntegrationTests.setUp(self)
        self.shift = EmployeeShift.objects.get(employee=self.operator, closed_at__isnull=True)

    def send(self, *, manual=True, previous=None, action=None, **extra):
        payload = dict(client_action_id=action or str(uuid4()), truck_id=self.truck.pk,
                       excavator_id=self.excavator.pk, dump_point_id=self.dump_point.pk,
                       rock_type=self.rock.pk, manual_control=manual,
                       expected_open_trip_id=previous.pk if previous else '')
        payload.update(extra)
        return self.client.post(reverse('excavator_truck_loaded'), json.dumps(payload), content_type='application/json')

    def presence(self, kind='online'):
        ActiveApplicationSession.objects.filter(access=self.driver_access).delete()
        now = timezone.now()
        seen = now if kind in {'online', 'background'} else now - timedelta(minutes=4 if kind == 'recent' else 40)
        return ActiveApplicationSession.objects.create(
            session_key='driver-test', access=self.driver_access, app_code='driver', role_code='driver',
            last_seen_at=seen, foreground_seen_at=None if kind == 'background' else seen,
            background_seen_at=seen if kind == 'background' else None,
        )

    def unload(self, trip, **payload):
        client = Client()
        session = client.session
        session['employee_access_id'] = self.driver_access.pk
        session.save()
        return client.post(reverse('driver_complete_trip', args=[trip.pk]),
                           dict(client_action_id=str(uuid4()), **payload))

    def distinct_load_times(self):
        """Two taps far enough apart to represent two physical loads."""
        second_at = timezone.now()
        return second_at - timedelta(minutes=6), second_at

    def test_no_driver_shift_no_fake_shift_and_loading_counts_immediately(self):
        self.truck_shift.closed_at = timezone.now()
        self.truck_shift.save()
        response = self.send()
        self.assertEqual(response.status_code, 200, response.content)
        trip = Trip.objects.get(pk=response.json()['trip_id'])
        self.assertIsNone(trip.driver_control_shift_id)
        self.assertIsNone(trip.driver_id)
        self.assertIsNone(trip.actual_dump_point_id)
        self.assertIsNone(trip.completed_at)
        self.assertEqual(calculate_open_shift_progress(self.shift)['trip_count'], 1)

    def test_passive_requires_deliberate_manual_action(self):
        self.assertEqual(self.send(manual=False).status_code, 409)
        self.assertEqual(self.send().status_code, 200)

    def test_next_loading_closes_previous_without_inventing_unload(self):
        first_at, second_at = self.distinct_load_times()
        first = self.send(occurred_at=first_at.isoformat())
        self.assertEqual(first.status_code, 200, first.content)
        old = Trip.objects.get(pk=first.json()['trip_id'])
        response = self.send(previous=old, occurred_at=second_at.isoformat())
        self.assertEqual(response.status_code, 200, response.content)
        old.refresh_from_db()
        self.assertEqual(old.status, TripStatus.UNCONTROLLED)
        self.assertIsNone(old.completed_at)
        self.assertIsNone(old.unloading_shift_id)
        self.assertEqual(old.closure_recorded_by_id, self.operator.pk)
        self.assertEqual(old.superseded_by_id, response.json()['trip_id'])
        self.assertIn('uncontrolled_unload', _trip_quality_flags(old))
        self.assertFalse(_trip_spans([old], old.created_at, timezone.now()))
        self.assertFalse(_trip_is_open_at(old, timezone.now()))
        self.assertEqual(Trip.objects.filter(truck=self.truck, status__in=OPEN_TRIP_STATUSES).count(), 1)
        self.assertEqual(calculate_open_shift_progress(self.shift)['trip_count'], 2)

    def test_unique_load_taps_create_trips_and_exact_retry_is_deduplicated(self):
        first_at, second_at = self.distinct_load_times()
        first_action = str(uuid4())
        response = self.send(action=first_action, occurred_at=first_at.isoformat())
        self.assertEqual(response.status_code, 200, response.content)
        old = Trip.objects.get(pk=response.json()['trip_id'])
        self.assertTrue(self.send(
            action=first_action,
            occurred_at=first_at.isoformat(),
        ).json()['deduplicated'])

        second_action = str(uuid4())
        second = self.send(
            action=second_action,
            previous=old,
            occurred_at=second_at.isoformat(),
        )
        self.assertEqual(second.status_code, 200, second.content)
        self.assertTrue(self.send(
            action=second_action,
            previous=old,
            occurred_at=second_at.isoformat(),
        ).json()['deduplicated'])
        self.assertEqual(Trip.objects.count(), 2)

    def test_driver_joins_receives_only_next_trip(self):
        first_at, second_at = self.distinct_load_times()
        response = self.send(occurred_at=first_at.isoformat())
        self.assertEqual(response.status_code, 200, response.content)
        old = Trip.objects.get(pk=response.json()['trip_id'])
        self.presence()
        self.unload(old)
        old.refresh_from_db()
        self.assertEqual(old.status, TripStatus.LOADED_WAITING_UNLOAD)
        response = self.send(
            manual=False,
            previous=old,
            occurred_at=second_at.isoformat(),
        )
        self.assertEqual(response.status_code, 200, response.content)
        new = Trip.objects.get(pk=response.json()['trip_id'])
        self.assertEqual(new.driver_control_shift_id, self.truck_shift.pk)
        self.unload(new)
        new.refresh_from_db()
        self.assertEqual(new.status, TripStatus.COMPLETED)

    def test_online_background_recent_load_supersedes_controlled_trip(self):
        for kind in ('online', 'background', 'recent'):
            with self.subTest(kind=kind):
                self.presence(kind)
                self.assertFalse(truck_driver_participation([self.truck.pk])[self.truck.pk]['passive'])
        first_at, second_at = self.distinct_load_times()
        response = self.send(manual=False, occurred_at=first_at.isoformat())
        self.assertEqual(response.status_code, 200, response.content)
        old_trip = Trip.objects.get(pk=response.json()['trip_id'])
        second = self.send(previous=old_trip, occurred_at=second_at.isoformat())
        self.assertEqual(second.status_code, 200, second.content)
        old_trip.refresh_from_db()
        new_trip = Trip.objects.get(pk=second.json()['trip_id'])
        self.assertEqual(old_trip.status, TripStatus.UNCONTROLLED)
        self.assertEqual(old_trip.superseded_by_id, new_trip.id)
        self.assertEqual(old_trip.driver_id, self.truck_shift.employee_id)
        self.assertEqual(new_trip.status, TripStatus.LOADED_WAITING_UNLOAD)

    def test_late_confirmation_changes_only_original_trip(self):
        self.presence()
        first_at, second_at = self.distinct_load_times()
        unloaded_at = second_at - timedelta(minutes=1)
        response = self.send(manual=False, occurred_at=first_at.isoformat())
        self.assertEqual(response.status_code, 200, response.content)
        old = Trip.objects.get(pk=response.json()['trip_id'])
        self.presence('offline')
        response = self.send(previous=old, occurred_at=second_at.isoformat())
        self.assertEqual(response.status_code, 200, response.content)
        new = Trip.objects.get(pk=response.json()['trip_id'])
        self.unload(old, occurred_at=unloaded_at.isoformat())
        old.refresh_from_db(); new.refresh_from_db()
        self.assertEqual(old.status, TripStatus.COMPLETED)
        self.assertEqual(old.completed_at, unloaded_at)
        self.assertGreater(old.unload_received_at, old.completed_at)
        self.assertEqual(new.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertEqual(calculate_open_shift_progress(self.shift)['trip_count'], 2)

    def test_failed_new_load_does_not_close_previous(self):
        first_at, second_at = self.distinct_load_times()
        response = self.send(occurred_at=first_at.isoformat())
        self.assertEqual(response.status_code, 200, response.content)
        old = Trip.objects.get(pk=response.json()['trip_id'])
        self.capacity_rule.delete()
        response = self.send(previous=old, occurred_at=second_at.isoformat())
        self.assertEqual(response.status_code, 409, response.content)
        old.refresh_from_db()
        self.assertEqual(old.status, TripStatus.LOADED_WAITING_UNLOAD)

    def test_reassignment_preserves_first_excavator_and_operator(self):
        first_at, second_at = self.distinct_load_times()
        response = self.send(occurred_at=first_at.isoformat())
        self.assertEqual(response.status_code, 200, response.content)
        old = Trip.objects.get(pk=response.json()['trip_id'])
        HaulAssignment.objects.filter(truck=self.truck).update(excavator=self.other_excavator)
        self.shift.equipment = self.other_excavator
        self.shift.save()
        response = self.send(
            previous=old,
            excavator_id=self.other_excavator.pk,
            occurred_at=second_at.isoformat(),
        )
        self.assertEqual(response.status_code, 200, response.content)
        old.refresh_from_db()
        self.assertEqual(old.excavator_id, self.excavator.pk)
        self.assertEqual(old.excavator_operator_id, self.operator.pk)
        self.assertEqual(Trip.objects.get(pk=response.json()['trip_id']).excavator_id, self.other_excavator.pk)

    def test_manual_load_closes_breakdown_but_inactive_truck_stays_blocked(self):
        reason, _ = DowntimeReason.objects.get_or_create(name='Поломка', defaults={'is_critical': True})
        event = DowntimeEvent.objects.create(equipment=self.truck, reason=reason, started_at=timezone.now())
        loaded = self.send()
        self.assertEqual(loaded.status_code, 200, loaded.content)
        event.refresh_from_db()
        self.assertIsNotNone(event.ended_at)
        self.assertEqual(getattr(event, 'closure_reason', ''), 'work_resumed_by_load')
        Trip.objects.update(status=TripStatus.CANCELLED, cancelled_at=timezone.now())
        self.truck.is_active = False
        self.truck.save()
        self.assertEqual(self.send().status_code, 409)

    @override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=False)
    def test_feature_can_be_disabled(self):
        self.truck_shift.closed_at = timezone.now()
        self.truck_shift.save()
        self.assertEqual(self.send().status_code, 409)

    def test_screen_exposes_manual_permission_and_presence(self):
        response = self.client.get(reverse('excavator_work'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-eo-manual-available="1"')
        self.assertContains(response, 'eo-driver-presence')

    def test_passive_truck_remains_manually_loadable_while_release_is_pending(self):
        self.truck_shift.closed_at = timezone.now()
        self.truck_shift.save(update_fields=['closed_at'])
        pending, created = schedule_haul_release(
            truck=self.truck,
            assigned_by=self.operator,
        )
        self.assertTrue(created)

        screen = self.client.get(reverse('excavator_work'))
        card = next(
            item for item in screen.context['truck_cards']
            if item['assignment'].truck_id == self.truck.id
        )
        self.assertEqual(card['transfer']['kind'], 'release')
        self.assertEqual(card['transfer']['route_label'], 'В свободные')
        self.assertTrue(card['manual_available'])
        self.assertNotEqual(card['load_block_reason_code'], 'transfer_outgoing')
        self.assertContains(screen, 'data-eo-manual-available="1"')

        loaded = self.send(action='manual-load-before-release-deadline')
        self.assertEqual(loaded.status_code, 200, loaded.content)
        pending.refresh_from_db()
        self.assertEqual(pending.status, AssignmentStatus.PENDING)
        self.assertIsNone(pending.ended_at)
        trip = Trip.objects.get(pk=loaded.json()['trip_id'])
        self.assertIsNone(trip.driver_control_shift_id)
        self.assertEqual(trip.excavator_id, self.excavator.id)

    def test_manual_dump_badge_expiry_reconciles_passive_trip(self):
        response = self.send(action='manual-preview-expiry')
        self.assertEqual(response.status_code, 200, response.content)
        trip = Trip.objects.get(pk=response.json()['trip_id'])
        expected_deadline = manual_dump_card_expires_at(trip)
        self.assertEqual(response.json()['dump_badge_auto_hide_at'], expected_deadline.isoformat())

        fresh = self.client.get(reverse('excavator_work'))
        dump_card = next(card for card in fresh.context['dump_cards'] if card['point'].id == self.dump_point.id)
        self.assertEqual([row['trip_id'] for row in dump_card['pending_trucks']], [trip.id])
        self.assertEqual(dump_card['pending_trucks'][0]['auto_hide_at'], expected_deadline)

        expired_created_at = timezone.now() - timedelta(minutes=5, seconds=1)
        Trip.objects.filter(pk=trip.pk).update(
            created_at=expired_created_at,
            loaded_at=expired_created_at,
        )
        expired = self.client.get(reverse('excavator_work'))
        dump_card = next(card for card in expired.context['dump_cards'] if card['point'].id == self.dump_point.id)
        self.assertEqual(dump_card['pending_trucks'], [])
        self.assertEqual(expired.context['active_trips_count'], 0)
        truck_card = next(
            card for card in expired.context['truck_cards']
            if card['assignment'].truck_id == self.truck.id
        )
        self.assertEqual(truck_card['open_trip_id'], '')
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.UNCONTROLLED)
        self.assertEqual(
            trip.operationally_closed_at,
            expired_created_at + timedelta(minutes=5),
        )
        self.assertIsNone(trip.completed_at)
        self.assertIsNone(trip.unload_received_at)

    def test_controlled_trip_badge_does_not_expire_after_five_minutes(self):
        self.presence()
        response = self.send(manual=False, action='controlled-preview')
        self.assertEqual(response.status_code, 200, response.content)
        trip = Trip.objects.get(pk=response.json()['trip_id'])
        self.assertEqual(response.json()['dump_badge_auto_hide_at'], '')
        Trip.objects.filter(pk=trip.pk).update(created_at=timezone.now() - timedelta(minutes=30))

        screen = self.client.get(reverse('excavator_work'))
        dump_card = next(card for card in screen.context['dump_cards'] if card['point'].id == self.dump_point.id)
        self.assertEqual([row['trip_id'] for row in dump_card['pending_trucks']], [trip.id])
        self.assertIsNone(dump_card['pending_trucks'][0]['auto_hide_at'])

    def test_manual_dump_badge_retry_and_replacement_keep_trip_specific_deadlines(self):
        first_at, second_at = self.distinct_load_times()
        action = 'manual-preview-retry'
        first = self.send(action=action, occurred_at=first_at.isoformat())
        self.assertEqual(first.status_code, 200, first.content)
        old = Trip.objects.get(pk=first.json()['trip_id'])
        retry = self.send(action=action, occurred_at=first_at.isoformat())
        self.assertTrue(retry.json()['deduplicated'])
        self.assertEqual(retry.json()['trip_id'], old.id)
        self.assertEqual(retry.json()['dump_badge_auto_hide_at'], first.json()['dump_badge_auto_hide_at'])

        replacement = self.send(
            previous=old,
            action='manual-preview-replacement',
            occurred_at=second_at.isoformat(),
        )
        self.assertEqual(replacement.status_code, 200, replacement.content)
        new = Trip.objects.get(pk=replacement.json()['trip_id'])
        old.refresh_from_db()
        self.assertEqual(old.status, TripStatus.UNCONTROLLED)
        self.assertGreater(new.created_at, old.created_at)
        screen = self.client.get(reverse('excavator_work'))
        dump_card = next(card for card in screen.context['dump_cards'] if card['point'].id == self.dump_point.id)
        self.assertEqual([row['trip_id'] for row in dump_card['pending_trucks']], [new.id])

    def test_driver_connection_and_reassignment_do_not_move_or_adopt_manual_preview(self):
        response = self.send(action='manual-preview-owner')
        self.assertEqual(response.status_code, 200, response.content)
        trip = Trip.objects.get(pk=response.json()['trip_id'])
        deadline = response.json()['dump_badge_auto_hide_at']
        self.presence()
        HaulAssignment.objects.filter(truck=self.truck).update(excavator=self.other_excavator)

        screen = self.client.get(reverse('excavator_work'))
        dump_card = next(card for card in screen.context['dump_cards'] if card['point'].id == self.dump_point.id)
        self.assertEqual([row['trip_id'] for row in dump_card['pending_trucks']], [trip.id])
        self.assertEqual(dump_card['pending_trucks'][0]['auto_hide_at'].isoformat(), deadline)
        trip.refresh_from_db()
        self.assertEqual(trip.excavator_id, self.excavator.id)
        self.assertIsNone(trip.driver_control_shift_id)

    def test_driver_screen_does_not_inherit_old_manual_trip(self):
        self.assertEqual(self.send().status_code, 200)
        self.presence()
        session = self.client.session
        session['employee_access_id'] = self.driver_access.pk
        session.save()
        response = self.client.get(reverse('driver_shift'))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['active_trip'])

    def test_confirming_unload_keeps_loading_volume_snapshot(self):
        self.presence()
        response = self.send(manual=False)
        self.assertEqual(response.status_code, 200, response.content)
        trip = Trip.objects.get(pk=response.json()['trip_id'])
        old_volume = trip.volume_m3
        self.capacity_rule.volume_m3 = 100
        self.capacity_rule.save()
        self.unload(trip)
        trip.refresh_from_db()
        self.assertEqual(trip.volume_m3, old_volume)

    def test_unload_outbox_ack_is_bound_to_trip_and_action(self):
        self.presence()
        response = self.send(manual=False)
        self.assertEqual(response.status_code, 200, response.content)
        trip = Trip.objects.get(pk=response.json()['trip_id'])
        session = self.client.session
        session['employee_access_id'] = self.driver_access.pk
        session.save()
        payload = dict(client_action_id='queued-confirmation', occurred_at=timezone.now().isoformat())
        url = reverse('driver_complete_trip', args=[trip.pk])
        first = self.client.post(url, payload, HTTP_ACCEPT='application/json')
        again = self.client.post(url, payload, HTTP_ACCEPT='application/json')
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(first.json(), again.json())
        self.assertEqual(first.json()['trip_id'], trip.pk)
        self.assertEqual(first.json()['client_action_id'], payload['client_action_id'])

    def test_cancel_next_loading_restores_previous_operational_trip(self):
        first_at, second_at = self.distinct_load_times()
        first = self.send(occurred_at=first_at.isoformat())
        self.assertEqual(first.status_code, 200, first.content)
        old = Trip.objects.get(pk=first.json()['trip_id'])
        response = self.send(previous=old, occurred_at=second_at.isoformat())
        new = Trip.objects.get(pk=response.json()['trip_id'])
        response = self.client.post(reverse('excavator_truck_loaded_cancel'), json.dumps(dict(
            client_action_id='cancel-new', trip_id=new.pk, truck_id=new.truck_id,
            dump_point_id=new.dump_point_id,
        )), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        old.refresh_from_db(); new.refresh_from_db()
        self.assertEqual(old.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertIsNone(old.operationally_closed_at)
        self.assertEqual(new.status, TripStatus.CANCELLED)
        self.assertEqual(calculate_open_shift_progress(self.shift)['trip_count'], 1)

    def test_driver_notifications_exclude_manual_and_other_shift_loads(self):
        def event(shift_id):
            return SimpleNamespace(event_type='trip_changed', payload={
                'action': 'truck_loaded', 'truck_id': self.truck.pk,
                'driver_participation_recorded': True, 'driver_control_shift_id': shift_id,
            })
        self.assertFalse(event_is_relevant(event(None), self.driver_access))
        self.assertFalse(event_is_relevant(event(self.shift.pk), self.driver_access))
        self.assertTrue(event_is_relevant(event(self.truck_shift.pk), self.driver_access))


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class ManualTripAutoReconcileTests(TestCase):
    """Боевой случай 20.09.2026: пульт диспетчера показывал самосвал «на
    разгрузку», а экран водителя — «на загрузку»; и пульт, и экскаваторщик
    держали устаревшую картинку сколько угодно долго, пока кто-то вручную не
    перезагружал страницу целиком. Обычный фоновый опрос (тот же запрос,
    который телефон и браузер каждой роли шлют каждые несколько секунд) её не
    гасил — reconcile_expired_manual_trips() вызывался только из полной
    перезагрузки dispatcher_control_view / excavator_work_view."""

    create_registered_driver_shift = fixtures.ExcavatorWorkServerIntegrationTests.create_registered_driver_shift
    send = ManualLoadingTests.send
    presence = ManualLoadingTests.presence

    def setUp(self):
        fixtures.ExcavatorWorkServerIntegrationTests.setUp(self)
        self.shift = EmployeeShift.objects.get(employee=self.operator, closed_at__isnull=True)
        manual_loading_module._manual_trip_reconcile_next_check = 0.0
        manual_loading_module._manual_trip_reconcile_running = False
        try:
            manual_loading_module._MANUAL_TRIP_RECONCILE_LOCK_PATH.unlink()
        except FileNotFoundError:
            pass

    def make_expired_passive_trip(self, **send_kwargs):
        self.truck_shift.closed_at = timezone.now()
        self.truck_shift.save(update_fields=['closed_at'])
        response = self.send(**send_kwargs)
        self.assertEqual(response.status_code, 200, response.content)
        trip = Trip.objects.get(pk=response.json()['trip_id'])
        self.assertIsNone(trip.driver_control_shift_id)
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        stale = timezone.now() - timedelta(seconds=400)
        Trip.objects.filter(pk=trip.pk).update(created_at=stale, loaded_at=stale)
        # Смена должна снова быть открытой, чтобы следующая погрузка на этот
        # самосвал не упала на «нет смены на технике» в других тестах ниже.
        self.truck_shift.closed_at = None
        self.truck_shift.save(update_fields=['closed_at'])
        return trip

    def test_ordinary_poll_expires_a_stale_passive_trip_for_every_role(self):
        trip = self.make_expired_passive_trip()

        # Не полная перезагрузка страницы — тот самый realtime-опрос,
        # который каждая роль шлёт каждые несколько секунд.
        response = self.client.get(
            reverse('operational_state_version'), {'include_events': '0'},
        )
        self.assertEqual(response.status_code, 200)

        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.UNCONTROLLED)

    def test_reconcile_is_throttled_across_consecutive_polls(self):
        first = self.make_expired_passive_trip(action='throttle-first')
        self.assertEqual(
            manual_loading_module.reconcile_expired_manual_trips_throttled(),
            [first.pk],
        )

        second = self.make_expired_passive_trip(action='throttle-second')
        # В пределах окна троттлинга второй вызов подряд ничего не делает —
        # иначе каждый опрос долбил бы базу проверкой раз в несколько секунд.
        self.assertEqual(
            manual_loading_module.reconcile_expired_manual_trips_throttled(),
            [],
        )
        second.refresh_from_db()
        self.assertEqual(second.status, TripStatus.LOADED_WAITING_UNLOAD)

    @override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=False)
    def test_throttled_reconcile_is_a_noop_when_manual_loading_is_disabled(self):
        self.assertEqual(
            manual_loading_module.reconcile_expired_manual_trips_throttled(),
            [],
        )

    def test_carryover_survives_background_expiry_and_replacement_driver_unloads(self):
        self.presence('online')
        response = self.send(action='carryover-after-shift-close')
        self.assertEqual(response.status_code, 200, response.content)
        trip = Trip.objects.get(pk=response.json()['trip_id'])
        original_driver = trip.driver
        self.assertEqual(trip.driver_control_shift_id, self.truck_shift.pk)
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)

        stale = timezone.now() - timedelta(seconds=400)
        self.truck_shift.closed_at = stale
        self.truck_shift.save(update_fields=['closed_at'])
        trip.is_carryover = True
        trip.save(update_fields=['is_carryover'])
        replacement, replacement_access, replacement_shift = self.create_registered_driver_shift(
            self.truck,
            full_name='Сменщик водителя',
            access_code='200099',
        )

        cleared = manual_loading_module.reconcile_expired_manual_trips_throttled()
        self.assertEqual(cleared, [])
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)

        client = Client()
        session = client.session
        session['employee_access_id'] = replacement_access.pk
        session.save()
        unloaded_at = replacement_shift.opened_at + timedelta(seconds=1)
        unload = client.post(
            reverse('driver_complete_trip', args=[trip.pk]),
            {
                'client_action_id': 'replacement-unloads-after-reconcile-window',
                'occurred_at': unloaded_at.isoformat(),
            },
            HTTP_ACCEPT='application/json',
        )

        self.assertEqual(unload.status_code, 200, unload.content)
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.driver, original_driver)
        self.assertEqual(trip.driver_control_shift, self.truck_shift)
        self.assertEqual(trip.unloading_shift, replacement_shift)

    def test_driver_trip_actions_lock_only_trip_across_nullable_control_shift_join(self):
        self.presence('online')
        loaded = self.send(action='driver-lock-scope-load')
        self.assertEqual(loaded.status_code, 200, loaded.content)
        trip = Trip.objects.get(pk=loaded.json()['trip_id'])
        self.assertEqual(trip.driver_control_shift_id, self.truck_shift.id)

        driver_client = Client()
        session = driver_client.session
        session['employee_access_id'] = self.driver_access.pk
        session.save()

        original_select_for_update = QuerySet.select_for_update
        trip_lock_scopes = []

        def record_lock_scope(queryset, *args, **kwargs):
            if queryset.model is Trip:
                trip_lock_scopes.append(tuple(kwargs.get('of') or ()))
            return original_select_for_update(queryset, *args, **kwargs)

        with patch.object(QuerySet, 'select_for_update', new=record_lock_scope):
            changed = driver_client.post(
                reverse('driver_change_unload_point', args=[trip.pk]),
                {
                    'client_action_id': 'driver-lock-scope-point',
                    'dump_point': self.dump_point.pk,
                },
            )
            point_lock_scopes = list(trip_lock_scopes)
            trip_lock_scopes.clear()
            unloaded = driver_client.post(
                reverse('driver_complete_trip', args=[trip.pk]),
                {'client_action_id': 'driver-lock-scope-unload'},
            )
            unload_lock_scopes = list(trip_lock_scopes)

        self.assertEqual(changed.status_code, 302, changed.content)
        self.assertEqual(unloaded.status_code, 302, unloaded.content)
        self.assertIn(('self',), point_lock_scopes, point_lock_scopes)
        self.assertIn(('self',), unload_lock_scopes, unload_lock_scopes)

    def test_legacy_closed_control_shift_without_carryover_marker_still_expires(self):
        self.presence('online')
        response = self.send(action='legacy-closed-shift-orphan')
        self.assertEqual(response.status_code, 200, response.content)
        trip = Trip.objects.get(pk=response.json()['trip_id'])
        self.assertFalse(trip.is_carryover)

        self.truck_shift.closed_at = timezone.now() - timedelta(seconds=400)
        self.truck_shift.save(update_fields=['closed_at'])

        self.assertEqual(
            manual_loading_module.reconcile_expired_manual_trips_throttled(),
            [trip.pk],
        )
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.UNCONTROLLED)

    def test_recently_closed_shift_is_not_expired_yet(self):
        """Смена, закрытая только что, не должна мгновенно гасить рейс — иначе
        водитель, честно закрывший смену сразу после погрузки, терял бы
        видимость своего же рейса раньше, чем успевает его выгрузить."""
        self.presence('online')
        response = self.send(action='fresh-closed-shift')
        self.assertEqual(response.status_code, 200, response.content)
        trip = Trip.objects.get(pk=response.json()['trip_id'])
        self.assertEqual(trip.driver_control_shift_id, self.truck_shift.pk)

        self.truck_shift.closed_at = timezone.now()
        self.truck_shift.save(update_fields=['closed_at'])

        self.assertEqual(manual_loading_module.reconcile_expired_manual_trips_throttled(), [])
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
