from datetime import date

from django.test import SimpleTestCase

from assignments.models import WorkShiftType
from reports.driver_watch_rating import DRIVER_RATING_FORMULA_VERSION
from tools.generate_driver_rating_30d_qa import (
    DAY_BRIGADE,
    EXPECTED_DRIVER_COUNT,
    NIGHT_BRIGADE,
    PERIOD_DAY_COUNT,
    QA_DB_ENGINE,
    QA_DB_HOST,
    QA_DB_NAME,
    QA_DB_PORT,
    QA_DB_USER,
    Rating30dConfig,
    Rating30dQAError,
    Rating30dRunner,
    period_calendar,
    validate_configured_database,
    validate_daily_formula_result,
)


class Rating30dQAGeneratorContractTests(SimpleTestCase):
    def test_database_guard_accepts_only_exact_local_postgresql(self):
        valid = {
            "ENGINE": QA_DB_ENGINE,
            "NAME": QA_DB_NAME,
            "USER": QA_DB_USER,
            "HOST": QA_DB_HOST,
            "PORT": QA_DB_PORT,
        }
        validate_configured_database(valid)

        invalid_variants = (
            {**valid, "ENGINE": "django.db.backends.sqlite3"},
            {**valid, "NAME": "accounting_mvp"},
            {**valid, "USER": "postgres"},
            {**valid, "HOST": "localhost"},
            {**valid, "PORT": "5432"},
        )
        for invalid in invalid_variants:
            with self.subTest(invalid=invalid):
                with self.assertRaises(Rating30dQAError):
                    validate_configured_database(invalid)

    def test_calendar_is_exact_30_day_14_to_14_window(self):
        config = Rating30dConfig(start_date=date(2026, 6, 14))

        calendar = period_calendar(config)

        self.assertEqual(config.day_count, PERIOD_DAY_COUNT)
        self.assertEqual(calendar["starts_on"], date(2026, 6, 14))
        self.assertEqual(calendar["watch_ends_on"], date(2026, 7, 13))
        self.assertEqual(
            calendar["rating_ends_before"],
            date(2026, 7, 14),
        )

    def test_calendar_rejects_any_non_30_day_configuration(self):
        config = Rating30dConfig(day_count=29)

        with self.assertRaises(Rating30dQAError):
            period_calendar(config)

    def test_runner_keeps_fixed_day_and_night_brigades_for_all_60_shifts(self):
        actual = tuple(
            Rating30dRunner.brigade_for_shift(shift_index)
            for shift_index in range(PERIOD_DAY_COUNT * 2)
        )

        self.assertEqual(actual[::2], (DAY_BRIGADE,) * PERIOD_DAY_COUNT)
        self.assertEqual(actual[1::2], (NIGHT_BRIGADE,) * PERIOD_DAY_COUNT)

    @staticmethod
    def _formula_result(day_number, shift_type, employee_ids):
        entries = [
            {
                "employee_id": employee_id,
                "full_name": f"ТЕСТ_ВОДИТЕЛЬ_{index:02d}",
                "shift_count": day_number,
            }
            for index, employee_id in enumerate(employee_ids, start=1)
        ]
        return {
            "available": True,
            "official": False,
            "formula_version": DRIVER_RATING_FORMULA_VERSION,
            "shift_type": shift_type,
            "status": "Рабочий рейтинг рассчитан.",
            "summary": {
                "rated_shift_count": EXPECTED_DRIVER_COUNT * day_number,
                "withheld_shift_count": 0,
                "withheld_reasons": {},
            },
            "linkage_audit": {"linkage_ready": True},
            "entries": entries,
        }

    def test_formula_guard_accepts_complete_fixed_cohort(self):
        employee_ids = tuple(range(1, EXPECTED_DRIVER_COUNT + 1))
        result = self._formula_result(
            12,
            WorkShiftType.SHIFT_1,
            employee_ids,
        )

        validate_daily_formula_result(
            result,
            day_number=12,
            shift_type=WorkShiftType.SHIFT_1,
            expected_employee_ids=employee_ids,
        )

    def test_formula_guard_rejects_withheld_shift(self):
        employee_ids = tuple(range(1, EXPECTED_DRIVER_COUNT + 1))
        result = self._formula_result(
            7,
            WorkShiftType.SHIFT_2,
            employee_ids,
        )
        result["summary"]["rated_shift_count"] -= 1
        result["summary"]["withheld_shift_count"] = 1
        result["summary"]["withheld_reasons"] = {
            "passport_coverage_incomplete": 1,
        }

        with self.assertRaises(Rating30dQAError):
            validate_daily_formula_result(
                result,
                day_number=7,
                shift_type=WorkShiftType.SHIFT_2,
                expected_employee_ids=employee_ids,
            )

    def test_formula_guard_rejects_changed_employee_cohort(self):
        employee_ids = tuple(range(1, EXPECTED_DRIVER_COUNT + 1))
        result = self._formula_result(
            30,
            WorkShiftType.SHIFT_1,
            employee_ids,
        )
        result["entries"][-1]["employee_id"] = 9999

        with self.assertRaises(Rating30dQAError):
            validate_daily_formula_result(
                result,
                day_number=30,
                shift_type=WorkShiftType.SHIFT_1,
                expected_employee_ids=employee_ids,
            )
