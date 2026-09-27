import json
import os
from datetime import timedelta

from django.db import connection
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    ExcavatorPlacement,
    HaulAssignment,
    HaulAssignmentAction,
)
from core.models import OfflineFieldEvent
from core.test_offline_sync import OfflineEventSyncTests
from downtimes.driver_workflow import driver_downtime_requires_loaded_trip
from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import Equipment, EquipmentType
from shifts.models import EmployeeShift, ShiftClientAction
from trips.models import Trip
from trips.views import get_operational_state_version
from users.models import Employee, EmployeeAccess, Role


def evidence(name, payload):
    print(f'EVIDENCE_R2 {name} ' + json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


class DatabaseEvidenceMixin:
    def assert_database_contract(self):
        expected_vendor = os.environ['PASSPORT_EXPECTED_VENDOR']
        expected_db = os.environ['PASSPORT_TEST_DB']
        actual_db = str(connection.settings_dict['NAME'])
        self.assertEqual(connection.vendor, expected_vendor)
        self.assertEqual(actual_db, expected_db)
        evidence('database', {
            'source_sha': os.environ['PASSPORT_SOURCE_SHA'],
            'package_sha256': os.environ['PASSPORT_PACKAGE_SHA256'],
            'vendor': connection.vendor,
            'test_database': actual_db,
        })


class PassportDowntimeR2Probe(DatabaseEvidenceMixin, OfflineEventSyncTests):
    def test_disputed_start_exact_scope_and_idempotency(self):
        self.assert_database_contract()
        reason, _ = DowntimeReason.objects.get_or_create(
            name='Ожидание разгрузки',
            defaults={
                'equipment_type': self.truck_type,
                'show_for_truck_driver': True,
            },
        )
        self.assertTrue(driver_downtime_requires_loaded_trip(reason))
        self.assertFalse(
            Trip.objects.filter(truck=self.truck).exclude(
                status__in=('completed', 'cancelled', 'uncontrolled'),
            ).exists()
        )
        occurred_at = timezone.now() - timedelta(minutes=2)
        self.truck_shift.opened_at = occurred_at - timedelta(minutes=5)
        self.truck_shift.save(update_fields=['opened_at'])
        event = self.driver_downtime_event(
            event_id='passport-r2-unload-wait-empty-server',
            sequence=1,
            reason=reason,
            occurred_at=occurred_at,
        )
        self.assertNotIn('sent_live', event)
        self.assertNotIn('clock_unreliable', event)
        self.assertNotIn('sent_live', event['payload'])
        self.assertNotIn('clock_unreliable', event['payload'])

        client = self.driver_client()
        version_before = get_operational_state_version()
        with self.assertLogs('core.offline_sync', level='WARNING') as warning_log:
            first = self.sync(
                [event], client=client, role_code='driver',
                device_id='passport-r2-driver',
            ).json()['results'][0]
        version_after_first = get_operational_state_version()
        second = self.sync(
            [event], client=client, role_code='driver',
            device_id='passport-r2-driver',
        ).json()['results'][0]
        version_after_second = get_operational_state_version()

        self.assertTrue(any('loaded_trip_required' in row for row in warning_log.output))
        self.assertEqual(first['status'], 'accepted', first)
        self.assertEqual(first['code'], '')
        self.assertFalse(first['retryable'])
        self.assertNotIn('device_clock_adjusted', first)
        self.assertNotIn('time_source', first)
        self.assertEqual(second['status'], 'deduplicated', second)
        receipt = OfflineFieldEvent.objects.get(event_id=event['event_id'])
        downtime = DowntimeEvent.objects.get(pk=first['server_ids']['downtime_event_id'])
        self.assertEqual(receipt.status, 'accepted')
        self.assertEqual(receipt.error_code, '')
        self.assertEqual(receipt.downtime_event_id, downtime.id)
        self.assertEqual(receipt.occurred_at, occurred_at)
        self.assertEqual(downtime.started_at, occurred_at)
        self.assertEqual(DowntimeEvent.objects.filter(reason=reason).count(), 1)
        self.assertGreater(version_after_first, version_before)
        self.assertEqual(version_after_second, version_after_first)
        evidence('downtime', {
            'precondition_requires_loaded_trip': True,
            'precondition_open_trip_count': 0,
            'hints': {'sent_live': False, 'clock_unreliable': False},
            'clock_adjustment_fields_absent': True,
            'effective_time_basis': 'device occurred_at (proved by exact persisted equality)',
            'first_status': first['status'],
            'first_code': first['code'],
            'warning': 'loaded_trip_required',
            'receipt_status': receipt.status,
            'receipt_error_code': receipt.error_code,
            'receipt_downtime_id': receipt.downtime_event_id,
            'downtime_id': downtime.id,
            'occurred_at': receipt.occurred_at.isoformat(),
            'started_at': downtime.started_at.isoformat(),
            'second_status': second['status'],
            'downtime_count': DowntimeEvent.objects.filter(reason=reason).count(),
            'version_before': version_before,
            'version_after_first': version_after_first,
            'version_after_second': version_after_second,
        })


class DispatcherMutationFixture(DatabaseEvidenceMixin, TestCase):
    role_code = 'manager'

    def setUp(self):
        self.dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.manager_role = Role.objects.create(code='manager', name='Руководитель')
        self.admin_role = Role.objects.create(code='admin', name='Администратор')
        self.master_role = Role.objects.create(code='mining_master', name='Горный мастер')
        self.dispatcher = Employee.objects.create(full_name='Чужой диспетчер', status=Employee.Status.ACTIVE)
        self.manager = Employee.objects.create(full_name='Отдельный manager', status=Employee.Status.ACTIVE)
        self.admin = Employee.objects.create(full_name='Контрольный admin', status=Employee.Status.ACTIVE)
        self.master = Employee.objects.create(full_name='Контрольный мастер', status=Employee.Status.ACTIVE)
        now = timezone.now()
        self.accesses = {}
        for employee, role, code in (
            (self.dispatcher, self.dispatcher_role, 'dispatcher'),
            (self.manager, self.manager_role, 'manager'),
            (self.admin, self.admin_role, 'admin'),
            (self.master, self.master_role, 'mining_master'),
        ):
            access = EmployeeAccess.objects.create(
                employee=employee, role=role, access_code=f'R2-{code}',
                is_active=True, status=EmployeeAccess.Status.ACTIVATED,
                last_login_at=now,
            )
            self.accesses[code] = access
        self.dispatcher_shift = EmployeeShift.objects.create(
            employee=self.dispatcher, shift_type='day', workplace_code='dispatcher',
            opened_at=now - timedelta(hours=1), opened_by=self.dispatcher,
        )
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck = Equipment.objects.create(equipment_type=truck_type, garage_number='R2-T')
        self.source = Equipment.objects.create(equipment_type=excavator_type, garage_number='R2-E1')
        self.target = Equipment.objects.create(equipment_type=excavator_type, garage_number='R2-E2')
        ExcavatorPlacement.objects.create(excavator=self.source, zone=ExcavatorPlacement.Zone.ACTIVE)
        ExcavatorPlacement.objects.create(excavator=self.target, zone=ExcavatorPlacement.Zone.ACTIVE)
        self.assignment = HaulAssignment.objects.create(
            truck=self.truck, excavator=self.source, assigned_by=self.dispatcher,
            status=AssignmentStatus.ACCEPTED, accepted_at=now,
        )

    def client_for(self, role_code):
        access = self.accesses[role_code]
        client = Client()
        session = client.session
        session['employee_access_id'] = access.id
        session['active_role_access_id'] = access.id
        session['active_role_code'] = role_code
        session['active_role_login_at'] = access.last_login_at.isoformat()
        session.save()
        return client

    def request(self, role_code, action, *, client_action_id=None, expected_state_id=None):
        payload = {
            'action': action,
            'truck_id': self.truck.id,
            'expected_assignment_state_id': self.assignment.id if expected_state_id is None else expected_state_id,
            'client_action_id': client_action_id or f'r2-{role_code}-{action}',
        }
        if action == 'assign':
            payload['excavator_id'] = self.target.id
        return self.client_for(role_code).post(
            reverse('dispatcher_assign_truck'),
            data=json.dumps(payload), content_type='application/json',
        )

    def snapshot(self):
        return list(
            HaulAssignment.objects.filter(truck=self.truck)
            .order_by('id')
            .values('id', 'excavator_id', 'action', 'status', 'ended_at', 'assigned_by_id')
        )

    def assert_success(self, role_code, action):
        self.assert_database_contract()
        actor = self.accesses[role_code].employee
        self.assertNotEqual(actor.id, self.dispatcher_shift.employee_id)
        self.assertFalse(EmployeeShift.objects.filter(employee=actor, closed_at__isnull=True).exists())
        response = self.request(role_code, action)
        self.assertEqual(response.status_code, 200, response.content)
        created = HaulAssignment.objects.get(pk=response.json()['assignment_id'])
        self.assertEqual(created.assigned_by_id, actor.id)
        self.assertEqual(created.action, action)
        self.assertEqual(created.status, AssignmentStatus.PENDING)
        self.assertEqual(
            created.excavator_id,
            self.target.id if action == 'assign' else self.source.id,
        )
        receipt = ShiftClientAction.objects.get(
            action_type='dispatcher_assign_truck',
            client_action_id=f'r2-{role_code}-{action}',
        )
        self.assertEqual(receipt.employee_id, actor.id)
        self.assertEqual(receipt.shift_id, self.dispatcher_shift.id)
        evidence(f'{role_code}_{action}_success', {
            'http': response.status_code,
            'actor_id': actor.id,
            'dispatcher_shift_employee_id': self.dispatcher_shift.employee_id,
            'created_assignment': created.id,
            'created_action': created.action,
            'created_status': created.status,
            'receipt_id': receipt.id,
            'receipt_shift_id': receipt.shift_id,
        })

    def assert_closed_shift_refusal(self, action):
        self.dispatcher_shift.closed_at = timezone.now()
        self.dispatcher_shift.save(update_fields=['closed_at'])
        before = self.snapshot()
        version_before = get_operational_state_version()
        response = self.request('manager', action)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(get_operational_state_version(), version_before)
        self.assertFalse(ShiftClientAction.objects.filter(client_action_id=f'r2-manager-{action}').exists())
        evidence(f'manager_{action}_closed_shift', {
            'http': response.status_code,
            'assignments_unchanged': True,
            'version_unchanged': True,
            'receipt_count': 0,
        })

    def assert_forbidden(self, action):
        before = self.snapshot()
        version_before = get_operational_state_version()
        response = self.request('mining_master', action)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(get_operational_state_version(), version_before)
        self.assertFalse(ShiftClientAction.objects.filter(client_action_id=f'r2-mining_master-{action}').exists())

    def test_manager_assign_with_foreign_dispatcher_shift(self):
        self.assert_success('manager', 'assign')

    def test_manager_release_with_foreign_dispatcher_shift(self):
        self.assert_success('manager', 'release')

    def test_dispatcher_assign_control_same_payload(self):
        response = self.request('dispatcher', 'assign')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(HaulAssignment.objects.get(pk=response.json()['assignment_id']).action, 'assign')

    def test_dispatcher_release_control_same_payload(self):
        response = self.request('dispatcher', 'release')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(HaulAssignment.objects.get(pk=response.json()['assignment_id']).action, 'release')

    def test_admin_assign_control_same_payload(self):
        self.assert_success('admin', 'assign')

    def test_admin_release_control_same_payload(self):
        self.assert_success('admin', 'release')

    def test_manager_assign_closed_shift_has_no_mutation(self):
        self.assert_closed_shift_refusal('assign')

    def test_manager_release_closed_shift_has_no_mutation(self):
        self.assert_closed_shift_refusal('release')

    def test_mining_master_assign_is_forbidden_without_mutation(self):
        self.assert_forbidden('assign')

    def test_mining_master_release_is_forbidden_without_mutation(self):
        self.assert_forbidden('release')


class StaleAssignPlacementR2Probe(DispatcherMutationFixture):
    def stale_assign(self, client_action_id):
        return self.request(
            'dispatcher', 'assign', client_action_id=client_action_id,
            expected_state_id=self.assignment.id + 1000,
        )

    def assert_stale_side_effect(self, *, placement_exists):
        self.assert_database_contract()
        if placement_exists:
            placement = ExcavatorPlacement.objects.get(excavator=self.target)
            placement.zone = ExcavatorPlacement.Zone.INACTIVE
            placement.save(update_fields=['zone'])
        else:
            ExcavatorPlacement.objects.filter(excavator=self.target).delete()
        assignments_before = self.snapshot()
        version_before = get_operational_state_version()
        action_id = f'r2-stale-placement-{placement_exists}'
        response = self.stale_assign(action_id)
        version_after = get_operational_state_version()
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()['code'], 'state_conflict')
        self.assertEqual(self.snapshot(), assignments_before)
        placement = ExcavatorPlacement.objects.get(excavator=self.target)
        self.assertEqual(placement.zone, ExcavatorPlacement.Zone.ACTIVE)
        self.assertGreater(version_after, version_before)
        self.assertFalse(ShiftClientAction.objects.filter(
            action_type='dispatcher_assign_truck', client_action_id=action_id,
        ).exists())
        evidence('stale_assign_placement_side_effect', {
            'placement_existed_before': placement_exists,
            'http': response.status_code,
            'code': response.json()['code'],
            'assignments_unchanged': True,
            'placement_after': placement.zone,
            'version_before': version_before,
            'version_after': version_after,
            'receipt_count': 0,
        })

    def test_stale_assign_activates_existing_inactive_placement_before_409(self):
        self.assert_stale_side_effect(placement_exists=True)

    def test_stale_assign_creates_missing_placement_before_409(self):
        self.assert_stale_side_effect(placement_exists=False)
