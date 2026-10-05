from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse

from core import test_offline_driver_autonomous_shift as fixtures
from core.models import OfflineFieldEvent
from core.offline_sync import PROCESSORS
from downtimes.models import DowntimeEvent, DowntimeReason
from shifts.models import EmployeeShift
from trips.models import Trip, TripStatus


class DriverShiftArchiveTests(TestCase):
    fixture = fixtures.DriverAutonomousShiftTests
    create_registered_driver_shift = fixture.create_registered_driver_shift
    driver_event = fixture.driver_event
    sync = fixture.sync
    sync_driver = fixture.sync_driver
    local_event = fixture.local_event
    opening = fixture.opening
    closing = fixture.closing
    manual_load = fixture.manual_load
    manual_complete = fixture.manual_complete

    def setUp(self):
        self.fixture.setUp(self)
        self.archive_url = reverse('driver_shift_archive')
        self.open = self.opening('driver-open', 1, self.base)

    def accepted(self, *events):
        response = self.sync_driver(list(events))
        self.assertEqual(response.status_code, 200)
        results = response.json()['results']
        self.assertEqual([item['status'] for item in results], ['accepted'] * len(events), results)
        return results[-1]

    def closed(self, sequence=2):
        self.accepted(self.open)
        self.accepted(self.closing(self.open, 'driver-close', sequence, self.base + timedelta(minutes=40)))

    def archive(self, **params):
        return self.driver_client.get(self.archive_url, {
            'device_id': 'driver-free-bucket-001', 'close_event_id': 'driver-close', **params,
        })

    def test_complete_originals_and_readings_are_read_only_and_session_scoped(self):
        self.closed()
        before = list(OfflineFieldEvent.objects.values('input_envelope', 'result_payload'))
        response = self.archive()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response['Cache-Control'], 'no-store')
        body = response.json()
        self.assertEqual(body['identity']['actor_id'], self.driver.pk)
        self.assertEqual(body['identity']['role_code'], 'driver')
        self.assertEqual(body['shift']['local_shift_id'], 'driver-open')
        self.assertEqual(Decimal(body['shift']['readings']['end_mileage']), Decimal('12040'))
        self.assertEqual([item['event'] for item in body['entries']], [item['input_envelope']['raw_event'] for item in before])
        self.assertEqual(body['projection']['source_event_ids'], ['driver-open', 'driver-close'])
        self.assertEqual(list(OfflineFieldEvent.objects.values('input_envelope', 'result_payload')), before)
        self.assertEqual(Client().get(self.archive_url).status_code, 403)
        self.assertEqual(self.client.get(self.archive_url).status_code, 403)  # Excavator session.

    def test_server_opened_shift_with_no_local_opening_has_a_complete_archive(self):
        EmployeeShift.objects.filter(pk=self.truck_shift.pk).update(closed_at=None)
        close = self.driver_event('driver-close', 'driver.shift.closed', 1,
                                 payload={'end_fuel': '300', 'end_mileage': '12040', 'end_engine_hours': '3008'})
        self.accepted(close)
        response = self.archive(server_shift_id=self.truck_shift.pk)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['shift']['open_event_id'], '')
        self.assertEqual(response.json()['event_count'], 1)

    def test_numeric_and_local_shift_originals_are_both_in_the_archive(self):
        opened = self.accepted(self.open)
        shift_id = opened['server_ids']['shift_id']
        reason = DowntimeReason.objects.create(name='Архивный простой', equipment_type=self.truck.equipment_type)
        start = self.local_event('stop-start', 'driver.downtime.started', 2, local_shift_id='driver-open',
                                payload={'reason_id': reason.pk}, occurred_at=self.base + timedelta(minutes=5))
        self.accepted(start)
        stop = self.driver_event('stop-end', 'driver.downtime.ended', 3,
                                 payload={'local_downtime_id': 'stop-start'}, occurred_at=self.base + timedelta(minutes=12))
        stop.update(shift_id=shift_id, local_downtime_id='stop-start')
        self.accepted(stop)
        self.accepted(self.closing(self.open, 'driver-close', 4, self.base + timedelta(minutes=40)))
        body = self.archive().json()
        self.assertTrue(body.get('ok'), body)
        self.assertEqual(body['event_count'], 4)
        self.assertEqual(body['projection']['downtime_seconds'], 7 * 60)
        self.assertEqual(len(body['projection']['source_downtime_ids']), 1)
        self.assertEqual(body['entries'][1]['downtime_fact'], body['entries'][2]['downtime_fact'])

    def test_trip_projection_counts_load_and_completion_once_and_keeps_cancellations(self):
        load = self.manual_load(self.open, 'load', 2, self.base + timedelta(minutes=5))
        complete = self.manual_complete(load, 'finish', 3, self.base + timedelta(minutes=12))
        self.accepted(self.open, load, complete, self.closing(self.open, 'driver-close', 4, self.base + timedelta(minutes=40)))
        body = self.archive().json()
        self.assertTrue(body.get('ok'), body)
        self.assertEqual(body['projection']['completed_trip_count'], 1)
        self.assertEqual(body['projection']['credited_trip_count'], 1)
        self.assertEqual(len(body['projection']['source_trip_ids']), 1)
        self.assertEqual(body['entries'][1]['trip_fact'], body['entries'][2]['trip_fact'])
        Trip.objects.update(status=TripStatus.CANCELLED)
        changed = self.archive().json()
        self.assertEqual(changed['projection']['cancelled_trip_count'], 1)
        self.assertEqual(changed['projection']['credited_trip_count'], 0)
        self.assertNotEqual(changed['snapshot_id'], body['snapshot_id'])

    def test_carryover_records_unloading_shift_separately_from_proven_original_credit(self):
        load = self.manual_load(self.open, 'load', 2, self.base + timedelta(minutes=5))
        complete = self.manual_complete(load, 'finish', 3, self.base + timedelta(minutes=12))
        self.accepted(self.open, load, complete, self.closing(self.open, 'driver-close', 4, self.base + timedelta(minutes=40)))
        shift_id = OfflineFieldEvent.objects.get(event_id='driver-open').shift_id
        # The old source shift precedes the current unloading shift; same truck,
        # recorded original driver and load inside D1 are required by the passport.
        Trip.objects.update(driver_control_shift=self.truck_shift, unloading_shift_id=shift_id,
                            loaded_at=self.truck_shift.opened_at + timedelta(minutes=5), is_carryover=True)
        body = self.archive().json()
        self.assertTrue(body.get('ok'), body)
        fact = body['entries'][1]['trip_fact']
        self.assertEqual(fact['credited_shift_id'], self.truck_shift.pk)
        self.assertEqual(fact['unloading_shift_id'], shift_id)
        self.assertEqual(body['projection']['completed_trip_count'], 1)
        self.assertEqual(body['projection']['credited_trip_count'], 0)

    def test_235_sources_have_three_pages_with_no_200_event_or_two_hour_cutoff(self):
        self.accepted(self.open)
        shift = EmployeeShift.objects.get(pk=OfflineFieldEvent.objects.get(event_id='driver-open').shift_id)
        with patch.dict(PROCESSORS, {'driver.assignment.accepted': lambda *args: (
                {'server_ids': {'shift_id': shift.pk}}, {'shift': shift, 'equipment': self.truck})}):
            events = [self.local_event(f'action-{i}', 'driver.assignment.accepted', i + 2,
                                      local_shift_id='driver-open', payload={'assignment_action_id': i + 1},
                                      occurred_at=self.base + timedelta(minutes=10)) for i in range(235)]
            for offset in range(0, len(events), 80):
                self.accepted(*events[offset:offset + 80])
        self.accepted(self.closing(self.open, 'driver-close', 237, self.base + timedelta(minutes=40)))
        first = self.archive().json()
        second = self.archive(offset=100, snapshot_id=first['snapshot_id']).json()
        third = self.archive(offset=200, snapshot_id=first['snapshot_id']).json()
        self.assertEqual([len(page['entries']) for page in [first, second, third]], [100, 100, 37])
        self.assertEqual(len({entry['event']['event_id'] for page in [first, second, third] for entry in page['entries']}), 237)
        receipt = OfflineFieldEvent.objects.get(event_id='action-230')
        receipt.input_envelope['checksum'] = 'bad'
        receipt.save(update_fields=['input_envelope'])
        self.assertEqual(self.archive().json()['code'], 'archive_source_invalid')  # Even on page 1.

    def test_unresolved_original_without_application_fk_blocks_coverage_for_both_aliases(self):
        self.closed()
        shift_id = OfflineFieldEvent.objects.get(event_id='driver-open').shift_id
        for numeric in [False, True]:
            item = self.local_event(f'pending-{numeric}', 'driver.assignment.accepted', 10 + int(numeric),
                                    local_shift_id='driver-open', payload={'assignment_action_id': 1},
                                    occurred_at=self.base + timedelta(minutes=10), depends_on=['missing-parent'])
            if numeric:
                item['shift_id'] = shift_id
                item.pop('local_shift_id')
                item['payload'].pop('local_shift_id')
            result = self.sync_driver([item]).json()['results'][0]
            self.assertEqual(result['status'], 'retry', result)
            self.assertIsNone(OfflineFieldEvent.objects.get(event_id=item['event_id']).shift_id)
            self.assertEqual(self.archive().json()['code'], 'archive_incomplete')
            OfflineFieldEvent.objects.get(event_id=item['event_id']).delete()

    def test_changed_projection_readings_and_receipt_invalidate_snapshot(self):
        self.closed()
        first = self.archive().json()
        EmployeeShift.objects.filter(pk=first['shift']['server_shift_id']).update(end_fuel=Decimal('123'))
        self.assertEqual(self.archive(snapshot_id=first['snapshot_id']).json()['code'], 'archive_snapshot_changed')
        fresh = self.archive().json()
        receipt = OfflineFieldEvent.objects.get(event_id='driver-close')
        receipt.result_payload['version'] = 999
        receipt.save(update_fields=['result_payload'])
        self.assertEqual(self.archive(snapshot_id=fresh['snapshot_id']).json()['code'], 'archive_snapshot_changed')

    def test_missing_domain_projection_and_corrupt_source_never_prove_coverage(self):
        load = self.manual_load(self.open, 'load', 2, self.base + timedelta(minutes=5))
        self.accepted(self.open, load, self.closing(self.open, 'driver-close', 3, self.base + timedelta(minutes=40)))
        receipt = OfflineFieldEvent.objects.get(event_id='load')
        OfflineFieldEvent.objects.filter(pk=receipt.pk).update(trip=None)
        self.assertEqual(self.archive().json()['code'], 'trip_projection_missing')
        OfflineFieldEvent.objects.filter(pk=receipt.pk).update(trip=receipt.trip)
        original = deepcopy(receipt.input_envelope)
        for bad in [{}, [], {**original, 'checksum': 'bad'}]:
            OfflineFieldEvent.objects.filter(pk=receipt.pk).update(input_envelope=bad)
            self.assertEqual(self.archive().json()['code'], 'archive_source_invalid')

    def test_authority_boundaries_parameters_and_revocation(self):
        self.closed()
        for params in [{'device_id': 'foreign-device'}, {'local_shift_id': 'foreign-open'},
                       {'close_event_id': 'foreign-close'}, {'server_shift_id': self.truck_shift.pk}]:
            response = self.archive(**params)
            self.assertEqual(response.status_code, 409)
            self.assertNotIn('entries', response.json())
        for params in [{'device_id': '../bad'}, {'offset': 'bad'}, {'server_shift_id': -1}]:
            self.assertEqual(self.archive(**params).status_code, 400)
        self.assertEqual(self.archive(offset=1).status_code, 409)
        self.assertEqual(self.driver_client.post(self.archive_url).status_code, 405)
        self.driver_access.is_active = False
        self.driver_access.save(update_fields=['is_active'])
        self.assertEqual(self.archive().status_code, 403)
