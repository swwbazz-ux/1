from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .driver_watch_rating import (
    DRIVER_RATING_BLOCKING_FLAGS,
    DRIVER_RATING_FORMULA_VERSION,
    DRIVER_RATING_LEVELS,
    DRIVER_RATING_WEIGHTS,
)


RATING_TV_FORMULA_REPLAY_SCHEMA = 'copper.driver-rating-formula-replay'
RATING_TV_FORMULA_REPLAY_SCHEMA_VERSION = 1
RATING_TV_FORMULA_REPLAY_DAY_COUNT = 30
RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT = 53
RATING_TV_FORMULA_REPLAY_DATA_CLASSIFICATION = (
    'isolated_synthetic_formula_qa_only'
)
RATING_TV_FORMULA_REPLAY_MAX_BYTES = 16 * 1024 * 1024
RATING_TV_FORMULA_REPLAY_MODE = 'qa_formula_replay'

ROW_STATUS_RATED = 'rated'
ROW_STATUS_WITHHELD = 'withheld'
ROW_STATUS_NOT_OBSERVED = 'not_observed'
ROW_STATUSES = {
    ROW_STATUS_RATED,
    ROW_STATUS_WITHHELD,
    ROW_STATUS_NOT_OBSERVED,
}
QUALITY_FLAGS_STATUS_CAPTURED = 'captured'
QUALITY_FLAGS_STATUS_NOT_EXPOSED = 'not_exposed_by_formula_payload'
QUALITY_FLAGS_STATUS_NOT_APPLICABLE = 'not_applicable'
QUALITY_FLAGS_STATUSES = {
    QUALITY_FLAGS_STATUS_CAPTURED,
    QUALITY_FLAGS_STATUS_NOT_EXPOSED,
    QUALITY_FLAGS_STATUS_NOT_APPLICABLE,
}

SHA256_RE = re.compile(r'^[0-9A-F]{64}$')
SCORE_4_RE = re.compile(r'^(?:0|[1-9][0-9]{0,2})\.[0-9]{4}$')
NONNEGATIVE_2_RE = re.compile(r'^(?:0|[1-9][0-9]*)\.[0-9]{2}$')
CODE_RE = re.compile(r'^[a-z0-9_]+$')
REASON_RE = re.compile(r'^[a-z0-9_]+(?::[a-z0-9_,.-]+)?$')

FORBIDDEN_PERSONNEL_FIELDS = {
    'access_code',
    'address',
    'birth_date',
    'email',
    'passport',
    'personnel_number',
    'phone',
    'photo',
    'photo_url',
    'pin',
    'snils',
}

DOCUMENT_KEYS = {
    'schema',
    'schema_version',
    'data_classification',
    'synthetic',
    'formula_evaluated',
    'official',
    'official_rating_eligible',
    'warning',
    'replay',
    'scope',
    'snapshots',
    'integrity',
}
INTEGRITY_KEYS = {
    'algorithm',
    'canonicalization',
    'snapshot_chain_sha256',
    'canonical_sha256',
}
REPLAY_KEYS = {
    'id',
    'label',
    'scenario_version',
    'rating_mode',
    'synthetic',
    'formula_evaluated',
    'official',
    'day_count',
    'expected_employee_count',
    'initial_day',
    'base_step_ms',
    'created_at',
    'formula_version',
    'formula_label',
    'timezone',
    'source_commit',
    'source_run_id',
    'source_manifest_sha256',
    'source_database_classification',
    'notice',
}
SCOPE_KEYS = {
    'scope_type',
    'profession',
    'profession_label',
    'rating_period',
    'watch_composition',
    'shift_type',
    'shift_type_label',
    'cohort_sha256',
    'cohort',
}
RATING_PERIOD_KEYS = {
    'id',
    'name',
    'starts_on',
    'ends_before',
    'is_active',
}
WATCH_COMPOSITION_KEYS = {
    'id',
    'code',
    'name',
    'is_active',
}
COHORT_ENTRY_KEYS = {
    'employee_id',
    'full_name',
}
SNAPSHOT_KEYS = {
    'day',
    'work_date',
    'as_of',
    'previous_payload_sha256',
    'payload_sha256',
    'payload',
}
PAYLOAD_KEYS = {
    'available',
    'calculation_available',
    'official',
    'official_rating_eligible',
    'synthetic',
    'formula_evaluated',
    'rating_mode',
    'scope_type',
    'formula_version',
    'formula_label',
    'status',
    'generated_at',
    'source_raw_path',
    'source_raw_sha256',
    'source_fingerprint',
    'shift_score_fingerprint',
    'rating_period',
    'watch_composition',
    'shift_type',
    'shift_type_label',
    'available_rating_periods',
    'available_watch_compositions',
    'calculation_window',
    'weights',
    'distance_metrics',
    'linkage_audit',
    'calculation_summary',
    'display_summary',
    'entries',
    'qa_day',
    'qa_day_count',
    'qa_work_date',
    'replay_run_id',
}
CALCULATION_WINDOW_KEYS = {
    'starts_on',
    'ends_before',
}
WEIGHT_KEYS = set(DRIVER_RATING_WEIGHTS)
DISTANCE_METRIC_KEYS = {
    'weight',
    'status',
    'label',
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
CALCULATION_SUMMARY_KEYS = {
    'employee_count',
    'rated_shift_count',
    'withheld_shift_count',
    'withheld_reasons',
    'trip_count',
    'volume_m3',
    'tonnage_t',
}
DISPLAY_SUMMARY_KEYS = {
    'cohort_employee_count',
    'rated_employee_count',
    'withheld_employee_count',
    'not_observed_employee_count',
}
ENTRY_KEYS = {
    'employee_id',
    'full_name',
    'equipment',
    'row_status',
    'ranking_eligible',
    'shift_count',
    'withheld_shift_count',
    'withheld_reasons',
    'quality_flags',
    'quality_flags_status',
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
    'position_delta',
}


class RatingTvFormulaReplayError(ValueError):
    """Сохранённый формульный QA-прогон непригоден."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError) as error:
        raise RatingTvFormulaReplayError(
            'Formula replay содержит неканонизируемое JSON-значение.',
        ) from error


def canonical_formula_replay_bytes(document: dict[str, Any]) -> bytes:
    if not isinstance(document, dict):
        raise RatingTvFormulaReplayError(
            'Корень formula replay должен быть объектом.',
        )
    normalized = deepcopy(document)
    integrity = normalized.get('integrity')
    if isinstance(integrity, dict):
        integrity.pop('canonical_sha256', None)
    return canonical_json_bytes(normalized)


def canonical_formula_replay_sha256(document: dict[str, Any]) -> str:
    return hashlib.sha256(
        canonical_formula_replay_bytes(document),
    ).hexdigest().upper()


def formula_replay_payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest().upper()


def formula_replay_cohort_sha256(cohort: list[dict[str, Any]]) -> str:
    return hashlib.sha256(canonical_json_bytes(cohort)).hexdigest().upper()


def formula_replay_snapshot_chain_sha256(snapshots) -> str:
    chain_rows = [
        {
            'day': snapshot.get('day'),
            'work_date': snapshot.get('work_date'),
            'as_of': snapshot.get('as_of'),
            'previous_payload_sha256': snapshot.get(
                'previous_payload_sha256',
            ),
            'payload_sha256': snapshot.get('payload_sha256'),
        }
        for snapshot in snapshots
    ]
    return hashlib.sha256(canonical_json_bytes(chain_rows)).hexdigest().upper()


def attach_formula_replay_integrity(
    document: dict[str, Any],
) -> dict[str, Any]:
    result = deepcopy(document)
    snapshots = result.get('snapshots')
    if not isinstance(snapshots, list):
        raise RatingTvFormulaReplayError(
            'Formula replay должен иметь список snapshots.',
        )
    previous_payload_sha256 = None
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            raise RatingTvFormulaReplayError(
                'Каждый formula replay snapshot должен быть объектом.',
            )
        payload = snapshot.get('payload')
        if not isinstance(payload, dict):
            raise RatingTvFormulaReplayError(
                'Каждый formula replay snapshot должен иметь payload.',
            )
        snapshot['previous_payload_sha256'] = previous_payload_sha256
        snapshot['payload_sha256'] = formula_replay_payload_sha256(payload)
        previous_payload_sha256 = snapshot['payload_sha256']
    result['integrity'] = {
        'algorithm': 'SHA-256',
        'canonicalization': 'json-sort-keys-utf8-v1',
        'snapshot_chain_sha256': formula_replay_snapshot_chain_sha256(
            snapshots,
        ),
        'canonical_sha256': '',
    }
    result['integrity']['canonical_sha256'] = (
        canonical_formula_replay_sha256(result)
    )
    return result


def _require_dict(value, label):
    if not isinstance(value, dict):
        raise RatingTvFormulaReplayError(f'{label} должен быть объектом.')
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
        raise RatingTvFormulaReplayError(
            f'{label} имеет неверный набор полей ({"; ".join(details)}).',
        )
    return value


def _require_nonempty_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RatingTvFormulaReplayError(
            f'{label} должен быть непустой строкой.',
        )
    return value


def _require_nonnegative_int(value, label):
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise RatingTvFormulaReplayError(
            f'{label} должен быть целым неотрицательным числом.',
        )
    return value


def _require_positive_int(value, label):
    value = _require_nonnegative_int(value, label)
    if value < 1:
        raise RatingTvFormulaReplayError(
            f'{label} должен быть положительным.',
        )
    return value


def _require_sha256(value, label):
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise RatingTvFormulaReplayError(
            f'{label} должен быть SHA-256 в верхнем регистре.',
        )
    return value


def _parse_iso_date(value, label):
    if not isinstance(value, str):
        raise RatingTvFormulaReplayError(
            f'{label} должен быть ISO-датой.',
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise RatingTvFormulaReplayError(
            f'{label} должен быть ISO-датой.',
        ) from error
    if parsed.isoformat() != value:
        raise RatingTvFormulaReplayError(
            f'{label} должен быть канонической ISO-датой.',
        )
    return parsed


def _parse_iso_datetime(value, label):
    if not isinstance(value, str):
        raise RatingTvFormulaReplayError(
            f'{label} должен быть ISO-временем.',
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise RatingTvFormulaReplayError(
            f'{label} должен быть ISO-временем.',
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RatingTvFormulaReplayError(
            f'{label} должен содержать часовой пояс.',
        )
    return parsed


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RatingTvFormulaReplayError(
                f'Formula replay содержит повтор поля {key}.',
            )
        result[key] = value
    return result


def _strict_json_loads(raw):
    def reject_constant(value):
        raise RatingTvFormulaReplayError(
            f'Formula replay содержит запрещённое значение {value}.',
        )

    try:
        return json.loads(
            raw.decode('utf-8'),
            parse_constant=reject_constant,
            object_pairs_hook=_strict_object,
        )
    except UnicodeDecodeError as error:
        raise RatingTvFormulaReplayError(
            'Formula replay должен быть UTF-8.',
        ) from error
    except json.JSONDecodeError as error:
        raise RatingTvFormulaReplayError(
            'Formula replay содержит некорректный JSON.',
        ) from error


def _reject_forbidden_personnel_fields(value, path='document'):
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = str(key).strip().casefold()
            if normalized_key in FORBIDDEN_PERSONNEL_FIELDS:
                raise RatingTvFormulaReplayError(
                    f'Formula replay содержит запрещённое поле {path}.{key}.',
                )
            _reject_forbidden_personnel_fields(
                nested,
                f'{path}.{key}',
            )
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_forbidden_personnel_fields(
                nested,
                f'{path}[{index}]',
            )


def _parse_score_4(value, label):
    if not isinstance(value, str) or not SCORE_4_RE.fullmatch(value):
        raise RatingTvFormulaReplayError(
            f'{label} должен иметь ровно четыре десятичных знака.',
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise RatingTvFormulaReplayError(
            f'{label} содержит некорректное число.',
        ) from error
    if not parsed.is_finite() or not Decimal('0') <= parsed <= Decimal('100'):
        raise RatingTvFormulaReplayError(
            f'{label} должен быть в диапазоне 0–100.',
        )
    return parsed


def _parse_nonnegative_2(value, label):
    if not isinstance(value, str) or not NONNEGATIVE_2_RE.fullmatch(value):
        raise RatingTvFormulaReplayError(
            f'{label} должен иметь ровно два десятичных знака.',
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise RatingTvFormulaReplayError(
            f'{label} содержит некорректное число.',
        ) from error
    if not parsed.is_finite() or parsed < 0:
        raise RatingTvFormulaReplayError(
            f'{label} должен быть неотрицательным.',
        )
    return parsed


def _validate_code_list(value, label):
    if not isinstance(value, list):
        raise RatingTvFormulaReplayError(f'{label} должен быть списком.')
    if any(
        not isinstance(item, str)
        or not CODE_RE.fullmatch(item)
        for item in value
    ):
        raise RatingTvFormulaReplayError(
            f'{label} содержит некорректный код.',
        )
    if value != sorted(set(value)):
        raise RatingTvFormulaReplayError(
            f'{label} должен быть отсортирован и не иметь дублей.',
        )
    return tuple(value)


def _validate_reason_map(value, label):
    value = _require_dict(value, label)
    result = {}
    for reason, count in value.items():
        if (
            not isinstance(reason, str)
            or not REASON_RE.fullmatch(reason)
        ):
            raise RatingTvFormulaReplayError(
                f'{label} содержит некорректную причину.',
            )
        result[reason] = _require_nonnegative_int(
            count,
            f'{label}.{reason}',
        )
    if list(value) != sorted(value):
        raise RatingTvFormulaReplayError(
            f'{label} должен быть отсортирован по коду.',
        )
    return result


def _validate_rating_period(value, label):
    value = _require_exact_keys(value, RATING_PERIOD_KEYS, label)
    period_id = value.get('id')
    if (
        not isinstance(period_id, int)
        or isinstance(period_id, bool)
        or period_id >= 0
    ):
        raise RatingTvFormulaReplayError(
            f'{label}.id должен быть отрицательным QA-ID.',
        )
    name = _require_nonempty_text(value.get('name'), f'{label}.name')
    if 'тест' not in name.casefold():
        raise RatingTvFormulaReplayError(
            f'{label}.name должен иметь тестовый маркер.',
        )
    starts_on = _parse_iso_date(value.get('starts_on'), f'{label}.starts_on')
    ends_before = _parse_iso_date(
        value.get('ends_before'),
        f'{label}.ends_before',
    )
    if ends_before <= starts_on:
        raise RatingTvFormulaReplayError(
            f'{label} имеет обратные календарные границы.',
        )
    if not isinstance(value.get('is_active'), bool):
        raise RatingTvFormulaReplayError(
            f'{label}.is_active должен быть логическим.',
        )
    return starts_on, ends_before


def _validate_watch_composition(value, label):
    value = _require_exact_keys(value, WATCH_COMPOSITION_KEYS, label)
    composition_id = value.get('id')
    if (
        not isinstance(composition_id, int)
        or isinstance(composition_id, bool)
        or composition_id >= 0
    ):
        raise RatingTvFormulaReplayError(
            f'{label}.id должен быть отрицательным QA-ID.',
        )
    code = _require_nonempty_text(value.get('code'), f'{label}.code')
    if not code.startswith('qa-'):
        raise RatingTvFormulaReplayError(
            f'{label}.code должен начинаться с qa-.',
        )
    name = _require_nonempty_text(value.get('name'), f'{label}.name')
    if 'тест' not in name.casefold():
        raise RatingTvFormulaReplayError(
            f'{label}.name должен иметь тестовый маркер.',
        )
    if not isinstance(value.get('is_active'), bool):
        raise RatingTvFormulaReplayError(
            f'{label}.is_active должен быть логическим.',
        )


def _validate_cohort(value):
    if (
        not isinstance(value, list)
        or len(value) != RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT
    ):
        raise RatingTvFormulaReplayError(
            'Formula replay cohort должен содержать ровно 53 сотрудника.',
        )
    result = {}
    for index, item in enumerate(value, start=1):
        item = _require_exact_keys(
            item,
            COHORT_ENTRY_KEYS,
            f'cohort[{index}]',
        )
        employee_id = item.get('employee_id')
        if (
            not isinstance(employee_id, int)
            or isinstance(employee_id, bool)
            or employee_id >= 0
            or employee_id in result
        ):
            raise RatingTvFormulaReplayError(
                'Cohort содержит неверный или повторный отрицательный QA-ID.',
            )
        full_name = _require_nonempty_text(
            item.get('full_name'),
            f'cohort[{index}].full_name',
        )
        if not full_name.startswith('ТЕСТ_'):
            raise RatingTvFormulaReplayError(
                'Каждое ФИО cohort должно начинаться с ТЕСТ_.',
            )
        result[employee_id] = full_name
    return result


def _validate_weights(value, label):
    value = _require_exact_keys(value, WEIGHT_KEYS, label)
    expected = {
        key: str(weight)
        for key, weight in DRIVER_RATING_WEIGHTS.items()
    }
    if value != expected:
        raise RatingTvFormulaReplayError(
            f'{label} не совпадает с рабочими весами формулы.',
        )


def _validate_distance_metrics(value, label):
    value = _require_exact_keys(value, DISTANCE_METRIC_KEYS, label)
    if (
        value.get('weight') != '0'
        or value.get('status') != 'planned'
        or value.get('label') != 'м³·км и т·км пока не учитываются'
    ):
        raise RatingTvFormulaReplayError(
            f'{label} искажает нулевой вес дистанционных метрик.',
        )


def _validate_linkage_audit(value, label):
    value = _require_exact_keys(value, LINKAGE_AUDIT_KEYS, label)
    for key in LINKAGE_AUDIT_KEYS - {'linkage_ready'}:
        _require_nonnegative_int(value.get(key), f'{label}.{key}')
    if not isinstance(value.get('linkage_ready'), bool):
        raise RatingTvFormulaReplayError(
            f'{label}.linkage_ready должен быть логическим.',
        )
    return value


def _validate_blocks(value, label):
    value = _require_exact_keys(value, WEIGHT_KEYS, label)
    return {
        key: _parse_score_4(value.get(key), f'{label}.{key}')
        for key in DRIVER_RATING_WEIGHTS
    }


def _validate_source_shift_ids(value, label):
    if not isinstance(value, list):
        raise RatingTvFormulaReplayError(f'{label} должен быть списком.')
    if any(
        not isinstance(item, int)
        or isinstance(item, bool)
        or item >= 0
        for item in value
    ):
        raise RatingTvFormulaReplayError(
            f'{label} должен содержать только отрицательные QA-ID.',
        )
    if value != sorted(set(value)):
        raise RatingTvFormulaReplayError(
            f'{label} должен быть отсортирован и не иметь дублей.',
        )
    return tuple(value)


def _validate_entry(entry, *, day, cohort):
    entry = _require_exact_keys(
        entry,
        ENTRY_KEYS,
        f'Строка сотрудника дня {day}',
    )
    employee_id = entry.get('employee_id')
    if employee_id not in cohort:
        raise RatingTvFormulaReplayError(
            f'employee_id={employee_id} дня {day} отсутствует в cohort.',
        )
    if entry.get('full_name') != cohort[employee_id]:
        raise RatingTvFormulaReplayError(
            f'ФИО employee_id={employee_id} дня {day} изменилось.',
        )
    equipment = entry.get('equipment')
    if (
        not isinstance(equipment, list)
        or any(
            not isinstance(item, str) or not item.strip()
            for item in equipment
        )
        or equipment != sorted(set(equipment))
    ):
        raise RatingTvFormulaReplayError(
            f'Техника employee_id={employee_id} дня {day} некорректна.',
        )
    row_status = entry.get('row_status')
    if row_status not in ROW_STATUSES:
        raise RatingTvFormulaReplayError(
            f'Статус employee_id={employee_id} дня {day} неизвестен.',
        )
    ranking_eligible = entry.get('ranking_eligible')
    if not isinstance(ranking_eligible, bool):
        raise RatingTvFormulaReplayError(
            f'ranking_eligible employee_id={employee_id} должен быть bool.',
        )
    shift_count = _require_nonnegative_int(
        entry.get('shift_count'),
        f'shift_count employee_id={employee_id} дня {day}',
    )
    withheld_shift_count = _require_nonnegative_int(
        entry.get('withheld_shift_count'),
        f'withheld_shift_count employee_id={employee_id} дня {day}',
    )
    if withheld_shift_count > shift_count:
        raise RatingTvFormulaReplayError(
            f'Удержанных смен больше всех смен employee_id={employee_id}.',
        )
    withheld_reasons = _validate_reason_map(
        entry.get('withheld_reasons'),
        f'withheld_reasons employee_id={employee_id} дня {day}',
    )
    quality_flags = _validate_code_list(
        entry.get('quality_flags'),
        f'quality_flags employee_id={employee_id} дня {day}',
    )
    quality_flags_status = entry.get('quality_flags_status')
    if quality_flags_status not in QUALITY_FLAGS_STATUSES:
        raise RatingTvFormulaReplayError(
            f'quality_flags_status employee_id={employee_id} неизвестен.',
        )
    if (
        quality_flags_status == QUALITY_FLAGS_STATUS_NOT_EXPOSED
        and quality_flags
    ):
        raise RatingTvFormulaReplayError(
            f'Неэкспортированные quality_flags employee_id={employee_id} '
            'не должны содержать придуманные значения.',
        )
    source_shift_ids = _validate_source_shift_ids(
        entry.get('source_shift_ids'),
        f'source_shift_ids employee_id={employee_id} дня {day}',
    )
    display_order = _require_positive_int(
        entry.get('display_order'),
        f'display_order employee_id={employee_id} дня {day}',
    )
    level = entry.get('level')
    if not isinstance(level, str):
        raise RatingTvFormulaReplayError(
            f'level employee_id={employee_id} дня {day} должен быть строкой.',
        )

    result = {
        'employee_id': employee_id,
        'row_status': row_status,
        'shift_count': shift_count,
        'withheld_shift_count': withheld_shift_count,
        'withheld_reasons': withheld_reasons,
        'quality_flags': quality_flags,
        'quality_flags_status': quality_flags_status,
        'source_shift_ids': source_shift_ids,
        'display_order': display_order,
        'place': entry.get('place'),
        'shared_score_place': entry.get('shared_score_place'),
        'level': level,
        'score': None,
        'trip_count': entry.get('trip_count'),
        'volume_m3': None,
        'tonnage_t': None,
        'position_delta': entry.get('position_delta'),
    }

    nullable_kpi_fields = (
        'trip_count',
        'volume_m3',
        'tonnage_t',
        'score',
        'blocks',
        'confidence',
        'place',
        'shared_score_place',
        'position_delta',
    )
    if row_status != ROW_STATUS_RATED:
        if ranking_eligible is not False:
            raise RatingTvFormulaReplayError(
                f'Неоценённая строка employee_id={employee_id} '
                'не может участвовать в ранжировании.',
            )
        if any(entry.get(field) is not None for field in nullable_kpi_fields):
            raise RatingTvFormulaReplayError(
                f'Неоценённая строка employee_id={employee_id} '
                'не может содержать KPI, место или движение.',
            )
        if level:
            raise RatingTvFormulaReplayError(
                f'Неоценённая строка employee_id={employee_id} '
                'не может иметь премиальный уровень.',
            )
        if row_status == ROW_STATUS_WITHHELD:
            if quality_flags_status == QUALITY_FLAGS_STATUS_NOT_APPLICABLE:
                raise RatingTvFormulaReplayError(
                    f'Удержанная строка employee_id={employee_id} '
                    'должна явно описывать доступность quality_flags.',
                )
            if shift_count < 1 or withheld_shift_count < 1:
                raise RatingTvFormulaReplayError(
                    f'Удержанная строка employee_id={employee_id} '
                    'должна иметь удержанные смены.',
                )
            if (
                sum(withheld_reasons.values()) != withheld_shift_count
                or not any(withheld_reasons.values())
            ):
                raise RatingTvFormulaReplayError(
                    f'Причины удержания employee_id={employee_id} '
                    'не совпадают с количеством удержанных смен.',
                )
            if len(source_shift_ids) != shift_count:
                raise RatingTvFormulaReplayError(
                    f'Число source_shift_ids employee_id={employee_id} '
                    'не совпадает с числом смен.',
                )
        else:
            if (
                shift_count != 0
                or withheld_shift_count != 0
                or withheld_reasons
                or quality_flags
                or source_shift_ids
            ):
                raise RatingTvFormulaReplayError(
                    f'Строка not_observed employee_id={employee_id} '
                    'не может содержать данные смены или качества.',
                )
            if (
                quality_flags_status
                != QUALITY_FLAGS_STATUS_NOT_APPLICABLE
            ):
                raise RatingTvFormulaReplayError(
                    f'Строка not_observed employee_id={employee_id} '
                    'должна иметь quality_flags_status=not_applicable.',
                )
        return result

    if ranking_eligible is not True:
        raise RatingTvFormulaReplayError(
            f'Оценённая строка employee_id={employee_id} '
            'должна участвовать в ранжировании.',
        )
    if shift_count < 1 or withheld_shift_count != 0 or withheld_reasons:
        raise RatingTvFormulaReplayError(
            f'Оценённая строка employee_id={employee_id} '
            'имеет некорректную семантику смен или удержаний.',
        )
    if DRIVER_RATING_BLOCKING_FLAGS.intersection(quality_flags):
        raise RatingTvFormulaReplayError(
            f'Оценённая строка employee_id={employee_id} '
            'содержит блокирующий флаг качества.',
        )
    if quality_flags_status == QUALITY_FLAGS_STATUS_NOT_APPLICABLE:
        raise RatingTvFormulaReplayError(
            f'Оценённая строка employee_id={employee_id} '
            'не может иметь quality_flags_status=not_applicable.',
        )
    if len(source_shift_ids) != shift_count:
        raise RatingTvFormulaReplayError(
            f'Число source_shift_ids employee_id={employee_id} '
            'не совпадает с числом оценённых смен.',
        )
    trip_count = _require_nonnegative_int(
        entry.get('trip_count'),
        f'trip_count employee_id={employee_id} дня {day}',
    )
    volume_m3 = _parse_nonnegative_2(
        entry.get('volume_m3'),
        f'volume_m3 employee_id={employee_id} дня {day}',
    )
    tonnage_t = _parse_nonnegative_2(
        entry.get('tonnage_t'),
        f'tonnage_t employee_id={employee_id} дня {day}',
    )
    score = _parse_score_4(
        entry.get('score'),
        f'score employee_id={employee_id} дня {day}',
    )
    _validate_blocks(
        entry.get('blocks'),
        f'blocks employee_id={employee_id} дня {day}',
    )
    _parse_score_4(
        entry.get('confidence'),
        f'confidence employee_id={employee_id} дня {day}',
    )
    place = _require_positive_int(
        entry.get('place'),
        f'place employee_id={employee_id} дня {day}',
    )
    shared_score_place = _require_positive_int(
        entry.get('shared_score_place'),
        f'shared_score_place employee_id={employee_id} дня {day}',
    )
    if shared_score_place != place:
        raise RatingTvFormulaReplayError(
            f'Общее место employee_id={employee_id} не совпадает с местом.',
        )
    position_delta = entry.get('position_delta')
    if position_delta is not None and (
        not isinstance(position_delta, int)
        or isinstance(position_delta, bool)
        or abs(position_delta) >= RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT
    ):
        raise RatingTvFormulaReplayError(
            f'position_delta employee_id={employee_id} некорректен.',
        )
    result.update({
        'place': place,
        'score': score,
        'trip_count': trip_count,
        'volume_m3': volume_m3,
        'tonnage_t': tonnage_t,
        'position_delta': position_delta,
    })
    return result


def _validate_calculation_summary(value, label):
    value = _require_exact_keys(value, CALCULATION_SUMMARY_KEYS, label)
    return {
        'employee_count': _require_nonnegative_int(
            value.get('employee_count'),
            f'{label}.employee_count',
        ),
        'rated_shift_count': _require_nonnegative_int(
            value.get('rated_shift_count'),
            f'{label}.rated_shift_count',
        ),
        'withheld_shift_count': _require_nonnegative_int(
            value.get('withheld_shift_count'),
            f'{label}.withheld_shift_count',
        ),
        'withheld_reasons': _validate_reason_map(
            value.get('withheld_reasons'),
            f'{label}.withheld_reasons',
        ),
        'trip_count': _require_nonnegative_int(
            value.get('trip_count'),
            f'{label}.trip_count',
        ),
        'volume_m3': _parse_nonnegative_2(
            value.get('volume_m3'),
            f'{label}.volume_m3',
        ),
        'tonnage_t': _parse_nonnegative_2(
            value.get('tonnage_t'),
            f'{label}.tonnage_t',
        ),
    }


def _validate_display_summary(value, label):
    value = _require_exact_keys(value, DISPLAY_SUMMARY_KEYS, label)
    return {
        key: _require_nonnegative_int(value.get(key), f'{label}.{key}')
        for key in DISPLAY_SUMMARY_KEYS
    }


def validate_rating_tv_formula_replay(document: dict[str, Any]) -> None:
    canonical_json_bytes(document)
    _reject_forbidden_personnel_fields(document)
    document = _require_exact_keys(
        document,
        DOCUMENT_KEYS,
        'Formula replay',
    )
    if document.get('schema') != RATING_TV_FORMULA_REPLAY_SCHEMA:
        raise RatingTvFormulaReplayError(
            'Название схемы formula replay не поддерживается.',
        )
    if (
        document.get('schema_version')
        != RATING_TV_FORMULA_REPLAY_SCHEMA_VERSION
    ):
        raise RatingTvFormulaReplayError(
            'Версия схемы formula replay не поддерживается.',
        )
    if (
        document.get('data_classification')
        != RATING_TV_FORMULA_REPLAY_DATA_CLASSIFICATION
    ):
        raise RatingTvFormulaReplayError(
            'Formula replay не имеет изолированной QA-классификации.',
        )
    for field, expected in (
        ('synthetic', True),
        ('formula_evaluated', True),
        ('official', False),
        ('official_rating_eligible', False),
    ):
        if document.get(field) is not expected:
            raise RatingTvFormulaReplayError(
                f'Formula replay имеет неверное поле {field}.',
            )
    _require_nonempty_text(document.get('warning'), 'warning')

    integrity = _require_exact_keys(
        document.get('integrity'),
        INTEGRITY_KEYS,
        'integrity',
    )
    if integrity.get('algorithm') != 'SHA-256':
        raise RatingTvFormulaReplayError(
            'Formula replay поддерживает только SHA-256.',
        )
    if integrity.get('canonicalization') != 'json-sort-keys-utf8-v1':
        raise RatingTvFormulaReplayError(
            'Способ канонизации formula replay не поддерживается.',
        )
    expected_canonical_sha256 = _require_sha256(
        integrity.get('canonical_sha256'),
        'integrity.canonical_sha256',
    )
    if expected_canonical_sha256 != canonical_formula_replay_sha256(document):
        raise RatingTvFormulaReplayError(
            'Внутренняя контрольная сумма formula replay не совпала.',
        )

    replay = _require_exact_keys(
        document.get('replay'),
        REPLAY_KEYS,
        'replay',
    )
    replay_id = _require_nonempty_text(replay.get('id'), 'replay.id')
    _require_nonempty_text(replay.get('label'), 'replay.label')
    _require_nonempty_text(
        replay.get('scenario_version'),
        'replay.scenario_version',
    )
    if replay.get('rating_mode') != RATING_TV_FORMULA_REPLAY_MODE:
        raise RatingTvFormulaReplayError(
            'Formula replay имеет неверный rating_mode.',
        )
    for field, expected in (
        ('synthetic', True),
        ('formula_evaluated', True),
        ('official', False),
    ):
        if replay.get(field) is not expected:
            raise RatingTvFormulaReplayError(
                f'replay.{field} имеет неверное значение.',
            )
    if replay.get('day_count') != RATING_TV_FORMULA_REPLAY_DAY_COUNT:
        raise RatingTvFormulaReplayError(
            'Formula replay должен иметь ровно 30 дней.',
        )
    if (
        replay.get('expected_employee_count')
        != RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT
    ):
        raise RatingTvFormulaReplayError(
            'Formula replay должен иметь ровно 53 сотрудника.',
        )
    if replay.get('initial_day') != 1:
        raise RatingTvFormulaReplayError(
            'Formula replay должен начинаться с первого дня.',
        )
    base_step_ms = replay.get('base_step_ms')
    if (
        not isinstance(base_step_ms, int)
        or isinstance(base_step_ms, bool)
        or not 250 <= base_step_ms <= 60_000
    ):
        raise RatingTvFormulaReplayError(
            'replay.base_step_ms вышел за допустимый диапазон.',
        )
    _parse_iso_datetime(replay.get('created_at'), 'replay.created_at')
    formula_version = _require_nonempty_text(
        replay.get('formula_version'),
        'replay.formula_version',
    )
    if formula_version != DRIVER_RATING_FORMULA_VERSION:
        raise RatingTvFormulaReplayError(
            'Formula replay рассчитан неподдерживаемой версией формулы.',
        )
    formula_label = _require_nonempty_text(
        replay.get('formula_label'),
        'replay.formula_label',
    )
    _require_nonempty_text(replay.get('timezone'), 'replay.timezone')
    _require_nonempty_text(replay.get('source_commit'), 'replay.source_commit')
    _require_nonempty_text(replay.get('source_run_id'), 'replay.source_run_id')
    _require_sha256(
        replay.get('source_manifest_sha256'),
        'replay.source_manifest_sha256',
    )
    if (
        replay.get('source_database_classification')
        != 'isolated_synthetic_postgresql_qa'
    ):
        raise RatingTvFormulaReplayError(
            'Formula replay должен происходить из изолированной '
            'синтетической PostgreSQL QA-базы.',
        )
    _require_nonempty_text(replay.get('notice'), 'replay.notice')

    scope = _require_exact_keys(document.get('scope'), SCOPE_KEYS, 'scope')
    if (
        scope.get('scope_type') != 'rating_period'
        or scope.get('profession') != 'driver'
        or not isinstance(scope.get('profession_label'), str)
        or scope.get('shift_type') not in {'day', 'night'}
        or not isinstance(scope.get('shift_type_label'), str)
    ):
        raise RatingTvFormulaReplayError(
            'Formula replay имеет неверную фиксированную область.',
        )
    rating_period = _require_dict(
        scope.get('rating_period'),
        'scope.rating_period',
    )
    period_start, period_end = _validate_rating_period(
        rating_period,
        'scope.rating_period',
    )
    if period_end - period_start != timedelta(
        days=RATING_TV_FORMULA_REPLAY_DAY_COUNT,
    ):
        raise RatingTvFormulaReplayError(
            'Formula replay должен покрывать ровно 30 календарных дней.',
        )
    watch_composition = _require_dict(
        scope.get('watch_composition'),
        'scope.watch_composition',
    )
    _validate_watch_composition(
        watch_composition,
        'scope.watch_composition',
    )
    cohort = scope.get('cohort')
    cohort_by_id = _validate_cohort(cohort)
    expected_cohort_sha256 = _require_sha256(
        scope.get('cohort_sha256'),
        'scope.cohort_sha256',
    )
    if expected_cohort_sha256 != formula_replay_cohort_sha256(cohort):
        raise RatingTvFormulaReplayError(
            'Контрольная сумма cohort не совпала.',
        )

    snapshots = document.get('snapshots')
    if (
        not isinstance(snapshots, list)
        or len(snapshots) != RATING_TV_FORMULA_REPLAY_DAY_COUNT
    ):
        raise RatingTvFormulaReplayError(
            'Formula replay должен содержать ровно 30 snapshots.',
        )
    expected_chain_sha256 = _require_sha256(
        integrity.get('snapshot_chain_sha256'),
        'integrity.snapshot_chain_sha256',
    )
    if expected_chain_sha256 != formula_replay_snapshot_chain_sha256(
        snapshots,
    ):
        raise RatingTvFormulaReplayError(
            'Цепочка formula replay snapshots повреждена.',
        )

    previous_payload_sha256 = None
    previous_as_of = None
    previous_rated_places = {}
    previous_statuses = {}
    previous_shift_counts = {
        employee_id: 0
        for employee_id in cohort_by_id
    }
    for expected_day, snapshot in enumerate(snapshots, start=1):
        snapshot = _require_exact_keys(
            snapshot,
            SNAPSHOT_KEYS,
            f'snapshot дня {expected_day}',
        )
        if snapshot.get('day') != expected_day:
            raise RatingTvFormulaReplayError(
                'Дни formula replay должны идти строго 1–30.',
            )
        expected_work_date = period_start + timedelta(days=expected_day - 1)
        work_date = _parse_iso_date(
            snapshot.get('work_date'),
            f'snapshot[{expected_day}].work_date',
        )
        if work_date != expected_work_date:
            raise RatingTvFormulaReplayError(
                f'work_date дня {expected_day} не соответствует периоду.',
            )
        as_of = _parse_iso_datetime(
            snapshot.get('as_of'),
            f'snapshot[{expected_day}].as_of',
        )
        if previous_as_of is not None and as_of <= previous_as_of:
            raise RatingTvFormulaReplayError(
                'Время snapshots должно строго возрастать.',
            )
        previous_as_of = as_of
        if (
            snapshot.get('previous_payload_sha256')
            != previous_payload_sha256
        ):
            raise RatingTvFormulaReplayError(
                f'Цепочка payload нарушена в дне {expected_day}.',
            )
        payload = _require_exact_keys(
            snapshot.get('payload'),
            PAYLOAD_KEYS,
            f'payload дня {expected_day}',
        )
        payload_sha256 = _require_sha256(
            snapshot.get('payload_sha256'),
            f'snapshot[{expected_day}].payload_sha256',
        )
        if payload_sha256 != formula_replay_payload_sha256(payload):
            raise RatingTvFormulaReplayError(
                f'Контрольная сумма payload дня {expected_day} не совпала.',
            )
        previous_payload_sha256 = payload_sha256

        for field, expected in (
            ('available', True),
            ('official', False),
            ('official_rating_eligible', False),
            ('synthetic', True),
            ('formula_evaluated', True),
            ('rating_mode', RATING_TV_FORMULA_REPLAY_MODE),
            ('scope_type', 'rating_period'),
            ('formula_version', formula_version),
            ('formula_label', formula_label),
            ('rating_period', rating_period),
            ('watch_composition', watch_composition),
            ('shift_type', scope.get('shift_type')),
            ('shift_type_label', scope.get('shift_type_label')),
            ('available_rating_periods', [rating_period]),
            ('available_watch_compositions', [watch_composition]),
            ('qa_day', expected_day),
            ('qa_day_count', RATING_TV_FORMULA_REPLAY_DAY_COUNT),
            ('qa_work_date', work_date.isoformat()),
            ('replay_run_id', replay_id),
        ):
            if payload.get(field) != expected:
                raise RatingTvFormulaReplayError(
                    f'Поле payload.{field} неверно в дне {expected_day}.',
                )
        calculation_available = payload.get('calculation_available')
        if not isinstance(calculation_available, bool):
            raise RatingTvFormulaReplayError(
                'payload.calculation_available должен быть логическим.',
            )
        _require_nonempty_text(
            payload.get('status'),
            f'payload.status дня {expected_day}',
        )
        _parse_iso_datetime(
            payload.get('generated_at'),
            f'payload.generated_at дня {expected_day}',
        )
        source_raw_path = _require_nonempty_text(
            payload.get('source_raw_path'),
            f'payload.source_raw_path дня {expected_day}',
        )
        expected_raw_path = (
            f'raw_formula/{scope.get("shift_type")}/'
            f'day_{expected_day:02d}.json'
        )
        if source_raw_path != expected_raw_path:
            raise RatingTvFormulaReplayError(
                f'Путь raw formula дня {expected_day} вышел за fixed scope.',
            )
        _require_sha256(
            payload.get('source_raw_sha256'),
            f'payload.source_raw_sha256 дня {expected_day}',
        )
        source_fingerprint = payload.get('source_fingerprint')
        shift_score_fingerprint = payload.get('shift_score_fingerprint')
        if calculation_available:
            _require_sha256(
                source_fingerprint,
                f'payload.source_fingerprint дня {expected_day}',
            )
            _require_sha256(
                shift_score_fingerprint,
                f'payload.shift_score_fingerprint дня {expected_day}',
            )
        elif (
            source_fingerprint is not None
            or shift_score_fingerprint is not None
        ):
            raise RatingTvFormulaReplayError(
                'Удержанный общий расчёт не должен иметь ложные fingerprints.',
            )

        calculation_window = _require_exact_keys(
            payload.get('calculation_window'),
            CALCULATION_WINDOW_KEYS,
            f'payload.calculation_window дня {expected_day}',
        )
        calculation_start = _parse_iso_date(
            calculation_window.get('starts_on'),
            f'calculation_window.starts_on дня {expected_day}',
        )
        calculation_end = _parse_iso_date(
            calculation_window.get('ends_before'),
            f'calculation_window.ends_before дня {expected_day}',
        )
        if (
            calculation_start != period_start
            or calculation_end != work_date + timedelta(days=1)
        ):
            raise RatingTvFormulaReplayError(
                f'День {expected_day} не является накопительным расчётом.',
            )
        _validate_weights(payload.get('weights'), f'weights дня {expected_day}')
        _validate_distance_metrics(
            payload.get('distance_metrics'),
            f'distance_metrics дня {expected_day}',
        )
        linkage_audit = _validate_linkage_audit(
            payload.get('linkage_audit'),
            f'linkage_audit дня {expected_day}',
        )
        if calculation_available and not linkage_audit['linkage_ready']:
            raise RatingTvFormulaReplayError(
                'Доступный формульный расчёт не может иметь linkage_ready=false.',
            )

        entries = payload.get('entries')
        if (
            not isinstance(entries, list)
            or len(entries) != RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT
        ):
            raise RatingTvFormulaReplayError(
                f'Payload дня {expected_day} должен иметь ровно 53 строки.',
            )
        validated_entries = [
            _validate_entry(
                entry,
                day=expected_day,
                cohort=cohort_by_id,
            )
            for entry in entries
        ]
        employee_ids = [entry['employee_id'] for entry in validated_entries]
        if set(employee_ids) != set(cohort_by_id) or len(set(employee_ids)) != len(
            employee_ids
        ):
            raise RatingTvFormulaReplayError(
                f'Набор сотрудников дня {expected_day} не совпадает с cohort.',
            )
        display_orders = {
            entry['display_order']
            for entry in validated_entries
        }
        if display_orders != set(
            range(1, RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT + 1),
        ):
            raise RatingTvFormulaReplayError(
                f'display_order дня {expected_day} должен быть 1–53.',
            )
        ordered_entries = sorted(
            validated_entries,
            key=lambda entry: entry['display_order'],
        )
        rated_entries = [
            entry
            for entry in ordered_entries
            if entry['row_status'] == ROW_STATUS_RATED
        ]
        if ordered_entries[:len(rated_entries)] != rated_entries:
            raise RatingTvFormulaReplayError(
                f'Оценённые строки дня {expected_day} должны идти первыми.',
            )
        previous_score = None
        dense_place = 0
        current_rated_places = {}
        current_statuses = {}
        for entry in ordered_entries:
            employee_id = entry['employee_id']
            row_status = entry['row_status']
            current_statuses[employee_id] = row_status
            if entry['shift_count'] < previous_shift_counts[employee_id]:
                raise RatingTvFormulaReplayError(
                    f'Число смен employee_id={employee_id} уменьшилось.',
                )
            previous_shift_counts[employee_id] = entry['shift_count']
            if row_status != ROW_STATUS_RATED:
                continue
            score = entry['score']
            if previous_score is not None and score > previous_score:
                raise RatingTvFormulaReplayError(
                    f'Баллы дня {expected_day} должны идти по убыванию.',
                )
            if previous_score is None or score != previous_score:
                dense_place += 1
            expected_level = DRIVER_RATING_LEVELS.get(dense_place, '')
            if (
                entry['place'] != dense_place
                or entry['shared_score_place'] != dense_place
                or entry['level'] != expected_level
            ):
                raise RatingTvFormulaReplayError(
                    f'Dense-place employee_id={employee_id} '
                    f'дня {expected_day} некорректен.',
                )
            expected_delta = (
                previous_rated_places[employee_id] - dense_place
                if previous_statuses.get(employee_id) == ROW_STATUS_RATED
                else None
            )
            if entry.get('position_delta') != expected_delta:
                raise RatingTvFormulaReplayError(
                    f'position_delta employee_id={employee_id} '
                    f'дня {expected_day} некорректен.',
                )
            current_rated_places[employee_id] = dense_place
            previous_score = score

        calculation_summary = _validate_calculation_summary(
            payload.get('calculation_summary'),
            f'calculation_summary дня {expected_day}',
        )
        display_summary = _validate_display_summary(
            payload.get('display_summary'),
            f'display_summary дня {expected_day}',
        )
        status_counts = {
            status: sum(
                entry['row_status'] == status
                for entry in validated_entries
            )
            for status in ROW_STATUSES
        }
        if display_summary != {
            'cohort_employee_count': RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT,
            'rated_employee_count': status_counts[ROW_STATUS_RATED],
            'withheld_employee_count': status_counts[ROW_STATUS_WITHHELD],
            'not_observed_employee_count': status_counts[
                ROW_STATUS_NOT_OBSERVED
            ],
        }:
            raise RatingTvFormulaReplayError(
                f'display_summary дня {expected_day} не совпадает со строками.',
            )
        if calculation_available != bool(rated_entries):
            raise RatingTvFormulaReplayError(
                f'calculation_available дня {expected_day} '
                'не совпадает с наличием оценённых строк.',
            )
        aggregated_reasons = {}
        for entry in validated_entries:
            for reason, count in entry['withheld_reasons'].items():
                aggregated_reasons[reason] = (
                    aggregated_reasons.get(reason, 0) + count
                )
        positive_summary_reasons = {
            reason: count
            for reason, count in calculation_summary[
                'withheld_reasons'
            ].items()
            if count
        }
        if aggregated_reasons != positive_summary_reasons:
            raise RatingTvFormulaReplayError(
                f'Причины удержаний дня {expected_day} '
                'не совпадают с calculation_summary.',
            )
        expected_summary = {
            'employee_count': len(rated_entries),
            'rated_shift_count': sum(
                entry['shift_count']
                for entry in rated_entries
            ),
            'withheld_shift_count': sum(
                entry['withheld_shift_count']
                for entry in validated_entries
            ),
            'trip_count': sum(
                entry['trip_count']
                for entry in rated_entries
            ),
            'volume_m3': sum(
                (entry['volume_m3'] for entry in rated_entries),
                Decimal('0'),
            ),
            'tonnage_t': sum(
                (entry['tonnage_t'] for entry in rated_entries),
                Decimal('0'),
            ),
        }
        for key, expected in expected_summary.items():
            if calculation_summary[key] != expected:
                raise RatingTvFormulaReplayError(
                    f'calculation_summary.{key} дня {expected_day} '
                    'не совпадает со строками.',
                )
        previous_rated_places = current_rated_places
        previous_statuses = current_statuses


def load_rating_tv_formula_replay(path, *, expected_sha256):
    expected_sha256 = _require_sha256(
        str(expected_sha256 or '').upper(),
        'Ожидаемая внешняя SHA-256 formula replay',
    )
    artifact_path = Path(path).expanduser().resolve()
    try:
        raw = artifact_path.read_bytes()
    except OSError as error:
        raise RatingTvFormulaReplayError(
            'Formula replay-артефакт не найден или недоступен.',
        ) from error
    if len(raw) > RATING_TV_FORMULA_REPLAY_MAX_BYTES:
        raise RatingTvFormulaReplayError(
            'Formula replay-артефакт превышает допустимый размер.',
        )
    raw_sha256 = hashlib.sha256(raw).hexdigest().upper()
    if raw_sha256 != expected_sha256:
        raise RatingTvFormulaReplayError(
            'Внешняя контрольная сумма formula replay не совпала.',
        )
    document = _strict_json_loads(raw)
    validate_rating_tv_formula_replay(document)
    return deepcopy(document), raw_sha256
