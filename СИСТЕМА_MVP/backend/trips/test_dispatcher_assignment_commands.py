import inspect
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import resolve, reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment, HaulAssignmentAction
from assignments.services import HaulAssignmentStateConflict
from references.models import Equipment, EquipmentType
from shifts.models import EmployeeShift
from users.models import Employee, EmployeeAccess, Role

from . import dispatcher_assignment_commands
from . import views as trips_views
from .models import DispatcherActionLog, DispatcherActionType


class DispatcherAssignmentCommandBoundaryTests(SimpleTestCase):
    def test_url_keeps_public_views_facade(self):
        match = resolve(
            reverse('dispatcher_cancel_assignment', kwargs={'assignment_id': 17}),
        )

        self.assertIs(match.func, trips_views.dispatcher_cancel_assignment_view)

    def test_public_view_is_thin_assignment_command_facade(self):
        source = inspect.getsource(trips_views.dispatcher_cancel_assignment_view)

        self.assertIn('_execute_dispatcher_cancel_assignment(', source)
        self.assertIn('lock_mutation_access=lock_dispatcher_mutation_access', source)
        self.assertIn('action_logger=log_dispatcher_action', source)
        self.assertNotIn('EmployeeAccess.objects', source)
        self.assertNotIn('HaulAssignment.objects', source)
        self.assertNotIn('schedule_haul_release', source)
        self.assertNotIn('lock_production_state', source)


class DispatcherAssignmentFacadeDelegationTests(TestCase):
    def test_facade_injects_views_patch_seams(self):
        request = RequestFactory().post('/dispatcher/assignments/17/cancel/')
        expected = HttpResponse(status=302)
        with patch.object(
            trips_views,
            '_execute_dispatcher_cancel_assignment',
            return_value=expected,
        ) as execute:
            response = trips_views.dispatcher_cancel_assignment_view(
                request,
                assignment_id=17,
            )

        self.assertIs(response, expected)
        execute.assert_called_once_with(
            request,
            17,
            lock_mutation_access=trips_views.lock_dispatcher_mutation_access,
            action_logger=trips_views.log_dispatcher_action,
        )


class DispatcherCancelAssignmentCommandTests(TestCase):
    def setUp(self):
        self.dispatcher_role = Role.objects.create(
            code='dispatcher',
            name='Диспетчер',
        )
        self.dispatcher = Employee.objects.create(
            full_name='Диспетчер отмены назначения',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=self.dispatcher_role,
            access_code='ASSIGNMENT-COMMAND',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.shift = EmployeeShift.objects.create(
            employee=self.dispatcher,
            shift_type='day',
            workplace_code='dispatcher',
            opened_at=timezone.now(),
            opened_by=self.dispatcher,
        )
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck = Equipment.objects.create(
            equipment_type=truck_type,
            garage_number='ASSIGNMENT-TRUCK',
        )
        self.excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='ASSIGNMENT-EXCAVATOR',
        )
        session = self.client.session
        session['employee_access_id'] = self.access.id
        session.save()

    def create_assignment(self, status):
        return HaulAssignment.objects.create(
            truck=self.truck,
            excavator=self.excavator,
            assigned_by=self.dispatcher,
            status=status,
            accepted_at=(
                timezone.now()
                if status == AssignmentStatus.ACCEPTED
                else None
            ),
        )

    def post_cancel(self, assignment, **payload):
        data = {'reason': 'Корректировка расстановки', **payload}
        return self.client.post(
            reverse('dispatcher_cancel_assignment', args=[assignment.id]),
            data,
        )

    def messages_text(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    def test_pending_assignment_is_cancelled_immediately_and_logged(self):
        assignment = self.create_assignment(AssignmentStatus.PENDING)

        response = self.post_cancel(
            assignment,
            truck='ASSIGNMENT-TRUCK',
            show_pending_assignments='1',
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response['Location'],
            f"{reverse('dispatcher_control')}?"
            'truck=ASSIGNMENT-TRUCK&show_pending_assignments=1',
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.CANCELLED)
        self.assertIsNotNone(assignment.ended_at)
        self.assertFalse(
            HaulAssignment.objects.filter(
                truck=self.truck,
                action=HaulAssignmentAction.RELEASE,
                ended_at__isnull=True,
            ).exists(),
        )
        action = DispatcherActionLog.objects.get()
        self.assertEqual(action.actor, self.dispatcher)
        self.assertEqual(action.action_type, DispatcherActionType.CANCEL_ASSIGNMENT)
        self.assertEqual(action.haul_assignment, assignment)
        self.assertEqual(action.reason, 'Корректировка расстановки')
        self.assertEqual(
            action.target_summary,
            f'{self.truck} под {self.excavator}',
        )
        self.assertIn('отменено', ' | '.join(self.messages_text(response)))

    def test_accepted_assignment_schedules_release_and_logs_new_state(self):
        assignment = self.create_assignment(AssignmentStatus.ACCEPTED)

        response = self.post_cancel(assignment)

        self.assertEqual(response.status_code, 302)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(assignment.ended_at)
        release = HaulAssignment.objects.get(
            truck=self.truck,
            action=HaulAssignmentAction.RELEASE,
            status=AssignmentStatus.PENDING,
            ended_at__isnull=True,
        )
        self.assertEqual(release.excavator, self.excavator)
        self.assertEqual(release.assigned_by, self.dispatcher)
        self.assertIsNotNone(release.effective_at)
        action = DispatcherActionLog.objects.get()
        self.assertEqual(action.actor, self.dispatcher)
        self.assertEqual(action.haul_assignment, release)
        self.assertEqual(action.reason, 'Корректировка расстановки')

    def test_stale_accepted_assignment_returns_error_without_log(self):
        assignment = self.create_assignment(AssignmentStatus.ACCEPTED)
        conflict = HaulAssignmentStateConflict(
            expected_state_id=assignment.id,
            actual_state_id=assignment.id + 1,
        )

        with patch.object(
            dispatcher_assignment_commands,
            'schedule_haul_release',
            side_effect=conflict,
        ):
            response = self.post_cancel(assignment)

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(assignment.ended_at)
        self.assertFalse(DispatcherActionLog.objects.exists())
        self.assertFalse(
            HaulAssignment.objects.filter(
                truck=self.truck,
                action=HaulAssignmentAction.RELEASE,
            ).exists(),
        )
        self.assertIn(
            'Назначение уже изменилось. Обновите пульт и повторите действие.',
            self.messages_text(response),
        )

    def test_get_preserves_filters_and_does_not_mutate_assignment(self):
        assignment = self.create_assignment(AssignmentStatus.PENDING)

        response = self.client.get(
            reverse('dispatcher_cancel_assignment', args=[assignment.id]),
            {'truck': 'ASSIGNMENT-TRUCK'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response['Location'],
            f"{reverse('dispatcher_control')}?truck=ASSIGNMENT-TRUCK",
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.PENDING)
        self.assertIsNone(assignment.ended_at)
        self.assertFalse(DispatcherActionLog.objects.exists())

    def test_closed_dispatcher_shift_blocks_assignment_mutation(self):
        assignment = self.create_assignment(AssignmentStatus.PENDING)
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.dispatcher
        self.shift.save(update_fields=['closed_at', 'closed_by'])

        response = self.post_cancel(assignment)

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.PENDING)
        self.assertIsNone(assignment.ended_at)
        self.assertFalse(DispatcherActionLog.objects.exists())
        self.assertIn(
            'Смена горного диспетчера закрыта. Изменения на пульте недоступны.',
            self.messages_text(response),
        )

    def test_missing_assignment_returns_original_error_message(self):
        response = self.client.post(
            reverse('dispatcher_cancel_assignment', args=[999999]),
            {'reason': 'Уже снято'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(DispatcherActionLog.objects.exists())
        self.assertIn(
            'Активное назначение для отмены не найдено.',
            self.messages_text(response),
        )

    def test_manager_role_keeps_original_role_home_redirect(self):
        assignment = self.create_assignment(AssignmentStatus.PENDING)
        manager_role = Role.objects.create(code='manager', name='Руководитель')
        self.access.role = manager_role
        self.access.save(update_fields=['role'])

        response = self.post_cancel(assignment)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('role_home'))
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.PENDING)
        self.assertIsNone(assignment.ended_at)
        self.assertFalse(DispatcherActionLog.objects.exists())
