#!/usr/bin/env python
"""Сформировать 30-дневные raw-снимки рабочей формулы рейтинга.

Сценарий разрешён только для отдельной пустой локальной PostgreSQL-БД
``copper_rating_30d_qa_20260730`` на ``127.0.0.1:55434``. Он не читает
production и рабочую SQLite, не запускает web-сервер и не преобразует
результаты формулы в TV replay.

Справочники и схема БД должны быть подготовлены заранее. Производственные
действия выполняются теми же Django HTTP-механизмами, что и проверенный
недельный QA-сценарий. После завершения каждой пары дневная/ночная смена
текущая формула вызывается напрямую, а её неизменённый результат немедленно
сохраняется в новый JSON-файл.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.db import connection  # noqa: E402

from assignments.models import (  # noqa: E402
    CrewPlan,
    EquipmentAssignment,
    ExcavatorPlacement,
    HaulAssignment,
    WorkShiftType,
)
from downtimes.models import DowntimeEvent  # noqa: E402
from reports.driver_watch_rating import (  # noqa: E402
    DRIVER_RATING_FORMULA_VERSION,
    build_driver_rating_period,
)
from reports.models import (  # noqa: E402
    DriverShiftPassportCaptureRequest,
    DriverShiftPassportRequestStatus,
    DriverShiftPassportSnapshot,
    RatingPeriod,
)
from shifts.models import EmployeeShift, WatchPeriod  # noqa: E402
from tools.prepare_rating_30d_qa_database import (  # noqa: E402
    TARGET_DB_ENGINE as QA_DB_ENGINE,
    TARGET_DB_HOST as QA_DB_HOST,
    TARGET_DB_NAME as QA_DB_NAME,
    TARGET_DB_PORT as QA_DB_PORT,
    TARGET_DB_USER as QA_DB_USER,
)
from tools.full_week_qa import (  # noqa: E402
    FullWeekRunner,
    QAError,
    ReferenceCatalog,
    ShiftResult,
    WeekOnboarding,
    at_time,
    json_default,
    local_dt,
)
from trips.models import OPEN_TRIP_STATUSES, Trip  # noqa: E402
from users.models import Employee, EmployeeAccess, WatchComposition  # noqa: E402


PERIOD_DAY_COUNT = 30
DAY_BRIGADE = 1
NIGHT_BRIGADE = 3
EXPECTED_DRIVER_COUNT = 53
EXPECTED_EXCAVATOR_COUNT = 8
EXPECTED_FORMULA_SNAPSHOT_COUNT = PERIOD_DAY_COUNT * 2

DEFAULT_START_DATE = date(2026, 6, 14)
DEFAULT_RUN_ID = "QA-RATING-30D-20260730"
DEFAULT_MARKER = "ТЕСТ_РЕЙТИНГ_30Д_20260730"
DEFAULT_ARTIFACT_DIR = Path(
    r"C:\Users\swwba\AppData\Local\Temp\copper-rating-30d-qa-20260730"
)


class Rating30dQAError(QAError):
    """Проверяемая fail-closed ошибка 30-дневного сценария."""


@dataclass(frozen=True)
class Rating30dConfig:
    run_id: str = DEFAULT_RUN_ID
    marker: str = DEFAULT_MARKER
    start_date: date = DEFAULT_START_DATE
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR
    expected_trucks: int = EXPECTED_DRIVER_COUNT
    expected_excavators: int = EXPECTED_EXCAVATOR_COUNT
    day_count: int = PERIOD_DAY_COUNT

    @property
    def end_date(self) -> date:
        """Последний включённый производственный день."""

        return self.start_date + timedelta(days=self.day_count - 1)

    @property
    def ends_before(self) -> date:
        """Исключающая верхняя граница RatingPeriod."""

        return self.start_date + timedelta(days=self.day_count)

    @property
    def total_shift_count(self) -> int:
        return self.day_count * 2


@dataclass(frozen=True)
class Rating30dScope:
    composition: WatchComposition
    watch_period: WatchPeriod
    rating_period: RatingPeriod
    day_employee_ids: tuple[int, ...]
    night_employee_ids: tuple[int, ...]


def configured_database_identity(database: dict[str, Any]) -> tuple[str, ...]:
    """Вернуть проверяемую идентичность Django database settings."""

    return (
        str(database.get("ENGINE") or ""),
        str(database.get("NAME") or ""),
        str(database.get("USER") or ""),
        str(database.get("HOST") or ""),
        str(database.get("PORT") or ""),
    )


def validate_configured_database(database: dict[str, Any]) -> None:
    """Отклонить SQLite, production и любую нецелевую PostgreSQL-БД."""

    identity = configured_database_identity(database)
    expected = (
        QA_DB_ENGINE,
        QA_DB_NAME,
        QA_DB_USER,
        QA_DB_HOST,
        QA_DB_PORT,
    )
    if identity != expected:
        raise Rating30dQAError(
            "Защитная остановка: генератор разрешён только для отдельной "
            f"PostgreSQL-БД {QA_DB_NAME}@{QA_DB_HOST}:{QA_DB_PORT}; "
            f"роль={QA_DB_USER}; получено identity={identity!r}."
        )


def period_calendar(config: Rating30dConfig) -> dict[str, date]:
    """Единый календарный контракт WatchPeriod и RatingPeriod."""

    if config.day_count != PERIOD_DAY_COUNT:
        raise Rating30dQAError(
            f"Сценарий должен содержать ровно {PERIOD_DAY_COUNT} дней."
        )
    return {
        "starts_on": config.start_date,
        "watch_ends_on": config.end_date,
        "rating_ends_before": config.ends_before,
    }


def ensure_new_artifact_directory(path: Path) -> Path:
    """Не позволить повторному запуску перезаписать доказательства."""

    resolved = path.resolve()
    if resolved.exists() and any(resolved.iterdir()):
        raise Rating30dQAError(
            "Каталог артефактов уже содержит файлы; перезапись запрещена: "
            f"{resolved}"
        )
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=json_default,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def write_new_json(path: Path, payload: Any) -> str:
    """Атомарность не нужна, но существующий файл никогда не перезаписываем."""

    encoded = json_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(encoded)
    return hashlib.sha256(encoded).hexdigest().upper()


def verify_rating_30d_database(*, require_empty: bool) -> dict[str, Any]:
    """Проверить и настройки, и фактическое PostgreSQL-соединение."""

    database = settings.DATABASES["default"]
    validate_configured_database(database)
    with connection.cursor() as cursor:
        cursor.execute(
            "select current_database(), inet_server_addr()::text, "
            "inet_server_port()::text, current_user"
        )
        actual_name, actual_host, actual_port, actual_user = cursor.fetchone()
    actual = {
        "name": str(actual_name or ""),
        "host": str(actual_host or ""),
        "port": str(actual_port or ""),
        "user": str(actual_user or ""),
    }
    if (
        actual["name"] != QA_DB_NAME
        or actual["host"] not in {QA_DB_HOST, "127.0.0.1/32", "::1"}
        or actual["port"] != QA_DB_PORT
        or actual["user"] != QA_DB_USER
    ):
        raise Rating30dQAError(
            "Фактическое соединение не соответствует целевой локальной "
            f"QA-БД: {actual!r}."
        )

    business_counts = {
        "employees": Employee.objects.count(),
        "employee_shifts": EmployeeShift.objects.count(),
        "trips": Trip.objects.count(),
        "downtime_events": DowntimeEvent.objects.count(),
        "crew_plans": CrewPlan.objects.count(),
        "equipment_assignments": EquipmentAssignment.objects.count(),
        "haul_assignments": HaulAssignment.objects.count(),
        "excavator_placements": ExcavatorPlacement.objects.count(),
        "watch_compositions": WatchComposition.objects.count(),
        "watch_periods": WatchPeriod.objects.count(),
        "rating_periods": RatingPeriod.objects.count(),
        "passport_requests": DriverShiftPassportCaptureRequest.objects.count(),
        "passport_snapshots": DriverShiftPassportSnapshot.objects.count(),
    }
    if require_empty and any(business_counts.values()):
        raise Rating30dQAError(
            "Целевая QA-БД уже содержит рабочие данные; очистка и повторное "
            f"использование запрещены: {business_counts!r}."
        )
    return {
        "configured": {
            "engine": str(database.get("ENGINE") or ""),
            "name": QA_DB_NAME,
            "user": QA_DB_USER,
            "host": QA_DB_HOST,
            "port": QA_DB_PORT,
        },
        "actual": actual,
        "business_counts": business_counts,
    }


class Rating30dOnboarding(WeekOnboarding):
    """Минимальный штат: только фиксированные дневная и ночная группы."""

    BRIGADES = (DAY_BRIGADE, NIGHT_BRIGADE)

    def _personnel_number(
        self,
        role_code: str,
        *,
        brigade: int | None,
        ordinal: int,
    ) -> str:
        role_token = {
            "oup": "OUP",
            "deputy_mining_manager": "DEP",
            "dispatcher": "DSP",
            "mining_master": "MM",
            "excavator_operator": "EO",
            "driver": "DRV",
            "manager": "MGR",
        }[role_code]
        brigade_token = f"B{brigade}" if brigade is not None else "BI"
        return f"QAR30D-20260730-{role_token}-{brigade_token}-{ordinal:03d}"

    def run(self) -> "Rating30dOnboarding":
        onboarding_time = local_dt(self.config.start_date, 6, 0)
        self.bootstrap_admin(onboarding_time)
        self.create_oup(onboarding_time + timedelta(minutes=1))
        self.start_oup_period(onboarding_time + timedelta(minutes=2))
        self.deputy = self.create_employee(
            role_code="deputy_mining_manager",
            when=onboarding_time + timedelta(minutes=3),
        )

        creation_minute = 4
        for brigade in self.BRIGADES:
            dispatcher = self.create_employee(
                role_code="dispatcher",
                brigade=brigade,
                when=onboarding_time + timedelta(
                    minutes=creation_minute,
                    seconds=brigade,
                ),
            )
            mining_master = self.create_employee(
                role_code="mining_master",
                brigade=brigade,
                when=onboarding_time + timedelta(
                    minutes=creation_minute,
                    seconds=10 + brigade,
                ),
            )
            self.shift_roles_by_brigade["dispatcher"][brigade] = dispatcher
            self.shift_roles_by_brigade["mining_master"][brigade] = mining_master
            creation_minute += 1

        for brigade in self.BRIGADES:
            for index, excavator in enumerate(self.catalog.excavators):
                member = self.create_employee(
                    role_code="excavator_operator",
                    brigade=brigade,
                    equipment=excavator,
                    when=onboarding_time
                    + timedelta(minutes=creation_minute, seconds=index),
                )
                self.operators_by_brigade[brigade][excavator.id] = member
            creation_minute += 1

        for brigade in self.BRIGADES:
            for index, truck in enumerate(self.catalog.trucks):
                member = self.create_employee(
                    role_code="driver",
                    brigade=brigade,
                    equipment=truck,
                    when=onboarding_time
                    + timedelta(minutes=creation_minute, seconds=index),
                )
                self.drivers_by_brigade[brigade][truck.id] = member
            creation_minute += 1

        self.close_oup_period(
            onboarding_time + timedelta(minutes=creation_minute + 1)
        )
        self._verify()
        return self

    def _verify(self) -> None:
        expected_by_role = {
            "admin": 1,
            "oup": 1,
            "deputy_mining_manager": 1,
            "dispatcher": 2,
            "mining_master": 2,
            "excavator_operator": EXPECTED_EXCAVATOR_COUNT * 2,
            "driver": EXPECTED_DRIVER_COUNT * 2,
        }
        actual_by_role = {
            role_code: len(self.by_role.get(role_code, ()))
            for role_code in expected_by_role
        }
        if actual_by_role != expected_by_role:
            raise Rating30dQAError(
                f"Состав тестового штата неверен: {actual_by_role!r}; "
                f"ожидался {expected_by_role!r}."
            )
        expected_total = sum(expected_by_role.values())
        marker_count = Employee.objects.filter(
            full_name__startswith=self.config.marker
        ).count()
        active_access_count = EmployeeAccess.objects.filter(
            employee__full_name__startswith=self.config.marker,
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        ).count()
        if (
            marker_count != expected_total
            or len(self.staff) != expected_total
            or active_access_count != expected_total
        ):
            raise Rating30dQAError(
                "Созданный тестовый штат или его активные доступы неполны: "
                f"employees={marker_count}, in_memory={len(self.staff)}, "
                f"accesses={active_access_count}, expected={expected_total}."
            )


def create_rating_scope(
    config: Rating30dConfig,
    onboarding: Rating30dOnboarding,
) -> Rating30dScope:
    """Создать структурный состав и оба календарных окна до первой смены."""

    calendar = period_calendar(config)
    composition = WatchComposition.objects.create(
        code="qa-rating-30d-20260730",
        name="ТЕСТОВЫЙ СОСТАВ РЕЙТИНГА 30 ДНЕЙ 20260730",
        is_active=True,
    )
    day_employee_ids = tuple(sorted(
        member.employee_id
        for member in onboarding.drivers_by_brigade[DAY_BRIGADE].values()
    ))
    night_employee_ids = tuple(sorted(
        member.employee_id
        for member in onboarding.drivers_by_brigade[NIGHT_BRIGADE].values()
    ))
    if (
        len(day_employee_ids) != EXPECTED_DRIVER_COUNT
        or len(night_employee_ids) != EXPECTED_DRIVER_COUNT
        or set(day_employee_ids) & set(night_employee_ids)
    ):
        raise Rating30dQAError(
            "Дневная и ночная когорты должны содержать по 53 разных Водителя."
        )
    driver_ids = (*day_employee_ids, *night_employee_ids)
    updated = Employee.objects.filter(pk__in=driver_ids).update(
        watch_composition=composition
    )
    if updated != EXPECTED_DRIVER_COUNT * 2:
        raise Rating30dQAError(
            f"К составу привязано {updated} Водителей вместо 106."
        )

    watch_period = WatchPeriod.objects.create(
        name=(
            "ТЕСТОВАЯ ВАХТА РЕЙТИНГА "
            f"{calendar['starts_on']:%d.%m.%Y}–"
            f"{calendar['watch_ends_on']:%d.%m.%Y}"
        ),
        watch_composition=composition,
        starts_on=calendar["starts_on"],
        ends_on=calendar["watch_ends_on"],
        is_active=True,
    )
    rating_period = RatingPeriod.objects.create(
        name=(
            "ТЕСТОВЫЙ ПЕРИОД РЕЙТИНГА "
            f"{calendar['starts_on']:%d.%m.%Y}–"
            f"{calendar['watch_ends_on']:%d.%m.%Y}"
        ),
        starts_on=calendar["starts_on"],
        ends_before=calendar["rating_ends_before"],
        comment=(
            f"{config.marker}: изолированный 30-дневный вызов "
            "текущей рабочей формулы."
        ),
        is_active=True,
    )
    return Rating30dScope(
        composition=composition,
        watch_period=watch_period,
        rating_period=rating_period,
        day_employee_ids=day_employee_ids,
        night_employee_ids=night_employee_ids,
    )


def validate_daily_formula_result(
    result: dict[str, Any],
    *,
    day_number: int,
    shift_type: str,
    expected_employee_ids: tuple[int, ...],
) -> None:
    """Fail closed: raw-результат не дополняется и не исправляется."""

    if day_number not in range(1, PERIOD_DAY_COUNT + 1):
        raise Rating30dQAError(f"Некорректный номер дня: {day_number}.")
    expected_rated_shifts = EXPECTED_DRIVER_COUNT * day_number
    summary = result.get("summary")
    linkage = result.get("linkage_audit")
    entries = result.get("entries")
    failures = []
    if result.get("available") is not True:
        failures.append("available")
    if result.get("official") is not False:
        failures.append("official")
    if result.get("formula_version") != DRIVER_RATING_FORMULA_VERSION:
        failures.append("formula_version")
    if result.get("shift_type") != shift_type:
        failures.append("shift_type")
    if not isinstance(summary, dict):
        failures.append("summary")
        summary = {}
    if not isinstance(linkage, dict) or linkage.get("linkage_ready") is not True:
        failures.append("linkage_ready")
    if not isinstance(entries, list) or len(entries) != EXPECTED_DRIVER_COUNT:
        failures.append("entry_count")
        entries = entries if isinstance(entries, list) else []
    if summary.get("rated_shift_count") != expected_rated_shifts:
        failures.append("rated_shift_count")
    if summary.get("withheld_shift_count") != 0:
        failures.append("withheld_shift_count")
    if summary.get("withheld_reasons") not in ({}, None):
        failures.append("withheld_reasons")

    expected_ids = set(expected_employee_ids)
    actual_ids = {
        entry.get("employee_id")
        for entry in entries
        if isinstance(entry, dict)
    }
    if len(expected_ids) != EXPECTED_DRIVER_COUNT or actual_ids != expected_ids:
        failures.append("employee_cohort")
    if any(
        not isinstance(entry, dict)
        or entry.get("shift_count") != day_number
        for entry in entries
    ):
        failures.append("employee_shift_count")
    if any(
        not isinstance(entry, dict)
        or not str(entry.get("full_name") or "").startswith("ТЕСТ_")
        for entry in entries
    ):
        failures.append("synthetic_marker")

    if failures:
        raise Rating30dQAError(
            f"Формула удержана или нарушила контракт дня {day_number} "
            f"({shift_type}): {', '.join(sorted(set(failures)))}; "
            f"status={result.get('status')!r}, summary={summary!r}, "
            f"linkage={linkage!r}."
        )


class Rating30dRunner(FullWeekRunner):
    """30 дней поверх штатных HTTP-шагов FullWeekRunner."""

    def __init__(
        self,
        config: Rating30dConfig,
        catalog: ReferenceCatalog,
        onboarding: Rating30dOnboarding,
        scope: Rating30dScope,
    ):
        super().__init__(config, catalog, onboarding)
        self.scope = scope
        self.formula_artifacts: list[dict[str, Any]] = []

    @staticmethod
    def brigade_for_shift(shift_index: int) -> int:
        return (
            DAY_BRIGADE
            if (
                FullWeekRunner.shift_type_for_index(shift_index)
                == WorkShiftType.SHIFT_1
            )
            else NIGHT_BRIGADE
        )

    def publish_daily_plans(self, day_index: int):
        production_date = self.config.start_date + timedelta(days=day_index)
        when = local_dt(production_date, 7, 1)
        driver_plan = self._plan_one_role(
            production_date=production_date,
            role_code="driver",
            day_brigade=DAY_BRIGADE,
            night_brigade=NIGHT_BRIGADE,
            when=when,
        )
        operator_plan = self._plan_one_role(
            production_date=production_date,
            role_code="excavator_operator",
            day_brigade=DAY_BRIGADE,
            night_brigade=NIGHT_BRIGADE,
            when=when,
        )
        return driver_plan, operator_plan

    def _capture_formula(
        self,
        *,
        day_number: int,
        shift_type: str,
        expected_employee_ids: tuple[int, ...],
        as_of: datetime,
    ) -> None:
        with at_time(as_of):
            result = build_driver_rating_period(
                self.scope.rating_period,
                self.scope.composition,
                shift_type=shift_type,
                allowed_employee_ids=expected_employee_ids,
                expected_employee_ids=expected_employee_ids,
            )
        validate_daily_formula_result(
            result,
            day_number=day_number,
            shift_type=shift_type,
            expected_employee_ids=expected_employee_ids,
        )
        relative_path = (
            Path("raw_formula")
            / shift_type
            / f"day_{day_number:02d}.json"
        )
        absolute_path = self.config.artifact_dir / relative_path
        sha256 = write_new_json(absolute_path, result)
        self.formula_artifacts.append({
            "day": day_number,
            "shift_type": shift_type,
            "path": relative_path.as_posix(),
            "sha256": sha256,
            "generated_at": result["generated_at"],
            "source_fingerprint": result["source_fingerprint"],
            "shift_score_fingerprint": result["shift_score_fingerprint"],
            "employee_count": len(result["entries"]),
            "rated_shift_count": result["summary"]["rated_shift_count"],
            "withheld_shift_count": result["summary"]["withheld_shift_count"],
        })

    def capture_daily_formulas(
        self,
        *,
        day_index: int,
        as_of: datetime,
    ) -> None:
        day_number = day_index + 1
        self._capture_formula(
            day_number=day_number,
            shift_type=WorkShiftType.SHIFT_1,
            expected_employee_ids=self.scope.day_employee_ids,
            as_of=as_of,
        )
        self._capture_formula(
            day_number=day_number,
            shift_type=WorkShiftType.SHIFT_2,
            expected_employee_ids=self.scope.night_employee_ids,
            as_of=as_of,
        )
        print(
            "RATING_DAY_OK "
            f"{day_number:02d}/{self.config.day_count} "
            f"day_rated={EXPECTED_DRIVER_COUNT * day_number} "
            f"night_rated={EXPECTED_DRIVER_COUNT * day_number}",
            flush=True,
        )

    def run(self) -> list[ShiftResult]:
        run_started = time.perf_counter()
        for day_index in range(self.config.day_count):
            self.publish_daily_plans(day_index)
            night_close_time = None
            for shift_in_day in range(2):
                shift_index = day_index * 2 + shift_in_day
                shift_started = time.perf_counter()
                open_time, close_time = self.shift_bounds(shift_index)
                night_close_time = close_time
                carry_in_trip_id = self.carryover_trip_id
                dispatcher, mining_master = self.open_shift_roles(
                    shift_index=shift_index,
                    open_time=open_time,
                )
                driver_shifts, operator_shifts = self.open_equipment_shifts(
                    shift_index=shift_index,
                    open_time=open_time,
                )
                self.close_transferred_downtimes(
                    shift_index=shift_index,
                    when=open_time + timedelta(minutes=2),
                )
                if shift_index == 0:
                    self.establish_initial_complexes(
                        shift_index=shift_index,
                        dispatcher=dispatcher,
                        mining_master=mining_master,
                        when=open_time + timedelta(minutes=3),
                    )
                else:
                    self.rotate_daily_complexes(
                        shift_index=shift_index,
                        mining_master=mining_master,
                        when=open_time + timedelta(minutes=3),
                    )
                contexts = self.apply_excavator_settings(
                    shift_index=shift_index,
                    when=open_time + timedelta(minutes=5),
                )
                if shift_index == 0:
                    self.probe_missing_capacity_rule(
                        shift_index=shift_index,
                        when=open_time + timedelta(minutes=10),
                        contexts=contexts,
                    )
                carry_out_trip_id, carry_out_truck_id = self.execute_trip_cycle(
                    shift_index=shift_index,
                    open_time=open_time,
                    close_time=close_time,
                    contexts=contexts,
                    driver_shifts=driver_shifts,
                    operator_shifts=operator_shifts,
                )
                excluded_ids = self.start_handoff_downtime(
                    shift_index=shift_index,
                    when=close_time - timedelta(minutes=10),
                    carry_out_truck_id=carry_out_truck_id,
                )
                self.close_equipment_shifts(
                    shift_index=shift_index,
                    close_time=close_time,
                    driver_shifts=driver_shifts,
                    operator_shifts=operator_shifts,
                    carry_out_truck_id=carry_out_truck_id,
                )
                self.record_maintenance_handoff_check(
                    shift_index=shift_index,
                    excluded_equipment_ids=excluded_ids,
                    checked_at=close_time + timedelta(seconds=40),
                )
                self.close_shift_roles(
                    shift_index=shift_index,
                    close_time=close_time,
                    dispatcher=dispatcher,
                    mining_master=mining_master,
                )
                self.verify_shift(
                    shift_index=shift_index,
                    driver_shifts=driver_shifts,
                    operator_shifts=operator_shifts,
                    carry_in_trip_id=carry_in_trip_id,
                    carry_out_trip_id=carry_out_trip_id,
                    started_at=shift_started,
                )
                self.carryover_trip_id = carry_out_trip_id
                self.carryover_truck_id = carry_out_truck_id
                result = self.shift_results[-1]
                print(
                    "SHIFT_OK "
                    f"{shift_index + 1:02d}/{self.config.total_shift_count} "
                    f"{result.production_date} {result.shift_type} "
                    f"trips={result.unloaded_trips} "
                    f"range={min(result.trip_counts_by_truck.values())}-"
                    f"{max(result.trip_counts_by_truck.values())} "
                    f"seconds={result.duration_seconds}",
                    flush=True,
                )

            assert night_close_time is not None
            self.capture_daily_formulas(
                day_index=day_index,
                as_of=night_close_time + timedelta(minutes=1),
            )

        if self.carryover_trip_id or self.carryover_truck_id:
            raise Rating30dQAError(
                "После последней смены остался переходящий рейс."
            )
        if len(self.formula_artifacts) != EXPECTED_FORMULA_SNAPSHOT_COUNT:
            raise Rating30dQAError(
                f"Сохранено raw-снимков формулы: "
                f"{len(self.formula_artifacts)}, ожидалось "
                f"{EXPECTED_FORMULA_SNAPSHOT_COUNT}."
            )
        summary = {
            "run_id": self.config.run_id,
            "start_date": self.config.start_date,
            "end_date": self.config.end_date,
            "shift_count": len(self.shift_results),
            "shifts": [asdict(item) for item in self.shift_results],
            "total_loaded_trips": sum(
                item.loaded_trips for item in self.shift_results
            ),
            "total_unloaded_trips": sum(
                item.unloaded_trips for item in self.shift_results
            ),
            "formula_snapshot_count": len(self.formula_artifacts),
            "duration_seconds": round(time.perf_counter() - run_started, 3),
        }
        write_new_json(
            self.config.artifact_dir / "generation_summary.json",
            summary,
        )
        return self.shift_results


def verify_final_state(
    config: Rating30dConfig,
    scope: Rating30dScope,
    runner: Rating30dRunner,
) -> dict[str, Any]:
    expected_driver_shifts = (
        config.day_count * EXPECTED_DRIVER_COUNT * 2
    )
    expected_operator_shifts = (
        config.day_count * EXPECTED_EXCAVATOR_COUNT * 2
    )
    driver_shifts = EmployeeShift.objects.filter(
        workplace_code="driver",
        employee_id__in=(
            *scope.day_employee_ids,
            *scope.night_employee_ids,
        ),
    )
    operator_shifts = EmployeeShift.objects.filter(
        workplace_code="excavator_operator",
    )
    completed_passport_requests = (
        DriverShiftPassportCaptureRequest.objects.filter(
            status=DriverShiftPassportRequestStatus.COMPLETED,
            shift__in=driver_shifts,
        ).count()
    )
    counts = {
        "driver_shifts": driver_shifts.count(),
        "operator_shifts": operator_shifts.count(),
        "linked_driver_shifts": driver_shifts.filter(
            watch_period=scope.watch_period,
        ).count(),
        "closed_driver_shifts": driver_shifts.filter(
            closed_at__isnull=False,
        ).count(),
        "closed_operator_shifts": operator_shifts.filter(
            closed_at__isnull=False,
        ).count(),
        "passport_snapshots": DriverShiftPassportSnapshot.objects.filter(
            shift__in=driver_shifts,
        ).count(),
        "completed_passport_requests": completed_passport_requests,
        "open_shifts": EmployeeShift.objects.filter(
            closed_at__isnull=True,
        ).count(),
        "open_trips": Trip.objects.filter(
            status__in=OPEN_TRIP_STATUSES,
        ).count(),
        "completed_trips": Trip.objects.filter(status="completed").count(),
        "formula_snapshots": len(runner.formula_artifacts),
    }
    expected = {
        "driver_shifts": expected_driver_shifts,
        "operator_shifts": expected_operator_shifts,
        "linked_driver_shifts": expected_driver_shifts,
        "closed_driver_shifts": expected_driver_shifts,
        "closed_operator_shifts": expected_operator_shifts,
        "passport_snapshots": expected_driver_shifts,
        "completed_passport_requests": expected_driver_shifts,
        "open_shifts": 0,
        "open_trips": 0,
        "formula_snapshots": EXPECTED_FORMULA_SNAPSHOT_COUNT,
    }
    failures = {
        key: {"actual": counts[key], "expected": value}
        for key, value in expected.items()
        if counts[key] != value
    }
    if failures:
        raise Rating30dQAError(
            f"Финальная целостность 30-дневного прогона нарушена: {failures!r}."
        )
    return {"counts": counts, "expected": expected}


def build_run_manifest(
    config: Rating30dConfig,
    database_identity: dict[str, Any],
    catalog: ReferenceCatalog,
    onboarding: Rating30dOnboarding,
    scope: Rating30dScope,
    runner: Rating30dRunner,
    final_state: dict[str, Any],
    *,
    duration_seconds: float,
) -> dict[str, Any]:
    return {
        "schema": "copper.driver-rating-30d-qa-run",
        "schema_version": 1,
        "data_classification": "synthetic_qa_only",
        "synthetic": True,
        "official": False,
        "official_rating_eligible": False,
        "warning": (
            "Синтетический технический прогон текущей рабочей формулы. "
            "Не является калибровкой KPI и не используется для премирования."
        ),
        "run": {
            "id": config.run_id,
            "marker": config.marker,
            "day_count": config.day_count,
            "start_date": config.start_date,
            "end_date": config.end_date,
            "ends_before": config.ends_before,
            "day_brigade": DAY_BRIGADE,
            "night_brigade": NIGHT_BRIGADE,
            "formula_version": DRIVER_RATING_FORMULA_VERSION,
            "formula_call_mode": "direct_after_each_completed_day",
            "duration_seconds": round(duration_seconds, 3),
        },
        "database": database_identity,
        "references": {
            "truck_count": len(catalog.trucks),
            "excavator_count": len(catalog.excavators),
            "rock_count": len(catalog.rocks),
            "dump_point_count": len(catalog.dump_points),
        },
        "staff": {
            role_code: len(members)
            for role_code, members in onboarding.by_role.items()
        },
        "scope": {
            "watch_composition": {
                "id": scope.composition.id,
                "code": scope.composition.code,
                "name": scope.composition.name,
            },
            "watch_period": {
                "id": scope.watch_period.id,
                "starts_on": scope.watch_period.starts_on,
                "ends_on": scope.watch_period.ends_on,
            },
            "rating_period": {
                "id": scope.rating_period.id,
                "starts_on": scope.rating_period.starts_on,
                "ends_before": scope.rating_period.ends_before,
            },
            "day_employee_count": len(scope.day_employee_ids),
            "night_employee_count": len(scope.night_employee_ids),
        },
        "generation": {
            "shift_count": len(runner.shift_results),
            "loaded_trip_count": sum(
                item.loaded_trips for item in runner.shift_results
            ),
            "unloaded_trip_count": sum(
                item.unloaded_trips for item in runner.shift_results
            ),
        },
        "formula_artifacts": runner.formula_artifacts,
        "final_state": final_state,
        "replay_conversion": {
            "performed": False,
            "reason": (
                "Raw-результаты сохраняются отдельно; преобразование в "
                "formula replay выполняет отдельный проверяемый контракт."
            ),
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Сформировать 30 дней дневной/ночной группы и сохранить 60 "
            "raw-вызовов текущей рабочей формулы рейтинга."
        )
    )
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=DEFAULT_START_DATE,
    )
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--marker", default=DEFAULT_MARKER)
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_DIR,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = Rating30dConfig(
        run_id=args.run_id,
        marker=args.marker,
        start_date=args.start_date,
        artifact_dir=args.artifact_dir.resolve(),
    )
    started_at = time.perf_counter()
    artifact_directory_ready = False
    try:
        # Не создаём даже локальный failure-артефакт, если процесс по ошибке
        # направлен на SQLite, production или соседнюю QA-БД.
        validate_configured_database(settings.DATABASES["default"])
        config = Rating30dConfig(
            run_id=config.run_id,
            marker=config.marker,
            start_date=config.start_date,
            artifact_dir=ensure_new_artifact_directory(
                config.artifact_dir
            ),
        )
        artifact_directory_ready = True
        database_identity = verify_rating_30d_database(require_empty=True)
        period_calendar(config)
        catalog = ReferenceCatalog(config)
        onboarding = Rating30dOnboarding(config, catalog).run()
        scope = create_rating_scope(config, onboarding)
        runner = Rating30dRunner(
            config,
            catalog,
            onboarding,
            scope,
        )
        runner.run()
        final_state = verify_final_state(
            config,
            scope,
            runner,
        )
        manifest = build_run_manifest(
            config,
            database_identity,
            catalog,
            onboarding,
            scope,
            runner,
            final_state,
            duration_seconds=time.perf_counter() - started_at,
        )
        manifest_sha256 = write_new_json(
            config.artifact_dir / "run_manifest.json",
            manifest,
        )
        print(
            "DRIVER_RATING_30D_QA_COMPLETE "
            f"days={config.day_count} "
            f"formula_snapshots={len(runner.formula_artifacts)} "
            f"trips={manifest['generation']['unloaded_trip_count']} "
            f"manifest_sha256={manifest_sha256}",
            flush=True,
        )
        return 0
    except Exception as error:
        failure = {
            "run_id": config.run_id,
            "data_classification": "synthetic_qa_only",
            "error_type": type(error).__name__,
            "error": str(error),
            "traceback": traceback.format_exc(),
            "duration_seconds": round(time.perf_counter() - started_at, 3),
        }
        if artifact_directory_ready:
            failure_path = config.artifact_dir / "failure.json"
            if not failure_path.exists():
                write_new_json(failure_path, failure)
        print(
            "DRIVER_RATING_30D_QA_FAILED "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
