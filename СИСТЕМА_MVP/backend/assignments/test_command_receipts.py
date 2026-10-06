"""Старая очередь подтверждается только точной квитанцией, без нового эффекта."""
import json
from copy import deepcopy

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import ExcavatorPlacement, HaulAssignment
from assignments.test_command_context import ContextFixture
from shifts.models import EmployeeShift, ShiftClientAction


class CommandReceiptTests(ContextFixture, TestCase):
    def source(self, case):
        return {'id': case['context']['id'], 'kind': 'json', 'url': reverse(case['route']),
                'data': deepcopy(case['payload']), 'author': {'shift_id': ''}}

    def lookup(self, case, source=None, client=None):
        return (client or self.login(case['access'])).post(reverse('assignment_command_receipt'),
            data=json.dumps(self.source(case) if source is None else source), content_type='application/json')

    def snapshot(self):
        return [list(model.objects.values()) for model in
                (ExcavatorPlacement, HaulAssignment, EmployeeShift, ShiftClientAction)]

    def test_all_four_legacy_receipts_recover_after_close_without_inventing_context(self):
        for role in ('dispatcher', 'mining_master'):
            for action in ('move_excavator', 'assign_truck'):
                with self.subTest(role=role, action=action):
                    case = self.scenario(role, action)
                    self.assertEqual(self.post(case, context=None).status_code, 200)
                    case['shift'].closed_at = timezone.now()
                    case['shift'].save(update_fields=['closed_at'])
                    EmployeeShift.objects.create(employee=case['access'].employee, workplace_code=role,
                                                 opened_at=timezone.now(), shift_type='day')
                    before = self.snapshot()
                    result = self.lookup(case)
                    self.assertEqual(result.status_code, 200)
                    data = result.json()
                    self.assertEqual(data['status'], 'acknowledged')
                    self.assertEqual(data['evidence'], {'actor_id': case['access'].employee_id,
                        'shift_id': case['shift'].pk, 'action_type': case['route'],
                        'client_action_id': case['payload']['client_action_id']})
                    self.assertTrue(data['receipt']['deduplicated'])
                    self.assertNotIn('_request_signature', data['receipt'])
                    self.assertNotIn('_command_context', data['receipt'])
                    self.assertIn('no-store', result['Cache-Control'])
                    self.assertEqual(self.snapshot(), before)

    def test_unknown_command_is_not_executed_and_not_reported_as_rejected(self):
        case = self.scenario('mining_master')
        before = self.snapshot()
        self.assertEqual(self.lookup(case).json(), {'ok': True, 'status': 'unresolved'})
        self.assertEqual(self.snapshot(), before)

    def test_wrong_employee_payload_or_known_context_does_not_expose_receipt(self):
        case = self.scenario('dispatcher')
        self.assertEqual(self.post(case).status_code, 200)
        successor, _ = self.actor('dispatcher', 'successor', shift=False)
        before = self.snapshot()
        unresolved = {'ok': True, 'status': 'unresolved'}
        self.assertEqual(self.lookup(case, client=self.login(successor)).json(), unresolved)
        for field in ('actor_id', 'access_id', 'shift_id', 'role'):
            source = self.source(case)
            source['author'][field] = 'wrong'
            self.assertEqual(self.lookup(case, source).json(), unresolved)
        for field, value in [('id', 'wrong'), ('occurredAt', '2000-01-01T00:00:00Z')]:
            source = self.source(case)
            source[field] = value
            self.assertEqual(self.lookup(case, source).json(), unresolved)
        source = self.source(case)
        source['data']['zone'] = 'inactive'
        self.assertEqual(self.lookup(case, source).json(), unresolved)
        self.assertEqual(self.snapshot(), before)

    def test_modern_receipt_returns_only_original_context_even_with_new_shift(self):
        case = self.scenario('dispatcher')
        self.assertEqual(self.post(case).status_code, 200)
        source = self.source(case)
        result = self.lookup(case, source).json()
        self.assertEqual(result['status'], 'acknowledged')
        self.assertEqual(result['evidence']['command_context'], self.receipt(case).response_payload['_command_context'])
        self.assertEqual(source['author'], {'shift_id': ''})

    def test_missing_signature_or_unsuccessful_receipt_cannot_confirm(self):
        case = self.scenario('mining_master')
        self.assertEqual(self.post(case, context=None).status_code, 200)
        receipt = self.receipt(case)
        original = deepcopy(receipt.response_payload)
        for stored in ({'ok': True}, {**original, 'ok': False}):
            receipt.response_payload = stored
            receipt.save(update_fields=['response_payload'])
            self.assertEqual(self.lookup(case).json(), {'ok': True, 'status': 'unresolved'})

    def test_authentication_revocation_and_route_roles_are_enforced(self):
        case = self.scenario('mining_master')
        self.assertEqual(self.post(case).status_code, 200)
        self.assertEqual(self.lookup(case, client=Client()).status_code, 403)
        other, _ = self.actor('driver', 'wrong-role', shift=False)
        self.assertEqual(self.lookup(case, client=self.login(other)).status_code, 403)
        case['access'].is_active = False
        case['access'].save(update_fields=['is_active'])
        self.assertEqual(self.lookup(case).status_code, 409)  # общий middleware отзыва доступа

    def test_method_csrf_and_malformed_input_never_execute_commands(self):
        case = self.scenario('dispatcher')
        client = self.login(case['access'])
        self.assertEqual(client.get(reverse('assignment_command_receipt')).status_code, 405)
        csrf = Client(enforce_csrf_checks=True)
        csrf.cookies = client.cookies
        self.assertEqual(self.lookup(case, client=csrf).status_code, 403)
        before = self.snapshot()
        for source in ([], {}, {'kind': 'form'}, {**self.source(case), 'url': 'https://example.com/'},
                       {**self.source(case), 'url': []}, {**self.source(case), 'author': []},
                       {**self.source(case), 'data': {'client_action_id': 'x' * 129}},
                       {**self.source(case), 'extra': 'x' * 65537}):
            self.assertEqual(self.lookup(case, source).status_code, 400)
        self.assertEqual(self.snapshot(), before)
