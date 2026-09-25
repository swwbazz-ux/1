import inspect
from decimal import Decimal
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import resolve, reverse
from django.utils import timezone

from core.models import OperationalStateEvent
from references.models import DumpPoint, Equipment, EquipmentType, RockType
from shifts.models import EmployeeShift
from users.models import Employee, EmployeeAccess, Role

from . import dispatcher_trip_commands
from . import views as trips_views
from .models import (
    DispatcherActionLog,
    DispatcherActionType,
    FreeBucketAcceptance,
    FreeBucketAcceptanceStatus,
    Trip,
    TripStatus,
)


class DispatcherTripCommandBoundaryTests(SimpleTestCase):
    def test_cancel_url_keeps_public_views_facade(self):
        match = resolve(reverse('dispatcher_cancel_trip', kwargs={'trip_id': 17}))

        self.assertIs(match.func, trips_views.dispatcher_cancel_trip_view)

    def test_complete_url_keeps_public_views_facade(self):
        match = resolve(reverse('dispatcher_complete_trip', kwargs={'trip_id': 17}))

        self.assertIs(match.func, trips_views.dispatcher_complete_trip_view)

    def test_manual_url_keeps_public_views_facade(self):
        match = resolve(
            reverse('dispatcher_manual_trip', kwargs={'equipment_id': 17}),
        )

        self.assertIs(match.func, trips_views.dispatcher_manual_trip_view)

    def test_public_cancel_view_is_thin_trip_command_facade(self):
        source = inspect.getsource(trips_views.dispatcher_cancel_trip_view)

        self.assertIn('_execute_dispatcher_cancel_trip(', source)
        self.assertIn('lock_mutation_access=lock_dispatcher_mutation_access', source)
        self.assertIn('reconcile_excavator=reconcile_excavator_waiting_for_trucks', source)
        self.assertIn('action_logger=log_dispatcher_action', source)
        self.assertNotIn('Trip.objects', source)
        self.assertNotIn('lock_production_state', source)
        self.assertNotIn('bump_operational_state', source)
        self.assertNotIn('close_free_bucket_acceptance_for_trip', source)

    def test_public_complete_view_is_thin_trip_command_facade(self):
        source = inspect.getsource(trips_views.dispatcher_complete_trip_view)

        self.assertIn('_execute_dispatcher_complete_trip(', source)
        self.assertIn('lock_mutation_access=lock_dispatcher_mutation_access', source)
        self.assertIn('finalize_trip=finalize_trip_unloaded', source)
        self.assertIn('action_logger=log_dispatcher_action', source)
        self.assertNotIn('Trip.objects', source)
        self.assertNotIn('EmployeeShift.objects', source)
        self.assertNotIn('lock_production_state', source)
        self.assertNotIn('bump_operational_state', source)

    def test_public_manual_view_is_thin_trip_command_facade(self):
        source = inspect.getsource(trips_views.dispatcher_manual_trip_view)

        self.assertIn('_execute_dispatcher_manual_trip(', source)
        self.assertIn('lock_mutation_access=lock_dispatcher_mutation_access', source)
        self.assertIn('format_datetime=format_dispatcher_datetime', source)
        self.assertIn('action_logger=log_dispatcher_action', source)
        self.assertNotIn('Trip.objects', source)
        self.assertNotIn('Equipment.objects', source)
        self.assertNotIn('EmployeeShift.objects', source)
        self.assertNotIn('bump_operational_state', source)


class DispatcherTripFacadeDelegationTests(TestCase):
    def test_cancel_facade_injects_views_patch_seams(self):
        request = RequestFactory().post('/dispatcher/trips/17/cancel/')
        expected = HttpResponse(status=302)
        with patch.object(
            trips_views,
            '_execute_dispatcher_cancel_trip',
            return_value=expected,
        ) as execute:
            response = trips_views.dispatcher_cancel_trip_view(request, trip_id=17)

        self.assertIs(response, expected)
        execute.assert_called_once_with(
            request,
            17,
            lock_mutation_access=trips_views.lock_dispatcher_mutation_access,
            reconcile_excavator=trips_views.reconcile_excavator_waiting_for_trucks,
            action_logger=trips_views.log_dispatcher_action,
        )

    def test_complete_facade_injects_views_patch_seams(self):
        request = RequestFactory().post('/dispatcher/trips/17/complete/')
        expected = HttpResponse(status=302)
        with patch.object(
            trips_views,
            '_execute_dispatcher_complete_trip',
            return_value=expected,
        ) as execute:
            response = trips_views.dispatcher_complete_trip_view(request, trip_id=17)

        self.assertIs(response, expected)
        execute.assert_called_once_with(
            request,
            17,
            lock_mutation_access=trips_views.lock_dispatcher_mutation_access,
            finalize_trip=trips_views.finalize_trip_unloaded,
            action_logger=trips_views.log_dispatcher_action,
        )

    def test_manual_facade_injects_views_patch_seams(self):
        request = RequestFactory().post('/dispatcher/trucks/17/manual-trip/')
        expected = HttpResponse(status=302)
        with patch.object(
            trips_views,
            '_execute_dispatcher_manual_trip',
            return_value=expected,
        ) as execute:
            response = trips_views.dispatcher_manual_trip_view(
                request,
                equipment_id=17,
            )

        self.assertIs(response, expected)
        execute.assert_called_once_with(
            request,
            17,
            lock_mutation_access=trips_views.lock_dispatcher_mutation_access,
            format_datetime=trips_views.format_dispatcher_datetime,
            action_logger=trips_views.log_dispatcher_action,
        )


class DispatcherTripCommandTests(TestCase):
    def setUp(self):
        self.dispatcher_role = Role.objects.create(
            code='dispatcher',
            name='Диспетчер',
        )
        self.dispatcher = Employee.objects.create(
            full_name='Диспетчер отмены рейса',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=self.dispatcher_role,
            access_code='TRIP-CANCEL-COMMAND',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.driver = Employee.objects.create(
            full_name='Водитель служебного завершения рейса',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.shift = EmployeeShift.objects.create(
            employee=self.dispatcher,
            shift_type='day',
            workplace_code='dispatcher',
            opened_at=timezone.now(),
            opened_by=self.dispatcher,
        )
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck = Equipment.objects.create(
            equipment_type=truck_type,
            garage_number='TRIP-CANCEL-TRUCK',
        )
        self.excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='TRIP-CANCEL-EXCAVATOR',
        )
        self.driver_shift = EmployeeShift.objects.create(
            employee=self.driver,
            shift_type='day',
            workplace_code='driver',
            equipment=self.truck,
            opened_at=timezone.now(),
            opened_by=self.driver,
        )
        self.rock = RockType.objects.create(
            name='Руда для отмены рейса',
            density=Decimal('2.5000'),
        )
        self.dump_point = DumpPoint.objects.create(name='Склад отмены рейса')
        session = self.client.session
        session['employee_access_id'] = self.access.id
        session.save()

    def create_trip(self, *, status=TripStatus.LOADED_WAITING_UNLOAD):
        return Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            rock_type=self.rock,
            dump_point=self.dump_point,
            assigned_dump_point=self.dump_point,
            actual_dump_point=self.dump_point,
            volume_m3=Decimal('47.00'),
            tonnage=Decimal('117.50'),
            status=status,
        )

    def post_cancel(self, trip, **payload):
        data = {'reason': '  Ошибка маршрута  ', **payload}
        return self.client.post(
            reverse('dispatcher_cancel_trip', args=[trip.id]),
            data,
        )

    def post_complete(self, trip, **payload):
        data = {'reason': '  Подтверждено диспетчером  ', **payload}
        return self.client.post(
            reverse('dispatcher_complete_trip', args=[trip.id]),
            data,
        )

    @staticmethod
    def messages_text(response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    @staticmethod
    def cancel_events():
        return OperationalStateEvent.objects.filter(
            reason='Trip:dispatcher_cancel_trip',
        )

    @staticmethod
    def complete_events():
        return OperationalStateEvent.objects.filter(
            reason='Trip:dispatcher_complete_trip',
        )

    def test_cancel_writes_full_contract_and_closes_free_bucket(self):
        trip = self.create_trip()
        used_at = timezone.now()
        acceptance = FreeBucketAcceptance.objects.create(
            client_acceptance_id='trip-cancel-free-bucket',
            truck=self.truck,
            excavator=self.excavator,
            operator=self.dispatcher,
            loading_shift=self.shift,
            status=FreeBucketAcceptanceStatus.USED,
            occurred_at=used_at,
            received_at=used_at,
            accepted_at=used_at,
            used_at=used_at,
            used_trip=trip,
        )

        response = self.post_cancel(
            trip,
            truck='TRIP-CANCEL-TRUCK',
            show_pending_assignments='1',
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response['Location'],
            f"{reverse('dispatcher_control')}?"
            'truck=TRIP-CANCEL-TRUCK&show_pending_assignments=1',
        )
        trip.refresh_from_db()
        acceptance.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        self.assertIsNotNone(trip.cancelled_at)
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)
        self.assertEqual(acceptance.closed_at, trip.cancelled_at)

        action = DispatcherActionLog.objects.get()
        self.assertEqual(action.actor, self.dispatcher)
        self.assertEqual(action.action_type, DispatcherActionType.CANCEL_TRIP)
        self.assertEqual(action.trip, trip)
        self.assertEqual(action.reason, 'Ошибка маршрута')
        self.assertEqual(
            action.target_summary,
            f'{self.truck} -> {self.dump_point}',
        )

        event = self.cancel_events().get(
            object_type='Trip',
            object_id=str(trip.id),
        )
        self.assertEqual(event.key, 'production')
        self.assertEqual(event.reason, 'Trip:dispatcher_cancel_trip')
        self.assertEqual(event.event_type, 'trip_changed')
        self.assertEqual(
            event.payload,
            {
                'action': 'dispatcher_cancel_trip',
                'trip_id': trip.id,
                'truck_id': self.truck.id,
                'excavator_id': self.excavator.id,
                'status': TripStatus.CANCELLED,
            },
        )
        self.assertIn('отменен', ' | '.join(self.messages_text(response)))

    def test_missing_reason_does_not_mutate_or_emit_side_effects(self):
        trip = self.create_trip()

        response = self.client.post(
            reverse('dispatcher_cancel_trip', args=[trip.id]),
            {'reason': '  '},
        )

        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertIsNone(trip.cancelled_at)
        self.assertFalse(DispatcherActionLog.objects.exists())
        self.assertFalse(self.cancel_events().exists())
        self.assertIn(
            'Укажите причину отмены рейса.',
            self.messages_text(response),
        )

    def test_production_state_is_locked_before_trip_row(self):
        trip = self.create_trip()
        lock_order = []
        select_trip_for_update = Trip.objects.select_for_update

        def record_trip_lock(*args, **kwargs):
            lock_order.append('trip')
            return select_trip_for_update(*args, **kwargs)

        with (
            patch.object(
                dispatcher_trip_commands,
                'lock_production_state',
                side_effect=lambda: lock_order.append('production'),
            ),
            patch.object(
                Trip.objects,
                'select_for_update',
                side_effect=record_trip_lock,
            ),
        ):
            response = self.post_cancel(trip)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(lock_order[:2], ['production', 'trip'])

    def test_inactive_role_does_not_mutate_or_emit_side_effects(self):
        trip = self.create_trip()

        with patch.object(
            trips_views,
            'role_session_state',
            return_value={'is_active': False},
        ):
            response = self.post_cancel(trip)

        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertIsNone(trip.cancelled_at)
        self.assertFalse(DispatcherActionLog.objects.exists())
        self.assertFalse(self.cancel_events().exists())
        self.assertIn(
            'Роль неактивна — доступен только просмотр.',
            self.messages_text(response),
        )

    def test_closed_dispatcher_shift_blocks_trip_mutation(self):
        trip = self.create_trip()
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.dispatcher
        self.shift.save(update_fields=['closed_at', 'closed_by'])

        response = self.post_cancel(trip)

        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertIsNone(trip.cancelled_at)
        self.assertFalse(DispatcherActionLog.objects.exists())
        self.assertFalse(self.cancel_events().exists())
        self.assertIn(
            'Смена горного диспетчера закрыта. Изменения на пульте недоступны.',
            self.messages_text(response),
        )

    def test_get_preserves_filters_and_does_not_mutate_trip(self):
        trip = self.create_trip()

        response = self.client.get(
            reverse('dispatcher_cancel_trip', args=[trip.id]),
            {'truck': 'TRIP-CANCEL-TRUCK'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response['Location'],
            f"{reverse('dispatcher_control')}?truck=TRIP-CANCEL-TRUCK",
        )
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertIsNone(trip.cancelled_at)
        self.assertFalse(DispatcherActionLog.objects.exists())
        self.assertFalse(self.cancel_events().exists())

    def test_terminal_trip_returns_original_error_without_new_side_effects(self):
        trip = self.create_trip(status=TripStatus.COMPLETED)

        response = self.post_cancel(trip)

        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertIsNone(trip.cancelled_at)
        self.assertFalse(DispatcherActionLog.objects.exists())
        self.assertFalse(self.cancel_events().exists())
        self.assertIn(
            'Активный рейс для отмены не найден.',
            self.messages_text(response),
        )

    def test_manager_role_keeps_original_role_home_redirect(self):
        trip = self.create_trip()
        manager_role = Role.objects.create(code='manager', name='Руководитель')
        self.access.role = manager_role
        self.access.save(update_fields=['role'])

        response = self.post_cancel(trip)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('role_home'))
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertFalse(DispatcherActionLog.objects.exists())
        self.assertFalse(self.cancel_events().exists())

    def test_complete_writes_full_contract_and_closes_free_bucket(self):
        trip = self.create_trip()
        used_at = timezone.now()
        acceptance = FreeBucketAcceptance.objects.create(
            client_acceptance_id='trip-complete-free-bucket',
            truck=self.truck,
            excavator=self.excavator,
            operator=self.dispatcher,
            loading_shift=self.shift,
            status=FreeBucketAcceptanceStatus.USED,
            occurred_at=used_at,
            received_at=used_at,
            accepted_at=used_at,
            used_at=used_at,
            used_trip=trip,
        )

        response = self.post_complete(
            trip,
            truck='TRIP-CANCEL-TRUCK',
            show_pending_assignments='1',
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response['Location'],
            f"{reverse('dispatcher_control')}?"
            'truck=TRIP-CANCEL-TRUCK&show_pending_assignments=1',
        )
        trip.refresh_from_db()
        acceptance.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertIsNotNone(trip.completed_at)
        self.assertEqual(trip.completed_at, trip.unload_received_at)
        self.assertEqual(trip.unload_time_source, 'server_receipt')
        self.assertEqual(trip.driver, self.driver)
        self.assertEqual(trip.unloading_shift, self.driver_shift)
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)
        self.assertEqual(acceptance.closed_at, trip.completed_at)

        action = DispatcherActionLog.objects.get()
        self.assertEqual(action.actor, self.dispatcher)
        self.assertEqual(action.action_type, DispatcherActionType.COMPLETE_TRIP)
        self.assertEqual(action.trip, trip)
        self.assertEqual(action.reason, 'Подтверждено диспетчером')
        self.assertEqual(
            action.target_summary,
            f'{self.truck} -> {self.dump_point}',
        )

        event = self.complete_events().get(
            object_type='Trip',
            object_id=str(trip.id),
        )
        self.assertEqual(event.key, 'production')
        self.assertEqual(event.reason, 'Trip:dispatcher_complete_trip')
        self.assertEqual(event.event_type, 'trip_changed')
        self.assertEqual(
            event.payload,
            {
                'action': 'dispatcher_complete_trip',
                'trip_id': trip.id,
                'truck_id': self.truck.id,
                'excavator_id': self.excavator.id,
                'assigned_dump_point_id': self.dump_point.id,
                'actual_dump_point_id': self.dump_point.id,
                'status': TripStatus.COMPLETED,
            },
        )
        self.assertIn(
            'завершен служебно',
            ' | '.join(self.messages_text(response)),
        )

    def test_complete_requires_reason_before_any_trip_mutation(self):
        trip = self.create_trip()

        response = self.client.post(
            reverse('dispatcher_complete_trip', args=[trip.id]),
            {'reason': '  '},
        )

        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertIsNone(trip.completed_at)
        self.assertIsNone(trip.driver_id)
        self.assertIsNone(trip.unloading_shift_id)
        self.assertFalse(DispatcherActionLog.objects.exists())
        self.assertFalse(self.complete_events().exists())
        self.assertIn(
            'Укажите причину служебного завершения рейса.',
            self.messages_text(response),
        )

    def test_complete_requires_open_shift_for_trip_truck(self):
        trip = self.create_trip()
        self.driver_shift.delete()

        response = self.post_complete(trip)

        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertIsNone(trip.completed_at)
        self.assertFalse(DispatcherActionLog.objects.exists())
        self.assertFalse(self.complete_events().exists())
        self.assertIn(
            'Нельзя служебно завершить рейс: не найдена открытая смена по этому самосвалу.',
            self.messages_text(response),
        )

    def test_complete_locks_production_state_before_trip_row(self):
        trip = self.create_trip()
        lock_order = []
        select_trip_for_update = Trip.objects.select_for_update

        def record_trip_lock(*args, **kwargs):
            lock_order.append('trip')
            return select_trip_for_update(*args, **kwargs)

        with (
            patch.object(
                dispatcher_trip_commands,
                'lock_production_state',
                side_effect=lambda: lock_order.append('production'),
            ),
            patch.object(
                Trip.objects,
                'select_for_update',
                side_effect=record_trip_lock,
            ),
        ):
            response = self.post_complete(trip)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(lock_order[:2], ['production', 'trip'])

    def test_inactive_role_blocks_service_completion(self):
        trip = self.create_trip()

        with patch.object(
            trips_views,
            'role_session_state',
            return_value={'is_active': False},
        ):
            response = self.post_complete(trip)

        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertIsNone(trip.completed_at)
        self.assertFalse(DispatcherActionLog.objects.exists())
        self.assertFalse(self.complete_events().exists())
        self.assertIn(
            'Роль неактивна — доступен только просмотр.',
            self.messages_text(response),
        )

    def test_closed_dispatcher_shift_blocks_service_completion(self):
        trip = self.create_trip()
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.dispatcher
        self.shift.save(update_fields=['closed_at', 'closed_by'])

        response = self.post_complete(trip)

        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertIsNone(trip.completed_at)
        self.assertFalse(DispatcherActionLog.objects.exists())
        self.assertFalse(self.complete_events().exists())
        self.assertIn(
            'Смена горного диспетчера закрыта. Изменения на пульте недоступны.',
            self.messages_text(response),
        )

    def test_complete_get_preserves_filters_without_mutation(self):
        trip = self.create_trip()

        response = self.client.get(
            reverse('dispatcher_complete_trip', args=[trip.id]),
            {'truck': 'TRIP-CANCEL-TRUCK'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response['Location'],
            f"{reverse('dispatcher_control')}?truck=TRIP-CANCEL-TRUCK",
        )
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertIsNone(trip.completed_at)
        self.assertFalse(DispatcherActionLog.objects.exists())
        self.assertFalse(self.complete_events().exists())

    def test_complete_rejects_terminal_trip_with_original_message(self):
        trip = self.create_trip(status=TripStatus.COMPLETED)

        response = self.post_complete(trip)

        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertIsNone(trip.completed_at)
        self.assertFalse(DispatcherActionLog.objects.exists())
        self.assertFalse(self.complete_events().exists())
        self.assertIn(
            'Активный рейс для служебного завершения не найден.',
            self.messages_text(response),
        )

    def test_manager_role_cannot_service_complete_trip(self):
        trip = self.create_trip()
        manager_role = Role.objects.create(code='manager', name='Руководитель')
        self.access.role = manager_role
        self.access.save(update_fields=['role'])

        response = self.post_complete(trip)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('role_home'))
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertIsNone(trip.completed_at)
        self.assertFalse(DispatcherActionLog.objects.exists())
        self.assertFalse(self.complete_events().exists())
