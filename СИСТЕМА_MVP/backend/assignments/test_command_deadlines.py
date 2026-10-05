"""Срок ASSIGN от исходной команды, без продления доставкой или повтором."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest import skipUnless

from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from assignments.models import AssignmentStatus, ExcavatorPlacement, HaulAssignment
from assignments.services import (
    accept_haul_assignment_now,
    reconcile_due_haul_assignments,
    schedule_haul_release,
)
from assignments.test_command_context import ContextFixture
from core.models import OperationalStateEvent
from references.models import Equipment
from shifts.models import ShiftClientAction


class CommandDeadlineTests(ContextFixture, TestCase):
    def late_case(self, role, *, minutes=2):
        case = self.scenario(role, 'assign_truck')
        received_at = timezone.now()
        case['shift'].opened_at = received_at - timedelta(hours=1)
        case['shift'].save(update_fields=['opened_at'])
        issued_at = received_at - timedelta(minutes=minutes)
        case['context']['occurred_at'] = issued_at.isoformat()
        return case, issued_at, received_at

    def accepted(self, case):
        response = self.post(case)
        self.assertEqual(response.status_code, 200, response.content)
        return HaulAssignment.objects.get(pk=response.json()['assignment_id']), response.json()

    def test_both_roles_count_remaining_time_from_original_command_and_publish_same_deadline(self):
        for role in ('dispatcher', 'mining_master'):
            with self.subTest(role=role):
                case, issued_at, received_at = self.late_case(role)
                assignment, response = self.accepted(case)
                deadline = issued_at + timedelta(minutes=5)
                self.assertEqual(assignment.effective_at, deadline)
                self.assertGreaterEqual(assignment.assigned_at, received_at)
                self.assertEqual(response['assignment_effective_at'], deadline.isoformat())
                self.assertEqual(self.receipt(case).response_payload['assignment_effective_at'], deadline.isoformat())
                event = OperationalStateEvent.objects.filter(reason='HaulAssignment:assignment_pending',
                    object_id=str(assignment.pk)).latest('version')
                self.assertEqual(event.payload['effective_at'], deadline.isoformat())
                self.assertEqual(reconcile_due_haul_assignments(truck_id=assignment.truck_id,
                    now=deadline - timedelta(microseconds=1)), 0)
                self.assertEqual(reconcile_due_haul_assignments(truck_id=assignment.truck_id, now=deadline), 1)

    def test_expired_command_is_due_immediately_and_lost_ack_after_close_does_not_restart_timer(self):
        for role in ('dispatcher', 'mining_master'):
            case, issued_at, received_at = self.late_case(role, minutes=8)
            assignment, response = self.accepted(case)
            self.assertLess(assignment.effective_at, received_at)
            self.assertEqual(reconcile_due_haul_assignments(truck_id=assignment.truck_id, now=timezone.now()), 1)
            assignment.refresh_from_db()
            self.assertEqual(assignment.status, AssignmentStatus.ACCEPTED)
            before = list(HaulAssignment.objects.values())
            saved_receipt = deepcopy(self.receipt(case).response_payload)
            case['shift'].closed_at = timezone.now()
            case['shift'].save(update_fields=['closed_at'])
            repeated = self.post(case)
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(repeated.json(), {**response, 'deduplicated': True})
            self.assertEqual(list(HaulAssignment.objects.values()), before)
            self.assertEqual(self.receipt(case).response_payload, saved_receipt)

    def test_new_id_for_same_pending_target_keeps_first_deadline(self):
        for role in ('dispatcher', 'mining_master'):
            case, _, _ = self.late_case(role)
            original, _ = self.accepted(case)
            case['payload']['client_action_id'] += '-again'
            case['payload']['expected_assignment_state_id'] = original.pk
            case['context']['id'] += '-again'
            case['context']['occurred_at'] = timezone.now().isoformat()
            repeated, response = self.accepted(case)
            self.assertEqual(repeated.pk, original.pk)
            self.assertFalse(response['created'])
            self.assertEqual(repeated.effective_at, original.effective_at)

    def test_bad_clock_and_before_shift_never_become_fresh_deadlines_or_partial_effects(self):
        for role in ('dispatcher', 'mining_master'):
            case, _, received_at = self.late_case(role)
            if role == 'dispatcher':
                case['placement'].zone = ExcavatorPlacement.Zone.INACTIVE
                case['placement'].save(update_fields=['zone'])
            before = (list(ExcavatorPlacement.objects.values()), list(HaulAssignment.objects.values()))
            for occurred_at in [received_at + timedelta(minutes=6), case['shift'].opened_at - timedelta(microseconds=1)]:
                case['context']['occurred_at'] = occurred_at.isoformat()
                response = self.post(case)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()['code'], 'command_time_invalid')
                self.assertEqual((list(ExcavatorPlacement.objects.values()), list(HaulAssignment.objects.values())), before)
                self.assertFalse(ShiftClientAction.objects.filter(client_action_id=case['payload']['client_action_id']).exists())

    def test_late_stale_command_cannot_replace_a_new_assignment_or_activate_placement(self):
        for role in ('dispatcher', 'mining_master'):
            case, _, _ = self.late_case(role, minutes=8)
            newer = HaulAssignment.objects.create(truck_id=case['payload']['truck_id'],
                excavator=case['placement'].excavator, status=AssignmentStatus.PENDING,
                effective_at=timezone.now() + timedelta(minutes=5))
            if role == 'dispatcher':
                case['placement'].zone = ExcavatorPlacement.Zone.INACTIVE
                case['placement'].save(update_fields=['zone'])
            before = (list(ExcavatorPlacement.objects.values()), list(HaulAssignment.objects.values()))
            response = self.post(case)
            self.assertEqual(response.status_code, 409, response.content)
            self.assertEqual((list(ExcavatorPlacement.objects.values()), list(HaulAssignment.objects.values())), before)
            newer.refresh_from_db()
            self.assertEqual(newer.status, AssignmentStatus.PENDING)

    def test_legacy_without_context_and_release_keep_their_existing_protocol(self):
        for role in ('dispatcher', 'mining_master'):
            case, issued_at, received_at = self.late_case(role)
            response = self.post(case, context=None)
            self.assertEqual(response.status_code, 200)
            assignment = HaulAssignment.objects.get(pk=response.json()['assignment_id'])
            self.assertGreaterEqual(assignment.effective_at, received_at + timedelta(minutes=5))
            self.assertNotEqual(assignment.effective_at, issued_at + timedelta(minutes=5))
            truck = Equipment.objects.get(pk=case['payload']['truck_id'])
            release, _ = schedule_haul_release(truck=truck, now=received_at, expected_state_id=assignment.pk)
            self.assertEqual(release.effective_at, received_at + timedelta(minutes=5))

    def test_explicit_acceptance_still_applies_immediately_before_original_deadline(self):
        for role in ('dispatcher', 'mining_master'):
            case, _, _ = self.late_case(role)
            assignment, _ = self.accepted(case)
            tap = timezone.now()
            self.assertLess(tap, assignment.effective_at)
            accepted, applied = accept_haul_assignment_now(assignment.pk, truck_id=assignment.truck_id, at=tap)
            self.assertTrue(applied)
            self.assertEqual(accepted.accepted_at, tap)
            self.assertEqual(accepted.effective_at, tap)

    def test_named_test_truck_keeps_zero_delay_from_original_time(self):
        case, issued_at, _ = self.late_case('dispatcher')
        Equipment.objects.filter(pk=case['payload']['truck_id']).update(garage_number='ТЕСТ-1')
        assignment, _ = self.accepted(case)
        self.assertEqual(assignment.effective_at, issued_at)


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL row locks required')
class CommandDeadlinePostgreSQLTests(ContextFixture, TransactionTestCase):
    def concurrent_deadline(self, role):
        case = self.scenario(role, 'assign_truck')
        issued_at = timezone.now() - timedelta(minutes=8)
        case['shift'].opened_at = issued_at - timedelta(hours=1)
        case['shift'].save(update_fields=['opened_at'])
        case['context']['occurred_at'] = issued_at.isoformat()
        clients = [self.login(case['access']), self.login(case['access'])]
        barrier = Barrier(2)

        def send(client):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = self.post(case, client)
                return response.status_code, response.json()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(send, clients))
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(sum(bool(body.get('deduplicated')) for _, body in results), 1)
        self.assertEqual(HaulAssignment.objects.count(), 1)
        self.assertEqual(ShiftClientAction.objects.count(), 1)
        deadline = issued_at + timedelta(minutes=5)
        self.assertEqual(HaulAssignment.objects.get().effective_at, deadline)
        self.assertTrue(all(body['assignment_effective_at'] == deadline.isoformat() for _, body in results))

    def test_dispatcher_duplicate_has_one_effect_and_one_original_deadline(self):
        self.concurrent_deadline('dispatcher')

    def test_master_duplicate_has_one_effect_and_one_original_deadline(self):
        self.concurrent_deadline('mining_master')
