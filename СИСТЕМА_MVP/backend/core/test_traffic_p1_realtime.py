import time
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    EquipmentAssignment,
    HaulAssignment,
    WorkShiftType,
)
from assignments.services import (
    get_or_create_crew_draft,
    publish_crew_plan,
    update_crew_draft_slot,
)
from references.models import Equipment, EquipmentType
from shifts.models import EmployeeShift, ShiftType
from users.models import Employee, EmployeeAccess, Role

from .models import OperationalStateEvent, OperationalStateVersion, bump_operational_state
from .realtime import MAX_EVENT_SCAN, relevant_event_delta
from .traffic_test_worker import run_reconcile_server_gate_worker


class RealtimeEventScopeTrafficRegressionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        cls.driver_truck = Equipment.objects.create(
            equipment_type=truck_type,
            garage_number='TRAFFIC-TRUCK-1',
        )
        cls.other_truck = Equipment.objects.create(
            equipment_type=truck_type,
            garage_number='TRAFFIC-TRUCK-2',
        )
        cls.linked_excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='TRAFFIC-EXCAVATOR-1',
        )
        cls.foreign_excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='TRAFFIC-EXCAVATOR-2',
        )
        cls.driver_access = cls.create_access('driver', 'Водитель трафика', '92000000001')
        cls.foreign_driver_access = cls.create_access(
            'driver',
            'Чужой водитель трафика',
            '92000000004',
        )
        cls.operator_access = cls.create_access(
            'excavator_operator',
            'Связанный машинист трафика',
            '92000000005',
        )
        cls.foreign_operator_access = cls.create_access(
            'excavator_operator',
            'Чужой машинист трафика',
            '92000000006',
        )
        cls.dispatcher_access = cls.create_access(
            'dispatcher',
            'Диспетчер трафика',
            '92000000002',
        )
        cls.admin_access = cls.create_access('admin', 'Администратор трафика', '92000000003')
        cls.driver_shift = EmployeeShift.objects.create(
            employee=cls.driver_access.employee,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            equipment=cls.driver_truck,
            opened_at=timezone.now(),
        )
        cls.foreign_driver_shift = EmployeeShift.objects.create(
            employee=cls.foreign_driver_access.employee,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            equipment=cls.other_truck,
            opened_at=timezone.now(),
        )
        cls.operator_shift = EmployeeShift.objects.create(
            employee=cls.operator_access.employee,
            shift_type=ShiftType.DAY,
            workplace_code='excavator_operator',
            equipment=cls.linked_excavator,
            opened_at=timezone.now(),
        )
        cls.foreign_operator_shift = EmployeeShift.objects.create(
            employee=cls.foreign_operator_access.employee,
            shift_type=ShiftType.DAY,
            workplace_code='excavator_operator',
            equipment=cls.foreign_excavator,
            opened_at=timezone.now(),
        )
        cls.linked_haul_assignment = HaulAssignment.objects.create(
            excavator=cls.linked_excavator,
            truck=cls.driver_truck,
            status=AssignmentStatus.ACCEPTED,
        )
        cls.foreign_haul_assignment = HaulAssignment.objects.create(
            excavator=cls.foreign_excavator,
            truck=cls.other_truck,
            status=AssignmentStatus.ACCEPTED,
        )

    @classmethod
    def create_access(cls, role_code, full_name, phone):
        role, _ = Role.objects.get_or_create(
            code=role_code,
            defaults={'name': role_code, 'is_active': True},
        )
        employee = Employee.objects.create(
            full_name=full_name,
            phone=phone,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        return EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code=phone[-6:],
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    def setUp(self):
        self.url = reverse('operational_state_version')
        self.reconcile_lock_dir = TemporaryDirectory()
        self.addCleanup(self.reconcile_lock_dir.cleanup)
        self.reconcile_lock_patch = patch(
            'assignments.services.RECONCILE_LOCK_PATH',
            Path(self.reconcile_lock_dir.name) / 'reconcile.lock',
        )
        self.reconcile_lock_patch.start()
        self.addCleanup(self.reconcile_lock_patch.stop)

    def authorize(self, access):
        client = Client()
        session = client.session
        session['employee_access_id'] = access.id
        session.save()
        return client

    @staticmethod
    def current_version():
        return OperationalStateVersion.objects.get(key='production').version

    def emit_trip(self, *, truck, action='truck_loaded', object_id='501'):
        return bump_operational_state(
            f'Trip:{action}',
            event_type='trip_changed',
            object_type='Trip',
            object_id=object_id,
            payload={
                'action': action,
                'trip_id': int(object_id),
                'truck_id': truck.id,
                'excavator_id': 700,
                'status': 'loaded_waiting_unload',
            },
        )

    def test_driver_receives_only_events_for_own_truck_in_same_state_response(self):
        after = self.current_version()
        self.emit_trip(truck=self.other_truck, object_id='501')
        relevant = self.emit_trip(truck=self.driver_truck, object_id='502')

        response = self.authorize(self.driver_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['version'], relevant.version)
        self.assertEqual([event['object_id'] for event in payload['events']], ['502'])
        self.assertEqual(payload['events'][0]['payload']['truck_id'], self.driver_truck.id)

    def test_published_crew_plan_reaches_removed_and_new_but_not_unchanged_worker(self):
        driver_role = self.driver_access.role
        removed_access = self.create_access(
            'driver',
            'Снятый водитель расстановки',
            '92000000011',
        )
        unchanged_access = self.create_access(
            'driver',
            'Неизменённый водитель расстановки',
            '92000000012',
        )
        new_access = self.create_access(
            'driver',
            'Новый водитель расстановки',
            '92000000013',
        )
        third_truck = Equipment.objects.create(
            equipment_type=self.driver_truck.equipment_type,
            garage_number='TRAFFIC-TRUCK-3',
        )
        actor = Employee.objects.create(
            full_name='Публикатор расстановки',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EquipmentAssignment.objects.create(
            employee=removed_access.employee,
            role=driver_role,
            equipment=self.driver_truck,
            shift_type=WorkShiftType.SHIFT_1,
            status=AssignmentStatus.ACCEPTED,
            assigned_by=actor,
            accepted_at=timezone.now(),
        )
        EquipmentAssignment.objects.create(
            employee=unchanged_access.employee,
            role=driver_role,
            equipment=self.other_truck,
            shift_type=WorkShiftType.SHIFT_1,
            status=AssignmentStatus.ACCEPTED,
            assigned_by=actor,
            accepted_at=timezone.now(),
        )
        plan, _created = get_or_create_crew_draft(role=driver_role, actor=actor)
        plan = update_crew_draft_slot(
            plan=plan,
            equipment=self.driver_truck,
            shift_type=WorkShiftType.SHIFT_1,
            employee=None,
            expected_version=plan.version,
            actor=actor,
        )
        plan = update_crew_draft_slot(
            plan=plan,
            equipment=third_truck,
            shift_type=WorkShiftType.SHIFT_1,
            employee=new_access.employee,
            expected_version=plan.version,
            actor=actor,
        )
        after = self.current_version()

        publish_crew_plan(
            plan=plan,
            expected_version=plan.version,
            actor=actor,
        )

        results = {}
        for label, access in (
            ('removed', removed_access),
            ('new', new_access),
            ('unchanged', unchanged_access),
        ):
            response = self.authorize(access).get(
                self.url,
                {'after': after, 'include_events': '1'},
            )
            self.assertEqual(response.status_code, 200)
            results[label] = [
                event
                for event in response.json()['events']
                if event['payload'].get('action') == 'crew_plan_published'
            ]

        self.assertEqual(len(results['removed']), 1)
        self.assertEqual(len(results['new']), 1)
        self.assertEqual(results['unchanged'], [])
        changed_payload = results['removed'][0]['payload']
        self.assertEqual(
            set(changed_payload['employee_ids']),
            {removed_access.employee_id, new_access.employee_id},
        )
        self.assertEqual(
            set(changed_payload['equipment_ids']),
            {self.driver_truck.id, third_truck.id},
        )

    def test_driver_crew_plan_event_does_not_reach_other_role_of_same_employee(self):
        driver_role = self.driver_access.role
        operator_access = EmployeeAccess.objects.create(
            employee=self.driver_access.employee,
            role=self.operator_access.role,
            access_code='same-employee-operator',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        actor = Employee.objects.create(
            full_name='Публикатор ролевой области расстановки',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EquipmentAssignment.objects.create(
            employee=self.driver_access.employee,
            role=driver_role,
            equipment=self.driver_truck,
            shift_type=WorkShiftType.SHIFT_1,
            status=AssignmentStatus.ACCEPTED,
            assigned_by=actor,
            accepted_at=timezone.now(),
        )
        plan, _created = get_or_create_crew_draft(role=driver_role, actor=actor)
        plan = update_crew_draft_slot(
            plan=plan,
            equipment=self.driver_truck,
            shift_type=WorkShiftType.SHIFT_1,
            employee=None,
            expected_version=plan.version,
            actor=actor,
        )
        after = self.current_version()

        publish_crew_plan(
            plan=plan,
            expected_version=plan.version,
            actor=actor,
        )

        driver_payload = self.authorize(self.driver_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        ).json()
        operator_payload = self.authorize(operator_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        ).json()
        self.assertTrue(driver_payload['relevant'])
        self.assertEqual(
            [
                event['payload']['action']
                for event in driver_payload['events']
                if event['payload'].get('action') == 'crew_plan_published'
            ],
            ['crew_plan_published'],
        )
        self.assertFalse(operator_payload['relevant'])
        self.assertEqual(operator_payload['events'], [])

    def test_personal_access_event_still_reaches_other_role_of_same_employee(self):
        operator_access = EmployeeAccess.objects.create(
            employee=self.driver_access.employee,
            role=self.operator_access.role,
            access_code='same-employee-access-event',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        after = self.current_version()

        self.driver_access.save()

        payload = self.authorize(operator_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        ).json()
        self.assertTrue(payload['relevant'])
        self.assertEqual([event['type'] for event in payload['events']], ['access_changed'])
        self.assertEqual(
            payload['events'][0]['payload']['employee_id'],
            self.driver_access.employee_id,
        )

    def test_unchanged_opposite_shift_worker_on_same_equipment_gets_no_crew_plan_event(self):
        driver_role = self.driver_access.role
        changed_day_access = self.create_access(
            'driver',
            'Снятый дневной водитель расстановки',
            '92000000014',
        )
        new_day_access = self.create_access(
            'driver',
            'Новый дневной водитель расстановки',
            '92000000015',
        )
        unchanged_night_access = self.create_access(
            'driver',
            'Неизменённый ночной водитель расстановки',
            '92000000016',
        )
        actor = Employee.objects.create(
            full_name='Публикатор посменной области расстановки',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EquipmentAssignment.objects.bulk_create([
            EquipmentAssignment(
                employee=changed_day_access.employee,
                role=driver_role,
                equipment=self.driver_truck,
                shift_type=WorkShiftType.SHIFT_1,
                status=AssignmentStatus.ACCEPTED,
                assigned_by=actor,
                accepted_at=timezone.now(),
            ),
            EquipmentAssignment(
                employee=unchanged_night_access.employee,
                role=driver_role,
                equipment=self.driver_truck,
                shift_type=WorkShiftType.SHIFT_2,
                status=AssignmentStatus.ACCEPTED,
                assigned_by=actor,
                accepted_at=timezone.now(),
            ),
        ])
        plan, _created = get_or_create_crew_draft(role=driver_role, actor=actor)
        plan = update_crew_draft_slot(
            plan=plan,
            equipment=self.driver_truck,
            shift_type=WorkShiftType.SHIFT_1,
            employee=new_day_access.employee,
            expected_version=plan.version,
            actor=actor,
        )
        after = self.current_version()

        publish_crew_plan(
            plan=plan,
            expected_version=plan.version,
            actor=actor,
        )

        results = {}
        for label, access in (
            ('removed_day', changed_day_access),
            ('new_day', new_day_access),
            ('unchanged_night', unchanged_night_access),
        ):
            payload = self.authorize(access).get(
                self.url,
                {'after': after, 'include_events': '1'},
            ).json()
            results[label] = [
                event
                for event in payload['events']
                if event['payload'].get('action') == 'crew_plan_published'
            ]

        self.assertEqual(len(results['removed_day']), 1)
        self.assertEqual(len(results['new_day']), 1)
        self.assertEqual(results['unchanged_night'], [])

    def test_explicit_empty_crew_plan_scope_does_not_fall_back_to_every_worker(self):
        after = self.current_version()
        state = bump_operational_state(
            'CrewPlan:published',
            event_type='personnel_assignment_changed',
            object_type='CrewPlan',
            object_id='901',
            payload={
                'action': 'crew_plan_published',
                'role_code': 'driver',
                'employee_ids': [],
                'equipment_ids': [],
            },
        )

        response = self.authorize(self.driver_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['version'], state.version)
        self.assertFalse(response.json()['relevant'])
        self.assertEqual(response.json()['events'], [])

    def test_irrelevant_trip_does_not_reach_driver_or_system_admin(self):
        after = self.current_version()
        state = self.emit_trip(truck=self.other_truck)

        for access in (self.driver_access, self.admin_access):
            with self.subTest(role=access.role.code):
                response = self.authorize(access).get(
                    self.url,
                    {'after': after, 'include_events': '1'},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['version'], state.version)
                self.assertEqual(response.json()['events'], [])
                self.assertFalse(response.json()['events_truncated'])

    def test_same_role_foreign_access_and_assignment_events_stay_irrelevant(self):
        after = self.current_version()
        bump_operational_state(
            'EmployeeAccess:save',
            event_type='access_changed',
            object_type='EmployeeAccess',
            object_id=str(self.foreign_driver_access.id),
            payload={
                'action': 'save',
                'employee_id': self.foreign_driver_access.employee_id,
                'access_id': self.foreign_driver_access.id,
                'role_code': 'driver',
            },
        )
        state = bump_operational_state(
            'EquipmentAssignment:save',
            event_type='personnel_assignment_changed',
            object_type='EquipmentAssignment',
            object_id='901',
            payload={
                'action': 'save',
                'employee_id': self.foreign_driver_access.employee_id,
                'equipment_id': self.other_truck.id,
                'role_code': 'driver',
            },
        )

        response = self.authorize(self.driver_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        )

        self.assertEqual(response.json()['version'], state.version)
        self.assertFalse(response.json()['relevant'])
        self.assertEqual(response.json()['events'], [])

    def test_complex_scope_links_truck_and_excavator_events_only_to_assigned_workers(self):
        after = self.current_version()
        truck_state = bump_operational_state(
            'DowntimeEvent:started',
            event_type='downtime_changed',
            object_type='DowntimeEvent',
            object_id='801',
            payload={
                'action': 'downtime_started',
                'equipment_id': self.driver_truck.id,
            },
        )

        linked_operator = self.authorize(self.operator_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        ).json()
        foreign_operator = self.authorize(self.foreign_operator_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        ).json()
        self.assertEqual(linked_operator['version'], truck_state.version)
        self.assertTrue(linked_operator['relevant'])
        self.assertFalse(foreign_operator['relevant'])

        after = truck_state.version
        excavator_state = bump_operational_state(
            'EmployeeShift:changed',
            event_type='shift_changed',
            object_type='EmployeeShift',
            object_id='802',
            payload={
                'action': 'save',
                'equipment_id': self.linked_excavator.id,
            },
        )
        linked_driver = self.authorize(self.driver_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        ).json()
        foreign_driver = self.authorize(self.foreign_driver_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        ).json()
        self.assertEqual(linked_driver['version'], excavator_state.version)
        self.assertTrue(linked_driver['relevant'])
        self.assertFalse(foreign_driver['relevant'])

    def test_linked_driver_access_change_reaches_only_its_complex_operator(self):
        after = self.current_version()
        self.driver_access.save()

        linked_operator = self.authorize(self.operator_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        ).json()
        foreign_operator = self.authorize(self.foreign_operator_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        ).json()

        self.assertTrue(linked_operator['relevant'])
        self.assertEqual(linked_operator['events'][0]['type'], 'access_changed')
        self.assertIn(
            self.driver_truck.id,
            linked_operator['events'][0]['payload']['equipment_ids'],
        )
        self.assertFalse(foreign_operator['relevant'])

    def test_release_disabled_excavator_event_reaches_released_truck_driver_only(self):
        after = self.current_version()
        self.linked_excavator.is_active = False
        self.linked_excavator.save(update_fields=['is_active'])

        linked_driver = self.authorize(self.driver_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        ).json()
        foreign_driver = self.authorize(self.foreign_driver_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        ).json()

        released_events = [
            event
            for event in linked_driver['events']
            if event['payload'].get('action') == 'release_disabled_excavator'
        ]
        self.assertEqual(len(released_events), 1)
        self.assertEqual(released_events[0]['payload']['truck_ids'], [self.driver_truck.id])
        self.assertTrue(linked_driver['relevant'])
        self.assertFalse(foreign_driver['relevant'])

    def test_dispatcher_receives_operational_trip_delta(self):
        after = self.current_version()
        self.emit_trip(truck=self.other_truck)

        response = self.authorize(self.dispatcher_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([event['type'] for event in response.json()['events']], ['trip_changed'])

    def test_generic_save_event_is_collapsed_when_enriched_event_follows(self):
        after = self.current_version()
        bump_operational_state(
            'Trip:save',
            event_type='trip_changed',
            object_type='Trip',
            object_id='503',
            payload={
                'action': 'save',
                'trip_id': 503,
                'truck_id': self.driver_truck.id,
                'excavator_id': 700,
            },
        )
        self.emit_trip(truck=self.driver_truck, object_id='503')

        response = self.authorize(self.driver_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        )

        self.assertEqual(len(response.json()['events']), 1)
        self.assertEqual(response.json()['events'][0]['payload']['action'], 'truck_loaded')

    def test_legacy_unscoped_production_event_is_not_silently_skipped(self):
        after = self.current_version()
        bump_operational_state(
            'Trip:legacy',
            event_type='trip_changed',
            object_type='Trip',
            object_id='504',
            payload={'action': 'save'},
        )

        response = self.authorize(self.driver_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        )

        self.assertTrue(response.json()['relevant'])
        self.assertEqual(response.json()['events'][0]['object_id'], '504')

    def test_test_shift_reset_reaches_open_production_and_admin_screens(self):
        after = self.current_version()
        state = bump_operational_state(
            'SystemAdmin:test_shift_data_reset',
            event_type='test_shift_data_reset',
            object_type='SystemAdmin',
            payload={'action': 'test_shift_data_reset'},
        )

        for access in (
            self.driver_access,
            self.operator_access,
            self.dispatcher_access,
            self.admin_access,
        ):
            with self.subTest(role=access.role.code):
                payload = self.authorize(access).get(
                    self.url,
                    {'after': after, 'include_events': '1'},
                ).json()
                self.assertEqual(payload['version'], state.version)
                self.assertTrue(payload['relevant'])
                self.assertEqual(payload['events'][0]['type'], 'test_shift_data_reset')

    def test_delta_never_contains_event_newer_than_response_state_version(self):
        after = self.current_version()
        state = bump_operational_state(
            'Trip:visible',
            event_type='trip_changed',
            object_type='Trip',
            object_id='505',
            payload={'action': 'save', 'truck_id': self.driver_truck.id},
        )
        OperationalStateEvent.objects.create(
            key='production',
            version=state.version + 1,
            event_type='trip_changed',
            object_type='Trip',
            object_id='506',
            reason='simulated concurrent commit',
            payload={'action': 'save', 'truck_id': self.driver_truck.id},
        )

        response = self.authorize(self.driver_access).get(
            self.url,
            {'after': after, 'include_events': '1'},
        )

        payload = response.json()
        self.assertEqual(payload['version'], state.version)
        self.assertEqual([event['version'] for event in payload['events']], [state.version])

    def test_stale_client_event_scan_is_strictly_bounded(self):
        class SliceOnlyEventSource:
            def __init__(self, events):
                self.events = events
                self.requested_slice = None

            def __iter__(self):
                raise AssertionError('The complete stale event source must not be materialized.')

            def __getitem__(self, item):
                self.requested_slice = item
                return self.events[item]

        created_at = timezone.now()
        source = SliceOnlyEventSource([
            OperationalStateEvent(
                key='production',
                version=index,
                event_type='trip_changed',
                object_type='Trip',
                object_id=str(index),
                payload={'action': 'save'},
                created_at=created_at,
            )
            for index in range(1, MAX_EVENT_SCAN + 2)
        ])

        events, truncated = relevant_event_delta(
            source,
            self.driver_access,
            limit=50,
        )

        self.assertEqual(len(events), 50)
        self.assertTrue(truncated)
        self.assertIsInstance(source.requested_slice, slice)
        self.assertIn(source.requested_slice.start, (None, 0))
        self.assertEqual(source.requested_slice.stop, MAX_EVENT_SCAN + 1)

    def test_shift_signal_delta_contains_employee_and_equipment_scope(self):
        event = self.driver_shift.__class__._meta.apps.get_model(
            'core',
            'OperationalStateEvent',
        ).objects.filter(
            object_type='EmployeeShift',
            object_id=str(self.driver_shift.id),
        ).latest('version')

        self.assertEqual(event.payload['employee_id'], self.driver_access.employee_id)
        self.assertEqual(event.payload['equipment_id'], self.driver_truck.id)
        self.assertEqual(event.payload['workplace_code'], 'driver')

    @patch('assignments.services.reconcile_due_haul_assignments')
    def test_one_hundred_realtime_clients_share_one_assignment_reconcile_window(
        self,
        reconcile,
    ):
        from assignments import services

        services._reconcile_next_check = 0
        client = self.authorize(self.dispatcher_access)

        responses = [
            client.get(self.url, {'include_events': '1'})
            for _ in range(100)
        ]

        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(reconcile.call_count, 1)

    @patch('assignments.services.reconcile_due_haul_assignments')
    def test_shared_server_gate_deduplicates_sequential_worker_processes(self, reconcile):
        from assignments import services

        services._reconcile_next_check = 0
        services.reconcile_due_haul_assignments_throttled()
        # A different worker has its own local timer, but shares the server lock file.
        services._reconcile_next_check = 0
        services.reconcile_due_haul_assignments_throttled()

        self.assertEqual(reconcile.call_count, 1)
        self.assertTrue(services.RECONCILE_LOCK_PATH.exists())

    def test_shared_server_gate_deduplicates_genuine_spawned_processes(self):
        context = get_context('spawn')
        ready_queue = context.Queue()
        start_event = context.Event()
        lock_path = Path(self.reconcile_lock_dir.name) / 'spawn-reconcile.lock'
        result_path = Path(self.reconcile_lock_dir.name) / 'spawn-reconcile-results.txt'
        processes = [
            context.Process(
                target=run_reconcile_server_gate_worker,
                args=(
                    str(lock_path),
                    str(result_path),
                    ready_queue,
                    start_event,
                ),
            )
            for _ in range(2)
        ]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                self.assertEqual(ready_queue.get(timeout=20), 'ready')
            start_event.set()
            for process in processes:
                process.join(timeout=20)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            ready_queue.close()

        self.assertTrue(result_path.exists())
        executions = result_path.read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(executions), 1)

    @patch('assignments.services.reconcile_due_haul_assignments', return_value=0)
    def test_lock_open_failure_degrades_to_process_gate_without_realtime_500(
        self,
        reconcile,
    ):
        from assignments import services

        services._reconcile_next_check = 0
        with patch.object(Path, 'open', side_effect=PermissionError('lock unavailable')):
            response = self.authorize(self.dispatcher_access).get(
                self.url,
                {'include_events': '1'},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(reconcile.call_count, 1)

    def test_marker_failure_never_masks_domain_reconcile_error(self):
        from assignments import services

        services._reconcile_next_check = 0
        with (
            patch.object(services, 'RECONCILE_INTERVAL_SECONDS', 0),
            patch.object(
                services,
                'reconcile_due_haul_assignments',
                side_effect=ValueError('domain reconcile failure'),
            ),
            patch.object(services.os, 'fsync', side_effect=OSError('marker failure')),
        ):
            with self.assertRaisesRegex(ValueError, 'domain reconcile failure'):
                services.reconcile_due_haul_assignments_throttled()

        services._reconcile_next_check = 0
        with (
            patch.object(services, 'RECONCILE_INTERVAL_SECONDS', 0),
            patch.object(
                services,
                'reconcile_due_haul_assignments',
                return_value=0,
            ) as retry,
        ):
            self.assertEqual(services.reconcile_due_haul_assignments_throttled(), 0)
        self.assertEqual(retry.call_count, 1)

    def test_marker_read_failure_unlocks_and_closes_before_process_fallback(self):
        from assignments import services

        lock_file = MagicMock()
        lock_file.read.side_effect = OSError('marker read failure')
        with (
            patch.object(Path, 'open', return_value=lock_file),
            patch.object(services.locks, 'lock', return_value=True),
            patch.object(services.locks, 'unlock') as unlock,
        ):
            gate = services._acquire_reconcile_server_gate()

        self.assertIs(gate, services.RECONCILE_PROCESS_ONLY)
        unlock.assert_called_once_with(lock_file)
        lock_file.close.assert_called_once_with()

    def test_marker_unlock_failure_still_closes_without_masking_success(self):
        from assignments import services

        lock_file = MagicMock()
        with (
            patch.object(services.os, 'fsync', return_value=None),
            patch.object(
                services.locks,
                'unlock',
                side_effect=OSError('marker unlock failure'),
            ),
        ):
            services._release_reconcile_server_gate(lock_file)

        lock_file.close.assert_called_once_with()

    def test_server_gate_prevents_overlap_after_interval_elapsed(self):
        from assignments import services

        started = Event()

        def slow_reconcile():
            started.set()
            time.sleep(0.30)
            return 0

        with (
            patch.object(services, '_acquire_reconcile_process_gate', return_value=True),
            patch.object(services, 'RECONCILE_INTERVAL_SECONDS', 0.10),
            patch.object(
                services,
                'reconcile_due_haul_assignments',
                side_effect=slow_reconcile,
            ) as reconcile,
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(services.reconcile_due_haul_assignments_throttled)
            self.assertTrue(started.wait(timeout=2))
            time.sleep(0.15)
            second = executor.submit(services.reconcile_due_haul_assignments_throttled)
            self.assertEqual(first.result(timeout=3), 0)
            self.assertEqual(second.result(timeout=3), 0)

        self.assertEqual(reconcile.call_count, 1)

    def test_process_only_fallback_prevents_overlap_for_complete_reconcile(self):
        from assignments import services

        started = Event()
        counter_lock = Lock()
        counters = {'active': 0, 'max_active': 0}

        def slow_reconcile():
            with counter_lock:
                counters['active'] += 1
                counters['max_active'] = max(
                    counters['max_active'],
                    counters['active'],
                )
            started.set()
            time.sleep(0.30)
            with counter_lock:
                counters['active'] -= 1
            return 0

        services._reconcile_next_check = 0
        with (
            patch.object(services, 'RECONCILE_INTERVAL_SECONDS', 0.10),
            patch.object(
                services,
                '_acquire_reconcile_server_gate',
                return_value=services.RECONCILE_PROCESS_ONLY,
            ),
            patch.object(
                services,
                'reconcile_due_haul_assignments',
                side_effect=slow_reconcile,
            ) as reconcile,
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(services.reconcile_due_haul_assignments_throttled)
            self.assertTrue(started.wait(timeout=2))
            time.sleep(0.15)
            second = executor.submit(services.reconcile_due_haul_assignments_throttled)
            self.assertEqual(first.result(timeout=3), 0)
            self.assertEqual(second.result(timeout=3), 0)

        self.assertEqual(reconcile.call_count, 1)
        self.assertEqual(counters['max_active'], 1)

    def test_due_check_after_interval_has_no_database_writes(self):
        from assignments import services

        services._reconcile_next_check = 0
        with (
            patch.object(services, '_acquire_reconcile_process_gate', return_value=True),
            patch.object(services, 'RECONCILE_INTERVAL_SECONDS', 0),
            CaptureQueriesContext(connection) as captured,
        ):
            applied = services.reconcile_due_haul_assignments_throttled()

        dml_sql = [
            query['sql']
            for query in captured.captured_queries
            if query['sql'].lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE '))
        ]
        self.assertEqual(applied, 0)
        self.assertEqual(dml_sql, [])

    def test_warmed_realtime_reads_do_not_query_or_write_haul_assignments(self):
        from assignments import services

        services._reconcile_next_check = 0
        client = self.authorize(self.dispatcher_access)
        current_version = self.current_version()
        warm = client.get(
            self.url,
            {'after': current_version, 'include_events': '1'},
        )
        self.assertEqual(warm.status_code, 200)

        with CaptureQueriesContext(connection) as captured:
            responses = [
                client.get(
                    self.url,
                    {'after': current_version, 'include_events': '1'},
                )
                for _ in range(100)
            ]

        assignment_sql = [
            query['sql']
            for query in captured.captured_queries
            if 'assignments_haulassignment' in query['sql'].lower()
        ]
        dml_sql = [
            query['sql']
            for query in captured.captured_queries
            if query['sql'].lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE '))
        ]
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(assignment_sql, [])
        self.assertEqual(dml_sql, [])
