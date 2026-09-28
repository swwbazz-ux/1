"""Independent acceptance probe, no edits to candidate code.

Input preparation uses the candidate's fixture helpers. The duplicate is sent
through the real normalize/process_offline_batch -> excavator loader path;
neither receipts nor adapter results are mocked or manufactured.
"""

import json
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from assignments.models import HaulAssignment
from core.models import OfflineFieldEvent, OfflineFieldEventStatus
from core.offline_sync import process_offline_batch
from shifts.models import EmployeeShift
from trips.models import Trip, TripClientAction, TripStatus
from trips.route_projection_adapter import read_trip_route_evidence
from trips import test_route_projection_adapter as fixture_module
from trips.trip_creation import create_loaded_waiting_unload_trip
from users.models import Employee, EmployeeAccess


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class LateLoadOriginProbe(TestCase):
    create_registered_driver_shift = fixture_module.RouteProjectionAdapterTests.create_registered_driver_shift
    setUp = fixture_module.RouteProjectionAdapterTests.setUp
    factory_trip = fixture_module.RouteProjectionAdapterTests.factory_trip

    def test_accepted_late_duplicate_is_not_the_original_loading_source(self):
        now = timezone.now()
        origin_time = now - timedelta(minutes=20)
        takeover_time = now - timedelta(minutes=19)
        duplicate_time = now - timedelta(minutes=18)
        next_time = now - timedelta(minutes=2)
        # Explicit historical fixture boundary; no live/persistent data.
        EmployeeShift.objects.filter(pk__in=(self.shift.pk, self.truck_shift.pk)).update(
            opened_at=now - timedelta(hours=1),
        )
        self.shift.refresh_from_db()
        self.truck_shift.refresh_from_db()
        HaulAssignment.objects.filter(pk=self.assignment.pk).update(
            assigned_at=now - timedelta(hours=1),
            accepted_at=now - timedelta(hours=1),
        )
        self.assignment.refresh_from_db()
        original = self.factory_trip(loaded_at=origin_time)
        original_actor_id = self.operator.pk
        Trip.objects.filter(pk=original.pk).update(
            status=TripStatus.UNCONTROLLED,
            operationally_closed_at=now - timedelta(minutes=10),
        )
        EmployeeShift.objects.filter(pk=self.shift.pk).update(closed_at=takeover_time)
        duplicate_actor = Employee.objects.create(
            full_name='P28 independent late duplicate operator',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        duplicate_access = EmployeeAccess.objects.create(
            employee=duplicate_actor,
            role=self.role,
            access_code='929991',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        duplicate_shift = EmployeeShift.objects.create(
            employee=duplicate_actor,
            workplace_code='excavator_operator',
            equipment=self.excavator,
            shift_type='day',
            opened_at=takeover_time,
            opened_by=duplicate_actor,
        )
        successor = create_loaded_waiting_unload_trip(
            assignment=self.assignment,
            excavator_operator=duplicate_actor,
            loading_shift=duplicate_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            occurred_at=next_time,
            driver=self.driver,
            participation={'shift': self.truck_shift, 'control_shift': self.truck_shift},
        )
        event_id = 'astra-late-load-not-origin'
        raw = {
            'event_id': event_id,
            'event_type': 'excavator.trip.loaded',
            'format_version': 1,
            'actor_id': duplicate_actor.pk,
            'access_id': duplicate_access.pk,
            'role_code': 'excavator_operator',
            'sequence': 1,
            'depends_on': [],
            'occurred_at': duplicate_time.isoformat(),
            'shift_id': duplicate_shift.pk,
            'equipment_id': self.excavator.pk,
            'local_trip_id': event_id,
            'payload': {
                'truck_id': self.truck.pk,
                'assignment_id': self.assignment.pk,
                'dump_point_id': self.dump_point.pk,
                'rock_type_id': self.rock.pk,
                'manual_control': False,
            },
        }
        results = process_offline_batch(
            duplicate_access,
            role_code='excavator_operator',
            device_id='astra-late-source-device',
            events=[raw],
        )
        self.assertEqual(results[0]['status'], 'accepted', results)
        receipt = OfflineFieldEvent.objects.get(event_id=event_id)
        self.assertEqual(receipt.status, OfflineFieldEventStatus.ACCEPTED)
        self.assertEqual(receipt.trip_id, original.pk)
        self.assertFalse(receipt.result_payload.get('no_effect'))
        self.assertFalse(TripClientAction.objects.filter(client_action_id=event_id).exists())
        self.assertEqual(Trip.objects.count(), 2)
        original.refresh_from_db()
        successor.refresh_from_db()
        self.assertEqual(original.excavator_operator_id, original_actor_id)
        self.assertEqual(original.loaded_at, origin_time)
        self.assertEqual(successor.status, TripStatus.LOADED_WAITING_UNLOAD)

        evidence = read_trip_route_evidence(original.pk)
        observed = {
            'synthetic': evidence.loading_reference['synthetic'],
            'loading_actor_id': evidence.author_context['loading_actor_id'],
            'loading_reference': evidence.loading_reference['value'],
        }
        print('TRACE_LATE_LOAD_ORIGIN ' + json.dumps({
            'path': 'real process_offline_batch -> _process_excavator_loaded -> _process_late_excavator_load same_load',
            'result': results[0],
            'original_trip_id': original.pk,
            'original_actor_id': original_actor_id,
            'late_duplicate_actor_id': duplicate_actor.pk,
            'load_action_count_for_duplicate': TripClientAction.objects.filter(client_action_id=event_id).count(),
            'observed': observed,
            'incomplete_reasons': evidence.incomplete_reasons,
        }, ensure_ascii=False, default=str))
        # A receipt of a duplicate is a genuine receipt, but is not proof that
        # this event/actor created the original load. With no original load
        # event recorded, use the explicitly synthetic snapshot provenance.
        self.assertEqual(observed, {
            'synthetic': True,
            'loading_actor_id': str(original_actor_id),
            'loading_reference': f'source:trip:{original.pk}:loading_snapshot',
        })
