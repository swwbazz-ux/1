import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.db import close_old_connections, connection
from django.test import Client, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, EquipmentAssignment, WorkShiftType
from core.models import OperationalStateEvent
from references.models import Equipment, EquipmentModel, EquipmentType
from users.models import Employee, EmployeeAccess, Role

from .models import EmployeeShift, ShiftClientAction


@skipUnless(
    connection.vendor == 'postgresql',
    'Требуется отдельная тестовая PostgreSQL для конкурентного старта смен.',
)
class ExcavatorConcurrentShiftStartPostgreSQLTests(TransactionTestCase):
    def setUp(self):
        self.operator_role = Role.objects.create(
            code='excavator_operator',
            name='Машинист экскаватора',
        )
        equipment_type = EquipmentType.objects.create(name='Экскаватор')
        equipment_model = EquipmentModel.objects.create(
            equipment_type=equipment_type,
            name='ЭКГ P04-009 PostgreSQL',
            fuel_capacity_limit_l=2000,
        )
        self.excavator = Equipment.objects.create(
            equipment_type=equipment_type,
            model=equipment_model,
            garage_number='EXC-P04-009-PG',
        )
        self.operators = []
        self.session_keys = []
        for index, shift_type in enumerate(
            (WorkShiftType.SHIFT_1, WorkShiftType.SHIFT_2),
            start=1,
        ):
            employee = Employee.objects.create(
                full_name=f'Машинист P04-009 PG {index}',
                status=Employee.Status.ACTIVE,
                is_active=True,
            )
            access = EmployeeAccess.objects.create(
                employee=employee,
                role=self.operator_role,
                access_code=f'PG040{index}',
                status=EmployeeAccess.Status.ACTIVATED,
                is_active=True,
                last_login_at=timezone.now(),
            )
            EquipmentAssignment.objects.create(
                employee=employee,
                role=self.operator_role,
                equipment=self.excavator,
                shift_type=shift_type,
                status=AssignmentStatus.ACCEPTED,
                accepted_at=timezone.now(),
            )
            self.operators.append(employee)
            self.session_keys.append(self.create_session(access))

    @staticmethod
    def create_session(access):
        session = SessionStore()
        session['employee_access_id'] = access.pk
        session['active_role_access_id'] = access.pk
        session['active_role_login_at'] = access.last_login_at.isoformat()
        session['active_role_code'] = access.role.code
        session.save()
        return session.session_key

    @staticmethod
    def post_start(session_key, client_action_id, barrier):
        close_old_connections()
        try:
            client = Client(raise_request_exception=False)
            client.cookies[settings.SESSION_COOKIE_NAME] = session_key
            barrier.wait(timeout=20)
            response = client.post(
                reverse('excavator_shift_action'),
                data=json.dumps({
                    'action': 'open',
                    'fuel': '900',
                    'engine_hours': '501',
                    'client_action_id': client_action_id,
                }),
                content_type='application/json',
                HTTP_HOST='localhost',
            )
            try:
                payload = response.json()
            except ValueError:
                payload = {'raw': response.content.decode('utf-8', errors='replace')}
            exc_info = getattr(response, 'exc_info', None)
            return {
                'status': response.status_code,
                'payload': payload,
                'exc_info': exc_info,
                'error': (
                    f'{exc_info[0].__name__}: {exc_info[1]}'
                    if exc_info
                    else None
                ),
            }
        except Exception as error:
            return {
                'status': 599,
                'payload': None,
                'exc_info': None,
                'error': f'{type(error).__name__}: {error}',
            }
        finally:
            close_old_connections()

    def test_two_simultaneous_starts_leave_one_shift_and_domain_conflict(self):
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    self.post_start,
                    self.session_keys[index],
                    f'p04-009-postgresql-{index + 1}',
                    barrier,
                )
                for index in range(2)
            ]
            results = [future.result(timeout=40) for future in futures]

        for result in results:
            self.assertIsNone(result['error'], result)
            self.assertIsNone(result['exc_info'], result)
            self.assertLess(result['status'], 500, result)
        self.assertEqual(sorted(result['status'] for result in results), [200, 409])
        winner_shift = (
            EmployeeShift.objects
            .select_related('employee', 'equipment', 'equipment__equipment_type')
            .get(equipment=self.excavator, closed_at__isnull=True)
        )
        loser_payload = next(
            result['payload']
            for result in results
            if result['status'] == 409
        )
        self.assertEqual(loser_payload['code'], 'equipment_shift_already_open')
        self.assertIn(winner_shift.employee.full_name, loser_payload['error'])
        self.assertIn(str(self.excavator), loser_payload['error'])
        self.assertEqual(
            ShiftClientAction.objects.filter(
                action_type='excavator_shift_opened',
            ).count(),
            1,
        )
        self.assertEqual(
            OperationalStateEvent.objects.filter(
                event_type='excavator_shift_opened',
            ).count(),
            1,
        )
