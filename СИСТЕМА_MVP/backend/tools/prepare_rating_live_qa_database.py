#!/usr/bin/env python
"""Подготовить отдельную PostgreSQL-БД ускоренного live-QA рейтинга.

Инструмент разрешён только для одной локальной identity. Сначала он
проверяет configured и фактическое соединение, применяет миграции,
запрещает любые business-строки, а затем под эксклюзивными блокировками
атомарно заменяет только справочники из allowlisted fixture.
"""

from __future__ import annotations

import argparse
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

from core.models import (  # noqa: E402
    OperationalStateEvent,
    OperationalStateVersion,
)
from tools import prepare_rating_30d_qa_database as shared  # noqa: E402


TARGET_DB_ENGINE = "django.db.backends.postgresql"
TARGET_DB_NAME = "copper_rating_live_qa_20260730"
TARGET_DB_USER = "copper_rating_live_qa_runner"
TARGET_DB_HOST = "127.0.0.1"
TARGET_DB_PORT = "55436"

PreparationError = shared.PreparationError
ALLOWED_REFERENCE_LABELS = shared.ALLOWED_REFERENCE_LABELS
MIGRATION_CONFIGURATION_LABELS = shared.MIGRATION_CONFIGURATION_LABELS
REFERENCE_MODELS_BY_LABEL = shared.REFERENCE_MODELS_BY_LABEL
protected_business_models = shared.protected_business_models
business_table_counts = shared.business_table_counts
lock_preparation_tables = shared.lock_preparation_tables
validate_fixture = shared.validate_fixture
clear_reference_tables = shared.clear_reference_tables
KNOWN_FRAMEWORK_TABLES = frozenset({"django_migrations"})
MIGRATION_POPULATED_MODEL_LABELS = frozenset({
    "auth.permission",
    "contenttypes.contenttype",
})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Применить миграции и загрузить allowlisted справочники "
            "в отдельную PostgreSQL-БД ускоренного live-QA рейтинга."
        ),
    )
    parser.add_argument("--fixture", required=True, type=Path)
    return parser.parse_args(argv)


def configured_database_identity(
    database: dict,
) -> tuple[str, ...]:
    return (
        str(database.get("ENGINE") or ""),
        str(database.get("NAME") or ""),
        str(database.get("USER") or ""),
        str(database.get("HOST") or ""),
        str(database.get("PORT") or ""),
    )


def validate_configured_database_identity(database: dict) -> None:
    configured = configured_database_identity(database)
    expected = (
        TARGET_DB_ENGINE,
        TARGET_DB_NAME,
        TARGET_DB_USER,
        TARGET_DB_HOST,
        TARGET_DB_PORT,
    )
    if configured != expected:
        raise PreparationError(
            "Разрешена только отдельная live-QA PostgreSQL-БД "
            f"{TARGET_DB_NAME}@{TARGET_DB_HOST}:{TARGET_DB_PORT} "
            f"с ролью {TARGET_DB_USER}; получено {configured}."
        )


def verify_database_identity() -> dict[str, str]:
    validate_configured_database_identity(
        settings.DATABASES["default"]
    )
    if connection.vendor != "postgresql":
        raise PreparationError(
            "Фактическое соединение live-QA не является PostgreSQL."
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "select current_database(), inet_server_addr()::text, "
            "inet_server_port()::text, current_user"
        )
        actual_name, actual_host, actual_port, actual_user = (
            cursor.fetchone()
        )
    actual = (
        str(actual_name or ""),
        str(actual_host or ""),
        str(actual_port or ""),
        str(actual_user or ""),
    )
    expected = (
        TARGET_DB_NAME,
        TARGET_DB_HOST,
        TARGET_DB_PORT,
        TARGET_DB_USER,
    )
    if (
        actual[0] != expected[0]
        or actual[1] not in {expected[1], f"{expected[1]}/32"}
        or actual[2:] != expected[2:]
    ):
        raise PreparationError(
            "Фактическое соединение не соответствует отдельной "
            f"live-QA PostgreSQL-БД: ожидалось {expected}, "
            f"получено {actual}."
        )
    return {
        "name": actual[0],
        "host": actual[1],
        "port": actual[2],
        "user": actual[3],
    }


def verify_no_business_data() -> dict[str, int]:
    counts = business_table_counts()
    nonempty = {
        label: count
        for label, count in counts.items()
        if count
    }
    if nonempty:
        raise PreparationError(
            "Live-QA БД содержит рабочие, вахтовые, рейтинговые "
            "или snapshot-записи; замена справочников запрещена: "
            f"{nonempty}."
        )
    return counts


def known_schema_tables() -> frozenset[str]:
    managed_tables = {
        model._meta.db_table
        for model in shared.apps.get_models(
            include_auto_created=True,
        )
        if model._meta.managed and not model._meta.proxy
    }
    return frozenset(managed_tables | set(KNOWN_FRAMEWORK_TABLES))


def allowed_nonempty_preflight_tables() -> frozenset[str]:
    allowed_model_labels = (
        ALLOWED_REFERENCE_LABELS
        | MIGRATION_CONFIGURATION_LABELS
        | MIGRATION_POPULATED_MODEL_LABELS
    )
    allowed_models = tuple(
        model
        for model in shared.apps.get_models()
        if model._meta.label_lower in allowed_model_labels
    )
    allowed_model_tables = {
        model._meta.db_table
        for model in allowed_models
    }
    allowed_auto_created_m2m_tables = {
        through._meta.db_table
        for model in allowed_models
        for field in model._meta.local_many_to_many
        for through in (field.remote_field.through,)
        if through._meta.auto_created is model
    }
    return frozenset(
        allowed_model_tables
        | allowed_auto_created_m2m_tables
        | set(KNOWN_FRAMEWORK_TABLES)
    )


def table_row_counts(
    table_names: frozenset[str],
) -> dict[str, int]:
    counts = {}
    with connection.cursor() as cursor:
        for table_name in sorted(table_names):
            quoted_table = connection.ops.quote_name(table_name)
            cursor.execute(f"SELECT COUNT(*) FROM {quoted_table}")
            counts[table_name] = int(cursor.fetchone()[0])
    return counts


def clear_setup_derived_operational_state(
    *,
    event_model=OperationalStateEvent,
    version_model=OperationalStateVersion,
) -> dict[str, int]:
    """Remove only realtime rows derived from this reference import."""

    counts = {
        event_model._meta.label_lower: event_model.objects.count(),
        version_model._meta.label_lower: (
            version_model.objects.count()
        ),
    }
    event_model.objects.all().delete()
    version_model.objects.all().delete()
    return counts


def preflight_existing_schema() -> dict[str, object]:
    existing_tables = frozenset(
        connection.introspection.table_names()
    )
    unexpected_tables = existing_tables - known_schema_tables()
    if unexpected_tables:
        raise PreparationError(
            "В live-QA БД найдены неожиданные таблицы; migrate и "
            f"запись запрещены: {sorted(unexpected_tables)}."
        )
    existing_protected_models = tuple(
        model
        for model in protected_business_models()
        if model._meta.db_table in existing_tables
    )
    protected_tables = frozenset(
        model._meta.db_table
        for model in existing_protected_models
    )
    counts = business_table_counts(existing_protected_models)
    nonempty = {
        label: count
        for label, count in counts.items()
        if count
    }
    if nonempty:
        raise PreparationError(
            "Live-QA БД уже содержит business-строки; migrate и "
            f"запись запрещены: {nonempty}."
        )
    runtime_tables = (
        existing_tables
        - protected_tables
        - allowed_nonempty_preflight_tables()
    )
    runtime_counts = table_row_counts(runtime_tables)
    nonempty_runtime = {
        table_name: count
        for table_name, count in runtime_counts.items()
        if count
    }
    if nonempty_runtime:
        raise PreparationError(
            "Live-QA БД уже содержит пользовательские или runtime-строки "
            "в служебных таблицах; migrate и запись запрещены: "
            f"{nonempty_runtime}."
        )
    return {
        "existing_table_count": len(existing_tables),
        "protected_existing_table_count": len(
            existing_protected_models
        ),
        "business_counts": counts,
        "runtime_counts": runtime_counts,
    }


def apply_migrations() -> None:
    verify_database_identity()
    preflight_existing_schema()
    call_command(
        "migrate",
        interactive=False,
        verbosity=0,
    )
    verify_database_identity()
    preflight_existing_schema()
    verify_no_business_data()


def import_reference_fixture(
    fixture: Path,
    expected_counts: Counter[str],
) -> None:
    preflight_existing_schema()
    verify_no_business_data()
    with transaction.atomic():
        lock_preparation_tables()
        verify_database_identity()
        preflight_existing_schema()
        verify_no_business_data()
        clear_reference_tables()
        call_command("loaddata", str(fixture), verbosity=0)
        actual_counts = Counter({
            label: model.objects.count()
            for label, model in REFERENCE_MODELS_BY_LABEL.items()
        })
        if actual_counts != expected_counts:
            raise PreparationError(
                "Состав загруженных live-QA справочников не совпал "
                f"с fixture: actual={dict(actual_counts)}, "
                f"expected={dict(expected_counts)}."
            )
        clear_setup_derived_operational_state()
        verify_no_business_data()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    fixture = args.fixture.expanduser().resolve()
    expected_counts = validate_fixture(fixture)
    apply_migrations()
    import_reference_fixture(fixture, expected_counts)

    print("RATING_LIVE_QA_REFERENCE_IMPORT=OK")
    for label in sorted(expected_counts):
        print(f"COUNT {label}={expected_counts[label]}")
    print(f"COUNT TOTAL={sum(expected_counts.values())}")
    print(f"FIXTURE={fixture}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
