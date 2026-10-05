"""Автор команды не заменяется cookie, а квитанция остаётся в исходной смене."""
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import ExcavatorPlacement, HaulAssignment
from core.operational_fragments import extract_outer_html
from references.models import Equipment, EquipmentType
from shifts.models import EmployeeShift, ShiftClientAction
from users.models import Employee, EmployeeAccess, Role


class ContextFixture:
    def actor(self, role, suffix, *, shift=True):
        role, _ = Role.objects.get_or_create(code=role, defaults={'name': role})
        employee = Employee.objects.create(full_name='Автор ' + suffix, status=Employee.Status.ACTIVE)
        access = EmployeeAccess.objects.create(employee=employee, role=role, access_code=suffix,
                                               status=EmployeeAccess.Status.ACTIVATED, is_active=True)
        current = EmployeeShift.objects.create(employee=employee, workplace_code=role.code,
                                               opened_at=timezone.now(), opened_by=employee, shift_type='day') if shift else None
        return access, current

    def scenario(self, role, action='move_excavator'):
        access, shift = self.actor(role, role + action)
        kind, _ = EquipmentType.objects.get_or_create(name='Экскаватор')
        excavator = Equipment.objects.create(equipment_type=kind, garage_number=role + action)
        placement = ExcavatorPlacement.objects.create(excavator=excavator,
            zone=ExcavatorPlacement.Zone.INACTIVE if action == 'move_excavator' else ExcavatorPlacement.Zone.ACTIVE)
        payload = {'client_action_id': role + action, 'excavator_id': excavator.pk,
                   'zone': 'active', 'expected_zone': 'inactive', 'expected_assignment_states': {}}
        if action == 'assign_truck':
            kind, _ = EquipmentType.objects.get_or_create(name='Самосвал')
            truck = Equipment.objects.create(equipment_type=kind, garage_number=role)
            payload = {'client_action_id': role + action, 'action': 'assign', 'truck_id': truck.pk,
                       'excavator_id': excavator.pk, 'expected_assignment_state_id': 0}
        context = {'version': 1, 'id': 'sync-' + payload['client_action_id'], 'occurred_at': timezone.now().isoformat(),
                   'author': {'actor_id': str(access.employee_id), 'access_id': str(access.pk),
                              'role': role, 'shift_id': str(shift.pk)}}
        return {'access': access, 'shift': shift, 'placement': placement, 'payload': payload,
                'context': context, 'route': role + '_' + action}

    def login(self, access):
        client = Client()
        session = client.session
        session['employee_access_id'] = access.pk
        session.save()
        return client

    def post(self, case, client=None, context=True):
        header = case['context'] if context is True else context
        headers = {} if header is None else {'HTTP_X_COMMAND_CONTEXT': header if isinstance(header, str) else json.dumps(header)}
        return (client or self.login(case['access'])).post(reverse(case['route']),
            data=json.dumps(case['payload']), content_type='application/json', **headers)

    def receipt(self, case):
        return ShiftClientAction.objects.get(client_action_id=case['payload']['client_action_id'])


class BoundCommandContextTests(ContextFixture, TestCase):
    def test_changed_cookie_cannot_apply_any_of_four_commands_as_the_next_employee(self):
        for role in ('dispatcher', 'mining_master'):
            for action in ('move_excavator', 'assign_truck'):
                with self.subTest(role=role, action=action):
                    case = self.scenario(role, action)
                    successor, _ = self.actor(role, 'next-' + role + action, shift=False)
                    before = (list(ExcavatorPlacement.objects.values()), list(HaulAssignment.objects.values()))
                    response = self.post(case, self.login(successor))
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json()['code'], 'command_author_mismatch')
                    self.assertEqual(before, (list(ExcavatorPlacement.objects.values()), list(HaulAssignment.objects.values())))
                    self.assertFalse(ShiftClientAction.objects.filter(client_action_id=case['payload']['client_action_id']).exists())
                    response = self.post(case)
                    self.assertEqual(response.status_code, 200, response.content)
                    receipt = self.receipt(case)
                    self.assertEqual(receipt.employee_id, case['access'].employee_id)
                    self.assertEqual(receipt.shift_id, case['shift'].pk)

    def test_same_author_new_session_is_allowed_but_wrong_employee_role_or_shift_is_not(self):
        for role in ('dispatcher', 'mining_master'):
            case = self.scenario(role)
            for field, value in [('actor_id', '999999'), ('role', 'manager'), ('shift_id', '999999')]:
                with self.subTest(role=role, field=field):
                    changed = deepcopy(case['context'])
                    changed['author'][field] = value
                    response = self.post(case, context=changed)
                    self.assertEqual(response.status_code, 409)
                    self.assertIn(response.json()['code'], ['command_author_mismatch', 'command_shift_mismatch'])
            case['access'].last_login_at = timezone.now()
            case['access'].save(update_fields=['last_login_at'])
            self.assertEqual(self.post(case, self.login(case['access'])).status_code, 200)
            saved = self.receipt(case).response_payload['_command_context']
            self.assertEqual(saved['occurred_at'], case['context']['occurred_at'])
            self.assertEqual(saved['author']['actor_id'], case['access'].employee_id)

    def test_lost_ack_after_shift_change_returns_original_receipt_without_new_effect(self):
        for role in ('dispatcher', 'mining_master'):
            case = self.scenario(role)
            self.assertEqual(self.post(case).status_code, 200)
            saved = deepcopy(self.receipt(case).response_payload)
            case['shift'].closed_at = timezone.now()
            case['shift'].save(update_fields=['closed_at'])
            next_shift = EmployeeShift.objects.create(employee=case['access'].employee, workplace_code=role,
                opened_at=timezone.now(), shift_type='day')
            repeated = self.post(case)
            self.assertEqual(repeated.status_code, 200, repeated.content)
            self.assertTrue(repeated.json()['deduplicated'])
            self.assertNotIn('_command_context', repeated.json())
            self.assertNotIn('_request_signature', repeated.json())
            self.assertEqual(self.receipt(case).response_payload, saved)
            self.assertEqual(self.receipt(case).shift_id, case['shift'].pk)
            case['payload']['client_action_id'] += '-late'
            response = self.post(case)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()['code'], 'command_shift_mismatch')
            self.assertFalse(ShiftClientAction.objects.filter(shift=next_shift).exists())

    def test_accepted_context_is_immutable_and_old_payload_fingerprints_remain_compatible(self):
        case = self.scenario('mining_master')
        self.assertEqual(self.post(case).status_code, 200)
        saved = deepcopy(self.receipt(case).response_payload)
        for field, value in [('occurred_at', '2026-10-05T00:00:00+00:00'), ('id', 'different-request')]:
            changed = deepcopy(case['context'])
            changed[field] = value
            self.assertEqual(self.post(case, context=changed).json()['code'], 'command_context_changed')
        self.assertEqual(self.receipt(case).response_payload, saved)
        legacy = self.post(case, context=None)
        self.assertEqual(legacy.status_code, 200)
        self.assertTrue(legacy.json()['deduplicated'])
        self.assertNotIn('_command_context', legacy.json())

    def test_incomplete_or_malformed_context_never_silently_falls_back_to_the_cookie(self):
        case = self.scenario('dispatcher')
        for context in ['', '{broken', '[]', '{}', 'x' * 4097,
                        {**case['context'], 'version': True},
                        {**case['context'], 'author': {'access_id': str(case['access'].pk), 'role': 'dispatcher', 'shift_id': ''}},
                        {**case['context'], 'occurred_at': 'not-a-date'}]:
            with self.subTest(context=str(context)[:50]):
                response = self.post(case, context=context)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()['code'], 'command_context_invalid')
        self.assertFalse(ShiftClientAction.objects.exists())
        case['placement'].refresh_from_db()
        self.assertEqual(case['placement'].zone, 'inactive')

    def test_revocation_and_missing_login_do_not_grant_rights_from_the_saved_header(self):
        case = self.scenario('dispatcher')
        self.assertEqual(self.post(case, Client()).status_code, 403)
        case['access'].is_active = False
        case['access'].save(update_fields=['is_active'])
        self.assertIn(self.post(case).status_code, [403, 409])
        self.assertFalse(ShiftClientAction.objects.exists())

    def test_admin_keeps_actual_author_role_while_using_the_dispatcher_workplace_shift(self):
        case = self.scenario('dispatcher')
        access, _ = self.actor('admin', 'admin-author', shift=False)
        case['access'] = access
        case['context']['author'].update(actor_id=str(access.employee_id), access_id=str(access.pk), role='admin')
        response = self.post(case)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.receipt(case).employee_id, access.employee_id)
        self.assertEqual(self.receipt(case).shift_id, case['shift'].pk)

    def test_unexpected_receipt_shift_rolls_back_the_whole_domain_change(self):
        from assignments.command_guards import complete_client_action
        case = self.scenario('mining_master')
        _, other_shift = self.actor('mining_master', 'unexpected-shift')
        def wrong_shift(**kwargs):
            return complete_client_action(**{**kwargs, 'shift': other_shift})
        with patch('assignments.views.complete_client_action', side_effect=wrong_shift):
            response = self.post(case)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'command_context_changed')
        self.assertFalse(ShiftClientAction.objects.exists())
        case['placement'].refresh_from_db()
        self.assertEqual(case['placement'].zone, 'inactive')
        self.assertEqual(self.post(case).status_code, 200)

    def test_full_page_and_realtime_fragment_carry_the_actual_author_and_shift(self):
        for role in ('dispatcher', 'mining_master'):
            case = self.scenario(role)
            route = 'dispatcher_control' if role == 'dispatcher' else 'mining_master_assignments'
            selector = '.dispatcher-board' if role == 'dispatcher' else '.mm-mobile-shell'
            client = self.login(case['access'])
            full = client.get(reverse(route))
            self.assertEqual(full.status_code, 200)
            fragment = client.get(reverse(route), {'_operational_fragment': role})
            self.assertEqual(fragment.status_code, 200)
            for html in [extract_outer_html(full.content.decode(), selector), fragment.json()['html']]:
                self.assertIn(f'data-dispatcher-command-actor-id="{case["access"].employee_id}"', html)
                self.assertIn(f'data-dispatcher-command-access-id="{case["access"].pk}"', html)
                self.assertIn(f'data-dispatcher-command-shift-id="{case["shift"].pk}"', html)
                self.assertIn(f'data-dispatcher-command-role="{role}"', html)


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL row locks required')
class BoundCommandContextPostgreSQLTests(ContextFixture, TransactionTestCase):
    def concurrent_duplicate(self, role):
        case = self.scenario(role)
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
        self.assertEqual(ShiftClientAction.objects.count(), 1)
        self.assertEqual(self.receipt(case).response_payload['_command_context']['author']['shift_id'], case['shift'].pk)

    def test_dispatcher_concurrent_duplicate_keeps_one_context_and_effect(self):
        self.concurrent_duplicate('dispatcher')

    def test_master_concurrent_duplicate_keeps_one_context_and_effect(self):
        self.concurrent_duplicate('mining_master')
