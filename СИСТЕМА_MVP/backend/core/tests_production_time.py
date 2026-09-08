from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, override_settings

from assignments.services import production_work_date
from assignments.views import get_shift_type_for_now as mining_master_shift_type
from core.production_time import (
    BUSINESS_TIME_ZONE_NAME,
    production_shift_context,
    production_shift_label,
    production_work_date_for_shift,
)
from shifts.models import ShiftType
from trips.dispatcher_header import get_dispatcher_shift_type_for_now
from trips.views import default_excavator_shift_type
from reports.shift_analytics import (
    authoritative_loading_shift_window,
    dynamics_bucket,
    downtime_shift_type,
    trip_loading_date,
)


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

    def test_assigned_shift_owns_early_handover_regardless_of_clock_bucket(self):
        early_start = datetime(2026, 9, 8, 7, 52, tzinfo=VLADIVOSTOK)
        early_evening_start = datetime(2026, 9, 8, 18, 52, tzinfo=VLADIVOSTOK)

        self.assertEqual(
            production_work_date_for_shift(early_start, ShiftType.DAY),
            date(2026, 9, 8),
        )
        self.assertEqual(
            production_work_date_for_shift(early_start, ShiftType.NIGHT),
            date(2026, 9, 7),
        )
        self.assertEqual(
            production_work_date_for_shift(early_evening_start, ShiftType.NIGHT),
            date(2026, 9, 8),
        )

    def test_operational_labels_are_numbered_not_clock_names(self):
        self.assertEqual(production_shift_label(ShiftType.DAY), 'Первая смена')
        self.assertEqual(production_shift_label(ShiftType.NIGHT), 'Вторая смена')

    def test_linked_shift_controls_trip_date_and_window_during_handover(self):
        early_start = datetime(2026, 9, 8, 7, 52, tzinfo=VLADIVOSTOK)
        shift = SimpleNamespace(
            shift_type=ShiftType.NIGHT,
            opened_at=early_start,
        )
        trip = SimpleNamespace(
            loading_shift_id=243,
            loading_shift=shift,
        )

        self.assertEqual(trip_loading_date(trip), date(2026, 9, 7))
        production_date, shift_type, _start, _end = authoritative_loading_shift_window(trip)
        self.assertEqual(production_date, date(2026, 9, 7))
        self.assertEqual(shift_type, ShiftType.NIGHT)

        early_evening_start = datetime(2026, 9, 8, 18, 52, tzinfo=VLADIVOSTOK)
        evening_shift = SimpleNamespace(
            shift_type=ShiftType.NIGHT,
            opened_at=early_evening_start,
        )
        evening_trip = SimpleNamespace(
            loading_shift_id=244,
            loading_shift=evening_shift,
        )
        self.assertEqual(trip_loading_date(evening_trip), date(2026, 9, 8))
