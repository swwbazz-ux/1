import json
from types import SimpleNamespace
from datetime import timedelta
from uuid import uuid4

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import manual_loading as manual_loading_module
from . import tests as fixtures
from .manual_loading import truck_driver_participation
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
        first = self.send()
        self.assertEqual(first.status_code, 200, first.content)
        old = Trip.objects.get(pk=first.json()['trip_id'])
        response = self.send(previous=old)
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

    def test_stale_next_send_and_retries_do_not_create_extra_trips(self):
        action = str(uuid4())
        response = self.send(action=action)
        self.assertEqual(response.status_code, 200, response.content)
        old = Trip.objects.get(pk=response.json()['trip_id'])
        self.assertTrue(self.send(action=action).json()['deduplicated'])
        self.assertEqual(self.send().status_code, 409)
        self.assertEqual(self.send(previous=old).status_code, 200)
        self.assertEqual(self.send(previous=old).status_code, 409)
        self.assertEqual(Trip.objects.count(), 2)

    def test_driver_joins_receives_only_next_trip(self):
        response = self.send()
        self.assertEqual(response.status_code, 200, response.content)
        old = Trip.objects.get(pk=response.json()['trip_id'])
        self.presence()
        self.unload(old)
        old.refresh_from_db()
        self.assertEqual(old.status, TripStatus.LOADED_WAITING_UNLOAD)
        response = self.send(manual=False, previous=old)
        self.assertEqual(response.status_code, 200, response.content)
        new = Trip.objects.get(pk=response.json()['trip_id'])
        self.assertEqual(new.driver_control_shift_id, self.truck_shift.pk)
        self.unload(new)
        new.refresh_from_db()
        self.assertEqual(new.status, TripStatus.COMPLETED)

    def test_online_background_recent_do_not_allow_overriding_controlled_trip(self):
        for kind in ('online', 'background', 'recent'):
            with self.subTest(kind=kind):
                self.presence(kind)
                self.assertFalse(truck_driver_participation([self.truck.pk])[self.truck.pk]['passive'])
        response = self.send(manual=False)
        self.assertEqual(response.status_code, 200, response.content)
        trip = Trip.objects.get(pk=response.json()['trip_id'])
        self.assertEqual(self.send(previous=trip).status_code, 409)

    def test_late_confirmation_changes_only_original_trip(self):
        self.presence()
        response = self.send(manual=False)
        self.assertEqual(response.status_code, 200, response.content)
        old = Trip.objects.get(pk=response.json()['trip_id'])
        unloaded_at = timezone.now()
        self.presence('offline')
        response = self.send(previous=old)
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
        response = self.send()
        self.assertEqual(response.status_code, 200, response.content)
        old = Trip.objects.get(pk=response.json()['trip_id'])
        self.capacity_rule.delete()
        response = self.send(previous=old)
        self.assertEqual(response.status_code, 409, response.content)
        old.refresh_from_db()
        self.assertEqual(old.status, TripStatus.LOADED_WAITING_UNLOAD)

    def test_reassignment_preserves_first_excavator_and_operator(self):
        response = self.send()
        self.assertEqual(response.status_code, 200, response.content)
        old = Trip.objects.get(pk=response.json()['trip_id'])
        HaulAssignment.objects.filter(truck=self.truck).update(excavator=self.other_excavator)
        self.shift.equipment = self.other_excavator
        self.shift.save()
        response = self.send(previous=old, excavator_id=self.other_excavator.pk)
        self.assertEqual(response.status_code, 200, response.content)
        old.refresh_from_db()
        self.assertEqual(old.excavator_id, self.excavator.pk)
        self.assertEqual(old.excavator_operator_id, self.operator.pk)
        self.assertEqual(Trip.objects.get(pk=response.json()['trip_id']).excavator_id, self.other_excavator.pk)

    def test_manual_cannot_bypass_breakdown_or_inactive_truck(self):
        reason, _ = DowntimeReason.objects.get_or_create(name='Поломка', defaults={'is_critical': True})
        event = DowntimeEvent.objects.create(equipment=self.truck, reason=reason, started_at=timezone.now())
        self.assertEqual(self.send().status_code, 409)
        event.delete()
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
        expected_deadline = trip.created_at + timedelta(minutes=5)
        self.assertEqual(response.json()['dump_badge_auto_hide_at'], expected_deadline.isoformat())

        fresh = self.client.get(reverse('excavator_work'))
        dump_card = next(card for card in fresh.context['dump_cards'] if card['point'].id == self.dump_point.id)
        self.assertEqual([row['trip_id'] for row in dump_card['pending_trucks']], [trip.id])
        self.assertEqual(dump_card['pending_trucks'][0]['auto_hide_at'], expected_deadline)

        expired_created_at = timezone.now() - timedelta(minutes=5, seconds=1)
        Trip.objects.filter(pk=trip.pk).update(created_at=expired_created_at)
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
        action = 'manual-preview-retry'
        first = self.send(action=action)
        self.assertEqual(first.status_code, 200, first.content)
        old = Trip.objects.get(pk=first.json()['trip_id'])
        retry = self.send(action=action)
        self.assertTrue(retry.json()['deduplicated'])
        self.assertEqual(retry.json()['trip_id'], old.id)
        self.assertEqual(retry.json()['dump_badge_auto_hide_at'], first.json()['dump_badge_auto_hide_at'])

        replacement = self.send(previous=old, action='manual-preview-replacement')
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
        first = self.send()
        self.assertEqual(first.status_code, 200, first.content)
        old = Trip.objects.get(pk=first.json()['trip_id'])
        response = self.send(previous=old)
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

    def test_shift_closed_after_loading_also_expires(self):
        """Боевой случай 20.09.2026, рейс id 1816 на бою: driver_control_shift
        был установлен ПРАВИЛЬНО в момент погрузки, но та смена закрылась через
        минуту, и рейс провисел «на разгрузку» больше четырёх часов — старая
        проверка смотрела только на пустой control_shift, а этот не пуст,
        просто указывает на мёртвую смену."""
        self.presence('online')
        response = self.send(action='closed-shift-orphan')
        self.assertEqual(response.status_code, 200, response.content)
        trip = Trip.objects.get(pk=response.json()['trip_id'])
        self.assertEqual(trip.driver_control_shift_id, self.truck_shift.pk)
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)

        stale = timezone.now() - timedelta(seconds=400)
        self.truck_shift.closed_at = stale
        self.truck_shift.save(update_fields=['closed_at'])

        cleared = manual_loading_module.reconcile_expired_manual_trips_throttled()
        self.assertEqual(cleared, [trip.pk])
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
