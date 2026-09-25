import json
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.exceptions import ValidationError
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from assignments import views as assignment_views
from users.models import Employee, EmployeeAccess, Role

from . import dispatcher_guards
from . import views as trips_views


class DispatcherGuardContractTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_views_reexports_guard_contracts(self):
        for name in (
            'dispatcher_access_from_request',
            'dispatcher_client_action_error',
            'dispatcher_downtime_close_response',
            'dispatcher_equipment_detail_error',
            'dispatcher_json_payload',
            'dispatcher_shift_required_redirect',
            'dispatcher_shift_required_response',
            'get_dispatcher_action_redirect_url',
            'get_dispatcher_control_url',
            'protect_dispatcher_equipment_detail_response',
        ):
            self.assertIs(
                getattr(trips_views, name),
                getattr(dispatcher_guards, name),
            )
        self.assertIs(
            trips_views._lock_dispatcher_mutation_access,
            dispatcher_guards.lock_dispatcher_mutation_access,
        )

    def test_mining_master_keeps_dispatcher_renderer_contract(self):
        self.assertIs(
            assignment_views.render_dispatcher_control_view,
            trips_views.dispatcher_control_view,
        )

    def test_action_redirect_removes_internal_fragment_parameters(self):
        request = self.factory.post(
            '/dispatcher/shift/toggle/',
            {
                'next': (
                    '/dispatcher/control/?truck=7&'
                    '_operational_fragment=dispatcher&'
                    '_operational_version=44&show_active_trips=1'
                ),
            },
        )

        self.assertEqual(
            dispatcher_guards.get_dispatcher_action_redirect_url(request),
            '/dispatcher/control/?truck=7&show_active_trips=1',
        )

    def test_external_redirect_falls_back_to_filtered_control_url(self):
        request = self.factory.post(
            '/dispatcher/shift/toggle/?truck=9&show_pending_assignments=1',
            {'next': 'https://attacker.example/collect'},
        )

        self.assertEqual(
            dispatcher_guards.get_dispatcher_action_redirect_url(request),
            f'{reverse("dispatcher_control")}?truck=9&show_pending_assignments=1',
        )

    def test_json_payload_and_client_conflict_shape_are_stable(self):
        valid_request = self.factory.post(
            '/dispatcher/control/truck/assign/',
            data='{"client_action_id":"client-44"}',
            content_type='application/json',
        )
        invalid_request = self.factory.post(
            '/dispatcher/control/truck/assign/',
            data='{',
            content_type='application/json',
        )

        self.assertEqual(
            dispatcher_guards.dispatcher_json_payload(valid_request),
            {'client_action_id': 'client-44'},
        )
        self.assertEqual(
            dispatcher_guards.dispatcher_json_payload(invalid_request),
            {},
        )

        response = dispatcher_guards.dispatcher_client_action_error(
            {'client_action_id': 'client-44'},
            ValidationError(['Первая ошибка', 'Вторая ошибка']),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            json.loads(response.content),
            {
                'ok': False,
                'error': 'Первая ошибка; Вторая ошибка',
                'code': 'stale_client',
                'conflict': True,
                'client_action_id': 'client-44',
            },
        )

    def test_closed_shift_keeps_json_and_redirect_responses(self):
        access = object()
        request = self.factory.post('/dispatcher/shift/toggle/')
        request.session = {}
        request._messages = FallbackStorage(request)

        with patch(
            'trips.dispatcher_guards.get_active_dispatcher_shift',
            return_value=None,
        ):
            json_response = dispatcher_guards.dispatcher_shift_required_response(access)
            redirect_response = dispatcher_guards.dispatcher_shift_required_redirect(
                request,
                access,
                reverse('dispatcher_control'),
            )

        expected_error = (
            'Смена горного диспетчера закрыта. '
            'Изменения на пульте недоступны.'
        )
        self.assertEqual(json_response.status_code, 409)
        self.assertEqual(
            json.loads(json_response.content),
            {'ok': False, 'error': expected_error},
        )
        self.assertEqual(redirect_response.status_code, 302)
        self.assertEqual(redirect_response['Location'], reverse('dispatcher_control'))
        self.assertEqual(
            [str(message) for message in get_messages(request)],
            [expected_error],
        )

    def test_detail_and_downtime_responses_keep_contract_and_no_cache_headers(self):
        detail_error = dispatcher_guards.dispatcher_equipment_detail_error(
            'forbidden',
            status=403,
        )
        downtime_response = dispatcher_guards.dispatcher_downtime_close_response(
            {'ok': True, 'closed': True},
        )

        self.assertEqual(detail_error.status_code, 403)
        self.assertEqual(
            json.loads(detail_error.content),
            {
                'contract': 'dispatcher-equipment-detail-v1',
                'error': 'forbidden',
            },
        )
        self.assertEqual(
            json.loads(downtime_response.content),
            {
                'contract': 'dispatcher-downtime-close-v1',
                'ok': True,
                'closed': True,
            },
        )
        for response in (detail_error, downtime_response):
            with self.subTest(contract=json.loads(response.content)['contract']):
                self.assertEqual(
                    response['Cache-Control'],
                    'private, no-store, max-age=0',
                )
                self.assertEqual(response['Pragma'], 'no-cache')
                self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
                self.assertEqual(response['Vary'], 'Cookie')


class DispatcherGuardDatabaseTests(TestCase):
    def setUp(self):
        self.role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.employee = Employee.objects.create(
            full_name='Диспетчер теста guards',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.role,
            access_code='550055',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.request = RequestFactory().post('/dispatcher/control/')
        self.request.session = {'employee_access_id': self.access.id}

    def test_access_lookup_keeps_dispatcher_role_contract(self):
        resolved = dispatcher_guards.dispatcher_access_from_request(self.request)
        self.assertEqual(resolved, self.access)

        EmployeeAccess.objects.filter(pk=self.access.pk).update(is_active=False)
        self.assertIsNone(
            dispatcher_guards.dispatcher_access_from_request(self.request),
        )

    def test_views_wrapper_keeps_role_session_patch_seam(self):
        with patch(
            'trips.views.role_session_state',
            return_value={'is_active': False},
        ):
            self.assertIsNone(
                trips_views.lock_dispatcher_mutation_access(
                    self.request,
                    self.access,
                )
            )

        with patch(
            'trips.views.role_session_state',
            return_value={'is_active': True},
        ):
            locked_access = trips_views.lock_dispatcher_mutation_access(
                self.request,
                self.access,
            )

        self.assertEqual(locked_access, self.access)
