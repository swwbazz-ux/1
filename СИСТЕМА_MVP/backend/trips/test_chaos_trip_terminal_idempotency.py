from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from html.parser import HTMLParser
from threading import Barrier, BrokenBarrierError
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.contrib.messages import get_messages
from django.db import close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from core.models import OperationalStateEvent
from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import (
    Dormitory,
    DormitoryBlock,
    DormitorySection,
    DumpPoint,
    Equipment,
    EquipmentModel,
    EquipmentType,
    RockType,
)
from shifts.models import EmployeeShift, ShiftClientAction
from shifts.services import close_driver_shift
from trips.models import (
    DispatcherActionLog,
    DispatcherActionType,
    Trip,
    TripClientAction,
    TripStatus,
)
from trips.views import EXCAVATOR_AUTO_DOWNTIME_COMMENT, finalize_trip_unloaded
from users.models import DriverPrimaryRegistration, Employee, EmployeeAccess, Role


class TripActionReasonInputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current_form_action = None
        self.reason_inputs = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'form':
            self.current_form_action = attributes.get('action')
        elif (
            tag == 'input'
            and self.current_form_action
            and attributes.get('name') == 'reason'
        ):
            self.reason_inputs[self.current_form_action] = attributes

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current_form_action = None


class TripTerminalFixtureMixin:
    def setUp(self):
        super().setUp()
        self.truck_type = EquipmentType.objects.create(name='Самосвал')
        self.excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck_model = EquipmentModel.objects.create(
            equipment_type=self.truck_type,
            name='Тестовая модель самосвала',
            body_volume_m3=Decimal('47.00'),
            fuel_capacity_limit_l=2000,
        )
        self.excavator = Equipment.objects.create(
            equipment_type=self.excavator_type,
            garage_number='ЭКС-CHAOS',
        )
        self.truck_one = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number='СА-CHAOS-1',
        )
        self.truck_two = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number='СА-CHAOS-2',
        )
        self.rock = RockType.objects.create(name='Руда CHAOS', density=Decimal('2.5000'))
        self.dump_point = DumpPoint.objects.create(name='ККД CHAOS')

        self.driver_role = Role.objects.create(code='driver', name='Водитель')
        self.dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.driver_one = Employee.objects.create(
            full_name='Водитель CHAOS 1',
            status=Employee.Status.ACTIVE,
        )
        self.driver_two = Employee.objects.create(
            full_name='Водитель CHAOS 2',
            status=Employee.Status.ACTIVE,
        )
        self.dispatcher = Employee.objects.create(
            full_name='Диспетчер CHAOS',
            status=Employee.Status.ACTIVE,
        )
        self.driver_one_access = EmployeeAccess.objects.create(
            employee=self.driver_one,
            role=self.driver_role,
            access_code='chaos-driver-1',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        self.driver_two_access = EmployeeAccess.objects.create(
            employee=self.driver_two,
            role=self.driver_role,
            access_code='chaos-driver-2',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        self.dispatcher_access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=self.dispatcher_role,
            access_code='chaos-dispatcher',
            status=EmployeeAccess.Status.ACTIVATED,
        )

        dormitory = Dormitory.objects.create(number='CHAOS')
        dormitory_block = DormitoryBlock.objects.create(dormitory=dormitory, name='A')
        dormitory_section = DormitorySection.objects.create(block=dormitory_block, name='1')
        DriverPrimaryRegistration.objects.create(
            employee=self.driver_one,
            dormitory_section=dormitory_section,
        )
        DriverPrimaryRegistration.objects.create(
            employee=self.driver_two,
            dormitory_section=dormitory_section,
        )

        opened_at = timezone.now()
        self.driver_one_shift = EmployeeShift.objects.create(
            employee=self.driver_one,
            shift_type='day',
            equipment=self.truck_one,
            start_fuel=Decimal('1000.00'),
            start_mileage=Decimal('10000.00'),
            start_engine_hours=Decimal('1000.00'),
            opened_at=opened_at,
            opened_by=self.driver_one,
        )
        self.driver_two_shift = EmployeeShift.objects.create(
            employee=self.driver_two,
            shift_type='day',
            equipment=self.truck_two,
            start_fuel=Decimal('1000.00'),
            start_mileage=Decimal('10000.00'),
            start_engine_hours=Decimal('1000.00'),
            opened_at=opened_at,
            opened_by=self.driver_two,
        )
        self.dispatcher_shift = EmployeeShift.objects.create(
            employee=self.dispatcher,
            shift_type='day',
            opened_at=opened_at,
            opened_by=self.dispatcher,
        )

    def create_trip(self, *, truck=None, status=TripStatus.LOADED_WAITING_UNLOAD, **overrides):
        values = {
            'excavator': self.excavator,
            'truck': truck or self.truck_one,
            'rock_type': self.rock,
            'dump_point': self.dump_point,
            'assigned_dump_point': self.dump_point,
            'actual_dump_point': self.dump_point,
            'volume_m3': Decimal('47.00'),
            'tonnage': Decimal('117.50'),
            'status': status,
        }
        values.update(overrides)
        return Trip.objects.create(**values)

    def client_for_access(self, access):
        client = Client()
        session = client.session
        session['employee_access_id'] = access.pk
        session.save()
        return client

    def session_key_for_access(self, access):
        return self.client_for_access(access).session.session_key

    @staticmethod
    def client_for_session_key(session_key):
        client = Client(raise_request_exception=False)
        client.cookies[settings.SESSION_COOKIE_NAME] = session_key
        return client

    @staticmethod
    def close_readings():
        return {
            'end_fuel': Decimal('900.00'),
            'end_mileage': Decimal('10100.00'),
            'end_engine_hours': Decimal('1010.00'),
        }

    def post_driver_unload(self, client, trip, client_action_id):
        return client.post(
            reverse('driver_complete_trip', args=[trip.pk]),
            data={'client_action_id': client_action_id},
            HTTP_HOST='localhost',
        )

    def post_dispatcher_cancel(self, client, trip):
        return client.post(
            reverse('dispatcher_cancel_trip', args=[trip.pk]),
            data={'reason': 'Конкурентная отмена'},
            HTTP_HOST='localhost',
        )

    def post_dispatcher_complete(self, client, trip):
        return client.post(
            reverse('dispatcher_complete_trip', args=[trip.pk]),
            data={'reason': 'Конкурентное служебное завершение'},
            HTTP_HOST='localhost',
        )


class TripTerminalSequentialRegressionTests(TripTerminalFixtureMixin, TestCase):
    def test_dispatcher_terminal_actions_reject_missing_blank_and_whitespace_reason(self):
        dispatcher_client = self.client_for_access(self.dispatcher_access)
        invalid_payloads = (
            ('missing', {}),
            ('blank', {'reason': ''}),
            ('whitespace', {'reason': ' \t\r\n '}),
        )
        endpoints = (
            (
                'cancel',
                'dispatcher_cancel_trip',
                'Укажите причину отмены рейса.',
            ),
            (
                'complete',
                'dispatcher_complete_trip',
                'Укажите причину служебного завершения рейса.',
            ),
        )

        for action_name, route_name, expected_error in endpoints:
            for payload_name, payload in invalid_payloads:
                with self.subTest(action=action_name, payload=payload_name):
                    trip = self.create_trip()
                    audit_count_before = DispatcherActionLog.objects.count()
                    state_count_before = OperationalStateEvent.objects.filter(
                        object_type='Trip',
                        object_id=str(trip.pk),
                    ).count()

                    response = dispatcher_client.post(
                        reverse(route_name, args=[trip.pk]),
                        data=payload,
                        HTTP_HOST='localhost',
                    )

                    trip.refresh_from_db()
                    response_messages = [
                        str(message)
                        for message in get_messages(response.wsgi_request)
                    ]
                    self.assertEqual(response.status_code, 302)
                    self.assertIn(expected_error, response_messages)
                    self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
                    self.assertIsNone(trip.completed_at)
                    self.assertIsNone(trip.driver_id)
                    self.assertIsNone(trip.unloading_shift_id)
                    self.assertEqual(
                        DispatcherActionLog.objects.count(),
                        audit_count_before,
                    )
                    self.assertEqual(
                        OperationalStateEvent.objects.filter(
                            object_type='Trip',
                            object_id=str(trip.pk),
                        ).count(),
                        state_count_before,
                    )

    def test_dispatcher_terminal_actions_accept_trimmed_reason_and_write_full_audit(self):
        dispatcher_client = self.client_for_access(self.dispatcher_access)
        scenarios = (
            (
                'cancel',
                'dispatcher_cancel_trip',
                DispatcherActionType.CANCEL_TRIP,
                TripStatus.CANCELLED,
                'Отмена по подтвержденной причине',
            ),
            (
                'complete',
                'dispatcher_complete_trip',
                DispatcherActionType.COMPLETE_TRIP,
                TripStatus.COMPLETED,
                'Служебное завершение по подтвержденной причине',
            ),
        )

        for action_name, route_name, action_type, expected_status, reason in scenarios:
            with self.subTest(action=action_name):
                trip = self.create_trip()
                requested_at = timezone.now()

                response = dispatcher_client.post(
                    reverse(route_name, args=[trip.pk]),
                    data={'reason': f'  {reason}  '},
                    HTTP_HOST='localhost',
                )

                trip.refresh_from_db()
                self.assertEqual(response.status_code, 302)
                self.assertEqual(trip.status, expected_status)
                action = DispatcherActionLog.objects.get(
                    trip=trip,
                    action_type=action_type,
                )
                self.assertEqual(action.actor, self.dispatcher)
                self.assertEqual(action.reason, reason)
                self.assertEqual(
                    action.target_summary,
                    f'{trip.truck} -> {trip.dump_point}',
                )
                self.assertGreaterEqual(action.created_at, requested_at)
                self.assertLessEqual(action.created_at, timezone.now())
                self.assertEqual(
                    DispatcherActionLog.objects.filter(trip=trip).count(),
                    1,
                )
                if expected_status == TripStatus.COMPLETED:
                    self.assertIsNotNone(trip.completed_at)
                    self.assertEqual(trip.driver, self.driver_one)
                    self.assertEqual(trip.unloading_shift, self.driver_one_shift)
                else:
                    self.assertIsNone(trip.completed_at)
                    self.assertIsNone(trip.driver_id)
                    self.assertIsNone(trip.unloading_shift_id)
                    self.assertIsNotNone(trip.cancelled_at)

    def test_repeated_dispatcher_terminal_actions_keep_first_result_and_single_audit(self):
        dispatcher_client = self.client_for_access(self.dispatcher_access)
        scenarios = (
            (
                'cancel',
                'dispatcher_cancel_trip',
                DispatcherActionType.CANCEL_TRIP,
                TripStatus.CANCELLED,
            ),
            (
                'complete',
                'dispatcher_complete_trip',
                DispatcherActionType.COMPLETE_TRIP,
                TripStatus.COMPLETED,
            ),
        )

        for action_name, route_name, action_type, expected_status in scenarios:
            with self.subTest(action=action_name):
                trip = self.create_trip()
                action_url = reverse(route_name, args=[trip.pk])

                first_response = dispatcher_client.post(
                    action_url,
                    data={'reason': 'Первая подтвержденная причина'},
                    HTTP_HOST='localhost',
                )
                trip.refresh_from_db()
                terminal_snapshot = (
                    trip.status,
                    trip.completed_at,
                    trip.driver_id,
                    trip.unloading_shift_id,
                )
                second_response = dispatcher_client.post(
                    action_url,
                    data={'reason': 'Повторная причина не должна попасть в аудит'},
                    HTTP_HOST='localhost',
                )

                trip.refresh_from_db()
                self.assertEqual(first_response.status_code, 302)
                self.assertEqual(second_response.status_code, 302)
                self.assertEqual(trip.status, expected_status)
                self.assertEqual(
                    (
                        trip.status,
                        trip.completed_at,
                        trip.driver_id,
                        trip.unloading_shift_id,
                    ),
                    terminal_snapshot,
                )
                actions = DispatcherActionLog.objects.filter(
                    trip=trip,
                    action_type=action_type,
                )
                self.assertEqual(actions.count(), 1)
                self.assertEqual(
                    actions.get().reason,
                    'Первая подтвержденная причина',
                )

    def test_dispatcher_terminal_forms_render_required_reason_inputs(self):
        trip = self.create_trip()
        dispatcher_client = self.client_for_access(self.dispatcher_access)

        response = dispatcher_client.get(
            reverse('dispatcher_control'),
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        parser = TripActionReasonInputParser()
        parser.feed(response.content.decode('utf-8'))
        for route_name in (
            'dispatcher_cancel_trip',
            'dispatcher_complete_trip',
        ):
            action_url = reverse(route_name, args=[trip.pk])
            with self.subTest(action=route_name):
                self.assertIn(action_url, parser.reason_inputs)
                self.assertIn('required', parser.reason_inputs[action_url])

    def test_service_close_shift_reason_validation_is_preserved(self):
        dispatcher_client = self.client_for_access(self.dispatcher_access)
        invalid_payloads = (
            ('missing', {}),
            ('blank', {'reason': ''}),
            ('whitespace', {'reason': ' \t\r\n '}),
        )

        for payload_name, payload in invalid_payloads:
            with self.subTest(payload=payload_name):
                response = dispatcher_client.post(
                    reverse(
                        'dispatcher_service_close_shift',
                        args=[self.driver_one_shift.pk],
                    ),
                    data=payload,
                    HTTP_HOST='localhost',
                )

                self.driver_one_shift.refresh_from_db()
                response_messages = [
                    str(message)
                    for message in get_messages(response.wsgi_request)
                ]
                self.assertEqual(response.status_code, 302)
                self.assertIn(
                    'Укажите причину служебного закрытия смены.',
                    response_messages,
                )
                self.assertIsNone(self.driver_one_shift.closed_at)
                self.assertFalse(self.driver_one_shift.is_service_closed)
                self.assertFalse(
                    DispatcherActionLog.objects.filter(
                        shift=self.driver_one_shift,
                        action_type=DispatcherActionType.SERVICE_CLOSE_SHIFT,
                    ).exists()
                )

    def test_dispatcher_cancel_reconciles_waiting_when_truck_becomes_loadable(self):
        HaulAssignment.objects.create(
            truck=self.truck_one,
            excavator=self.excavator,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        trip = self.create_trip()
        waiting_reason = DowntimeReason.objects.get(name='Ожидание самосвалов')
        waiting_reason.equipment_type = self.excavator_type
        waiting_reason.show_for_excavator_operator = True
        waiting_reason.is_active = True
        waiting_reason.save(
            update_fields=[
                'equipment_type',
                'show_for_excavator_operator',
                'is_active',
            ],
        )
        waiting = DowntimeEvent.objects.create(
            equipment=self.excavator,
            reason=waiting_reason,
            started_at=timezone.now(),
            comment=EXCAVATOR_AUTO_DOWNTIME_COMMENT,
        )
        dispatcher_client = self.client_for_access(self.dispatcher_access)

        response = self.post_dispatcher_cancel(dispatcher_client, trip)

        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        waiting.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        self.assertIsNotNone(waiting.ended_at)

    def test_repeated_finalize_does_not_overwrite_completed_terminal_fact(self):
        original_completed_at = timezone.now()
        trip = self.create_trip(
            status=TripStatus.COMPLETED,
            driver=self.driver_one,
            unloading_shift=self.driver_one_shift,
            completed_at=original_completed_at,
        )

        finalize_trip_unloaded(
            trip,
            driver=self.driver_two,
            unloading_shift=self.driver_two_shift,
        )

        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.completed_at, original_completed_at)
        self.assertEqual(trip.driver, self.driver_one)
        self.assertEqual(trip.unloading_shift, self.driver_one_shift)

    def test_finalize_cannot_turn_cancelled_trip_into_completed_trip(self):
        trip = self.create_trip(
            status=TripStatus.CANCELLED,
            driver=None,
            unloading_shift=None,
            completed_at=None,
        )

        finalize_trip_unloaded(
            trip,
            driver=self.driver_one,
            unloading_shift=self.driver_one_shift,
        )

        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        self.assertIsNone(trip.completed_at)
        self.assertIsNone(trip.driver)
        self.assertIsNone(trip.unloading_shift)

    def test_sequential_unload_then_dispatcher_cancel_keeps_completed_winner(self):
        trip = self.create_trip()
        driver_client = self.client_for_access(self.driver_one_access)
        dispatcher_client = self.client_for_access(self.dispatcher_access)

        self.post_driver_unload(driver_client, trip, 'sequential-unload-first')
        trip.refresh_from_db()
        original_completed_at = trip.completed_at
        self.post_dispatcher_cancel(dispatcher_client, trip)

        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.completed_at, original_completed_at)
        self.assertEqual(trip.driver, self.driver_one)
        self.assertEqual(trip.unloading_shift, self.driver_one_shift)
        self.assertEqual(TripClientAction.objects.filter(action_type='trip_unloaded').count(), 1)
        self.assertFalse(
            DispatcherActionLog.objects.filter(
                trip=trip,
                action_type=DispatcherActionType.CANCEL_TRIP,
            ).exists()
        )

    def test_sequential_dispatcher_cancel_then_unload_keeps_cancelled_winner(self):
        trip = self.create_trip()
        dispatcher_client = self.client_for_access(self.dispatcher_access)
        driver_client = self.client_for_access(self.driver_one_access)

        self.post_dispatcher_cancel(dispatcher_client, trip)
        self.post_driver_unload(driver_client, trip, 'sequential-cancel-first')

        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        self.assertIsNone(trip.completed_at)
        self.assertIsNone(trip.driver)
        self.assertIsNone(trip.unloading_shift)
        self.assertFalse(
            TripClientAction.objects.filter(
                action_type='trip_unloaded',
                client_action_id='sequential-cancel-first',
            ).exists()
        )
        self.assertEqual(
            DispatcherActionLog.objects.filter(
                trip=trip,
                action_type=DispatcherActionType.CANCEL_TRIP,
            ).count(),
            1,
        )

    def test_sequential_service_complete_then_unload_creates_one_terminal_audit(self):
        trip = self.create_trip()
        dispatcher_client = self.client_for_access(self.dispatcher_access)
        driver_client = self.client_for_access(self.driver_one_access)

        self.post_dispatcher_complete(dispatcher_client, trip)
        trip.refresh_from_db()
        original_completed_at = trip.completed_at
        self.post_driver_unload(driver_client, trip, 'sequential-service-first')

        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.completed_at, original_completed_at)
        self.assertEqual(trip.driver, self.driver_one)
        self.assertEqual(trip.unloading_shift, self.driver_one_shift)
        self.assertEqual(
            DispatcherActionLog.objects.filter(
                trip=trip,
                action_type=DispatcherActionType.COMPLETE_TRIP,
            ).count(),
            1,
        )
        self.assertFalse(
            TripClientAction.objects.filter(
                action_type='trip_unloaded',
                client_action_id='sequential-service-first',
            ).exists()
        )

    def test_sequential_unload_then_service_complete_creates_one_terminal_audit(self):
        trip = self.create_trip()
        driver_client = self.client_for_access(self.driver_one_access)
        dispatcher_client = self.client_for_access(self.dispatcher_access)

        self.post_driver_unload(driver_client, trip, 'sequential-driver-first')
        trip.refresh_from_db()
        original_completed_at = trip.completed_at
        self.post_dispatcher_complete(dispatcher_client, trip)

        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.completed_at, original_completed_at)
        self.assertEqual(
            TripClientAction.objects.filter(
                action_type='trip_unloaded',
                client_action_id='sequential-driver-first',
            ).count(),
            1,
        )
        self.assertFalse(
            DispatcherActionLog.objects.filter(
                trip=trip,
                action_type=DispatcherActionType.COMPLETE_TRIP,
            ).exists()
        )

    def test_trip_action_same_id_same_object_returns_one_terminal_result(self):
        trip = self.create_trip()
        driver_client = self.client_for_access(self.driver_one_access)

        first = self.post_driver_unload(driver_client, trip, 'same-trip-action')
        trip.refresh_from_db()
        original_completed_at = trip.completed_at
        second = self.post_driver_unload(driver_client, trip, 'same-trip-action')

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.completed_at, original_completed_at)
        action = TripClientAction.objects.get(
            action_type='trip_unloaded',
            client_action_id='same-trip-action',
        )
        self.assertEqual(action.trip, trip)

    def test_trip_action_same_id_different_object_returns_original_result(self):
        first_trip = self.create_trip()
        second_trip = self.create_trip()
        driver_client = self.client_for_access(self.driver_one_access)

        first = self.post_driver_unload(driver_client, first_trip, 'stale-tab-trip-action')
        second = self.post_driver_unload(driver_client, second_trip, 'stale-tab-trip-action')

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        first_trip.refresh_from_db()
        second_trip.refresh_from_db()
        self.assertEqual(first_trip.status, TripStatus.COMPLETED)
        self.assertEqual(second_trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        action = TripClientAction.objects.get(
            action_type='trip_unloaded',
            client_action_id='stale-tab-trip-action',
        )
        self.assertEqual(action.trip, first_trip)

    def test_shift_action_same_id_same_object_returns_one_shift_result(self):
        first_shift, first_created = close_driver_shift(
            shift=self.driver_one_shift,
            employee=self.driver_one,
            readings=self.close_readings(),
            client_action_id='same-shift-action',
        )
        second_shift, second_created = close_driver_shift(
            shift=self.driver_one_shift,
            employee=self.driver_one,
            readings=self.close_readings(),
            client_action_id='same-shift-action',
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_shift.pk, second_shift.pk)
        self.assertEqual(
            ShiftClientAction.objects.filter(
                action_type='driver_shift_closed',
                client_action_id='same-shift-action',
            ).count(),
            1,
        )

    def test_shift_action_same_id_different_object_returns_original_result(self):
        first_shift, first_created = close_driver_shift(
            shift=self.driver_one_shift,
            employee=self.driver_one,
            readings=self.close_readings(),
            client_action_id='stale-tab-shift-action',
        )
        second_shift, second_created = close_driver_shift(
            shift=self.driver_two_shift,
            employee=self.driver_two,
            readings=self.close_readings(),
            client_action_id='stale-tab-shift-action',
        )

        self.driver_two_shift.refresh_from_db()
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_shift.pk, second_shift.pk)
        self.assertIsNone(self.driver_two_shift.closed_at)
        action = ShiftClientAction.objects.get(
            action_type='driver_shift_closed',
            client_action_id='stale-tab-shift-action',
        )
        self.assertEqual(action.shift, first_shift)

    def test_driver_http_retry_after_closed_shift_returns_saved_result(self):
        close_driver_shift(
            shift=self.driver_one_shift,
            employee=self.driver_one,
            readings=self.close_readings(),
            client_action_id='lost-http-shift-close',
        )
        driver_client = self.client_for_access(self.driver_one_access)

        response = driver_client.post(
            reverse('driver_close_shift'),
            data={
                'end_fuel': '900.00',
                'end_mileage': '10100.00',
                'end_engine_hours': '1010.00',
                'client_action_id': 'lost-http-shift-close',
                'shift_action': 'close',
            },
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            ShiftClientAction.objects.filter(
                action_type='driver_shift_closed',
                client_action_id='lost-http-shift-close',
            ).count(),
            1,
        )


@skipUnless(
    connection.vendor == 'postgresql',
    'Настоящая конкурентная гарантия проверяется только на тестовой PostgreSQL.',
)
class TripTerminalPostgreSQLConcurrencyTests(TripTerminalFixtureMixin, TransactionTestCase):
    def run_pair(self, first_callable, second_callable):
        start = Barrier(2)

        def worker(callable_):
            close_old_connections()
            try:
                start.wait(timeout=10)
                return callable_()
            except Exception as error:  # Результат нужен тесту, а не test-client traceback.
                return {
                    'status': 500,
                    'messages': [],
                    'exc_info': None,
                    'error': f'{type(error).__name__}: {error}',
                }
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(worker, first_callable)
            second_future = executor.submit(worker, second_callable)
            return first_future.result(timeout=30), second_future.result(timeout=30)

    def post_in_thread(self, session_key, url, data):
        client = self.client_for_session_key(session_key)
        response = client.post(url, data=data, HTTP_HOST='localhost')
        exc_info = getattr(response, 'exc_info', None)
        return {
            'status': response.status_code,
            'messages': [str(message) for message in get_messages(response.wsgi_request)],
            'exc_info': exc_info,
            'error': (
                f'{exc_info[0].__name__}: {exc_info[1]}'
                if exc_info
                else None
            ),
        }

    def assert_no_server_error(self, results):
        for result in results:
            self.assertIsNone(result['error'], result)
            self.assertIsNone(result.get('exc_info'), result)
            self.assertLess(result['status'], 500, result)

    @staticmethod
    def wait_for_competitor(barrier):
        try:
            barrier.wait(timeout=2)
        except BrokenBarrierError:
            # После исправления проигравший запрос может не дойти до мутации:
            # он будет ждать row lock и штатно завершится после победителя.
            pass

    def assert_single_coherent_terminal_fact(self, trip):
        trip.refresh_from_db()
        unload_actions = TripClientAction.objects.filter(
            trip=trip,
            action_type='trip_unloaded',
        ).count()
        dispatcher_actions = DispatcherActionLog.objects.filter(
            trip=trip,
            action_type__in=[
                DispatcherActionType.CANCEL_TRIP,
                DispatcherActionType.COMPLETE_TRIP,
            ],
        ).count()
        self.assertEqual(unload_actions + dispatcher_actions, 1)
        if trip.status == TripStatus.COMPLETED:
            self.assertIsNotNone(trip.completed_at)
            self.assertIsNotNone(trip.driver)
            self.assertIsNotNone(trip.unloading_shift)
        elif trip.status == TripStatus.CANCELLED:
            self.assertIsNone(trip.completed_at)
            self.assertIsNone(trip.driver)
            self.assertIsNone(trip.unloading_shift)
        else:
            self.fail(f'Получен нетерминальный статус после конфликта: {trip.status}')

    def test_unload_races_dispatcher_cancel(self):
        trip = self.create_trip()
        driver_session = self.session_key_for_access(self.driver_one_access)
        dispatcher_session = self.session_key_for_access(self.dispatcher_access)
        save_barrier = Barrier(2)
        original_save = Trip.save

        def coordinated_save(instance, *args, **kwargs):
            if (
                instance.pk == trip.pk
                and 'status' in set(kwargs.get('update_fields') or [])
            ):
                self.wait_for_competitor(save_barrier)
            return original_save(instance, *args, **kwargs)

        with patch.object(Trip, 'save', new=coordinated_save):
            results = self.run_pair(
                lambda: self.post_in_thread(
                    driver_session,
                    reverse('driver_complete_trip', args=[trip.pk]),
                    {'client_action_id': 'pg-unload-vs-cancel'},
                ),
                lambda: self.post_in_thread(
                    dispatcher_session,
                    reverse('dispatcher_cancel_trip', args=[trip.pk]),
                    {'reason': 'PG конкурентная отмена'},
                ),
            )

        self.assert_no_server_error(results)
        self.assert_single_coherent_terminal_fact(trip)

    def test_unload_races_dispatcher_service_complete(self):
        trip = self.create_trip()
        driver_session = self.session_key_for_access(self.driver_one_access)
        dispatcher_session = self.session_key_for_access(self.dispatcher_access)
        finalize_barrier = Barrier(2)
        original_finalize = finalize_trip_unloaded

        def coordinated_finalize(*args, **kwargs):
            self.wait_for_competitor(finalize_barrier)
            return original_finalize(*args, **kwargs)

        with patch('trips.views.finalize_trip_unloaded', new=coordinated_finalize):
            results = self.run_pair(
                lambda: self.post_in_thread(
                    driver_session,
                    reverse('driver_complete_trip', args=[trip.pk]),
                    {'client_action_id': 'pg-unload-vs-service'},
                ),
                lambda: self.post_in_thread(
                    dispatcher_session,
                    reverse('dispatcher_complete_trip', args=[trip.pk]),
                    {'reason': 'PG служебное завершение'},
                ),
            )

        self.assert_no_server_error(results)
        self.assert_single_coherent_terminal_fact(trip)

    def test_two_unloads_with_different_action_ids(self):
        trip = self.create_trip()
        first_session = self.session_key_for_access(self.driver_one_access)
        second_session = self.session_key_for_access(self.driver_one_access)

        results = self.run_pair(
            lambda: self.post_in_thread(
                first_session,
                reverse('driver_complete_trip', args=[trip.pk]),
                {'client_action_id': 'pg-unload-a'},
            ),
            lambda: self.post_in_thread(
                second_session,
                reverse('driver_complete_trip', args=[trip.pk]),
                {'client_action_id': 'pg-unload-b'},
            ),
        )

        self.assert_no_server_error(results)
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(
            TripClientAction.objects.filter(trip=trip, action_type='trip_unloaded').count(),
            1,
        )

    def test_two_dispatcher_service_completions(self):
        trip = self.create_trip()
        first_session = self.session_key_for_access(self.dispatcher_access)
        second_session = self.session_key_for_access(self.dispatcher_access)
        finalize_barrier = Barrier(2)
        original_finalize = finalize_trip_unloaded

        def coordinated_finalize(*args, **kwargs):
            self.wait_for_competitor(finalize_barrier)
            return original_finalize(*args, **kwargs)

        with patch('trips.views.finalize_trip_unloaded', new=coordinated_finalize):
            results = self.run_pair(
                lambda: self.post_in_thread(
                    first_session,
                    reverse('dispatcher_complete_trip', args=[trip.pk]),
                    {'reason': 'PG завершение A'},
                ),
                lambda: self.post_in_thread(
                    second_session,
                    reverse('dispatcher_complete_trip', args=[trip.pk]),
                    {'reason': 'PG завершение B'},
                ),
            )

        self.assert_no_server_error(results)
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(
            DispatcherActionLog.objects.filter(
                trip=trip,
                action_type=DispatcherActionType.COMPLETE_TRIP,
            ).count(),
            1,
        )

    def test_same_trip_action_id_same_object_returns_success_to_both_clients(self):
        trip = self.create_trip()
        first_session = self.session_key_for_access(self.driver_one_access)
        second_session = self.session_key_for_access(self.driver_one_access)
        action_save_barrier = Barrier(2)
        original_save = TripClientAction.save

        def delayed_action_save(instance, *args, **kwargs):
            if (
                instance.action_type == 'trip_unloaded'
                and instance.client_action_id == 'pg-same-trip-object'
            ):
                self.wait_for_competitor(action_save_barrier)
            return original_save(instance, *args, **kwargs)

        with patch.object(TripClientAction, 'save', new=delayed_action_save):
            results = self.run_pair(
                lambda: self.post_in_thread(
                    first_session,
                    reverse('driver_complete_trip', args=[trip.pk]),
                    {'client_action_id': 'pg-same-trip-object'},
                ),
                lambda: self.post_in_thread(
                    second_session,
                    reverse('driver_complete_trip', args=[trip.pk]),
                    {'client_action_id': 'pg-same-trip-object'},
                ),
            )

        self.assert_no_server_error(results)
        for result in results:
            self.assertFalse(
                any('Активный рейс не найден' in message for message in result['messages']),
                result['messages'],
            )
        action = TripClientAction.objects.get(
            action_type='trip_unloaded',
            client_action_id='pg-same-trip-object',
        )
        self.assertEqual(action.trip, trip)

    def test_same_trip_action_id_different_objects_returns_original_result(self):
        first_trip = self.create_trip(truck=self.truck_one)
        second_trip = self.create_trip(truck=self.truck_two)
        first_session = self.session_key_for_access(self.driver_one_access)
        second_session = self.session_key_for_access(self.driver_two_access)
        action_save_barrier = Barrier(2)
        original_save = TripClientAction.save

        def coordinated_action_save(instance, *args, **kwargs):
            if (
                instance.action_type == 'trip_unloaded'
                and instance.client_action_id == 'pg-different-trip-objects'
            ):
                self.wait_for_competitor(action_save_barrier)
            return original_save(instance, *args, **kwargs)

        with patch.object(TripClientAction, 'save', new=coordinated_action_save):
            results = self.run_pair(
                lambda: self.post_in_thread(
                    first_session,
                    reverse('driver_complete_trip', args=[first_trip.pk]),
                    {'client_action_id': 'pg-different-trip-objects'},
                ),
                lambda: self.post_in_thread(
                    second_session,
                    reverse('driver_complete_trip', args=[second_trip.pk]),
                    {'client_action_id': 'pg-different-trip-objects'},
                ),
            )

        self.assert_no_server_error(results)
        action = TripClientAction.objects.get(
            action_type='trip_unloaded',
            client_action_id='pg-different-trip-objects',
        )
        first_trip.refresh_from_db()
        second_trip.refresh_from_db()
        completed = [
            item.pk
            for item in (first_trip, second_trip)
            if item.status == TripStatus.COMPLETED
        ]
        self.assertEqual(completed, [action.trip_id])
        self.assertEqual(
            TripClientAction.objects.filter(
                action_type='trip_unloaded',
                client_action_id='pg-different-trip-objects',
            ).count(),
            1,
        )

    def close_shift_in_thread(self, shift_id, employee_id, client_action_id):
        try:
            shift = EmployeeShift.objects.get(pk=shift_id)
            employee = Employee.objects.get(pk=employee_id)
            result_shift, _created = close_driver_shift(
                shift=shift,
                employee=employee,
                readings=self.close_readings(),
                client_action_id=client_action_id,
            )
            return {'shift_id': result_shift.pk, 'error': None}
        except Exception as error:
            return {
                'shift_id': None,
                'error': f'{type(error).__name__}: {error}',
            }

    def run_shift_pair(self, first_callable, second_callable):
        start = Barrier(2)

        def worker(callable_):
            close_old_connections()
            try:
                start.wait(timeout=10)
                return callable_()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(worker, first_callable)
            second_future = executor.submit(worker, second_callable)
            return first_future.result(timeout=30), second_future.result(timeout=30)

    def test_same_shift_action_id_same_object_returns_same_shift(self):
        results = self.run_shift_pair(
            lambda: self.close_shift_in_thread(
                self.driver_one_shift.pk,
                self.driver_one.pk,
                'pg-same-shift-object',
            ),
            lambda: self.close_shift_in_thread(
                self.driver_one_shift.pk,
                self.driver_one.pk,
                'pg-same-shift-object',
            ),
        )

        for result in results:
            self.assertIsNone(result['error'], result['error'])
            self.assertEqual(result['shift_id'], self.driver_one_shift.pk)
        self.assertEqual(
            ShiftClientAction.objects.filter(
                action_type='driver_shift_closed',
                client_action_id='pg-same-shift-object',
            ).count(),
            1,
        )

    def test_same_shift_action_id_different_objects_returns_original_result(self):
        action_save_barrier = Barrier(2)
        original_save = ShiftClientAction.save

        def coordinated_action_save(instance, *args, **kwargs):
            if (
                instance.action_type == 'driver_shift_closed'
                and instance.client_action_id == 'pg-different-shift-objects'
            ):
                self.wait_for_competitor(action_save_barrier)
            return original_save(instance, *args, **kwargs)

        with patch.object(ShiftClientAction, 'save', new=coordinated_action_save):
            results = self.run_shift_pair(
                lambda: self.close_shift_in_thread(
                    self.driver_one_shift.pk,
                    self.driver_one.pk,
                    'pg-different-shift-objects',
                ),
                lambda: self.close_shift_in_thread(
                    self.driver_two_shift.pk,
                    self.driver_two.pk,
                    'pg-different-shift-objects',
                ),
            )

        action = ShiftClientAction.objects.get(
            action_type='driver_shift_closed',
            client_action_id='pg-different-shift-objects',
        )
        for result in results:
            self.assertIsNone(result['error'], result['error'])
            self.assertEqual(result['shift_id'], action.shift_id)
        self.driver_one_shift.refresh_from_db()
        self.driver_two_shift.refresh_from_db()
        closed = [
            shift.pk
            for shift in (self.driver_one_shift, self.driver_two_shift)
            if shift.closed_at is not None
        ]
        self.assertEqual(closed, [action.shift_id])
