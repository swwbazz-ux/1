#!/usr/bin/env python
"""Safely export the reference data needed by the full-week QA scenario.

The resulting file is a standard Django JSON fixture.  The export is limited
to the explicit model allowlist below and never includes employees, access
codes, shifts, trips, sessions, or other operational data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from itertools import chain
from pathlib import Path
from typing import Iterable


ALLOWED_MODEL_LABELS = (
    # Put dependencies before their consumers so that ``loaddata`` can import
    # the fixture on every supported database backend.
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
)

# This denylist is intentionally redundant with the allowlist.  It makes an
# accidental future expansion immediately visible during code review/runtime.
FORBIDDEN_MODEL_LABELS = frozenset(
    {
        "users.employee",
        "users.employeeaccess",
        "users.driverprimaryregistration",
        "users.adminactionlog",
        "users.adminconflict",
        "sessions.session",
        "shifts.employeeshift",
        "shifts.watchperiod",
        "trips.trip",
        "downtimes.downtimeevent",
    }
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Экспортировать только разрешённые справочники в стандартный "
            "Django JSON fixture без изменения базы данных."
        )
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Новый файл .json. Существующий файл никогда не перезаписывается.",
    )
    parser.add_argument(
        "--database",
        default="default",
        help="Django database alias (по умолчанию: default).",
    )
    return parser.parse_args(argv)


def _resolve_models() -> list[type]:
    from django.apps import apps

    models = []
    for label in ALLOWED_MODEL_LABELS:
        app_label, model_name = label.split(".", maxsplit=1)
        model = apps.get_model(app_label, model_name)
        if model is None:
            raise RuntimeError(f"Разрешённая модель не найдена: {label}")
        actual_label = model._meta.label_lower
        if actual_label != label:
            raise RuntimeError(
                f"Модель {label} разрешилась как неожиданный label {actual_label}"
            )
        models.append(model)
    return models


def _ordered_objects(models: Iterable[type], database_alias: str):
    querysets = []
    for model in models:
        primary_key_name = model._meta.pk.name
        querysets.append(
            model._default_manager.using(database_alias).order_by(primary_key_name)
        )
    return chain.from_iterable(querysets)


def _serialize_fixture(database_alias: str) -> bytes:
    from django.core import serializers
    from django.db import connections, transaction

    connection = connections[database_alias]
    models = _resolve_models()

    with transaction.atomic(using=database_alias):
        if connection.vendor == "postgresql":
            # The database itself rejects any accidental write attempted while
            # this script is collecting the consistent export snapshot.
            with connection.cursor() as cursor:
                cursor.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )

        fixture_text = serializers.serialize(
            "json",
            _ordered_objects(models, database_alias),
            indent=2,
            use_natural_foreign_keys=False,
            use_natural_primary_keys=False,
        )

    return (fixture_text.rstrip() + "\n").encode("utf-8")


def _validate_and_count(payload: bytes) -> Counter[str]:
    try:
        records = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Сформирован некорректный JSON fixture") from exc

    if not isinstance(records, list):
        raise RuntimeError("Корень Django fixture должен быть JSON-массивом")

    allowed = frozenset(ALLOWED_MODEL_LABELS)
    counts: Counter[str] = Counter()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise RuntimeError(f"Запись fixture #{index} не является объектом")
        label = record.get("model")
        if label in FORBIDDEN_MODEL_LABELS:
            raise RuntimeError(f"Обнаружена явно запрещённая модель: {label}")
        if label not in allowed:
            raise RuntimeError(f"Обнаружена модель вне allowlist: {label!r}")
        if "pk" not in record or not isinstance(record.get("fields"), dict):
            raise RuntimeError(f"Некорректная структура записи {label} #{index}")
        if label == "users.personnelposition":
            allowed_specializations = record["fields"].get(
                "allowed_specializations"
            )
            if not isinstance(allowed_specializations, list):
                raise RuntimeError(
                    "M2M users.PersonnelPosition.allowed_specializations "
                    "отсутствует в fixture"
                )
        counts[label] += 1

    unexpected = set(counts) - allowed
    if unexpected:
        raise RuntimeError(
            "В fixture появились неожиданные модели: "
            + ", ".join(sorted(unexpected))
        )
    return counts


def _write_new_file(output_path: Path, payload: bytes) -> None:
    if output_path.suffix.lower() != ".json":
        raise RuntimeError("Выходной файл должен иметь расширение .json")

    parent = output_path.parent
    if not parent.exists():
        raise RuntimeError(f"Родительская папка не существует: {parent}")
    if not parent.is_dir():
        raise RuntimeError(f"Родительский путь не является папкой: {parent}")

    # ``xb`` provides an atomic no-overwrite guarantee even if the file is
    # created by another process after the checks above.
    with output_path.open("xb") as fixture_file:
        fixture_file.write(payload)
        fixture_file.flush()
        os.fsync(fixture_file.fileno())


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_path = args.output.expanduser().resolve()

    if output_path.exists():
        print(
            f"ОТКАЗ: выходной путь уже существует: {output_path}",
            file=sys.stderr,
        )
        return 2

    backend_root = Path(__file__).resolve().parent.parent
    backend_root_text = str(backend_root)
    if backend_root_text not in sys.path:
        sys.path.insert(0, backend_root_text)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    try:
        import django

        django.setup()

        from django.db import connections

        if args.database not in connections:
            raise RuntimeError(
                f"Неизвестный Django database alias: {args.database}"
            )

        payload = _serialize_fixture(args.database)
        counts = _validate_and_count(payload)
        _write_new_file(output_path, payload)
    except Exception as exc:
        print(f"ОТКАЗ: {exc}", file=sys.stderr)
        return 1

    digest = hashlib.sha256(payload).hexdigest().upper()
    print("READ_ONLY_REFERENCE_EXPORT=OK")
    print(f"DATABASE_ALIAS={args.database}")
    for label in ALLOWED_MODEL_LABELS:
        print(f"COUNT {label}={counts[label]}")
    print(f"COUNT TOTAL={sum(counts.values())}")
    print(f"OUTPUT={output_path}")
    print(f"SHA256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
