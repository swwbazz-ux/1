from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from tools.generate_driver_rating_passport_benchmark import (
    DAY_DRIVER_COUNT,
    NIGHT_DRIVER_COUNT,
    RATING_DAY_COUNT,
    RATING_SHIFT_COUNT_PER_GROUP,
    RATING_TRIP_COUNT_PER_GROUP,
    TARGET_DATABASE_IDENTITY,
    TARGET_DB_ENGINE,
    TARGET_DB_HOST,
    TARGET_DB_NAME,
    TARGET_DB_PORT,
    TARGET_DB_USER,
    TOTAL_EMBEDDED_TRIP_COUNT,
    TOTAL_SHIFT_COUNT,
    TRIPS_PER_SHIFT,
    WATCH_PERIOD_COUNT,
    BenchmarkConfig,
    PassportBenchmarkError,
    _parse_refresh_command_cycle,
    _validate_refresh_command_cycles,
    assert_empty_counts,
    assert_final_counts,
    build_embedded_trips,
    build_passport_payload,
    configured_database_identity,
    employee_scope_fingerprint,
    ensure_new_artifact_directory,
    expected_final_counts,
    fingerprint,
    iter_shift_specs,
    validate_actual_database_identity,
    validate_config,
    validate_configured_database,
    validate_formula_result,
    watch_period_ranges,
)


class DriverRatingPassportBenchmarkPureTests(unittest.TestCase):
    def setUp(self):
        self.config = BenchmarkConfig()

    @staticmethod
    def configured_database(**overrides):
        database = {
            "ENGINE": TARGET_DB_ENGINE,
            "NAME": TARGET_DB_NAME,
            "USER": TARGET_DB_USER,
            "HOST": TARGET_DB_HOST,
            "PORT": TARGET_DB_PORT,
            "PASSWORD": "",
        }
        database.update(overrides)
        return database

    def test_fixed_config_is_exactly_10070_passports(self):
        validate_config(self.config)
        self.assertEqual(self.config.total_shift_count, TOTAL_SHIFT_COUNT)
        self.assertEqual(TOTAL_SHIFT_COUNT, 10070)
        self.assertEqual(
            self.config.total_embedded_trip_count,
            TOTAL_EMBEDDED_TRIP_COUNT,
        )
        self.assertEqual(TOTAL_EMBEDDED_TRIP_COUNT, 201400)
        self.assertEqual(
            self.config.rating_day_count,
            RATING_DAY_COUNT,
        )

    def test_config_rejects_any_scope_drift(self):
        mutations = {
            "start_date": self.config.start_date.replace(day=9),
            "production_day_count": 94,
            "truck_count": 52,
            "day_driver_count": 52,
            "night_driver_count": 52,
            "trips_per_shift": 19,
            "watch_period_count": 3,
            "rating_day_count": 29,
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                with self.assertRaises(PassportBenchmarkError):
                    validate_config(replace(self.config, **{field: value}))

    def test_configured_database_identity_must_match_every_field(self):
        expected = self.configured_database()
        self.assertEqual(
            configured_database_identity(expected),
            TARGET_DATABASE_IDENTITY,
        )
        validate_configured_database(expected)

        wrong_values = {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": "accounting_mvp",
            "USER": "accounting_mvp",
            "HOST": "localhost",
            "PORT": "5432",
        }
        for field, value in wrong_values.items():
            with self.subTest(field=field):
                with self.assertRaises(PassportBenchmarkError):
                    validate_configured_database(
                        self.configured_database(**{field: value}),
                    )
        with self.assertRaises(PassportBenchmarkError):
            validate_configured_database(
                self.configured_database(PASSWORD="not-empty"),
            )

    def test_actual_database_identity_is_equally_strict(self):
        exact = {
            "name": TARGET_DB_NAME,
            "user": TARGET_DB_USER,
            "host": TARGET_DB_HOST,
            "port": TARGET_DB_PORT,
        }
        validate_actual_database_identity(exact)
        for field, value in {
            "name": "postgres",
            "user": "postgres",
            "host": "127.0.0.1/32",
            "port": "5432",
        }.items():
            with self.subTest(field=field):
                changed = dict(exact)
                changed[field] = value
                with self.assertRaises(PassportBenchmarkError):
                    validate_actual_database_identity(changed)

    def test_four_watch_periods_cover_all_95_days(self):
        ranges = watch_period_ranges(self.config)
        self.assertEqual(len(ranges), WATCH_PERIOD_COUNT)
        self.assertEqual(
            [
                (item[0].isoformat(), item[1].isoformat())
                for item in ranges
            ],
            [
                ("2026-01-14", "2026-02-13"),
                ("2026-02-14", "2026-03-13"),
                ("2026-03-14", "2026-04-13"),
                ("2026-04-14", "2026-05-13"),
            ],
        )
        self.assertEqual(self.config.end_date.isoformat(), "2026-05-13")
        self.assertEqual(
            self.config.rating_starts_on.isoformat(),
            "2026-04-14",
        )
        self.assertEqual(
            self.config.rating_ends_before.isoformat(),
            "2026-05-14",
        )

    def test_shift_specs_have_exact_groups_dates_and_watch_distribution(self):
        specs = list(iter_shift_specs(self.config))
        self.assertEqual(len(specs), TOTAL_SHIFT_COUNT)
        self.assertEqual(
            sum(item.shift_type == "day" for item in specs),
            DAY_DRIVER_COUNT * self.config.production_day_count,
        )
        self.assertEqual(
            sum(item.shift_type == "night" for item in specs),
            NIGHT_DRIVER_COUNT * self.config.production_day_count,
        )
        self.assertEqual(
            sum(
                item.production_date >= self.config.rating_starts_on
                and item.shift_type == "day"
                for item in specs
            ),
            RATING_SHIFT_COUNT_PER_GROUP,
        )
        self.assertEqual(
            [
                sum(item.watch_period_index == index for item in specs)
                for index in range(WATCH_PERIOD_COUNT)
            ],
            [636, 2968, 3286, 3180],
        )

    def test_embedded_trips_are_exactly_twenty_and_deterministic(self):
        opened_at = datetime(2026, 8, 18, 8, tzinfo=timezone.utc)
        kwargs = {
            "shift_id": 71,
            "employee_id": 81,
            "truck_id": 91,
            "truck_model_id": 101,
            "rock_type_id": 111,
            "dump_point_id": 121,
            "opened_at": opened_at,
            "driver_index": 7,
        }
        first = build_embedded_trips(**kwargs)
        second = build_embedded_trips(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(len(first), TRIPS_PER_SHIFT)
        self.assertEqual(
            len({item["id"] for item in first}),
            TRIPS_PER_SHIFT,
        )
        self.assertTrue(
            all(item["unloading_shift_id"] == 71 for item in first),
        )
        self.assertTrue(
            all(item["driver_id"] == 81 for item in first),
        )
        self.assertTrue(
            all(item["transport_distance_km"] is None for item in first),
        )
        self.assertTrue(
            all(
                item["completed_at"] > item["created_at"]
                for item in first
            ),
        )

    def test_passport_payload_is_fingerprint_stable_and_has_no_live_events(self):
        opened_at = datetime(2026, 8, 18, 8, tzinfo=timezone.utc)
        kwargs = {
            "shift_id": 71,
            "employee_id": 81,
            "equipment_id": 91,
            "truck_model_id": 101,
            "rock_type_id": 111,
            "dump_point_id": 121,
            "watch_period_id": 131,
            "watch_composition_id": 141,
            "shift_type": "day",
            "opened_at": opened_at,
            "closed_at": opened_at.replace(hour=20),
            "driver_index": 7,
        }
        first = build_passport_payload(**kwargs)
        second = build_passport_payload(**kwargs)
        self.assertEqual(fingerprint(first), fingerprint(second))
        manifest = first["source_manifest"]
        self.assertEqual(len(manifest["trips"]), TRIPS_PER_SHIFT)
        self.assertEqual(manifest["downtimes"], [])
        self.assertEqual(manifest["assignments"], [])
        self.assertEqual(manifest["reading_corrections"], [])
        self.assertEqual(
            first["passport"]["production"]["m3_km"],
            {"known_value": None, "is_complete": False},
        )

    def test_empty_and_final_count_guards_fail_closed(self):
        assert_empty_counts({"employees": 0, "trips": 0})
        with self.assertRaises(PassportBenchmarkError):
            assert_empty_counts({"employees": 1, "trips": 0})

        expected = expected_final_counts(self.config)
        assert_final_counts(expected, expected)
        changed = dict(expected)
        changed["trips"] = 1
        with self.assertRaises(PassportBenchmarkError):
            assert_final_counts(changed, expected)
        changed = dict(expected)
        changed["unexpected_table"] = 1
        with self.assertRaises(PassportBenchmarkError):
            assert_final_counts(changed, expected)

    def test_formula_result_contract_requires_complete_last30_group(self):
        employee_ids = tuple(range(1, DAY_DRIVER_COUNT + 1))
        result = {
            "available": True,
            "official": False,
            "official_rating_eligible": False,
            "summary": {
                "employee_count": DAY_DRIVER_COUNT,
                "rated_shift_count": RATING_SHIFT_COUNT_PER_GROUP,
                "withheld_shift_count": 0,
                "withheld_reasons": {},
                "trip_count": RATING_TRIP_COUNT_PER_GROUP,
            },
            "linkage_audit": {},
            "entries": [
                {"employee_id": employee_id}
                for employee_id in employee_ids
            ],
            "distance_metrics": {"weight": "0"},
        }
        validate_formula_result(
            result,
            expected_employee_ids=employee_ids,
        )
        broken = {
            **result,
            "summary": {
                **result["summary"],
                "withheld_shift_count": 1,
            },
        }
        with self.assertRaises(PassportBenchmarkError):
            validate_formula_result(
                broken,
                expected_employee_ids=employee_ids,
            )
        duplicate = {
            **result,
            "entries": [
                *result["entries"],
                dict(result["entries"][0]),
            ],
        }
        with self.assertRaisesRegex(
            PassportBenchmarkError,
            "duplicate_employee_ids",
        ):
            validate_formula_result(
                duplicate,
                expected_employee_ids=employee_ids,
            )

    def test_refresh_command_cycle_parser_is_fail_closed(self):
        stdout = (
            "rating-test-watch/day: verified, revision=1\n"
            "rating-test-watch/night: published, revision=1\n"
            "Групп: 2; опубликовано: 1; без изменения данных: 1; "
            "уже выполнялись: 0; ошибок: 0.\n"
        )
        parsed = _parse_refresh_command_cycle(
            ordinal=1,
            stdout=stdout,
            stderr="",
            metrics={"seconds": 12.5, "query_count": 17},
        )
        self.assertEqual(parsed["ordinal"], 1)
        self.assertEqual(
            {item["shift_type"] for item in parsed["groups"]},
            {"day", "night"},
        )
        with self.assertRaises(PassportBenchmarkError):
            _parse_refresh_command_cycle(
                ordinal=1,
                stdout=stdout.replace(
                    "без изменения данных: 1",
                    "без изменения данных: 0",
                ),
                stderr="",
                metrics={"seconds": 12.5, "query_count": 17},
            )
        with self.assertRaises(PassportBenchmarkError):
            _parse_refresh_command_cycle(
                ordinal=1,
                stdout=stdout.replace(
                    "опубликовано: 1; без изменения данных: 1",
                    "опубликовано: 2; без изменения данных: 0",
                ),
                stderr="",
                metrics={"seconds": 12.5, "query_count": 17},
            )
        with self.assertRaises(PassportBenchmarkError):
            _parse_refresh_command_cycle(
                ordinal=1,
                stdout=stdout,
                stderr="unexpected error",
                metrics={"seconds": 12.5, "query_count": 17},
            )

    def test_refresh_command_cycle_parser_requires_exact_lifecycle(self):
        cycle_one = (
            "rating-test-watch/day: verified, revision=1\n"
            "rating-test-watch/night: published, revision=1\n"
            "Групп: 2; опубликовано: 1; без изменения данных: 1; "
            "уже выполнялись: 0; ошибок: 0.\n"
        )
        cycle_two = (
            "rating-test-watch/day: verified, revision=1\n"
            "rating-test-watch/night: verified, revision=1\n"
            "Групп: 2; опубликовано: 0; без изменения данных: 2; "
            "уже выполнялись: 0; ошибок: 0.\n"
        )
        metrics = {"seconds": 12.5, "query_count": 17}

        _parse_refresh_command_cycle(
            ordinal=1,
            stdout=cycle_one,
            stderr="",
            metrics=metrics,
        )
        _parse_refresh_command_cycle(
            ordinal=2,
            stdout=cycle_two,
            stderr="",
            metrics=metrics,
        )

        swapped_statuses = (
            "rating-test-watch/day: published, revision=1\n"
            "rating-test-watch/night: verified, revision=1\n"
            "Групп: 2; опубликовано: 1; без изменения данных: 1; "
            "уже выполнялись: 0; ошибок: 0.\n"
        )
        with self.assertRaises(PassportBenchmarkError):
            _parse_refresh_command_cycle(
                ordinal=1,
                stdout=swapped_statuses,
                stderr="",
                metrics=metrics,
            )

        with self.assertRaises(PassportBenchmarkError):
            _parse_refresh_command_cycle(
                ordinal=1,
                stdout=cycle_one.replace(
                    "revision=1",
                    "revision=0",
                ),
                stderr="",
                metrics=metrics,
            )

        with self.assertRaises(PassportBenchmarkError):
            _parse_refresh_command_cycle(
                ordinal=2,
                stdout=cycle_two.replace(
                    "night: verified",
                    "night: published",
                ).replace(
                    "опубликовано: 0; без изменения данных: 2",
                    "опубликовано: 1; без изменения данных: 1",
                ),
                stderr="",
                metrics=metrics,
            )

    def test_refresh_command_cycles_require_exact_lifecycle(self):
        valid_cycles = [
            {
                "ordinal": 1,
                "groups": [
                    {
                        "shift_type": "day",
                        "status": "verified",
                        "revision": 1,
                    },
                    {
                        "shift_type": "night",
                        "status": "published",
                        "revision": 1,
                    },
                ],
            },
            {
                "ordinal": 2,
                "groups": [
                    {
                        "shift_type": "day",
                        "status": "verified",
                        "revision": 1,
                    },
                    {
                        "shift_type": "night",
                        "status": "verified",
                        "revision": 1,
                    },
                ],
            },
        ]
        _validate_refresh_command_cycles(valid_cycles)

        swapped_first_cycle_statuses = [
            {
                **valid_cycles[0],
                "groups": [
                    {
                        "shift_type": "day",
                        "status": "published",
                        "revision": 1,
                    },
                    {
                        "shift_type": "night",
                        "status": "verified",
                        "revision": 1,
                    },
                ],
            },
            valid_cycles[1],
        ]
        with self.assertRaises(PassportBenchmarkError):
            _validate_refresh_command_cycles(
                swapped_first_cycle_statuses
            )

        unchanged_wrong_revision = [
            {
                **cycle,
                "groups": [
                    {
                        **group,
                        "revision": 0,
                    }
                    for group in cycle["groups"]
                ],
            }
            for cycle in valid_cycles
        ]
        with self.assertRaises(PassportBenchmarkError):
            _validate_refresh_command_cycles(
                unchanged_wrong_revision
            )

    def test_scope_fingerprint_is_order_and_duplicate_invariant(self):
        self.assertEqual(
            employee_scope_fingerprint([3, 1, 2, 2]),
            employee_scope_fingerprint([1, 2, 3]),
        )
        self.assertNotEqual(
            employee_scope_fingerprint([1, 2, 3]),
            employee_scope_fingerprint([1, 2, 4]),
        )

    def test_artifact_directory_never_overwrites_existing_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "new"
            self.assertEqual(
                ensure_new_artifact_directory(target),
                target.resolve(),
            )
            (target / "evidence.json").write_text(
                "{}",
                encoding="utf-8",
            )
            with self.assertRaises(PassportBenchmarkError):
                ensure_new_artifact_directory(target)


if __name__ == "__main__":
    unittest.main()
