#!/usr/bin/env python
"""Подготовить точную копию справочников в отдельной недельной QA-БД."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import connection, transaction  # noqa: E402

from assignments.models import (  # noqa: E402
    CrewPlan,
    EquipmentAssignment,
    ExcavatorPlacement,
    HaulAssignment,
)
from downtimes.models import DowntimeEvent, DowntimeReason  # noqa: E402
from references.models import (  # noqa: E402
    Dormitory,
    DormitoryBlock,
    DormitorySection,
    DumpPoint,
    Equipment,
    EquipmentModel,
    EquipmentState,
    EquipmentType,
    RockType,
    TruckCapacityRule,
)
from shifts.models import EmployeeShift, WatchPeriod  # noqa: E402
from trips.models import Trip  # noqa: E402
from users.models import (  # noqa: E402
    Employee,
    PersonnelDepartment,
    PersonnelPosition,
    ProductionSpecialization,
    Role,
    WatchComposition,
    WorkSchedule,
)


QA_DB_NAME = os.environ.get(
    "WEEK_QA_DB_NAME",
    "copper_week_qa_20260727",
)
QA_DB_HOST = os.environ.get("WEEK_QA_DB_HOST", "127.0.0.1")
QA_DB_PORT = os.environ.get("WEEK_QA_DB_PORT", "55432")

ALLOWED_LABELS = {
    "references.equipmenttype",
    "references.equipmentstate",
    "users.role",
    "references.equipmentmodel",
    "references.equipment",
    "references.rocktype",
    "references.dumppoint",
    "references.truckcapacityrule",
    "references.dormitory",
    "references.dormitoryblock",
    "references.dormitorysection",
    "downtimes.downtimereason",
    "users.personneldepartment",
    "users.workschedule",
    "users.productionspecialization",
    "users.personnelposition",
}

MODEL_BY_LABEL = {
    model._meta.label_lower: model
    for model in (
        EquipmentType,
        EquipmentState,
        Role,
        EquipmentModel,
        Equipment,
        RockType,
        DumpPoint,
        TruckCapacityRule,
        Dormitory,
        DormitoryBlock,
        DormitorySection,
        DowntimeReason,
        PersonnelDepartment,
        WorkSchedule,
        ProductionSpecialization,
        PersonnelPosition,
    )
}


class PreparationError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, type=Path)
    return parser.parse_args()


def verify_database_identity() -> None:
    database = settings.DATABASES["default"]
    configured = (
        str(database.get("NAME") or ""),
        str(database.get("HOST") or ""),
        str(database.get("PORT") or ""),
    )
    if configured != (QA_DB_NAME, QA_DB_HOST, QA_DB_PORT):
        raise PreparationError(
            "Разрешена только отдельная QA-БД "
            f"{QA_DB_NAME}@{QA_DB_HOST}:{QA_DB_PORT}; получено {configured}."
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "select current_database(), inet_server_addr()::text, "
            "inet_server_port()::text"
        )
        actual_name, actual_host, actual_port = cursor.fetchone()
    if (
        actual_name != QA_DB_NAME
        or actual_host not in {QA_DB_HOST, "127.0.0.1/32", "::1"}
        or actual_port != QA_DB_PORT
    ):
        raise PreparationError(
            "Фактическое соединение не соответствует локальной QA-БД."
        )


def verify_no_business_data() -> None:
    counts = {
        "employees": Employee.objects.count(),
        "watch_compositions": WatchComposition.objects.count(),
        "watch_periods": WatchPeriod.objects.count(),
        "shifts": EmployeeShift.objects.count(),
        "trips": Trip.objects.count(),
        "downtime_events": DowntimeEvent.objects.count(),
        "crew_plans": CrewPlan.objects.count(),
        "equipment_assignments": EquipmentAssignment.objects.count(),
        "haul_assignments": HaulAssignment.objects.count(),
        "excavator_placements": ExcavatorPlacement.objects.count(),
    }
    if any(counts.values()):
        raise PreparationError(
            f"QA-БД содержит рабочие данные, очистка запрещена: {counts}."
        )


def validate_fixture(path: Path) -> Counter[str]:
    if not path.is_file():
        raise PreparationError(f"Fixture не найден: {path}")
    try:
        rows = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreparationError("Fixture не является корректным JSON.") from error
    if not isinstance(rows, list):
        raise PreparationError("Корень fixture должен быть массивом.")
    counts: Counter[str] = Counter()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise PreparationError(f"Запись fixture #{index} не объект.")
        label = row.get("model")
        if label not in ALLOWED_LABELS:
            raise PreparationError(
                f"Запрещённая модель в fixture #{index}: {label!r}."
            )
        if "pk" not in row or not isinstance(row.get("fields"), dict):
            raise PreparationError(
                f"Некорректная структура fixture #{index}: {label}."
            )
        counts[label] += 1
    if set(counts) != ALLOWED_LABELS:
        raise PreparationError(
            "Fixture не содержит полный разрешённый набор моделей: "
            f"missing={sorted(ALLOWED_LABELS - set(counts))}."
        )
    return counts


def clear_reference_tables() -> None:
    # Порядок строго обратный внешним ключам.
    for model in (
        PersonnelPosition,
        ProductionSpecialization,
        DowntimeReason,
        TruckCapacityRule,
        Equipment,
        DormitorySection,
        DormitoryBlock,
        Dormitory,
        DumpPoint,
        RockType,
        EquipmentModel,
        WorkSchedule,
        PersonnelDepartment,
        Role,
        EquipmentState,
        EquipmentType,
    ):
        model.objects.all().delete()


def main() -> int:
    args = parse_args()
    fixture = args.fixture.resolve()
    expected_counts = validate_fixture(fixture)
    verify_database_identity()
    verify_no_business_data()

    with transaction.atomic():
        clear_reference_tables()
        call_command("loaddata", str(fixture), verbosity=0)
        actual_counts = Counter(
            {
                label: model.objects.count()
                for label, model in MODEL_BY_LABEL.items()
            }
        )
        if actual_counts != expected_counts:
            raise PreparationError(
                "Состав загруженных справочников не совпал с fixture: "
                f"actual={dict(actual_counts)}, "
                f"expected={dict(expected_counts)}."
            )

    print("WEEK_QA_REFERENCE_IMPORT=OK")
    for label in sorted(expected_counts):
        print(f"COUNT {label}={expected_counts[label]}")
    print(f"COUNT TOTAL={sum(expected_counts.values())}")
    print(f"FIXTURE={fixture}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
