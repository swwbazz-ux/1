"""T-031: доказательство дефекта T-030 ДО правки (временный тест, только PG).

Проверяет исходный запрос `update_shift_report_trip` до замены на
`select_for_update(of=('self',))`: PostgreSQL строит outer join из-за
необязательной связи `actual_dump_point` и отказывает в FOR UPDATE.

Файл создан только для доказательства в отчёте T-031; в manifest не входит.
"""

from datetime import datetime, time
from unittest import skipUnless

from django.db import connection, transaction
from django.db.utils import NotSupportedError
from django.test import TestCase
from django.utils import timezone

from references.models import DumpPoint, Equipment, EquipmentType, RockType
from trips.models import Trip, TripStatus


@skipUnless(
    connection.vendor == 'postgresql',
    'FOR UPDATE на nullable стороне outer join запрещён только на PostgreSQL.',
)
class BeforeFixReproTests(TestCase):
    def test_old_query_fails_on_nullable_outer_join(self):
        day = timezone.localdate()
        trip = Trip.objects.create(
            excavator=Equipment.objects.create(
                equipment_type=EquipmentType.objects.create(name='Экскаватор'),
                garage_number='Э-92',
            ),
            truck=Equipment.objects.create(
                equipment_type=EquipmentType.objects.create(name='Самосвал'),
                garage_number='92',
            ),
            rock_type=RockType.objects.create(name='Порода', density='2.6000'),
            dump_point=DumpPoint.objects.create(name='Отвал'),
            status=TripStatus.COMPLETED,
            completed_at=timezone.make_aware(datetime.combine(day, time(10, 0))),
            volume_m3='10.00',
        )

        with self.assertRaises(NotSupportedError):
            with transaction.atomic():
                Trip.objects.select_for_update().select_related(
                    'rock_type', 'actual_dump_point', 'dump_point', 'truck', 'excavator'
                ).get(pk=trip.pk, status=TripStatus.COMPLETED)
