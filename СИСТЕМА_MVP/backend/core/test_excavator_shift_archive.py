from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core import test_excavator_offline_work_context as fixtures
from core.models import OfflineFieldEvent
from core.offline_sync import PROCESSORS
from shifts.models import EmployeeShift
from trips.models import Trip, TripStatus


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class ExcavatorShiftArchiveTests(TestCase):
    base_fixture = fixtures.ExcavatorOfflineWorkContextTests.base_fixture
    create_registered_driver_shift = base_fixture.create_registered_driver_shift
    event = base_fixture.event
    sync = base_fixture.sync
    local = base_fixture.local
    opening = base_fixture.opening
    closing = base_fixture.closing
    result = base_fixture.result
    context = fixtures.ExcavatorOfflineWorkContextTests.context

    def setUp(self):
        fixtures.ExcavatorOfflineWorkContextTests.setUp(self)
        self.archive_url = reverse('excavator_shift_archive')

    def archive(self, **params):
        query = {'device_id': 'free-bucket-device-001', 'local_shift_id': 'eo-open', 'close_event_id': 'eo-close'}
        query.update(params)
        return self.client.get(self.archive_url, query)

    def accepted(self, event):
        result = self.result(event)
        self.assertEqual(result['status'], 'accepted', result)
        return result

    def closed(self):
        self.accepted(self.opening())
        self.accepted(self.closing())

    def load(self, ident='load', sequence=2):
        event = self.local(ident, 'excavator.trip.loaded', sequence, minutes=10,
                           payload={'truck_id': self.truck.pk, 'dump_point_id': self.dump_point.pk,
                                    'assignment_id': self.assignment.pk, 'manual_control': True,
                                    'rock_type_id': self.rock.pk, 'loading_horizon': '125', 'loading_block': '4'})
        event['local_trip_id'] = ident
        return event

    def test_complete_read_only_snapshot_has_originals_identity_and_closed_shift(self):
        self.closed()
        originals = list(OfflineFieldEvent.objects.order_by('sequence').values('input_envelope', 'result_payload'))
        response = self.archive()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(body['identity']['actor_id'], self.operator.pk)
        self.assertEqual(body['event_count'], 2)
        self.assertIsNone(body['next_offset'])
        self.assertEqual([item['event'] for item in body['entries']], [item['input_envelope']['raw_event'] for item in originals])
        self.assertEqual(body['projection']['trip_count'], 0)
        self.assertEqual(body['shift']['closed_at'], self.closing()['occurred_at'])
        self.assertEqual(list(OfflineFieldEvent.objects.order_by('sequence').values('input_envelope', 'result_payload')), originals)

    def test_235_old_actions_are_paginated_without_hour_window_or_event_cap_loss(self):
        self.accepted(self.opening())
        events = [self.context(f'face-{i}', i + 2, minutes=1 + i / 10) for i in range(235)]
        for offset in range(0, len(events), 80):
            for result in self.sync(events[offset:offset + 80]).json()['results']:
                self.assertEqual(result['status'], 'accepted', result)
        self.accepted(self.closing(sequence=237))
        first = self.archive().json()
        self.assertEqual((len(first['entries']), first['next_offset'], first['event_count']), (100, 100, 237))
        second = self.archive(offset=100, snapshot_id=first['snapshot_id']).json()
        third = self.archive(offset=200, snapshot_id=first['snapshot_id']).json()
        self.assertEqual((len(third['entries']), third['next_offset']), (37, None))
        ids = [entry['event']['event_id'] for page in [first, second, third] for entry in page['entries']]
        self.assertEqual(len(set(ids)), 237)
        self.assertEqual(set(ids), {'eo-open', 'eo-close', *[event['event_id'] for event in events]})
        self.assertEqual({first['snapshot_id'], second['snapshot_id'], third['snapshot_id']}, {first['snapshot_id']})

    def test_open_or_unreceived_closing_is_not_archive_coverage(self):
        self.accepted(self.opening())
        self.assertEqual(self.archive().json()['code'], 'closed_shift_required')
        self.accepted(self.closing())
        self.assertEqual(self.archive(close_event_id='missing').json()['code'], 'closing_not_covered')

    def test_accepted_close_does_not_hide_an_unresolved_original(self):
        self.closed()
        pending = self.context('missing-parent-child', 3)
        pending['depends_on'] = ['not-yet-received']
        self.assertEqual(self.result(pending)['status'], 'retry')
        self.assertEqual(self.archive().json()['code'], 'archive_incomplete')

    def test_corrupt_or_legacy_receipt_cannot_prove_original_coverage(self):
        self.closed()
        receipt = OfflineFieldEvent.objects.get(event_id='eo-open')
        original = deepcopy(receipt.input_envelope)
        for value in [{}, {**original, 'checksum': 'bad'}, None]:
            # JSON null itself is rejected by the field, so exercise corrupt
            # non-object legacy data with an array instead.
            receipt.input_envelope = [] if value is None else value
            receipt.save(update_fields=['input_envelope'])
            response = self.archive()
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()['code'], 'archive_source_invalid')

    def test_snapshot_token_changes_when_projection_or_shift_changes(self):
        self.closed()
        old = self.archive().json()['snapshot_id']
        shift_id = OfflineFieldEvent.objects.get(event_id='eo-open').shift_id
        EmployeeShift.objects.filter(pk=shift_id).update(end_fuel=Decimal('89'))
        self.assertEqual(self.archive(snapshot_id=old).json()['code'], 'archive_snapshot_changed')
        self.assertNotEqual(self.archive().json()['snapshot_id'], old)

    def test_other_device_or_shift_cannot_read_archive_and_revoked_access_stops_reading(self):
        self.closed()
        for params in [{'device_id': 'foreign-device-001'}, {'local_shift_id': 'foreign-open'}]:
            response = self.archive(**params)
            self.assertEqual(response.status_code, 409)
            self.assertNotIn('entries', response.json())
        self.assertEqual(Client().get(self.archive_url).status_code, 403)
        self.access.is_active = False
        self.access.save(update_fields=['is_active'])
        self.assertEqual(self.archive().status_code, 403)

    def test_request_limits_and_method_do_not_modify_data(self):
        self.closed()
        for params, status in [({'offset': 'bad'}, 400), ({'device_id': '../private'}, 400),
                               ({'offset': -1}, 409), ({'offset': 1}, 409), ({'offset': 100}, 409)]:
            self.assertEqual(self.archive(**params).status_code, status)
        self.assertEqual(self.client.post(self.archive_url).status_code, 405)
        self.assertEqual(OfflineFieldEvent.objects.count(), 2)

    def test_domain_projection_covers_loaded_cancelled_and_duplicate_action_ids_once(self):
        self.accepted(self.opening())
        first = self.accepted(self.load())
        trip = Trip.objects.get(pk=first['server_ids']['trip_id'])
        trip.volume_m3 = Decimal('42.50')
        trip.save(update_fields=['volume_m3'])
        shift = EmployeeShift.objects.get(pk=OfflineFieldEvent.objects.get(event_id='eo-open').shift_id)
        with patch.dict(PROCESSORS, {'excavator.trip.loaded': lambda *args: (
            {'server_ids': {'trip_id': trip.pk, 'shift_id': shift.pk}},
            {'trip': trip, 'shift': shift, 'equipment': self.excavator},
        )}):
            self.accepted(self.load('alias', 3))
        self.accepted(self.closing(sequence=4))
        body = self.archive().json()
        self.assertTrue(body.get('ok'), body)
        self.assertEqual(body['projection']['source_event_ids'], ['load', 'alias'])
        self.assertEqual(body['projection']['trip_count'], 1)
        self.assertEqual(body['projection']['volume_m3'], '42.50')
        Trip.objects.filter(pk=trip.pk).update(status=TripStatus.CANCELLED)
        changed = self.archive().json()
        self.assertEqual(changed['projection']['trip_count'], 0)
        self.assertEqual(changed['projection']['cancelled_trip_count'], 1)
        self.assertNotEqual(changed['snapshot_id'], body['snapshot_id'])
        self.assertEqual(len(changed['projection']['source_event_ids']), 2)

    def test_receipt_without_its_domain_loading_cannot_cover_report(self):
        self.accepted(self.opening())
        self.accepted(self.load())
        self.accepted(self.closing(sequence=3))
        OfflineFieldEvent.objects.filter(event_id='load').update(trip=None)
        self.assertEqual(self.archive().json()['code'], 'loading_projection_missing')
