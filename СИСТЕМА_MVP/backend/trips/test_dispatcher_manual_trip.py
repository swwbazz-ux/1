"""Карточка самосвала на пульте: ручной рейс диспетчера и признак чужой смены.

Ручной рейс идёт обычной формой (редирект + сообщение), создаёт сразу
выполненные рейсы водителю открытой смены и пишет журнал действий —
как служебное завершение рейса, только без исходного открытого рейса.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, ExcavatorPlacement, HaulAssignment
from references.models import DumpPoint, Equipment, EquipmentModel, EquipmentType, RockType, TruckCapacityRule
from shifts.models import EmployeeShift
from trips.models import DispatcherActionLog, DispatcherActionType, Trip, TripStatus
from trips.views import dispatcher_manual_trip_payload, dispatcher_shift_period_fields
from users.models import Employee, EmployeeAccess, Role

BUSINESS_TZ = ZoneInfo('Asia/Vladivostok')


class DispatcherManualTripTests(TestCase):
    def setUp(self):
        self.dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.driver_role = Role.objects.create(code='driver', name='Водитель')
        self.dispatcher = Employee.objects.create(
            full_name='Дежурный диспетчер',
            phone='79000000500',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=self.dispatcher_role,
            access_code='500000',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        session = self.client.session
        session['employee_access_id'] = self.access.id
        session['device_kind'] = 'personal'
        session.save()
        self.client.post(reverse('dispatcher_toggle_shift'), {'shift_action': 'start'})
        self.assertTrue(
            EmployeeShift.objects.filter(employee=self.dispatcher, closed_at__isnull=True).exists(),
            'смена диспетчера должна открыться',
        )

        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck_model = EquipmentModel.objects.create(equipment_type=truck_type, name='БелАЗ тест')
        self.truck = Equipment.objects.create(equipment_type=truck_type, model=truck_model, garage_number='101')
        self.excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='2')
        self.rock = RockType.objects.create(name='Скальная порода', density='2.6000', loosening_factor='1.5000')
        TruckCapacityRule.objects.create(equipment_model=truck_model, rock_type=self.rock, volume_m3='80.00')
        self.dump_point = DumpPoint.objects.create(name='Отвал 60')
        ExcavatorPlacement.objects.create(
            excavator=self.excavator,
            work_rock_type=self.rock,
            work_dump_point=self.dump_point,
            transport_distance_km=Decimal('4.20'),
            loading_horizon='90',
        )
        self.driver = Employee.objects.create(
            full_name='Водитель Тестовый',
            phone='79000000777',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.truck_shift = EmployeeShift.objects.create(
            employee=self.driver,
            equipment=self.truck,
            shift_type='day',
            opened_at=timezone.now() - timedelta(hours=2),
        )
        self.assignment = HaulAssignment.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            status=AssignmentStatus.ACCEPTED,
            assigned_by=self.dispatcher,
        )
        self.url = reverse('dispatcher_manual_trip', args=[self.truck.id])

    def post_manual_trip(self, **overrides):
        payload = {
            'excavator_id': self.excavator.id,
            'dump_point_id': self.dump_point.id,
            'rock_type_id': self.rock.id,
            'trips_count': '2',
            'completed_at': '',
            'reason': 'водитель не отметил разгрузку',
        }
        payload.update(overrides)
        return self.client.post(self.url, payload)

    def messages_text(self, response):
        return ' | '.join(str(message) for message in get_messages(response.wsgi_request))

    def test_manual_trips_are_created_completed_for_driver_shift_and_logged(self):
        response = self.post_manual_trip()

        self.assertRedirects(response, reverse('dispatcher_control'), fetch_redirect_response=False)
        self.assertIn('добавлено 2 рейса вручную', self.messages_text(response))
        trips = list(Trip.objects.filter(truck=self.truck).order_by('completed_at'))
        self.assertEqual(len(trips), 2)
        for trip in trips:
            self.assertEqual(trip.status, TripStatus.COMPLETED)
            self.assertEqual(trip.driver, self.driver)
            self.assertEqual(trip.unloading_shift, self.truck_shift)
            self.assertEqual(trip.excavator, self.excavator)
            self.assertEqual(trip.dump_point, self.dump_point)
            self.assertEqual(trip.actual_dump_point, self.dump_point)
            self.assertEqual(trip.rock_type, self.rock)
            self.assertEqual(trip.volume_m3, Decimal('80.00'))
            self.assertEqual(trip.tonnage, Decimal('208.00'))
            self.assertEqual(trip.transport_distance_km, Decimal('4.20'))
            self.assertEqual(trip.loading_horizon, '90')
            self.assertIn('Добавлен диспетчером вручную', trip.note)
            self.assertIsNotNone(trip.completed_at)
        self.assertLess(trips[0].completed_at, trips[1].completed_at)
        logs = DispatcherActionLog.objects.filter(action_type=DispatcherActionType.MANUAL_TRIP)
        self.assertEqual(logs.count(), 2)
        self.assertEqual(logs.first().actor, self.dispatcher)
        self.assertIn('Отвал 60', logs.first().target_summary)

    def test_manual_trip_accepts_explicit_time_in_business_timezone(self):
        stamp = timezone.now().astimezone(BUSINESS_TZ) - timedelta(minutes=30)
        response = self.post_manual_trip(trips_count='1', completed_at=stamp.strftime('%Y-%m-%dT%H:%M'))

        self.assertIn('добавлено 1 рейс', self.messages_text(response))
        trip = Trip.objects.get(truck=self.truck)
        self.assertEqual(
            trip.completed_at.astimezone(BUSINESS_TZ).strftime('%Y-%m-%d %H:%M'),
            stamp.strftime('%Y-%m-%d %H:%M'),
        )

    def test_manual_trip_requires_reason_and_open_driver_shift(self):
        response = self.post_manual_trip(reason='')
        self.assertIn('Укажите причину', self.messages_text(response))
        self.assertFalse(Trip.objects.exists())

        self.truck_shift.closed_at = timezone.now()
        self.truck_shift.save(update_fields=['closed_at'])
        response = self.post_manual_trip()
        self.assertIn('нет открытой смены водителя', self.messages_text(response))
        self.assertFalse(Trip.objects.exists())

    def test_manual_trip_rejects_future_time_and_wrong_excavator(self):
        future = (timezone.now().astimezone(BUSINESS_TZ) + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M')
        response = self.post_manual_trip(completed_at=future)
        self.assertIn('не может быть в будущем', self.messages_text(response))

        response = self.post_manual_trip(excavator_id=self.excavator.id + 100)
        self.assertIn('больше не назначен', self.messages_text(response))
        self.assertFalse(Trip.objects.exists())

    def test_manual_trip_payload_offers_face_destinations_and_blocks_without_assignment(self):
        placement = ExcavatorPlacement.objects.get(excavator=self.excavator)
        payload = dispatcher_manual_trip_payload(
            self.truck,
            excavator=self.excavator,
            placement=placement,
            truck_shift=self.truck_shift,
            rock_types=[self.rock],
            dump_points=[self.dump_point],
        )
        self.assertTrue(payload['can_add'])
        self.assertEqual(payload['url'], self.url)
        self.assertEqual(payload['excavator_id'], self.excavator.id)
        self.assertEqual(payload['rock_type_id'], self.rock.id)
        self.assertEqual(payload['destinations'][0]['dump_point_id'], self.dump_point.id)
        self.assertEqual(payload['destinations'][0]['transport_distance_km'], '4.20')

        blocked = dispatcher_manual_trip_payload(
            self.truck,
            excavator=None,
            placement=None,
            truck_shift=self.truck_shift,
            rock_types=[],
            dump_points=[],
        )
        self.assertFalse(blocked['can_add'])
        self.assertIn('не назначен в комплекс', blocked['blocked_reason'])


class DispatcherShiftPeriodFieldsTests(TestCase):
    """Чья смена: открыта в текущем периоде или это хвост прошлого водителя."""

    def setUp(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        self.truck = Equipment.objects.create(equipment_type=truck_type, garage_number='101')
        self.driver = Employee.objects.create(
            full_name='Водитель Тестовый',
            phone='79000000777',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )

    def shift(self, opened_at, shift_type):
        return EmployeeShift.objects.create(
            employee=self.driver,
            equipment=self.truck,
            shift_type=shift_type,
            opened_at=opened_at,
        )

    def test_current_period_shift_is_marked_current(self):
        now = datetime(2026, 9, 12, 10, 0, tzinfo=BUSINESS_TZ)
        fields = dispatcher_shift_period_fields(
            self.shift(datetime(2026, 9, 12, 7, 15, tzinfo=BUSINESS_TZ), 'day'),
            now=now,
        )
        self.assertEqual(fields['verdict'], 'current')
        self.assertEqual(fields['period_label'], 'Первая смена 12.09')
        self.assertEqual(fields['duration_label'], '2 ч 45 мин')
        self.assertEqual(fields['alert'], '')

    def test_previous_night_shift_left_open_is_marked_stale(self):
        now = datetime(2026, 9, 12, 10, 0, tzinfo=BUSINESS_TZ)
        fields = dispatcher_shift_period_fields(
            self.shift(datetime(2026, 9, 11, 19, 5, tzinfo=BUSINESS_TZ), 'night'),
            now=now,
        )
        self.assertEqual(fields['verdict'], 'stale')
        self.assertEqual(fields['period_label'], 'Вторая смена 11.09')
        self.assertEqual(fields['current_period_label'], 'Первая смена 12.09')
        self.assertIn('водитель прошлой смены не закрыл её', fields['alert'])
        self.assertIn('14 ч 55 мин', fields['alert'])

    def test_handover_window_shift_is_marked_overlap_not_stale(self):
        now = datetime(2026, 9, 12, 7, 30, tzinfo=BUSINESS_TZ)
        fields = dispatcher_shift_period_fields(
            self.shift(datetime(2026, 9, 12, 6, 40, tzinfo=BUSINESS_TZ), 'night'),
            now=now,
        )
        self.assertEqual(fields['verdict'], 'overlap')
        self.assertIn('Проверьте, что это нынешний водитель', fields['alert'])
