import json
from datetime import timedelta

from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, ExcavatorPlacement, HaulAssignment
from assignments.tests import MiningMasterAssignmentsViewTests
from core.models import OfflineFieldEvent
from core.test_offline_sync import OfflineEventSyncTests
from downtimes.models import DowntimeEvent, DowntimeReason
from shifts.models import EmployeeShift
from trips.test_dispatcher_equipment_commands import DispatcherEquipmentCommandBehaviorTests
from trips.test_dispatcher_topology_commands import DispatcherMoveExcavatorCommandTests
from trips.tests import DispatcherAssignmentRealtimeTests, DispatcherDowntimeControlTests
from trips.views import get_operational_state_version
from users.models import EmployeeAccess, Role


class RoleProbeMixin:
    def set_active_role(self, code):
        role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})
        access_id = self.client.session['employee_access_id']
        access = EmployeeAccess.objects.get(pk=access_id)
        access.role = role
        access.last_login_at = timezone.now()
        access.save(update_fields=['role', 'last_login_at'])
        session = self.client.session
        session['active_role_access_id'] = access.id
        session['active_role_code'] = code
        session['active_role_login_at'] = access.last_login_at.isoformat()
        session.save()
        EmployeeShift.objects.filter(closed_at__isnull=True).update(workplace_code='dispatcher')
        return access

    @staticmethod
    def close_dispatcher_shift():
        EmployeeShift.objects.filter(
            workplace_code='dispatcher', closed_at__isnull=True,
        ).update(closed_at=timezone.now())


class PassportDowntimePostgreSQLProbe(OfflineEventSyncTests):
    def test_disputed_start_is_idempotent_and_preserves_action_time(self):
        reason, _ = DowntimeReason.objects.get_or_create(
            name='Ожидание разгрузки',
            defaults={
                'equipment_type': self.truck_type,
                'show_for_truck_driver': True,
            },
        )
        occurred_at = timezone.now() - timedelta(minutes=2)
        self.truck_shift.opened_at = occurred_at - timedelta(minutes=5)
        self.truck_shift.save(update_fields=['opened_at'])
        event = self.driver_downtime_event(
            event_id='passport-driver-unload-wait-empty-server',
            sequence=1,
            reason=reason,
            occurred_at=occurred_at,
        )
        client = self.driver_client()
        version_before = get_operational_state_version()
        first = self.sync(
            [event], client=client, role_code='driver',
            device_id='passport-postgresql-driver',
        ).json()['results'][0]
        version_after_first = get_operational_state_version()
        second = self.sync(
            [event], client=client, role_code='driver',
            device_id='passport-postgresql-driver',
        ).json()['results'][0]
        version_after_second = get_operational_state_version()

        self.assertEqual(first['status'], 'accepted', first)
        self.assertEqual(second['status'], 'deduplicated', second)
        receipt = OfflineFieldEvent.objects.get(event_id=event['event_id'])
        downtime = DowntimeEvent.objects.get(
            pk=first['server_ids']['downtime_event_id'],
        )
        self.assertEqual(receipt.status, 'accepted')
        self.assertEqual(receipt.downtime_event_id, downtime.id)
        self.assertEqual(receipt.occurred_at, occurred_at)
        self.assertEqual(downtime.started_at, occurred_at)
        self.assertEqual(DowntimeEvent.objects.filter(reason=reason).count(), 1)
        self.assertGreater(version_after_first, version_before)
        self.assertEqual(version_after_second, version_after_first)
        print('EVIDENCE downtime_start', json.dumps({
            'first_status': first['status'],
            'second_status': second['status'],
            'deduplicated': second['status'] == 'deduplicated',
            'receipt_id': receipt.id,
            'downtime_id': downtime.id,
            'receipt_downtime_id': receipt.downtime_event_id,
            'occurred_at': receipt.occurred_at.isoformat(),
            'started_at': downtime.started_at.isoformat(),
            'downtime_count': DowntimeEvent.objects.filter(reason=reason).count(),
            'result_version': first['version'],
            'version_before': version_before,
            'version_after_first': version_after_first,
            'version_after_second': version_after_second,
        }, ensure_ascii=False, sort_keys=True))


class PassportManagerMoveProbe(RoleProbeMixin, DispatcherMoveExcavatorCommandTests):
    def request_move(self):
        return self.client.post(reverse('dispatcher_move_excavator'), data=json.dumps({
            'excavator_id': self.excavator.id,
            'zone': ExcavatorPlacement.Zone.INACTIVE,
            'expected_zone': ExcavatorPlacement.Zone.ACTIVE,
            'expected_assignment_states': {str(self.truck.id): self.assignment.id},
            'client_action_id': 'passport-manager-move',
        }), content_type='application/json')

    def test_manager_open_shift_can_move_excavator(self):
        self.set_active_role('manager')
        response = self.request_move()
        self.assertEqual(response.status_code, 200, response.content)
        self.placement.refresh_from_db()
        self.assertEqual(self.placement.zone, ExcavatorPlacement.Zone.INACTIVE)

    def test_manager_closed_shift_cannot_move_excavator(self):
        self.set_active_role('manager')
        self.close_dispatcher_shift()
        response = self.request_move()
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn('Смена', response.json()['error'])

    def test_mining_master_cannot_use_dispatcher_move_endpoint(self):
        self.set_active_role('mining_master')
        self.assertEqual(self.request_move().status_code, 403)


class PassportManagerAssignmentProbe(RoleProbeMixin, DispatcherAssignmentRealtimeTests):
    def request_release(self):
        state_id = HaulAssignment.objects.get(truck=self.truck, ended_at__isnull=True).id
        return self.client.post(reverse('dispatcher_assign_truck'), data=json.dumps({
            'action': 'release',
            'truck_id': self.truck.id,
            'expected_assignment_state_id': state_id,
            'client_action_id': 'passport-manager-release',
        }), content_type='application/json')

    def test_manager_open_shift_can_release_truck(self):
        self.set_active_role('manager')
        response = self.request_release()
        self.assertEqual(response.status_code, 200, response.content)

    def test_manager_closed_shift_cannot_release_truck(self):
        self.set_active_role('manager')
        self.close_dispatcher_shift()
        response = self.request_release()
        self.assertEqual(response.status_code, 409, response.content)

    def test_mining_master_cannot_use_dispatcher_assignment_endpoint(self):
        self.set_active_role('mining_master')
        self.assertEqual(self.request_release().status_code, 403)


class PassportManagerSettingsProbe(RoleProbeMixin, DispatcherEquipmentCommandBehaviorTests):
    def request_settings(self):
        return self.post_settings({
            'state_version': get_operational_state_version(),
            'rock_type_id': self.rock.id,
            'destinations': [{
                'dump_point_id': self.first_dump.id,
                'transport_distance_km': '3.75',
            }],
            'loading_horizon': '75',
            'loading_block': '52',
        })

    def test_manager_open_shift_can_change_face(self):
        self.set_active_role('manager')
        response = self.request_settings()
        self.assertEqual(response.status_code, 200, response.content)

    def test_manager_closed_shift_cannot_change_face(self):
        self.set_active_role('manager')
        self.close_dispatcher_shift()
        response = self.request_settings()
        self.assertEqual(response.status_code, 409, response.content)

    def test_mining_master_cannot_use_dispatcher_settings_endpoint(self):
        self.set_active_role('mining_master')
        self.assertEqual(self.request_settings().status_code, 403)


class PassportManagerDowntimeProbe(RoleProbeMixin, DispatcherDowntimeControlTests):
    def test_manager_open_shift_can_close_downtime(self):
        self.set_active_role('manager')
        downtime = self.create_downtime(self.truck, self.truck_reason)
        response = self.close_downtime(downtime)
        self.assertEqual(response.status_code, 200, response.content)
        downtime.refresh_from_db()
        self.assertIsNotNone(downtime.ended_at)

    def test_manager_closed_shift_cannot_close_downtime(self):
        self.set_active_role('manager')
        downtime = self.create_downtime(self.truck, self.truck_reason)
        self.close_dispatcher_shift()
        response = self.close_downtime(downtime)
        self.assertEqual(response.status_code, 409, response.content)
        downtime.refresh_from_db()
        self.assertIsNone(downtime.ended_at)

    def test_mining_master_cannot_use_dispatcher_downtime_endpoint(self):
        self.set_active_role('mining_master')
        downtime = self.create_downtime(self.truck, self.truck_reason)
        self.assertEqual(self.close_downtime(downtime).status_code, 403)


class PassportRoleGuardMatrixProbe(DispatcherMoveExcavatorCommandTests):
    def test_common_dispatcher_guard_role_matrix(self):
        from trips.dispatcher_guards import dispatcher_access_from_request

        access_id = self.client.session['employee_access_id']
        request = RequestFactory().get('/dispatcher/control/')
        request.session = {'employee_access_id': access_id}
        access = EmployeeAccess.objects.get(pk=access_id)
        matrix = {}
        for code in ('dispatcher', 'admin', 'manager', 'mining_master', 'driver'):
            role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})
            access.role = role
            access.save(update_fields=['role'])
            matrix[code] = dispatcher_access_from_request(request) is not None
        self.assertEqual(matrix, {
            'dispatcher': True,
            'admin': True,
            'manager': True,
            'mining_master': False,
            'driver': False,
        })
        print('EVIDENCE role_guard', json.dumps(matrix, sort_keys=True))


class PassportDispatcherStaleCommandProbe(DispatcherAssignmentRealtimeTests):
    def test_dispatcher_stale_command_is_409_and_version_is_unchanged(self):
        state_id = HaulAssignment.objects.get(truck=self.truck).id
        other = self.excavator.__class__.objects.create(
            equipment_type=self.excavator.equipment_type,
            model=self.excavator.model,
            garage_number='PASSPORT-OTHER',
            is_active=True,
        )
        ExcavatorPlacement.objects.create(excavator=other, zone=ExcavatorPlacement.Zone.ACTIVE)
        first = self.client.post(reverse('dispatcher_assign_truck'), data=json.dumps({
            'action': 'assign', 'truck_id': self.truck.id,
            'excavator_id': other.id, 'expected_assignment_state_id': state_id,
            'client_action_id': 'passport-dispatcher-first',
        }), content_type='application/json')
        self.assertEqual(first.status_code, 200, first.content)
        version_before = get_operational_state_version()
        stale = self.client.post(reverse('dispatcher_assign_truck'), data=json.dumps({
            'action': 'release', 'truck_id': self.truck.id,
            'expected_assignment_state_id': state_id,
            'client_action_id': 'passport-dispatcher-stale',
        }), content_type='application/json')
        version_after = get_operational_state_version()
        self.assertEqual(stale.status_code, 409, stale.content)
        self.assertEqual(stale.json()['code'], 'state_conflict')
        self.assertEqual(version_after, version_before)
        print('EVIDENCE dispatcher_stale', json.dumps({
            'http': stale.status_code, 'code': stale.json()['code'],
            'version_before': version_before, 'version_after': version_after,
        }, sort_keys=True))


class PassportMiningMasterStaleCommandProbe(MiningMasterAssignmentsViewTests):
    def test_master_stale_command_is_409_and_version_is_unchanged(self):
        state_id = HaulAssignment.objects.get(truck=self.assigned_truck).id
        version_before = get_operational_state_version()
        response = self.client.post(reverse('mining_master_assign_truck'), data=json.dumps({
            'action': 'assign', 'truck_id': self.assigned_truck.id,
            'excavator_id': self.other_excavator.id,
            'expected_assignment_state_id': state_id + 1000,
            'client_action_id': 'passport-master-stale',
        }), content_type='application/json')
        version_after = get_operational_state_version()
        self.assertEqual(response.status_code, 409, response.content)
        self.assertTrue(response.json()['conflict'])
        self.assertEqual(version_after, version_before)
        print('EVIDENCE master_stale', json.dumps({
            'http': response.status_code, 'conflict': response.json()['conflict'],
            'version_before': version_before, 'version_after': version_after,
        }, sort_keys=True))
