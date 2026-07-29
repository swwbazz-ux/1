from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier, Event
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from reports.models import RatingPeriod
from reports.rating_period_generation import ensure_rating_periods


@skipUnless(
    connection.vendor == 'postgresql',
    'Требуется PostgreSQL для проверки advisory lock периодов рейтинга.',
)
class RatingPeriodGenerationPostgreSQLTests(TransactionTestCase):
    as_of = date(2026, 7, 29)

    @staticmethod
    def _run_pair(first_callable, second_callable):
        start = Barrier(2)

        def worker(callable_):
            close_old_connections()
            try:
                start.wait(timeout=20)
                return ('ok', callable_())
            except Exception as error:
                return ('error', error)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(worker, first_callable),
                executor.submit(worker, second_callable),
            )
            return tuple(future.result(timeout=60) for future in futures)

    @staticmethod
    def _active_overlap_count():
        periods = list(
            RatingPeriod.objects
            .filter(is_active=True)
            .order_by('starts_on', 'ends_before', 'id')
        )
        overlap_count = 0
        for index, period in enumerate(periods):
            for following in periods[index + 1:]:
                if following.starts_on >= period.ends_before:
                    break
                if (
                    period.starts_on < following.ends_before
                    and period.ends_before > following.starts_on
                ):
                    overlap_count += 1
        return overlap_count

    def test_two_concurrent_bootstraps_create_one_catalog(self):
        results = self._run_pair(
            lambda: ensure_rating_periods(as_of=self.as_of),
            lambda: ensure_rating_periods(as_of=self.as_of),
        )

        self.assertEqual([status for status, _value in results], ['ok', 'ok'])
        self.assertEqual(
            sorted(value.created_count for _status, value in results),
            [0, 13],
        )
        self.assertEqual(RatingPeriod.objects.count(), 13)
        self.assertEqual(
            RatingPeriod.objects.values('nominal_starts_on').distinct().count(),
            13,
        )
        self.assertEqual(self._active_overlap_count(), 0)

    def test_two_concurrent_monthly_runs_add_one_far_future_slot(self):
        ensure_rating_periods(as_of=self.as_of)

        results = self._run_pair(
            lambda: ensure_rating_periods(as_of=date(2026, 8, 14)),
            lambda: ensure_rating_periods(as_of=date(2026, 8, 14)),
        )

        self.assertEqual([status for status, _value in results], ['ok', 'ok'])
        self.assertEqual(
            sorted(value.created_count for _status, value in results),
            [0, 1],
        )
        self.assertEqual(RatingPeriod.objects.count(), 14)
        self.assertEqual(
            RatingPeriod.objects.filter(
                nominal_starts_on=date(2027, 8, 14),
            ).count(),
            1,
        )
        self.assertEqual(self._active_overlap_count(), 0)

    def test_concurrent_runs_with_different_dates_keep_unique_slots(self):
        results = self._run_pair(
            lambda: ensure_rating_periods(as_of=date(2026, 7, 13)),
            lambda: ensure_rating_periods(as_of=date(2026, 7, 14)),
        )

        self.assertEqual([status for status, _value in results], ['ok', 'ok'])
        self.assertEqual(
            RatingPeriod.objects.count(),
            RatingPeriod.objects
            .values('nominal_starts_on')
            .distinct()
            .count(),
        )
        self.assertEqual(self._active_overlap_count(), 0)

    def test_generator_and_manual_period_never_leave_active_overlap(self):
        def create_manual_period():
            return RatingPeriod.objects.create(
                name='Ручной период одновременно с генератором',
                starts_on=date(2026, 7, 14),
                ends_before=date(2026, 8, 14),
            )

        results = self._run_pair(
            lambda: ensure_rating_periods(as_of=self.as_of),
            create_manual_period,
        )

        errors = [
            value
            for status, value in results
            if status == 'error'
        ]
        self.assertTrue(
            all(isinstance(error, ValidationError) for error in errors),
            errors,
        )
        self.assertEqual(
            RatingPeriod.objects.filter(
                starts_on=date(2026, 7, 14),
                ends_before=date(2026, 8, 14),
                is_active=True,
            ).count(),
            1,
        )
        self.assertEqual(self._active_overlap_count(), 0)

    def test_manual_disable_is_not_reverted_by_concurrent_generation(self):
        ensure_rating_periods(as_of=self.as_of)
        period_id = RatingPeriod.objects.get(
            nominal_starts_on=date(2027, 7, 14),
        ).id

        def disable_period():
            period = RatingPeriod.objects.get(pk=period_id)
            period.is_active = False
            period.save(update_fields=['is_active'])
            return period.id

        results = self._run_pair(
            lambda: ensure_rating_periods(as_of=self.as_of),
            disable_period,
        )

        self.assertEqual([status for status, _value in results], ['ok', 'ok'])
        period = RatingPeriod.objects.get(pk=period_id)
        self.assertFalse(period.is_active)
        self.assertEqual(
            RatingPeriod.objects.filter(
                nominal_starts_on=date(2027, 7, 14),
            ).count(),
            1,
        )
        self.assertEqual(self._active_overlap_count(), 0)

    def test_concurrent_date_edit_and_enable_never_leave_overlap(self):
        RatingPeriod.objects.create(
            name='Действующий период',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
        )
        inactive = RatingPeriod.objects.create(
            name='Отключённый будущий период',
            starts_on=date(2026, 8, 14),
            ends_before=date(2026, 9, 14),
            is_active=False,
        )
        loaded = Barrier(2)
        edit_finished = Event()

        def edit_dates():
            period = RatingPeriod.objects.get(pk=inactive.pk)
            period.starts_on = date(2026, 7, 20)
            period.ends_before = date(2026, 8, 1)
            period.comment = 'Проверка конкурентного изменения дат.'
            loaded.wait(timeout=20)
            try:
                period.save()
            finally:
                edit_finished.set()
            return period.id

        def enable_period():
            period = RatingPeriod.objects.get(pk=inactive.pk)
            loaded.wait(timeout=20)
            edit_finished.wait(timeout=20)
            period.is_active = True
            period.save(update_fields=['is_active'])
            return period.id

        results = self._run_pair(edit_dates, enable_period)
        errors = [
            value
            for status, value in results
            if status == 'error'
        ]

        self.assertEqual(len(errors), 1, results)
        self.assertIsInstance(errors[0], ValidationError)
        self.assertEqual(self._active_overlap_count(), 0)
