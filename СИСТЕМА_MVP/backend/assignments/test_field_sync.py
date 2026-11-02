import json
from datetime import timedelta
from uuid import uuid4
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import OfflineFieldEvent, OfflineFieldEventConflict, OperationalStateVersion
from references.models import Equipment, EquipmentType
from shifts.models import EmployeeShift
from users.active_role import activate_role_session
from users.models import AdminActionLog, Employee, EmployeeAccess, Role

from .field_sync import EVENT_PREFIX, _actor, _alias
from .models import AssignmentStatus, ExcavatorPlacement, HaulAssignment
from .services import HaulAssignmentStateConflict, schedule_haul_assignment


class MasterFieldSyncTests(TestCase):
    def setUp(self):
        self.role, _ = Role.objects.get_or_create(code='mining_master', defaults={'name': 'Горный мастер'})
        self.employee = Employee.objects.create(full_name='Мастер А', phone='+79991234567', status=Employee.Status.ACTIVE)
        self.access = EmployeeAccess.objects.create(employee=self.employee, role=self.role, access_code='246824', status=EmployeeAccess.Status.ACTIVATED)
        self.device = str(uuid4())
        self.url = reverse('mining_master_field_sync')
        self.login(self.access)
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck = Equipment.objects.create(equipment_type=truck_type, garage_number='11')
        self.other_truck = Equipment.objects.create(equipment_type=truck_type, garage_number='12')
        self.excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        self.second_excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='2')
        self.now = timezone.now() - timedelta(seconds=5)
        self.ticket_data = self.get_ticket()

    def login(self, access):
        from django.test import RequestFactory
        request = RequestFactory().get('/')
        request.session = self.client.session
        activate_role_session(request, access)
        request.session.save()
        self.client.cookies['sessionid'] = request.session.session_key
        access.refresh_from_db()

    def get_ticket(self):
        response = self.client.get(self.url, {'device_id': self.device})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def event(self, kind, sequence, *, local_shift=None, payload=None, base=None):
        event_id = str(uuid4())
        actor = self.ticket_data['snapshot']['actor']
        event = {
            'id': event_id, 'format_version': 1, 'type': kind, 'device_id': self.device,
            'sequence': sequence, 'occurred_at': (self.now + timedelta(milliseconds=sequence)).isoformat(),
            'actor_key': actor['key'], 'actor_id': actor['id'], 'actor_access_id': actor['access_id'],
            'actor_generation': actor['generation'], 'local_shift_id': local_shift or event_id,
            'payload': payload or {},
        }
        if base is not None:
            event['base_payload'] = base
        return event

    def send(self, event, ticket=None):
        response = self.client.post(self.url, json.dumps({'event': event, 'ticket': ticket or self.ticket_data['ticket']}), content_type='application/json')
        return response.json()

    def opened(self):
        event = self.event('shift.open', 1)
        result = self.send(event)
        self.assertEqual(result['status'], 'applied', result)
        return event, OfflineFieldEvent.objects.get(event_id=event['id']).shift

    def payload(self, *, excavator=None, trucks=None):
        return {'excavators': [{'excavator_id': (excavator or self.excavator).pk, 'truck_ids': [item.pk for item in (trucks if trucks is not None else [self.truck])]}]}

    def test_get_requires_verified_master_and_uuid_device(self):
        self.assertEqual(self.ticket_data['protocol'], 'master-field/1')
        self.assertEqual(self.ticket_data['snapshot']['actor']['key'], _actor(self.access)['key'])
        self.assertEqual(Client().get(self.url, {'device_id': self.device}).status_code, 403)
        self.assertEqual(self.client.get(self.url, {'device_id': 'arbitrary'}).status_code, 400)

    def test_open_replay_and_close_preserve_real_time_and_stable_shift(self):
        event, shift = self.opened()
        self.assertEqual(shift.opened_at.isoformat(), event['occurred_at'])
        self.assertTrue(self.send(event)['deduplicated'])
        self.assertEqual(EmployeeShift.objects.count(), 1)
        close = self.event('shift.close', 2, local_shift=event['id'])
        self.assertEqual(self.send(close)['status'], 'applied')
        shift.refresh_from_db()
        self.assertEqual(shift.closed_at.isoformat(), close['occurred_at'])
        self.assertTrue(self.send(close)['deduplicated'])

    def test_existing_server_shift_get_has_stable_uuid_without_open_event(self):
        shift = EmployeeShift.objects.create(employee=self.employee, opened_by=self.employee, workplace_code='mining_master', shift_type='day', opened_at=self.now-timedelta(minutes=1))
        first, second = self.get_ticket(), self.get_ticket()
        self.assertEqual(first['snapshot']['shift']['local_shift_id'], second['snapshot']['shift']['local_shift_id'])
        self.ticket_data = first
        event = self.event('shift.close', 1, local_shift=first['snapshot']['shift']['local_shift_id'])
        self.assertEqual(self.send(event)['status'], 'applied')
        self.assertFalse(OfflineFieldEvent.objects.filter(event_type=EVENT_PREFIX+'shift.open').exists())
        shift.refresh_from_db(); self.assertIsNotNone(shift.closed_at)

    def test_changed_payload_or_sequence_never_overwrites_receipt(self):
        event, _ = self.opened()
        changed = {**event, 'payload': {'other': True}}
        self.assertEqual(self.send(changed)['code'], 'event_id_reused')
        changed = {**event, 'id': str(uuid4()), 'type': 'shift.close'}
        self.assertEqual(self.send(changed)['code'], 'sequence_reused')
        self.assertEqual(OfflineFieldEvent.objects.count(), 1)
        self.assertEqual(OfflineFieldEventConflict.objects.count(), 2)

    def test_opening_waits_for_other_master_and_never_closes_their_shift(self):
        other = Employee.objects.create(full_name='Действующий мастер', status=Employee.Status.ACTIVE)
        shift = EmployeeShift.objects.create(employee=other, opened_by=other, workplace_code='mining_master', shift_type='day', opened_at=self.now-timedelta(hours=1))
        result = self.send(self.event('shift.open', 1))
        self.assertEqual(result['code'], 'shift_occupied')
        shift.refresh_from_db(); self.assertIsNone(shift.closed_at)

    def test_wrong_ticket_device_actor_and_revocation_preserve_no_mutation(self):
        event = self.event('shift.open', 1)
        self.assertEqual(self.send(event, 'invalid')['status'], 'auth')
        self.assertEqual(self.send({**event, 'actor_id': 999})['status'], 'auth')
        self.assertEqual(self.send({**event, 'device_id': str(uuid4())})['status'], 'auth')
        EmployeeAccess.objects.filter(pk=self.access.pk).update(is_active=False)
        self.assertEqual(self.send(event)['status'], 'auth')
        self.assertFalse(EmployeeShift.objects.exists())
        self.assertFalse(OfflineFieldEvent.objects.exists())

    def test_old_signed_actor_not_replaced_by_new_cookie_at_handover(self):
        event, shift = self.opened()
        old_ticket = self.ticket_data['ticket']
        other = Employee.objects.create(full_name='Мастер Б', phone='+79997654321', status=Employee.Status.ACTIVE)
        access = EmployeeAccess.objects.create(employee=other, role=self.role, access_code='864286', status=EmployeeAccess.Status.ACTIVATED)
        self.login(access)
        close = self.event('shift.close', 2, local_shift=event['id'])
        self.assertEqual(self.send(close, old_ticket)['status'], 'applied')
        shift.refresh_from_db(); self.assertEqual(shift.closed_by_id, self.employee.pk)
        self.assertEqual(self.client.session['employee_access_id'], access.pk)

    def test_session_alias_requires_exact_current_generation(self):
        event = self.event('shift.open', 1)
        event['actor_key'] = _alias(event['actor_generation'])
        event['actor_id'] = event['actor_access_id'] = None
        self.assertEqual(self.send(event)['status'], 'applied')
        self.login(self.access)
        self.assertEqual(self.send(event)['status'], 'auth')

    def test_draft_is_durable_shared_and_does_not_touch_live_or_push_state(self):
        event, _ = self.opened()
        version = OperationalStateVersion.objects.get(key='production').version
        draft = self.event('draft.save', 2, local_shift=event['id'], payload=self.payload(), base={'excavators': []})
        result = self.send(draft)
        self.assertEqual(result['status'], 'applied', result)
        self.assertEqual(self.get_ticket()['snapshot']['draft']['payload'], self.payload())
        self.assertFalse(HaulAssignment.objects.exists())
        self.assertFalse(ExcavatorPlacement.objects.exists())
        self.assertEqual(OperationalStateVersion.objects.get(key='production').version, version)

    def test_live_apply_uses_pending_service_real_actor_and_idempotent_receipt(self):
        event, shift = self.opened()
        command = self.event('live.save', 2, local_shift=event['id'], payload=self.payload(), base={'excavators': []})
        self.assertEqual(self.send(command)['status'], 'applied')
        assignment = HaulAssignment.objects.get(truck=self.truck)
        self.assertEqual(assignment.assigned_by_id, self.employee.pk)
        self.assertEqual(assignment.status, AssignmentStatus.PENDING)
        self.assertGreater(assignment.effective_at, timezone.now())
        self.assertEqual(OfflineFieldEvent.objects.get(event_id=command['id']).shift_id, shift.pk)
        self.assertTrue(self.send(command)['deduplicated'])
        self.assertEqual(HaulAssignment.objects.count(), 1)

    def test_independent_live_change_preserved_but_same_truck_conflict_is_saved(self):
        event, _ = self.opened()
        schedule_haul_assignment(truck=self.other_truck, excavator=self.second_excavator, assigned_by=self.employee)
        command = self.event('live.save', 2, local_shift=event['id'], payload=self.payload(), base={'excavators': []})
        self.assertEqual(self.send(command)['status'], 'applied')
        self.assertEqual(HaulAssignment.objects.filter(truck=self.other_truck, ended_at__isnull=True).get().excavator_id, self.second_excavator.pk)
        conflicting = self.event('live.save', 3, local_shift=event['id'], payload=self.payload(excavator=self.second_excavator), base={'excavators': []})
        self.assertEqual(self.send(conflicting)['code'], 'base_conflict')
        self.assertEqual(OfflineFieldEvent.objects.get(event_id=conflicting['id']).status, 'conflict')
        self.assertEqual(HaulAssignment.objects.filter(truck=self.truck, ended_at__isnull=True).get().excavator_id, self.excavator.pk)

    def test_late_or_unbound_shift_cannot_edit_live(self):
        event, shift = self.opened()
        shift.closed_at = timezone.now(); shift.closed_by=self.employee; shift.save()
        command = self.event('live.save', 2, local_shift=event['id'], payload=self.payload(), base={'excavators': []})
        self.assertEqual(self.send(command)['code'], 'shift_closed')
        self.assertFalse(HaulAssignment.objects.exists())

    def test_event_before_opening_retries_then_replays_without_duplicate(self):
        opened = self.event('shift.open', 1)
        command = self.event('draft.save', 2, local_shift=opened['id'], payload=self.payload(), base={'excavators': []})
        self.assertEqual(self.send(command)['status'], 'retry')
        self.assertEqual(self.send(opened)['status'], 'applied')
        self.assertEqual(self.send(command)['status'], 'applied')
        receipt = OfflineFieldEvent.objects.get(event_id=command['id'])
        self.assertFalse(receipt.retryable)
        self.assertEqual(receipt.error_code, '')

    def test_old_event_after_newer_accepted_never_reorders_live(self):
        event, _ = self.opened()
        newer = self.event('draft.save', 3, local_shift=event['id'], payload=self.payload(), base={'excavators': []})
        self.assertEqual(self.send(newer)['status'], 'applied')
        earlier = self.event('draft.save', 2, local_shift=event['id'], payload=self.payload(excavator=self.second_excavator), base={'excavators': []})
        self.assertEqual(self.send(earlier)['code'], 'event_out_of_order')
        self.assertEqual(self.get_ticket()['snapshot']['draft']['payload'], self.payload())

    def test_csrf_is_required_even_for_signed_ticket(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(self.url, json.dumps({'ticket': self.ticket_data['ticket'], 'event': self.event('shift.open', 1)}), content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_draft_passes_between_real_masters_after_closed_shift(self):
        opened, _ = self.opened()
        self.assertEqual(self.send(self.event('draft.save', 2, local_shift=opened['id'], payload=self.payload(), base={'excavators': []}))['status'], 'applied')
        self.assertEqual(self.send(self.event('shift.close', 3, local_shift=opened['id']))['status'], 'applied')
        other = Employee.objects.create(full_name='Следующий мастер', phone='+79998765432', status=Employee.Status.ACTIVE)
        access = EmployeeAccess.objects.create(employee=other, role=self.role, access_code='864286', status=EmployeeAccess.Status.ACTIVATED)
        self.login(access)
        snapshot = self.get_ticket()['snapshot']
        self.assertEqual(snapshot['actor']['id'], other.pk)
        self.assertEqual(snapshot['draft']['payload'], self.payload())
        self.assertFalse(snapshot['shift']['is_open'])

    def test_apply_draft_changes_live_and_resets_shared_draft_to_target(self):
        opened, _ = self.opened()
        draft = self.event('draft.save', 2, local_shift=opened['id'], payload=self.payload(), base={'excavators': []})
        self.assertEqual(self.send(draft)['status'], 'applied')
        applied = self.event('draft.apply', 3, local_shift=opened['id'], payload=self.payload(), base={'excavators': []})
        result = self.send(applied)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['snapshot']['live'], self.payload())
        self.assertEqual(result['snapshot']['draft']['payload'], self.payload())

    def test_closed_historical_shift_prevents_backdated_overlapping_opening(self):
        EmployeeShift.objects.create(employee=self.employee, opened_by=self.employee, workplace_code='mining_master',
            shift_type='day', opened_at=self.now-timedelta(hours=1), closed_at=self.now+timedelta(seconds=2), closed_by=self.employee)
        self.assertEqual(self.send(self.event('shift.open', 1))['code'], 'late_shift')
        self.assertEqual(EmployeeShift.objects.count(), 1)

    def test_second_assignment_failure_rolls_back_first_and_preserves_conflict_receipt(self):
        opened, _ = self.opened()
        version = OperationalStateVersion.objects.get(key='production').version
        calls = []
        def fail_second(**kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise HaulAssignmentStateConflict(expected_state_id=0, actual_state_id=999)
            return schedule_haul_assignment(**kwargs)
        event = self.event('live.save', 2, local_shift=opened['id'], payload=self.payload(trucks=[self.truck,self.other_truck]), base={'excavators': []})
        with patch('assignments.field_sync.schedule_haul_assignment', side_effect=fail_second):
            result = self.send(event)
        self.assertEqual(result['status'], 'conflict')
        self.assertFalse(HaulAssignment.objects.exists())
        self.assertFalse(ExcavatorPlacement.objects.exists())
        self.assertFalse(AdminActionLog.objects.exists())
        self.assertEqual(OperationalStateVersion.objects.get(key='production').version, version)
        self.assertEqual(OfflineFieldEvent.objects.get(event_id=event['id']).status, 'conflict')

    def test_noop_does_not_normalize_inferred_active_placement(self):
        opened, _ = self.opened()
        schedule_haul_assignment(truck=self.truck, excavator=self.excavator, assigned_by=self.employee)
        version = OperationalStateVersion.objects.get(key='production').version
        event = self.event('live.save', 2, local_shift=opened['id'], payload=self.payload(), base=self.payload())
        result = self.send(event)
        self.assertEqual(result['status'], 'applied')
        self.assertFalse(ExcavatorPlacement.objects.exists())
        self.assertFalse(AdminActionLog.objects.exists())
        self.assertEqual(HaulAssignment.objects.count(), 1)
        self.assertEqual(OperationalStateVersion.objects.get(key='production').version, version)

    def test_unrelated_inactive_excavator_assignment_is_not_released_by_unrepresentable_snapshot(self):
        opened, _ = self.opened()
        assignment, _ = schedule_haul_assignment(truck=self.truck, excavator=self.excavator, assigned_by=self.employee)
        Equipment.objects.filter(pk=self.excavator.pk).update(is_active=False)
        before = (assignment.status, assignment.ended_at)
        event = self.event('live.save', 2, local_shift=opened['id'], payload={'excavators': []}, base={'excavators': []})
        self.assertEqual(self.send(event)['status'], 'applied')
        assignment.refresh_from_db()
        self.assertEqual((assignment.status, assignment.ended_at), before)
        self.assertEqual(HaulAssignment.objects.count(), 1)

    def test_malformed_ids_and_body_never_mutate_domain(self):
        event = self.event('shift.open', 1)
        for change in ({'type': []}, {'sequence': 2**70}, {'format_version': True}, {'id': event['id'].upper()}):
            with self.subTest(change=change):
                result = self.send({**event, **change})
                self.assertEqual(result['code'], 'invalid_event')
        self.assertFalse(EmployeeShift.objects.exists())
        self.assertFalse(OfflineFieldEvent.objects.exists())

    def test_server_never_issues_shared_phone_key_for_blank_employee_phone(self):
        Employee.objects.filter(pk=self.employee.pk).update(phone='')
        self.assertEqual(self.client.get(self.url, {'device_id': self.device}).status_code, 403)
