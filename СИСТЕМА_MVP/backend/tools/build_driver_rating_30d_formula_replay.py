#!/usr/bin/env python
"""Преобразовать завершённый 30-дневный raw-прогон в два formula replay.

Скрипт не подключается к базе и не выполняет формулу повторно. Он читает
только ``run_manifest.json`` и 60 закреплённых raw JSON, проверяет их SHA и
контракт, а затем атомарно публикует новый каталог с дневным и ночным
неизменяемыми replay-артефактами.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django  # noqa: E402

django.setup()

from reports.driver_watch_rating import (  # noqa: E402
    DRIVER_RATING_FORMULA_VERSION,
    DRIVER_RATING_LEVELS,
    DRIVER_RATING_WEIGHTS,
)
from reports.rating_tv_formula_replay import (  # noqa: E402
    RATING_TV_FORMULA_REPLAY_DATA_CLASSIFICATION,
    RATING_TV_FORMULA_REPLAY_DAY_COUNT,
    RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT,
    RATING_TV_FORMULA_REPLAY_MODE,
    RATING_TV_FORMULA_REPLAY_SCHEMA,
    RATING_TV_FORMULA_REPLAY_SCHEMA_VERSION,
    attach_formula_replay_integrity,
    formula_replay_cohort_sha256,
    validate_rating_tv_formula_replay,
)


SOURCE_RUN_SCHEMA = 'copper.driver-rating-30d-qa-run'
SOURCE_RUN_SCHEMA_VERSION = 1
SOURCE_RAW_COUNT = RATING_TV_FORMULA_REPLAY_DAY_COUNT * 2
SOURCE_RAW_MAX_BYTES = 4 * 1024 * 1024
SOURCE_MANIFEST_MAX_BYTES = 2 * 1024 * 1024
SOURCE_DB_ENGINE = 'django.db.backends.postgresql'
SOURCE_DB_NAME = 'copper_rating_30d_qa_20260730'
SOURCE_DB_USER = 'copper_rating30_qa_runner'
SOURCE_DB_HOST = '127.0.0.1'
SOURCE_DB_PORT = '55434'

OUTPUT_NAMES = {
    'day': 'driver_rating_30d_formula_replay_day_v1.json',
    'night': 'driver_rating_30d_formula_replay_night_v1.json',
}
SHIFT_LABELS = {
    'day': 'Дневная',
    'night': 'Ночная',
}

SHA256_RE = re.compile(r'^[0-9A-F]{64}$')
GIT_SHA_RE = re.compile(r'^[0-9a-fA-F]{40}$')
SCORE_4_RE = re.compile(r'^(?:0|[1-9][0-9]{0,2})\.[0-9]{4}$')
NONNEGATIVE_2_RE = re.compile(r'^(?:0|[1-9][0-9]*)\.[0-9]{2}$')

MANIFEST_KEYS = {
    'schema',
    'schema_version',
    'data_classification',
    'synthetic',
    'official',
    'official_rating_eligible',
    'warning',
    'run',
    'database',
    'references',
    'staff',
    'scope',
    'generation',
    'formula_artifacts',
    'final_state',
    'replay_conversion',
}
RUN_KEYS = {
    'id',
    'marker',
    'day_count',
    'start_date',
    'end_date',
    'ends_before',
    'day_brigade',
    'night_brigade',
    'formula_version',
    'formula_call_mode',
    'duration_seconds',
}
DATABASE_KEYS = {'configured', 'actual', 'business_counts'}
CONFIGURED_DATABASE_KEYS = {'engine', 'name', 'user', 'host', 'port'}
ACTUAL_DATABASE_KEYS = {'name', 'host', 'port', 'user'}
SCOPE_KEYS = {
    'watch_composition',
    'watch_period',
    'rating_period',
    'day_employee_count',
    'night_employee_count',
}
MANIFEST_COMPOSITION_KEYS = {'id', 'code', 'name'}
MANIFEST_WATCH_KEYS = {'id', 'starts_on', 'ends_on'}
MANIFEST_PERIOD_KEYS = {'id', 'starts_on', 'ends_before'}
FORMULA_ARTIFACT_KEYS = {
    'day',
    'shift_type',
    'path',
    'sha256',
    'generated_at',
    'source_fingerprint',
    'shift_score_fingerprint',
    'employee_count',
    'rated_shift_count',
    'withheld_shift_count',
}
REPLAY_CONVERSION_KEYS = {'performed', 'reason'}
RAW_RESULT_KEYS = {
    'available',
    'official',
    'rating_mode',
    'scope_type',
    'formula_version',
    'formula_label',
    'status',
    'generated_at',
    'source_fingerprint',
    'shift_score_fingerprint',
    'rating_period',
    'watch_composition',
    'shift_type',
    'shift_type_label',
    'weights',
    'distance_metrics',
    'linkage_audit',
    'summary',
    'entries',
}
RAW_PERIOD_KEYS = {'id', 'name', 'starts_on', 'ends_before'}
RAW_COMPOSITION_KEYS = {'id', 'code', 'name'}
RAW_SUMMARY_KEYS = {
    'employee_count',
    'rated_shift_count',
    'withheld_shift_count',
    'withheld_reasons',
    'trip_count',
    'volume_m3',
    'tonnage_t',
}
RAW_ENTRY_KEYS = {
    'employee_id',
    'full_name',
    'equipment',
    'shift_count',
    'trip_count',
    'volume_m3',
    'tonnage_t',
    'score',
    'blocks',
    'confidence',
    'source_shift_ids',
    'place',
    'shared_score_place',
    'display_order',
    'level',
}
LINKAGE_AUDIT_KEYS = {
    'candidate_closed_shift_count',
    'linked_to_selected_composition_count',
    'unlinked_shift_count',
    'linked_to_other_composition_count',
    'selected_watch_date_mismatch_count',
    'covered_watch_period_count',
    'linkage_ready',
}
DISTANCE_METRIC_KEYS = {'weight', 'status', 'label'}


class FormulaReplayBuildError(ValueError):
    """Raw-прогон нельзя безопасно преобразовать в formula replay."""


def _require_dict(value, label):
    if not isinstance(value, dict):
        raise FormulaReplayBuildError(f'{label} должен быть объектом.')
    return value


def _require_exact_keys(value, expected_keys, label):
    value = _require_dict(value, label)
    actual_keys = set(value)
    missing = sorted(expected_keys - actual_keys)
    unexpected = sorted(actual_keys - expected_keys)
    if missing or unexpected:
        details = []
        if missing:
            details.append('нет: ' + ', '.join(missing))
        if unexpected:
            details.append('лишние: ' + ', '.join(unexpected))
        raise FormulaReplayBuildError(
            f'{label} имеет неверный набор полей ({"; ".join(details)}).',
        )
    return value


def _require_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise FormulaReplayBuildError(
            f'{label} должен быть непустой строкой.',
        )
    return value


def _require_nonnegative_int(value, label):
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise FormulaReplayBuildError(
            f'{label} должен быть целым неотрицательным.',
        )
    return value


def _require_sha256(value, label):
    normalized = str(value or '').upper()
    if not SHA256_RE.fullmatch(normalized):
        raise FormulaReplayBuildError(f'{label} должен быть SHA-256.')
    return normalized


def _parse_date(value, label):
    if not isinstance(value, str):
        raise FormulaReplayBuildError(f'{label} должен быть ISO-датой.')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise FormulaReplayBuildError(
            f'{label} должен быть ISO-датой.',
        ) from error
    if parsed.isoformat() != value:
        raise FormulaReplayBuildError(
            f'{label} должен быть канонической ISO-датой.',
        )
    return parsed


def _parse_datetime(value, label):
    if not isinstance(value, str):
        raise FormulaReplayBuildError(f'{label} должен быть ISO-временем.')
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise FormulaReplayBuildError(
            f'{label} должен быть ISO-временем.',
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FormulaReplayBuildError(
            f'{label} должен содержать часовой пояс.',
        )
    return parsed


def _parse_score_4(value, label):
    if not isinstance(value, str) or not SCORE_4_RE.fullmatch(value):
        raise FormulaReplayBuildError(
            f'{label} должен иметь четыре десятичных знака.',
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise FormulaReplayBuildError(f'{label} некорректен.') from error
    if not parsed.is_finite() or not Decimal('0') <= parsed <= Decimal('100'):
        raise FormulaReplayBuildError(f'{label} вышел за диапазон 0–100.')
    return parsed


def _parse_nonnegative_2(value, label):
    if not isinstance(value, str) or not NONNEGATIVE_2_RE.fullmatch(value):
        raise FormulaReplayBuildError(
            f'{label} должен иметь два десятичных знака.',
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise FormulaReplayBuildError(f'{label} некорректен.') from error
    if not parsed.is_finite() or parsed < 0:
        raise FormulaReplayBuildError(f'{label} должен быть неотрицательным.')
    return parsed


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FormulaReplayBuildError(
                f'JSON содержит повтор поля {key}.',
            )
        result[key] = value
    return result


def _decode_json(raw, label):
    def reject_constant(value):
        raise FormulaReplayBuildError(
            f'{label} содержит запрещённое значение {value}.',
        )

    try:
        return json.loads(
            raw.decode('utf-8'),
            parse_constant=reject_constant,
            object_pairs_hook=_strict_object,
        )
    except UnicodeDecodeError as error:
        raise FormulaReplayBuildError(f'{label} должен быть UTF-8.') from error
    except json.JSONDecodeError as error:
        raise FormulaReplayBuildError(
            f'{label} содержит некорректный JSON.',
        ) from error


def _read_json(path, *, label, max_bytes, expected_sha256=None):
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise FormulaReplayBuildError(
            f'{label} отсутствует или недоступен: {path}',
        ) from error
    if len(raw) > max_bytes:
        raise FormulaReplayBuildError(
            f'{label} превышает допустимый размер.',
        )
    raw_sha256 = hashlib.sha256(raw).hexdigest().upper()
    if (
        expected_sha256 is not None
        and raw_sha256 != _require_sha256(expected_sha256, f'SHA {label}')
    ):
        raise FormulaReplayBuildError(
            f'Фактическая SHA {label} не совпала с manifest.',
        )
    return _decode_json(raw, label), raw_sha256


def _json_bytes(payload):
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + '\n'
        ).encode('utf-8')
    except (TypeError, ValueError) as error:
        raise FormulaReplayBuildError(
            'Formula replay не сериализуется в строгий JSON.',
        ) from error


def _validate_database(database):
    database = _require_exact_keys(database, DATABASE_KEYS, 'database')
    configured = _require_exact_keys(
        database.get('configured'),
        CONFIGURED_DATABASE_KEYS,
        'database.configured',
    )
    expected_configured = {
        'engine': SOURCE_DB_ENGINE,
        'name': SOURCE_DB_NAME,
        'user': SOURCE_DB_USER,
        'host': SOURCE_DB_HOST,
        'port': SOURCE_DB_PORT,
    }
    if configured != expected_configured:
        raise FormulaReplayBuildError(
            'Manifest не относится к целевой изолированной PostgreSQL QA-БД.',
        )
    actual = _require_exact_keys(
        database.get('actual'),
        ACTUAL_DATABASE_KEYS,
        'database.actual',
    )
    if (
        actual.get('name') != SOURCE_DB_NAME
        or actual.get('user') != SOURCE_DB_USER
        or actual.get('port') != SOURCE_DB_PORT
        or actual.get('host') not in {
            SOURCE_DB_HOST,
            '127.0.0.1/32',
            '::1',
        }
    ):
        raise FormulaReplayBuildError(
            'Фактическая БД manifest не является целевой локальной QA-БД.',
        )
    _require_dict(database.get('business_counts'), 'database.business_counts')


def _validate_manifest(manifest):
    manifest = _require_exact_keys(
        manifest,
        MANIFEST_KEYS,
        'run_manifest',
    )
    if (
        manifest.get('schema') != SOURCE_RUN_SCHEMA
        or manifest.get('schema_version') != SOURCE_RUN_SCHEMA_VERSION
        or manifest.get('data_classification') != 'synthetic_qa_only'
        or manifest.get('synthetic') is not True
        or manifest.get('official') is not False
        or manifest.get('official_rating_eligible') is not False
    ):
        raise FormulaReplayBuildError(
            'run_manifest не является синтетическим 30-дневным QA-прогоном.',
        )
    _require_text(manifest.get('warning'), 'run_manifest.warning')
    run = _require_exact_keys(manifest.get('run'), RUN_KEYS, 'run')
    run_id = _require_text(run.get('id'), 'run.id')
    marker = _require_text(run.get('marker'), 'run.marker')
    if not marker.startswith('ТЕСТ_'):
        raise FormulaReplayBuildError('run.marker должен начинаться с ТЕСТ_.')
    if (
        run.get('day_count') != RATING_TV_FORMULA_REPLAY_DAY_COUNT
        or run.get('formula_version') != DRIVER_RATING_FORMULA_VERSION
        or run.get('formula_call_mode')
        != 'direct_after_each_completed_day'
    ):
        raise FormulaReplayBuildError(
            'run нарушает календарный или формульный контракт.',
        )
    start_date = _parse_date(run.get('start_date'), 'run.start_date')
    end_date = _parse_date(run.get('end_date'), 'run.end_date')
    ends_before = _parse_date(run.get('ends_before'), 'run.ends_before')
    if (
        end_date != start_date + timedelta(days=29)
        or ends_before != start_date + timedelta(days=30)
    ):
        raise FormulaReplayBuildError('run не образует ровно 30 дней.')
    if run.get('day_brigade') != 1 or run.get('night_brigade') != 3:
        raise FormulaReplayBuildError(
            'run имеет неверные фиксированные бригады day/night.',
        )
    if (
        not isinstance(run.get('duration_seconds'), (int, float))
        or isinstance(run.get('duration_seconds'), bool)
        or run.get('duration_seconds') < 0
    ):
        raise FormulaReplayBuildError('run.duration_seconds должен быть числом.')
    _validate_database(manifest.get('database'))

    scope = _require_exact_keys(manifest.get('scope'), SCOPE_KEYS, 'scope')
    composition = _require_exact_keys(
        scope.get('watch_composition'),
        MANIFEST_COMPOSITION_KEYS,
        'scope.watch_composition',
    )
    if (
        not isinstance(composition.get('id'), int)
        or not str(composition.get('code') or '').startswith('qa-')
        or 'тест' not in str(composition.get('name') or '').casefold()
    ):
        raise FormulaReplayBuildError(
            'scope.watch_composition не имеет синтетического маркера.',
        )
    watch_period = _require_exact_keys(
        scope.get('watch_period'),
        MANIFEST_WATCH_KEYS,
        'scope.watch_period',
    )
    rating_period = _require_exact_keys(
        scope.get('rating_period'),
        MANIFEST_PERIOD_KEYS,
        'scope.rating_period',
    )
    if (
        _parse_date(watch_period.get('starts_on'), 'watch.starts_on')
        != start_date
        or _parse_date(watch_period.get('ends_on'), 'watch.ends_on')
        != end_date
        or _parse_date(rating_period.get('starts_on'), 'period.starts_on')
        != start_date
        or _parse_date(
            rating_period.get('ends_before'),
            'period.ends_before',
        )
        != ends_before
    ):
        raise FormulaReplayBuildError(
            'Scope manifest не совпадает с календарём run.',
        )
    if (
        scope.get('day_employee_count')
        != RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT
        or scope.get('night_employee_count')
        != RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT
    ):
        raise FormulaReplayBuildError(
            'Каждая shift-когорта manifest должна иметь ровно 53 сотрудника.',
        )
    conversion = _require_exact_keys(
        manifest.get('replay_conversion'),
        REPLAY_CONVERSION_KEYS,
        'replay_conversion',
    )
    if (
        conversion.get('performed') is not False
        or not isinstance(conversion.get('reason'), str)
    ):
        raise FormulaReplayBuildError(
            'run_manifest уже помечен как преобразованный или повреждён.',
        )
    artifacts = manifest.get('formula_artifacts')
    if not isinstance(artifacts, list) or len(artifacts) != SOURCE_RAW_COUNT:
        raise FormulaReplayBuildError(
            'run_manifest должен перечислять ровно 60 raw formula.',
        )
    final_state = _require_dict(manifest.get('final_state'), 'final_state')
    counts = _require_dict(final_state.get('counts'), 'final_state.counts')
    if counts.get('formula_snapshots') != SOURCE_RAW_COUNT:
        raise FormulaReplayBuildError(
            'Финальная целостность не подтверждает 60 raw formula.',
        )
    return {
        'run_id': run_id,
        'marker': marker,
        'start_date': start_date,
        'end_date': end_date,
        'ends_before': ends_before,
        'composition': composition,
        'watch_period': watch_period,
        'rating_period': rating_period,
        'artifacts': artifacts,
    }


def _validate_raw_blocks(value, label):
    value = _require_exact_keys(
        value,
        set(DRIVER_RATING_WEIGHTS),
        label,
    )
    return {
        key: _parse_score_4(value.get(key), f'{label}.{key}')
        for key in DRIVER_RATING_WEIGHTS
    }


def _validate_raw_entry(entry, *, label, day_number):
    entry = _require_exact_keys(entry, RAW_ENTRY_KEYS, label)
    employee_id = entry.get('employee_id')
    if (
        not isinstance(employee_id, int)
        or isinstance(employee_id, bool)
        or employee_id <= 0
    ):
        raise FormulaReplayBuildError(
            f'{label}.employee_id должен быть положительным QA DB-ID.',
        )
    full_name = _require_text(entry.get('full_name'), f'{label}.full_name')
    if not full_name.startswith('ТЕСТ_'):
        raise FormulaReplayBuildError(
            f'{label}.full_name не имеет синтетического маркера.',
        )
    equipment = entry.get('equipment')
    if (
        not isinstance(equipment, list)
        or not equipment
        or any(
            not isinstance(item, str) or not item.strip()
            for item in equipment
        )
        or equipment != sorted(set(equipment))
    ):
        raise FormulaReplayBuildError(f'{label}.equipment некорректен.')
    if entry.get('shift_count') != day_number:
        raise FormulaReplayBuildError(
            f'{label}.shift_count не совпадает с номером дня.',
        )
    trip_count = _require_nonnegative_int(
        entry.get('trip_count'),
        f'{label}.trip_count',
    )
    volume_m3 = _parse_nonnegative_2(
        entry.get('volume_m3'),
        f'{label}.volume_m3',
    )
    tonnage_t = _parse_nonnegative_2(
        entry.get('tonnage_t'),
        f'{label}.tonnage_t',
    )
    score = _parse_score_4(entry.get('score'), f'{label}.score')
    _validate_raw_blocks(entry.get('blocks'), f'{label}.blocks')
    _parse_score_4(entry.get('confidence'), f'{label}.confidence')
    source_shift_ids = entry.get('source_shift_ids')
    if (
        not isinstance(source_shift_ids, list)
        or len(source_shift_ids) != day_number
        or any(
            not isinstance(item, int)
            or isinstance(item, bool)
            or item <= 0
            for item in source_shift_ids
        )
        or source_shift_ids != sorted(set(source_shift_ids))
    ):
        raise FormulaReplayBuildError(
            f'{label}.source_shift_ids некорректен.',
        )
    place = _require_nonnegative_int(entry.get('place'), f'{label}.place')
    shared_place = _require_nonnegative_int(
        entry.get('shared_score_place'),
        f'{label}.shared_score_place',
    )
    display_order = _require_nonnegative_int(
        entry.get('display_order'),
        f'{label}.display_order',
    )
    if place < 1 or shared_place < 1 or display_order < 1:
        raise FormulaReplayBuildError(
            f'{label} имеет неположительное место или display_order.',
        )
    level = entry.get('level')
    if not isinstance(level, str):
        raise FormulaReplayBuildError(f'{label}.level должен быть строкой.')
    return {
        'employee_id': employee_id,
        'full_name': full_name,
        'score': score,
        'trip_count': trip_count,
        'volume_m3': volume_m3,
        'tonnage_t': tonnage_t,
        'place': place,
        'shared_score_place': shared_place,
        'display_order': display_order,
        'level': level,
        'source_shift_ids': tuple(source_shift_ids),
    }


def _validate_raw_result(
    raw,
    *,
    day_number,
    shift_type,
    manifest_scope,
    artifact_record,
):
    raw = _require_exact_keys(
        raw,
        RAW_RESULT_KEYS,
        f'raw {shift_type} day {day_number}',
    )
    if (
        raw.get('available') is not True
        or raw.get('official') is not False
        or raw.get('rating_mode') != 'working'
        or raw.get('scope_type') != 'rating_period'
        or raw.get('formula_version') != DRIVER_RATING_FORMULA_VERSION
        or raw.get('shift_type') != shift_type
    ):
        raise FormulaReplayBuildError(
            f'Raw {shift_type} day {day_number} нарушает formula scope.',
        )
    _require_text(raw.get('formula_label'), 'raw.formula_label')
    _require_text(raw.get('status'), 'raw.status')
    generated_at = _parse_datetime(
        raw.get('generated_at'),
        f'raw {shift_type} day {day_number}.generated_at',
    )
    if artifact_record.get('generated_at') != raw.get('generated_at'):
        raise FormulaReplayBuildError(
            f'generated_at raw {shift_type} day {day_number} '
            'не совпал с manifest.',
        )
    source_fingerprint = str(raw.get('source_fingerprint') or '')
    shift_score_fingerprint = str(
        raw.get('shift_score_fingerprint') or '',
    )
    if (
        _require_sha256(
            source_fingerprint,
            'raw.source_fingerprint',
        )
        != _require_sha256(
            artifact_record.get('source_fingerprint'),
            'manifest.source_fingerprint',
        )
        or _require_sha256(
            shift_score_fingerprint,
            'raw.shift_score_fingerprint',
        )
        != _require_sha256(
            artifact_record.get('shift_score_fingerprint'),
            'manifest.shift_score_fingerprint',
        )
    ):
        raise FormulaReplayBuildError(
            f'Formula fingerprints {shift_type} day {day_number} '
            'не совпали с manifest.',
        )
    period = _require_exact_keys(
        raw.get('rating_period'),
        RAW_PERIOD_KEYS,
        'raw.rating_period',
    )
    composition = _require_exact_keys(
        raw.get('watch_composition'),
        RAW_COMPOSITION_KEYS,
        'raw.watch_composition',
    )
    expected_period = manifest_scope['rating_period']
    expected_composition = manifest_scope['composition']
    if (
        period.get('id') != expected_period.get('id')
        or period.get('starts_on') != expected_period.get('starts_on')
        or period.get('ends_before') != expected_period.get('ends_before')
        or composition != expected_composition
        or 'тест' not in str(period.get('name') or '').casefold()
    ):
        raise FormulaReplayBuildError(
            f'Raw scope {shift_type} day {day_number} изменился.',
        )
    expected_weights = {
        key: str(value)
        for key, value in DRIVER_RATING_WEIGHTS.items()
    }
    if raw.get('weights') != expected_weights:
        raise FormulaReplayBuildError(
            f'Raw weights {shift_type} day {day_number} изменились.',
        )
    distance_metrics = _require_exact_keys(
        raw.get('distance_metrics'),
        DISTANCE_METRIC_KEYS,
        'raw.distance_metrics',
    )
    if distance_metrics != {
        'weight': '0',
        'status': 'planned',
        'label': 'м³·км и т·км пока не учитываются',
    }:
        raise FormulaReplayBuildError(
            f'Raw distance metrics {shift_type} day {day_number} изменились.',
        )
    linkage = _require_exact_keys(
        raw.get('linkage_audit'),
        LINKAGE_AUDIT_KEYS,
        'raw.linkage_audit',
    )
    summary = _require_exact_keys(
        raw.get('summary'),
        RAW_SUMMARY_KEYS,
        'raw.summary',
    )
    expected_shift_count = (
        RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT * day_number
    )
    expected_linkage = {
        'candidate_closed_shift_count': expected_shift_count,
        'linked_to_selected_composition_count': expected_shift_count,
        'unlinked_shift_count': 0,
        'linked_to_other_composition_count': 0,
        'selected_watch_date_mismatch_count': 0,
        'covered_watch_period_count': 1,
        'linkage_ready': True,
    }
    if linkage != expected_linkage:
        raise FormulaReplayBuildError(
            f'Raw linkage {shift_type} day {day_number} '
            'не подтверждает полную однозначную привязку смен.',
        )
    if (
        summary.get('employee_count')
        != RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT
        or summary.get('rated_shift_count') != expected_shift_count
        or summary.get('withheld_shift_count') != 0
        or summary.get('withheld_reasons') != {}
        or artifact_record.get('employee_count')
        != RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT
        or artifact_record.get('rated_shift_count') != expected_shift_count
        or artifact_record.get('withheld_shift_count') != 0
    ):
        raise FormulaReplayBuildError(
            f'Raw {shift_type} day {day_number} содержит удержание '
            'или неполную когорту.',
        )
    _require_nonnegative_int(summary.get('trip_count'), 'summary.trip_count')
    summary_volume = _parse_nonnegative_2(
        summary.get('volume_m3'),
        'summary.volume_m3',
    )
    summary_tonnage = _parse_nonnegative_2(
        summary.get('tonnage_t'),
        'summary.tonnage_t',
    )
    entries = raw.get('entries')
    if (
        not isinstance(entries, list)
        or len(entries) != RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT
    ):
        raise FormulaReplayBuildError(
            f'Raw {shift_type} day {day_number} должен иметь 53 entries.',
        )
    validated = [
        _validate_raw_entry(
            entry,
            label=f'raw {shift_type} day {day_number} entry',
            day_number=day_number,
        )
        for entry in entries
    ]
    employee_ids = [entry['employee_id'] for entry in validated]
    if len(set(employee_ids)) != RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT:
        raise FormulaReplayBuildError(
            f'Raw cohort {shift_type} day {day_number} имеет дубли.',
        )
    all_source_shift_ids = [
        source_shift_id
        for entry in validated
        for source_shift_id in entry['source_shift_ids']
    ]
    if (
        len(all_source_shift_ids) != expected_shift_count
        or len(set(all_source_shift_ids)) != expected_shift_count
    ):
        raise FormulaReplayBuildError(
            f'Raw source_shift_ids {shift_type} day {day_number} '
            'не образуют однозначные связи сотрудник–смена.',
        )
    orders = {entry['display_order'] for entry in validated}
    if orders != set(
        range(1, RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT + 1),
    ):
        raise FormulaReplayBuildError(
            f'Raw display_order {shift_type} day {day_number} повреждён.',
        )
    ordered = sorted(validated, key=lambda entry: entry['display_order'])
    previous_score = None
    dense_place = 0
    for entry in ordered:
        score = entry['score']
        if previous_score is not None and score > previous_score:
            raise FormulaReplayBuildError(
                f'Raw score order {shift_type} day {day_number} повреждён.',
            )
        if previous_score is None or score != previous_score:
            dense_place += 1
        if (
            entry['place'] != dense_place
            or entry['shared_score_place'] != dense_place
            or entry['level'] != DRIVER_RATING_LEVELS.get(
                dense_place,
                '',
            )
        ):
            raise FormulaReplayBuildError(
                f'Raw dense place {shift_type} day {day_number} повреждён.',
            )
        previous_score = score
    if (
        sum(entry['trip_count'] for entry in validated)
        != summary.get('trip_count')
        or sum(
            (entry['volume_m3'] for entry in validated),
            Decimal('0'),
        )
        != summary_volume
        or sum(
            (entry['tonnage_t'] for entry in validated),
            Decimal('0'),
        )
        != summary_tonnage
    ):
        raise FormulaReplayBuildError(
            f'Raw summary {shift_type} day {day_number} '
            'не совпадает с entries.',
        )
    return {
        'raw': raw,
        'generated_at': generated_at,
        'validated_entries': validated,
    }


def _safe_raw_path(run_dir, relative_path, expected_path):
    if relative_path != expected_path.as_posix():
        raise FormulaReplayBuildError(
            f'Raw path {relative_path!r} не совпадает с fixed layout.',
        )
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or '..' in pure.parts:
        raise FormulaReplayBuildError('Raw path пытается выйти из run_dir.')
    resolved = (run_dir / Path(*pure.parts)).resolve()
    if run_dir != resolved and run_dir not in resolved.parents:
        raise FormulaReplayBuildError('Raw path вышел из run_dir.')
    return resolved


def load_verified_source_run(run_dir):
    run_dir = Path(run_dir).expanduser().resolve()
    if not run_dir.is_dir():
        raise FormulaReplayBuildError('Каталог завершённого run не найден.')
    manifest, manifest_sha256 = _read_json(
        run_dir / 'run_manifest.json',
        label='run_manifest',
        max_bytes=SOURCE_MANIFEST_MAX_BYTES,
    )
    manifest_scope = _validate_manifest(manifest)
    artifact_records = {}
    for item in manifest_scope['artifacts']:
        item = _require_exact_keys(
            item,
            FORMULA_ARTIFACT_KEYS,
            'formula_artifacts[]',
        )
        day_number = item.get('day')
        shift_type = item.get('shift_type')
        if (
            day_number not in range(
                1,
                RATING_TV_FORMULA_REPLAY_DAY_COUNT + 1,
            )
            or shift_type not in OUTPUT_NAMES
            or (shift_type, day_number) in artifact_records
        ):
            raise FormulaReplayBuildError(
                'formula_artifacts имеет неверный или повторный день/shift.',
            )
        artifact_records[(shift_type, day_number)] = item
    expected_keys = {
        (shift_type, day_number)
        for shift_type in OUTPUT_NAMES
        for day_number in range(
            1,
            RATING_TV_FORMULA_REPLAY_DAY_COUNT + 1,
        )
    }
    if set(artifact_records) != expected_keys:
        raise FormulaReplayBuildError(
            'formula_artifacts не образует полную сетку day/night × 1–30.',
        )

    raw_by_shift = {shift_type: [] for shift_type in OUTPUT_NAMES}
    previous_cohort_by_shift = {}
    previous_names_by_shift = {}
    previous_shift_ids_by_shift = {}
    for shift_type in OUTPUT_NAMES:
        for day_number in range(
            1,
            RATING_TV_FORMULA_REPLAY_DAY_COUNT + 1,
        ):
            item = artifact_records[(shift_type, day_number)]
            expected_path = (
                Path('raw_formula')
                / shift_type
                / f'day_{day_number:02d}.json'
            )
            raw_path = _safe_raw_path(
                run_dir,
                item.get('path'),
                expected_path,
            )
            raw, raw_sha256 = _read_json(
                raw_path,
                label=f'raw {shift_type} day {day_number}',
                max_bytes=SOURCE_RAW_MAX_BYTES,
                expected_sha256=item.get('sha256'),
            )
            result = _validate_raw_result(
                raw,
                day_number=day_number,
                shift_type=shift_type,
                manifest_scope=manifest_scope,
                artifact_record=item,
            )
            identities = {
                entry['employee_id']: entry['full_name']
                for entry in result['validated_entries']
            }
            shift_ids = {
                entry['employee_id']: set(entry['source_shift_ids'])
                for entry in result['validated_entries']
            }
            if shift_type not in previous_cohort_by_shift:
                previous_cohort_by_shift[shift_type] = set(identities)
                previous_names_by_shift[shift_type] = identities
            elif (
                set(identities) != previous_cohort_by_shift[shift_type]
                or identities != previous_names_by_shift[shift_type]
            ):
                raise FormulaReplayBuildError(
                    f'Когорта {shift_type} изменилась в дне {day_number}.',
                )
            previous_shift_ids = previous_shift_ids_by_shift.get(
                shift_type,
                {},
            )
            for employee_id, current_ids in shift_ids.items():
                if not previous_shift_ids.get(employee_id, set()).issubset(
                    current_ids,
                ):
                    raise FormulaReplayBuildError(
                        f'История смен employee_id={employee_id} '
                        f'{shift_type} уменьшилась в дне {day_number}.',
                    )
            previous_shift_ids_by_shift[shift_type] = shift_ids
            raw_by_shift[shift_type].append({
                **result,
                'raw_path': item.get('path'),
                'raw_sha256': raw_sha256,
            })
    if (
        previous_cohort_by_shift['day']
        & previous_cohort_by_shift['night']
    ):
        raise FormulaReplayBuildError(
            'Дневная и ночная raw-когорты пересекаются.',
        )
    final_day_shift_ids = {
        source_shift_id
        for source_shift_ids in previous_shift_ids_by_shift['day'].values()
        for source_shift_id in source_shift_ids
    }
    final_night_shift_ids = {
        source_shift_id
        for source_shift_ids in previous_shift_ids_by_shift['night'].values()
        for source_shift_id in source_shift_ids
    }
    if final_day_shift_ids & final_night_shift_ids:
        raise FormulaReplayBuildError(
            'Дневная и ночная raw-когорты ссылаются на общие смены.',
        )
    return {
        'run_dir': run_dir,
        'manifest': manifest,
        'manifest_sha256': manifest_sha256,
        'scope': manifest_scope,
        'raw_by_shift': raw_by_shift,
    }


def _pseudonym_maps(raw_days, *, shift_type):
    employee_offset = (
        0
        if shift_type == 'day'
        else RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT
    )
    shift_offset = (
        0
        if shift_type == 'day'
        else (
            RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT
            * RATING_TV_FORMULA_REPLAY_DAY_COUNT
        )
    )
    baseline_entries = raw_days[0]['validated_entries']
    employee_ids = sorted(entry['employee_id'] for entry in baseline_entries)
    employee_map = {
        source_id: -(employee_offset + ordinal)
        for ordinal, source_id in enumerate(employee_ids, start=1)
    }
    final_shift_ids = sorted({
        shift_id
        for entry in raw_days[-1]['validated_entries']
        for shift_id in entry['source_shift_ids']
    })
    shift_map = {
        source_id: -(shift_offset + ordinal)
        for ordinal, source_id in enumerate(final_shift_ids, start=1)
    }
    return employee_map, shift_map


def build_formula_replay_document(
    source_run,
    *,
    shift_type,
    source_commit,
):
    if shift_type not in OUTPUT_NAMES:
        raise FormulaReplayBuildError('shift_type должен быть day или night.')
    if not isinstance(source_commit, str) or not GIT_SHA_RE.fullmatch(
        source_commit,
    ):
        raise FormulaReplayBuildError(
            'source_commit должен быть полным Git SHA из 40 символов.',
        )
    raw_days = source_run['raw_by_shift'][shift_type]
    scope = source_run['scope']
    employee_map, shift_map = _pseudonym_maps(
        raw_days,
        shift_type=shift_type,
    )
    first_raw = raw_days[0]['raw']
    source_ids_by_replay = {
        replay_id: source_id
        for source_id, replay_id in employee_map.items()
    }
    first_entries_by_id = {
        entry['employee_id']: entry
        for entry in first_raw['entries']
    }
    cohort = [
        {
            'employee_id': replay_id,
            'full_name': first_entries_by_id[source_id]['full_name'],
        }
        for replay_id, source_id in sorted(source_ids_by_replay.items())
    ]
    period = {
        'id': -4101,
        'name': first_raw['rating_period']['name'],
        'starts_on': scope['start_date'].isoformat(),
        'ends_before': scope['ends_before'].isoformat(),
        'is_active': True,
    }
    composition = {
        'id': -4201,
        'code': scope['composition']['code'],
        'name': scope['composition']['name'],
        'is_active': True,
    }
    replay_id = (
        f'{scope["run_id"]}-{shift_type.upper()}-FORMULA-REPLAY-V1'
    )
    snapshots = []
    previous_places = {}
    for day_number, raw_day in enumerate(raw_days, start=1):
        raw = raw_day['raw']
        rows = []
        current_places = {}
        for source_entry in sorted(
            raw['entries'],
            key=lambda entry: entry['display_order'],
        ):
            replay_employee_id = employee_map[source_entry['employee_id']]
            place = source_entry['place']
            current_places[replay_employee_id] = place
            rows.append({
                'employee_id': replay_employee_id,
                'full_name': source_entry['full_name'],
                'equipment': source_entry['equipment'],
                'row_status': 'rated',
                'ranking_eligible': True,
                'shift_count': source_entry['shift_count'],
                'withheld_shift_count': 0,
                'withheld_reasons': {},
                'quality_flags': [],
                'quality_flags_status': (
                    'not_exposed_by_formula_payload'
                ),
                'trip_count': source_entry['trip_count'],
                'volume_m3': source_entry['volume_m3'],
                'tonnage_t': source_entry['tonnage_t'],
                'score': source_entry['score'],
                'blocks': source_entry['blocks'],
                'confidence': source_entry['confidence'],
                'source_shift_ids': sorted(
                    shift_map[source_shift_id]
                    for source_shift_id in source_entry['source_shift_ids']
                ),
                'place': place,
                'shared_score_place': source_entry['shared_score_place'],
                'display_order': source_entry['display_order'],
                'level': source_entry['level'],
                'position_delta': (
                    None
                    if day_number == 1
                    else previous_places[replay_employee_id] - place
                ),
            })
        work_date = scope['start_date'] + timedelta(days=day_number - 1)
        summary = raw['summary']
        payload = {
            'available': True,
            'calculation_available': True,
            'official': False,
            'official_rating_eligible': False,
            'synthetic': True,
            'formula_evaluated': True,
            'rating_mode': RATING_TV_FORMULA_REPLAY_MODE,
            'scope_type': 'rating_period',
            'formula_version': raw['formula_version'],
            'formula_label': raw['formula_label'],
            'status': raw['status'],
            'generated_at': raw['generated_at'],
            'source_raw_path': raw_day['raw_path'],
            'source_raw_sha256': raw_day['raw_sha256'],
            'source_fingerprint': str(raw['source_fingerprint']).upper(),
            'shift_score_fingerprint': str(
                raw['shift_score_fingerprint'],
            ).upper(),
            'rating_period': period,
            'watch_composition': composition,
            'shift_type': shift_type,
            'shift_type_label': raw['shift_type_label'],
            'available_rating_periods': [period],
            'available_watch_compositions': [composition],
            'calculation_window': {
                'starts_on': scope['start_date'].isoformat(),
                'ends_before': (
                    work_date + timedelta(days=1)
                ).isoformat(),
            },
            'weights': raw['weights'],
            'distance_metrics': raw['distance_metrics'],
            'linkage_audit': raw['linkage_audit'],
            'calculation_summary': {
                'employee_count': summary['employee_count'],
                'rated_shift_count': summary['rated_shift_count'],
                'withheld_shift_count': summary['withheld_shift_count'],
                'withheld_reasons': summary['withheld_reasons'],
                'trip_count': summary['trip_count'],
                'volume_m3': summary['volume_m3'],
                'tonnage_t': summary['tonnage_t'],
            },
            'display_summary': {
                'cohort_employee_count': (
                    RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT
                ),
                'rated_employee_count': (
                    RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT
                ),
                'withheld_employee_count': 0,
                'not_observed_employee_count': 0,
            },
            'entries': rows,
            'qa_day': day_number,
            'qa_day_count': RATING_TV_FORMULA_REPLAY_DAY_COUNT,
            'qa_work_date': work_date.isoformat(),
            'replay_run_id': replay_id,
        }
        snapshots.append({
            'day': day_number,
            'work_date': work_date.isoformat(),
            'as_of': raw['generated_at'],
            'payload': payload,
        })
        previous_places = current_places

    document = {
        'schema': RATING_TV_FORMULA_REPLAY_SCHEMA,
        'schema_version': RATING_TV_FORMULA_REPLAY_SCHEMA_VERSION,
        'data_classification': RATING_TV_FORMULA_REPLAY_DATA_CLASSIFICATION,
        'synthetic': True,
        'formula_evaluated': True,
        'official': False,
        'official_rating_eligible': False,
        'warning': (
            'СИНТЕТИЧЕСКИЙ ФОРМУЛЬНЫЙ QA-ПРОГОН. '
            'Не является реальным или официальным рейтингом.'
        ),
        'replay': {
            'id': replay_id,
            'label': (
                '30-дневный формульный replay · '
                f'{SHIFT_LABELS[shift_type]} смена'
            ),
            'scenario_version': 'DRIVER_RATING_30D_FORMULA_REPLAY_V1',
            'rating_mode': RATING_TV_FORMULA_REPLAY_MODE,
            'synthetic': True,
            'formula_evaluated': True,
            'official': False,
            'day_count': RATING_TV_FORMULA_REPLAY_DAY_COUNT,
            'expected_employee_count': (
                RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT
            ),
            'initial_day': 1,
            'base_step_ms': 3000,
            'created_at': raw_days[-1]['raw']['generated_at'],
            'formula_version': DRIVER_RATING_FORMULA_VERSION,
            'formula_label': first_raw['formula_label'],
            'timezone': 'Asia/Vladivostok',
            'source_commit': source_commit.lower(),
            'source_run_id': scope['run_id'],
            'source_manifest_sha256': source_run['manifest_sha256'],
            'source_database_classification': (
                'isolated_synthetic_postgresql_qa'
            ),
            'notice': (
                'Построено только из проверенных raw-вызовов формулы '
                'изолированного синтетического PostgreSQL QA-прогона. '
                'Не использовать для оценки сотрудников или премирования.'
            ),
        },
        'scope': {
            'scope_type': 'rating_period',
            'profession': 'driver',
            'profession_label': 'Водитель самосвала',
            'rating_period': period,
            'watch_composition': composition,
            'shift_type': shift_type,
            'shift_type_label': first_raw['shift_type_label'],
            'cohort_sha256': formula_replay_cohort_sha256(cohort),
            'cohort': cohort,
        },
        'snapshots': snapshots,
    }
    result = attach_formula_replay_integrity(document)
    validate_rating_tv_formula_replay(result)
    return result


def _write_staging_file(path, payload):
    raw = _json_bytes(payload)
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return {
        'path': path,
        'bytes': len(raw),
        'raw_sha256': hashlib.sha256(raw).hexdigest().upper(),
        'canonical_sha256': payload['integrity']['canonical_sha256'],
    }


def publish_formula_replays(
    source_run,
    *,
    output_dir,
    source_commit,
):
    output_dir = Path(output_dir).expanduser().resolve()
    if output_dir.exists():
        raise FormulaReplayBuildError(
            f'Каталог результата уже существует; перезапись запрещена: '
            f'{output_dir}',
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    documents = {
        shift_type: build_formula_replay_document(
            source_run,
            shift_type=shift_type,
            source_commit=source_commit,
        )
        for shift_type in OUTPUT_NAMES
    }
    staging_dir = (
        output_dir.parent
        / f'.{output_dir.name}.tmp-{uuid.uuid4().hex}'
    )
    staging_dir.mkdir(exist_ok=False)
    results = {}
    try:
        for shift_type, output_name in OUTPUT_NAMES.items():
            results[shift_type] = _write_staging_file(
                staging_dir / output_name,
                documents[shift_type],
            )
        os.rename(staging_dir, output_dir)
    except Exception:
        raise
    for result in results.values():
        result['path'] = output_dir / result['path'].name
    return results


def convert_formula_replays(
    *,
    run_dir,
    output_dir,
    source_commit,
):
    source_run = load_verified_source_run(run_dir)
    return publish_formula_replays(
        source_run,
        output_dir=output_dir,
        source_commit=source_commit,
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            'Проверить 60 raw formula и атомарно создать day/night replay.'
        ),
    )
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--source-commit', required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    results = convert_formula_replays(
        run_dir=args.run_dir,
        output_dir=args.output_dir,
        source_commit=args.source_commit,
    )
    for shift_type in ('day', 'night'):
        result = results[shift_type]
        print(
            f'FORMULA_REPLAY_{shift_type.upper()}={result["path"]} '
            f'BYTES={result["bytes"]} '
            f'RAW_SHA256={result["raw_sha256"]} '
            f'CANONICAL_SHA256={result["canonical_sha256"]}',
        )


if __name__ == '__main__':
    main()
