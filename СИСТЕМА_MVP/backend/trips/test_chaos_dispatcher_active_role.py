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
from references.models import Equipment, EquipmentType
from reports.models import PilotFeedback, ReportTemplate
from shifts.models import EmployeeShift
from trips.models import DispatcherActionLog
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
    'Гонка переключения роли и действия Диспетчера проверяется только на PostgreSQL.',
)
class DispatcherActiveRolePostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.dispatcher_role = Role.objects.create(
            code='dispatcher',
            name='Диспетчер',
        )
        self.admin_role = Role.objects.create(
            code='admin',
            name='Администратор',
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
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=self.admin_role,
            access_code='PG-ADMIN-ROLE',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            last_login_at=now - timedelta(minutes=1),
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

    def run_switch_wins(self, action_callable):
        switch_locked = Event()
        release_switch = Event()
        action_entered = Event()

        from trips import views as trips_views
        from users import active_role as active_role_module

        original_blockers = active_role_module._role_switch_blockers
        original_control_url = trips_views.get_dispatcher_control_url

        def paused_role_switch(*args, **kwargs):
            switch_locked.set()
            if not release_switch.wait(timeout=10):
                raise TimeoutError('Действие Диспетчера не вошло в гонку.')
            return original_blockers(*args, **kwargs)

        def marked_control_url(request):
            result = original_control_url(request)
            action_entered.set()
            return result

        def switch_worker():
            close_old_connections()
            try:
                access = (
                    EmployeeAccess.objects
                    .select_related('employee', 'role')
                    .get(pk=self.admin_access.pk)
                )
                request = SimpleNamespace(session={})
                activate_role_session(request, access)
                return ''
            except Exception as error:
                return f'{type(error).__name__}: {error}'
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
                'users.active_role._role_switch_blockers',
                new=paused_role_switch,
            ),
            patch(
                'trips.views.get_dispatcher_control_url',
                new=marked_control_url,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            switch_future = executor.submit(switch_worker)
            if not switch_locked.wait(timeout=10):
                release_switch.set()
                if switch_future.done():
                    self.fail(
                        'Переключение роли завершилось до блокировки Employee: '
                        f'{switch_future.result()!r}'
                    )
                self.fail('Переключение роли не получило блокировку Employee.')
            action_future = executor.submit(action_worker)
            if not action_entered.wait(timeout=10):
                release_switch.set()
                if action_future.done():
                    response = action_future.result()
                    exc_info = getattr(response, 'exc_info', None)
                    error = (
                        f'{exc_info[0].__name__}: {exc_info[1]}'
                        if exc_info
                        else 'без response.exc_info'
                    )
                    self.fail(
                        'Действие завершилось до транзакционного барьера: '
                        f'HTTP {response.status_code}; {error}'
                    )
                self.fail('Действие Диспетчера не дошло до транзакционного барьера.')
            release_switch.set()
            switch_error = switch_future.result(timeout=30)
            action_response = action_future.result(timeout=30)

        self.assertEqual(switch_error, '', switch_error)
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
        self.assertEqual(action_response.status_code, 302)
        return action_response

    def test_role_switch_wins_against_dispatcher_service_close(self):
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

        self.run_switch_wins(action)

        self.target_shift.refresh_from_db()
        self.assertIsNone(self.target_shift.closed_at)
        self.assertFalse(self.target_shift.is_service_closed)
        self.assertFalse(
            DispatcherActionLog.objects.filter(shift=self.target_shift).exists(),
        )

    def test_role_switch_wins_against_dispatcher_assignment_cancel(self):
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

        self.run_switch_wins(action)

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, AssignmentStatus.PENDING)
        self.assertIsNone(self.assignment.ended_at)
        self.assertFalse(
            DispatcherActionLog.objects.filter(
                haul_assignment=self.assignment,
            ).exists(),
        )
