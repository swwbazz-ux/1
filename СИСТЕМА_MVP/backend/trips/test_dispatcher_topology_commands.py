import json
import inspect
from unittest.mock import patch

from django.http import JsonResponse
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    ExcavatorPlacement,
    HaulAssignment,
    HaulAssignmentAction,
)
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
