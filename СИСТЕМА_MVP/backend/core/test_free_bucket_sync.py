import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

from django.apps import apps
from django.core.management.color import no_style
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.models import Sum
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    ExcavatorDumpPointSetting,
    ExcavatorPlacement,
    HaulAssignment,
)
from core.models import OfflineFieldEvent
from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import DumpPoint, Equipment
from shifts.models import EmployeeShift
from trips import tests as trip_fixtures
from trips.models import FreeBucketAcceptance, FreeBucketAcceptanceStatus, Trip, TripStatus
from users.models import AdminConflict, EmployeeAccess


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class FreeBucketServerIntegrationTests(TestCase):
    """Server contract for a one-load acceptance that does not reassign a truck."""

    create_registered_driver_shift = (
        trip_fixtures.ExcavatorWorkServerIntegrationTests.create_registered_driver_shift
    )

    def setUp(self):
        trip_fixtures.ExcavatorWorkServerIntegrationTests.setUp(self)
        from shifts.models import EmployeeShift

        self.url = reverse('offline_events_sync')
        self.shift = EmployeeShift.objects.get(employee=self.operator, closed_at__isnull=True)
        self.assignment = HaulAssignment.objects.get(
            truck=self.truck,
            excavator=self.excavator,
            status=AssignmentStatus.ACCEPTED,
        )
        for excavator in (self.excavator, self.other_excavator):
            placement = ExcavatorPlacement.objects.create(
                excavator=excavator,
                zone=ExcavatorPlacement.Zone.ACTIVE,
                work_rock_type=self.rock,
                work_dump_point=self.dump_point,
                loading_horizon='125',
                loading_block='4',
            )
            ExcavatorDumpPointSetting.objects.create(
                placement=placement,
                dump_point=self.dump_point,
                position=0,
            )
        self.driver_client = Client()
        driver_session = self.driver_client.session
        driver_session['employee_access_id'] = self.driver_access.id
        driver_session.save()

    def dispatcher_dashboard(self):
        from trips.views import build_dispatcher_dashboard_context

        return build_dispatcher_dashboard_context(
            dispatcher_shift=self.shift,
            active_trips=Trip.objects.filter(
                status__in=(TripStatus.ACTIVE, TripStatus.LOADED_WAITING_UNLOAD),
            ),
            pending_assignments=HaulAssignment.objects.filter(status=AssignmentStatus.PENDING),
            accepted_assignments=HaulAssignment.objects.filter(status=AssignmentStatus.ACCEPTED),
            recent_completed_trips=Trip.objects.none(),
            open_shifts=EmployeeShift.objects.filter(closed_at__isnull=True).exclude(pk=self.shift.pk),
            open_mechanic_downtimes=DowntimeEvent.objects.filter(ended_at__isnull=True),
            trucks=Equipment.objects.filter(equipment_type=self.truck_type).order_by('garage_number'),
            excavators=Equipment.objects.filter(equipment_type=self.excavator_type).order_by('garage_number'),
            recent_dispatcher_actions=[],
        )

    def dispatcher_primary_tile(self):
        return next(
            tile
            for complex_card in self.dispatcher_dashboard()['complex_cards']
            for tile in complex_card.get('active_truck_tiles', [])
            if tile.get('card_id') == str(self.truck.id)
        )

    def event(
        self,
        event_id,
        event_type,
        sequence,
        *,
        payload,
        occurred_at=None,
        depends_on=None,
        actor=None,
        access=None,
        shift=None,
        excavator=None,
        local_trip_id='',
    ):
        actor = actor or self.operator
        access = access or self.access
        shift = shift or self.shift
        excavator = excavator or self.excavator
        return {
            'event_id': event_id,
            'event_type': event_type,
            'format_version': 1,
            'actor_id': actor.id,
            'access_id': access.id,
            'role_code': 'excavator_operator',
            'occurred_at': (occurred_at or timezone.now()).isoformat(),
            'sequence': sequence,
            'depends_on': list(depends_on or []),
            'shift_id': shift.id,
            'equipment_id': excavator.id,
            'local_trip_id': local_trip_id,
            'context_snapshot': {
                'actor_id': actor.id,
                'access_id': access.id,
                'role_code': 'excavator_operator',
            },
            'payload': payload,
        }

    def sync(
        self,
        events,
        *,
        client=None,
        actor=None,
        access=None,
        role_code='excavator_operator',
        device_id='free-bucket-device-001',
    ):
        actor = actor or self.operator
        access = access or self.access
        return (client or self.client).post(
            self.url,
            data=json.dumps({
                'protocol_version': 1,
                'actor_id': actor.id,
                'access_id': access.id,
                'role_code': role_code,
                'device_id': device_id,
                'events': events,
            }),
            content_type='application/json',
        )

    def accept_event(self, event_id='free-accept-1', sequence=1, **identity):
        return self.event(
            event_id,
            'excavator.free_bucket.accepted',
            sequence,
            payload={'truck_id': self.truck.id},
            **identity,
        )

    def driver_event(
        self,
        event_id,
        event_type,
        sequence,
        *,
        payload,
        occurred_at=None,
        depends_on=None,
    ):
        return {
            'event_id': event_id,
            'event_type': event_type,
            'format_version': 1,
            'actor_id': self.driver.id,
            'access_id': self.driver_access.id,
            'role_code': 'driver',
            'occurred_at': (occurred_at or timezone.now()).isoformat(),
            'sequence': sequence,
            'depends_on': list(depends_on or []),
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'context_snapshot': {
                'actor_id': self.driver.id,
                'access_id': self.driver_access.id,
                'role_code': 'driver',
            },
            'payload': payload,
        }

    def select_event(self, event_id='driver-free-select-1', sequence=1, *, excavator=None, **kwargs):
        return self.driver_event(
            event_id,
            'driver.free_bucket.selected',
            sequence,
            payload={'excavator_id': (excavator or self.other_excavator).id},
            **kwargs,
        )

    def sync_driver(self, events, *, device_id='driver-free-bucket-001'):
        return self.sync(
            events,
            client=self.driver_client,
            actor=self.driver,
            access=self.driver_access,
            role_code='driver',
            device_id=device_id,
        )

    def other_excavator_identity(self):
        client, operator, shift = (
            trip_fixtures.ExcavatorWorkServerIntegrationTests.create_other_excavator_client(self)
        )
        access = EmployeeAccess.objects.get(employee=operator, role=self.role)
        return client, {
            'actor': operator,
            'access': access,
            'shift': shift,
            'excavator': self.other_excavator,
        }

    def load_event(
        self,
        acceptance_event,
        event_id='free-load-1',
        sequence=2,
        *,
        occurred_at=None,
        **identity,
    ):
        occurred_at = occurred_at or (
            timezone.datetime.fromisoformat(acceptance_event['occurred_at']) + timedelta(seconds=1)
        )

        return self.event(
            event_id,
            'excavator.free_bucket.loaded',
            sequence,
            occurred_at=occurred_at,
            depends_on=[acceptance_event['event_id']],
            local_trip_id=f'local-{event_id}',
            payload={
                'truck_id': self.truck.id,
                'free_bucket_acceptance_local_id': acceptance_event['event_id'],
                'dump_point_id': self.dump_point.id,
                'rock_type_id': self.rock.id,
                'manual_control': False,
                'loading_horizon': '125',
                'loading_block': '4',
            },
            **identity,
        )

    def test_database_rejects_requested_row_without_driver_context(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                FreeBucketAcceptance.objects.create(
                    client_acceptance_id='invalid-request-without-driver',
                    truck=self.truck,
                    excavator=self.other_excavator,
                    primary_assignment=self.assignment,
                    status=FreeBucketAcceptanceStatus.REQUESTED,
                    occurred_at=timezone.now(),
                )

    def test_driver_then_excavator_promotes_the_same_request_without_trip_or_reassignment(self):
        selected = self.select_event()
        selected_result = self.sync_driver([selected]).json()['results'][0]
        self.assertEqual(selected_result['status'], 'accepted', selected_result)
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.REQUESTED)
        self.assertEqual(acceptance.requested_by_id, self.driver.id)
        self.assertEqual(acceptance.requesting_shift_id, self.truck_shift.id)
        self.assertIsNone(acceptance.operator_id)
        self.assertIsNone(acceptance.loading_shift_id)
        self.assertEqual(Trip.objects.count(), 0)

        other_client, identity = self.other_excavator_identity()
        accepted = self.accept_event(event_id='free-accept-after-driver', **identity)
        accepted_result = self.sync(
            [accepted], client=other_client, actor=identity['actor'], access=identity['access'],
            device_id='free-bucket-other-after-driver-001',
        ).json()['results'][0]
        self.assertEqual(accepted_result['status'], 'accepted', accepted_result)
        acceptance.refresh_from_db()
        self.assertEqual(FreeBucketAcceptance.objects.count(), 1)
        self.assertEqual(accepted_result['server_ids']['free_bucket_acceptance_id'], acceptance.id)
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.ACCEPTED)
        self.assertEqual(acceptance.operator_id, identity['actor'].id)
        self.assertEqual(acceptance.loading_shift_id, identity['shift'].id)
        self.assertIsNotNone(acceptance.accepted_at)
        self.assertEqual(Trip.objects.count(), 0)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(self.assignment.ended_at)

    def test_excavator_directory_allows_promoting_driver_request_for_same_excavator(self):
        other_client, identity = self.other_excavator_identity()
        selected = self.select_event(excavator=self.other_excavator)
        selected_result = self.sync_driver([selected]).json()['results'][0]
        self.assertEqual(selected_result['status'], 'accepted', selected_result)

        payload = other_client.get(
            reverse('excavator_work'),
            {'_operational_fragment': 'excavator', '_operational_version': 0},
        ).json()
        directory_item = next(
            item
            for item in payload['free_bucket_truck_directory']['trucks']
            if item['id'] == self.truck.id
        )

        self.assertTrue(directory_item['can_accept_free_bucket'])
        self.assertEqual(directory_item['availability_label'], 'Запрошен водителем')

    def test_excavator_then_driver_maps_same_row_without_moving_acceptance_time(self):
        other_client, identity = self.other_excavator_identity()
        accepted = self.accept_event(event_id='free-accept-before-driver', **identity)
        accepted_result = self.sync(
            [accepted], client=other_client, actor=identity['actor'], access=identity['access'],
            device_id='free-bucket-other-before-driver-001',
        ).json()['results'][0]
        self.assertEqual(accepted_result['status'], 'accepted', accepted_result)
        acceptance = FreeBucketAcceptance.objects.get()
        accepted_at = acceptance.accepted_at

        selected = self.select_event(
            event_id='driver-free-select-after-accept',
            occurred_at=accepted_at + timedelta(seconds=30),
        )
        selected_result = self.sync_driver([selected]).json()['results'][0]
        self.assertEqual(selected_result['status'], 'accepted', selected_result)
        acceptance.refresh_from_db()
        self.assertEqual(FreeBucketAcceptance.objects.count(), 1)
        self.assertEqual(selected_result['server_ids']['free_bucket_acceptance_id'], acceptance.id)
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.ACCEPTED)
        self.assertEqual(acceptance.requested_by_id, self.driver.id)
        self.assertEqual(acceptance.accepted_at, accepted_at)

    def test_different_targets_conflict_without_mutating_driver_request(self):
        selected = self.select_event(excavator=self.other_excavator)
        self.assertEqual(self.sync_driver([selected]).json()['results'][0]['status'], 'accepted')
        conflicting = self.accept_event(event_id='free-accept-different-target')
        result = self.sync([conflicting], device_id='free-bucket-different-target-001').json()['results'][0]
        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'free_bucket_target_changed')
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.excavator_id, self.other_excavator.id)
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.REQUESTED)
        self.assertEqual(Trip.objects.count(), 0)

    def stale_driver_request(self, *, age):
        """A REQUESTED right created directly, ``age`` ago, for the primary excavator."""
        from trips.free_bucket import FREE_BUCKET_REQUEST_TTL

        driver_shift = EmployeeShift.objects.get(employee=self.driver, closed_at__isnull=True)
        occurred_at = timezone.now() - age
        return FreeBucketAcceptance.objects.create(
            client_acceptance_id='driver-free-select-stale',
            truck=self.truck,
            excavator=self.excavator,
            requested_by=self.driver,
            requesting_shift=driver_shift,
            primary_assignment=self.assignment,
            status=FreeBucketAcceptanceStatus.REQUESTED,
            occurred_at=occurred_at,
            received_at=occurred_at,
            work_context_snapshot={},
        ), FREE_BUCKET_REQUEST_TTL

    def test_expired_driver_request_is_closed_and_a_new_pick_is_accepted(self):
        # Стенд 28.09.2026: просроченный запрос (#4) уже невидим водителю (TTL 10 минут),
        # но приём нового выбора смотрел только на статус и отвечал «уже выбран другой
        # экскаватор» — окно пустое, выбрать нельзя. Правило: сервер не держит работника.
        from trips.free_bucket import FREE_BUCKET_REQUEST_TTL

        stale, ttl = self.stale_driver_request(age=FREE_BUCKET_REQUEST_TTL + timedelta(minutes=1))
        selected = self.select_event(event_id='driver-free-select-after-expiry', excavator=self.other_excavator)
        result = self.sync_driver([selected]).json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        stale.refresh_from_db()
        # Как при закрытии смены: CANCELLED моментом истечения (CLOSED по ограничению
        # модели принадлежит только использованному праву с рейсом).
        self.assertEqual(stale.status, FreeBucketAcceptanceStatus.CANCELLED)
        self.assertEqual(stale.cancelled_at, stale.occurred_at + ttl)
        self.assertIsNone(stale.closed_at)
        fresh = FreeBucketAcceptance.objects.get(id=result['server_ids']['free_bucket_acceptance_id'])
        self.assertNotEqual(fresh.id, stale.id)
        self.assertEqual(fresh.excavator_id, self.other_excavator.id)
        self.assertEqual(fresh.status, FreeBucketAcceptanceStatus.REQUESTED)

    def test_fresh_driver_request_still_blocks_a_different_pick(self):
        # Правило ковша не меняется: живой запрос на другой экскаватор — отказ.
        fresh, _ = self.stale_driver_request(age=timedelta(minutes=1))
        selected = self.select_event(event_id='driver-free-select-while-fresh', excavator=self.other_excavator)
        result = self.sync_driver([selected]).json()['results'][0]
        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'free_bucket_target_changed')
        fresh.refresh_from_db()
        self.assertEqual(fresh.status, FreeBucketAcceptanceStatus.REQUESTED)
        self.assertEqual(FreeBucketAcceptance.objects.count(), 1)

    def test_driver_can_cancel_own_accepted_request_before_load(self):
        selected = self.select_event()
        self.sync_driver([selected])
        other_client, identity = self.other_excavator_identity()
        accepted = self.accept_event(event_id='free-accept-before-driver-cancel', **identity)
        self.sync(
            [accepted], client=other_client, actor=identity['actor'], access=identity['access'],
            device_id='free-bucket-other-before-cancel-001',
        )
        acceptance = FreeBucketAcceptance.objects.get()
        cancelled = self.driver_event(
            'driver-free-cancel-1',
            'driver.free_bucket.cancelled',
            2,
            occurred_at=acceptance.accepted_at + timedelta(seconds=1),
            depends_on=[selected['event_id']],
            payload={'free_bucket_acceptance_local_id': selected['event_id']},
        )
        result = self.sync_driver([cancelled]).json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        acceptance.refresh_from_db()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CANCELLED)
        self.assertEqual(Trip.objects.count(), 0)

    def test_excavator_cancel_before_acceptance_time_is_terminal_conflict(self):
        accepted = self.accept_event(event_id='free-accept-before-stale-cancel')
        accepted_result = self.sync([accepted]).json()['results'][0]
        self.assertEqual(accepted_result['status'], 'accepted', accepted_result)
        acceptance = FreeBucketAcceptance.objects.get()
        cancelled = self.event(
            'free-cancel-stale',
            'excavator.free_bucket.cancelled',
            2,
            occurred_at=acceptance.accepted_at - timedelta(milliseconds=1),
            depends_on=[accepted['event_id']],
            payload={'free_bucket_acceptance_local_id': accepted['event_id']},
        )

        result = self.sync([cancelled]).json()['results'][0]

        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'free_bucket_cancel_stale')
        acceptance.refresh_from_db()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.ACCEPTED)
        self.assertIsNone(acceptance.cancelled_at)

    def test_driver_shift_close_cancels_request_without_changing_primary_assignment(self):
        selected = self.select_event(event_id='driver-free-before-shift-close')
        self.assertEqual(self.sync_driver([selected]).json()['results'][0]['status'], 'accepted')
        self.truck.model.fuel_capacity_limit_l = Decimal('1000')
        self.truck.model.save(update_fields=['fuel_capacity_limit_l'])
        self.truck_shift.start_fuel = Decimal('500')
        self.truck_shift.start_mileage = Decimal('1000')
        self.truck_shift.start_engine_hours = Decimal('2000')
        self.truck_shift.save(update_fields=['start_fuel', 'start_mileage', 'start_engine_hours'])
        from shifts.services import close_driver_shift

        closed, created = close_driver_shift(
            shift=self.truck_shift,
            employee=self.driver,
            readings={
                'end_fuel': Decimal('500'),
                'end_mileage': Decimal('1000'),
                'end_engine_hours': Decimal('2000'),
            },
            client_action_id='driver-close-with-free-bucket-request',
        )

        self.assertTrue(created)
        self.assertIsNotNone(closed.closed_at)
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CANCELLED)
        self.assertEqual(acceptance.cancelled_at, closed.closed_at)
        self.assertEqual(Trip.objects.count(), 0)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(self.assignment.ended_at)

    def test_acceptance_of_another_shift_never_leaks_into_the_open_shift_screen(self):
        """Право на свободный ковш живёт внутри своей смены и не течёт в чужую.

        Найдено на боевом 20.09.2026: экран водителя несколько дней показывал
        заявку на чужой экскаватор вместо настоящего назначения, выбрать другой
        было нельзя (мешала «активная» заявка), отменить — тоже. Причина: экран
        искал заявку по самосвалу без привязки к смене, поэтому подхватывал
        заявку из другой смены, которую штатная отмена при закрытии её смены
        уже не касалась.
        """
        from users.views import driver_free_bucket_payload

        selected = self.select_event(event_id='driver-free-bucket-shift-scope')
        self.assertEqual(self.sync_driver([selected]).json()['results'][0]['status'], 'accepted')
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.requesting_shift_id, self.truck_shift.id)

        _, own_shift_state, own_shift_acceptance = driver_free_bucket_payload(
            current_truck=self.truck,
            current_assignment=self.assignment,
            version=1,
            open_shift=self.truck_shift,
        )
        self.assertTrue(own_shift_state['active'])
        self.assertEqual(own_shift_acceptance, acceptance)

        # Воспроизводим боевую ситуацию: прошлая смена закрылась, НЕ погасив
        # заявку (именно такая осиротевшая запись и висела несколько дней),
        # после чего водитель открыл новую смену на том же самосвале.
        EmployeeShift.objects.filter(pk=self.truck_shift.pk).update(closed_at=timezone.now())
        other_shift = EmployeeShift.objects.create(
            employee=self.driver,
            equipment=self.truck,
            opened_at=timezone.now(),
            workplace_code='driver',
        )
        acceptance.refresh_from_db()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.REQUESTED)
        _, other_shift_state, other_shift_acceptance = driver_free_bucket_payload(
            current_truck=self.truck,
            current_assignment=self.assignment,
            version=1,
            open_shift=other_shift,
        )
        self.assertFalse(other_shift_state['active'])
        self.assertIsNone(other_shift_acceptance)
        self.assertIsNone(other_shift_state['selection'])
        self.assertFalse(other_shift_state['can_cancel'])

    def test_used_right_leaves_the_driver_screen_once_its_trip_is_unloaded(self):
        """Свободный ковш — на один рейс: после разгрузки режим сам выключается.

        Владелец, 28.09.2026: водитель включил ковш → погрузился у выбранного →
        разгрузился → остаётся основное назначение. Раньше USED-принятие текущей
        смены считалось активным для экрана водителя до конца смены, и режим
        оставался включённым после завершённого рейса. Статус USED остаётся в
        истории — ничего не отменяется задним числом.
        """
        from users.views import driver_free_bucket_payload

        def driver_state():
            _, state, active = driver_free_bucket_payload(
                current_truck=self.truck,
                current_assignment=self.assignment,
                version=1,
                open_shift=self.truck_shift,
            )
            return state, active

        selected = self.select_event(event_id='driver-free-select-one-load')
        self.assertEqual(self.sync_driver([selected]).json()['results'][0]['status'], 'accepted')
        other_client, identity = self.other_excavator_identity()
        accepted = self.accept_event(event_id='free-accept-one-load', **identity)
        accepted_result = self.sync(
            [accepted], client=other_client, actor=identity['actor'], access=identity['access'],
            device_id='free-bucket-one-load-device',
        ).json()['results'][0]
        self.assertEqual(accepted_result['status'], 'accepted', accepted_result)
        loaded = self.load_event(accepted, event_id='free-load-one-load', sequence=2, **identity)
        loaded_result = self.sync(
            [loaded], client=other_client, actor=identity['actor'], access=identity['access'],
            device_id='free-bucket-one-load-device',
        ).json()['results'][0]
        self.assertEqual(loaded_result['status'], 'accepted', loaded_result)
        trip = Trip.objects.get(pk=loaded_result['server_ids']['trip_id'])
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.USED)

        # Погружен, ещё не разгружен — режим на экране водителя активен.
        loaded_state, loaded_active = driver_state()
        self.assertTrue(loaded_state['active'])
        self.assertEqual(loaded_state['status'], 'used')
        self.assertEqual(loaded_active, acceptance)

        occurred_at = trip.loaded_at + timedelta(minutes=3)
        unload = {
            'event_id': 'driver-unload-one-load',
            'event_type': 'driver.trip.unloaded',
            'format_version': 1,
            'actor_id': self.driver.id,
            'access_id': self.driver_access.id,
            'role_code': 'driver',
            'occurred_at': occurred_at.isoformat(),
            'sequence': 2,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': trip.id,
            'context_snapshot': {
                'actor_id': self.driver.id,
                'access_id': self.driver_access.id,
                'role_code': 'driver',
            },
            'payload': {'trip_id': trip.id},
        }
        unload_result = self.sync_driver([unload]).json()['results'][0]
        self.assertEqual(unload_result['status'], 'accepted', unload_result)
        trip.refresh_from_db()
        self.assertNotIn(trip.status, (TripStatus.ACTIVE, TripStatus.LOADED_WAITING_UNLOAD))
        acceptance.refresh_from_db()
        # Использованное право при разгрузке закрывается (CLOSED — «использовано и
        # завершено»), ничего не отменяется задним числом: рейс и погрузка остаются.
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)
        self.assertEqual(acceptance.used_trip_id, trip.id)
        self.assertIsNone(acceptance.cancelled_at)

        # Разгружен — режим выключен, осталось основное назначение.
        unloaded_state, unloaded_active = driver_state()
        self.assertFalse(unloaded_state['active'])
        self.assertIsNone(unloaded_active)
        self.assertIsNone(unloaded_state['selection'])
        self.assertFalse(unloaded_state['can_cancel'])

    def test_used_right_whose_trip_left_open_statuses_without_closing_is_off_for_the_driver(self):
        """Рейс ковша ушёл из открытых, а право осталось USED — экран водителя всё равно гасит режим.

        Разгрузка и отмены закрывают право (CLOSED), но не все пути: истечение
        пассивного ручного рейса (trips/manual_loading.py, UNCONTROLLED) право не
        трогает. Раньше USED считалось активным для экрана водителя безусловно —
        кнопка ковша горела до конца смены. Правило владельца: ковш на один рейс —
        право активно, только пока его рейс открыт.
        """
        from users.views import driver_free_bucket_payload

        selected = self.select_event(event_id='driver-free-select-uncontrolled')
        self.assertEqual(self.sync_driver([selected]).json()['results'][0]['status'], 'accepted')
        other_client, identity = self.other_excavator_identity()
        accepted = self.accept_event(event_id='free-accept-uncontrolled', **identity)
        loaded = self.load_event(accepted, event_id='free-load-uncontrolled', sequence=2, **identity)
        results = self.sync(
            [accepted, loaded], client=other_client, actor=identity['actor'], access=identity['access'],
            device_id='free-bucket-uncontrolled-device',
        ).json()['results']
        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'], results)
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.USED)

        Trip.objects.filter(pk=acceptance.used_trip_id).update(
            status=TripStatus.UNCONTROLLED, operationally_closed_at=timezone.now(),
        )
        acceptance.refresh_from_db()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.USED)

        _, state, active = driver_free_bucket_payload(
            current_truck=self.truck,
            current_assignment=self.assignment,
            version=1,
            open_shift=self.truck_shift,
        )
        self.assertFalse(state['active'])
        self.assertIsNone(active)
        self.assertIsNone(state['selection'])
        self.assertFalse(state['can_cancel'])

    def backdate_truck_shift(self, minutes=30):
        from shifts.models import EmployeeShift

        started_at = timezone.now() - timedelta(minutes=minutes)
        EmployeeShift.objects.filter(pk=self.truck_shift.pk).update(opened_at=started_at)
        self.truck_shift.refresh_from_db()
        # Основное назначение тоже должно действовать в прошлом — для отметок по нему.
        HaulAssignment.objects.filter(pk=self.assignment.pk).update(assigned_at=started_at)
        self.assignment.refresh_from_db()

    def driver_manual_load_under_bucket(self, select, *, event_id, sequence, occurred_at):
        event = self.driver_event(
            event_id,
            'driver.trip.loaded',
            sequence,
            occurred_at=occurred_at,
            payload={
                'manual_control': True,
                'truck_id': self.truck.id,
                'excavator_id': self.other_excavator.id,
                'dump_point_id': self.dump_point.id,
                'rock_type_id': self.rock.id,
                'loading_horizon': '125',
                'loading_block': '4',
                'assignment_id': None,
                'free_bucket_acceptance_id': None,
                'free_bucket_acceptance_local_id': select['event_id'],
            },
        )
        event['local_trip_id'] = event_id
        event['context_snapshot'].update({
            'source': 'driver_manual',
            'authority_type': 'free_bucket',
            'free_bucket_acceptance_local_id': select['event_id'],
            'selected_dump_point_id': self.dump_point.id,
        })
        return event

    def driver_manual_complete_under_bucket(self, loaded, *, event_id, sequence, occurred_at):
        event = self.driver_event(
            event_id,
            'driver.trip.manual_completed',
            sequence,
            occurred_at=occurred_at,
            depends_on=[loaded['event_id']],
            payload={
                'manual_control': True,
                'truck_id': self.truck.id,
                'excavator_id': self.other_excavator.id,
                'dump_point_id': self.dump_point.id,
            },
        )
        event['trip_id'] = None
        event['local_trip_id'] = loaded['event_id']
        event['context_snapshot'].update({
            'source': 'driver_manual',
            'action': 'manual_completed',
            'selected_dump_point_id': self.dump_point.id,
        })
        return event

    def test_manual_load_under_requested_bucket_is_accepted_by_the_driver_action(self):
        """Ручная погрузка под ещё не принятым ковшом — принята по факту действия водителя.

        Бой 28.09.2026 (телефон владельца, v365): в ручном режиме машинист без
        приложения, подтверждать запрос некому, а сервер отвечал retry
        «free_bucket_acceptance_pending» 260 раз подряд ~38 минут — три рейса не
        записаны, за первым повисла вся очередь. По правилу владельца в ручном
        режиме погрузку определяет водитель: запрос принимается временем погрузки
        от лица машиниста, который на смене у этого экскаватора, и рейс создаётся
        обычным путём до USED. Следом разгрузка по этому рейсу — цепочка не висит.
        """
        # Времена в прошлом (в пределах TTL запроса): будущие отметки сервер подрезает,
        # как и отметки раньше открытия смены — смену водителя открываем раньше.
        self.backdate_truck_shift()
        select = self.select_event(
            event_id='driver-free-select-manual-accept',
            occurred_at=timezone.now() - timedelta(minutes=9),
        )
        self.assertEqual(self.sync_driver([select]).json()['results'][0]['status'], 'accepted')
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.REQUESTED)
        # Машинист на смене у экскаватора, но кнопку «принять» не нажимал.
        _, identity = self.other_excavator_identity()
        operator_shift = identity['shift']

        loaded_at = timezone.datetime.fromisoformat(select['occurred_at']) + timedelta(minutes=1)
        loaded = self.driver_manual_load_under_bucket(
            select, event_id='driver-manual-load-under-requested', sequence=2, occurred_at=loaded_at,
        )
        loaded_result = self.sync_driver([loaded]).json()['results'][0]
        self.assertEqual(loaded_result['status'], 'accepted', loaded_result)
        self.assertEqual(loaded_result['trip_origin'], 'driver_manual')

        acceptance.refresh_from_db()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.USED)
        self.assertEqual(acceptance.accepted_at, loaded_at)
        self.assertEqual(acceptance.used_at, loaded_at)
        self.assertEqual(acceptance.operator_id, identity['actor'].id)
        self.assertEqual(acceptance.loading_shift_id, operator_shift.id)
        self.assertEqual(acceptance.requested_by_id, self.driver.id)
        trip = Trip.objects.get(pk=loaded_result['server_ids']['trip_id'])
        self.assertEqual(acceptance.used_trip_id, trip.id)
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertEqual(trip.excavator_id, self.other_excavator.id)
        self.assertEqual(trip.excavator_operator_id, identity['actor'].id)
        self.assertEqual(trip.loading_shift_id, operator_shift.id)
        self.assertEqual(trip.loaded_at, loaded_at)
        self.assertEqual(trip.load_time_source, 'driver_device')

        completed = self.driver_manual_complete_under_bucket(
            loaded, event_id='driver-manual-complete-under-requested', sequence=3,
            occurred_at=loaded_at + timedelta(minutes=2),
        )
        completed_result = self.sync_driver([completed]).json()['results'][0]
        self.assertEqual(completed_result['status'], 'accepted', completed_result)
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        acceptance.refresh_from_db()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)
        # Кнопку «принять» нажимал не машинист — признак стоит и при записанном машинисте.
        self.assertTrue(acceptance.accepted_by_driver_action)

    def test_free_bucket_constraints_admit_operatorless_right_only_when_accepted_by_driver_action(self):
        """Ограничения БД (и PostgreSQL в CI): без машиниста живёт только принятое отметкой водителя."""
        now = timezone.now()

        def build(**overrides):
            fields = {
                'client_acceptance_id': overrides.pop('client_acceptance_id', 'constraint-probe'),
                'truck': self.truck,
                'excavator': self.other_excavator,
                'requested_by': self.driver,
                'requesting_shift': self.truck_shift,
                'status': FreeBucketAcceptanceStatus.ACCEPTED,
                'occurred_at': now,
                'accepted_at': now,
                'accepted_by_driver_action': True,
            }
            fields.update(overrides)
            return FreeBucketAcceptance(**fields)

        with transaction.atomic():
            build(client_acceptance_id='probe-accepted').save()
        FreeBucketAcceptance.objects.all().delete()

        trip = Trip.objects.create(
            excavator=self.other_excavator, truck=self.truck, driver=self.driver,
            rock_type=self.rock, dump_point=self.dump_point,
            status=TripStatus.LOADED_WAITING_UNLOAD, loaded_at=now,
        )
        with transaction.atomic():
            build(
                client_acceptance_id='probe-used', status=FreeBucketAcceptanceStatus.USED,
                used_at=now, used_trip=trip,
            ).save()
        FreeBucketAcceptance.objects.all().delete()

        # Без признака — по-прежнему нельзя: принятое право обязано знать, кто принял.
        with self.assertRaises(IntegrityError), transaction.atomic():
            build(client_acceptance_id='probe-no-flag', accepted_by_driver_action=False).save()
        # Признак без водителя-заказчика тоже не спасает.
        with self.assertRaises(IntegrityError), transaction.atomic():
            build(client_acceptance_id='probe-no-driver', requested_by=None, requesting_shift=None).save()
        # Признак без времени принятия — нет.
        with self.assertRaises(IntegrityError), transaction.atomic():
            build(client_acceptance_id='probe-no-time', accepted_at=None).save()
        # Ещё не принятый запрос с признаком — противоречие.
        with self.assertRaises(IntegrityError), transaction.atomic():
            build(
                client_acceptance_id='probe-requested-flag',
                status=FreeBucketAcceptanceStatus.REQUESTED, accepted_at=None,
            ).save()

    def test_driver_screen_keeps_the_bucket_module_on_while_the_bucket_trip_is_open(self):
        """Пока рейс под ковшом не разгружен, модуль ковша на экране водителя включён.

        Боевой Infinix 29.09.2026 (v367/v368): после ручной погрузки под ковшом
        сервер рендерил data-driver-free-bucket-enabled="false" и не отдавал окно
        ковша (любой открытый рейс выключал модуль) — телефон терял состояние
        ковша через полсекунды, круг становился синим. После разгрузки модуль
        снова обычный: рейса нет, выбрать можно.
        """
        self.backdate_truck_shift()
        select = self.select_event(
            event_id='driver-free-select-screen',
            occurred_at=timezone.now() - timedelta(minutes=2),
        )
        self.assertEqual(self.sync_driver([select]).json()['results'][0]['status'], 'accepted')
        loaded = self.driver_manual_load_under_bucket(
            select, event_id='driver-manual-load-screen', sequence=2,
            occurred_at=timezone.now() - timedelta(minutes=1),
        )
        self.assertEqual(self.sync_driver([loaded]).json()['results'][0]['status'], 'accepted')

        page = self.driver_client.get('/driver/').content.decode()
        self.assertIn('data-driver-free-bucket-enabled="true"', page)
        self.assertIn('id="driver-free-bucket-dialog"', page)
        state = json.loads(
            page.split('id="driver-free-bucket-state-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        )
        self.assertTrue(state['active'])
        self.assertEqual(state['status'], 'used')
        self.assertEqual(state['selection']['id'], self.other_excavator.id)

        completed = self.driver_manual_complete_under_bucket(
            loaded, event_id='driver-manual-complete-screen', sequence=3, occurred_at=timezone.now(),
        )
        self.assertEqual(self.sync_driver([completed]).json()['results'][0]['status'], 'accepted')
        page = self.driver_client.get('/driver/').content.decode()
        self.assertIn('data-driver-free-bucket-enabled="true"', page)
        state = json.loads(
            page.split('id="driver-free-bucket-state-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        )
        self.assertFalse(state['active'])

    def test_manual_load_under_requested_bucket_without_operator_shift_is_accepted_by_driver_action(self):
        """Типичный ручной режим: у экскаватора никого на смене — принято действием водителя.

        Подтверждать некому по построению; retry держал бы всю очередь телефона.
        Право принимается без машиниста с признаком accepted_by_driver_action и
        временем погрузки, рейс создаётся, следом разгрузка и следующая пара
        погрузка/разгрузка проходят — цепочка не висит.
        """
        from shifts.models import EmployeeShift

        self.assertFalse(
            EmployeeShift.objects.filter(equipment=self.other_excavator, closed_at__isnull=True).exists()
        )
        self.backdate_truck_shift()
        select = self.select_event(
            event_id='driver-free-select-no-operator',
            occurred_at=timezone.now() - timedelta(minutes=9),
        )
        self.assertEqual(self.sync_driver([select]).json()['results'][0]['status'], 'accepted')
        loaded_at = timezone.datetime.fromisoformat(select['occurred_at']) + timedelta(minutes=1)
        loaded = self.driver_manual_load_under_bucket(
            select, event_id='driver-manual-load-no-operator', sequence=2, occurred_at=loaded_at,
        )
        completed = self.driver_manual_complete_under_bucket(
            loaded, event_id='driver-manual-complete-no-operator', sequence=3,
            occurred_at=loaded_at + timedelta(minutes=2),
        )
        results = self.sync_driver([loaded, completed]).json()['results']
        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'], results)

        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)
        self.assertTrue(acceptance.accepted_by_driver_action)
        self.assertEqual(acceptance.accepted_at, loaded_at)
        self.assertEqual(acceptance.used_at, loaded_at)
        self.assertIsNone(acceptance.operator_id)
        self.assertIsNone(acceptance.loading_shift_id)
        self.assertEqual(acceptance.requested_by_id, self.driver.id)
        trip = Trip.objects.get(pk=results[0]['server_ids']['trip_id'])
        self.assertEqual(acceptance.used_trip_id, trip.id)
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertIsNone(trip.excavator_operator_id)
        self.assertIsNone(trip.loading_shift_id)
        self.assertEqual(trip.driver_id, self.driver.id)
        self.assertEqual(trip.loaded_at, loaded_at)

        # Следующая пара по основному назначению — очередь идёт дальше.
        from trips.free_bucket import canonical_free_bucket_work_context_snapshot

        primary_context = canonical_free_bucket_work_context_snapshot(self.excavator)
        next_loaded_at = loaded_at + timedelta(minutes=4)
        next_loaded = self.driver_event(
            'driver-manual-load-after-bucket', 'driver.trip.loaded', 4,
            occurred_at=next_loaded_at,
            payload={
                'manual_control': True,
                'truck_id': self.truck.id,
                'excavator_id': self.excavator.id,
                'dump_point_id': self.dump_point.id,
                'rock_type_id': self.rock.id,
                'placement_id': primary_context['placement_id'],
                'loading_horizon': primary_context['loading_horizon'],
                'loading_block': primary_context['loading_block'],
                'assignment_id': self.assignment.id,
                'free_bucket_acceptance_id': None,
                'free_bucket_acceptance_local_id': None,
            },
        )
        next_loaded['local_trip_id'] = next_loaded['event_id']
        next_loaded['context_snapshot'].update({
            'source': 'driver_manual',
            'dump_points': primary_context['dump_points'],
            'selected_dump_point_id': self.dump_point.id,
            'selected_one_off': False,
        })
        next_completed = self.driver_manual_complete_under_bucket(
            next_loaded, event_id='driver-manual-complete-after-bucket', sequence=5,
            occurred_at=next_loaded_at + timedelta(minutes=2),
        )
        next_completed['payload']['excavator_id'] = self.excavator.id
        next_results = self.sync_driver([next_loaded, next_completed]).json()['results']
        self.assertEqual([item['status'] for item in next_results], ['accepted', 'accepted'], next_results)
        self.assertEqual(Trip.objects.filter(status=TripStatus.COMPLETED).count(), 2)

    def _expired_bucket_load_pair(self, suffix):
        """Ковш включён 40 мин назад, погрузка через минуту, сервер погасил запрос по TTL."""
        from trips.free_bucket import expire_stale_free_bucket_requests

        self.backdate_truck_shift(minutes=60)
        select = self.select_event(
            event_id=f'driver-free-select-expired-{suffix}',
            occurred_at=timezone.now() - timedelta(minutes=40),
        )
        self.assertEqual(self.sync_driver([select]).json()['results'][0]['status'], 'accepted')
        expire_stale_free_bucket_requests(self.truck)
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CANCELLED)
        loaded_at = timezone.datetime.fromisoformat(select['occurred_at']) + timedelta(minutes=1)
        loaded = self.driver_manual_load_under_bucket(
            select, event_id=f'driver-manual-load-expired-{suffix}', sequence=2, occurred_at=loaded_at,
        )
        completed = self.driver_manual_complete_under_bucket(
            loaded, event_id=f'driver-manual-complete-expired-{suffix}', sequence=3,
            occurred_at=loaded_at + timedelta(minutes=2),
        )
        return acceptance, loaded, completed, loaded_at

    def test_manual_load_under_bucket_cancelled_by_server_ttl_is_recorded_by_fact(self):
        """Погрузка под ковшом, который сервер погасил до прихода отметки, — записана.

        Бой 28.09.2026 (телефон владельца, после выкладки 0017): отметка висела в
        очереди, запрос истёк через 10 минут, и сервер ответил
        «free_bucket_not_available» — погрузку отклонил, следом каскадом
        dependency_rejected ещё пять отметок. Нажатие водителя — истина: отмена
        снимается, право принимается отметкой водителя, рейс создаётся и завершается.
        """
        acceptance, loaded, completed, loaded_at = self._expired_bucket_load_pair('fact')
        results = self.sync_driver([loaded, completed]).json()['results']
        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'], results)

        acceptance.refresh_from_db()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)
        self.assertIsNone(acceptance.cancelled_at)
        self.assertTrue(acceptance.accepted_by_driver_action)
        self.assertEqual(acceptance.accepted_at, loaded_at)
        trip = Trip.objects.get(pk=results[0]['server_ids']['trip_id'])
        self.assertEqual(acceptance.used_trip_id, trip.id)
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.excavator_id, self.other_excavator.id)
        self.assertEqual(trip.loaded_at, loaded_at)

    def test_already_rejected_cancelled_bucket_load_is_accepted_on_resend(self):
        """Отметка, отклонённая старым сервером, и её каскад проходят при повторной отправке.

        Телефон владельца продолжает слать отклонённые записи (try914); после
        выкладки правки корневая погрузка обрабатывается заново, а зависимые
        dependency_rejected восстанавливаются, как только корень принят.
        """
        from unittest import mock

        acceptance, loaded, completed, loaded_at = self._expired_bucket_load_pair('resend')
        # Старое поведение сервера: отмену не снимали.
        with mock.patch(
            'core.offline_sync._revive_cancelled_free_bucket_by_driver_load',
            side_effect=lambda acceptance, **kwargs: acceptance,
        ):
            rejected = self.sync_driver([loaded, completed]).json()['results']
        self.assertEqual(
            [(item['status'], item.get('code')) for item in rejected],
            [('conflict', 'free_bucket_not_available'), ('conflict', 'dependency_rejected')],
            rejected,
        )
        self.assertFalse(Trip.objects.exists())

        results = self.sync_driver([loaded, completed]).json()['results']
        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'], results)
        acceptance.refresh_from_db()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)
        trip = Trip.objects.get()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.loaded_at, loaded_at)
        receipts = OfflineFieldEvent.objects.filter(event_id__in=[loaded['event_id'], completed['event_id']])
        self.assertEqual({item.status for item in receipts}, {'accepted'})

    def test_bucket_trip_completion_signed_with_the_primary_excavator_is_accepted(self):
        """Завершение рейса под ковшом с подписью основного экскаватора — принято.

        Телефон подписывает завершение ручного рейса базовым контекстом рабочего
        места — при живом основном назначении это основной экскаватор, а не тот,
        у которого рейс погружен (driver-manual-excavator-workspace-v1.js). Рейс
        назван однозначно (trip_id / local_trip_id) и принадлежит самосвалу смены;
        раньше сервер отвечал conflict driver_manual_trip_changed — рейс оставался
        открытым, очередь телефона вставала (стенд 28.09.2026).
        """
        self.backdate_truck_shift()
        select = self.select_event(
            event_id='driver-free-select-primary-signed',
            occurred_at=timezone.now() - timedelta(minutes=9),
        )
        self.assertEqual(self.sync_driver([select]).json()['results'][0]['status'], 'accepted')
        loaded_at = timezone.datetime.fromisoformat(select['occurred_at']) + timedelta(minutes=1)
        loaded = self.driver_manual_load_under_bucket(
            select, event_id='driver-manual-load-primary-signed', sequence=2, occurred_at=loaded_at,
        )
        completed = self.driver_manual_complete_under_bucket(
            loaded, event_id='driver-manual-complete-primary-signed', sequence=3,
            occurred_at=loaded_at + timedelta(minutes=2),
        )
        completed['payload']['excavator_id'] = self.excavator.id
        results = self.sync_driver([loaded, completed]).json()['results']
        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'], results)
        trip = Trip.objects.get(pk=results[0]['server_ids']['trip_id'])
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.excavator_id, self.other_excavator.id)
        self.assertEqual(FreeBucketAcceptance.objects.get().status, FreeBucketAcceptanceStatus.CLOSED)

    def test_orphaned_request_stops_holding_the_truck_for_dispatcher_and_excavator(self):
        """Осиротевшая заявка не держит самосвал «под свободным ковшом» нигде.

        Продолжение боевого случая 20.09.2026: после починки экрана водителя
        пульт диспетчера и приложение экскаваторщика всё ещё показывали самосвал
        под свободным ковшом — они читают ту же запись своим общим фильтром
        active_free_bucket_acceptance_filter(). Право живёт внутри своей смены,
        поэтому запись от закрытой смены не должна считаться активной ни для
        одного экрана.
        """
        from trips.free_bucket import (
            active_free_bucket_acceptance_filter,
            active_free_bucket_acceptance_for_truck,
        )

        selected = self.select_event(event_id='driver-free-bucket-orphan-scope')
        self.assertEqual(self.sync_driver([selected]).json()['results'][0]['status'], 'accepted')
        acceptance = FreeBucketAcceptance.objects.get()

        active_ids = list(
            FreeBucketAcceptance.objects
            .filter(active_free_bucket_acceptance_filter(now=timezone.now()))
            .values_list('id', flat=True)
        )
        self.assertIn(acceptance.id, active_ids)
        self.assertIsNotNone(active_free_bucket_acceptance_for_truck(self.truck))

        # Смена заявки закончилась, а саму заявку погасить не успели.
        EmployeeShift.objects.filter(pk=self.truck_shift.pk).update(closed_at=timezone.now())

        active_ids_after = list(
            FreeBucketAcceptance.objects
            .filter(active_free_bucket_acceptance_filter(now=timezone.now()))
            .values_list('id', flat=True)
        )
        self.assertNotIn(acceptance.id, active_ids_after)
        self.assertIsNone(active_free_bucket_acceptance_for_truck(self.truck))
        acceptance.refresh_from_db()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.REQUESTED)

    def test_request_expires_ten_minutes_after_switching_on(self):
        """Включённый свободный ковш живёт 10 минут с момента включения.

        Решение пользователя 20.09.2026: режим действует с момента включения и до
        первой погрузки или отмены, но не дольше десяти минут. Приём машинистом
        окно не продлевает. Правило живёт в общем фильтре, поэтому по истечении
        срока самосвал одновременно уходит из-под ковша у водителя, на пульте и
        у экскаваторщика, а сама запись остаётся как есть.
        """
        from datetime import timedelta
        from trips.free_bucket import (
            FREE_BUCKET_REQUEST_TTL,
            active_free_bucket_acceptance_filter,
            active_free_bucket_acceptance_for_truck,
        )

        selected = self.select_event(event_id='driver-free-bucket-ttl')
        self.assertEqual(self.sync_driver([selected]).json()['results'][0]['status'], 'accepted')
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(FREE_BUCKET_REQUEST_TTL, timedelta(minutes=10))

        switched_on = acceptance.occurred_at

        def active_at(moment):
            return FreeBucketAcceptance.objects.filter(
                active_free_bucket_acceptance_filter(now=moment)
            ).filter(pk=acceptance.pk).exists()

        self.assertTrue(active_at(switched_on + timedelta(minutes=9, seconds=59)))
        self.assertFalse(active_at(switched_on + timedelta(minutes=10, seconds=1)))

        # Приём машинистом на девятой минуте срок не продлевает.
        FreeBucketAcceptance.objects.filter(pk=acceptance.pk).update(
            status=FreeBucketAcceptanceStatus.ACCEPTED,
            operator=self.operator,
            loading_shift=self.shift,
            accepted_at=switched_on + timedelta(minutes=9),
        )
        self.assertTrue(active_at(switched_on + timedelta(minutes=9, seconds=30)))
        self.assertFalse(active_at(switched_on + timedelta(minutes=10, seconds=1)))

        # Истёкшее право не держит самосвал и для погрузки.
        FreeBucketAcceptance.objects.filter(pk=acceptance.pk).update(
            occurred_at=timezone.now() - timedelta(minutes=11),
        )
        self.assertIsNone(active_free_bucket_acceptance_for_truck(self.truck))
        acceptance.refresh_from_db()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.ACCEPTED)

    def test_driver_shift_close_keeps_used_acceptance_and_loaded_trip(self):
        selected = self.select_event(event_id='driver-free-before-used-shift-close')
        self.assertEqual(self.sync_driver([selected]).json()['results'][0]['status'], 'accepted')
        other_client, identity = self.other_excavator_identity()
        accepted = self.accept_event(event_id='free-accept-before-used-shift-close', **identity)
        loaded = self.load_event(
            accepted,
            event_id='free-load-before-used-shift-close',
            **identity,
        )
        load_results = self.sync(
            [accepted, loaded],
            client=other_client,
            actor=identity['actor'],
            access=identity['access'],
            device_id='free-bucket-before-used-shift-close-001',
        ).json()['results']
        self.assertEqual({item['status'] for item in load_results}, {'accepted'})
        trip = Trip.objects.get()

        self.truck.model.fuel_capacity_limit_l = Decimal('1000')
        self.truck.model.save(update_fields=['fuel_capacity_limit_l'])
        self.truck_shift.start_fuel = Decimal('500')
        self.truck_shift.start_mileage = Decimal('1000')
        self.truck_shift.start_engine_hours = Decimal('2000')
        self.truck_shift.save(update_fields=['start_fuel', 'start_mileage', 'start_engine_hours'])
        from shifts.services import close_driver_shift

        closed, created = close_driver_shift(
            shift=self.truck_shift,
            employee=self.driver,
            readings={
                'end_fuel': Decimal('500'),
                'end_mileage': Decimal('1000'),
                'end_engine_hours': Decimal('2000'),
            },
            client_action_id='driver-close-with-used-free-bucket',
        )

        self.assertTrue(created)
        self.assertIsNotNone(closed.closed_at)
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.USED)
        self.assertEqual(acceptance.used_trip_id, trip.id)
        self.assertIsNone(acceptance.cancelled_at)
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertTrue(trip.is_carryover)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(self.assignment.ended_at)

    def test_excavator_shift_close_cancels_acceptance_without_changing_primary_assignment(self):
        accepted = self.accept_event(event_id='free-accept-before-shift-close')
        self.assertEqual(self.sync([accepted]).json()['results'][0]['status'], 'accepted')
        from shifts.services import close_excavator_shift

        response = close_excavator_shift(
            employee=self.operator,
            fuel_value=self.shift.start_fuel,
            engine_hours_value=self.shift.start_engine_hours,
            client_action_id='excavator-close-with-free-bucket-acceptance',
            expected_shift_id=self.shift.id,
        )

        self.assertFalse(response['shift_open'])
        acceptance = FreeBucketAcceptance.objects.get()
        self.shift.refresh_from_db()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CANCELLED)
        self.assertEqual(acceptance.cancelled_at, self.shift.closed_at)
        self.assertEqual(Trip.objects.count(), 0)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(self.assignment.ended_at)

    def test_driver_request_is_rejected_while_truck_has_open_trip(self):
        Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            rock_type=self.rock,
            dump_point=self.dump_point,
            assigned_dump_point=self.dump_point,
            actual_dump_point=self.dump_point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            loaded_at=timezone.now(),
        )
        result = self.sync_driver([self.select_event()]).json()['results'][0]
        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'open_trip_exists')
        self.assertEqual(FreeBucketAcceptance.objects.count(), 0)

    def test_driver_cannot_request_the_current_primary_excavator(self):
        result = self.sync_driver([
            self.select_event(excavator=self.excavator),
        ]).json()['results'][0]
        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'free_bucket_primary_target')
        self.assertEqual(FreeBucketAcceptance.objects.count(), 0)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, AssignmentStatus.ACCEPTED)

    def test_late_driver_request_does_not_resurrect_after_completed_trip(self):
        occurred_at = self.truck_shift.opened_at + timedelta(milliseconds=1)
        completed_at = timezone.now()
        Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            rock_type=self.rock,
            dump_point=self.dump_point,
            assigned_dump_point=self.dump_point,
            actual_dump_point=self.dump_point,
            status=TripStatus.COMPLETED,
            loaded_at=occurred_at + timedelta(milliseconds=1),
            completed_at=completed_at,
        )
        late = self.select_event(
            event_id='driver-free-select-after-finished-trip',
            occurred_at=occurred_at,
        )
        result = self.sync_driver([late]).json()['results'][0]
        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'free_bucket_request_stale')
        self.assertEqual(FreeBucketAcceptance.objects.count(), 0)

    def test_selection_after_offline_trip_is_not_stale_because_the_trip_reached_the_server_later(self):
        # Матрица без сети, стенд 30.09.2026: без связи водитель сделал ручной
        # рейс (погрузка и завершение по часам телефона), потом выбрал свободный
        # ковш. Очередь дошла пачками: рейс записан раньше выбора, и его
        # created_at — время сервера, позже нажатия «выбрать». Сервер считал это
        # «состояние рейса изменилось после выбора» и отклонял выбор.
        from shifts.models import EmployeeShift

        now = timezone.now()
        EmployeeShift.objects.filter(pk=self.truck_shift.pk).update(opened_at=now - timedelta(hours=1))
        self.truck_shift.refresh_from_db()
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            rock_type=self.rock,
            dump_point=self.dump_point,
            assigned_dump_point=self.dump_point,
            actual_dump_point=self.dump_point,
            status=TripStatus.COMPLETED,
            loaded_at=now - timedelta(seconds=20),
            completed_at=now - timedelta(seconds=10),
        )
        Trip.objects.filter(pk=trip.pk).update(created_at=now)
        selected = self.select_event(
            event_id='driver-free-select-after-offline-trip',
            occurred_at=now - timedelta(seconds=5),
        )
        result = self.sync_driver([selected]).json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        self.assertEqual(FreeBucketAcceptance.objects.count(), 1)

    def test_duplicate_and_late_driver_selection_do_not_revive_cancelled_request(self):
        selected_at = timezone.now()
        selected = self.select_event(occurred_at=selected_at)
        first = self.sync_driver([selected]).json()['results'][0]
        repeated = self.sync_driver([selected]).json()['results'][0]
        self.assertEqual(first['status'], 'accepted', first)
        self.assertEqual(repeated['status'], 'deduplicated', repeated)
        cancelled = self.driver_event(
            'driver-free-cancel-before-late',
            'driver.free_bucket.cancelled',
            2,
            occurred_at=selected_at + timedelta(seconds=2),
            depends_on=[selected['event_id']],
            payload={'free_bucket_acceptance_local_id': selected['event_id']},
        )
        self.assertEqual(self.sync_driver([cancelled]).json()['results'][0]['status'], 'accepted')
        late = self.select_event(
            event_id='driver-free-select-late',
            sequence=3,
            occurred_at=selected_at + timedelta(seconds=1),
        )
        late_result = self.sync_driver([late]).json()['results'][0]
        self.assertEqual(late_result['status'], 'conflict', late_result)
        self.assertEqual(late_result['code'], 'free_bucket_request_stale')
        self.assertEqual(FreeBucketAcceptance.objects.count(), 1)
        self.assertEqual(FreeBucketAcceptance.objects.get().status, FreeBucketAcceptanceStatus.CANCELLED)

    def test_driver_snapshot_survives_context_and_dispatcher_changes_and_drives_trip(self):
        selected = self.select_event()
        self.assertEqual(self.sync_driver([selected]).json()['results'][0]['status'], 'accepted')
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.primary_assignment_id, self.assignment.id)
        self.assertEqual(acceptance.work_context_snapshot['rock_type_id'], self.rock.id)
        self.assertEqual(acceptance.work_context_snapshot['dump_points'][0]['id'], self.dump_point.id)

        changed_at = timezone.now()
        self.assignment.status = AssignmentStatus.CANCELLED
        self.assignment.ended_at = changed_at
        self.assignment.save(update_fields=['status', 'ended_at'])
        new_primary = HaulAssignment.objects.create(
            truck=self.truck,
            excavator=self.other_excavator,
            status=AssignmentStatus.ACCEPTED,
            assigned_at=changed_at,
            accepted_at=changed_at,
        )
        placement = ExcavatorPlacement.objects.get(excavator=self.other_excavator)
        placement.loading_horizon = '999'
        placement.loading_block = '99'
        placement.save(update_fields=['loading_horizon', 'loading_block'])
        self.dump_point.is_active = False
        self.dump_point.save(update_fields=['is_active'])

        other_client, identity = self.other_excavator_identity()
        accepted = self.accept_event(event_id='free-accept-snapshot', **identity)
        accepted_result = self.sync(
            [accepted], client=other_client, actor=identity['actor'], access=identity['access'],
            device_id='free-bucket-snapshot-001',
        ).json()['results'][0]
        self.assertEqual(accepted_result['status'], 'accepted', accepted_result)
        loaded = self.load_event(accepted, event_id='free-load-snapshot', **identity)
        loaded_result = self.sync(
            [loaded], client=other_client, actor=identity['actor'], access=identity['access'],
            device_id='free-bucket-snapshot-001',
        ).json()['results'][0]
        self.assertEqual(loaded_result['status'], 'accepted', loaded_result)
        trip = Trip.objects.get()
        self.assertEqual(trip.excavator_id, self.other_excavator.id)
        self.assertEqual(trip.rock_type_id, self.rock.id)
        self.assertEqual(trip.dump_point_id, self.dump_point.id)
        self.assertEqual(trip.loading_horizon, '125')
        self.assertEqual(trip.loading_block, '4')
        acceptance.refresh_from_db()
        self.assertEqual(acceptance.primary_assignment_id, self.assignment.id)
        new_primary.refresh_from_db()
        self.assertEqual(new_primary.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(new_primary.ended_at)

    def test_accept_then_load_creates_one_trip_without_changing_primary_assignment(self):
        other_client, other_operator, other_shift = (
            trip_fixtures.ExcavatorWorkServerIntegrationTests.create_other_excavator_client(self)
        )
        other_access = EmployeeAccess.objects.get(employee=other_operator, role=self.role)
        identity = {
            'actor': other_operator,
            'access': other_access,
            'shift': other_shift,
            'excavator': self.other_excavator,
        }
        accepted = self.accept_event(**identity)
        loaded = self.load_event(accepted, **identity)

        response = self.sync(
            [loaded, accepted],
            client=other_client,
            actor=other_operator,
            access=other_access,
            device_id='free-bucket-other-001',
        )

        self.assertEqual(response.status_code, 200, response.content)
        results = {item['event_id']: item for item in response.json()['results']}
        self.assertEqual(results[accepted['event_id']]['status'], 'accepted', results)
        self.assertEqual(results[loaded['event_id']]['status'], 'accepted', results)
        trip = Trip.objects.get()
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertEqual(trip.excavator_id, self.other_excavator.id)
        self.assertEqual(trip.truck_id, self.truck.id)
        self.assertEqual(trip.assigned_dump_point_id, self.dump_point.id)
        self.assertEqual(trip.driver_control_shift_id, self.truck_shift.id)
        self.assertTrue(trip.driver_participation_recorded)
        self.assertEqual(trip.volume_m3, Decimal('49.40'))
        self.assertEqual(trip.tonnage, Decimal('127.45'))
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.USED)
        self.assertEqual(acceptance.used_trip_id, trip.id)
        self.assertEqual(acceptance.primary_assignment_id, self.assignment.id)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(self.assignment.ended_at)
        self.assertEqual(
            results[loaded['event_id']]['server_ids']['free_bucket_acceptance_id'],
            acceptance.id,
        )
        from trips.excavator_hourly_report import build_excavator_hourly_report

        report_at = trip.loaded_at + timedelta(minutes=1)
        temporary_report = build_excavator_hourly_report(
            self.other_excavator,
            captured_at=report_at,
        )
        primary_report = build_excavator_hourly_report(
            self.excavator,
            captured_at=report_at,
        )
        self.assertEqual(sum(hour['source_trip_count'] for hour in temporary_report['hours']), 1)
        self.assertEqual(sum(hour['source_trip_count'] for hour in primary_report['hours']), 0)
        self.assertEqual(
            Trip.objects.filter(excavator=self.other_excavator).aggregate(total=Sum('volume_m3'))['total'],
            Decimal('49.40'),
        )
        self.assertIsNone(
            Trip.objects.filter(excavator=self.excavator).aggregate(total=Sum('volume_m3'))['total'],
        )

        repeated = self.sync(
            [accepted, loaded],
            client=other_client,
            actor=other_operator,
            access=other_access,
            device_id='free-bucket-other-001',
        )
        self.assertEqual(
            {item['event_id']: item['status'] for item in repeated.json()['results']},
            {accepted['event_id']: 'deduplicated', loaded['event_id']: 'deduplicated'},
        )
        self.assertEqual(Trip.objects.count(), 1)

    def test_new_load_event_id_after_used_acceptance_is_terminal_conflict(self):
        other_client, identity = self.other_excavator_identity()
        accepted = self.accept_event(event_id='free-accept-terminal-load', **identity)
        loaded = self.load_event(accepted, event_id='free-load-terminal-first', **identity)
        first_response = self.sync(
            [accepted, loaded],
            client=other_client,
            actor=identity['actor'],
            access=identity['access'],
            device_id='free-bucket-terminal-load-001',
        )
        self.assertEqual(
            {item['status'] for item in first_response.json()['results']},
            {'accepted'},
        )
        second = self.load_event(
            accepted,
            event_id='free-load-terminal-second',
            sequence=3,
            **identity,
        )
        second_result = self.sync(
            [second],
            client=other_client,
            actor=identity['actor'],
            access=identity['access'],
            device_id='free-bucket-terminal-load-001',
        ).json()['results'][0]
        self.assertEqual(second_result['status'], 'conflict', second_result)
        self.assertEqual(second_result['code'], 'free_bucket_already_loaded')
        self.assertEqual(Trip.objects.count(), 1)

    def test_server_confirmed_acceptance_can_be_loaded_from_another_device(self):
        """A rendered server card must not depend on the device that accepted it."""
        other_client, other_operator, other_shift = (
            trip_fixtures.ExcavatorWorkServerIntegrationTests.create_other_excavator_client(self)
        )
        other_access = EmployeeAccess.objects.get(employee=other_operator, role=self.role)
        identity = {
            'actor': other_operator,
            'access': other_access,
            'shift': other_shift,
            'excavator': self.other_excavator,
        }
        accepted = self.accept_event('free-cross-device-accept', 1, **identity)
        accepted_result = self.sync(
            [accepted], client=other_client, actor=other_operator, access=other_access,
            device_id='free-bucket-origin-device',
        ).json()['results'][0]
        self.assertEqual(accepted_result['status'], 'accepted', accepted_result)

        acceptance_id = accepted_result['server_ids']['free_bucket_acceptance_id']
        loaded = self.load_event(
            accepted,
            event_id='free-cross-device-load',
            sequence=1,
            **identity,
        )
        loaded['depends_on'] = []
        loaded['payload'].pop('free_bucket_acceptance_local_id')
        loaded['payload']['free_bucket_acceptance_id'] = acceptance_id

        loaded_result = self.sync(
            [loaded], client=other_client, actor=other_operator, access=other_access,
            device_id='free-bucket-loading-device',
        ).json()['results'][0]

        self.assertEqual(loaded_result['status'], 'accepted', loaded_result)
        self.assertEqual(Trip.objects.count(), 1)
        acceptance = FreeBucketAcceptance.objects.get(pk=acceptance_id)
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.USED)
        self.assertEqual(acceptance.used_trip_id, Trip.objects.get().id)

        work = other_client.get(reverse('excavator_work'))
        self.assertEqual(work.status_code, 200)
        self.assertEqual(work.context['free_bucket_cards'], [])
        dump_card = next(
            card for card in work.context['dump_cards']
            if card['point'].id == self.dump_point.id
        )
        dump_badge = next(
            item for item in dump_card['pending_trucks']
            if item['trip_id'] == acceptance.used_trip_id
        )
        self.assertEqual(dump_badge['auto_hide_kind'], 'free_bucket')
        self.assertEqual(
            dump_badge['auto_hide_at'],
            acceptance.used_trip.loaded_at + timedelta(minutes=5),
        )

        trip = acceptance.used_trip
        trip.loaded_at = timezone.now() - timedelta(minutes=5, seconds=1)
        trip.save(update_fields=['loaded_at'])
        expired_work = other_client.get(reverse('excavator_work'))
        self.assertFalse(any(
            item['trip_id'] == trip.id
            for card in expired_work.context['dump_cards']
            for item in card['pending_trucks']
        ))
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertIsNone(trip.completed_at)
        self.assertIsNone(trip.unload_received_at)

    def test_server_confirmed_acceptance_can_be_cancelled_from_another_device(self):
        other_client, identity = self.other_excavator_identity()
        accepted = self.accept_event('free-cross-device-cancel-accept', 1, **identity)
        accepted_result = self.sync(
            [accepted], client=other_client, actor=identity['actor'], access=identity['access'],
            device_id='free-bucket-cancel-origin-device',
        ).json()['results'][0]
        self.assertEqual(accepted_result['status'], 'accepted', accepted_result)

        cancelled = self.event(
            'free-cross-device-cancel',
            'excavator.free_bucket.cancelled',
            1,
            occurred_at=timezone.datetime.fromisoformat(accepted['occurred_at']) + timedelta(seconds=1),
            depends_on=[],
            payload={
                'free_bucket_acceptance_id': (
                    accepted_result['server_ids']['free_bucket_acceptance_id']
                ),
            },
            **identity,
        )
        cancelled_result = self.sync(
            [cancelled], client=other_client, actor=identity['actor'], access=identity['access'],
            device_id='free-bucket-cancel-second-device',
        ).json()['results'][0]

        self.assertEqual(cancelled_result['status'], 'accepted', cancelled_result)
        self.assertEqual(
            FreeBucketAcceptance.objects.get().status,
            FreeBucketAcceptanceStatus.CANCELLED,
        )
        self.assertEqual(Trip.objects.count(), 0)

    def test_free_bucket_trip_uses_only_snapshot_dump_points_for_driver_changes(self):
        second_point = DumpPoint.objects.create(name='Склад из снимка')
        outside_point = DumpPoint.objects.create(name='Чужая активная точка')
        placement = ExcavatorPlacement.objects.get(excavator=self.other_excavator)
        ExcavatorDumpPointSetting.objects.create(
            placement=placement,
            dump_point=second_point,
            position=1,
        )

        selected = self.select_event(event_id='driver-free-snapshot-points', sequence=1)
        selected_result = self.sync_driver([selected]).json()['results'][0]
        self.assertEqual(selected_result['status'], 'accepted', selected_result)

        other_client, identity = self.other_excavator_identity()
        accepted = self.accept_event(
            event_id='free-snapshot-points-accept',
            sequence=1,
            occurred_at=timezone.datetime.fromisoformat(selected['occurred_at']) + timedelta(seconds=1),
            **identity,
        )
        accepted_result = self.sync(
            [accepted], client=other_client, actor=identity['actor'], access=identity['access'],
            device_id='free-snapshot-points-operator',
        ).json()['results'][0]
        self.assertEqual(accepted_result['status'], 'accepted', accepted_result)
        loaded = self.load_event(
            accepted,
            event_id='free-snapshot-points-load',
            sequence=2,
            **identity,
        )
        loaded_result = self.sync(
            [loaded], client=other_client, actor=identity['actor'], access=identity['access'],
            device_id='free-snapshot-points-operator',
        ).json()['results'][0]
        self.assertEqual(loaded_result['status'], 'accepted', loaded_result)
        trip = Trip.objects.get()

        second_point.name = 'Переименованная после погрузки точка'
        second_point.is_active = False
        second_point.save(update_fields=['name', 'is_active'])
        screen = self.driver_client.get(reverse('driver_shift'))
        self.assertEqual(screen.status_code, 200)
        self.assertEqual(
            [point['id'] for point in screen.context['unload_points']],
            [self.dump_point.id, second_point.id],
        )
        self.assertEqual(
            [point['name'] for point in screen.context['unload_points']],
            [self.dump_point.name, 'Склад из снимка'],
        )
        # Чужая точка не должна быть выбираемой карточкой. В разметке она
        # встречается как запасной контекст ручного рабочего места, который
        # отдаётся намеренно ради мгновенного возврата после отмены ковша,
        # поэтому проверяется признак карточки, а не строка по всей странице.
        self.assertNotContains(screen, f'data-eo-dump-name="{outside_point.name}"')

        change_to_saved = self.driver_event(
            'free-snapshot-change-saved',
            'driver.trip.dump_point_changed',
            2,
            occurred_at=trip.loaded_at + timedelta(seconds=1),
            payload={
                'trip_id': trip.id,
                'dump_point_id': second_point.id,
                'expected_actual_dump_point_id': self.dump_point.id,
            },
        )
        change_to_saved['trip_id'] = trip.id
        saved_result = self.sync_driver([change_to_saved]).json()['results'][0]
        self.assertEqual(saved_result['status'], 'accepted', saved_result)
        trip.refresh_from_db()
        self.assertEqual(trip.actual_dump_point_id, second_point.id)

        change_to_outside = self.driver_event(
            'free-snapshot-change-outside',
            'driver.trip.dump_point_changed',
            3,
            occurred_at=trip.loaded_at + timedelta(seconds=2),
            depends_on=[change_to_saved['event_id']],
            payload={
                'trip_id': trip.id,
                'dump_point_id': outside_point.id,
                'expected_actual_dump_point_id': second_point.id,
            },
        )
        change_to_outside['trip_id'] = trip.id
        outside_result = self.sync_driver([change_to_outside]).json()['results'][0]
        self.assertEqual(outside_result['status'], 'conflict', outside_result)
        self.assertEqual(outside_result['code'], 'free_bucket_dump_point_changed')
        trip.refresh_from_db()
        self.assertEqual(trip.actual_dump_point_id, second_point.id)

        online_response = self.driver_client.post(
            reverse('driver_change_unload_point', args=[trip.id]),
            {
                'client_action_id': 'free-snapshot-online-outside',
                'dump_point': outside_point.id,
            },
            follow=True,
        )
        self.assertContains(
            online_response,
            'Точка разгрузки не входила в сохранённые настройки свободного ковша.',
        )
        trip.refresh_from_db()
        self.assertEqual(trip.actual_dump_point_id, second_point.id)

    def test_out_of_order_free_bucket_load_retries_then_creates_exactly_one_trip(self):
        """A saved load must wait for its acceptance, not be discarded or duplicated."""
        accepted = self.accept_event('free-accept-later', 1)
        loaded = self.load_event(accepted, event_id='free-load-earlier', sequence=2)

        waiting = self.sync([loaded]).json()['results'][0]
        self.assertEqual(waiting['status'], 'retry', waiting)
        self.assertTrue(waiting['retryable'])
        self.assertEqual(Trip.objects.count(), 0)

        accepted_result = self.sync([accepted]).json()['results'][0]
        self.assertEqual(accepted_result['status'], 'accepted', accepted_result)

        replayed = self.sync([loaded]).json()['results'][0]
        self.assertEqual(replayed['status'], 'accepted', replayed)
        trip = Trip.objects.get()
        self.assertEqual(trip.loaded_at, timezone.datetime.fromisoformat(loaded['occurred_at']))
        self.assertEqual(trip.excavator_id, self.excavator.id)
        self.assertEqual(FreeBucketAcceptance.objects.get().used_trip_id, trip.id)

        deduplicated = self.sync([loaded]).json()['results'][0]
        self.assertEqual(deduplicated['status'], 'deduplicated', deduplicated)
        self.assertEqual(Trip.objects.count(), 1)

    def test_second_confirmed_acceptance_of_the_same_truck_is_a_stable_conflict(self):
        first = self.accept_event('free-accept-first', 1)
        first_result = self.sync([first], device_id='free-bucket-first-device').json()['results'][0]
        self.assertEqual(first_result['status'], 'accepted', first_result)

        other_client, other_operator, other_shift = (
            trip_fixtures.ExcavatorWorkServerIntegrationTests.create_other_excavator_client(self)
        )
        other_access = EmployeeAccess.objects.get(employee=other_operator, role=self.role)
        second = self.accept_event(
            'free-accept-second', 1,
            actor=other_operator,
            access=other_access,
            shift=other_shift,
            excavator=self.other_excavator,
        )
        second_result = self.sync(
            [second], client=other_client, actor=other_operator, access=other_access,
            device_id='free-bucket-second-device',
        ).json()['results'][0]
        self.assertEqual(second_result['status'], 'conflict', second_result)
        self.assertEqual(second_result['code'], 'free_bucket_already_accepted')
        self.assertEqual(FreeBucketAcceptance.objects.count(), 1)

    def test_second_acceptance_after_free_bucket_load_is_a_stable_conflict(self):
        other_client, other_operator, other_shift = (
            trip_fixtures.ExcavatorWorkServerIntegrationTests.create_other_excavator_client(self)
        )
        other_access = EmployeeAccess.objects.get(employee=other_operator, role=self.role)
        identity = {
            'actor': other_operator,
            'access': other_access,
            'shift': other_shift,
            'excavator': self.other_excavator,
        }
        accepted = self.accept_event('free-accept-used', 1, **identity)
        loaded = self.load_event(accepted, event_id='free-load-used', sequence=2, **identity)
        first = self.sync(
            [loaded, accepted],
            client=other_client,
            actor=other_operator,
            access=other_access,
            device_id='free-bucket-used-device',
        ).json()['results']
        self.assertEqual({item['status'] for item in first}, {'accepted'}, first)

        second = self.accept_event('free-accept-after-used', 1)
        result = self.sync(
            [second],
            device_id='free-bucket-after-used-device',
        ).json()['results'][0]
        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'open_trip_exists')
        self.assertEqual(Trip.objects.count(), 1)
        self.assertEqual(
            FreeBucketAcceptance.objects.get().status,
            FreeBucketAcceptanceStatus.USED,
        )

    def test_used_state_expires_at_five_minutes_without_closing_trip_or_assignment(self):
        accepted = self.accept_event()
        loaded = self.load_event(accepted)
        response = self.sync([loaded, accepted])
        self.assertEqual(response.status_code, 200, response.content)
        trip = Trip.objects.get()
        acceptance = FreeBucketAcceptance.objects.get()
        anchor = timezone.now() - timedelta(minutes=5)
        Trip.objects.filter(pk=trip.pk).update(loaded_at=anchor)
        FreeBucketAcceptance.objects.filter(pk=acceptance.pk).update(used_at=anchor)

        from trips.free_bucket import (
            active_free_bucket_acceptance_for_truck,
            reconcile_expired_free_bucket_acceptances,
        )

        self.assertIsNotNone(active_free_bucket_acceptance_for_truck(
            self.truck,
            now=anchor + timedelta(minutes=5) - timedelta(microseconds=1),
        ))
        self.assertEqual(
            reconcile_expired_free_bucket_acceptances(now=anchor + timedelta(minutes=5)),
            1,
        )
        acceptance.refresh_from_db()
        trip.refresh_from_db()
        self.assignment.refresh_from_db()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)
        self.assertEqual(acceptance.closed_at, anchor + timedelta(minutes=5))
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertEqual(self.assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(self.assignment.ended_at)
        self.assertEqual(
            reconcile_expired_free_bucket_acceptances(now=anchor + timedelta(minutes=6)),
            0,
        )

        duplicate = self.load_event(accepted, event_id='free-load-after-timeout', sequence=3)
        duplicate['depends_on'] = []
        duplicate['payload'].pop('free_bucket_acceptance_local_id')
        duplicate['payload']['free_bucket_acceptance_id'] = acceptance.id
        duplicate_result = self.sync([duplicate]).json()['results'][0]
        self.assertEqual(duplicate_result['status'], 'conflict', duplicate_result)
        self.assertEqual(duplicate_result['code'], 'free_bucket_not_available')
        self.assertEqual(Trip.objects.count(), 1)

    def test_database_blocks_second_open_right_until_used_history_is_closed(self):
        accepted = self.accept_event()
        loaded = self.load_event(accepted)
        response = self.sync([loaded, accepted])
        self.assertEqual(response.status_code, 200, response.content)
        used = FreeBucketAcceptance.objects.get()
        self.assertEqual(used.status, FreeBucketAcceptanceStatus.USED)
        with self.assertRaises(IntegrityError), transaction.atomic():
            FreeBucketAcceptance.objects.create(
                client_acceptance_id='constraint-second-while-used',
                truck=self.truck,
                excavator=self.excavator,
                operator=self.operator,
                loading_shift=self.shift,
                primary_assignment=self.assignment,
                occurred_at=timezone.now(),
                accepted_at=timezone.now(),
            )
        used.status = FreeBucketAcceptanceStatus.CLOSED
        used.closed_at = timezone.now()
        used.save(update_fields=['status', 'closed_at'])
        second = FreeBucketAcceptance.objects.create(
            client_acceptance_id='constraint-second-accepted',
            truck=self.truck,
            excavator=self.excavator,
            operator=self.operator,
            loading_shift=self.shift,
            primary_assignment=self.assignment,
            occurred_at=timezone.now(),
            accepted_at=timezone.now(),
        )
        self.assertEqual(second.status, FreeBucketAcceptanceStatus.ACCEPTED)
        with self.assertRaises(IntegrityError), transaction.atomic():
            FreeBucketAcceptance.objects.create(
                client_acceptance_id='constraint-third-accepted',
                truck=self.truck,
                excavator=self.excavator,
                operator=self.operator,
                loading_shift=self.shift,
                primary_assignment=self.assignment,
                occurred_at=timezone.now(),
                accepted_at=timezone.now(),
            )

    def test_free_bucket_load_preserves_passive_manual_control(self):
        accepted = self.accept_event()
        loaded = self.load_event(accepted)
        loaded['payload']['manual_control'] = True

        response = self.sync([loaded, accepted])
        self.assertEqual(response.status_code, 200, response.content)
        result = {item['event_id']: item for item in response.json()['results']}
        self.assertEqual(result[loaded['event_id']]['status'], 'accepted', result)
        trip = Trip.objects.get()
        self.assertTrue(trip.driver_participation_recorded)
        self.assertIsNone(trip.driver_control_shift_id)

    def test_driver_offline_unload_completes_the_normal_free_bucket_trip(self):
        accepted = self.accept_event()
        loaded = self.load_event(accepted)
        response = self.sync([loaded, accepted])
        self.assertEqual(response.status_code, 200, response.content)
        trip = Trip.objects.get()
        occurred_at = trip.loaded_at + timedelta(minutes=3)
        unload = {
            'event_id': 'driver-unload-free-bucket-1',
            'event_type': 'driver.trip.unloaded',
            'format_version': 1,
            'actor_id': self.driver.id,
            'access_id': self.driver_access.id,
            'role_code': 'driver',
            'occurred_at': occurred_at.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': trip.id,
            'context_snapshot': {
                'actor_id': self.driver.id,
                'access_id': self.driver_access.id,
                'role_code': 'driver',
            },
            'payload': {'trip_id': trip.id},
        }
        driver_client = Client()
        session = driver_client.session
        session['employee_access_id'] = self.driver_access.id
        session.save()

        unload_response = self.sync(
            [unload], client=driver_client, actor=self.driver, access=self.driver_access,
            role_code='driver',
            device_id='free-bucket-driver-offline-001',
        )
        self.assertEqual(unload_response.status_code, 200, unload_response.content)
        unload_result = unload_response.json()['results'][0]
        self.assertEqual(unload_result['status'], 'accepted', unload_result)
        trip.refresh_from_db()
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.completed_at, occurred_at)
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)

    def test_accept_then_cancel_releases_without_creating_trip(self):
        accepted = self.accept_event()
        cancelled = self.event(
            'free-cancel-1',
            'excavator.free_bucket.cancelled',
            2,
            occurred_at=timezone.datetime.fromisoformat(accepted['occurred_at']) + timedelta(seconds=1),
            depends_on=[accepted['event_id']],
            payload={'free_bucket_acceptance_local_id': accepted['event_id']},
        )

        response = self.sync([cancelled, accepted])

        self.assertEqual(response.status_code, 200, response.content)
        results = {item['event_id']: item for item in response.json()['results']}
        self.assertEqual(results[accepted['event_id']]['status'], 'accepted', results)
        self.assertEqual(results[cancelled['event_id']]['status'], 'accepted', results)
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CANCELLED)
        self.assertIsNotNone(acceptance.cancelled_at)
        self.assertEqual(Trip.objects.count(), 0)

    def test_cancelling_loaded_trip_closes_consumed_acceptance(self):
        accepted = self.accept_event()
        loaded = self.load_event(accepted)
        cancelled = self.event(
            'free-loaded-cancel-1',
            'excavator.trip.loaded.cancelled',
            3,
            occurred_at=timezone.datetime.fromisoformat(loaded['occurred_at']) + timedelta(seconds=1),
            depends_on=[loaded['event_id']],
            local_trip_id=loaded['local_trip_id'],
            payload={'local_trip_id': loaded['local_trip_id']},
        )

        response = self.sync([cancelled, loaded, accepted])

        self.assertEqual(response.status_code, 200, response.content)
        results = {item['event_id']: item for item in response.json()['results']}
        self.assertEqual(
            {key: results[key]['status'] for key in (accepted['event_id'], loaded['event_id'], cancelled['event_id'])},
            {accepted['event_id']: 'accepted', loaded['event_id']: 'accepted', cancelled['event_id']: 'accepted'},
        )
        trip = Trip.objects.get()
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)
        self.assertEqual(acceptance.used_trip_id, trip.id)
        self.assertIsNotNone(acceptance.closed_at)

    def test_confirmed_other_excavator_acceptance_blocks_online_and_offline_ordinary_load(self):
        other_client, other_operator, other_shift = (
            trip_fixtures.ExcavatorWorkServerIntegrationTests.create_other_excavator_client(self)
        )
        other_access = EmployeeAccess.objects.get(employee=other_operator, role=self.role)
        accepted = self.accept_event(
            actor=other_operator,
            access=other_access,
            shift=other_shift,
            excavator=self.other_excavator,
        )
        accepted_result = self.sync(
            [accepted],
            client=other_client,
            actor=other_operator,
            access=other_access,
            device_id='free-bucket-other-guard-001',
        ).json()['results'][0]
        self.assertEqual(accepted_result['status'], 'accepted', accepted_result)

        online = trip_fixtures.ExcavatorWorkServerIntegrationTests.post_truck_loaded(
            self,
            client_action_id='ordinary-online-blocked',
            assignment=self.assignment,
        )
        self.assertEqual(online.status_code, 409, online.content)
        self.assertEqual(online.json()['code'], 'free_bucket_acceptance_required')
        primary_screen = self.client.get(reverse('excavator_work'))
        self.assertContains(primary_screen, 'Свободный ковш')
        self.assertContains(primary_screen, str(self.other_excavator.garage_number))
        self.assertContains(primary_screen, '"updated_at"')
        self.assertContains(primary_screen, '"availability_label"')

        ordinary_offline = self.event(
            'ordinary-offline-blocked',
            'excavator.trip.loaded',
            1,
            local_trip_id='local-ordinary-offline-blocked',
            payload={
                'truck_id': self.truck.id,
                'assignment_id': self.assignment.id,
                'dump_point_id': self.dump_point.id,
                'rock_type_id': self.rock.id,
                'manual_control': False,
            },
        )
        offline_result = self.sync(
            [ordinary_offline],
            device_id='free-bucket-ordinary-guard-001',
        ).json()['results'][0]
        self.assertEqual(offline_result['status'], 'conflict', offline_result)
        self.assertEqual(offline_result['code'], 'free_bucket_acceptance_required')
        self.assertEqual(Trip.objects.count(), 0)
        self.assertEqual(FreeBucketAcceptance.objects.get().status, FreeBucketAcceptanceStatus.ACCEPTED)
        self.assertEqual(
            OfflineFieldEvent.objects.get(event_id=ordinary_offline['event_id']).status,
            'conflict',
        )

    def test_driver_request_blocks_online_and_offline_ordinary_load_before_operator_accepts(self):
        selected = self.select_event(event_id='driver-free-request-ordinary-guard')
        selected_result = self.sync_driver([selected]).json()['results'][0]
        self.assertEqual(selected_result['status'], 'accepted', selected_result)

        online = trip_fixtures.ExcavatorWorkServerIntegrationTests.post_truck_loaded(
            self,
            client_action_id='ordinary-online-requested-blocked',
            assignment=self.assignment,
        )
        self.assertEqual(online.status_code, 409, online.content)
        self.assertEqual(online.json()['code'], 'free_bucket_acceptance_required')

        ordinary_offline = self.event(
            'ordinary-offline-requested-blocked',
            'excavator.trip.loaded',
            1,
            local_trip_id='local-ordinary-offline-requested-blocked',
            payload={
                'truck_id': self.truck.id,
                'assignment_id': self.assignment.id,
                'dump_point_id': self.dump_point.id,
                'rock_type_id': self.rock.id,
                'manual_control': False,
            },
        )
        offline_result = self.sync(
            [ordinary_offline],
            device_id='free-bucket-requested-ordinary-guard-001',
        ).json()['results'][0]
        self.assertEqual(offline_result['status'], 'conflict', offline_result)
        self.assertEqual(offline_result['code'], 'free_bucket_acceptance_required')
        self.assertEqual(Trip.objects.count(), 0)
        self.assertEqual(
            FreeBucketAcceptance.objects.get().status,
            FreeBucketAcceptanceStatus.REQUESTED,
        )

    def test_conflicting_free_bucket_load_is_retained_in_existing_review_queue(self):
        other_client, other_operator, other_shift = (
            trip_fixtures.ExcavatorWorkServerIntegrationTests.create_other_excavator_client(self)
        )
        other_access = EmployeeAccess.objects.get(employee=other_operator, role=self.role)
        accepted = self.accept_event(
            actor=other_operator,
            access=other_access,
            shift=other_shift,
            excavator=self.other_excavator,
        )
        self.sync(
            [accepted], client=other_client, actor=other_operator, access=other_access,
            device_id='free-bucket-other-review-001',
        )
        conflicting_load = self.load_event(accepted)
        result = self.sync([conflicting_load]).json()['results'][0]

        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(Trip.objects.count(), 0)
        review = AdminConflict.objects.get(process='Свободный ковш')
        self.assertEqual(review.employee_id, self.operator.id)
        self.assertIn(conflicting_load['event_id'], review.description)

    def test_driver_completion_closes_used_acceptance_without_changing_primary_assignment(self):
        accepted = self.accept_event()
        loaded = self.load_event(accepted)
        response = self.sync([loaded, accepted])
        self.assertEqual(response.status_code, 200, response.content)
        trip = Trip.objects.get()

        from trips.views import finalize_trip_unloaded

        self.assertTrue(finalize_trip_unloaded(
            trip,
            driver=self.truck_shift.employee,
            unloading_shift=self.truck_shift,
            occurred_at=timezone.now(),
        ))
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(self.assignment.ended_at)

    def test_changed_primary_assignment_does_not_break_accepted_free_bucket_load(self):
        other_client, other_operator, other_shift = (
            trip_fixtures.ExcavatorWorkServerIntegrationTests.create_other_excavator_client(self)
        )
        other_access = EmployeeAccess.objects.get(employee=other_operator, role=self.role)
        identity = {
            'actor': other_operator,
            'access': other_access,
            'shift': other_shift,
            'excavator': self.other_excavator,
        }
        accepted = self.accept_event(**identity)
        accepted_response = self.sync(
            [accepted], client=other_client, actor=other_operator, access=other_access,
            device_id='free-bucket-primary-changed-001',
        )
        self.assertEqual(accepted_response.json()['results'][0]['status'], 'accepted')

        changed_at = timezone.now()
        self.assignment.status = AssignmentStatus.CANCELLED
        self.assignment.ended_at = changed_at
        self.assignment.save(update_fields=['status', 'ended_at'])
        new_primary = HaulAssignment.objects.create(
            truck=self.truck,
            excavator=self.other_excavator,
            status=AssignmentStatus.ACCEPTED,
            assigned_at=changed_at,
            accepted_at=changed_at,
        )

        loaded = self.load_event(accepted, **identity)
        loaded_response = self.sync(
            [loaded], client=other_client, actor=other_operator, access=other_access,
            device_id='free-bucket-primary-changed-001',
        )
        result = loaded_response.json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        trip = Trip.objects.get()
        self.assertEqual(trip.excavator_id, self.other_excavator.id)
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.primary_assignment_id, self.assignment.id)
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.USED)
        new_primary.refresh_from_db()
        self.assertEqual(new_primary.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(new_primary.ended_at)

    def test_dispatcher_keeps_primary_tile_and_shows_requested_free_bucket_marker(self):
        selected = self.select_event(event_id='driver-free-dispatcher-marker')
        response = self.sync_driver(
            [selected],
            device_id='driver-free-bucket-dispatcher-marker-001',
        )
        self.assertEqual(response.json()['results'][0]['status'], 'accepted')

        primary_tile = self.dispatcher_primary_tile()
        self.assertIn('Свободный ковш', primary_tile['free_bucket_label'])
        self.assertIn(str(self.other_excavator.garage_number), primary_tile['free_bucket_label'])
        self.assertIsNone(primary_tile['free_bucket_expires_at'])

    def test_dispatcher_used_marker_is_visible_only_for_five_minutes(self):
        other_client, other_operator, other_shift = (
            trip_fixtures.ExcavatorWorkServerIntegrationTests.create_other_excavator_client(self)
        )
        other_access = EmployeeAccess.objects.get(employee=other_operator, role=self.role)
        identity = {
            'actor': other_operator,
            'access': other_access,
            'shift': other_shift,
            'excavator': self.other_excavator,
        }
        accepted = self.accept_event('free-marker-used', 1, **identity)
        loaded = self.load_event(accepted, event_id='free-marker-load', sequence=2, **identity)
        result = self.sync(
            [loaded, accepted], client=other_client, actor=other_operator, access=other_access,
            device_id='free-bucket-marker-used-device',
        ).json()['results']
        self.assertEqual({item['status'] for item in result}, {'accepted'}, result)
        trip = Trip.objects.get()
        acceptance = FreeBucketAcceptance.objects.get()
        recent = timezone.now() - timedelta(minutes=4, seconds=59)
        Trip.objects.filter(pk=trip.pk).update(loaded_at=recent)
        FreeBucketAcceptance.objects.filter(pk=acceptance.pk).update(used_at=recent)

        recent_tile = self.dispatcher_primary_tile()
        self.assertIn('Свободный ковш', recent_tile['free_bucket_label'])
        self.assertEqual(recent_tile['free_bucket_expires_at'], recent + timedelta(minutes=5))

        expired = timezone.now() - timedelta(minutes=5, seconds=1)
        Trip.objects.filter(pk=trip.pk).update(loaded_at=expired)
        FreeBucketAcceptance.objects.filter(pk=acceptance.pk).update(used_at=expired)
        expired_tile = self.dispatcher_primary_tile()
        self.assertEqual(expired_tile['free_bucket_label'], '')
        self.assertIsNone(expired_tile['free_bucket_expires_at'])

    def test_excavator_fragment_carries_free_bucket_snapshots(self):
        other_client, other_operator, other_shift = (
            trip_fixtures.ExcavatorWorkServerIntegrationTests.create_other_excavator_client(self)
        )
        other_access = EmployeeAccess.objects.get(employee=other_operator, role=self.role)
        accepted = self.accept_event(
            actor=other_operator,
            access=other_access,
            shift=other_shift,
            excavator=self.other_excavator,
        )
        result = self.sync(
            [accepted], client=other_client, actor=other_operator, access=other_access,
            device_id='free-bucket-fragment-001',
        ).json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)

        payload = other_client.get(
            reverse('excavator_work'),
            {'_operational_fragment': 'excavator', '_operational_version': 0},
        ).json()
        self.assertEqual(payload['screen'], 'excavator')
        self.assertIn('free_bucket_truck_directory', payload)
        self.assertEqual(
            [card['truck_id'] for card in payload['free_bucket_cards']],
            [self.truck.id],
        )
        self.assertEqual(
            payload['free_bucket_cards'][0]['client_acceptance_id'],
            accepted['event_id'],
        )


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class FreeBucketPostgreSQLConcurrencyTests(TransactionTestCase):
    """The two-device acceptance race needs real PostgreSQL row locks."""

    reset_sequences = True
    create_registered_driver_shift = (
        trip_fixtures.ExcavatorWorkServerIntegrationTests.create_registered_driver_shift
    )

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Конкурентный приём под свободный ковш проверяется только на PostgreSQL.')
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), apps.get_models()):
                cursor.execute(sql)
        DowntimeReason.objects.get_or_create(
            name='Ожидание самосвалов',
            defaults={
                'short_label': 'Ожидание самосвалов',
                'show_for_excavator_operator': True,
            },
        )
        trip_fixtures.ExcavatorWorkServerIntegrationTests.setUp(self)
        self.url = reverse('offline_events_sync')
        self.shift = EmployeeShift.objects.get(employee=self.operator, closed_at__isnull=True)
        self.assignment = HaulAssignment.objects.get(
            truck=self.truck,
            excavator=self.excavator,
            status=AssignmentStatus.ACCEPTED,
        )
        self.first_session_key = self.client.cookies['sessionid'].value
        self.other_client, self.other_operator, self.other_shift = (
            trip_fixtures.ExcavatorWorkServerIntegrationTests.create_other_excavator_client(self)
        )
        self.other_access = EmployeeAccess.objects.get(employee=self.other_operator, role=self.role)
        self.other_session_key = self.other_client.cookies['sessionid'].value
        driver_client = Client()
        driver_session = driver_client.session
        driver_session['employee_access_id'] = self.driver_access.id
        driver_session.save()
        self.driver_session_key = driver_client.cookies['sessionid'].value
        for excavator in (self.excavator, self.other_excavator):
            placement = ExcavatorPlacement.objects.create(
                excavator=excavator,
                zone=ExcavatorPlacement.Zone.ACTIVE,
                work_rock_type=self.rock,
                work_dump_point=self.dump_point,
            )
            ExcavatorDumpPointSetting.objects.create(
                placement=placement,
                dump_point=self.dump_point,
                position=0,
            )

    def acceptance_event(self, *, event_id, sequence, access, shift):
        return {
            'event_id': event_id,
            'event_type': 'excavator.free_bucket.accepted',
            'format_version': 1,
            'actor_id': access.employee_id,
            'access_id': access.id,
            'role_code': 'excavator_operator',
            'occurred_at': timezone.now().isoformat(),
            'sequence': sequence,
            'depends_on': [],
            'shift_id': shift.id,
            'equipment_id': shift.equipment_id,
            'context_snapshot': {
                'actor_id': access.employee_id,
                'access_id': access.id,
                'role_code': 'excavator_operator',
            },
            'payload': {'truck_id': self.truck.id},
        }

    def driver_selection_event(self, *, event_id, sequence):
        return {
            'event_id': event_id,
            'event_type': 'driver.free_bucket.selected',
            'format_version': 1,
            'actor_id': self.driver.id,
            'access_id': self.driver_access.id,
            'role_code': 'driver',
            'occurred_at': timezone.now().isoformat(),
            'sequence': sequence,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'context_snapshot': {
                'actor_id': self.driver.id,
                'access_id': self.driver_access.id,
                'role_code': 'driver',
            },
            'payload': {'excavator_id': self.other_excavator.id},
        }

    def post_from_thread(
        self, *, session_key, access, device_id, event, start,
        role_code='excavator_operator',
    ):
        close_old_connections()
        try:
            start.wait(timeout=10)
            client = Client()
            client.cookies['sessionid'] = session_key
            response = client.post(
                self.url,
                data=json.dumps({
                    'protocol_version': 1,
                    'actor_id': access.employee_id,
                    'access_id': access.id,
                    'role_code': role_code,
                    'device_id': device_id,
                    'events': [event],
                }),
                content_type='application/json',
            )
            return response.status_code, response.json()['results'][0]
        finally:
            close_old_connections()

    def test_two_offline_excavators_accept_same_truck_once(self):
        start = Barrier(2)
        first = self.acceptance_event(
            event_id='free-pg-accept-a',
            sequence=1,
            access=self.access,
            shift=self.shift,
        )
        second = self.acceptance_event(
            event_id='free-pg-accept-b',
            sequence=1,
            access=self.other_access,
            shift=self.other_shift,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda payload: self.post_from_thread(start=start, **payload),
                [
                    {
                        'session_key': self.first_session_key,
                        'access': self.access,
                        'device_id': 'free-pg-device-a',
                        'event': first,
                    },
                    {
                        'session_key': self.other_session_key,
                        'access': self.other_access,
                        'device_id': 'free-pg-device-b',
                        'event': second,
                    },
                ],
            ))

        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(sorted(result['status'] for _, result in results), ['accepted', 'conflict'])
        self.assertEqual(FreeBucketAcceptance.objects.count(), 1)
        self.assertEqual(Trip.objects.count(), 0)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(self.assignment.ended_at)

    def test_driver_selection_and_operator_acceptance_converge_to_same_row(self):
        start = Barrier(2)
        selected = self.driver_selection_event(
            event_id='free-pg-driver-select',
            sequence=1,
        )
        accepted = self.acceptance_event(
            event_id='free-pg-operator-accept',
            sequence=1,
            access=self.other_access,
            shift=self.other_shift,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda payload: self.post_from_thread(start=start, **payload),
                [
                    {
                        'session_key': self.driver_session_key,
                        'access': self.driver_access,
                        'role_code': 'driver',
                        'device_id': 'free-pg-driver-device',
                        'event': selected,
                    },
                    {
                        'session_key': self.other_session_key,
                        'access': self.other_access,
                        'device_id': 'free-pg-operator-device',
                        'event': accepted,
                    },
                ],
            ))

        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual([result['status'] for _, result in results], ['accepted', 'accepted'])
        self.assertEqual(FreeBucketAcceptance.objects.count(), 1)
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.ACCEPTED)
        self.assertEqual(acceptance.excavator_id, self.other_excavator.id)
        self.assertEqual(acceptance.requested_by_id, self.driver.id)
        self.assertEqual(acceptance.requesting_shift_id, self.truck_shift.id)
        self.assertEqual(acceptance.operator_id, self.other_operator.id)
        self.assertEqual(acceptance.loading_shift_id, self.other_shift.id)
        self.assertIsNotNone(acceptance.accepted_at)
        self.assertEqual(Trip.objects.count(), 0)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(self.assignment.ended_at)
