"""Границы и поведение POST-команды настроек техники Диспетчерского пульта."""

import inspect
import json
from decimal import Decimal
from unittest.mock import patch

from django.http import JsonResponse
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import resolve, reverse
from django.utils import timezone

from assignments.models import ExcavatorDumpPointSetting, ExcavatorPlacement
from core.models import OperationalStateEvent
from references.models import DumpPoint, Equipment, EquipmentType, RockType
from shifts.models import EmployeeShift
from users.models import Employee, EmployeeAccess, Role

from . import views as trips_views


class DispatcherEquipmentCommandBoundaryTests(SimpleTestCase):
    def test_url_keeps_public_views_facade(self):
        match = resolve(reverse(
            'dispatcher_equipment_detail',
            kwargs={'category': 'complex', 'equipment_id': 17},
        ))

        self.assertIs(match.func, trips_views.dispatcher_equipment_detail_view)

    def test_public_view_delegates_only_post_mutation_to_command(self):
        source = inspect.getsource(trips_views.dispatcher_equipment_detail_view)

        self.assertIn("if request.method == 'POST':", source)
        self.assertIn('_execute_dispatcher_equipment_settings(', source)
        self.assertIn('active_role_state=role_session_state', source)
        self.assertIn(
            'lock_mutation_access=lock_dispatcher_mutation_access',
            source,
        )
        self.assertIn('return dispatcher_control_view(', source)
        self.assertNotIn('lock_production_state', source)
        self.assertNotIn('bump_operational_state', source)
        self.assertNotIn('save_excavator_work_context(', source)
        self.assertNotIn('RockType.objects', source)


class DispatcherEquipmentFacadeDelegationTests(TestCase):
    def setUp(self):
        role = Role.objects.create(code='dispatcher', name='Диспетчер')
        employee = Employee.objects.create(
            full_name='Диспетчер фасада настроек',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code='EQUIPMENT-FACADE',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        equipment_type = EquipmentType.objects.create(name='Экскаватор')
        self.excavator = Equipment.objects.create(
            equipment_type=equipment_type,
            garage_number='EQUIPMENT-FACADE-1',
        )

    def test_post_facade_injects_views_compatibility_seams(self):
        request = RequestFactory().post(
            '/dispatcher/control/card/complex/17/',
            data='{}',
            content_type='application/json',
        )
        request.session = {'employee_access_id': self.access.id}
        expected = JsonResponse({'ok': True})

        with patch.object(
            trips_views,
            '_execute_dispatcher_equipment_settings',
            return_value=expected,
        ) as execute:
            response = trips_views.dispatcher_equipment_detail_view(
                request,
                'complex',
                self.excavator.id,
            )

        self.assertIs(response, expected)
        execute.assert_called_once()
        args, kwargs = execute.call_args
        self.assertIs(args[0], request)
        self.assertEqual(args[1].pk, self.access.pk)
        self.assertEqual(args[2].pk, self.excavator.pk)
        self.assertEqual(kwargs, {
            'active_role_state': trips_views.role_session_state,
            'active_shift_getter': trips_views.get_active_dispatcher_shift,
            'json_payload': trips_views.excavator_json_payload,
            'parse_destinations': trips_views.parse_excavator_destinations,
            'normalize_numeric_setting': (
                trips_views.normalize_excavator_numeric_setting
            ),
            'lock_mutation_access': trips_views.lock_dispatcher_mutation_access,
            'error_response': trips_views.dispatcher_equipment_detail_error,
            'save_work_context': trips_views.save_excavator_work_context,
            'build_settings': trips_views.dispatcher_excavator_settings,
            'protect_response': (
                trips_views.protect_dispatcher_equipment_detail_response
            ),
        })


class DispatcherEquipmentCommandBehaviorTests(TestCase):
    def setUp(self):
        role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.dispatcher = Employee.objects.create(
            full_name='Диспетчер настроек техники',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=role,
            access_code='EQUIPMENT-COMMAND',
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
        equipment_type = EquipmentType.objects.create(name='Экскаватор')
        self.excavator = Equipment.objects.create(
            equipment_type=equipment_type,
            garage_number='EQUIPMENT-COMMAND-1',
        )
        self.placement = ExcavatorPlacement.objects.create(
            excavator=self.excavator,
            zone=ExcavatorPlacement.Zone.ACTIVE,
            changed_by=self.dispatcher,
        )
        self.rock = RockType.objects.create(
            name='Скальная порода',
            density='2.6000',
            loosening_factor='1.5000',
        )
        self.first_dump = DumpPoint.objects.create(name='Отвал команды 1')
        self.second_dump = DumpPoint.objects.create(name='Отвал команды 2')
        session = self.client.session
        session['employee_access_id'] = access.id
        session.save()
        self.url = reverse(
            'dispatcher_equipment_detail',
            kwargs={
                'category': 'complex',
                'equipment_id': self.excavator.id,
            },
        )

    def post_settings(self, payload):
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_ACCEPT='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def test_post_saves_settings_and_returns_protected_v2_contract(self):
        initial_version = trips_views.get_operational_state_version()
        response = self.post_settings({
            'state_version': initial_version,
            'rock_type_id': self.rock.id,
            'destinations': [
                {
                    'dump_point_id': self.first_dump.id,
                    'transport_distance_km': '3.75',
                },
                {
                    'dump_point_id': self.second_dump.id,
                    'transport_distance_km': '',
                },
            ],
            'loading_horizon': '75',
            'loading_block': '52',
        })

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['contract'], 'dispatcher-equipment-settings-v2')
        self.assertEqual(payload['equipment_id'], self.excavator.id)
        self.assertGreater(payload['version'], initial_version)
        self.assertEqual(response['Cache-Control'], 'private, no-store, max-age=0')
        self.assertEqual(response['Pragma'], 'no-cache')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response['Vary'], 'Cookie')

        self.placement.refresh_from_db()
        self.assertEqual(self.placement.work_rock_type, self.rock)
        self.assertEqual(self.placement.work_dump_point, self.first_dump)
        self.assertEqual(self.placement.transport_distance_km, Decimal('3.75'))
        self.assertEqual(self.placement.loading_horizon, '75')
        self.assertEqual(self.placement.loading_block, '52')
        settings = list(
            ExcavatorDumpPointSetting.objects
            .filter(placement=self.placement)
            .order_by('position')
        )
        self.assertEqual(
            [row.dump_point_id for row in settings],
            [self.first_dump.id, self.second_dump.id],
        )
        self.assertEqual(settings[0].transport_distance_km, Decimal('3.75'))
        self.assertIsNone(settings[1].transport_distance_km)
        event = OperationalStateEvent.objects.get(
            payload__action='dispatcher_excavator_work_settings',
        )
        self.assertEqual(event.version, payload['version'])
        self.assertEqual(event.event_type, 'equipment_changed')
        self.assertEqual(
            event.payload['action'],
            'dispatcher_excavator_work_settings',
        )
        self.assertEqual(
            event.payload['dump_point_ids'],
            [self.first_dump.id, self.second_dump.id],
        )

    def test_stale_version_does_not_overwrite_saved_settings(self):
        initial_version = trips_views.get_operational_state_version()
        initial_command_event_count = OperationalStateEvent.objects.filter(
            payload__action='dispatcher_excavator_work_settings',
        ).count()
        first_response = self.post_settings({
            'state_version': initial_version,
            'rock_type_id': self.rock.id,
            'dump_point_ids': [self.first_dump.id],
            'dump_point_distances': {str(self.first_dump.id): '4.25'},
            'loading_horizon': '75',
            'loading_block': '52',
        })
        self.assertEqual(first_response.status_code, 200, first_response.content)

        stale_response = self.post_settings({
            'state_version': initial_version,
            'rock_type_id': self.rock.id,
            'dump_point_ids': [self.second_dump.id],
            'dump_point_distances': {str(self.second_dump.id): '9.50'},
            'loading_horizon': '90',
            'loading_block': '60',
        })

        self.assertEqual(stale_response.status_code, 409)
        self.assertEqual(stale_response.json(), {
            'contract': 'dispatcher-equipment-detail-v1',
            'error': 'stale_board',
        })
        self.placement.refresh_from_db()
        self.assertEqual(self.placement.work_dump_point, self.first_dump)
        self.assertEqual(self.placement.transport_distance_km, Decimal('4.25'))
        self.assertEqual(self.placement.loading_horizon, '75')
        self.assertEqual(self.placement.loading_block, '52')
        self.assertEqual(
            list(self.placement.dump_point_settings.values_list(
                'dump_point_id',
                flat=True,
            )),
            [self.first_dump.id],
        )
        self.assertEqual(
            OperationalStateEvent.objects.filter(
                payload__action='dispatcher_excavator_work_settings',
            ).count(),
            initial_command_event_count + 1,
        )
