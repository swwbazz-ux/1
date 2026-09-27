import json
import inspect
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.http import JsonResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    ExcavatorPlacement,
    HaulAssignment,
    HaulAssignmentAction,
)
from core.models import OperationalStateEvent, OperationalStateVersion
from references.models import Equipment, EquipmentType
from shifts.models import EmployeeShift, ShiftClientAction
from users.models import Employee, EmployeeAccess, Role

from . import dispatcher_topology_commands
from . import views as trips_views
from .models import DispatcherActionLog


class DispatcherTopologyCommandBoundaryTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_views_reexport_optimistic_state_parsers(self):
        self.assertIs(
            trips_views.required_assignment_state_id,
            dispatcher_topology_commands.required_assignment_state_id,
        )
        self.assertIs(
            trips_views.required_projected_assignment_states,
            dispatcher_topology_commands.required_projected_assignment_states,
        )

    def test_public_views_are_thin_topology_command_facades(self):
        move_source = inspect.getsource(trips_views.dispatcher_move_excavator_view)
        assign_source = inspect.getsource(trips_views.dispatcher_assign_truck_view)

        self.assertIn('_execute_dispatcher_move_excavator(', move_source)
        self.assertIn('_execute_dispatcher_assign_truck(', assign_source)
        for source in (move_source, assign_source):
            self.assertNotIn('Equipment.objects', source)
            self.assertNotIn('schedule_haul_', source)
            self.assertIn('lock_mutation_access=lock_dispatcher_mutation_access', source)
            self.assertIn('action_logger=log_dispatcher_action', source)
            self.assertIn('equipment_label=equipment_short_name', source)

    def test_public_views_keep_post_guard_before_command_execution(self):
        request = self.factory.get('/dispatcher/control/excavator/move/')
        with patch.object(trips_views, '_execute_dispatcher_move_excavator') as execute:
            response = trips_views.dispatcher_move_excavator_view(request)
        self.assertEqual(response.status_code, 405)
        execute.assert_not_called()

        request = self.factory.get('/dispatcher/control/truck/assign/')
        with patch.object(trips_views, '_execute_dispatcher_assign_truck') as execute:
            response = trips_views.dispatcher_assign_truck_view(request)
        self.assertEqual(response.status_code, 405)
        execute.assert_not_called()


class DispatcherTopologyFacadeDelegationTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def assert_facade_dependencies(self, execute, request):
        execute.assert_called_once_with(
            request,
            lock_mutation_access=trips_views.lock_dispatcher_mutation_access,
            action_logger=trips_views.log_dispatcher_action,
            equipment_label=trips_views.equipment_short_name,
        )

    def test_move_facade_injects_views_patch_seams(self):
        request = self.factory.post('/dispatcher/control/excavator/move/')
        expected = JsonResponse({'ok': True})
        with patch.object(
            trips_views,
            '_execute_dispatcher_move_excavator',
            return_value=expected,
        ) as execute:
            response = trips_views.dispatcher_move_excavator_view(request)

        self.assertIs(response, expected)
        self.assert_facade_dependencies(execute, request)

    def test_assign_facade_injects_views_patch_seams(self):
        request = self.factory.post('/dispatcher/control/truck/assign/')
        expected = JsonResponse({'ok': True})
        with patch.object(
            trips_views,
            '_execute_dispatcher_assign_truck',
            return_value=expected,
        ) as execute:
            response = trips_views.dispatcher_assign_truck_view(request)

        self.assertIs(response, expected)
        self.assert_facade_dependencies(execute, request)


class DispatcherMoveExcavatorCommandTests(TestCase):
    def setUp(self):
        role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.dispatcher = Employee.objects.create(
            full_name='Диспетчер команды расстановки',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=role,
            access_code='TOPOLOGY-COMMAND',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        EmployeeShift.objects.create(
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
            garage_number='TOPOLOGY-TRUCK',
        )
        self.excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='TOPOLOGY-EXCAVATOR',
        )
        self.placement = ExcavatorPlacement.objects.create(
            excavator=self.excavator,
            zone=ExcavatorPlacement.Zone.ACTIVE,
            changed_by=self.dispatcher,
        )
        self.assignment = HaulAssignment.objects.create(
            truck=self.truck,
            excavator=self.excavator,
            assigned_by=self.dispatcher,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        session = self.client.session
        session['employee_access_id'] = access.id
        session.save()

    def test_move_to_garage_is_atomic_and_idempotent(self):
        payload = {
            'excavator_id': self.excavator.id,
            'zone': ExcavatorPlacement.Zone.INACTIVE,
            'expected_zone': ExcavatorPlacement.Zone.ACTIVE,
            'expected_assignment_states': {
                str(self.truck.id): self.assignment.id,
            },
            'client_action_id': 'move-excavator-to-garage',
        }

        first_response = self.client.post(
            reverse('dispatcher_move_excavator'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        repeated_response = self.client.post(
            reverse('dispatcher_move_excavator'),
            data=json.dumps(payload),
            content_type='application/json',
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(repeated_response.status_code, 200)
        first_payload = first_response.json()
        repeated_payload = repeated_response.json()
        self.assertEqual(first_payload['scheduled'], 1)
        self.assertEqual(
            repeated_payload['assignment_state_ids'],
            first_payload['assignment_state_ids'],
        )
        self.assertTrue(repeated_payload['deduplicated'])
        self.placement.refresh_from_db()
        self.assertEqual(self.placement.zone, ExcavatorPlacement.Zone.INACTIVE)
        self.assertEqual(
            HaulAssignment.objects.filter(
                truck=self.truck,
                action=HaulAssignmentAction.RELEASE,
                status=AssignmentStatus.PENDING,
                ended_at__isnull=True,
            ).count(),
            1,
        )
        self.assertEqual(DispatcherActionLog.objects.count(), 1)
        self.assertEqual(
            ShiftClientAction.objects.filter(
                employee=self.dispatcher,
                action_type='dispatcher_move_excavator',
                client_action_id='move-excavator-to-garage',
            ).count(),
            1,
        )


class DispatcherAssignTruckAtomicityTests(TestCase):
    def setUp(self):
        role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.dispatcher = Employee.objects.create(
            full_name='Диспетчер атомарного назначения',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=role,
            access_code='ATOMIC-ASSIGN',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        EmployeeShift.objects.create(
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
            garage_number='ATOMIC-TRUCK',
        )
        self.source_excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='ATOMIC-SOURCE',
        )
        self.target_excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='ATOMIC-TARGET',
        )
        ExcavatorPlacement.objects.create(
            excavator=self.source_excavator,
            zone=ExcavatorPlacement.Zone.ACTIVE,
            changed_by=self.dispatcher,
        )
        self.target_placement = ExcavatorPlacement.objects.create(
            excavator=self.target_excavator,
            zone=ExcavatorPlacement.Zone.ACTIVE,
            changed_by=self.dispatcher,
        )
        self.assignment = HaulAssignment.objects.create(
            truck=self.truck,
            excavator=self.source_excavator,
            assigned_by=self.dispatcher,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        session = self.client.session
        session['employee_access_id'] = access.id
        session.save()

    def version(self):
        return OperationalStateVersion.objects.get(key='production').version

    def assignment_snapshot(self):
        return list(
            HaulAssignment.objects.filter(truck=self.truck)
            .order_by('id')
            .values(
                'id', 'excavator_id', 'assigned_by_id', 'action', 'status',
                'assigned_at', 'accepted_at', 'ended_at',
            )
        )

    def post_assign(self, *, client_action_id, expected_state_id):
        return self.client.post(
            reverse('dispatcher_assign_truck'),
            data=json.dumps({
                'action': 'assign',
                'truck_id': self.truck.id,
                'excavator_id': self.target_excavator.id,
                'expected_assignment_state_id': expected_state_id,
                'client_action_id': client_action_id,
            }),
            content_type='application/json',
        )

    def assert_stale_assign_is_fully_rolled_back(self, *, placement_exists):
        if placement_exists:
            self.target_placement.zone = ExcavatorPlacement.Zone.INACTIVE
            self.target_placement.save(update_fields=['zone'])
            placement_before = ExcavatorPlacement.objects.values().get(
                pk=self.target_placement.pk,
            )
        else:
            self.target_placement.delete()
            placement_before = None
        assignments_before = self.assignment_snapshot()
        version_before = self.version()
        events_before = list(
            OperationalStateEvent.objects.order_by('id').values()
        )
        action_id = f'stale-assign-placement-{placement_exists}'

        with patch(
            'core.dispatcher_push.send_dispatcher_push_for_event',
        ) as send_push, self.captureOnCommitCallbacks(execute=True):
            response = self.post_assign(
                client_action_id=action_id,
                expected_state_id=self.assignment.id + 1000,
            )

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()['code'], 'state_conflict')
        self.assertEqual(self.assignment_snapshot(), assignments_before)
        self.assertEqual(self.version(), version_before)
        self.assertEqual(
            list(OperationalStateEvent.objects.order_by('id').values()),
            events_before,
        )
        if placement_before is None:
            self.assertFalse(
                ExcavatorPlacement.objects.filter(
                    excavator=self.target_excavator,
                ).exists()
            )
        else:
            self.assertEqual(
                ExcavatorPlacement.objects.values().get(pk=self.target_placement.pk),
                placement_before,
            )
        self.assertFalse(
            ShiftClientAction.objects.filter(
                action_type='dispatcher_assign_truck',
                client_action_id=action_id,
            ).exists()
        )
        send_push.assert_not_called()

    def test_stale_assign_keeps_existing_inactive_placement_unchanged(self):
        self.assert_stale_assign_is_fully_rolled_back(placement_exists=True)

    def test_stale_assign_does_not_create_missing_placement(self):
        self.assert_stale_assign_is_fully_rolled_back(placement_exists=False)

    def test_successful_assign_and_repeat_remain_idempotent(self):
        self.target_placement.delete()
        action_id = 'successful-assign-idempotent'
        version_before = self.version()

        with patch(
            'core.dispatcher_push.send_dispatcher_push_for_event',
        ) as send_push, self.captureOnCommitCallbacks(execute=True) as callbacks:
            first = self.post_assign(
                client_action_id=action_id,
                expected_state_id=self.assignment.id,
            )

        self.assertEqual(first.status_code, 200, first.content)
        first_payload = first.json()
        created = HaulAssignment.objects.get(pk=first_payload['assignment_id'])
        self.assertEqual(created.truck_id, self.truck.id)
        self.assertEqual(created.excavator_id, self.target_excavator.id)
        self.assertEqual(created.status, AssignmentStatus.PENDING)
        self.assertEqual(created.action, HaulAssignmentAction.ASSIGN)
        self.assertTrue(first_payload['created'])
        self.assertEqual(
            ExcavatorPlacement.objects.get(
                excavator=self.target_excavator,
            ).zone,
            ExcavatorPlacement.Zone.ACTIVE,
        )
        self.assertEqual(
            ShiftClientAction.objects.filter(
                action_type='dispatcher_assign_truck',
                client_action_id=action_id,
            ).count(),
            1,
        )
        self.assertEqual(DispatcherActionLog.objects.count(), 1)
        self.assertEqual(len(callbacks), 1)
        send_push.assert_called_once()
        version_after_first = self.version()
        self.assertGreater(version_after_first, version_before)
        assignments_after_first = self.assignment_snapshot()
        events_after_first = list(
            OperationalStateEvent.objects.order_by('id').values()
        )

        with patch(
            'core.dispatcher_push.send_dispatcher_push_for_event',
        ) as repeat_push, self.captureOnCommitCallbacks(execute=True) as repeat_callbacks:
            repeated = self.post_assign(
                client_action_id=action_id,
                expected_state_id=self.assignment.id,
            )

        self.assertEqual(repeated.status_code, 200, repeated.content)
        self.assertTrue(repeated.json()['deduplicated'])
        self.assertEqual(repeated.json()['assignment_id'], created.id)
        self.assertEqual(self.assignment_snapshot(), assignments_after_first)
        self.assertEqual(
            list(OperationalStateEvent.objects.order_by('id').values()),
            events_after_first,
        )
        self.assertEqual(self.version(), version_after_first)
        self.assertEqual(
            ShiftClientAction.objects.filter(
                action_type='dispatcher_assign_truck',
                client_action_id=action_id,
            ).count(),
            1,
        )
        self.assertEqual(DispatcherActionLog.objects.count(), 1)
        self.assertEqual(repeat_callbacks, [])
        repeat_push.assert_not_called()


@skipUnless(connection.vendor == 'postgresql', 'Requires isolated PostgreSQL test database')
class DispatcherAssignTruckPostgreSQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.dispatcher = Employee.objects.create(
            full_name='Диспетчер конкурентного назначения',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=role,
            access_code='CONCURRENT-ASSIGN',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        EmployeeShift.objects.create(
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
            garage_number='CONCURRENT-TRUCK',
        )
        self.source_excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='CONCURRENT-SOURCE',
        )
        self.targets = [
            Equipment.objects.create(
                equipment_type=excavator_type,
                garage_number=f'CONCURRENT-TARGET-{index}',
            )
            for index in (1, 2)
        ]
        ExcavatorPlacement.objects.create(
            excavator=self.source_excavator,
            zone=ExcavatorPlacement.Zone.ACTIVE,
            changed_by=self.dispatcher,
        )
        self.assignment = HaulAssignment.objects.create(
            truck=self.truck,
            excavator=self.source_excavator,
            assigned_by=self.dispatcher,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )

    def authenticated_client(self):
        client = Client()
        session = client.session
        session['employee_access_id'] = self.access.id
        session.save()
        return client

    def test_competing_assigns_commit_one_target_without_loser_side_effects(self):
        clients = [self.authenticated_client(), self.authenticated_client()]
        barrier = Barrier(2)
        version_before = OperationalStateVersion.objects.get(key='production').version
        event_count_before = OperationalStateEvent.objects.count()

        def send(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = clients[index].post(
                    reverse('dispatcher_assign_truck'),
                    data=json.dumps({
                        'action': 'assign',
                        'truck_id': self.truck.id,
                        'excavator_id': self.targets[index].id,
                        'expected_assignment_state_id': self.assignment.id,
                        'client_action_id': f'concurrent-assign-{index}',
                    }),
                    content_type='application/json',
                )
                return response.status_code, response.json()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(send, (0, 1)))

        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        success_index = next(index for index, item in enumerate(results) if item[0] == 200)
        loser_index = 1 - success_index
        self.assertEqual(results[loser_index][1]['code'], 'state_conflict')
        pending = HaulAssignment.objects.get(
            truck=self.truck,
            status=AssignmentStatus.PENDING,
            ended_at__isnull=True,
        )
        self.assertEqual(pending.excavator_id, self.targets[success_index].id)
        self.assertTrue(
            ExcavatorPlacement.objects.filter(
                excavator=self.targets[success_index],
                zone=ExcavatorPlacement.Zone.ACTIVE,
            ).exists()
        )
        self.assertFalse(
            ExcavatorPlacement.objects.filter(
                excavator=self.targets[loser_index],
            ).exists()
        )
        self.assertEqual(
            ShiftClientAction.objects.filter(
                action_type='dispatcher_assign_truck',
            ).count(),
            1,
        )
        version_after = OperationalStateVersion.objects.get(key='production').version
        self.assertEqual(
            OperationalStateEvent.objects.count() - event_count_before,
            version_after - version_before,
        )
