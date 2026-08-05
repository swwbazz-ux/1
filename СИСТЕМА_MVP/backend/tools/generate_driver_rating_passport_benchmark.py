#!/usr/bin/env python
"""Изолированный PostgreSQL QA benchmark рейтинга на 10 070 паспортах.

Сценарий намеренно не создаёт настоящие ``Trip``, ``DowntimeEvent`` или
назначения. Вместо этого он формирует 53 самосвала, 53 дневных и 53 ночных
водителя, 95 производственных дней и по одному неизменяемому
``DriverShiftPassportSnapshot`` на каждую закрытую смену. В каждом паспорте
находится ровно 20 синтетических рейсов.

Сценарий разрешён только для новой пустой локальной PostgreSQL-БД с точной
идентичностью, зашитой ниже. Он не создаёт БД, не выполняет миграции, не
очищает данные и не умеет обходить защитную проверку через параметры CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import statistics
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


BACKEND_DIR = Path(__file__).resolve().parents[1]

TARGET_DB_ENGINE = "django.db.backends.postgresql"
TARGET_DB_NAME = "copper_rating_passport_bench_qa_20260730"
TARGET_DB_USER = "copper_rating_bench_qa_runner"
TARGET_DB_HOST = "127.0.0.1"
TARGET_DB_PORT = "55435"
TARGET_DATABASE_IDENTITY = (
    TARGET_DB_ENGINE,
    TARGET_DB_NAME,
    TARGET_DB_USER,
    TARGET_DB_HOST,
    TARGET_DB_PORT,
)

BENCHMARK_SCHEMA = "copper.driver-rating-passport-benchmark"
BENCHMARK_SCHEMA_VERSION = 2
BENCHMARK_CALCULATOR_VERSION = "driver-rating-passport-benchmark-v1"
BENCHMARK_RUN_ID = "QA-RATING-PASSPORT-10070-20260730"
BENCHMARK_MARKER = "ТЕСТ_РЕЙТИНГ_ПАСПОРТА_10070_20260730"
BENCHMARK_ADVISORY_LOCK_ID = 0x4352505242313037

TRUCK_COUNT = 53
DAY_DRIVER_COUNT = 53
NIGHT_DRIVER_COUNT = 53
PRODUCTION_DAY_COUNT = 95
TRIPS_PER_SHIFT = 20
WATCH_PERIOD_COUNT = 4
RATING_DAY_COUNT = 30
TOTAL_DRIVER_COUNT = DAY_DRIVER_COUNT + NIGHT_DRIVER_COUNT
TOTAL_SHIFT_COUNT = TOTAL_DRIVER_COUNT * PRODUCTION_DAY_COUNT
TOTAL_EMBEDDED_TRIP_COUNT = TOTAL_SHIFT_COUNT * TRIPS_PER_SHIFT
RATING_SHIFT_COUNT_PER_GROUP = TRUCK_COUNT * RATING_DAY_COUNT
RATING_TRIP_COUNT_PER_GROUP = (
    RATING_SHIFT_COUNT_PER_GROUP * TRIPS_PER_SHIFT
)

DEFAULT_START_DATE = date(2026, 2, 8)
FIRST_WATCH_START_DATE = date(2026, 1, 14)
DEFAULT_ARTIFACT_DIR = Path(
    r"C:\Users\swwba\AppData\Local\Temp"
    r"\copper-rating-passport-benchmark-20260730"
)


class PassportBenchmarkError(RuntimeError):
    """Проверяемая fail-closed ошибка изолированного QA-сценария."""


@dataclass(frozen=True)
class BenchmarkConfig:
    start_date: date = DEFAULT_START_DATE
    production_day_count: int = PRODUCTION_DAY_COUNT
    truck_count: int = TRUCK_COUNT
    day_driver_count: int = DAY_DRIVER_COUNT
    night_driver_count: int = NIGHT_DRIVER_COUNT
    trips_per_shift: int = TRIPS_PER_SHIFT
    watch_period_count: int = WATCH_PERIOD_COUNT
    rating_day_count: int = RATING_DAY_COUNT
    batch_size: int = 250
    materialized_read_repetitions: int = 25

    @property
    def end_date(self) -> date:
        return self.start_date + timedelta(
            days=self.production_day_count - 1,
        )

    @property
    def rating_starts_on(self) -> date:
        return self.end_date - timedelta(days=self.rating_day_count - 1)

    @property
    def rating_ends_before(self) -> date:
        return self.end_date + timedelta(days=1)

    @property
    def driver_count(self) -> int:
        return self.day_driver_count + self.night_driver_count

    @property
    def total_shift_count(self) -> int:
        return self.driver_count * self.production_day_count

    @property
    def total_embedded_trip_count(self) -> int:
        return self.total_shift_count * self.trips_per_shift


@dataclass(frozen=True)
class ShiftSpec:
    production_date: date
    day_index: int
    shift_type: str
    driver_index: int
    truck_index: int
    watch_period_index: int


@dataclass(frozen=True)
class SeededScope:
    composition: Any
    watch_periods: tuple[Any, ...]
    rating_period: Any
    day_employee_ids: tuple[int, ...]
    night_employee_ids: tuple[int, ...]
    shift_rows: tuple[Any, ...]


def validate_config(config: BenchmarkConfig) -> None:
    """Не позволить параметрам превратить фиксированный benchmark в иной."""

    expected = BenchmarkConfig()
    fixed_fields = (
        "start_date",
        "production_day_count",
        "truck_count",
        "day_driver_count",
        "night_driver_count",
        "trips_per_shift",
        "watch_period_count",
        "rating_day_count",
    )
    mismatches = {
        field: {
            "actual": getattr(config, field),
            "expected": getattr(expected, field),
        }
        for field in fixed_fields
        if getattr(config, field) != getattr(expected, field)
    }
    if mismatches:
        raise PassportBenchmarkError(
            "Фиксированный контракт benchmark изменён: "
            f"{mismatches!r}."
        )
    if config.batch_size < 1 or config.batch_size > 1000:
        raise PassportBenchmarkError(
            "batch_size должен находиться в диапазоне 1..1000."
        )
    if config.materialized_read_repetitions < 1:
        raise PassportBenchmarkError(
            "materialized_read_repetitions должен быть положительным."
        )
    if config.total_shift_count != TOTAL_SHIFT_COUNT:
        raise PassportBenchmarkError(
            f"Ожидалось ровно {TOTAL_SHIFT_COUNT} смен, "
            f"получено {config.total_shift_count}."
        )
    if config.total_embedded_trip_count != TOTAL_EMBEDDED_TRIP_COUNT:
        raise PassportBenchmarkError(
            f"Ожидалось ровно {TOTAL_EMBEDDED_TRIP_COUNT} embedded-рейсов, "
            f"получено {config.total_embedded_trip_count}."
        )


def configured_database_identity(
    database: Mapping[str, Any],
) -> tuple[str, ...]:
    """Вернуть точную проверяемую identity из Django settings."""

    return (
        str(database.get("ENGINE") or ""),
        str(database.get("NAME") or ""),
        str(database.get("USER") or ""),
        str(database.get("HOST") or ""),
        str(database.get("PORT") or ""),
    )


def validate_configured_database(database: Mapping[str, Any]) -> None:
    identity = configured_database_identity(database)
    password_is_empty = str(database.get("PASSWORD") or "") == ""
    if identity != TARGET_DATABASE_IDENTITY or not password_is_empty:
        raise PassportBenchmarkError(
            "Защитная остановка: benchmark разрешён только для "
            f"{TARGET_DB_NAME}@{TARGET_DB_HOST}:{TARGET_DB_PORT}, "
            f"роль={TARGET_DB_USER}, engine={TARGET_DB_ENGINE}, "
            "без пароля в отдельном локальном QA-кластере; "
            f"получено identity={identity!r}."
        )


def validate_actual_database_identity(
    actual: Mapping[str, Any],
) -> None:
    expected = {
        "name": TARGET_DB_NAME,
        "user": TARGET_DB_USER,
        "host": TARGET_DB_HOST,
        "port": TARGET_DB_PORT,
    }
    normalized = {
        key: str(actual.get(key) or "")
        for key in expected
    }
    if normalized != expected:
        raise PassportBenchmarkError(
            "Фактическое PostgreSQL-соединение не соответствует точной "
            f"QA identity: actual={normalized!r}, expected={expected!r}."
        )


def _next_month_anchor(value: date) -> date:
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    return date(year, month, value.day)


def watch_period_ranges(
    config: BenchmarkConfig,
) -> tuple[tuple[date, date], ...]:
    """Четыре календарные вахты 14-го числа по 13-е следующего месяца."""

    validate_config(config)
    ranges: list[tuple[date, date]] = []
    starts_on = FIRST_WATCH_START_DATE
    for _ in range(config.watch_period_count):
        next_starts_on = _next_month_anchor(starts_on)
        ranges.append((starts_on, next_starts_on - timedelta(days=1)))
        starts_on = next_starts_on
    if not (ranges[0][0] <= config.start_date <= ranges[0][1]):
        raise PassportBenchmarkError("Первая вахта не покрывает начало.")
    if not (ranges[-1][0] <= config.end_date <= ranges[-1][1]):
        raise PassportBenchmarkError(
            "Четыре вахты не покрывают все 95 производственных дней."
        )
    return tuple(ranges)


def watch_period_index_for_date(
    production_date: date,
    ranges: Sequence[tuple[date, date]],
) -> int:
    matches = [
        index
        for index, (starts_on, ends_on) in enumerate(ranges)
        if starts_on <= production_date <= ends_on
    ]
    if len(matches) != 1:
        raise PassportBenchmarkError(
            "Производственная дата должна принадлежать ровно одной вахте: "
            f"{production_date.isoformat()}, matches={matches!r}."
        )
    return matches[0]


def iter_shift_specs(config: BenchmarkConfig) -> Iterator[ShiftSpec]:
    ranges = watch_period_ranges(config)
    for day_index in range(config.production_day_count):
        production_date = config.start_date + timedelta(days=day_index)
        watch_index = watch_period_index_for_date(
            production_date,
            ranges,
        )
        for driver_index in range(config.day_driver_count):
            yield ShiftSpec(
                production_date=production_date,
                day_index=day_index,
                shift_type="day",
                driver_index=driver_index,
                truck_index=driver_index,
                watch_period_index=watch_index,
            )
        for driver_index in range(config.night_driver_count):
            yield ShiftSpec(
                production_date=production_date,
                day_index=day_index,
                shift_type="night",
                driver_index=driver_index,
                truck_index=driver_index,
                watch_period_index=watch_index,
            )


def shift_bounds(
    production_date: date,
    shift_type: str,
    *,
    tz: Any,
) -> tuple[datetime, datetime]:
    """Создать соприкасающиеся, но не пересекающиеся 12-часовые окна."""

    from django.utils import timezone

    if shift_type == "day":
        opened_naive = datetime.combine(
            production_date,
            clock_time(8, 0),
        )
    elif shift_type == "night":
        opened_naive = datetime.combine(
            production_date,
            clock_time(20, 0),
        )
    else:
        raise PassportBenchmarkError(
            f"Неизвестный тип смены: {shift_type!r}."
        )
    opened_at = timezone.make_aware(opened_naive, tz)
    return opened_at, opened_at + timedelta(hours=12)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def uppercase_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest().upper()


def employee_scope_fingerprint(employee_ids: Iterable[int]) -> str:
    normalized = tuple(sorted({int(item) for item in employee_ids}))
    return hashlib.sha256(
        ",".join(map(str, normalized)).encode("utf-8")
    ).hexdigest()


def build_embedded_trips(
    *,
    shift_id: int,
    employee_id: int,
    truck_id: int,
    truck_model_id: int,
    rock_type_id: int,
    dump_point_id: int,
    opened_at: datetime,
    driver_index: int,
    trips_per_shift: int = TRIPS_PER_SHIFT,
) -> list[dict[str, Any]]:
    """Создать только JSON-рейсы, не строки ``trips_trip``."""

    if trips_per_shift != TRIPS_PER_SHIFT:
        raise PassportBenchmarkError(
            f"В паспорте должно быть ровно {TRIPS_PER_SHIFT} рейсов."
        )
    result: list[dict[str, Any]] = []
    elapsed_seconds = 0
    base_cycle_seconds = 480 + (driver_index % 8) * 24
    jitter = (-18, -9, 0, 9, 18, 12, -12, 6, -6, 0)
    for trip_index in range(trips_per_shift):
        cycle_seconds = base_cycle_seconds + jitter[
            trip_index % len(jitter)
        ]
        created_at = opened_at + timedelta(
            minutes=8,
            seconds=elapsed_seconds,
        )
        completed_at = created_at + timedelta(seconds=cycle_seconds)
        elapsed_seconds += cycle_seconds + 30
        result.append({
            "id": shift_id * 100 + trip_index + 1,
            "truck_model_id": truck_model_id,
            "truck": {
                "id": truck_id,
                "model_id": truck_model_id,
            },
            "rock_type_id": rock_type_id,
            "rock_type": {
                "id": rock_type_id,
                "density": "2.5000",
            },
            "dump_point_id": dump_point_id,
            "excavator_id": 900001 + (driver_index % 8),
            "loading_horizon": f"H-{driver_index % 3 + 1}",
            "loading_block": f"B-{driver_index % 5 + 1}",
            "actual_dump_point_id": dump_point_id,
            "assigned_dump_point_id": dump_point_id,
            "unloading_shift_id": shift_id,
            "driver_id": employee_id,
            "volume_m3": "50.00",
            "tonnage": "125.00",
            "transport_distance_km": None,
            "created_at": created_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "is_carryover": False,
        })
    return result


def build_passport_payload(
    *,
    shift_id: int,
    employee_id: int,
    equipment_id: int,
    truck_model_id: int,
    rock_type_id: int,
    dump_point_id: int,
    watch_period_id: int,
    watch_composition_id: int,
    shift_type: str,
    opened_at: datetime,
    closed_at: datetime,
    driver_index: int,
    trips_per_shift: int = TRIPS_PER_SHIFT,
) -> dict[str, Any]:
    trips = build_embedded_trips(
        shift_id=shift_id,
        employee_id=employee_id,
        truck_id=equipment_id,
        truck_model_id=truck_model_id,
        rock_type_id=rock_type_id,
        dump_point_id=dump_point_id,
        opened_at=opened_at,
        driver_index=driver_index,
        trips_per_shift=trips_per_shift,
    )
    volume = 50 * len(trips)
    tonnage = 125 * len(trips)
    return {
        "schema_version": 1,
        "calculator_version": BENCHMARK_CALCULATOR_VERSION,
        "official": False,
        "shift_id": shift_id,
        "source_manifest": {
            "manifest_schema_version": 1,
            "shift": {
                "id": shift_id,
                "employee_id": employee_id,
                "equipment_id": equipment_id,
                "workplace_code": "driver",
                "shift_type": shift_type,
                "opened_at": opened_at.isoformat(),
                "closed_at": closed_at.isoformat(),
                "watch_period": {
                    "id": watch_period_id,
                    "watch_composition": {
                        "id": watch_composition_id,
                    },
                },
            },
            "trips": trips,
            "downtimes": [],
            "assignments": [],
            "reading_corrections": [],
        },
        "passport": {
            "production": {
                "completed_trip_count": len(trips),
                "output_attribution": {
                    "unloading_shift_trip_count": len(trips),
                    "legacy_driver_trip_count": 0,
                    "ambiguous_trip_count": 0,
                },
                "volume_m3": {
                    "known_value": f"{volume:.2f}",
                    "is_complete": True,
                },
                "tonnage_t": {
                    "known_value": f"{tonnage:.2f}",
                    "is_complete": True,
                },
                "m3_km": {
                    "known_value": None,
                    "is_complete": False,
                },
                "t_km": {
                    "known_value": None,
                    "is_complete": False,
                },
            },
            "time": {
                "available_seconds": 43200,
                "downtime_review_seconds": 0,
                "scheduled_window_status": (
                    "schedule_snapshot_unavailable"
                ),
                "unjustified_short_shift_seconds": None,
                "extra_presence_seconds": 0,
                "confirmed_extra_productive_seconds": 0,
                "inferred_schedule_gap_seconds": None,
                "work_time_rating_available": False,
                "work_time_rating_status": (
                    "neutral_structural_schedule_and_reason_policy_unavailable"
                ),
            },
            "routing": {
                "match_count": len(trips),
                "mismatch_count": 0,
                "missing_actual_count": 0,
                "missing_assigned_count": 0,
            },
            "open_close": {
                "window_valid": True,
                "opened_by_employee": True,
                "closed_by_employee": True,
                "service_closed": False,
                "start_readings_complete": True,
                "end_readings_complete": True,
            },
            "quality": {
                "coverage_percent": 45,
                "flags": ["unexplained_time"],
                "quality_metrics": {
                    "trip_without_assignment_seconds": 0,
                    "trip_assignment_mismatch_seconds": 0,
                },
                "official_rating_eligible": False,
            },
        },
    }


def chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    if size < 1:
        raise PassportBenchmarkError(
            "Размер batch должен быть положительным."
        )
    for offset in range(0, len(items), size):
        yield items[offset:offset + size]


def expected_final_counts(config: BenchmarkConfig) -> dict[str, int]:
    validate_config(config)
    return {
        "employees": TOTAL_DRIVER_COUNT,
        "equipment_types": 1,
        "equipment_models": 1,
        "equipment": TRUCK_COUNT,
        "rock_types": 1,
        "dump_points": 1,
        "watch_compositions": 1,
        "watch_periods": WATCH_PERIOD_COUNT,
        "rating_periods": 1,
        "employee_shifts": TOTAL_SHIFT_COUNT,
        "open_shifts": 0,
        "day_driver_shifts": TOTAL_SHIFT_COUNT // 2,
        "night_driver_shifts": TOTAL_SHIFT_COUNT // 2,
        "passport_snapshots": TOTAL_SHIFT_COUNT,
        "passport_requests": 0,
        "materialized_snapshots": 0,
        "trips": 0,
        "downtime_events": 0,
        "equipment_assignments": 0,
        "crew_plans": 0,
        "crew_plan_slots": 0,
        "haul_assignments": 0,
        "excavator_placements": 0,
    }


def assert_empty_counts(counts: Mapping[str, int]) -> None:
    nonempty = {
        key: int(value)
        for key, value in counts.items()
        if int(value) != 0
    }
    if nonempty:
        raise PassportBenchmarkError(
            "Целевая QA-БД не пуста; очистка и повторное использование "
            f"запрещены: {nonempty!r}."
        )


def assert_final_counts(
    actual: Mapping[str, int],
    expected: Mapping[str, int],
) -> None:
    mismatches = {
        key: {
            "actual": int(actual.get(key, -1)),
            "expected": int(expected_value),
        }
        for key, expected_value in expected.items()
        if int(actual.get(key, -1)) != int(expected_value)
    }
    unexpected = {
        key: int(value)
        for key, value in actual.items()
        if key not in expected and int(value) != 0
    }
    if mismatches or unexpected:
        raise PassportBenchmarkError(
            "Финальная целостность QA-БД нарушена: "
            f"mismatches={mismatches!r}, unexpected={unexpected!r}."
        )


def ensure_new_artifact_directory(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.exists() and any(resolved.iterdir()):
        raise PassportBenchmarkError(
            "Каталог артефактов уже содержит файлы; перезапись запрещена: "
            f"{resolved}"
        )
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def write_new_json(path: Path, payload: Any) -> str:
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
            default=str,
        )
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(encoded)
    return hashlib.sha256(encoded).hexdigest().upper()


def bootstrap_django() -> None:
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()


def database_counts() -> dict[str, int]:
    from assignments.models import (
        CrewPlan,
        CrewPlanSlot,
        EquipmentAssignment,
        ExcavatorPlacement,
        HaulAssignment,
    )
    from downtimes.models import DowntimeEvent
    from references.models import (
        DumpPoint,
        Equipment,
        EquipmentModel,
        EquipmentType,
        RockType,
    )
    from reports.models import (
        DriverRatingPeriodMaterializedSnapshot,
        DriverShiftPassportCaptureRequest,
        DriverShiftPassportSnapshot,
        RatingPeriod,
    )
    from shifts.models import EmployeeShift, ShiftType, WatchPeriod
    from trips.models import Trip
    from users.models import Employee, WatchComposition

    return {
        "employees": Employee.objects.count(),
        "equipment_types": EquipmentType.objects.count(),
        "equipment_models": EquipmentModel.objects.count(),
        "equipment": Equipment.objects.count(),
        "rock_types": RockType.objects.count(),
        "dump_points": DumpPoint.objects.count(),
        "watch_compositions": WatchComposition.objects.count(),
        "watch_periods": WatchPeriod.objects.count(),
        "rating_periods": RatingPeriod.objects.count(),
        "employee_shifts": EmployeeShift.objects.count(),
        "open_shifts": EmployeeShift.objects.filter(
            closed_at__isnull=True,
        ).count(),
        "day_driver_shifts": EmployeeShift.objects.filter(
            workplace_code="driver",
            shift_type=ShiftType.DAY,
        ).count(),
        "night_driver_shifts": EmployeeShift.objects.filter(
            workplace_code="driver",
            shift_type=ShiftType.NIGHT,
        ).count(),
        "passport_snapshots": (
            DriverShiftPassportSnapshot.objects.count()
        ),
        "passport_requests": (
            DriverShiftPassportCaptureRequest.objects.count()
        ),
        "materialized_snapshots": (
            DriverRatingPeriodMaterializedSnapshot.objects.count()
        ),
        "trips": Trip.objects.count(),
        "downtime_events": DowntimeEvent.objects.count(),
        "equipment_assignments": EquipmentAssignment.objects.count(),
        "crew_plans": CrewPlan.objects.count(),
        "crew_plan_slots": CrewPlanSlot.objects.count(),
        "haul_assignments": HaulAssignment.objects.count(),
        "excavator_placements": ExcavatorPlacement.objects.count(),
    }


def verify_database(*, require_empty: bool) -> dict[str, Any]:
    from django.conf import settings
    from django.db import connection

    database = settings.DATABASES["default"]
    validate_configured_database(database)
    with connection.cursor() as cursor:
        cursor.execute(
            "select current_database(), current_user, "
            "host(inet_server_addr()), inet_server_port()::text, "
            "current_setting('server_version')"
        )
        name, user, host, port, server_version = cursor.fetchone()
    actual = {
        "name": name,
        "user": user,
        "host": host,
        "port": port,
        "server_version": server_version,
    }
    validate_actual_database_identity(actual)
    counts = database_counts()
    if require_empty:
        assert_empty_counts(counts)
    return {
        "configured": {
            "engine": TARGET_DB_ENGINE,
            "name": TARGET_DB_NAME,
            "user": TARGET_DB_USER,
            "host": TARGET_DB_HOST,
            "port": TARGET_DB_PORT,
        },
        "actual": {
            key: str(value or "")
            for key, value in actual.items()
        },
        "counts": counts,
    }


@contextmanager
def benchmark_advisory_lock() -> Iterator[None]:
    from django.db import connection

    acquired = False
    with connection.cursor() as cursor:
        cursor.execute(
            "select pg_try_advisory_lock(%s)",
            [BENCHMARK_ADVISORY_LOCK_ID],
        )
        acquired = bool(cursor.fetchone()[0])
    if not acquired:
        raise PassportBenchmarkError(
            "Другой процесс уже выполняет этот benchmark."
        )
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_advisory_unlock(%s)",
                [BENCHMARK_ADVISORY_LOCK_ID],
            )


def seed_database(
    config: BenchmarkConfig,
) -> SeededScope:
    from django.db import transaction
    from django.utils import timezone

    from references.models import (
        DumpPoint,
        Equipment,
        EquipmentModel,
        EquipmentType,
        RockType,
    )
    from reports.models import (
        DriverShiftPassportSnapshot,
        DriverShiftPassportTrigger,
        RatingPeriod,
    )
    from reports.driver_shift_passport_snapshots import (
        _assert_diagnostic_payload,
        _fingerprint,
    )
    from shifts.models import EmployeeShift, ShiftType, WatchPeriod
    from users.models import Employee, WatchComposition

    validate_config(config)
    ranges = watch_period_ranges(config)
    tz = timezone.get_current_timezone()

    with transaction.atomic():
        composition = WatchComposition.objects.create(
            code="qa-rating-passport-10070",
            name=f"{BENCHMARK_MARKER}: утверждённый состав",
            is_active=True,
        )
        watch_periods = [
            WatchPeriod(
                name=(
                    f"{BENCHMARK_MARKER}: вахта "
                    f"{starts_on:%d.%m.%Y}–{ends_on:%d.%m.%Y}"
                ),
                watch_composition=composition,
                starts_on=starts_on,
                ends_on=ends_on,
                is_active=True,
            )
            for starts_on, ends_on in ranges
        ]
        WatchPeriod.objects.bulk_create(
            watch_periods,
            batch_size=config.batch_size,
        )
        if any(item.pk is None for item in watch_periods):
            raise PassportBenchmarkError(
                "PostgreSQL не вернул ID созданных WatchPeriod."
            )

        rating_period = RatingPeriod.objects.create(
            name=f"{BENCHMARK_MARKER}: последние 30 дней",
            starts_on=config.rating_starts_on,
            ends_before=config.rating_ends_before,
            nominal_starts_on=config.rating_starts_on,
            comment=(
                "Только синтетический технический benchmark; "
                "не является калибровкой KPI."
            ),
            is_active=True,
        )

        equipment_type = EquipmentType.objects.create(
            name=f"{BENCHMARK_MARKER}: самосвал",
        )
        equipment_model = EquipmentModel.objects.create(
            equipment_type=equipment_type,
            name=f"{BENCHMARK_MARKER}: модель 100/50",
            payload_tons="100.00",
            body_volume_m3="50.00",
        )
        rock_type = RockType.objects.create(
            name=f"{BENCHMARK_MARKER}: порода",
            density="2.5000",
        )
        dump_point = DumpPoint.objects.create(
            name=f"{BENCHMARK_MARKER}: точка разгрузки",
        )
        trucks = [
            Equipment(
                equipment_type=equipment_type,
                model=equipment_model,
                garage_number=f"QA-BENCH-{ordinal:03d}",
                is_own=True,
                is_active=True,
            )
            for ordinal in range(1, config.truck_count + 1)
        ]
        Equipment.objects.bulk_create(
            trucks,
            batch_size=config.batch_size,
        )
        if any(item.pk is None for item in trucks):
            raise PassportBenchmarkError(
                "PostgreSQL не вернул ID созданных самосвалов."
            )

        employees = [
            Employee(
                full_name=(
                    f"{BENCHMARK_MARKER} Дневной водитель "
                    f"{ordinal:03d}"
                ),
                position="Водитель самосвала",
                department="Синтетический QA",
                work_category=Employee.WorkCategory.DRIVER,
                personnel_number=f"QAB-D-{ordinal:03d}",
                status=Employee.Status.ACTIVE,
                brigade_number=Employee.BrigadeNumber.BRIGADE_1,
                watch_composition=composition,
                is_active=True,
            )
            for ordinal in range(1, config.day_driver_count + 1)
        ]
        employees.extend(
            Employee(
                full_name=(
                    f"{BENCHMARK_MARKER} Ночной водитель "
                    f"{ordinal:03d}"
                ),
                position="Водитель самосвала",
                department="Синтетический QA",
                work_category=Employee.WorkCategory.DRIVER,
                personnel_number=f"QAB-N-{ordinal:03d}",
                status=Employee.Status.ACTIVE,
                brigade_number=Employee.BrigadeNumber.BRIGADE_3,
                watch_composition=composition,
                is_active=True,
            )
            for ordinal in range(1, config.night_driver_count + 1)
        )
        Employee.objects.bulk_create(
            employees,
            batch_size=config.batch_size,
        )
        if any(item.pk is None for item in employees):
            raise PassportBenchmarkError(
                "PostgreSQL не вернул ID созданных водителей."
            )
        day_employees = employees[:config.day_driver_count]
        night_employees = employees[config.day_driver_count:]

        shifts: list[Any] = []
        shift_metadata: list[tuple[int, int]] = []
        for spec in iter_shift_specs(config):
            employee = (
                day_employees[spec.driver_index]
                if spec.shift_type == ShiftType.DAY
                else night_employees[spec.driver_index]
            )
            truck = trucks[spec.truck_index]
            opened_at, closed_at = shift_bounds(
                spec.production_date,
                spec.shift_type,
                tz=tz,
            )
            shifts.append(EmployeeShift(
                employee=employee,
                shift_type=spec.shift_type,
                workplace_code="driver",
                watch_period=watch_periods[
                    spec.watch_period_index
                ],
                equipment=truck,
                start_fuel="1000.00",
                start_mileage=f"{10000 + spec.day_index * 100:.2f}",
                start_engine_hours=(
                    f"{2000 + spec.day_index * 10:.2f}"
                ),
                end_fuel="900.00",
                end_mileage=f"{10100 + spec.day_index * 100:.2f}",
                end_engine_hours=(
                    f"{2010 + spec.day_index * 10:.2f}"
                ),
                opened_at=opened_at,
                closed_at=closed_at,
                opened_by=employee,
                closed_by=employee,
                is_service_closed=False,
            ))
            shift_metadata.append(
                (spec.driver_index, spec.watch_period_index)
            )
        EmployeeShift.objects.bulk_create(
            shifts,
            batch_size=config.batch_size,
        )
        if (
            len(shifts) != TOTAL_SHIFT_COUNT
            or any(item.pk is None for item in shifts)
        ):
            raise PassportBenchmarkError(
                "PostgreSQL не вернул полный набор ID для "
                f"{TOTAL_SHIFT_COUNT} смен."
            )

        for shift_chunk, metadata_chunk in zip(
            chunks(shifts, config.batch_size),
            chunks(shift_metadata, config.batch_size),
            strict=True,
        ):
            snapshots = []
            for shift, (driver_index, _watch_index) in zip(
                shift_chunk,
                metadata_chunk,
                strict=True,
            ):
                payload = build_passport_payload(
                    shift_id=shift.pk,
                    employee_id=shift.employee_id,
                    equipment_id=shift.equipment_id,
                    truck_model_id=equipment_model.pk,
                    rock_type_id=rock_type.pk,
                    dump_point_id=dump_point.pk,
                    watch_period_id=shift.watch_period_id,
                    watch_composition_id=composition.pk,
                    shift_type=shift.shift_type,
                    opened_at=shift.opened_at,
                    closed_at=shift.closed_at,
                    driver_index=driver_index,
                    trips_per_shift=config.trips_per_shift,
                )
                _assert_diagnostic_payload(payload)
                source_fingerprint = _fingerprint(
                    payload["source_manifest"]
                )
                payload_fingerprint = _fingerprint(payload)
                if (
                    source_fingerprint
                    != fingerprint(payload["source_manifest"])
                    or payload_fingerprint != fingerprint(payload)
                ):
                    raise PassportBenchmarkError(
                        "Standalone fingerprint разошёлся со штатным "
                        "fingerprint паспорта."
                    )
                snapshots.append(DriverShiftPassportSnapshot(
                    shift=shift,
                    revision=1,
                    schema_version=1,
                    calculator_version=(
                        BENCHMARK_CALCULATOR_VERSION
                    ),
                    source_fingerprint=source_fingerprint,
                    payload_fingerprint=payload_fingerprint,
                    payload=payload,
                    trigger=DriverShiftPassportTrigger.BACKFILL,
                    captured_late=True,
                ))
            DriverShiftPassportSnapshot.objects.bulk_create(
                snapshots,
                batch_size=config.batch_size,
            )

    return SeededScope(
        composition=composition,
        watch_periods=tuple(watch_periods),
        rating_period=rating_period,
        day_employee_ids=tuple(
            item.pk for item in day_employees
        ),
        night_employee_ids=tuple(
            item.pk for item in night_employees
        ),
        shift_rows=tuple(shifts),
    )


def verify_snapshot_integrity(
    config: BenchmarkConfig,
    scope: SeededScope,
) -> dict[str, Any]:
    from django.db import connection
    from django.db.models import Count, F
    from django.utils.dateparse import parse_datetime

    from core.production_time import production_work_date
    from reports.driver_shift_passport_snapshots import (
        _assert_diagnostic_payload,
        _fingerprint,
    )
    from reports.models import DriverShiftPassportSnapshot
    from shifts.models import EmployeeShift

    digest = hashlib.sha256()
    embedded_trip_ids: set[int] = set()
    checked = 0
    date_mismatch_count = 0
    structural_mismatch_count = 0
    rating_counts = {"day": 0, "night": 0}
    watch_usage_counts = [0] * config.watch_period_count
    for row in (
        DriverShiftPassportSnapshot.objects
        .order_by("shift_id", "revision")
        .values(
            "shift_id",
            "revision",
            "source_fingerprint",
            "payload_fingerprint",
            "payload",
            "trigger",
            "captured_late",
            "shift__employee_id",
            "shift__equipment_id",
            "shift__workplace_code",
            "shift__shift_type",
            "shift__opened_at",
            "shift__closed_at",
            "shift__watch_period_id",
            "shift__watch_period__starts_on",
            "shift__watch_period__ends_on",
            (
                "shift__watch_period__watch_composition_id"
            ),
        )
        .iterator(chunk_size=config.batch_size)
    ):
        payload = row["payload"]
        manifest = (
            payload.get("source_manifest")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(manifest, dict):
            raise PassportBenchmarkError(
                f"Паспорт смены {row['shift_id']} не содержит manifest."
            )
        _assert_diagnostic_payload(payload)
        shift_manifest = manifest.get("shift")
        if not isinstance(shift_manifest, dict):
            raise PassportBenchmarkError(
                f"Паспорт смены {row['shift_id']} не содержит shift manifest."
            )
        trips = manifest.get("trips")
        if (
            not isinstance(trips, list)
            or len(trips) != config.trips_per_shift
        ):
            raise PassportBenchmarkError(
                f"Смена {row['shift_id']} содержит неверное число рейсов."
            )
        if manifest.get("downtimes") != []:
            raise PassportBenchmarkError(
                f"Смена {row['shift_id']} содержит embedded-простой."
            )
        if manifest.get("assignments") != []:
            raise PassportBenchmarkError(
                f"Смена {row['shift_id']} содержит embedded-назначение."
            )
        if (
            row["source_fingerprint"] != _fingerprint(manifest)
            or row["source_fingerprint"] != fingerprint(manifest)
        ):
            raise PassportBenchmarkError(
                f"source_fingerprint не совпал для смены {row['shift_id']}."
            )
        if (
            row["payload_fingerprint"] != _fingerprint(payload)
            or row["payload_fingerprint"] != fingerprint(payload)
        ):
            raise PassportBenchmarkError(
                f"payload_fingerprint не совпал для смены {row['shift_id']}."
            )
        expected_structure = {
            "id": row["shift_id"],
            "employee_id": row["shift__employee_id"],
            "equipment_id": row["shift__equipment_id"],
            "workplace_code": row["shift__workplace_code"],
            "shift_type": row["shift__shift_type"],
        }
        if any(
            shift_manifest.get(key) != value
            for key, value in expected_structure.items()
        ):
            structural_mismatch_count += 1
        if (
            parse_datetime(str(shift_manifest.get("opened_at") or ""))
            != row["shift__opened_at"]
            or parse_datetime(
                str(shift_manifest.get("closed_at") or "")
            )
            != row["shift__closed_at"]
        ):
            structural_mismatch_count += 1
        watch_manifest = shift_manifest.get("watch_period")
        if (
            not isinstance(watch_manifest, dict)
            or watch_manifest.get("id")
            != row["shift__watch_period_id"]
            or not isinstance(
                watch_manifest.get("watch_composition"),
                dict,
            )
            or watch_manifest["watch_composition"].get("id")
            != row[
                "shift__watch_period__watch_composition_id"
            ]
        ):
            structural_mismatch_count += 1
        if (
            row["revision"] != 1
            or row["trigger"] != "backfill"
            or row["captured_late"] is not True
        ):
            structural_mismatch_count += 1

        work_date = production_work_date(row["shift__opened_at"])
        if not (
            row["shift__watch_period__starts_on"]
            <= work_date
            <= row["shift__watch_period__ends_on"]
        ):
            date_mismatch_count += 1
        if (
            config.rating_starts_on
            <= work_date
            < config.rating_ends_before
        ):
            rating_counts[row["shift__shift_type"]] += 1
        try:
            watch_index = tuple(
                item.pk for item in scope.watch_periods
            ).index(row["shift__watch_period_id"])
        except ValueError as error:
            raise PassportBenchmarkError(
                "Смена ссылается на WatchPeriod вне benchmark scope."
            ) from error
        watch_usage_counts[watch_index] += 1

        for trip in trips:
            trip_id = int(trip["id"])
            if trip_id in embedded_trip_ids:
                raise PassportBenchmarkError(
                    f"Повторился embedded trip ID {trip_id}."
                )
            if int(trip["unloading_shift_id"]) != int(row["shift_id"]):
                raise PassportBenchmarkError(
                    "Embedded рейс связан не со своей сменой: "
                    f"trip={trip_id}, shift={row['shift_id']}."
                )
            embedded_trip_ids.add(trip_id)
        digest.update(
            (
                f"{row['shift_id']}:{row['revision']}:"
                f"{row['source_fingerprint']}:"
                f"{row['payload_fingerprint']}\n"
            ).encode("ascii")
        )
        checked += 1

    if checked != config.total_shift_count:
        raise PassportBenchmarkError(
            f"Проверено паспортов {checked}, ожидалось "
            f"{config.total_shift_count}."
        )
    if len(embedded_trip_ids) != config.total_embedded_trip_count:
        raise PassportBenchmarkError(
            f"Проверено embedded-рейсов {len(embedded_trip_ids)}, "
            f"ожидалось {config.total_embedded_trip_count}."
        )
    if date_mismatch_count:
        raise PassportBenchmarkError(
            f"Найдено date mismatch смен/вахт: {date_mismatch_count}."
        )
    if structural_mismatch_count:
        raise PassportBenchmarkError(
            "Manifest/live structural mismatch: "
            f"{structural_mismatch_count}."
        )
    expected_rating_counts = {
        "day": RATING_SHIFT_COUNT_PER_GROUP,
        "night": RATING_SHIFT_COUNT_PER_GROUP,
    }
    if rating_counts != expected_rating_counts:
        raise PassportBenchmarkError(
            "Неверное число смен в последних 30 днях: "
            f"{rating_counts!r} != {expected_rating_counts!r}."
        )
    expected_watch_usage = [636, 2968, 3286, 3180]
    if watch_usage_counts != expected_watch_usage:
        raise PassportBenchmarkError(
            "Неверное распределение смен по четырём вахтам: "
            f"{watch_usage_counts!r} != {expected_watch_usage!r}."
        )

    duplicate_shift_revision_count = (
        DriverShiftPassportSnapshot.objects
        .values("shift_id", "revision")
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
        .count()
    )
    duplicate_shift_source_count = (
        DriverShiftPassportSnapshot.objects
        .values(
            "shift_id",
            "calculator_version",
            "source_fingerprint",
        )
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
        .count()
    )
    reversed_window_count = EmployeeShift.objects.filter(
        closed_at__lte=F("opened_at"),
    ).count()

    table_name = connection.ops.quote_name(
        EmployeeShift._meta.db_table,
    )

    def overlap_count(partition_column: str) -> int:
        if partition_column not in {"employee_id", "equipment_id"}:
            raise PassportBenchmarkError(
                "Недопустимый partition для overlap audit."
            )
        quoted_partition = connection.ops.quote_name(partition_column)
        with connection.cursor() as cursor:
            cursor.execute(
                (
                    "SELECT COUNT(*) FROM ("
                    " SELECT opened_at, "
                    f"LAG(closed_at) OVER (PARTITION BY {quoted_partition} "
                    "ORDER BY opened_at, id) AS previous_closed_at"
                    f" FROM {table_name}"
                    " WHERE workplace_code = %s"
                    ") AS ordered_shifts "
                    "WHERE previous_closed_at > opened_at"
                ),
                ["driver"],
            )
            return int(cursor.fetchone()[0])

    employee_overlap_count = overlap_count("employee_id")
    equipment_overlap_count = overlap_count("equipment_id")
    if any((
        duplicate_shift_revision_count,
        duplicate_shift_source_count,
        reversed_window_count,
        employee_overlap_count,
        equipment_overlap_count,
    )):
        raise PassportBenchmarkError(
            "Нарушена временная/уникальная целостность смен: "
            f"duplicate_snapshot_keys={duplicate_shift_revision_count}, "
            f"duplicate_source_keys={duplicate_shift_source_count}, "
            f"reversed_windows={reversed_window_count}, "
            f"employee_overlaps={employee_overlap_count}, "
            f"equipment_overlaps={equipment_overlap_count}."
        )

    return {
        "checked_passports": checked,
        "checked_embedded_trips": len(embedded_trip_ids),
        "historical_passports_outside_rating_period": (
            checked - sum(rating_counts.values())
        ),
        "rating_period_shift_counts": rating_counts,
        "watch_period_shift_counts": watch_usage_counts,
        "date_mismatch_count": date_mismatch_count,
        "structural_mismatch_count": structural_mismatch_count,
        "duplicate_snapshot_key_count": (
            duplicate_shift_revision_count
        ),
        "duplicate_snapshot_source_key_count": (
            duplicate_shift_source_count
        ),
        "reversed_window_count": reversed_window_count,
        "employee_overlap_count": employee_overlap_count,
        "equipment_overlap_count": equipment_overlap_count,
        "fingerprint_chain_sha256": digest.hexdigest().upper(),
    }


def validate_formula_result(
    result: Mapping[str, Any],
    *,
    expected_employee_ids: Sequence[int],
) -> None:
    summary = result.get("summary")
    linkage = result.get("linkage_audit")
    entries = result.get("entries")
    failures: list[str] = []
    if result.get("available") is not True:
        failures.append("available")
    if result.get("official") is not False:
        failures.append("official")
    if result.get("official_rating_eligible") is True:
        failures.append("official_rating_eligible")
    if not isinstance(summary, dict):
        failures.append("summary")
        summary = {}
    if not isinstance(linkage, dict):
        failures.append("linkage_audit")
    if not isinstance(entries, list):
        failures.append("entries")
        entries = []
    if int(summary.get("employee_count", -1)) != len(
        expected_employee_ids
    ):
        failures.append("employee_count")
    if int(summary.get("rated_shift_count", -1)) != (
        RATING_SHIFT_COUNT_PER_GROUP
    ):
        failures.append("rated_shift_count")
    if int(summary.get("withheld_shift_count", -1)) != 0:
        failures.append("withheld_shift_count")
    if summary.get("withheld_reasons") not in ({}, None):
        failures.append("withheld_reasons")
    if int(summary.get("trip_count", -1)) != (
        RATING_TRIP_COUNT_PER_GROUP
    ):
        failures.append("trip_count")
    expected_employee_ids = tuple(map(int, expected_employee_ids))
    entry_employee_ids: list[int] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("employee_id") is None:
            failures.append("entry_employee_id")
            continue
        try:
            entry_employee_ids.append(int(entry["employee_id"]))
        except (TypeError, ValueError):
            failures.append("entry_employee_id")
    if len(entries) != len(expected_employee_ids):
        failures.append("entry_count")
    if len(entry_employee_ids) != len(set(entry_employee_ids)):
        failures.append("duplicate_employee_ids")
    if set(entry_employee_ids) != set(expected_employee_ids):
        failures.append("employee_ids")
    distance_metrics = result.get("distance_metrics")
    if (
        not isinstance(distance_metrics, dict)
        or str(distance_metrics.get("weight")) != "0"
    ):
        failures.append("distance_weight")
    if failures:
        raise PassportBenchmarkError(
            "Формула не прошла фиксированный контракт: "
            f"{sorted(set(failures))!r}; summary={summary!r}."
        )


def _measure_call(callable_: Any) -> tuple[Any, dict[str, Any]]:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    started = time.perf_counter()
    with CaptureQueriesContext(connection) as queries:
        value = callable_()
    duration = time.perf_counter() - started
    return value, {
        "seconds": round(duration, 6),
        "query_count": len(queries),
    }


def benchmark_formula_group(
    scope: SeededScope,
    *,
    shift_type: str,
    employee_ids: Sequence[int],
    as_of: datetime,
) -> dict[str, Any]:
    from django.core.cache import cache
    from django.test import override_settings
    from unittest.mock import patch

    from reports.driver_watch_rating import (
        build_driver_rating_period,
        get_cached_driver_rating_period,
    )

    kwargs = {
        "shift_type": shift_type,
        "allowed_employee_ids": tuple(employee_ids),
        "expected_employee_ids": tuple(employee_ids),
    }

    def direct() -> dict[str, Any]:
        return build_driver_rating_period(
            scope.rating_period,
            scope.composition,
            **kwargs,
        )

    def cached() -> dict[str, Any]:
        return get_cached_driver_rating_period(
            scope.rating_period,
            scope.composition,
            **kwargs,
        )

    cache_settings = {
        "default": {
            "BACKEND": (
                "django.core.cache.backends.locmem.LocMemCache"
            ),
            "LOCATION": (
                f"{BENCHMARK_RUN_ID}-{shift_type}-isolated"
            ),
        },
    }
    with (
        patch(
            "reports.driver_watch_rating.timezone.now",
            return_value=as_of,
        ),
        override_settings(CACHES=cache_settings),
    ):
        first, direct_metrics = _measure_call(direct)
        validate_formula_result(
            first,
            expected_employee_ids=employee_ids,
        )
        repeated, repeat_metrics = _measure_call(direct)
        validate_formula_result(
            repeated,
            expected_employee_ids=employee_ids,
        )
        first_sha = uppercase_sha256(first)
        repeat_sha = uppercase_sha256(repeated)
        if first_sha != repeat_sha:
            raise PassportBenchmarkError(
                f"Повтор формулы {shift_type} недетерминирован: "
                f"{first_sha} != {repeat_sha}."
            )

        cache.clear()
        cold, cold_metrics = _measure_call(cached)
        validate_formula_result(
            cold,
            expected_employee_ids=employee_ids,
        )
        hot, hot_metrics = _measure_call(cached)
        validate_formula_result(
            hot,
            expected_employee_ids=employee_ids,
        )
        cold_sha = uppercase_sha256(cold)
        hot_sha = uppercase_sha256(hot)
        if len({first_sha, cold_sha, hot_sha}) != 1:
            raise PassportBenchmarkError(
                f"Direct/cold/hot формула {shift_type} разошлась: "
                f"{first_sha}, {cold_sha}, {hot_sha}."
            )

    return {
        "shift_type": shift_type,
        "employee_count": len(employee_ids),
        "rated_shift_count": RATING_SHIFT_COUNT_PER_GROUP,
        "embedded_trip_count": RATING_TRIP_COUNT_PER_GROUP,
        "result_sha256": first_sha,
        "source_fingerprint": first.get("source_fingerprint"),
        "shift_score_fingerprint": first.get(
            "shift_score_fingerprint"
        ),
        "direct": direct_metrics,
        "repeat_direct": repeat_metrics,
        "cold_cache": cold_metrics,
        "hot_cache": hot_metrics,
    }


def benchmark_formula(
    scope: SeededScope,
    config: BenchmarkConfig,
) -> dict[str, Any]:
    from django.utils import timezone

    as_of = timezone.make_aware(
        datetime.combine(
            config.rating_ends_before,
            clock_time(9, 0),
        ),
        timezone.get_current_timezone(),
    )
    return {
        "formula_repeat_check": True,
        "as_of": as_of.isoformat(),
        "groups": [
            benchmark_formula_group(
                scope,
                shift_type="day",
                employee_ids=scope.day_employee_ids,
                as_of=as_of,
            ),
            benchmark_formula_group(
                scope,
                shift_type="night",
                employee_ids=scope.night_employee_ids,
                as_of=as_of,
            ),
        ],
    }


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        raise PassportBenchmarkError(
            "Нельзя вычислить percentile пустой выборки."
        )
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        max(0, int(round((len(ordered) - 1) * fraction))),
    )
    return ordered[index]


_EXPECTED_REFRESH_COMMAND_LIFECYCLE = {
    1: {
        "groups": {
            "day": ("verified", 1),
            "night": ("published", 1),
        },
        "summary": {
            "attempted": 2,
            "published": 1,
            "verified": 1,
            "locked": 0,
            "failures": 0,
        },
    },
    2: {
        "groups": {
            "day": ("verified", 1),
            "night": ("verified", 1),
        },
        "summary": {
            "attempted": 2,
            "published": 0,
            "verified": 2,
            "locked": 0,
            "failures": 0,
        },
    },
}


def _parse_refresh_command_cycle(
    *,
    ordinal: int,
    stdout: str,
    stderr: str,
    metrics: Mapping[str, Any],
) -> dict[str, Any]:
    group_pattern = re.compile(
        r"^[^/\r\n]+/(?P<shift_type>day|night): "
        r"(?P<status>published|verified|locked), "
        r"revision=(?P<revision>\d+)$",
        re.MULTILINE,
    )
    groups = [
        {
            "shift_type": match.group("shift_type"),
            "status": match.group("status"),
            "revision": int(match.group("revision")),
        }
        for match in group_pattern.finditer(stdout)
    ]
    summary_pattern = re.compile(
        r"Групп: (?P<attempted>\d+); "
        r"опубликовано: (?P<published>\d+); "
        r"без изменения данных: (?P<verified>\d+); "
        r"уже выполнялись: (?P<locked>\d+); "
        r"ошибок: (?P<failures>\d+)\."
    )
    summary_match = summary_pattern.search(stdout)
    if summary_match is None:
        raise PassportBenchmarkError(
            "Команда refresh не вернула проверяемую итоговую строку."
        )
    summary = {
        key: int(value)
        for key, value in summary_match.groupdict().items()
    }
    status_counts = {
        status: sum(
            item["status"] == status
            for item in groups
        )
        for status in ("published", "verified", "locked")
    }
    if stderr.strip():
        raise PassportBenchmarkError(
            "Команда refresh записала сообщение в stderr: "
            f"{stderr.strip()!r}."
        )
    if (
        summary["attempted"] != 2
        or summary["failures"] != 0
        or summary["locked"] != 0
        or len(groups) != 2
        or {item["shift_type"] for item in groups} != {"day", "night"}
        or any(item["status"] == "locked" for item in groups)
        or any(
            summary[status] != count
            for status, count in status_counts.items()
        )
        or (
            summary["published"]
            + summary["verified"]
            + summary["locked"]
            != summary["attempted"]
        )
    ):
        raise PassportBenchmarkError(
            "Полный цикл refresh не прошёл фиксированный контракт: "
            f"summary={summary!r}, groups={groups!r}."
        )
    expected = _EXPECTED_REFRESH_COMMAND_LIFECYCLE.get(ordinal)
    actual_groups = {
        item["shift_type"]: (
            item["status"],
            item["revision"],
        )
        for item in groups
    }
    if (
        expected is None
        or actual_groups != expected["groups"]
        or any(
            summary[key] != value
            for key, value in expected["summary"].items()
        )
    ):
        raise PassportBenchmarkError(
            "Полный цикл refresh не совпал с точным жизненным циклом "
            "published/verified и revision=1: "
            f"ordinal={ordinal}, expected={expected!r}, "
            f"summary={summary!r}, groups={groups!r}."
        )
    return {
        "ordinal": ordinal,
        "seconds": metrics["seconds"],
        "query_count": metrics["query_count"],
        "groups": sorted(groups, key=lambda item: item["shift_type"]),
        "summary": summary,
    }


def _validate_refresh_command_cycles(
    cycles: Sequence[Mapping[str, Any]],
) -> None:
    if len(cycles) != len(_EXPECTED_REFRESH_COMMAND_LIFECYCLE):
        raise PassportBenchmarkError(
            "Полный benchmark должен содержать ровно два цикла refresh."
        )

    for expected_ordinal, cycle in enumerate(cycles, start=1):
        expected = _EXPECTED_REFRESH_COMMAND_LIFECYCLE[
            expected_ordinal
        ]["groups"]
        groups = cycle.get("groups", ())
        actual_lifecycle = {
            item.get("shift_type"): (
                item.get("status"),
                item.get("revision"),
            )
            for item in groups
        }
        if (
            cycle.get("ordinal") != expected_ordinal
            or len(groups) != 2
            or actual_lifecycle != expected
        ):
            raise PassportBenchmarkError(
                "Полные циклы refresh не прошли точный контракт "
                "published/verified и revision=1: "
                f"cycle={cycle!r}, "
                f"expected={expected!r}."
            )


def benchmark_refresh_command_cycles(
    scope: SeededScope,
    *,
    scope_code: str,
) -> dict[str, Any]:
    from django.core.management import call_command

    cycles = []
    for ordinal in (1, 2):
        stdout = io.StringIO()
        stderr = io.StringIO()

        def run_command() -> None:
            call_command(
                "refresh_driver_rating_snapshots",
                rating_period=scope.rating_period.pk,
                watch_composition=scope.composition.pk,
                site_code=scope_code,
                strict=True,
                stdout=stdout,
                stderr=stderr,
                no_color=True,
                verbosity=0,
            )

        _, metrics = _measure_call(run_command)
        cycles.append(_parse_refresh_command_cycle(
            ordinal=ordinal,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
            metrics=metrics,
        ))

    _validate_refresh_command_cycles(cycles)
    return {
        "command": "refresh_driver_rating_snapshots",
        "cycle_count": 2,
        "cycles": cycles,
    }


def benchmark_optional_materialized_read(
    scope: SeededScope,
    config: BenchmarkConfig,
) -> dict[str, Any]:
    """Позволить повторить замер после появления materialized-модели.

    Отсутствие модели или подходящей строки не является ошибкой текущего
    standalone benchmark: результат явно сообщает причину пропуска.
    """

    from django.apps import apps
    from django.conf import settings
    from django.db import connection, models
    from django.test.utils import CaptureQueriesContext

    model = None
    model_name = None
    for candidate in (
        "DriverRatingPeriodMaterializedSnapshot",
        "DriverRatingMaterializedSnapshot",
    ):
        try:
            model = apps.get_model("reports", candidate)
        except LookupError:
            model = None
        if model is not None:
            model_name = candidate
            break
    if model is None:
        return {
            "status": "model_unavailable",
            "measured": False,
        }

    fields = {
        field.name: field
        for field in model._meta.get_fields()
        if getattr(field, "concrete", False)
    }
    required = {
        "rating_period",
        "watch_composition",
        "shift_type",
    }
    if not required.issubset(fields):
        return {
            "status": "incompatible_model_contract",
            "measured": False,
            "fields": sorted(fields),
        }
    json_fields = [
        name
        for name, field in fields.items()
        if isinstance(field, models.JSONField)
    ]
    payload_field = next(
        (
            candidate
            for candidate in (
                "payload",
                "rating_payload",
                "result",
            )
            if candidate in json_fields
        ),
        json_fields[0] if json_fields else None,
    )
    if payload_field is None:
        return {
            "status": "json_payload_field_unavailable",
            "measured": False,
            "fields": sorted(fields),
        }

    composition_employee_ids = tuple(
        sorted(
            {
                *scope.day_employee_ids,
                *scope.night_employee_ids,
            }
        )
    )
    filters: dict[str, Any] = {
        "rating_period_id": scope.rating_period.pk,
        "watch_composition_id": scope.composition.pk,
        "shift_type": "day",
    }
    scope_code = str(
        getattr(settings, "PORTAL_SITE_CODE", "") or ""
    ).strip()
    if "scope_code" in fields:
        if not scope_code:
            return {
                "status": "scope_code_unavailable",
                "measured": False,
            }
        filters["scope_code"] = scope_code
    if "scope_fingerprint" in fields:
        try:
            from reports.driver_rating_materialization import (
                driver_rating_scope_fingerprint,
            )
        except (ImportError, AttributeError):
            driver_rating_scope_fingerprint = None
        if driver_rating_scope_fingerprint is not None:
            filters["scope_fingerprint"] = (
                driver_rating_scope_fingerprint(
                    scope_code=scope_code,
                    rating_period=scope.rating_period,
                    watch_composition=scope.composition,
                    shift_type="day",
                    allowed_employee_ids=composition_employee_ids,
                    expected_employee_ids=composition_employee_ids,
                )
            )
        else:
            filters["scope_fingerprint"] = employee_scope_fingerprint(
                scope.day_employee_ids
            )
    if "expected_fingerprint" in fields:
        filters["expected_fingerprint"] = employee_scope_fingerprint(
            scope.day_employee_ids
        )

    refresh_metrics: dict[str, Any] | None = None
    refresh_status: str | None = None
    refresh_command_cycles: dict[str, Any] | None = None
    if model_name == "DriverRatingPeriodMaterializedSnapshot":
        try:
            from reports.driver_rating_materialization import (
                get_materialized_driver_rating_period,
                refresh_driver_rating_group,
            )
        except (ImportError, AttributeError):
            get_materialized_driver_rating_period = None
            refresh_driver_rating_group = None
        if refresh_driver_rating_group is not None:
            refresh_result, refresh_metrics = _measure_call(
                lambda: refresh_driver_rating_group(
                    scope.rating_period,
                    scope.composition,
                    shift_type="day",
                    scope_code=scope_code,
                ),
            )
            refresh_status = str(refresh_result.status)
            refresh_command_cycles = benchmark_refresh_command_cycles(
                scope,
                scope_code=scope_code,
            )
    else:
        get_materialized_driver_rating_period = None

    queryset = model.objects.filter(**filters).order_by("-pk")
    if not queryset.exists():
        return {
            "status": "matching_snapshot_unavailable",
            "measured": False,
            "model": model_name,
            "refresh": refresh_metrics,
            "refresh_status": refresh_status,
            "refresh_command_cycles": refresh_command_cycles,
            "filters": {
                key: str(value)
                for key, value in filters.items()
            },
        }

    reference_snapshot = queryset.first()
    fixed_now = (
        getattr(reference_snapshot, "last_success_at", None)
        or getattr(reference_snapshot, "published_at", None)
    )

    def read_payload() -> Any:
        if get_materialized_driver_rating_period is not None:
            return get_materialized_driver_rating_period(
                scope.rating_period,
                scope.composition,
                shift_type="day",
                allowed_employee_ids=composition_employee_ids,
                expected_employee_ids=composition_employee_ids,
                scope_code=scope_code,
                now=fixed_now,
            )
        return (
            model.objects
            .filter(**filters)
            .order_by("-pk")
            .values_list(payload_field, flat=True)
            .first()
        )

    durations: list[float] = []
    query_count = 0
    payload_sha: str | None = None
    with CaptureQueriesContext(connection) as queries:
        for _ in range(config.materialized_read_repetitions):
            started = time.perf_counter()
            payload = read_payload()
            durations.append(time.perf_counter() - started)
            if get_materialized_driver_rating_period is not None:
                validate_formula_result(
                    payload,
                    expected_employee_ids=scope.day_employee_ids,
                )
            current_sha = uppercase_sha256(payload)
            if payload_sha is None:
                payload_sha = current_sha
            elif current_sha != payload_sha:
                raise PassportBenchmarkError(
                    "Materialized snapshot изменился внутри read benchmark."
                )
        query_count = len(queries)
    return {
        "status": "measured",
        "measured": True,
        "model": model_name,
        "reader": (
            "get_materialized_driver_rating_period"
            if get_materialized_driver_rating_period is not None
            else "raw_orm_json_read"
        ),
        "refresh": refresh_metrics,
        "refresh_status": refresh_status,
        "refresh_command_cycles": refresh_command_cycles,
        "repetitions": config.materialized_read_repetitions,
        "query_count": query_count,
        "payload_field": payload_field,
        "payload_sha256": payload_sha,
        "seconds": {
            "min": round(min(durations), 6),
            "median": round(statistics.median(durations), 6),
            "p95": round(_percentile(durations, 0.95), 6),
            "max": round(max(durations), 6),
        },
    }


def build_manifest(
    *,
    config: BenchmarkConfig,
    database_identity: Mapping[str, Any],
    final_counts: Mapping[str, int],
    integrity: Mapping[str, Any],
    benchmark: Mapping[str, Any],
    materialized_read: Mapping[str, Any],
    duration_seconds: float,
) -> dict[str, Any]:
    import django

    source_paths = (
        Path(__file__).resolve(),
        BACKEND_DIR / 'reports' / 'driver_watch_rating.py',
        BACKEND_DIR / 'reports' / 'driver_rating_materialization.py',
        BACKEND_DIR / 'reports' / 'driver_rating_scope_membership.py',
        BACKEND_DIR / 'reports' / 'models.py',
        (
            BACKEND_DIR
            / 'reports'
            / 'management'
            / 'commands'
            / 'refresh_driver_rating_snapshots.py'
        ),
        (
            BACKEND_DIR
            / 'reports'
            / 'migrations'
            / '0009_driver_rating_period_materialized_snapshot.py'
        ),
    )
    return {
        "schema": BENCHMARK_SCHEMA,
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "data_classification": "synthetic_qa_only",
        "synthetic": True,
        "official": False,
        "official_rating_eligible": False,
        "warning": (
            "Только технический benchmark. Не является реальными "
            "производственными данными, калибровкой KPI или основанием "
            "для премирования."
        ),
        "run_id": BENCHMARK_RUN_ID,
        "runtime": {
            "python": sys.version.split()[0],
            "django": django.get_version(),
        },
        "source_files_sha256": {
            path.relative_to(BACKEND_DIR).as_posix(): (
                hashlib.sha256(path.read_bytes()).hexdigest().upper()
            )
            for path in source_paths
        },
        "config": {
            **asdict(config),
            "start_date": config.start_date.isoformat(),
            "end_date": config.end_date.isoformat(),
            "rating_starts_on": (
                config.rating_starts_on.isoformat()
            ),
            "rating_ends_before": (
                config.rating_ends_before.isoformat()
            ),
            "watch_period_ranges": [
                {
                    "starts_on": starts_on.isoformat(),
                    "ends_on": ends_on.isoformat(),
                }
                for starts_on, ends_on in watch_period_ranges(config)
            ],
            "total_shift_count": config.total_shift_count,
            "total_embedded_trip_count": (
                config.total_embedded_trip_count
            ),
        },
        "database": dict(database_identity),
        "final_counts": dict(final_counts),
        "integrity": dict(integrity),
        "benchmark": dict(benchmark),
        "materialized_read": dict(materialized_read),
        "duration_seconds": round(duration_seconds, 3),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Создать ровно 10 070 synthetic QA-паспортов в новой "
            "изолированной PostgreSQL-БД и измерить формулу рейтинга."
        ),
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_DIR,
        help=(
            "Новый пустой каталог для run_manifest.json. "
            "Существующие файлы не перезаписываются."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BenchmarkConfig.batch_size,
        help="Размер ORM bulk_create batch (1..1000).",
    )
    parser.add_argument(
        "--include-materialized-read",
        action="store_true",
        help=(
            "Если materialized-контур существует, подготовить его строки, "
            "выполнить два полных command-цикла и добавить read benchmark."
        ),
    )
    parser.add_argument(
        "--materialized-read-repetitions",
        type=int,
        default=BenchmarkConfig.materialized_read_repetitions,
    )
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> dict[str, Any]:
    args = parse_args(argv)
    config = BenchmarkConfig(
        batch_size=args.batch_size,
        materialized_read_repetitions=(
            args.materialized_read_repetitions
        ),
    )
    validate_config(config)
    artifact_dir = ensure_new_artifact_directory(args.artifact_dir)
    bootstrap_django()

    started = time.perf_counter()
    prelock_identity = verify_database(require_empty=True)
    with benchmark_advisory_lock():
        database_identity = verify_database(require_empty=True)
        if (
            prelock_identity["configured"]
            != database_identity["configured"]
            or prelock_identity["actual"]
            != database_identity["actual"]
        ):
            raise PassportBenchmarkError(
                "PostgreSQL identity изменилась между защитными "
                "проверками до и внутри advisory lock."
            )
        database_identity["prelock_counts"] = (
            prelock_identity["counts"]
        )
        scope = seed_database(config)
        final_counts = database_counts()
        assert_final_counts(
            final_counts,
            expected_final_counts(config),
        )
        integrity = verify_snapshot_integrity(config, scope)
        benchmark = benchmark_formula(scope, config)
        materialized_read = (
            benchmark_optional_materialized_read(scope, config)
            if args.include_materialized_read
            else {
                "status": "not_requested",
                "measured": False,
            }
        )
        post_benchmark_counts = database_counts()
        expected_post_benchmark_counts = dict(final_counts)
        if materialized_read.get('measured'):
            expected_post_benchmark_counts['materialized_snapshots'] = 2
        if post_benchmark_counts != expected_post_benchmark_counts:
            raise PassportBenchmarkError(
                "Benchmark изменил количество строк вне разрешённого "
                "materialized-снимка: "
                f"expected={expected_post_benchmark_counts!r}, "
                f"after={post_benchmark_counts!r}."
            )
        final_counts = post_benchmark_counts

    manifest = build_manifest(
        config=config,
        database_identity=database_identity,
        final_counts=final_counts,
        integrity=integrity,
        benchmark=benchmark,
        materialized_read=materialized_read,
        duration_seconds=time.perf_counter() - started,
    )
    manifest_path = artifact_dir / "run_manifest.json"
    artifact_sha256 = write_new_json(manifest_path, manifest)
    print(
        "RATING_PASSPORT_BENCHMARK_OK "
        f"passports={TOTAL_SHIFT_COUNT} "
        f"embedded_trips={TOTAL_EMBEDDED_TRIP_COUNT} "
        f"artifact={manifest_path} "
        f"sha256={artifact_sha256}",
        flush=True,
    )
    return {
        "manifest": manifest,
        "manifest_path": str(manifest_path),
        "manifest_sha256": artifact_sha256,
    }


def main() -> int:
    try:
        run()
    except PassportBenchmarkError as exc:
        print(f"RATING_PASSPORT_BENCHMARK_STOP: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
