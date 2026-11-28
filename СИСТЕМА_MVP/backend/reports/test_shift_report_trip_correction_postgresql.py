"""T-030: корректировка рейса в отчётах под блокировкой — регрессия PostgreSQL.

`update_shift_report_trip` берёт рейс через
`Trip.objects.select_for_update().select_related('rock_type', 'actual_dump_point',
'dump_point', 'truck', 'excavator')`. Связь `actual_dump_point` необязательная,
поэтому PostgreSQL строит outer join и отказывает выполнять запрос:
`NotSupportedError: FOR UPDATE cannot be applied to the nullable side of an outer
join`. SQLite такого ограничения не знает, поэтому проверка только для
PostgreSQL.
"""

from datetime import datetime, time
from decimal import Decimal
from unittest import skipUnless

from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import connection
from django.test import RequestFactory, TestCase
from django.utils import timezone

from references.models import DumpPoint, Equipment, EquipmentType, RockType
from shifts.models import EmployeeShift
from trips.models import DispatcherActionLog, Trip, TripStatus
from users.active_role import activate_role_session
from users.models import Employee, EmployeeAccess, Role

from .views import get_report_access, update_shift_report_trip


@skipUnless(
    connection.vendor == 'postgresql',
    'FOR UPDATE на nullable стороне outer join запрещён только на PostgreSQL.',
)
class ShiftReportTripCorrectionPostgreSQLTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        role = Role.objects.create(code='mining_master', name='Горный мастер')
        employee = Employee.objects.create(full_name='Мастер корректировки', status=Employee.Status.ACTIVE)
        self.access = EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code='746382',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    def request(self, data):
        request = self.factory.post('/mining-master/tablet/reports/shift/', data)
        request.session = {}
        request._messages = FallbackStorage(request)
        request.mining_master_tablet_control = True
        activate_role_session(request, self.access)
        return request

    def completed_trip(self):
        day = timezone.localdate()
        completed_at = timezone.make_aware(datetime.combine(day, time(10, 0)))
        excavator = Equipment.objects.create(
            equipment_type=EquipmentType.objects.create(name='Экскаватор'),
            garage_number='Э-91',
        )
        truck = Equipment.objects.create(
            equipment_type=EquipmentType.objects.create(name='Самосвал'),
            garage_number='91',
        )
        trip = Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=RockType.objects.create(name='Порода', density='2.6000'),
            dump_point=DumpPoint.objects.create(name='Отвал'),
            status=TripStatus.COMPLETED,
            completed_at=completed_at,
            volume_m3='10.00',
        )
        return trip, day

    def test_correction_under_lock_updates_trip_without_outer_join_error(self):
        trip, day = self.completed_trip()
        EmployeeShift.objects.create(
            employee=self.access.employee,
            opened_by=self.access.employee,
            workplace_code='mining_master',
            shift_type='day',
            opened_at=timezone.now(),
        )
        request = self.request({
            'trip_id': trip.pk,
            'correction_reason': 'Проверка замера',
            'volume_m3': '12.50',
            'transport_distance_km': '2.10',
        })
        access = get_report_access(request)
        self.assertEqual(access.pk, self.access.pk)

        response = update_shift_report_trip(request, access, day, 'day', 'trucks')

        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.volume_m3, Decimal('12.50'))
        self.assertEqual(trip.transport_distance_km, Decimal('2.10'))
        self.assertEqual(trip.tonnage, Decimal('32.50'))
        log = DispatcherActionLog.objects.get(action_type='report_source_correction')
        self.assertEqual(log.trip_id, trip.pk)
        self.assertEqual(log.actor_id, self.access.employee_id)
