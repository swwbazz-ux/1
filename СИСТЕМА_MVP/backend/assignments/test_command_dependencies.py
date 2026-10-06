"""Причинные команды используют квитанцию предшественника, сохраняя CAS и исходник."""
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
from assignments.test_command_context import ContextFixture
from references.models import Equipment
from shifts.models import ShiftClientAction


class DependencyFixture(ContextFixture):
    def child(self, parent, *, action='assign', suffix='next'):
        child = deepcopy(parent)
        child['payload'].update(client_action_id=parent['payload']['client_action_id'] + '-' + suffix,
                                action=action,
                                expected_assignment_state_id='command:' + parent['payload']['client_action_id'])
        child['context']['id'] = 'sync-' + child['payload']['client_action_id']
        child['context']['occurred_at'] = timezone.now().isoformat()
        if action == 'assign':
            excavator = Equipment.objects.create(equipment_type=parent['placement'].excavator.equipment_type,
                                                  garage_number=child['payload']['client_action_id'])
            child['placement'] = ExcavatorPlacement.objects.create(excavator=excavator, zone='active')
            child['payload']['excavator_id'] = excavator.pk
        return child


class CommandDependencyTests(DependencyFixture, TestCase):
    def test_reversed_delivery_waits_then_assign_assign_release_keeps_source_and_receipts(self):
        for role in ('mining_master', 'dispatcher'):
            with self.subTest(role=role):
                parent = self.scenario(role, 'assign_truck')
                child = self.child(parent)
                release = self.child(child, action='release')
                original = deepcopy(child['payload'])
                before = HaulAssignment.objects.count()
                response = self.post(child)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()['code'], 'command_dependency_unresolved')
                self.assertEqual(HaulAssignment.objects.count(), before)
                self.assertFalse(ShiftClientAction.objects.filter(client_action_id=child['payload']['client_action_id']).exists())
                first = self.post(parent)
                self.assertEqual(first.status_code, 200, first.content)
                second = self.post(child)
                self.assertEqual(second.status_code, 200, second.content)
                final = self.post(release)
                self.assertEqual(final.status_code, 200, final.content)
                self.assertEqual(HaulAssignment.objects.filter(truck_id=parent['payload']['truck_id']).count(), 3)
                self.assertEqual(HaulAssignment.objects.get(pk=second.json()['assignment_id']).status, 'cancelled')
                self.assertEqual(self.receipt(child).response_payload['_request_signature'], _request_signature(original))
                self.assertEqual(child['payload'], original)
                self.assertEqual(second.json()['truck_id'], parent['payload']['truck_id'])
                self.assertEqual(second.json()['assignment_effective_at'],
                                 (timezone.datetime.fromisoformat(child['context']['occurred_at']) + timedelta(minutes=5)).isoformat())
                repeated = self.post(child)
                self.assertEqual(repeated.status_code, 200)
                self.assertTrue(repeated.json()['deduplicated'])
                self.assertEqual(repeated.json()['assignment_id'], second.json()['assignment_id'])

    def test_intervening_command_keeps_state_conflict_and_dispatcher_activation_rolls_back(self):
        for role in ('mining_master', 'dispatcher'):
            parent = self.scenario(role, 'assign_truck')
            first = self.post(parent).json()
            child = self.child(parent)
            newer = self.child(child, suffix='other')
            current, _ = schedule_haul_assignment(truck=Equipment.objects.get(pk=parent['payload']['truck_id']),
                excavator=newer['placement'].excavator, expected_state_id=first['assignment_state_id'])
            if role == 'dispatcher':
                child['placement'].zone = 'inactive'
                child['placement'].save(update_fields=['zone'])
            before = (list(HaulAssignment.objects.values()), list(ExcavatorPlacement.objects.values()))
            response = self.post(child)
            self.assertEqual(response.status_code, 409, response.content)
            self.assertEqual(response.json()['code'], 'state_conflict')
            self.assertEqual(before, (list(HaulAssignment.objects.values()), list(ExcavatorPlacement.objects.values())))
            current.refresh_from_db()
            self.assertEqual(current.status, 'pending')

    def test_receipt_must_match_truck_employee_access_role_shift_and_have_provenance(self):
        parent = self.scenario('mining_master', 'assign_truck')
        self.assertEqual(self.post(parent).status_code, 200)
        child = self.child(parent)
        receipt = self.receipt(parent)
        original = deepcopy(receipt.response_payload)
        for change in ('truck', 'employee', 'access', 'role', 'shift', 'legacy', 'state', 'ok'):
            with self.subTest(change=change):
                stored = deepcopy(original)
                if change == 'truck':
                    stored['truck_id'] += 1000
                elif change == 'legacy':
                    del stored['_command_context']
                elif change == 'state':
                    stored['assignment_state_id'] = True
                elif change == 'ok':
                    stored['ok'] = False
                else:
                    field = 'actor_id' if change == 'employee' else change + '_id' if change in ('access', 'shift') else change
                    stored['_command_context']['author'][field] = 'wrong'
                receipt.response_payload = stored
                receipt.save(update_fields=['response_payload'])
                response = self.post(child)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()['code'], 'command_dependency_unresolved')
                self.assertEqual(HaulAssignment.objects.count(), 1)
        receipt.response_payload = original
        receipt.save(update_fields=['response_payload'])
        self.assertEqual(self.post(child).status_code, 200)

    def test_self_reference_bad_clock_and_missing_header_do_not_execute(self):
        parent = self.scenario('dispatcher', 'assign_truck')
        child = self.child(parent)
        self.assertEqual(self.post(parent).status_code, 200)
        original = deepcopy(child)
        child['payload']['expected_assignment_state_id'] = 'command:' + child['payload']['client_action_id']
        self.assertEqual(self.post(child).json()['code'], 'command_dependency_invalid')
        child = deepcopy(original)
        child['context']['occurred_at'] = parent['shift'].opened_at.isoformat()
        self.assertEqual(self.post(child).json()['code'], 'command_dependency_invalid')
        self.assertEqual(self.post(original, context=None).status_code, 409)
        self.assertEqual(HaulAssignment.objects.count(), 1)
        self.assertEqual(ShiftClientAction.objects.count(), 1)

    def test_lost_child_ack_after_shift_close_returns_same_receipt_and_changed_reference_is_rejected(self):
        parent = self.scenario('dispatcher', 'assign_truck')
        self.assertEqual(self.post(parent).status_code, 200)
        child = self.child(parent)
        self.assertEqual(self.post(child).status_code, 200)
        stored = deepcopy(self.receipt(child).response_payload)
        parent['shift'].closed_at = timezone.now()
        parent['shift'].save(update_fields=['closed_at'])
        response = self.post(child)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['deduplicated'])
        child['payload']['expected_assignment_state_id'] = 0
        self.assertEqual(self.post(child).json()['code'], 'command_id_reused')
        self.assertEqual(self.receipt(child).response_payload, stored)

    def test_confirmed_empty_release_resolves_zero_and_later_assign_is_allowed(self):
        parent = self.scenario('mining_master', 'assign_truck')
        parent['payload']['action'] = 'release'
        response = self.post(parent)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['assignment_state_id'], 0)
        child = self.child(parent)
        self.assertEqual(self.post(child).status_code, 200)
        self.assertEqual(HaulAssignment.objects.count(), 1)


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL row locks required')
class CommandDependencyPostgreSQLTests(DependencyFixture, TransactionTestCase):
    def test_two_concurrent_children_of_same_receipt_cannot_both_overwrite_assignment(self):
        for role in ('mining_master', 'dispatcher'):
            parent = self.scenario(role, 'assign_truck')
            self.assertEqual(self.post(parent).status_code, 200)
            children = [self.child(parent, suffix=str(index)) for index in range(2)]
            clients = [self.login(parent['access']) for _ in children]
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
            self.assertEqual(HaulAssignment.objects.filter(truck_id=parent['payload']['truck_id']).count(), 2)
