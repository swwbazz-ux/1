import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.db import close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    ExcavatorPlacement,
    HaulAssignment,
    HaulAssignmentAction,
)
from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import DumpPoint, Equipment, EquipmentType, RockType
from reports.models import PilotFeedback, ReportTemplate
from shifts.models import EmployeeShift
from trips.models import DispatcherActionLog, Trip, TripClientAction, TripStatus
from trips.views import get_operational_state_version
from users.active_role import (
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    ACTIVE_ROLE_SESSION_KEY,
    activate_role_session,
)
from users.models import Employee, EmployeeAccess, Role


class DispatcherActiveRoleBarrierRegressionTests(TestCase):
    def setUp(self):
        self.dispatcher_role = Role.objects.create(
            code='dispatcher',
            name='Диспетчер',
        )
        self.driver_role = Role.objects.create(
            code='driver',
            name='Водитель',
        )
        self.dispatcher = Employee.objects.create(
            full_name='Диспетчер гонки активной роли',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.dispatcher_access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=self.dispatcher_role,
            access_code='CHAOS-DISPATCHER-ACTIVE-ROLE',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.dispatcher_shift = EmployeeShift.objects.create(
            employee=self.dispatcher,
            shift_type='day',
            workplace_code='dispatcher',
            opened_at=timezone.now(),
            opened_by=self.dispatcher,
        )
        self.driver = Employee.objects.create(
            full_name='Водитель целевой смены',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=self.driver,
            role=self.driver_role,
            access_code='CHAOS-DRIVER-TARGET-SHIFT',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.target_shift = EmployeeShift.objects.create(
            employee=self.driver,
            shift_type='day',
            workplace_code='driver',
            opened_at=timezone.now(),
            opened_by=self.driver,
        )

        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck = Equipment.objects.create(
            equipment_type=truck_type,
            garage_number='CHAOS-ROLE-TRUCK',
        )
        self.excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='CHAOS-ROLE-EXCAVATOR',
        )
        self.placement = ExcavatorPlacement.objects.create(
            excavator=self.excavator,
            zone=ExcavatorPlacement.Zone.ACTIVE,
            changed_by=self.dispatcher,
        )

        session = self.client.session
        session['employee_access_id'] = self.dispatcher_access.id
        session.save()

    @staticmethod
    def inactive_role_state(*_args, **_kwargs):
        return {'is_active': False}

    def test_service_close_rechecks_active_role_before_locking_target(self):
        with patch(
            'trips.views.role_session_state',
            side_effect=self.inactive_role_state,
        ):
            response = self.client.post(
                reverse(
                    'dispatcher_service_close_shift',
                    args=[self.target_shift.id],
                ),
                {'reason': 'Запрос старой вкладки Диспетчера'},
            )

        self.assertEqual(response.status_code, 302)
        self.target_shift.refresh_from_db()
        self.assertIsNone(self.target_shift.closed_at)
        self.assertFalse(self.target_shift.is_service_closed)
        self.assertFalse(
            DispatcherActionLog.objects.filter(shift=self.target_shift).exists(),
        )

    def test_cancel_assignment_rechecks_active_role_before_target_mutation(self):
        assignment = HaulAssignment.objects.create(
            truck=self.truck,
            excavator=self.excavator,
            assigned_by=self.dispatcher,
            status=AssignmentStatus.PENDING,
        )

        with patch(
            'trips.views.role_session_state',
            side_effect=self.inactive_role_state,
        ):
            response = self.client.post(
                reverse('dispatcher_cancel_assignment', args=[assignment.id]),
                {'reason': 'Запрос старой вкладки Диспетчера'},
            )

        self.assertEqual(response.status_code, 302)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.PENDING)
        self.assertIsNone(assignment.ended_at)
        self.assertFalse(
            DispatcherActionLog.objects.filter(
                haul_assignment=assignment,
            ).exists(),
        )

    def test_release_assignment_rechecks_active_role_before_service_mutation(self):
        assignment = HaulAssignment.objects.create(
            truck=self.truck,
            excavator=self.excavator,
            assigned_by=self.dispatcher,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )

        with patch(
            'trips.views.role_session_state',
            side_effect=self.inactive_role_state,
        ):
            response = self.client.post(
                reverse('dispatcher_assign_truck'),
                data=json.dumps({
                    'action': 'release',
                    'truck_id': self.truck.id,
                }),
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 409)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(assignment.ended_at)
        self.assertFalse(
            HaulAssignment.objects.filter(
                truck=self.truck,
                action=HaulAssignmentAction.RELEASE,
                ended_at__isnull=True,
            ).exists(),
        )
        self.assertFalse(DispatcherActionLog.objects.exists())

    def test_move_excavator_rechecks_active_role_before_placement_mutation(self):
        assignment = HaulAssignment.objects.create(
            truck=self.truck,
            excavator=self.excavator,
            assigned_by=self.dispatcher,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )

        with patch(
            'trips.views.role_session_state',
            side_effect=self.inactive_role_state,
        ):
            response = self.client.post(
                reverse('dispatcher_move_excavator'),
                data=json.dumps({
                    'excavator_id': self.excavator.id,
                    'zone': ExcavatorPlacement.Zone.INACTIVE,
                }),
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 409)
        self.placement.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(self.placement.zone, ExcavatorPlacement.Zone.ACTIVE)
        self.assertEqual(assignment.status, AssignmentStatus.ACCEPTED)
        self.assertFalse(
            HaulAssignment.objects.filter(
                truck=self.truck,
                action=HaulAssignmentAction.RELEASE,
                ended_at__isnull=True,
            ).exists(),
        )
        self.assertFalse(DispatcherActionLog.objects.exists())

    def test_end_dispatcher_shift_rechecks_active_role_before_mutation(self):
        with patch(
            'trips.views.role_session_state',
            side_effect=self.inactive_role_state,
        ):
            response = self.client.post(
                reverse('dispatcher_toggle_shift'),
                {'shift_action': 'end'},
            )

        self.assertEqual(response.status_code, 302)
        self.dispatcher_shift.refresh_from_db()
        self.assertIsNone(self.dispatcher_shift.closed_at)

    def test_report_template_builder_rechecks_active_role_before_save(self):
        with patch(
            'reports.views.role_session_state',
            side_effect=self.inactive_role_state,
        ):
            response = self.client.post(
                reverse('report_template_builder'),
                {
                    'name': 'Шаблон старой роли Диспетчера',
                    'columns': ['truck', 'volume_m3'],
                    'is_active': 'on',
                },
            )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(
            ReportTemplate.objects.filter(
                name='Шаблон старой роли Диспетчера',
            ).exists(),
        )

    def test_pilot_feedback_rechecks_active_role_before_status_change(self):
        feedback = PilotFeedback.objects.create(
            title='Замечание до переключения роли',
            category='access',
            priority='p1',
            status='new',
            created_by=self.dispatcher,
        )

        with patch(
            'reports.views.role_session_state',
            side_effect=self.inactive_role_state,
        ):
            response = self.client.post(
                reverse('pilot_feedback'),
                {
                    'action': 'change_status',
                    'feedback_id': feedback.id,
                    'status': 'decided',
                },
            )

        self.assertEqual(response.status_code, 409)
        feedback.refresh_from_db()
        self.assertEqual(feedback.status, 'new')


@skipUnless(
    connection.vendor == 'postgresql',
    'Гонка повторной активации роли и действия Диспетчера проверяется только на PostgreSQL.',
)
class DispatcherActiveRolePostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.dispatcher_role = Role.objects.create(
            code='dispatcher',
            name='Диспетчер',
        )
        self.driver_role = Role.objects.create(
            code='driver',
            name='Водитель',
        )
        self.dispatcher = Employee.objects.create(
            full_name='Диспетчер конкурентной смены роли',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        now = timezone.now()
        self.dispatcher_access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=self.dispatcher_role,
            access_code='PG-DISPATCHER-ROLE',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            last_login_at=now,
        )
        self.dispatcher_shift = EmployeeShift.objects.create(
            employee=self.dispatcher,
            shift_type='day',
            workplace_code='dispatcher',
            opened_at=now,
            opened_by=self.dispatcher,
        )
        self.driver = Employee.objects.create(
            full_name='Водитель конкурентной целевой смены',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=self.driver,
            role=self.driver_role,
            access_code='PG-DRIVER-TARGET',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.target_shift = EmployeeShift.objects.create(
            employee=self.driver,
            shift_type='day',
            workplace_code='driver',
            opened_at=now,
            opened_by=self.driver,
        )
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck = Equipment.objects.create(
            equipment_type=truck_type,
            garage_number='PG-ROLE-TRUCK',
        )
        self.excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='PG-ROLE-EXCAVATOR',
        )
        self.assignment = HaulAssignment.objects.create(
            truck=self.truck,
            excavator=self.excavator,
            assigned_by=self.dispatcher,
            status=AssignmentStatus.PENDING,
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
        self.dump_point = DumpPoint.objects.create(
            name='PG-ROLE-DUMP-POINT',
        )
        self.dispatcher_session_key = self.session_key_for_access(
            self.dispatcher_access,
        )

    @staticmethod
    def session_key_for_access(access):
        session = SessionStore()
        session['employee_access_id'] = access.id
        session[ACTIVE_ROLE_SESSION_KEY] = access.id
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = (
            access.last_login_at.isoformat()
        )
        session.save()
        return session.session_key

    @staticmethod
    def client_for_session(session_key):
        client = Client(raise_request_exception=False)
        client.cookies[settings.SESSION_COOKIE_NAME] = session_key
        return client

    def run_repeat_activation_wins(
        self,
        action_callable,
        *,
        pause_function='get_dispatcher_control_url',
        expected_status=302,
    ):
        access_loaded = Event()
        activation_committed = Event()

        from trips import views as trips_views

        original_boundary = getattr(trips_views, pause_function)

        def paused_after_access(*args, **kwargs):
            result = original_boundary(*args, **kwargs)
            access_loaded.set()
            if not activation_committed.wait(timeout=10):
                raise TimeoutError('Повторная активация роли не завершилась вовремя.')
            return result

        def activation_worker():
            close_old_connections()
            try:
                access = (
                    EmployeeAccess.objects
                    .select_related('employee', 'role')
                    .get(pk=self.dispatcher_access.pk)
                )
                request = SimpleNamespace(session={})
                activated = activate_role_session(request, access)
                return activated.last_login_at
            finally:
                close_old_connections()

        def action_worker():
            close_old_connections()
            try:
                return action_callable()
            finally:
                close_old_connections()

        with (
            patch(
                f'trips.views.{pause_function}',
                new=paused_after_access,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            action_future = executor.submit(action_worker)
            if not access_loaded.wait(timeout=10):
                activation_committed.set()
                if action_future.done():
                    response = action_future.result()
                    exc_info = getattr(response, 'exc_info', None)
                    error = (
                        f'{exc_info[0].__name__}: {exc_info[1]}'
                        if exc_info
                        else 'без response.exc_info'
                    )
                    self.fail(
                        'Действие завершилось до загрузки исходного доступа: '
                        f'HTTP {response.status_code}; {error}'
                    )
                self.fail('Действие Диспетчера не загрузило исходный доступ.')
            activation_future = executor.submit(activation_worker)
            try:
                activated_at = activation_future.result(timeout=30)
            finally:
                activation_committed.set()
            action_response = action_future.result(timeout=30)

        action_exc_info = getattr(action_response, 'exc_info', None)
        self.assertIsNone(
            action_exc_info,
            (
                f'{action_exc_info[0].__name__}: {action_exc_info[1]}'
                if action_exc_info
                else None
            ),
        )
        self.assertLess(action_response.status_code, 500)
        self.assertEqual(action_response.status_code, expected_status)
        self.dispatcher_access.refresh_from_db()
        self.assertEqual(self.dispatcher_access.last_login_at, activated_at)
        return action_response

    def test_repeat_activation_wins_against_dispatcher_service_close(self):
        def action():
            client = self.client_for_session(self.dispatcher_session_key)
            return client.post(
                reverse(
                    'dispatcher_service_close_shift',
                    args=[self.target_shift.id],
                ),
                {'reason': 'Конкурентное служебное закрытие'},
                HTTP_HOST='localhost',
            )

        self.run_repeat_activation_wins(action)

        self.target_shift.refresh_from_db()
        self.assertIsNone(self.target_shift.closed_at)
        self.assertFalse(self.target_shift.is_service_closed)
        self.assertFalse(
            DispatcherActionLog.objects.filter(shift=self.target_shift).exists(),
        )

    def test_repeat_activation_wins_against_dispatcher_assignment_cancel(self):
        def action():
            client = self.client_for_session(self.dispatcher_session_key)
            return client.post(
                reverse(
                    'dispatcher_cancel_assignment',
                    args=[self.assignment.id],
                ),
                {'reason': 'Конкурентная отмена назначения'},
                HTTP_HOST='localhost',
            )

        self.run_repeat_activation_wins(action)

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, AssignmentStatus.PENDING)
        self.assertIsNone(self.assignment.ended_at)
        self.assertFalse(
            DispatcherActionLog.objects.filter(
                haul_assignment=self.assignment,
            ).exists(),
        )

    def test_repeat_activation_wins_against_dispatcher_equipment_settings(self):
        def action():
            client = self.client_for_session(self.dispatcher_session_key)
            return client.post(
                reverse(
                    'dispatcher_equipment_detail',
                    kwargs={
                        'category': 'complex',
                        'equipment_id': self.excavator.id,
                    },
                ),
                data=json.dumps({
                    'state_version': get_operational_state_version(),
                    'rock_type_id': self.rock.id,
                    'dump_point_ids': [self.dump_point.id],
                    'loading_horizon': '75',
                    'loading_block': '52',
                }),
                content_type='application/json',
                HTTP_ACCEPT='application/json',
                HTTP_X_REQUESTED_WITH='XMLHttpRequest',
                HTTP_HOST='localhost',
            )

        response = self.run_repeat_activation_wins(
            action,
            pause_function='excavator_json_payload',
            expected_status=409,
        )

        self.assertEqual(response.json()['error'], 'inactive_role')
        self.placement.refresh_from_db()
        self.assertIsNone(self.placement.work_rock_type_id)
        self.assertIsNone(self.placement.work_dump_point_id)


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL row locks are required.')
class ExcavatorActiveRolePostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.role = Role.objects.create(
            code='excavator_operator',
            name='Машинист экскаватора',
        )
        self.operator = Employee.objects.create(
            full_name='Машинист конкурентной повторной авторизации',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.operator,
            role=self.role,
            access_code='PG-EXCAVATOR-ROLE-GENERATION',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            last_login_at=timezone.now(),
        )
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='PG-ROLE-EXCAVATOR-MUTATION',
        )
        self.shift = EmployeeShift.objects.create(
            employee=self.operator,
            shift_type='day',
            workplace_code='excavator_operator',
            equipment=self.excavator,
            opened_at=timezone.now(),
            opened_by=self.operator,
        )
        self.reason = DowntimeReason.objects.create(
            name='Конкурентная проверка активной роли',
            equipment_type=excavator_type,
            show_for_excavator_operator=True,
        )
        session = SessionStore()
        session['employee_access_id'] = self.access.id
        session[ACTIVE_ROLE_SESSION_KEY] = self.access.id
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = self.access.last_login_at.isoformat()
        session.save()
        self.session_key = session.session_key

    def _race_repeat_activation_against(self, mutation):
        access_loaded = Event()
        activation_committed = Event()

        from trips import views as trips_views

        original_access_from_request = trips_views.excavator_access_from_request

        def paused_access_from_request(*args, **kwargs):
            access = original_access_from_request(*args, **kwargs)
            access_loaded.set()
            if not activation_committed.wait(timeout=10):
                raise TimeoutError('Repeat activation did not finish in time.')
            return access

        def mutation_worker():
            close_old_connections()
            try:
                client = Client(raise_request_exception=False)
                client.cookies[settings.SESSION_COOKIE_NAME] = self.session_key
                return mutation(client)
            finally:
                close_old_connections()

        def activation_worker():
            close_old_connections()
            try:
                access = (
                    EmployeeAccess.objects
                    .select_related('employee', 'role')
                    .get(pk=self.access.pk)
                )
                request = SimpleNamespace(session={})
                activated = activate_role_session(request, access)
                return activated.last_login_at
            finally:
                close_old_connections()

        with (
            patch(
                'trips.views.excavator_access_from_request',
                new=paused_access_from_request,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            mutation_future = executor.submit(mutation_worker)
            if not access_loaded.wait(timeout=10):
                activation_committed.set()
                self.fail('Excavator mutation did not load the original access.')
            activation_future = executor.submit(activation_worker)
            try:
                activated_at = activation_future.result(timeout=30)
            finally:
                activation_committed.set()
            response = mutation_future.result(timeout=30)

        return response, activated_at

    def test_repeat_activation_wins_before_excavator_downtime_mutation(self):
        def mutation(client):
            return client.post(
                reverse('excavator_downtime_action'),
                data=json.dumps({
                    'action': 'start',
                    'reason_id': self.reason.id,
                    'client_action_id': 'pg-stale-excavator-downtime',
                }),
                content_type='application/json',
                HTTP_HOST='localhost',
            )

        response, activated_at = self._race_repeat_activation_against(mutation)

        self.access.refresh_from_db()
        self.assertEqual(self.access.last_login_at, activated_at)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'inactive_role')
        self.assertFalse(
            DowntimeEvent.objects.filter(
                equipment=self.excavator,
                ended_at__isnull=True,
            ).exists(),
        )

    def test_repeat_activation_wins_before_excavator_loaded_cancel_mutation(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        truck = Equipment.objects.create(
            equipment_type=truck_type,
            garage_number='PG-ROLE-CANCEL-TRUCK',
        )
        rock = RockType.objects.create(name='PG role cancellation rock')
        dump_point = DumpPoint.objects.create(name='PG role cancellation dump')
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=truck,
            excavator_operator=self.operator,
            loading_shift=self.shift,
            rock_type=rock,
            dump_point=dump_point,
            assigned_dump_point=dump_point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            loaded_at=timezone.now(),
        )

        def mutation(client):
            return client.post(
                reverse('excavator_truck_loaded_cancel'),
                data=json.dumps({
                    'client_action_id': 'pg-stale-excavator-loaded-cancel',
                    'trip_id': trip.id,
                    'truck_id': truck.id,
                    'dump_point_id': dump_point.id,
                }),
                content_type='application/json',
                HTTP_HOST='localhost',
            )

        response, activated_at = self._race_repeat_activation_against(mutation)

        self.access.refresh_from_db()
        trip.refresh_from_db()
        self.assertEqual(self.access.last_login_at, activated_at)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'inactive_role')
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertIsNone(trip.cancelled_at)
        self.assertFalse(
            TripClientAction.objects.filter(
                action_type='truck_loaded_cancel',
                client_action_id='pg-stale-excavator-loaded-cancel',
            ).exists(),
        )
