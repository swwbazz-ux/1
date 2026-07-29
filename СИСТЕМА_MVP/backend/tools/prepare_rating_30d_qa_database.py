#!/usr/bin/env python
"""Подготовить справочники в отдельной 30-дневной PostgreSQL QA-БД.

Скрипт намеренно привязан к одному локальному имени БД, роли и порту.
Он не очищает непустую БД: перед заменой разрешённых справочников
проверяются все рабочие, вахтовые, рейтинговые и snapshot-таблицы.
"""

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
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django  # noqa: E402

django.setup()

from django.apps import apps  # noqa: E402
from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import connection, transaction  # noqa: E402

from downtimes.models import DowntimeReason  # noqa: E402
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
from users.models import (  # noqa: E402
    PersonnelDepartment,
    PersonnelPosition,
    ProductionSpecialization,
    Role,
    WorkSchedule,
)


TARGET_DB_ENGINE = 'django.db.backends.postgresql'
TARGET_DB_NAME = 'copper_rating_30d_qa_20260730'
TARGET_DB_USER = 'copper_rating30_qa_runner'
TARGET_DB_HOST = '127.0.0.1'
TARGET_DB_PORT = '55434'

ALLOWED_REFERENCE_LABELS = frozenset({
    'references.equipmenttype',
    'references.equipmentstate',
    'users.role',
    'references.equipmentmodel',
    'references.equipment',
    'references.rocktype',
    'references.dumppoint',
    'references.truckcapacityrule',
    'references.dormitory',
    'references.dormitoryblock',
    'references.dormitorysection',
    'downtimes.downtimereason',
    'users.personneldepartment',
    'users.workschedule',
    'users.productionspecialization',
    'users.personnelposition',
})

# Эти локальные приложения содержат изменяемые производственные,
# кадровые, вахтовые, рейтинговые или аудиторские записи. Любая строка
# в их неразрешённой модели запрещает замену справочников.
PROTECTED_APP_LABELS = frozenset({
    'assignments',
    'core',
    'downtimes',
    'portal',
    'references',
    'reports',
    'rotations',
    'shifts',
    'trips',
    'users',
})

# Миграции создают эти справочные группы ещё до загрузки оборудования.
# Они не являются фактом 30-дневного прогона и не очищаются скриптом.
MIGRATION_CONFIGURATION_LABELS = frozenset({
    'shifts.equipmentplangroup',
})

REFERENCE_MODELS_BY_LABEL = {
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
    """Fail-closed остановка подготовки QA-БД."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            'Загрузить только разрешённые справочники в отдельную '
            '30-дневную PostgreSQL QA-БД.'
        ),
    )
    parser.add_argument('--fixture', required=True, type=Path)
    return parser.parse_args(argv)


def configured_database_identity(database: dict) -> tuple[str, ...]:
    return (
        str(database.get('ENGINE') or ''),
        str(database.get('NAME') or ''),
        str(database.get('USER') or ''),
        str(database.get('HOST') or ''),
        str(database.get('PORT') or ''),
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
            'Разрешена только отдельная QA-БД '
            f'{TARGET_DB_NAME}@{TARGET_DB_HOST}:{TARGET_DB_PORT} '
            f'с ролью {TARGET_DB_USER}; получено {configured}.'
        )


def verify_database_identity() -> None:
    validate_configured_database_identity(settings.DATABASES['default'])
    with connection.cursor() as cursor:
        cursor.execute(
            'select current_database(), inet_server_addr()::text, '
            'inet_server_port()::text, current_user'
        )
        actual_name, actual_host, actual_port, actual_user = cursor.fetchone()
    actual = (
        str(actual_name or ''),
        str(actual_host or ''),
        str(actual_port or ''),
        str(actual_user or ''),
    )
    expected = (
        TARGET_DB_NAME,
        TARGET_DB_HOST,
        TARGET_DB_PORT,
        TARGET_DB_USER,
    )
    if (
        actual[0] != TARGET_DB_NAME
        or actual[1] not in {
            TARGET_DB_HOST,
            f'{TARGET_DB_HOST}/32',
        }
        or actual[2] != TARGET_DB_PORT
        or actual[3] != TARGET_DB_USER
    ):
        raise PreparationError(
            'Фактическое соединение не соответствует отдельной локальной '
            f'30-дневной QA-БД: ожидалось {expected}, получено {actual}.'
        )


def protected_business_models():
    protected = []
    for model in apps.get_models():
        label = model._meta.label_lower
        if model._meta.app_label not in PROTECTED_APP_LABELS:
            continue
        if label in ALLOWED_REFERENCE_LABELS:
            continue
        if label in MIGRATION_CONFIGURATION_LABELS:
            continue
        if model._meta.proxy or not model._meta.managed:
            continue
        protected.append(model)
    return tuple(
        sorted(protected, key=lambda model: model._meta.label_lower)
    )


def business_table_counts(models=None) -> dict[str, int]:
    selected_models = (
        protected_business_models()
        if models is None
        else tuple(models)
    )
    return {
        model._meta.label_lower: model._default_manager.count()
        for model in selected_models
    }


def verify_no_business_data() -> None:
    counts = business_table_counts()
    nonempty = {
        label: count
        for label, count in counts.items()
        if count
    }
    if nonempty:
        raise PreparationError(
            '30-дневная QA-БД содержит рабочие, вахтовые, рейтинговые '
            'или snapshot-записи; очистка справочников запрещена: '
            f'{nonempty}.'
        )


def lock_preparation_tables() -> None:
    """Не позволить конкурентной записи обойти проверку пустоты."""

    models = {
        model._meta.db_table: model
        for model in apps.get_models()
        if (
            model._meta.app_label in PROTECTED_APP_LABELS
            and model._meta.managed
            and not model._meta.proxy
        )
    }
    table_names = sorted(models)
    quoted_tables = ', '.join(
        connection.ops.quote_name(table_name)
        for table_name in table_names
    )
    if not quoted_tables:
        raise PreparationError(
            'Не найдено ни одной локальной таблицы для защитной блокировки.'
        )
    with connection.cursor() as cursor:
        cursor.execute("set local lock_timeout = '5s'")
        cursor.execute(
            f'lock table {quoted_tables} in access exclusive mode'
        )


def validate_fixture(path: Path) -> Counter[str]:
    if not path.is_file():
        raise PreparationError(f'Fixture не найден: {path}')
    try:
        rows = json.loads(path.read_text(encoding='utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreparationError(
            'Fixture не является корректным JSON.'
        ) from error
    if not isinstance(rows, list):
        raise PreparationError('Корень fixture должен быть массивом.')

    counts: Counter[str] = Counter()
    seen_keys = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise PreparationError(f'Запись fixture #{index} не объект.')
        label = row.get('model')
        if label not in ALLOWED_REFERENCE_LABELS:
            raise PreparationError(
                f'Запрещённая модель в fixture #{index}: {label!r}.'
            )
        pk = row.get('pk')
        if (
            not isinstance(pk, int)
            or isinstance(pk, bool)
            or pk <= 0
            or not isinstance(row.get('fields'), dict)
        ):
            raise PreparationError(
                f'Некорректная структура fixture #{index}: {label}.'
            )
        fixture_key = (label, pk)
        if fixture_key in seen_keys:
            raise PreparationError(
                f'Повторная запись fixture #{index}: {label} pk={pk}.'
            )
        seen_keys.add(fixture_key)
        counts[label] += 1

    if set(counts) != ALLOWED_REFERENCE_LABELS:
        raise PreparationError(
            'Fixture не содержит полный разрешённый набор справочников: '
            f'missing={sorted(ALLOWED_REFERENCE_LABELS - set(counts))}.'
        )
    return counts


def clear_reference_tables() -> None:
    # Порядок строго обратный внешним ключам разрешённого fixture.
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    fixture = args.fixture.expanduser().resolve()
    expected_counts = validate_fixture(fixture)
    verify_database_identity()
    verify_no_business_data()

    with transaction.atomic():
        # Повторяем guard под блокировкой непосредственно перед единственной
        # разрешённой заменой данных.
        lock_preparation_tables()
        verify_no_business_data()
        clear_reference_tables()
        call_command('loaddata', str(fixture), verbosity=0)
        actual_counts = Counter({
            label: model.objects.count()
            for label, model in REFERENCE_MODELS_BY_LABEL.items()
        })
        if actual_counts != expected_counts:
            raise PreparationError(
                'Состав загруженных справочников не совпал с fixture: '
                f'actual={dict(actual_counts)}, '
                f'expected={dict(expected_counts)}.'
            )

    print('RATING_30D_QA_REFERENCE_IMPORT=OK')
    for label in sorted(expected_counts):
        print(f'COUNT {label}={expected_counts[label]}')
    print(f'COUNT TOTAL={sum(expected_counts.values())}')
    print(f'FIXTURE={fixture}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
