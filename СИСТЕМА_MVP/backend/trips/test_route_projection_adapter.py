import hashlib
import importlib
import json
from datetime import timedelta
from pathlib import Path

from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from core.models import (
    OfflineFieldEvent,
    OfflineFieldEventConflict,
    OfflineFieldEventStatus,
    OperationalStateEvent,
    OperationalStateVersion,
)
from references.models import DumpPoint, Equipment
from shifts.models import EmployeeShift
from trips import route_projection_adapter, tests as trip_fixtures
from trips.models import DispatcherActionLog, DispatcherActionType, Trip, TripClientAction, TripStatus
from trips.route_projection_adapter import read_trip_route_evidence
from trips.route_projection_core import RouteLedger, TripRouteContext, route_event
from trips.trip_creation import create_loaded_waiting_unload_trip
from users.models import Employee, EmployeeAccess, Role


class RouteProjectionCoreProvenanceTests(SimpleTestCase):
    def test_transferred_r1_core_has_the_accepted_git_blob(self):
        path = Path(__file__).with_name('route_projection_core.py')
        content = path.read_bytes()
        git_blob = hashlib.sha1(
            f'blob {len(content)}\0'.encode('ascii') + content,
        ).hexdigest()
        self.assertEqual(git_blob, '64b2519fb8bb0874012577fd2d779d7b9a619b74')

    def test_transferred_core_keeps_r1_collision_and_incomplete_history_guards(self):
        original = route_event(
            event_id='same-id', trip_id='Y', actor_id='1', actor_role='driver',
            target_point_id='2', loading_event_id='load-Y',
        )
        incoming = route_event(
            event_id='same-id', trip_id='X', actor_id='1', actor_role='driver',
            target_point_id='3', loading_event_id='load-X',
        )
        ledger = RouteLedger()
        self.assertEqual(ledger.append(original), 'stored')
        self.assertEqual(ledger.append(incoming), 'id_conflict')
        projection = ledger.project(TripRouteContext(
            trip_id='X', loading_event_id='load-X', loading_actor_id='2',
            loading_excavator_id='7', original_point_id='1', history_complete=False,
        ))
        self.assertEqual(projection.status, 'integrity_conflict')
        self.assertIsNone(projection.notification_key)


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class RouteProjectionAdapterTests(TestCase):
    create_registered_driver_shift = (
        trip_fixtures.ExcavatorWorkServerIntegrationTests.create_registered_driver_shift
    )

    def setUp(self):
        trip_fixtures.ExcavatorWorkServerIntegrationTests.setUp(self)
        self.shift = EmployeeShift.objects.get(employee=self.operator, closed_at__isnull=True)
        self.assignment = HaulAssignment.objects.get(
            truck=self.truck,
            excavator=self.excavator,
            status=AssignmentStatus.ACCEPTED,
        )
        self.other_dump = DumpPoint.objects.create(name='Склад P28-I2')

    def factory_trip(self, *, truck=None, excavator=None, assignment=None, loaded_at=None):
        truck = truck or self.truck
        excavator = excavator or self.excavator
        assignment = self.assignment if assignment is None and truck == self.truck else assignment
        return create_loaded_waiting_unload_trip(
            assignment=assignment,
            truck=None if assignment else truck,
            excavator=None if assignment else excavator,
            free_bucket_acceptance=None,
            excavator_operator=self.operator,
            loading_shift=self.shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            occurred_at=loaded_at or timezone.now() - timedelta(minutes=2),
            driver=self.driver,
            participation={'shift': self.truck_shift, 'control_shift': self.truck_shift},
        )

    def legacy_trip(self, *, truck=None, status=TripStatus.LOADED_WAITING_UNLOAD,
                    loaded_at=None, dump_point=None, actual_dump_point=None,
                    excavator=None):
        return Trip.objects.create(
            excavator=excavator or self.excavator,
            truck=truck or self.truck,
            excavator_operator=self.operator,
            driver=self.driver,
            loading_shift=self.shift,
            driver_control_shift=self.truck_shift if (truck or self.truck) == self.truck else None,
            rock_type=self.rock,
            dump_point=dump_point or self.dump_point,
            assigned_dump_point=dump_point or self.dump_point,
            actual_dump_point=actual_dump_point,
            status=status,
            loaded_at=loaded_at or timezone.now() - timedelta(minutes=2),
        )

    def driver_client(self):
        client = Client()
        session = client.session
        session['employee_access_id'] = self.driver_access.pk
        session.save()
        return client

    def driver_event(self, trip, *, event_id, sequence, point=None, event_type='driver.trip.dump_point_changed',
                     occurred_at=None, depends_on=()):
        payload = {'trip_id': trip.pk}
        if event_type == 'driver.trip.dump_point_changed':
            payload.update({
                'dump_point_id': (point or self.other_dump).pk,
                'expected_actual_dump_point_id': trip.actual_dump_point_id or trip.dump_point_id,
            })
        return {
            'event_id': event_id,
            'event_type': event_type,
            'format_version': 1,
            'actor_id': self.driver.pk,
            'access_id': self.driver_access.pk,
            'role_code': 'driver',
            'occurred_at': (occurred_at or timezone.now()).isoformat(),
            'sequence': sequence,
            'depends_on': list(depends_on),
            'shift_id': self.truck_shift.pk,
            'equipment_id': self.truck.pk,
            'trip_id': trip.pk,
            'context_snapshot': {
                'actor_id': self.driver.pk,
                'access_id': self.driver_access.pk,
                'role_code': 'driver',
            },
            'payload': payload,
        }

    def sync_driver(self, events, *, device_id='p28-i2-driver'):
        return self.driver_client().post(
            reverse('offline_events_sync'),
            data=json.dumps({
                'protocol_version': 1,
                'actor_id': self.driver.pk,
                'access_id': self.driver_access.pk,
                'role_code': 'driver',
                'device_id': device_id,
                'events': events,
            }),
            content_type='application/json',
        )

    def manual_receipt(self, trip, *, event_id, sequence, status, code='', result=None,
                       point=None, device='p28-i2-manual'):
        return OfflineFieldEvent.objects.create(
            event_id=event_id,
            event_type='driver.trip.dump_point_changed',
            format_version=1,
            actor=self.driver,
            access=self.driver_access,
            role_code='driver',
            device_id=device,
            sequence=sequence,
            depends_on=['legacy-queue-parent'],
            occurred_at=timezone.now(),
            received_at=timezone.now(),
            shift=self.truck_shift,
            equipment=self.truck,
            trip=trip,
            payload={'trip_id': trip.pk, 'dump_point_id': (point or self.other_dump).pk},
            fingerprint=f'fingerprint-{event_id}',
            status=status,
            retryable=status == OfflineFieldEventStatus.RETRY,
            error_code=code,
            error_message=code,
            result_payload=result or {'server_ids': {'trip_id': trip.pk}},
        )

    def test_factory_load_preserves_p1_and_author_without_inventing_driver_route_event(self):
        trip = self.factory_trip()

        evidence = read_trip_route_evidence(trip.pk)

        self.assertEqual(evidence.trip_snapshot['assigned_dump_point_id'], self.dump_point.pk)
        self.assertEqual(evidence.author_context['trip_excavator_operator_id'], self.operator.pk)
        self.assertEqual(evidence.loading_reference['source_kind'], 'trip_snapshot')
        self.assertTrue(evidence.loading_reference['synthetic'])
        self.assertEqual(evidence.normalized_route_inputs, ())
        self.assertFalse(evidence.history_complete)
        self.assertIn('loading_event_not_recorded', evidence.incomplete_reasons)
        self.assertEqual(evidence.projection.status, 'causality_incomplete')
        self.assertIsNone(evidence.projection.notification_key)

    def test_real_offline_point_change_exposes_identity_actor_and_both_times_without_promoting_depends_on(self):
        trip = self.factory_trip()
        first_event = self.driver_event(
            trip,
            event_id='p28-change-real',
            sequence=1,
            point=self.other_dump,
        )
        response = self.sync_driver([first_event])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results'][0]['status'], 'accepted')
        trip.refresh_from_db()
        second_event = self.driver_event(
            trip,
            event_id='p28-change-dependent',
            sequence=2,
            point=self.dump_point,
            occurred_at=timezone.now() + timedelta(seconds=1),
            depends_on=(first_event['event_id'],),
        )
        second_response = self.sync_driver([second_event])
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(second_response.json()['results'][0]['status'], 'accepted')

        evidence = read_trip_route_evidence(trip.pk)
        route_input = next(
            item for item in evidence.normalized_route_inputs
            if item['event']['event_id'] == second_event['event_id']
        )
        source = next(
            item for item in evidence.sources
            if item['source_kind'] == 'offline_field_event'
            and item['event_id'] == second_event['event_id']
        )
        self.assertEqual(route_input['event']['event_id'], second_event['event_id'])
        self.assertEqual(route_input['event']['target_point_id'], str(self.dump_point.pk))
        self.assertEqual(route_input['event']['actor_id'], str(self.driver.pk))
        self.assertEqual(route_input['event']['observed_ancestor_ids'], [])
        self.assertFalse(route_input['observed_ancestors_proven'])
        self.assertEqual(
            route_input['depends_on_preserved_as_queue_metadata'],
            (first_event['event_id'],),
        )
        self.assertEqual(source['depends_on'], (first_event['event_id'],))
        self.assertEqual(source['occurred_at'], second_event['occurred_at'])
        self.assertIsNotNone(source['received_at'])
        self.assertEqual(source['effect_disposition'], 'accepted_effect_recorded')
        self.assertEqual(evidence.projection.status, 'causality_incomplete')

    def test_no_change_retry_and_conflict_are_evidence_but_not_applied_route_inputs(self):
        trip = self.factory_trip()
        no_change = self.driver_event(
            trip,
            event_id='p28-no-change',
            sequence=1,
            point=self.dump_point,
        )
        response = self.sync_driver([no_change], device_id='p28-no-change-device')
        self.assertEqual(response.json()['results'][0]['status'], 'accepted')
        self.assertTrue(response.json()['results'][0]['no_change'])
        self.manual_receipt(
            trip,
            event_id='p28-retry',
            sequence=2,
            status=OfflineFieldEventStatus.RETRY,
            code='trip_reference_pending',
            device='p28-retry-device',
        )
        self.manual_receipt(
            trip,
            event_id='p28-conflict',
            sequence=3,
            status=OfflineFieldEventStatus.CONFLICT,
            code='stale_dump_point_change',
            device='p28-conflict-device',
        )

        evidence = read_trip_route_evidence(trip.pk)
        dispositions = {
            item['event_id']: item['effect_disposition']
            for item in evidence.sources if item['source_kind'] == 'offline_field_event'
        }
        self.assertEqual(dispositions['p28-no-change'], 'accepted_no_change')
        self.assertEqual(dispositions['p28-retry'], 'retry')
        self.assertEqual(dispositions['p28-conflict'], 'conflict')
        self.assertEqual(evidence.normalized_route_inputs, ())

    def test_incompatible_event_id_is_found_for_original_and_submitted_trips_without_touching_third(self):
        trip_y = self.factory_trip()
        trip_x = self.legacy_trip(truck=self.other_truck, status=TripStatus.UNCONTROLLED)
        third_truck = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number='P28-Z',
        )
        trip_z = self.legacy_trip(truck=third_truck, status=TripStatus.UNCONTROLLED)
        original = self.driver_event(
            trip_y,
            event_id='p28-shared-id',
            sequence=1,
            point=self.other_dump,
        )
        first = self.sync_driver([original], device_id='p28-collision-device').json()['results'][0]
        self.assertEqual(first['status'], 'accepted')
        incoming = self.driver_event(
            trip_x,
            event_id='p28-shared-id',
            sequence=2,
            point=self.dump_point,
        )
        second = self.sync_driver([incoming], device_id='p28-collision-device').json()['results'][0]
        self.assertEqual(second['status'], 'conflict')
        self.assertEqual(second['code'], 'event_id_reused')
        repeated = self.sync_driver([incoming], device_id='p28-collision-device').json()['results'][0]
        self.assertEqual(repeated['status'], 'conflict')
        self.assertEqual(repeated['code'], 'event_id_reused')

        x_evidence = read_trip_route_evidence(trip_x.pk)
        y_evidence = read_trip_route_evidence(trip_y.pk)
        z_before = _trip_values(trip_z.pk)
        z_evidence = read_trip_route_evidence(trip_z.pk)
        z_after = _trip_values(trip_z.pk)

        self.assertEqual(x_evidence.projection.status, 'integrity_conflict')
        self.assertEqual(y_evidence.projection.status, 'integrity_conflict')
        self.assertEqual(
            tuple(item for item in x_evidence.projection.diagnostics if item == 'id_collision:p28-shared-id'),
            ('id_collision:p28-shared-id',),
        )
        self.assertTrue(any(
            item['source_kind'] == 'offline_field_event_conflict'
            and trip_x.pk in item['submitted_trip_ids']
            for item in x_evidence.sources
        ))
        self.assertTrue(any(
            item['source_kind'] == 'offline_field_event_conflict'
            and item['existing_trip_id'] == trip_y.pk
            for item in y_evidence.sources
        ))
        self.assertFalse(any(
            item['source_kind'] == 'offline_field_event_conflict'
            for item in z_evidence.sources
        ))
        self.assertEqual(z_before, z_after)

    def test_direct_legacy_action_keeps_actual_value_but_reports_missing_payload(self):
        trip = self.legacy_trip(actual_dump_point=self.other_dump)
        TripClientAction.objects.create(
            action_type='change_actual_unload_point',
            client_action_id='legacy-direct-point',
            trip=trip,
            actor=self.driver,
        )

        evidence = read_trip_route_evidence(trip.pk)

        self.assertEqual(evidence.trip_snapshot['actual_dump_point_id'], self.other_dump.pk)
        self.assertEqual(evidence.normalized_route_inputs, ())
        self.assertIn('legacy_route_action_payload_missing', evidence.incomplete_reasons)
        self.assertEqual(
            evidence.projection.to_dict()['history'],
            (),
        )

    def test_real_report_correction_keeps_administrator_separate_from_driver(self):
        trip = self.legacy_trip(status=TripStatus.COMPLETED)
        Trip.objects.filter(pk=trip.pk).update(
            completed_at=timezone.now(),
            volume_m3='49.40',
            tonnage='127.45',
        )
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер P28')
        dispatcher = Employee.objects.create(
            full_name='Диспетчер коррекции P28', status=Employee.Status.ACTIVE, is_active=True,
        )
        dispatcher_access = EmployeeAccess.objects.create(
            employee=dispatcher,
            role=dispatcher_role,
            access_code='928001',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        EmployeeShift.objects.create(
            employee=dispatcher,
            shift_type='day',
            opened_at=timezone.now() - timedelta(hours=1),
            opened_by=dispatcher,
        )
        client = Client()
        session = client.session
        session['employee_access_id'] = dispatcher_access.pk
        session.save()
        url = reverse('dispatcher_shift_trucks')
        response = client.post(
            f'{url}?date={timezone.localdate().isoformat()}&shift_type=day',
            {
                'trip_id': trip.pk,
                'correction_reason': 'Адресная проверка P28-I2',
                'volume_m3': '49.40',
                'transport_distance_km': '1.25',
                'loading_horizon': '125',
                'loading_block': '4',
                'downtime_text': '',
                'note': 'P28-I2',
                'rock_type_id': self.rock.pk,
                'actual_dump_point_id': self.other_dump.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(DispatcherActionLog.objects.filter(
            trip=trip,
            actor=dispatcher,
            action_type='report_source_correction',
        ).exists())

        evidence = read_trip_route_evidence(trip.pk)

        self.assertEqual(evidence.author_context['trip_driver_id'], self.driver.pk)
        self.assertEqual(evidence.author_context['administrative_correction_actor_ids'], (dispatcher.pk,))
        self.assertNotEqual(dispatcher.pk, self.driver.pk)
        self.assertEqual(evidence.normalized_route_inputs, ())
        self.assertIn('administrative_correction_before_after_missing', evidence.incomplete_reasons)

    def test_uncontrolled_lifecycle_separates_technical_positive_and_possible_replacement(self):
        technical_truck = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number='P28-T',
        )
        technical = self.legacy_trip(truck=technical_truck, status=TripStatus.UNCONTROLLED)
        positive = self.legacy_trip(status=TripStatus.UNCONTROLLED)
        successor = self.legacy_trip(
            status=TripStatus.LOADED_WAITING_UNLOAD,
            loaded_at=positive.loaded_at + timedelta(minutes=6),
        )
        Trip.objects.filter(pk=positive.pk).update(
            superseded_by=successor,
            operationally_closed_at=successor.loaded_at,
        )
        possible_truck = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number='P28-U',
        )
        possible = self.legacy_trip(
            truck=possible_truck,
            status=TripStatus.UNCONTROLLED,
            loaded_at=timezone.now() - timedelta(minutes=20),
        )
        possible_later = self.legacy_trip(
            truck=possible_truck,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            loaded_at=timezone.now() - timedelta(minutes=5),
        )

        technical_evidence = read_trip_route_evidence(technical.pk)
        positive_evidence = read_trip_route_evidence(positive.pk)
        possible_evidence = read_trip_route_evidence(possible.pk)

        self.assertEqual(technical_evidence.projection.lifecycle_state, 'technical_uncontrolled')
        self.assertEqual(positive_evidence.projection.lifecycle_state, 'superseded')
        self.assertEqual(positive_evidence.projection.successor_trip_id, str(successor.pk))
        self.assertEqual(possible_evidence.projection.lifecycle_state, 'replacement_unknown')
        self.assertIsNone(possible_evidence.projection.successor_trip_id)
        self.assertIn(possible_later.pk, possible_evidence.lifecycle_detail['possible_successor_trip_ids'])
        self.assertIn('replacement_link_missing', possible_evidence.incomplete_reasons)

    def test_cancelled_driver_unload_and_service_completion_have_distinct_terminal_bases(self):
        cancelled = self.legacy_trip(status=TripStatus.CANCELLED)
        unloaded = self.factory_trip()
        unload_event = self.driver_event(
            unloaded,
            event_id='p28-unload-confirmed',
            sequence=1,
            event_type='driver.trip.unloaded',
            occurred_at=unloaded.loaded_at + timedelta(minutes=1),
        )
        unload_result = self.sync_driver(
            [unload_event], device_id='p28-unload-device',
        ).json()['results'][0]
        self.assertEqual(unload_result['status'], 'accepted')
        service_truck = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number='P28-S',
        )
        service = self.legacy_trip(truck=service_truck, status=TripStatus.COMPLETED)
        DispatcherActionLog.objects.create(
            actor=self.operator,
            action_type=DispatcherActionType.COMPLETE_TRIP,
            trip=service,
            shift=self.shift,
            target_summary='Служебное завершение P28-I2',
            reason='Тест источника',
        )

        cancelled_evidence = read_trip_route_evidence(cancelled.pk)
        unloaded_evidence = read_trip_route_evidence(unloaded.pk)
        service_evidence = read_trip_route_evidence(service.pk)

        self.assertEqual(cancelled_evidence.lifecycle_detail['basis'], 'trip_cancelled_state')
        self.assertEqual(unloaded_evidence.lifecycle_detail['basis'], 'driver_unload_confirmation_recorded')
        self.assertEqual(unloaded_evidence.projection.lifecycle_state, 'unloaded')
        self.assertEqual(service_evidence.lifecycle_detail['basis'], 'service_completion_without_driver_unload_confirmation')
        self.assertEqual(service_evidence.projection.lifecycle_state, 'completed_service')
        for evidence in (cancelled_evidence, unloaded_evidence, service_evidence):
            self.assertFalse(evidence.projection.operational_allowed)
            self.assertIsNone(evidence.projection.notification_key)

    def test_shift_handover_and_foreign_excavator_contexts_remain_separate(self):
        trip = self.legacy_trip(excavator=self.other_excavator)
        self.truck_shift.closed_at = timezone.now() - timedelta(minutes=5)
        self.truck_shift.save(update_fields=['closed_at'])
        replacement, _, replacement_shift = self.create_registered_driver_shift(
            self.truck,
            full_name='Сменщик P28-I2',
            access_code='928002',
        )
        Trip.objects.filter(pk=trip.pk).update(
            status=TripStatus.COMPLETED,
            unloading_shift=replacement_shift,
            completed_at=timezone.now(),
        )

        evidence = read_trip_route_evidence(trip.pk)

        self.assertEqual(evidence.trip_snapshot['excavator_id'], self.other_excavator.pk)
        self.assertEqual(evidence.author_context['trip_excavator_operator_id'], self.operator.pk)
        self.assertEqual(evidence.author_context['trip_driver_id'], self.driver.pk)
        self.assertEqual(evidence.author_context['driver_control_employee_id'], self.driver.pk)
        self.assertEqual(evidence.author_context['unloading_shift_employee_id'], replacement.pk)
        self.assertIn('completed_without_driver_unload_confirmation', evidence.incomplete_reasons)

    def test_repeated_reads_are_stable_and_execute_no_dml_or_operational_side_effects(self):
        trip = self.factory_trip()
        before = {
            'trip': _trip_values(trip.pk),
            'receipts': OfflineFieldEvent.objects.count(),
            'conflicts': OfflineFieldEventConflict.objects.count(),
            'actions': TripClientAction.objects.count(),
            'logs': DispatcherActionLog.objects.count(),
            'versions': OperationalStateVersion.objects.count(),
            'events': OperationalStateEvent.objects.count(),
        }
        with CaptureQueriesContext(connection) as queries:
            first = read_trip_route_evidence(trip.pk).to_dict()
            second = read_trip_route_evidence(trip.pk).to_dict()
        after = {
            'trip': _trip_values(trip.pk),
            'receipts': OfflineFieldEvent.objects.count(),
            'conflicts': OfflineFieldEventConflict.objects.count(),
            'actions': TripClientAction.objects.count(),
            'logs': DispatcherActionLog.objects.count(),
            'versions': OperationalStateVersion.objects.count(),
            'events': OperationalStateEvent.objects.count(),
        }
        mutating = [
            query['sql'] for query in queries.captured_queries
            if query['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE'))
        ]
        self.assertEqual(first, second)
        self.assertEqual(before, after)
        self.assertEqual(mutating, [])
        self.assertEqual(
            first['read_consistency'],
            'multiple_selects_database_default_isolation_no_snapshot_claim',
        )

    def test_adapter_import_has_no_database_queries_or_registration_side_effects(self):
        before = (
            Trip.objects.count(),
            OfflineFieldEvent.objects.count(),
            OperationalStateVersion.objects.count(),
        )
        with CaptureQueriesContext(connection) as queries:
            importlib.reload(route_projection_adapter)
        after = (
            Trip.objects.count(),
            OfflineFieldEvent.objects.count(),
            OperationalStateVersion.objects.count(),
        )
        self.assertEqual(queries.captured_queries, [])
        self.assertEqual(before, after)


def _trip_values(trip_id):
    return Trip.objects.filter(pk=trip_id).values(
        'status',
        'dump_point_id',
        'assigned_dump_point_id',
        'actual_dump_point_id',
        'completed_at',
        'cancelled_at',
        'operationally_closed_at',
        'superseded_by_id',
    ).get()
