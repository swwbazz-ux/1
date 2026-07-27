from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, override_settings

from assignments.services import production_work_date
from assignments.views import get_shift_type_for_now as mining_master_shift_type
from core.production_time import (
    BUSINESS_TIME_ZONE_NAME,
    production_shift_context,
)
from shifts.models import ShiftType
from trips.dispatcher_header import get_dispatcher_shift_type_for_now
from trips.views import default_excavator_shift_type
from reports.shift_analytics import dynamics_bucket, downtime_shift_type


VLADIVOSTOK = ZoneInfo(BUSINESS_TIME_ZONE_NAME)


class ProductionTimeContractTests(SimpleTestCase):
    def assert_contract(self, local_value, expected_shift, expected_date):
        context = production_shift_context(local_value)

        self.assertEqual(context.shift_type, expected_shift)
        self.assertEqual(context.production_date, expected_date)
        self.assertEqual(get_dispatcher_shift_type_for_now(local_value), expected_shift)
        self.assertEqual(mining_master_shift_type(local_value), expected_shift)
        self.assertEqual(default_excavator_shift_type(local_value), expected_shift)
        self.assertEqual(production_work_date(local_value), expected_date)
        bucket_key, _bucket_label = dynamics_bucket(local_value, 'shift')
        self.assertEqual(bucket_key, f'{expected_date:%Y-%m-%d}-{expected_shift}')
        self.assertEqual(
            downtime_shift_type(SimpleNamespace(started_at=local_value)),
            expected_shift,
        )

    def test_boundaries_use_confirmed_vladivostok_contract(self):
        self.assert_contract(
            datetime(2026, 7, 23, 6, 59, 59, tzinfo=VLADIVOSTOK),
            ShiftType.NIGHT,
            date(2026, 7, 22),
        )
        self.assert_contract(
            datetime(2026, 7, 23, 7, 0, 0, tzinfo=VLADIVOSTOK),
            ShiftType.DAY,
            date(2026, 7, 23),
        )
        self.assert_contract(
            datetime(2026, 7, 23, 18, 59, 59, tzinfo=VLADIVOSTOK),
            ShiftType.DAY,
            date(2026, 7, 23),
        )
        self.assert_contract(
            datetime(2026, 7, 23, 19, 0, 0, tzinfo=VLADIVOSTOK),
            ShiftType.NIGHT,
            date(2026, 7, 23),
        )
        self.assert_contract(
            datetime(2026, 7, 23, 23, 59, 59, tzinfo=VLADIVOSTOK),
            ShiftType.NIGHT,
            date(2026, 7, 23),
        )
        self.assert_contract(
            datetime(2026, 7, 24, 0, 0, 0, tzinfo=VLADIVOSTOK),
            ShiftType.NIGHT,
            date(2026, 7, 23),
        )
        self.assert_contract(
            datetime(2026, 7, 24, 1, 30, 0, tzinfo=VLADIVOSTOK),
            ShiftType.NIGHT,
            date(2026, 7, 23),
        )

    @override_settings(TIME_ZONE='Europe/Samara')
    def test_django_or_device_zone_does_not_change_business_shift(self):
        same_instant_utc = datetime(2026, 7, 23, 15, 30, 0, tzinfo=ZoneInfo('UTC'))
        same_instant_samara = same_instant_utc.astimezone(ZoneInfo('Europe/Samara'))
        same_instant_vladivostok = same_instant_utc.astimezone(VLADIVOSTOK)

        contexts = [
            production_shift_context(value)
            for value in (same_instant_utc, same_instant_samara, same_instant_vladivostok)
        ]

        self.assertEqual({item.shift_type for item in contexts}, {ShiftType.NIGHT})
        self.assertEqual({item.production_date for item in contexts}, {date(2026, 7, 23)})
