"""Массовая команда ждёт одиночные распоряжения и снимает состав атомарно."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from assignments.command_guards import _request_signature
from assignments.models import ExcavatorPlacement, HaulAssignment
from assignments.services import HaulAssignmentStateConflict, schedule_haul_assignment, schedule_haul_release
from assignments.test_command_dependencies import DependencyFixture
from references.models import Equipment
from shifts.models import ShiftClientAction


class MassCommandFixture(DependencyFixture):
    def parents(self, role):
        parent = self.scenario(role, 'assign_truck')
        second = deepcopy(parent)
        truck = Equipment.objects.create(equipment_type=Equipment.objects.get(pk=parent['payload']['truck_id']).equipment_type,
                                           garage_number=role + '-второй')
        second['payload'].update(truck_id=truck.pk, client_action_id=role + '-second')
        second['context'].update(id='sync-' + second['payload']['client_action_id'], occurred_at=timezone.now().isoformat())
        return [parent, second]

    @contextmanager
    def isolated(self, role):
        with transaction.atomic():
            yield self.parents(role)
            transaction.set_rollback(True)

    def mass(self, parents, disband):
        case = deepcopy(parents[0])
        role = case['context']['author']['role']
        ident = role + ('-disband' if disband else '-release-complex')
        case['route'] = role + ('_move_excavator' if disband else '_assign_truck')
        case['payload'] = {
            'client_action_id': ident, 'excavator_id': case['placement'].excavator_id,
            'expected_assignment_states': {str(parent['payload']['truck_id']): 'command:' + parent['payload']['client_action_id'] for parent in parents},
            'assignment_dependencies': [{'truck_id': str(parent['payload']['truck_id']), 'client_action_id': parent['payload']['client_action_id']} for parent in parents],
        }
        case['payload'].update({'zone': 'inactive', 'expected_zone': 'active'} if disband else {'action': 'release_complex'})
        case['context'].update(id='sync-' + ident, occurred_at=timezone.now().isoformat())
        return case


class MassCommandDependencyTests(MassCommandFixture, TestCase):
    def test_each_operation_waits_for_all_parents_then_preserves_original_and_deduplicates_after_close(self):
        for role in ('mining_master', 'dispatcher'):
            for disband in (False, True):
                with self.subTest(role=role, disband=disband), self.isolated(role) as parents:
                    mass = self.mass(parents, disband)
                    original = deepcopy(mass['payload'])
                    for parent in parents:
                        response = self.post(mass)
                        self.assertEqual(response.status_code, 409)
                        self.assertEqual(response.json()['code'], 'command_dependency_unresolved')
                        self.assertEqual(self.post(parent).status_code, 200)
                    response = self.post(mass)
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertEqual(response.json()['scheduled'], 2)
                    self.assertEqual(HaulAssignment.objects.filter(action='release').count(), 2)
                    mass['placement'].refresh_from_db()
                    self.assertEqual(mass['placement'].zone, 'inactive' if disband else 'active')
                    saved = deepcopy(self.receipt(mass).response_payload)
                    self.assertEqual(saved['_request_signature'], _request_signature(original))
                    mass['shift'].closed_at = timezone.now()
                    mass['shift'].save(update_fields=['closed_at'])
                    repeated = self.post(mass)
                    self.assertEqual(repeated.status_code, 200)
                    self.assertTrue(repeated.json()['deduplicated'])
                    self.assertEqual(HaulAssignment.objects.count(), 4)
                    mass['payload']['assignment_dependencies'] = []
                    self.assertEqual(self.post(mass).json()['code'], 'command_id_reused')
                    self.assertEqual(self.receipt(mass).response_payload, saved)

    def test_departed_truck_is_a_dependency_even_though_absent_from_visible_map(self):
        for role in ('mining_master', 'dispatcher'):
            for disband in (False, True):
                with self.subTest(role=role, disband=disband), self.isolated(role) as parents:
                    for parent in parents:
                        self.assertEqual(self.post(parent).status_code, 200)
                    outgoing = self.child(parents[0])
                    mass = self.mass(parents, disband)
                    del mass['payload']['expected_assignment_states'][str(parents[0]['payload']['truck_id'])]
                    mass['payload']['assignment_dependencies'][0]['client_action_id'] = outgoing['payload']['client_action_id']
                    mass['context']['occurred_at'] = timezone.now().isoformat()
                    self.assertEqual(self.post(mass).json()['code'], 'command_dependency_unresolved')
                    moved = self.post(outgoing)
                    self.assertEqual(moved.status_code, 200)
                    response = self.post(mass)
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertEqual(response.json()['scheduled'], 1)
                    assignment = HaulAssignment.objects.get(pk=moved.json()['assignment_id'])
                    self.assertEqual(assignment.status, 'pending')
                    self.assertEqual(assignment.excavator_id, outgoing['payload']['excavator_id'])

    def test_external_reassignment_causes_no_partial_release_or_placement(self):
        for role in ('mining_master', 'dispatcher'):
            for disband in (False, True):
                with self.subTest(role=role, disband=disband), self.isolated(role) as parents:
                    for parent in parents:
                        self.assertEqual(self.post(parent).status_code, 200)
                    other = self.child(parents[1])
                    previous = self.receipt(parents[1]).response_payload['assignment_state_id']
                    schedule_haul_assignment(truck=Equipment.objects.get(pk=parents[1]['payload']['truck_id']),
                        excavator=other['placement'].excavator, expected_state_id=previous)
                    mass = self.mass(parents, disband)
                    before = (list(HaulAssignment.objects.values()), list(ExcavatorPlacement.objects.values()), ShiftClientAction.objects.count())
                    response = self.post(mass)
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json()['code'], 'state_conflict')
                    self.assertEqual(self.receipt(mass).response_payload['_command_outcome'], 'rejected')
                    self.assertEqual(before, (list(HaulAssignment.objects.values()), list(ExcavatorPlacement.objects.values()), ShiftClientAction.objects.count() - 1))

    def test_late_conflict_on_second_truck_rolls_back_first_truck_and_signals(self):
        from core.models import OperationalStateEvent
        for role in ('mining_master', 'dispatcher'):
            for disband in (False, True):
                with self.subTest(role=role, disband=disband), self.isolated(role) as parents:
                    for parent in parents:
                        self.assertEqual(self.post(parent).status_code, 200)
                    mass = self.mass(parents, disband)
                    calls = []
                    def release(**kwargs):
                        calls.append(kwargs['truck'].pk)
                        if len(calls) == 2:
                            raise HaulAssignmentStateConflict(expected_state_id=1, actual_state_id=2)
                        return schedule_haul_release(**kwargs)
                    before = (list(HaulAssignment.objects.values()), list(ExcavatorPlacement.objects.values()),
                              ShiftClientAction.objects.count(), OperationalStateEvent.objects.count())
                    with patch('assignments.services.schedule_haul_release', side_effect=release):
                        response = self.post(mass)
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(self.receipt(mass).response_payload['_command_outcome'], 'rejected')
                    self.assertEqual(before, (list(HaulAssignment.objects.values()), list(ExcavatorPlacement.objects.values()),
                        ShiftClientAction.objects.count() - 1, OperationalStateEvent.objects.count()))

    def test_foreign_truck_wrong_access_missing_context_and_malformed_dependencies_never_execute(self):
        with self.isolated('mining_master') as parents:
            for parent in parents:
                self.assertEqual(self.post(parent).status_code, 200)
            mass = self.mass(parents, True)
            original = deepcopy(mass)
            # A dependency-only barrier cannot be silently ignored by a legacy request.
            mass['payload']['expected_assignment_states'] = {str(p['payload']['truck_id']): self.receipt(p).response_payload['assignment_state_id'] for p in parents}
            self.assertEqual(self.post(mass, context=None).json()['code'], 'command_context_invalid')
            for dependencies in [None, {}, ['broken'], [{}], [{'truck_id': '9999', 'client_action_id': parents[0]['payload']['client_action_id']}], [{}]*257]:
                changed = deepcopy(original)
                changed['payload']['assignment_dependencies'] = dependencies
                response = self.post(changed)
                self.assertEqual(response.status_code, 409)
            receipt = self.receipt(parents[0])
            receipt.response_payload['_command_context']['author']['access_id'] = 9999
            receipt.save(update_fields=['response_payload'])
            self.assertEqual(self.post(original).json()['code'], 'command_dependency_unresolved')
            self.assertEqual(HaulAssignment.objects.count(), 2)
            self.assertEqual(ShiftClientAction.objects.count(), 2)


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL row locks required')
class MassCommandDependencyPostgreSQLTests(MassCommandFixture, TransactionTestCase):
    def concurrent_mass(self, disband):
        for role in ('mining_master', 'dispatcher'):
            parents = self.parents(role)
            for parent in parents:
                self.assertEqual(self.post(parent).status_code, 200)
            first = self.mass(parents, disband)
            second = deepcopy(first)
            second['payload']['client_action_id'] += '-rival'
            second['context']['id'] += '-rival'
            clients = [self.login(first['access']), self.login(first['access'])]
            barrier = Barrier(2)
            def send(pair):
                case, client = pair
                close_old_connections()
                try:
                    barrier.wait(timeout=10)
                    response = self.post(case, client)
                    return response.status_code, response.json()
                finally:
                    close_old_connections()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(send, zip([first, second], clients)))
            self.assertEqual(sorted(status for status, _ in results), [200, 409])
            self.assertEqual([body['code'] for status, body in results if status == 409], ['state_conflict'])
            trucks = [parent['payload']['truck_id'] for parent in parents]
            self.assertEqual(HaulAssignment.objects.filter(truck_id__in=trucks, action='release').count(), 2)

    def test_concurrent_release_complex_keeps_one_atomic_effect(self):
        self.concurrent_mass(False)

    def test_concurrent_disband_keeps_one_atomic_effect(self):
        self.concurrent_mass(True)
