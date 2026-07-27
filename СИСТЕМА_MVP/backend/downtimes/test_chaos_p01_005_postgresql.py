import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError, Event, local
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.contrib.messages import get_messages
from django.db import close_old_connections, connection
from django.db.models.query import QuerySet
from django.test import Client, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from core.models import OperationalStateEvent
from downtimes.forms import MechanicDowntimeCreateForm
from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import DumpPoint, Equipment, EquipmentType, RockType
from shifts.models import EmployeeShift
from trips.models import Trip, TripStatus
from trips.views import excavator_json_payload
from users.models import Employee, EmployeeAccess, Role


@skipUnless(
    connection.vendor == 'postgresql',
    'Конкурентная гарантия QA-CHAOS-P1-005 проверяется только на тестовой PostgreSQL.',
)
class DowntimePostgreSQLConcurrencyRegressionTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.mechanic_role = Role.objects.create(code='mechanic', name='Механик')
        self.driver_role = Role.objects.create(code='driver', name='Водитель')
        self.operator_role = Role.objects.create(
            code='excavator_operator',
            name='Машинист экскаватора',
        )

        self.excavator_type = EquipmentType.objects.create(name='Экскаватор CHAOS PG 005')
        self.truck_type = EquipmentType.objects.create(name='Самосвал CHAOS PG 005')
        self.excavator = Equipment.objects.create(
            equipment_type=self.excavator_type,
            garage_number='CHAOS-PG-005-EXCAVATOR',
        )
        self.mechanic_source_truck = Equipment.objects.create(
            equipment_type=self.truck_type,
            garage_number='CHAOS-PG-005-SOURCE-TRUCK',
        )
        self.load_truck = Equipment.objects.create(
            equipment_type=self.truck_type,
            garage_number='CHAOS-PG-005-LOAD-TRUCK',
        )
        self.rock = RockType.objects.create(name='Руда CHAOS PG 005')
        self.dump_point = DumpPoint.objects.create(name='ККД CHAOS PG 005')

        self.mechanic_one, self.mechanic_one_access = self.create_access(
            self.mechanic_role,
            'Механик CHAOS PG 005-1',
            'CHAOS-PG-MECHANIC-1',
        )
        self.mechanic_two, self.mechanic_two_access = self.create_access(
            self.mechanic_role,
            'Механик CHAOS PG 005-2',
            'CHAOS-PG-MECHANIC-2',
        )
        self.operator, self.operator_access = self.create_access(
            self.operator_role,
            'Машинист CHAOS PG 005',
            'CHAOS-PG-OPERATOR',
        )
        self.driver, _ = self.create_access(
            self.driver_role,
            'Водитель CHAOS PG 005',
            'CHAOS-PG-DRIVER',
        )

        self.operator_shift = EmployeeShift.objects.create(
            employee=self.operator,
            shift_type='day',
            workplace_code='excavator_operator',
            equipment=self.excavator,
            opened_at=timezone.now(),
            opened_by=self.operator,
        )
        self.driver_shift = EmployeeShift.objects.create(
            employee=self.driver,
            shift_type='day',
            workplace_code='driver',
            equipment=self.load_truck,
            opened_at=timezone.now(),
            opened_by=self.driver,
        )
        self.load_assignment = HaulAssignment.objects.create(
            excavator=self.excavator,
            truck=self.load_truck,
            assigned_by=self.operator,
            status=AssignmentStatus.ACCEPTED,
            effective_at=timezone.now(),
            accepted_at=timezone.now(),
        )
        self.mechanic_source_trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.mechanic_source_truck,
            excavator_operator=self.operator,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.ACTIVE,
            downtime_text='Конкурентный простой QA-CHAOS-P1-005',
        )

        self.mechanic_reason_one = DowntimeReason.objects.create(
            name='Диагностика CHAOS PG 005-1',
            equipment_type=self.excavator_type,
            show_for_mechanic=True,
        )
        self.mechanic_reason_two = DowntimeReason.objects.create(
            name='Гидравлика CHAOS PG 005-2',
            equipment_type=self.excavator_type,
            show_for_mechanic=True,
        )
        self.operator_reason = DowntimeReason.objects.create(
            name='Остановка машинистом CHAOS PG 005',
            equipment_type=self.excavator_type,
            show_for_excavator_operator=True,
        )

    @staticmethod
    def create_access(role, full_name, access_code):
        employee = Employee.objects.create(
            full_name=full_name,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        access = EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code=access_code,
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        return employee, access

    @staticmethod
    def session_key_for_access(access):
        client = Client()
        session = client.session
        session['employee_access_id'] = access.pk
        session.save()
        return session.session_key

    @staticmethod
    def client_for_session_key(session_key):
        client = Client(raise_request_exception=False)
        client.cookies[settings.SESSION_COOKIE_NAME] = session_key
        return client

    @staticmethod
    def wait_for_competitor(barrier):
        try:
            barrier.wait(timeout=10)
        except BrokenBarrierError:
            pass

    def run_pair(self, first_callable, second_callable):
        request_start = Barrier(2)

        def worker(callable_):
            close_old_connections()
            try:
                request_start.wait(timeout=10)
                return callable_()
            except Exception as error:
                return {
                    'actor_id': None,
                    'kind': 'worker',
                    'status': 599,
                    'messages': [],
                    'json': None,
                    'exc_info': None,
                    'error': f'{type(error).__name__}: {error}',
                }
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(worker, first_callable)
            second_future = executor.submit(worker, second_callable)
            return first_future.result(timeout=30), second_future.result(timeout=30)

    @staticmethod
    def response_result(response, *, actor_id, kind):
        exc_info = getattr(response, 'exc_info', None)
        content_type = response.headers.get('Content-Type', '')
        payload = None
        if 'application/json' in content_type:
            try:
                payload = response.json()
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
        return {
            'actor_id': actor_id,
            'kind': kind,
            'status': response.status_code,
            'messages': [
                str(message)
                for message in get_messages(response.wsgi_request)
            ],
            'json': payload,
            'exc_info': exc_info,
            'error': (
                f'{exc_info[0].__name__}: {exc_info[1]}'
                if exc_info
                else None
            ),
        }

    def post_mechanic_create(self, session_key, employee_id, reason_id, comment):
        client = self.client_for_session_key(session_key)
        response = client.post(
            reverse('mechanic_create_downtime', args=[self.mechanic_source_trip.pk]),
            data={
                f'trip_{self.mechanic_source_trip.pk}-reason': str(reason_id),
                f'trip_{self.mechanic_source_trip.pk}-comment': comment,
            },
            HTTP_HOST='localhost',
        )
        return self.response_result(
            response,
            actor_id=employee_id,
            kind='mechanic',
        )

    def post_operator_start(self, session_key):
        client = self.client_for_session_key(session_key)
        response = client.post(
            reverse('excavator_downtime_action'),
            data=json.dumps({
                'action': 'start',
                'reason_id': self.operator_reason.pk,
                'comment': 'Авторский комментарий машиниста',
            }),
            content_type='application/json',
            HTTP_ACCEPT='application/json',
            HTTP_HOST='localhost',
        )
        return self.response_result(
            response,
            actor_id=self.operator.pk,
            kind='operator',
        )

    def post_mechanic_close(self, session_key, event_id):
        client = self.client_for_session_key(session_key)
        response = client.post(
            reverse('mechanic_close_downtime', args=[event_id]),
            HTTP_HOST='localhost',
        )
        return self.response_result(
            response,
            actor_id=self.mechanic_one.pk,
            kind='mechanic_close',
        )

    def post_successful_load(self, session_key):
        client = self.client_for_session_key(session_key)
        response = client.post(
            reverse('excavator_truck_loaded'),
            data=json.dumps({
                'client_action_id': 'chaos-pg-005-load-mechanic-close',
                'truck_id': self.load_truck.pk,
                'excavator_id': self.excavator.pk,
                'dump_point_id': self.dump_point.pk,
                'rock_type_id': self.rock.pk,
                'loading_horizon': '210',
                'loading_block': '7',
            }),
            content_type='application/json',
            HTTP_ACCEPT='application/json',
            HTTP_HOST='localhost',
        )
        return self.response_result(
            response,
            actor_id=self.operator.pk,
            kind='load',
        )

    def assert_no_server_error(self, results):
        for result in results:
            self.assertIsNone(result['error'], result)
            self.assertIsNone(result.get('exc_info'), result)
            self.assertLess(result['status'], 500, result)

    def assert_one_open_excavator_event(self):
        open_events = DowntimeEvent.objects.filter(
            equipment=self.excavator,
            ended_at__isnull=True,
        )
        self.assertEqual(open_events.count(), 1)
        return open_events.get()

    def assert_mechanic_loser_response(self, result):
        self.assertEqual(result['status'], 302, result)
        self.assertTrue(
            any(
                'уже есть открытый простой' in message
                for message in result['messages']
            ),
            result,
        )

    def test_two_mechanics_create_only_one_open_downtime(self):
        first_session = self.session_key_for_access(self.mechanic_one_access)
        second_session = self.session_key_for_access(self.mechanic_two_access)
        critical_point = Barrier(2)
        original_is_valid = MechanicDowntimeCreateForm.is_valid

        def coordinated_is_valid(form):
            is_valid = original_is_valid(form)
            if is_valid:
                self.wait_for_competitor(critical_point)
            return is_valid

        comments_by_employee = {
            self.mechanic_one.pk: 'Авторский комментарий механика 1',
            self.mechanic_two.pk: 'Авторский комментарий механика 2',
        }
        reasons_by_employee = {
            self.mechanic_one.pk: self.mechanic_reason_one.pk,
            self.mechanic_two.pk: self.mechanic_reason_two.pk,
        }

        with patch.object(
            MechanicDowntimeCreateForm,
            'is_valid',
            new=coordinated_is_valid,
        ):
            results = self.run_pair(
                lambda: self.post_mechanic_create(
                    first_session,
                    self.mechanic_one.pk,
                    self.mechanic_reason_one.pk,
                    comments_by_employee[self.mechanic_one.pk],
                ),
                lambda: self.post_mechanic_create(
                    second_session,
                    self.mechanic_two.pk,
                    self.mechanic_reason_two.pk,
                    comments_by_employee[self.mechanic_two.pk],
                ),
            )

        self.assert_no_server_error(results)
        event = self.assert_one_open_excavator_event()
        self.assertIn(event.employee_id, reasons_by_employee)
        self.assertEqual(event.reason_id, reasons_by_employee[event.employee_id])
        self.assertEqual(event.comment, comments_by_employee[event.employee_id])

        loser = next(
            result
            for result in results
            if result['actor_id'] != event.employee_id
        )
        self.assert_mechanic_loser_response(loser)

    def test_mechanic_and_operator_create_only_one_open_excavator_downtime(self):
        mechanic_session = self.session_key_for_access(self.mechanic_one_access)
        operator_session = self.session_key_for_access(self.operator_access)
        critical_point = Barrier(2)
        original_is_valid = MechanicDowntimeCreateForm.is_valid
        mechanic_comment = 'Авторский комментарий механика в гонке с машинистом'

        def coordinated_is_valid(form):
            is_valid = original_is_valid(form)
            if is_valid:
                self.wait_for_competitor(critical_point)
            return is_valid

        def coordinated_operator_payload(request):
            payload = excavator_json_payload(request)
            self.wait_for_competitor(critical_point)
            return payload

        with (
            patch.object(
                MechanicDowntimeCreateForm,
                'is_valid',
                new=coordinated_is_valid,
            ),
            patch(
                'trips.views.excavator_json_payload',
                new=coordinated_operator_payload,
            ),
        ):
            results = self.run_pair(
                lambda: self.post_mechanic_create(
                    mechanic_session,
                    self.mechanic_one.pk,
                    self.mechanic_reason_one.pk,
                    mechanic_comment,
                ),
                lambda: self.post_operator_start(operator_session),
            )

        self.assert_no_server_error(results)
        event = self.assert_one_open_excavator_event()
        result_by_kind = {result['kind']: result for result in results}

        if event.employee_id == self.mechanic_one.pk:
            self.assertEqual(event.reason_id, self.mechanic_reason_one.pk)
            self.assertEqual(event.comment, mechanic_comment)
            operator_result = result_by_kind['operator']
            self.assertEqual(operator_result['status'], 409, operator_result)
            self.assertIsNotNone(operator_result['json'], operator_result)
            self.assertFalse(operator_result['json'].get('ok'), operator_result)
            self.assertIn(
                'простой',
                str(operator_result['json'].get('error', '')).lower(),
                operator_result,
            )
        elif event.employee_id == self.operator.pk:
            self.assertEqual(event.reason_id, self.operator_reason.pk)
            self.assertEqual(event.comment, 'Авторский комментарий машиниста')
            self.assert_mechanic_loser_response(result_by_kind['mechanic'])
        else:
            self.fail(f'Неожиданный автор конкурентного простоя: {event.employee_id}')

    def test_successful_loading_and_mechanic_close_keep_one_end_boundary_and_audit(self):
        event = DowntimeEvent.objects.create(
            equipment=self.excavator,
            employee=self.mechanic_one,
            reason=self.mechanic_reason_one,
            started_at=timezone.now(),
            comment='Закрывается конкурентной погрузкой',
        )
        mechanic_session = self.session_key_for_access(self.mechanic_one_access)
        operator_session = self.session_key_for_access(self.operator_access)
        thread_state = local()
        production_saved = Event()
        release_production = Event()
        mechanic_equipment_lock_attempted = Event()
        production_ended_at = {}
        original_event_save = DowntimeEvent.save
        original_queryset_get = QuerySet.get

        def coordinated_event_save(instance, *args, **kwargs):
            result = original_event_save(instance, *args, **kwargs)
            update_fields = kwargs.get('update_fields') or ()
            if (
                getattr(thread_state, 'kind', '') == 'load'
                and instance.pk == event.pk
                and instance.ended_at is not None
                and 'ended_at' in update_fields
            ):
                production_ended_at['value'] = instance.ended_at
                production_saved.set()
                if not release_production.wait(timeout=10):
                    raise TimeoutError('Погрузка не получила разрешение завершить транзакцию.')
            return result

        def observed_queryset_get(queryset, *args, **kwargs):
            if (
                getattr(thread_state, 'kind', '') == 'mechanic_close'
                and queryset.model is Equipment
                and queryset.query.select_for_update
            ):
                mechanic_equipment_lock_attempted.set()
            return original_queryset_get(queryset, *args, **kwargs)

        def worker(kind, callable_):
            close_old_connections()
            thread_state.kind = kind
            try:
                return callable_()
            except Exception as error:
                return {
                    'actor_id': None,
                    'kind': kind,
                    'status': 599,
                    'messages': [],
                    'json': None,
                    'exc_info': None,
                    'error': f'{type(error).__name__}: {error}',
                }
            finally:
                close_old_connections()

        with (
            patch.object(DowntimeEvent, 'save', new=coordinated_event_save),
            patch.object(QuerySet, 'get', new=observed_queryset_get),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            load_future = executor.submit(
                worker,
                'load',
                lambda: self.post_successful_load(operator_session),
            )
            mechanic_future = None
            try:
                if not production_saved.wait(timeout=10):
                    if load_future.done():
                        self.fail(
                            'Погрузка завершилась до production barrier: '
                            f'{load_future.result()!r}'
                        )
                    self.fail(
                        'Погрузка не сохранила production ended_at под блокировкой.'
                    )
                mechanic_future = executor.submit(
                    worker,
                    'mechanic_close',
                    lambda: self.post_mechanic_close(mechanic_session, event.pk),
                )
                if not mechanic_equipment_lock_attempted.wait(timeout=10):
                    if mechanic_future.done():
                        self.fail(
                            'Закрытие Механиком завершилось до Equipment-lock: '
                            f'{mechanic_future.result()!r}'
                        )
                    self.fail(
                        'Механик не использовал Equipment FOR UPDATE перед закрытием.'
                    )
                if mechanic_future.done():
                    self.fail(
                        'Закрытие Механиком завершилось до commit погрузки: '
                        f'{mechanic_future.result()!r}'
                    )
            finally:
                release_production.set()

            load_result = load_future.result(timeout=30)
            mechanic_result = mechanic_future.result(timeout=30) if mechanic_future else None

        results = (load_result, mechanic_result)
        self.assert_no_server_error(results)
        self.assertEqual(load_result['status'], 200, load_result)
        self.assertIsNotNone(load_result['json'], load_result)
        self.assertTrue(load_result['json'].get('ok'), load_result)
        self.assertEqual(mechanic_result['status'], 302, mechanic_result)

        event.refresh_from_db()
        self.assertIsNotNone(event.ended_at)
        self.assertEqual(event.ended_at, production_ended_at.get('value'))
        self.assertEqual(
            Trip.objects.filter(
                truck=self.load_truck,
                status=TripStatus.LOADED_WAITING_UNLOAD,
            ).count(),
            1,
        )

        close_audits = OperationalStateEvent.objects.filter(
            object_type='DowntimeEvent',
            object_id=str(event.pk),
            payload__action='downtime_closed',
        )
        self.assertEqual(close_audits.count(), 1)
        self.assertEqual(
            close_audits.get().payload.get('ended_at'),
            event.ended_at.isoformat(),
        )
