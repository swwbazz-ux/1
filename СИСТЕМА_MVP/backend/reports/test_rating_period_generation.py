from datetime import date, datetime
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from reports.models import DriverShiftPassportSnapshot, RatingPeriod
from reports.rating_period_calendar import (
    add_calendar_months,
    nominal_rating_period_end,
    nominal_rating_period_start,
)
from reports.rating_period_generation import (
    RatingPeriodCatalogConflict,
    ensure_rating_periods,
    inspect_rating_period_calendar,
)
from shifts.models import EmployeeShift, WatchPeriod
from users.models import AdminActionLog


class RatingPeriodCalendarTests(TestCase):
    def test_nominal_start_switches_on_the_fourteenth(self):
        self.assertEqual(
            nominal_rating_period_start(date(2026, 7, 13)),
            date(2026, 6, 14),
        )
        self.assertEqual(
            nominal_rating_period_start(date(2026, 7, 14)),
            date(2026, 7, 14),
        )

    def test_calendar_months_handle_month_lengths_leap_year_and_year_change(self):
        cases = (
            (date(2026, 1, 14), date(2026, 2, 14), 31),
            (date(2026, 4, 14), date(2026, 5, 14), 30),
            (date(2025, 2, 14), date(2025, 3, 14), 28),
            (date(2024, 2, 14), date(2024, 3, 14), 29),
            (date(2026, 12, 14), date(2027, 1, 14), 31),
        )
        for starts_on, expected_end, expected_days in cases:
            with self.subTest(starts_on=starts_on):
                ends_before = nominal_rating_period_end(starts_on)
                self.assertEqual(ends_before, expected_end)
                self.assertEqual(
                    (ends_before - starts_on).days,
                    expected_days,
                )

    def test_add_calendar_months_clamps_general_month_end_safely(self):
        self.assertEqual(
            add_calendar_months(date(2024, 1, 31), 1),
            date(2024, 2, 29),
        )


class RatingPeriodGenerationTests(TestCase):
    as_of = date(2026, 7, 29)

    def test_empty_catalog_bootstraps_current_and_twelve_future_periods(self):
        result = ensure_rating_periods(as_of=self.as_of)
        periods = list(RatingPeriod.objects.order_by('nominal_starts_on'))

        self.assertTrue(result.bootstrap)
        self.assertEqual(result.created_count, 13)
        self.assertEqual(len(periods), 13)
        self.assertEqual(periods[0].starts_on, date(2026, 7, 14))
        self.assertEqual(periods[0].ends_before, date(2026, 8, 14))
        self.assertEqual(periods[-1].starts_on, date(2027, 7, 14))
        self.assertEqual(periods[-1].ends_before, date(2027, 8, 14))
        self.assertTrue(all(period.is_active for period in periods))
        self.assertTrue(all(period.nominal_starts_on for period in periods))
        self.assertEqual(
            periods[0].name,
            'Рейтинг 14.07.2026–13.08.2026',
        )
        self.assertEqual(
            AdminActionLog.objects.filter(
                action_code='rating_period_auto_created',
            ).count(),
            13,
        )
        self.assertTrue(result.inspection.is_ready)

    def test_run_on_fourteenth_starts_with_that_day(self):
        result = ensure_rating_periods(
            as_of=date(2026, 8, 14),
            months_ahead=0,
        )
        period = RatingPeriod.objects.get()

        self.assertEqual(result.created_count, 1)
        self.assertEqual(period.starts_on, date(2026, 8, 14))
        self.assertEqual(period.ends_before, date(2026, 9, 14))

    def test_default_date_before_seven_uses_previous_production_day(self):
        local_now = datetime(
            2026,
            7,
            14,
            6,
            59,
            59,
            tzinfo=ZoneInfo('Asia/Vladivostok'),
        )

        with patch(
            'core.production_time.timezone.now',
            return_value=local_now,
        ):
            ensure_rating_periods(months_ahead=0)

        self.assertEqual(
            RatingPeriod.objects.get().nominal_starts_on,
            date(2026, 6, 14),
        )

    def test_default_date_at_seven_uses_new_production_day(self):
        local_now = datetime(
            2026,
            7,
            14,
            7,
            0,
            tzinfo=ZoneInfo('Asia/Vladivostok'),
        )

        with patch(
            'core.production_time.timezone.now',
            return_value=local_now,
        ):
            ensure_rating_periods(months_ahead=0)

        self.assertEqual(
            RatingPeriod.objects.get().nominal_starts_on,
            date(2026, 7, 14),
        )

    def test_repeat_is_idempotent_and_does_not_touch_existing_rows(self):
        ensure_rating_periods(as_of=self.as_of)
        before = list(
            RatingPeriod.objects
            .order_by('id')
            .values(
                'id',
                'name',
                'starts_on',
                'ends_before',
                'nominal_starts_on',
                'comment',
                'is_active',
                'updated_at',
            )
        )
        log_count = AdminActionLog.objects.count()

        result = ensure_rating_periods(as_of=self.as_of)
        after = list(
            RatingPeriod.objects
            .order_by('id')
            .values(
                'id',
                'name',
                'starts_on',
                'ends_before',
                'nominal_starts_on',
                'comment',
                'is_active',
                'updated_at',
            )
        )

        self.assertFalse(result.bootstrap)
        self.assertEqual(result.created_count, 0)
        self.assertEqual(after, before)
        self.assertEqual(AdminActionLog.objects.count(), log_count)

    @override_settings(PORTAL_WORKING_DRIVER_RATING_ENABLED=False)
    def test_generation_does_not_touch_operational_or_rating_data(self):
        before = {
            'watch_periods': WatchPeriod.objects.count(),
            'employee_shifts': EmployeeShift.objects.count(),
            'passports': DriverShiftPassportSnapshot.objects.count(),
        }

        ensure_rating_periods(as_of=self.as_of)

        self.assertEqual(
            {
                'watch_periods': WatchPeriod.objects.count(),
                'employee_shifts': EmployeeShift.objects.count(),
                'passports': DriverShiftPassportSnapshot.objects.count(),
            },
            before,
        )
        self.assertFalse(settings.PORTAL_WORKING_DRIVER_RATING_ENABLED)

    def test_next_month_adds_only_one_far_future_period(self):
        ensure_rating_periods(as_of=self.as_of)

        result = ensure_rating_periods(as_of=date(2026, 8, 14))

        self.assertEqual(result.created_count, 1)
        self.assertEqual(RatingPeriod.objects.count(), 14)
        self.assertTrue(
            RatingPeriod.objects.filter(
                nominal_starts_on=date(2027, 8, 14),
                starts_on=date(2027, 8, 14),
                ends_before=date(2027, 9, 14),
            ).exists()
        )

    def test_generator_preserves_manual_override_and_inactive_auto_period(self):
        ensure_rating_periods(as_of=self.as_of)
        current = RatingPeriod.objects.get(
            nominal_starts_on=date(2026, 7, 14),
        )
        future = RatingPeriod.objects.get(
            nominal_starts_on=date(2027, 7, 14),
        )
        current.starts_on = date(2026, 7, 15)
        current.comment = 'Исключение по дате контрольного замера'
        current.save()
        future.is_active = False
        future.save(update_fields=['is_active'])
        current_updated_at = current.updated_at
        future_updated_at = future.updated_at

        ensure_rating_periods(as_of=self.as_of)

        current.refresh_from_db()
        future.refresh_from_db()
        self.assertEqual(current.starts_on, date(2026, 7, 15))
        self.assertEqual(current.ends_before, date(2026, 8, 14))
        self.assertEqual(current.updated_at, current_updated_at)
        self.assertFalse(future.is_active)
        self.assertEqual(future.updated_at, future_updated_at)
        self.assertEqual(
            RatingPeriod.objects.filter(
                nominal_starts_on=date(2026, 7, 14),
            ).count(),
            1,
        )
        self.assertEqual(
            RatingPeriod.objects.filter(
                nominal_starts_on=date(2027, 7, 14),
            ).count(),
            1,
        )

    def test_exact_manual_period_is_respected_and_not_duplicated(self):
        RatingPeriod.objects.create(
            name='Утверждённый вручную текущий период',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
        )

        result = ensure_rating_periods(as_of=self.as_of)

        self.assertFalse(result.bootstrap)
        self.assertEqual(result.created_count, 12)
        self.assertEqual(RatingPeriod.objects.count(), 13)
        self.assertEqual(
            RatingPeriod.objects.filter(
                starts_on=date(2026, 7, 14),
                ends_before=date(2026, 8, 14),
            ).count(),
            1,
        )

    def test_inactive_exact_future_period_is_not_reactivated_or_duplicated(self):
        RatingPeriod.objects.create(
            name='Текущий период вручную',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
        )
        inactive = RatingPeriod.objects.create(
            name='Август отключён вручную',
            starts_on=date(2026, 8, 14),
            ends_before=date(2026, 9, 14),
            is_active=False,
        )

        result = ensure_rating_periods(as_of=self.as_of)

        inactive.refresh_from_db()
        self.assertFalse(inactive.is_active)
        self.assertEqual(result.created_count, 11)
        self.assertEqual(
            RatingPeriod.objects.filter(
                starts_on=date(2026, 8, 14),
                ends_before=date(2026, 9, 14),
            ).count(),
            1,
        )
        self.assertTrue(result.inspection.gap_ranges)

    def test_active_manual_exception_skips_overlapped_slots_and_reports_gaps(self):
        exception = RatingPeriod.objects.create(
            name='Исключение контрольного замера',
            starts_on=date(2026, 7, 15),
            ends_before=date(2026, 9, 15),
            comment='Утверждённое исключение',
        )
        original = (
            exception.name,
            exception.starts_on,
            exception.ends_before,
            exception.comment,
            exception.is_active,
            exception.updated_at,
        )

        result = ensure_rating_periods(as_of=self.as_of)

        exception.refresh_from_db()
        self.assertEqual(
            (
                exception.name,
                exception.starts_on,
                exception.ends_before,
                exception.comment,
                exception.is_active,
                exception.updated_at,
            ),
            original,
        )
        self.assertEqual(
            result.skipped_overlap_nominal_starts,
            (date(2026, 8, 14), date(2026, 9, 14)),
        )
        self.assertEqual(result.created_count, 10)
        self.assertEqual(result.inspection.override_count, 1)
        self.assertTrue(result.inspection.gap_ranges)

    def test_inactive_nonexact_history_does_not_block_active_future_slot(self):
        RatingPeriod.objects.create(
            name='Текущий период вручную',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
        )
        RatingPeriod.objects.create(
            name='Старый отключённый черновик',
            starts_on=date(2026, 8, 20),
            ends_before=date(2026, 9, 1),
            comment='Техническая проверка ручного исключения.',
            is_active=False,
        )

        ensure_rating_periods(as_of=self.as_of)

        self.assertTrue(
            RatingPeriod.objects.filter(
                nominal_starts_on=date(2026, 8, 14),
                is_active=True,
            ).exists()
        )

    def test_nonempty_catalog_does_not_backfill_missing_current_slot(self):
        RatingPeriod.objects.create(
            name='Только будущая ручная запись',
            starts_on=date(2026, 9, 14),
            ends_before=date(2026, 10, 14),
        )

        result = ensure_rating_periods(as_of=self.as_of)

        self.assertFalse(
            RatingPeriod.objects.filter(
                starts_on=date(2026, 7, 14),
                ends_before=date(2026, 8, 14),
            ).exists()
        )
        self.assertTrue(result.inspection.gap_ranges)
        self.assertEqual(
            result.inspection.prepared_through,
            date(2026, 7, 14),
        )

    def test_corrupt_active_overlap_stops_generation_without_new_rows(self):
        first = RatingPeriod.objects.create(
            name='Первый',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
        )
        second = RatingPeriod.objects.create(
            name='Второй',
            starts_on=date(2026, 8, 14),
            ends_before=date(2026, 9, 14),
        )
        RatingPeriod._base_manager.filter(pk=second.pk).update(
            starts_on=date(2026, 8, 1),
        )

        with self.assertRaises(RatingPeriodCatalogConflict):
            ensure_rating_periods(as_of=self.as_of)

        self.assertEqual(
            set(RatingPeriod.objects.values_list('id', flat=True)),
            {first.id, second.id},
        )
        self.assertEqual(
            AdminActionLog.objects.filter(
                action_code='rating_period_auto_created',
            ).count(),
            0,
        )

    def test_unexpected_mid_bootstrap_error_rolls_back_all_periods_and_logs(self):
        original_save = RatingPeriod.save
        calls = {'count': 0}

        def failing_save(instance, *args, **kwargs):
            calls['count'] += 1
            if calls['count'] == 5:
                raise RuntimeError('Искусственная ошибка')
            return original_save(instance, *args, **kwargs)

        with patch.object(RatingPeriod, 'save', new=failing_save):
            with self.assertRaisesRegex(RuntimeError, 'Искусственная ошибка'):
                ensure_rating_periods(as_of=self.as_of)

        self.assertEqual(RatingPeriod.objects.count(), 0)
        self.assertEqual(
            AdminActionLog.objects.filter(
                action_code='rating_period_auto_created',
            ).count(),
            0,
        )


class RatingPeriodAutomaticIdentityTests(TestCase):
    def test_automatic_nominal_start_is_immutable(self):
        period = RatingPeriod.objects.create(
            name='Автоматический период',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
            nominal_starts_on=date(2026, 7, 14),
        )

        period.nominal_starts_on = date(2026, 8, 14)
        with self.assertRaises(ValidationError) as error:
            period.save()

        self.assertIn(
            'nominal_starts_on',
            error.exception.message_dict,
        )

    def test_automatic_date_override_requires_reason_and_is_preserved(self):
        period = RatingPeriod.objects.create(
            name='Автоматический период',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
            nominal_starts_on=date(2026, 7, 14),
        )
        period.ends_before = date(2026, 8, 15)

        with self.assertRaises(ValidationError) as error:
            period.save()

        self.assertIn('comment', error.exception.message_dict)

        period.comment = 'Изменена дата контрольного замера'
        period.save()
        period.refresh_from_db()
        self.assertTrue(period.has_manual_override)
        self.assertEqual(
            period.manual_override_label(),
            'Даты изменены вручную',
        )


class RatingPeriodGenerationCommandTests(TestCase):
    def test_command_is_idempotent_and_reports_success(self):
        first_stdout = StringIO()
        second_stdout = StringIO()

        call_command(
            'ensure_rating_periods',
            as_of='2026-07-29',
            stdout=first_stdout,
        )
        call_command(
            'ensure_rating_periods',
            as_of='2026-07-29',
            stdout=second_stdout,
        )

        self.assertEqual(RatingPeriod.objects.count(), 13)
        self.assertIn('Создано: 13', first_stdout.getvalue())
        self.assertIn('Создано: 0', second_stdout.getvalue())

    def test_command_rejects_invalid_arguments(self):
        with self.assertRaises(CommandError):
            call_command(
                'ensure_rating_periods',
                as_of='29.07.2026',
            )
        with self.assertRaises(CommandError):
            call_command(
                'ensure_rating_periods',
                as_of='2026-07-29',
                months_ahead=61,
            )

    def test_strict_command_reports_manual_gap(self):
        existing = RatingPeriod.objects.create(
            name='Только будущая запись',
            starts_on=date(2026, 9, 14),
            ends_before=date(2026, 10, 14),
        )
        stdout = StringIO()

        with self.assertRaisesRegex(CommandError, 'есть разрывы'):
            call_command(
                'ensure_rating_periods',
                as_of='2026-07-29',
                strict=True,
                stdout=stdout,
            )

        self.assertEqual(
            list(RatingPeriod.objects.values_list('id', flat=True)),
            [existing.id],
        )
        self.assertEqual(
            AdminActionLog.objects.filter(
                action_code='rating_period_auto_created',
            ).count(),
            0,
        )
        self.assertNotIn(
            'Календарь рейтинга проверен',
            stdout.getvalue(),
        )
        inspection = inspect_rating_period_calendar(as_of=date(2026, 7, 29))
        self.assertTrue(inspection.gap_ranges)
        self.assertEqual(
            inspection.prepared_through,
            date(2026, 7, 14),
        )
