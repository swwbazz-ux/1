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
from core.production_time import production_shift_bounds, production_shift_context
from references.models import DumpPoint, Equipment, EquipmentModel, EquipmentType, RockType, TruckCapacityRule
from shifts.models import EmployeeShift
from trips.models import DispatcherActionLog, DispatcherActionType, Trip, TripStatus
from trips.views import dispatcher_manual_trip_payload, dispatcher_shift_period_fields
from users.models import Employee, EmployeeAccess, Role

BUSINESS_TZ = ZoneInfo('Asia/Vladivostok')


def current_period_opened_at(hours_ago=2):
    """Смена, открытая внутри текущей производственной смены.

    Автозакрытие считает отсечку от конца производственного периода, поэтому
    «живая» смена в тестах должна принадлежать текущему периоду, иначе результат
    зависел бы от времени суток на машине.
    """
    context = production_shift_context()
    period_start, _ = production_shift_bounds(context.production_date, context.shift_type)
    return context.shift_type, max(period_start, timezone.now() - timedelta(hours=hours_ago))


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
        shift_type, opened_at = current_period_opened_at()
        self.truck_shift = EmployeeShift.objects.create(
            employee=self.driver,
            equipment=self.truck,
            shift_type=shift_type,
            opened_at=opened_at,
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


class DispatcherServiceCloseWithoutReadingsTests(TestCase):
    """Служебное закрытие смены: показания необязательны, введённые — проверяются."""

    def setUp(self):
        self.dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
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

        truck_type = EquipmentType.objects.create(name='Самосвал')
        truck_model = EquipmentModel.objects.create(equipment_type=truck_type, name='БелАЗ тест', fuel_capacity_limit_l=1500)
        self.truck = Equipment.objects.create(equipment_type=truck_type, model=truck_model, garage_number='48')
        self.driver = Employee.objects.create(
            full_name='Водитель Прошлой Смены',
            phone='79000000778',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.truck_shift = EmployeeShift.objects.create(
            employee=self.driver,
            equipment=self.truck,
            shift_type='night',
            opened_at=timezone.now() - timedelta(hours=40),
            start_fuel=780,
            start_mileage=63253,
            start_engine_hours=5705,
        )
        self.current_shift_type, self.current_shift_opened_at = current_period_opened_at()
        self.url = reverse('dispatcher_service_close_shift', args=[self.truck_shift.id])

    def messages_text(self, response):
        return ' | '.join(str(message) for message in get_messages(response.wsgi_request))

    def test_neglected_close_needs_only_one_click(self):
        """«Не закрыл сам»: без причины и показаний, в журнале — отметка о невыполненной обязанности."""
        response = self.client.post(self.url, {'close_kind': 'neglected'})

        self.assertIn('сотрудник не закрыл её сам', self.messages_text(response))
        self.truck_shift.refresh_from_db()
        self.assertIsNotNone(self.truck_shift.closed_at)
        self.assertTrue(self.truck_shift.is_service_closed)
        self.assertEqual(self.truck_shift.closed_by, self.dispatcher)
        self.assertEqual(self.truck_shift.service_close_kind, 'neglected')
        self.assertIn('не закрыл смену сам', self.truck_shift.service_close_note)
        self.assertIsNone(self.truck_shift.end_fuel)
        self.assertIsNone(self.truck_shift.end_mileage)
        self.assertIsNone(self.truck_shift.end_engine_hours)
        log = DispatcherActionLog.objects.get(shift=self.truck_shift)
        self.assertEqual(log.reason, self.truck_shift.service_close_note)

    def test_neglected_close_ignores_readings_even_if_sent(self):
        response = self.client.post(self.url, {'close_kind': 'neglected', 'end_fuel': '500'})

        self.assertIn('сотрудник не закрыл её сам', self.messages_text(response))
        self.truck_shift.refresh_from_db()
        self.assertIsNotNone(self.truck_shift.closed_at)
        self.assertIsNone(self.truck_shift.end_fuel)

    def test_coordinated_close_requires_reason_and_keeps_optional_readings(self):
        response = self.client.post(self.url, {'close_kind': 'coordinated', 'reason': ''})
        self.assertIn('Укажите причину закрытия смены по согласованию', self.messages_text(response))
        self.truck_shift.refresh_from_db()
        self.assertIsNone(self.truck_shift.closed_at)

        response = self.client.post(self.url, {
            'close_kind': 'coordinated',
            'reason': 'попросил по рации, нет интернета',
            'end_fuel': '',
            'end_mileage': '',
            'end_engine_hours': '',
        })
        self.assertIn('закрыта по согласованию', self.messages_text(response))
        self.truck_shift.refresh_from_db()
        self.assertIsNotNone(self.truck_shift.closed_at)
        self.assertEqual(self.truck_shift.service_close_kind, 'coordinated')
        self.assertEqual(self.truck_shift.service_close_note, 'попросил по рации, нет интернета')
        self.assertIsNone(self.truck_shift.end_fuel)

    def test_coordinated_partial_readings_are_still_validated(self):
        response = self.client.post(self.url, {
            'close_kind': 'coordinated',
            'reason': 'попросил по рации',
            'end_fuel': '500',
            'end_mileage': '',
            'end_engine_hours': '',
        })

        self.assertIn('Укажите показание на конец смены', self.messages_text(response))
        self.truck_shift.refresh_from_db()
        self.assertIsNone(self.truck_shift.closed_at)

    def test_legacy_form_without_kind_is_coordinated_when_reason_given(self):
        response = self.client.post(self.url, {'reason': 'из шапки пульта'})

        self.assertIn('закрыта по согласованию', self.messages_text(response))
        self.truck_shift.refresh_from_db()
        self.assertEqual(self.truck_shift.service_close_kind, 'coordinated')

    def test_equipment_shifts_are_cut_off_at_eight_and_twenty(self):
        """Смены техники отрубаются в 08:00 и 20:00; сменщика отсечка не задевает."""
        from trips.views import auto_close_expired_equipment_shifts, shift_auto_close_at

        def equipment_shift(opened_at, shift_type='day'):
            return EmployeeShift(
                employee=self.driver,
                equipment=self.truck,
                workplace_code='driver',
                shift_type=shift_type,
                opened_at=opened_at,
            )

        cases = [
            # обычная первая смена: работа до 19:00, отсечка в 20:00
            (equipment_shift(datetime(2026, 9, 12, 7, 5, tzinfo=BUSINESS_TZ)),
             datetime(2026, 9, 12, 20, 0, tzinfo=BUSINESS_TZ)),
            # ранний комплекс: работа до 18:00, отсечка та же
            (equipment_shift(datetime(2026, 9, 12, 6, 10, tzinfo=BUSINESS_TZ)),
             datetime(2026, 9, 12, 20, 0, tzinfo=BUSINESS_TZ)),
            # сменщик заступил в восемь утра — его нельзя рубить этим же утром
            (equipment_shift(datetime(2026, 9, 12, 8, 10, tzinfo=BUSINESS_TZ)),
             datetime(2026, 9, 12, 20, 0, tzinfo=BUSINESS_TZ)),
            # вторая смена: работа до 07:00, отсечка в 08:00 следующего утра
            (equipment_shift(datetime(2026, 9, 12, 19, 5, tzinfo=BUSINESS_TZ), 'night'),
             datetime(2026, 9, 13, 8, 0, tzinfo=BUSINESS_TZ)),
            # ранняя вторая смена и сменщик, заступивший после восьми вечера
            (equipment_shift(datetime(2026, 9, 12, 18, 10, tzinfo=BUSINESS_TZ), 'night'),
             datetime(2026, 9, 13, 8, 0, tzinfo=BUSINESS_TZ)),
            (equipment_shift(datetime(2026, 9, 12, 20, 10, tzinfo=BUSINESS_TZ), 'night'),
             datetime(2026, 9, 13, 8, 0, tzinfo=BUSINESS_TZ)),
        ]
        for shift, expected in cases:
            with self.subTest(opened=shift.opened_at.astimezone(BUSINESS_TZ).strftime('%H:%M')):
                self.assertEqual(shift_auto_close_at(shift), expected)

        # Живая смена текущего периода не трогается, просроченная закрывается.
        self.truck_shift.shift_type, self.truck_shift.opened_at = current_period_opened_at(1)
        self.truck_shift.save(update_fields=['shift_type', 'opened_at'])
        self.assertEqual(auto_close_expired_equipment_shifts(), [])

        self.truck_shift.opened_at = timezone.now() - timedelta(hours=20)
        self.truck_shift.save(update_fields=['opened_at'])
        closed = auto_close_expired_equipment_shifts()
        self.assertEqual([shift.id for shift in closed], [self.truck_shift.id])
        self.truck_shift.refresh_from_db()
        self.assertIsNotNone(self.truck_shift.closed_at)
        self.assertTrue(self.truck_shift.is_service_closed)
        self.assertIsNone(self.truck_shift.closed_by)
        self.assertEqual(self.truck_shift.service_close_kind, 'auto_expired')
        self.assertIn('не закрыл её сам', self.truck_shift.service_close_note)
        self.assertEqual(auto_close_expired_equipment_shifts(), [], 'повторный запуск ничего не трогает')

    def test_dispatcher_and_mining_master_close_half_an_hour_after_their_own_shift(self):
        """У диспетчера и горного мастера смена с 8 до 20, отсечка 20:30 и 08:30."""
        from trips.views import shift_auto_close_at

        cases = [
            ('dispatcher', datetime(2026, 9, 12, 8, 5, tzinfo=BUSINESS_TZ), datetime(2026, 9, 12, 20, 30, tzinfo=BUSINESS_TZ)),
            ('dispatcher', datetime(2026, 9, 12, 10, 0, tzinfo=BUSINESS_TZ), datetime(2026, 9, 12, 20, 30, tzinfo=BUSINESS_TZ)),
            ('dispatcher', datetime(2026, 9, 12, 20, 5, tzinfo=BUSINESS_TZ), datetime(2026, 9, 13, 8, 30, tzinfo=BUSINESS_TZ)),
            ('dispatcher', datetime(2026, 9, 12, 2, 0, tzinfo=BUSINESS_TZ), datetime(2026, 9, 12, 8, 30, tzinfo=BUSINESS_TZ)),
            ('mining_master', datetime(2026, 9, 12, 8, 10, tzinfo=BUSINESS_TZ), datetime(2026, 9, 12, 20, 30, tzinfo=BUSINESS_TZ)),
            ('mining_master', datetime(2026, 9, 12, 20, 10, tzinfo=BUSINESS_TZ), datetime(2026, 9, 13, 8, 30, tzinfo=BUSINESS_TZ)),
        ]
        for workplace_code, opened_at, expected in cases:
            with self.subTest(workplace=workplace_code, opened=opened_at.strftime('%H:%M')):
                shift = EmployeeShift(
                    employee=self.driver,
                    workplace_code=workplace_code,
                    shift_type='day',
                    opened_at=opened_at,
                )
                self.assertEqual(shift_auto_close_at(shift), expected)

        # У техники своя отсечка: ровно 20:00, без получаса на сдачу дел.
        equipment_shift = EmployeeShift(
            employee=self.driver,
            equipment=self.truck,
            workplace_code='driver',
            shift_type='day',
            opened_at=datetime(2026, 9, 12, 7, 5, tzinfo=BUSINESS_TZ),
        )
        self.assertEqual(
            shift_auto_close_at(equipment_shift),
            datetime(2026, 9, 12, 20, 0, tzinfo=BUSINESS_TZ),
        )

    def test_overdue_dispatcher_shift_is_closed_by_the_auto_pass(self):
        from trips.views import auto_close_expired_equipment_shifts

        stale_dispatcher = Employee.objects.create(
            full_name='Диспетчер прошлой смены',
            phone='79000000502',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        stale_shift = EmployeeShift.objects.create(
            employee=stale_dispatcher,
            workplace_code='dispatcher',
            shift_type='day',
            opened_at=timezone.now() - timedelta(hours=20),
        )

        closed_ids = [shift.id for shift in auto_close_expired_equipment_shifts()]

        self.assertIn(stale_shift.id, closed_ids)
        stale_shift.refresh_from_db()
        self.assertIsNotNone(stale_shift.closed_at)
        self.assertEqual(stale_shift.service_close_kind, 'auto_expired')

    def test_shift_opened_late_still_closes_at_its_shift_boundary(self):
        """Опоздавший не получает лишних часов: окно определяется периодом смены."""
        from trips.views import shift_auto_close_at

        late = EmployeeShift(
            employee=self.driver,
            equipment=self.truck,
            workplace_code='driver',
            shift_type='day',
            opened_at=datetime(2026, 9, 12, 9, 30, tzinfo=BUSINESS_TZ),
        )
        self.assertEqual(
            shift_auto_close_at(late),
            datetime(2026, 9, 12, 20, 0, tzinfo=BUSINESS_TZ),
        )

    def test_service_close_ends_open_downtimes_of_the_equipment(self):
        from downtimes.models import DowntimeEvent, DowntimeReason

        reason, _ = DowntimeReason.objects.get_or_create(name='Ожидание погрузки')
        downtime = DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=reason,
            started_at=timezone.now() - timedelta(hours=3),
        )

        self.client.post(self.url, {'close_kind': 'neglected'})

        downtime.refresh_from_db()
        self.truck_shift.refresh_from_db()
        self.assertIsNotNone(downtime.ended_at)
        self.assertEqual(downtime.ended_at, self.truck_shift.closed_at)

    def test_auto_pass_ends_orphan_downtimes_but_keeps_dispatcher_ones(self):
        from downtimes.models import DowntimeEvent, DowntimeEventSource, DowntimeReason
        from trips.views import auto_close_expired_equipment_shifts

        reason, _ = DowntimeReason.objects.get_or_create(name='Ожидание самосвалов')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='9')
        # смена водителя в этом тесте текущая — иначе её закроет сам проход
        self.truck_shift.shift_type = self.current_shift_type
        self.truck_shift.opened_at = self.current_shift_opened_at
        self.truck_shift.save(update_fields=['shift_type', 'opened_at'])
        orphan = DowntimeEvent.objects.create(
            equipment=excavator,
            reason=reason,
            started_at=timezone.now() - timedelta(hours=32),
        )
        manual = DowntimeEvent.objects.create(
            equipment=excavator,
            reason=reason,
            source=DowntimeEventSource.DISPATCHER_OVERRIDE,
            started_at=timezone.now() - timedelta(hours=32),
        )
        alive = DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=reason,
            started_at=timezone.now() - timedelta(hours=1),
        )

        auto_close_expired_equipment_shifts()

        orphan.refresh_from_db()
        manual.refresh_from_db()
        alive.refresh_from_db()
        self.assertIsNotNone(orphan.ended_at, 'простой техники без смены закрывается')
        self.assertIsNone(manual.ended_at, 'ручной простой диспетчера остаётся')
        self.assertIsNone(alive.ended_at, 'простой при открытой смене живёт')

    def test_excavator_auto_downtime_does_not_start_without_open_shift(self):
        from downtimes.models import DowntimeEvent, DowntimeReason
        from trips.views import EXCAVATOR_AUTO_DOWNTIME_WAITING_TRUCKS, start_excavator_auto_downtime

        DowntimeReason.objects.get_or_create(name=EXCAVATOR_AUTO_DOWNTIME_WAITING_TRUCKS)
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='9')

        self.assertIsNone(start_excavator_auto_downtime(excavator, None, EXCAVATOR_AUTO_DOWNTIME_WAITING_TRUCKS))
        self.assertFalse(DowntimeEvent.objects.filter(equipment=excavator).exists())

    def test_dispatcher_board_load_closes_expired_shifts(self):
        self.truck_shift.opened_at = timezone.now() - timedelta(hours=20)
        self.truck_shift.save(update_fields=['opened_at'])

        response = self.client.get(reverse('dispatcher_control'))

        self.assertEqual(response.status_code, 200)
        self.truck_shift.refresh_from_db()
        self.assertEqual(self.truck_shift.service_close_kind, 'auto_expired')


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
