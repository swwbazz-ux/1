"""Одиночное назначение после массового снятия использует его квитанцию."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Barrier
from unittest import skipUnless

from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from assignments.command_guards import _request_signature
from assignments.models import ExcavatorPlacement, HaulAssignment
from assignments.services import schedule_haul_assignment
from assignments.test_mass_command_dependencies import MassCommandFixture
from references.models import Equipment
from shifts.models import ShiftClientAction


class BulkSuccessorFixture(MassCommandFixture):
    def successor(self, parent, mass, disband, suffix='next'):
        case = self.child(parent, suffix=suffix)
        case['payload']['expected_assignment_state_id'] = 'bulk:' + ('disband' if disband else 'release') + ':' + mass['payload']['client_action_id']
        return case


class BulkCommandSuccessorTests(BulkSuccessorFixture, TestCase):
    def test_reversed_delivery_then_bulk_assign_and_lost_ack_preserve_receipt_source_and_deadline(self):
        for role in ('mining_master', 'dispatcher'):
            for disband in (False, True):
                with self.subTest(role=role, disband=disband), self.isolated(role) as parents:
                    for parent in parents:
                        self.assertEqual(self.post(parent).status_code, 200)
                    mass = self.mass(parents, disband)
                    child = self.successor(parents[0], mass, disband)
                    original = deepcopy(child['payload'])
                    self.assertEqual(self.post(child).json()['code'], 'command_dependency_unresolved')
                    self.assertEqual(self.post(mass).status_code, 200)
                    response = self.post(child)
                    self.assertEqual(response.status_code, 200, response.content)
                    assignment = HaulAssignment.objects.get(pk=response.json()['assignment_id'])
                    self.assertEqual(assignment.excavator_id, child['payload']['excavator_id'])
                    self.assertEqual(assignment.effective_at, timezone.datetime.fromisoformat(child['context']['occurred_at']) + timedelta(minutes=5))
                    self.assertEqual(self.receipt(child).response_payload['_request_signature'], _request_signature(original))
                    self.assertEqual(HaulAssignment.objects.count(), 5)
                    child['shift'].closed_at = timezone.now()
                    child['shift'].save(update_fields=['closed_at'])
                    self.assertTrue(self.post(child).json()['deduplicated'])
                    child['payload']['expected_assignment_state_id'] = 0
                    self.assertEqual(self.post(child).json()['code'], 'command_id_reused')
                    self.assertEqual(HaulAssignment.objects.count(), 5)

    def test_wrong_kind_individual_receipt_missing_truck_foreign_access_or_missing_header_do_not_apply(self):
        for role in ('mining_master', 'dispatcher'):
            for disband in (False, True):
                with self.subTest(role=role, disband=disband), self.isolated(role) as parents:
                    for parent in parents:
                        self.assertEqual(self.post(parent).status_code, 200)
                    mass = self.mass(parents, disband)
                    self.assertEqual(self.post(mass).status_code, 200)
                    child = self.successor(parents[0], mass, disband)
                    good = child['payload']['expected_assignment_state_id']
                    for token in ['bulk:unknown:x', 'bulk:release', 'bulk:release:',
                                  'bulk:release:' + parents[0]['payload']['client_action_id'],
                                  'bulk:' + ('release' if disband else 'disband') + ':' + mass['payload']['client_action_id']]:
                        child['payload']['expected_assignment_state_id'] = token
                        self.assertEqual(self.post(child).status_code, 409)
                    child['payload']['expected_assignment_state_id'] = good
                    self.assertEqual(self.post(child, context=None).status_code, 409)
                    receipt = self.receipt(mass)
                    original = deepcopy(receipt.response_payload)
                    del receipt.response_payload['assignment_state_ids'][str(child['payload']['truck_id'])]
                    receipt.save(update_fields=['response_payload'])
                    self.assertEqual(self.post(child).json()['code'], 'command_dependency_unresolved')
                    receipt.response_payload = deepcopy(original)
                    receipt.response_payload['_command_context']['author']['access_id'] = 999999
                    receipt.save(update_fields=['response_payload'])
                    self.assertEqual(self.post(child).json()['code'], 'command_dependency_unresolved')
                    self.assertEqual(HaulAssignment.objects.count(), 4)
                    self.assertEqual(ShiftClientAction.objects.count(), 3)

    def test_another_assignment_after_mass_keeps_conflict_and_rolls_back_dispatcher_activation(self):
        for role in ('mining_master', 'dispatcher'):
            for disband in (False, True):
                with self.subTest(role=role, disband=disband), self.isolated(role) as parents:
                    for parent in parents:
                        self.assertEqual(self.post(parent).status_code, 200)
                    mass = self.mass(parents, disband)
                    result = self.post(mass).json()
                    child = self.successor(parents[0], mass, disband)
                    other = self.child(parents[0], suffix='other')
                    schedule_haul_assignment(truck=Equipment.objects.get(pk=child['payload']['truck_id']),
                        excavator=other['placement'].excavator,
                        expected_state_id=result['assignment_state_ids'][str(child['payload']['truck_id'])])
                    if role == 'dispatcher':
                        child['placement'].zone = 'inactive'
                        child['placement'].save(update_fields=['zone'])
                    before = (list(HaulAssignment.objects.values()), list(ExcavatorPlacement.objects.values()), ShiftClientAction.objects.count())
                    response = self.post(child)
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json()['code'], 'state_conflict')
                    self.assertEqual(self.receipt(child).response_payload['_command_outcome'], 'rejected')
                    self.assertEqual(before, (list(HaulAssignment.objects.values()), list(ExcavatorPlacement.objects.values()), ShiftClientAction.objects.count() - 1))


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL row locks required')
class BulkCommandSuccessorPostgreSQLTests(BulkSuccessorFixture, TransactionTestCase):
    def concurrent_successors(self, disband):
        for role in ('mining_master', 'dispatcher'):
            parents = self.parents(role)
            for parent in parents:
                self.assertEqual(self.post(parent).status_code, 200)
            mass = self.mass(parents, disband)
            self.assertEqual(self.post(mass).status_code, 200)
            children = [self.successor(parents[0], mass, disband, suffix=str(i)) for i in range(2)]
            clients = [self.login(parents[0]['access']) for _ in children]
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
                results = list(pool.map(send, zip(children, clients)))
            self.assertEqual(sorted(status for status, _ in results), [200, 409])
            self.assertEqual([body['code'] for status, body in results if status == 409], ['state_conflict'])
            self.assertEqual(HaulAssignment.objects.filter(truck_id=parents[0]['payload']['truck_id']).count(), 3)

    def test_concurrent_assignments_after_mass_release(self):
        self.concurrent_successors(False)

    def test_concurrent_assignments_after_disband(self):
        self.concurrent_successors(True)
