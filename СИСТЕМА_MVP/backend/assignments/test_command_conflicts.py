"""Конфликт CAS имеет неизменяемый исход; неизвестный ответ отказом не считается."""
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from unittest import skipUnless
from unittest.mock import Mock

from django.db import close_old_connections, connection, transaction
from django.http import JsonResponse
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from assignments.command_context import bound_command
from assignments.models import ExcavatorPlacement, HaulAssignment
from assignments.test_command_context import ContextFixture
from core.models import OperationalStateVersion, OperationalStateEvent
from shifts.models import ShiftClientAction


class ConflictFixture(ContextFixture):
    def conflict(self, role, action='move_excavator'):
        case = self.scenario(role, action)
        if action == 'move_excavator':
            case['payload']['expected_zone'] = 'active'
        else:
            case['payload']['expected_assignment_state_id'] = 999999
        return case

    def source(self, case):
        return {'kind': 'json', 'url': reverse(case['route']), 'data': deepcopy(case['payload']),
                'id': case['context']['id'], 'occurredAt': case['context']['occurred_at'],
                'author': deepcopy(case['context']['author'])}

    def lookup(self, case, source=None):
        return self.login(case['access']).post(reverse('assignment_command_receipt'),
            data=json.dumps(self.source(case) if source is None else source), content_type='application/json')

    def effects(self):
        return [list(model.objects.values()) for model in (ExcavatorPlacement, HaulAssignment, OperationalStateVersion, OperationalStateEvent)]


class CommandConflictTests(ConflictFixture, TestCase):
    def test_all_four_routes_save_rejection_and_never_apply_it_when_state_later_matches(self):
        for role in ('dispatcher', 'mining_master'):
            for action in ('move_excavator', 'assign_truck'):
                with self.subTest(role=role, action=action):
                    case = self.conflict(role, action)
                    # Existing production singleton is locked outside the rollback savepoint.
                    OperationalStateVersion.objects.get_or_create(key='production')
                    before = self.effects()
                    response = self.post(case)
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json()['code'], 'state_conflict')
                    self.assertEqual(self.effects(), before)
                    saved = deepcopy(self.receipt(case).response_payload)
                    self.assertEqual(saved['_command_outcome'], 'rejected')
                    result = self.lookup(case).json()
                    self.assertEqual(result['status'], 'rejected')
                    self.assertEqual(result['evidence']['http_status'], 409)
                    self.assertEqual(result['evidence']['command_context'], saved['_command_context'])
                    self.assertFalse(any(k.startswith('_') for k in result['receipt']))
                    # A legacy client also receives a refusal, never a false 200.
                    legacy = self.post(case, context=None)
                    self.assertEqual(legacy.status_code, 409)
                    self.assertTrue(legacy.json()['deduplicated'])
                    self.assertNotIn('_command_outcome', legacy.json())
                    if action == 'move_excavator':
                        case['placement'].zone = 'active'
                        case['placement'].save()
                    case['shift'].closed_at = timezone.now()
                    case['shift'].save(update_fields=['closed_at'])
                    before = self.effects()
                    repeated = self.post(case)
                    self.assertEqual(repeated.status_code, 409)
                    self.assertTrue(repeated.json()['deduplicated'])
                    self.assertEqual(self.effects(), before)
                    self.assertEqual(self.receipt(case).response_payload, saved)
                    self.assertEqual(self.lookup(case).json()['status'], 'rejected')

    def test_single_release_bulk_release_and_disband_conflicts_are_durable(self):
        for role in ('dispatcher', 'mining_master'):
            case = self.scenario(role, 'assign_truck')
            base = deepcopy(case['payload'])
            for kind in ('release', 'release_complex', 'disband'):
                with self.subTest(role=role, kind=kind):
                    case['payload'] = {**base, 'client_action_id': base['client_action_id'] + kind,
                        'action': kind, 'expected_assignment_state_id': 999999,
                        'expected_assignment_states': {str(base['truck_id']): 999999}}
                    case['context']['id'] = 'sync-' + case['payload']['client_action_id']
                    case['route'] = role + '_assign_truck'
                    if kind == 'disband':
                        case['route'] = role + '_move_excavator'
                        case['payload'].update(zone='inactive', expected_zone='active')
                    self.assertEqual(self.post(case).status_code, 409)
                    self.assertEqual(self.lookup(case).json()['status'], 'rejected')
                    self.assertFalse(HaulAssignment.objects.exists())
                    case['placement'].refresh_from_db()
                    self.assertEqual(case['placement'].zone, 'active')

    def test_rejection_requires_exact_original_and_does_not_enrich_legacy_or_expose_other_context(self):
        case = self.conflict('dispatcher')
        self.assertEqual(self.post(case).status_code, 409)
        stored = deepcopy(self.receipt(case).response_payload)
        for field in ('actor_id', 'access_id', 'shift_id', 'role'):
            source = self.source(case)
            source['author'][field] = 'wrong'
            self.assertEqual(self.lookup(case, source).json()['status'], 'unresolved')
        for field in ('id', 'occurredAt', 'author'):
            source = self.source(case)
            del source[field]
            self.assertEqual(self.lookup(case, source).json()['status'], 'unresolved')
        source = self.source(case)
        source['data']['expected_zone'] = 'inactive'
        self.assertEqual(self.lookup(case, source).json()['status'], 'unresolved')
        changed = deepcopy(case['context'])
        changed['id'] += '-changed'
        self.assertEqual(self.post(case, context=changed).json()['code'], 'command_context_changed')
        case['payload']['expected_zone'] = 'inactive'
        self.assertEqual(self.post(case).json()['code'], 'command_id_reused')
        self.assertEqual(self.receipt(case).response_payload, stored)

    def test_legacy_and_guard_conflicts_have_no_fabricated_final_outcome(self):
        case = self.conflict('mining_master')
        self.assertEqual(self.post(case, context=None).status_code, 409)
        self.assertFalse(ShiftClientAction.objects.exists())
        for change in ('shift', 'dependency'):
            modified = deepcopy(case)
            if change == 'shift':
                modified['context']['author']['shift_id'] = '999999'
            else:
                modified = self.scenario('dispatcher', 'assign_truck')
                modified['payload']['expected_assignment_state_id'] = 'command:unknown'
            self.assertEqual(self.post(modified).status_code, 409)
            self.assertFalse(ShiftClientAction.objects.exists())
            self.assertEqual(self.lookup(modified).json()['status'], 'unresolved')

    def test_refusal_savepoint_rolls_back_view_effects_and_on_commit_callbacks(self):
        case = self.scenario('mining_master')
        OperationalStateVersion.objects.get_or_create(key='production')
        callback = Mock()
        @transaction.atomic
        @bound_command(case['route'], shift_getter=lambda access: case['shift'], allowed_roles={'mining_master'})
        def tentative_view(request):
            case['placement'].zone = 'active'
            case['placement'].save()
            transaction.on_commit(callback)
            return JsonResponse({'ok': False, 'code': 'state_conflict',
                'client_action_id': case['payload']['client_action_id']}, status=409)
        request = RequestFactory().post(reverse(case['route']), data=json.dumps(case['payload']),
            content_type='application/json', HTTP_X_COMMAND_CONTEXT=json.dumps(case['context']))
        request.session = self.login(case['access']).session
        before = self.effects()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(tentative_view(request).status_code, 409)
        callback.assert_not_called()
        self.assertEqual(self.effects(), before)
        self.assertEqual(self.lookup(case).json()['status'], 'rejected')

    def test_old_unsuccessful_payload_without_outcome_marker_is_still_unresolved(self):
        case = self.conflict('dispatcher')
        self.post(case)
        receipt = self.receipt(case)
        del receipt.response_payload['_command_outcome']
        receipt.save(update_fields=['response_payload'])
        self.assertEqual(self.lookup(case).json()['status'], 'unresolved')


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL row locks required')
class CommandConflictPostgreSQLTests(ConflictFixture, TransactionTestCase):
    def concurrent_refusal(self, role):
        case = self.conflict(role, 'assign_truck')
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
        self.assertEqual([status for status, _ in results], [409, 409])
        self.assertEqual(sum(bool(body.get('deduplicated')) for _, body in results), 1)
        self.assertEqual(ShiftClientAction.objects.count(), 1)
        self.assertFalse(HaulAssignment.objects.exists())
        self.assertEqual(self.lookup(case).json()['status'], 'rejected')

    def test_master_concurrent_refusal_is_one_durable_outcome(self):
        self.concurrent_refusal('mining_master')

    def test_dispatcher_concurrent_refusal_is_one_durable_outcome(self):
        self.concurrent_refusal('dispatcher')
