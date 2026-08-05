import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils.dateparse import parse_datetime

from shifts.models import ShiftType
from users.models import WatchComposition

from .driver_rating_materialization import (
    DriverRatingMaterializationError,
    refresh_driver_rating_group,
)
from .driver_rating_scope_membership import (
    discover_driver_rating_group_scope,
)
from .driver_watch_rating import build_driver_rating_period
from .models import (
    DriverRatingPeriodMaterializedSnapshot,
    RatingPeriod,
)
from .test_driver_watch_rating import DriverRatingFixtureMixin


@skipUnless(
    connection.vendor == 'postgresql',
    'Требуется PostgreSQL для проверки advisory lock снимка рейтинга.',
)
@override_settings(PORTAL_SITE_CODE='rating-materialization-pg-tests')
class DriverRatingMaterializationPostgreSQLTests(TransactionTestCase):
    def setUp(self):
        self.composition = WatchComposition.objects.create(
            code='rating-materialization-pg',
            name='Состав PostgreSQL-проверки снимка рейтинга',
        )
        self.rating_period = RatingPeriod.objects.create(
            name='Период PostgreSQL-проверки снимка рейтинга',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
            comment='Изолированная проверка конкурентной публикации.',
        )

    def _refresh(self):
        close_old_connections()
        try:
            period = RatingPeriod.objects.get(pk=self.rating_period.pk)
            composition = WatchComposition.objects.get(
                pk=self.composition.pk,
            )
            return refresh_driver_rating_group(
                period,
                composition,
                shift_type=ShiftType.DAY,
            )
        finally:
            close_old_connections()

    def test_two_workers_publish_once_instead_of_duplicate_calculation(self):
        payload = build_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
            allowed_employee_ids=(),
            expected_employee_ids=(),
        )
        calculation_started = Event()
        release_calculation = Event()
        call_count = 0

        def slow_calculation(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            calculation_started.set()
            if not release_calculation.wait(timeout=20):
                raise RuntimeError('PostgreSQL concurrency test timed out.')
            return copy.deepcopy(payload)

        with patch(
            (
                'reports.driver_rating_materialization.'
                'build_driver_rating_period'
            ),
            side_effect=slow_calculation,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(self._refresh)
                self.assertTrue(calculation_started.wait(timeout=20))
                second = executor.submit(self._refresh)
                second_result = second.result(timeout=20)
                release_calculation.set()
                first_result = first.result(timeout=30)

        self.assertEqual(call_count, 1)
        self.assertEqual(
            {first_result.status, second_result.status},
            {'published', 'locked'},
        )
        self.assertEqual(
            DriverRatingPeriodMaterializedSnapshot.objects.count(),
            1,
        )
        snapshot = DriverRatingPeriodMaterializedSnapshot.objects.get()
        self.assertEqual(snapshot.revision, 1)


@skipUnless(
    connection.vendor == 'postgresql',
    'Требуется PostgreSQL для проверки общего MVCC-снимка рейтинга.',
)
@override_settings(PORTAL_SITE_CODE='rating-materialization-isolation-tests')
class DriverRatingMaterializationSnapshotIsolationPostgreSQLTests(
    DriverRatingFixtureMixin,
    TransactionTestCase,
):
    def setUp(self):
        super().setUp()
        self.rating_period = RatingPeriod.objects.create(
            name='Период MVCC-проверки общего снимка рейтинга',
            starts_on=self.watch.starts_on,
            ends_before=self.watch.ends_on + date.resolution,
            comment='Двухсоединительная проверка REPEATABLE READ.',
        )
        self.driver = self.employee('Водитель MVCC-проверки рейтинга')
        first_passport = self.snapshot(
            self.driver,
            ordinal=2,
            trip_count=20,
        )
        self.first_closed_at = first_passport.shift.closed_at

    def _refresh(self):
        return refresh_driver_rating_group(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )

    def test_membership_and_formula_share_real_repeatable_read_snapshot(self):
        scope_read = Event()
        newer_passport_committed = Event()
        isolation_levels = []
        refresh_backend_pids = []
        writer_backend_pids = []

        def synchronized_scope_discovery(*args, **kwargs):
            scope = discover_driver_rating_group_scope(*args, **kwargs)
            with connection.cursor() as cursor:
                cursor.execute('SHOW transaction_isolation')
                isolation_levels.append(cursor.fetchone()[0])
                cursor.execute('SELECT pg_backend_pid()')
                refresh_backend_pids.append(cursor.fetchone()[0])
            scope_read.set()
            if not newer_passport_committed.wait(timeout=30):
                raise RuntimeError(
                    'Новый паспорт не был зафиксирован вторым соединением.'
                )
            return scope

        def commit_newer_passport():
            close_old_connections()
            try:
                if not scope_read.wait(timeout=30):
                    raise RuntimeError(
                        'Расчёт не прочитал исходный состав группы.'
                    )
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    writer_backend_pids.append(cursor.fetchone()[0])
                newer = self.snapshot(
                    self.driver,
                    ordinal=1,
                    trip_count=23,
                )
                return newer.shift.closed_at
            finally:
                newer_passport_committed.set()
                close_old_connections()

        with patch(
            (
                'reports.driver_rating_materialization.'
                'discover_driver_rating_group_scope'
            ),
            side_effect=synchronized_scope_discovery,
        ):
            with ThreadPoolExecutor(max_workers=1) as executor:
                writer = executor.submit(commit_newer_passport)
                first_result = self._refresh()
                newer_closed_at = writer.result(timeout=30)

        first_snapshot = (
            DriverRatingPeriodMaterializedSnapshot.objects.get(
                pk=first_result.snapshot_id,
            )
        )
        self.assertEqual(isolation_levels, ['repeatable read'])
        self.assertEqual(len(refresh_backend_pids), 1)
        self.assertEqual(len(writer_backend_pids), 1)
        self.assertNotEqual(
            refresh_backend_pids[0],
            writer_backend_pids[0],
        )
        self.assertEqual(first_result.status, 'published')
        self.assertEqual(first_result.revision, 1)
        self.assertEqual(
            first_snapshot.payload['summary']['rated_shift_count'],
            1,
        )
        self.assertEqual(
            parse_datetime(
                first_snapshot.member_latest_closed_at[str(self.driver.id)]
            ),
            self.first_closed_at,
        )

        second_result = self._refresh()
        second_snapshot = (
            DriverRatingPeriodMaterializedSnapshot.objects.get(
                pk=first_result.snapshot_id,
            )
        )
        self.assertEqual(second_result.status, 'published')
        self.assertEqual(second_result.revision, 2)
        self.assertEqual(
            second_snapshot.payload['summary']['rated_shift_count'],
            2,
        )
        self.assertEqual(
            parse_datetime(
                second_snapshot.member_latest_closed_at[str(self.driver.id)]
            ),
            newer_closed_at,
        )

    def test_nested_postgresql_transaction_is_rejected(self):
        with transaction.atomic():
            with self.assertRaisesMessage(
                DriverRatingMaterializationError,
                'отдельной верхнеуровневой транзакции PostgreSQL',
            ):
                self._refresh()
