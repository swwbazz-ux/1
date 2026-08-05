from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .driver_watch_rating import DRIVER_RATING_LEVELS


RATING_TV_REPLAY_SCHEMA = 'copper.driver-rating-replay'
RATING_TV_REPLAY_SCHEMA_VERSION = 1
RATING_TV_REPLAY_DAY_COUNT = 30
RATING_TV_REPLAY_EMPLOYEE_COUNT = 53
RATING_TV_REPLAY_DATA_CLASSIFICATION = 'synthetic_qa_only'
RATING_TV_REPLAY_MAX_BYTES = 5 * 1024 * 1024

SHA256_RE = re.compile(r'^[0-9A-F]{64}$')
SCORE_RE = re.compile(r'^(?:0|[1-9][0-9]{0,2})\.[0-9]{2}$')
FORBIDDEN_PERSONNEL_FIELDS = {
    'access_code',
    'email',
    'personnel_number',
    'phone',
    'photo',
    'photo_url',
    'pin',
}
DOCUMENT_KEYS = {
    'schema',
    'schema_version',
    'data_classification',
    'synthetic',
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
    'seed',
    'source_commit',
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
    'source_fingerprint',
    'rating_period',
    'watch_composition',
    'shift_type',
    'shift_type_label',
    'available_rating_periods',
    'available_watch_compositions',
    'summary',
    'entries',
    'qa_day',
    'qa_day_count',
    'qa_work_date',
    'replay_run_id',
}
SUMMARY_KEYS = {
    'employee_count',
    'rated_shift_count',
    'withheld_shift_count',
    'withheld_reasons',
}
ENTRY_KEYS = {
    'employee_id',
    'full_name',
    'equipment',
    'shift_count',
    'score',
    'place',
    'shared_score_place',
    'display_order',
    'level',
    'position_delta',
}


class RatingTvReplayError(ValueError):
    """Артефакт сохранённого QA-воспроизведения непригоден."""


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
        raise RatingTvReplayError(
            'Replay содержит значение вне строгого JSON-контракта.',
        ) from error


def canonical_replay_bytes(document: dict[str, Any]) -> bytes:
    if not isinstance(document, dict):
        raise RatingTvReplayError('Корень replay-артефакта должен быть объектом.')
    return canonical_json_bytes({
        key: value
        for key, value in document.items()
        if key != 'integrity'
    })


def canonical_replay_sha256(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_replay_bytes(document)).hexdigest().upper()


def replay_payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest().upper()


def replay_snapshot_source_fingerprint(replay_id, day, entries):
    return hashlib.sha256(canonical_json_bytes({
        'replay_id': replay_id,
        'day': day,
        'entries': entries,
    })).hexdigest().upper()


def replay_snapshot_chain_sha256(snapshots):
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
    return hashlib.sha256(
        canonical_json_bytes(chain_rows),
    ).hexdigest().upper()


def attach_replay_integrity(document: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(document)
    snapshots = result.get('snapshots')
    if not isinstance(snapshots, list):
        raise RatingTvReplayError('snapshots должен быть массивом.')
    previous_payload_sha256 = None
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            raise RatingTvReplayError('Каждый replay-снимок должен быть объектом.')
        payload = snapshot.get('payload')
        if not isinstance(payload, dict):
            raise RatingTvReplayError('Каждый replay-снимок должен иметь payload.')
        snapshot['previous_payload_sha256'] = previous_payload_sha256
        snapshot['payload_sha256'] = replay_payload_sha256(payload)
        previous_payload_sha256 = snapshot['payload_sha256']
    result['integrity'] = {
        'algorithm': 'sha256',
        'canonicalization': 'json-sort-keys-utf8-v1',
        'snapshot_chain_sha256': replay_snapshot_chain_sha256(snapshots),
        'canonical_sha256': canonical_replay_sha256(result),
    }
    return result


def _require_dict(value, label):
    if not isinstance(value, dict):
        raise RatingTvReplayError(f'{label} должен быть объектом.')
    return value


def _require_exact_keys(value, expected_keys, label):
    value = _require_dict(value, label)
    actual_keys = set(value)
    missing = expected_keys - actual_keys
    unexpected = actual_keys - expected_keys
    if missing or unexpected:
        details = []
        if missing:
            details.append('нет полей: ' + ', '.join(sorted(missing)))
        if unexpected:
            details.append(
                'лишние поля: ' + ', '.join(sorted(unexpected)),
            )
        raise RatingTvReplayError(
            f'{label} нарушает строгий список полей ({"; ".join(details)}).',
        )
    return value


def _require_nonempty_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RatingTvReplayError(f'{label} должен быть непустой строкой.')
    return value


def _require_nonnegative_int(value, label):
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise RatingTvReplayError(
            f'{label} должен быть неотрицательным целым числом.',
        )
    return value


def _require_sha256(value, label):
    value = _require_nonempty_text(value, label).upper()
    if not SHA256_RE.fullmatch(value):
        raise RatingTvReplayError(f'{label} не является SHA-256.')
    return value


def _parse_iso_date(value, label):
    try:
        return date.fromisoformat(_require_nonempty_text(value, label))
    except ValueError as error:
        raise RatingTvReplayError(f'{label} имеет неверную дату.') from error


def _parse_iso_datetime(value, label):
    try:
        parsed = datetime.fromisoformat(
            _require_nonempty_text(value, label),
        )
    except ValueError as error:
        raise RatingTvReplayError(
            f'{label} имеет неверные дату и время.',
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RatingTvReplayError(f'{label} должен содержать часовой пояс.')
    return parsed


def _strict_json_loads(raw):
    def reject_constant(value):
        raise RatingTvReplayError(
            f'Запрещена нечисловая JSON-константа {value}.',
        )

    try:
        return json.loads(
            raw.decode('utf-8-sig'),
            parse_constant=reject_constant,
        )
    except RatingTvReplayError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RatingTvReplayError(
            'Replay-артефакт не является корректным JSON.',
        ) from error


def _validate_entry(
    entry,
    *,
    day,
    seen_employee_ids,
    seen_display_orders,
):
    entry = _require_exact_keys(
        entry,
        ENTRY_KEYS,
        f'Сотрудник дня {day}',
    )
    forbidden = FORBIDDEN_PERSONNEL_FIELDS.intersection(entry)
    if forbidden:
        raise RatingTvReplayError(
            'Replay содержит запрещённые кадровые поля: '
            + ', '.join(sorted(forbidden)),
        )

    employee_id = entry.get('employee_id')
    if (
        not isinstance(employee_id, int)
        or isinstance(employee_id, bool)
        or employee_id >= 0
    ):
        raise RatingTvReplayError(
            f'employee_id дня {day} должен быть отрицательным QA-ID.',
        )
    if employee_id in seen_employee_ids:
        raise RatingTvReplayError(
            f'Дублируется employee_id={employee_id} в снимке дня {day}.',
        )
    seen_employee_ids.add(employee_id)

    full_name = _require_nonempty_text(
        entry.get('full_name'),
        f'full_name сотрудника {employee_id} дня {day}',
    )
    if not full_name.startswith('ТЕСТ_'):
        raise RatingTvReplayError(
            f'ФИО сотрудника {employee_id} не имеет QA-маркера ТЕСТ_.',
        )
    equipment = entry.get('equipment')
    if (
        not isinstance(equipment, list)
        or not equipment
        or any(
            not isinstance(item, str) or not item.strip()
            for item in equipment
        )
    ):
        raise RatingTvReplayError(
            f'Некорректная техника сотрудника {employee_id} дня {day}.',
        )

    display_order = entry.get('display_order')
    if (
        not isinstance(display_order, int)
        or isinstance(display_order, bool)
        or display_order < 1
        or display_order in seen_display_orders
    ):
        raise RatingTvReplayError(
            f'Некорректный display_order в снимке дня {day}.',
        )
    seen_display_orders.add(display_order)

    place = entry.get('place')
    if (
        not isinstance(place, int)
        or isinstance(place, bool)
        or place < 1
    ):
        raise RatingTvReplayError(
            f'Некорректное место сотрудника {employee_id} дня {day}.',
        )
    shared_score_place = entry.get('shared_score_place')
    if (
        not isinstance(shared_score_place, int)
        or isinstance(shared_score_place, bool)
        or shared_score_place < 1
    ):
        raise RatingTvReplayError(
            f'Некорректное общее место сотрудника {employee_id} дня {day}.',
        )
    level = entry.get('level')
    if not isinstance(level, str):
        raise RatingTvReplayError(
            f'Некорректный уровень сотрудника {employee_id} дня {day}.',
        )
    score = entry.get('score')
    if not isinstance(score, str) or not SCORE_RE.fullmatch(score):
        raise RatingTvReplayError(
            f'Балл сотрудника {employee_id} дня {day} '
            'должен быть строкой с двумя знаками.',
        )
    try:
        decimal_score = Decimal(score)
    except InvalidOperation as error:
        raise RatingTvReplayError(
            f'Некорректный балл сотрудника {employee_id} дня {day}.',
        ) from error
    if not decimal_score.is_finite() or not Decimal('0') <= decimal_score <= 100:
        raise RatingTvReplayError(
            f'Балл сотрудника {employee_id} дня {day} вне диапазона.',
        )

    position_delta = entry.get('position_delta')
    if not isinstance(position_delta, int) or isinstance(position_delta, bool):
        raise RatingTvReplayError(
            f'Изменение места сотрудника {employee_id} дня {day} '
            'должно быть целым числом.',
        )
    shift_count = entry.get('shift_count')
    if (
        not isinstance(shift_count, int)
        or isinstance(shift_count, bool)
        or shift_count < 1
    ):
        raise RatingTvReplayError(
            f'Некорректное число смен сотрудника {employee_id} дня {day}.',
        )
    return {
        'employee_id': employee_id,
        'full_name': full_name,
        'equipment': tuple(equipment),
        'display_order': display_order,
        'place': place,
        'shared_score_place': shared_score_place,
        'score': decimal_score,
        'level': level,
        'position_delta': position_delta,
        'shift_count': shift_count,
    }


def validate_rating_tv_replay(document: dict[str, Any]) -> None:
    document = _require_exact_keys(
        document,
        DOCUMENT_KEYS,
        'Replay-артефакт',
    )
    if document.get('schema') != RATING_TV_REPLAY_SCHEMA:
        raise RatingTvReplayError('Название схемы replay не поддерживается.')
    if document.get('schema_version') != RATING_TV_REPLAY_SCHEMA_VERSION:
        raise RatingTvReplayError('Версия схемы replay не поддерживается.')
    if (
        document.get('data_classification')
        != RATING_TV_REPLAY_DATA_CLASSIFICATION
    ):
        raise RatingTvReplayError('Replay имеет неверный класс данных.')
    if document.get('synthetic') is not True:
        raise RatingTvReplayError('QA replay должен быть явно синтетическим.')
    if document.get('official') is not False:
        raise RatingTvReplayError('QA replay не может быть официальным.')
    if document.get('official_rating_eligible') is not False:
        raise RatingTvReplayError(
            'QA replay не может быть допущен к официальному рейтингу.',
        )
    _require_nonempty_text(document.get('warning'), 'warning')

    integrity = _require_exact_keys(
        document.get('integrity'),
        INTEGRITY_KEYS,
        'integrity',
    )
    if integrity.get('algorithm') != 'sha256':
        raise RatingTvReplayError('Поддерживается только SHA-256.')
    if integrity.get('canonicalization') != 'json-sort-keys-utf8-v1':
        raise RatingTvReplayError('Способ канонизации replay не поддерживается.')
    expected_canonical_sha256 = _require_sha256(
        integrity.get('canonical_sha256'),
        'integrity.canonical_sha256',
    )
    if expected_canonical_sha256 != canonical_replay_sha256(document):
        raise RatingTvReplayError(
            'Внутренняя контрольная сумма replay не совпала.',
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
    formula_version = _require_nonempty_text(
        replay.get('formula_version'),
        'replay.formula_version',
    )
    formula_label = _require_nonempty_text(
        replay.get('formula_label'),
        'replay.formula_label',
    )
    if replay.get('rating_mode') != 'qa_saved_replay':
        raise RatingTvReplayError('Разрешён только режим qa_saved_replay.')
    if replay.get('synthetic') is not True:
        raise RatingTvReplayError('replay.synthetic должен быть true.')
    if replay.get('official') is not False:
        raise RatingTvReplayError('replay.official должен быть false.')
    formula_evaluated = replay.get('formula_evaluated')
    if not isinstance(formula_evaluated, bool):
        raise RatingTvReplayError(
            'replay.formula_evaluated должен быть логическим значением.',
        )
    if replay.get('day_count') != RATING_TV_REPLAY_DAY_COUNT:
        raise RatingTvReplayError(
            f'Replay должен содержать ровно {RATING_TV_REPLAY_DAY_COUNT} дней.',
        )
    if replay.get('expected_employee_count') != RATING_TV_REPLAY_EMPLOYEE_COUNT:
        raise RatingTvReplayError(
            f'Replay должен содержать ровно '
            f'{RATING_TV_REPLAY_EMPLOYEE_COUNT} сотрудников.',
        )
    if replay.get('initial_day') != 1:
        raise RatingTvReplayError('Replay должен начинаться с дня 1.')
    base_step_ms = replay.get('base_step_ms')
    if (
        not isinstance(base_step_ms, int)
        or isinstance(base_step_ms, bool)
        or not 500 <= base_step_ms <= 60_000
    ):
        raise RatingTvReplayError('Некорректный replay.base_step_ms.')
    _require_nonempty_text(replay.get('timezone'), 'replay.timezone')
    _require_nonempty_text(replay.get('seed'), 'replay.seed')
    _require_nonempty_text(replay.get('source_commit'), 'replay.source_commit')
    _parse_iso_datetime(replay.get('created_at'), 'replay.created_at')
    _require_nonempty_text(replay.get('notice'), 'replay.notice')

    scope = _require_exact_keys(
        document.get('scope'),
        SCOPE_KEYS,
        'scope',
    )
    if scope.get('scope_type') != 'rating_period':
        raise RatingTvReplayError('Replay должен иметь scope_type=rating_period.')
    if scope.get('profession') != 'driver':
        raise RatingTvReplayError('Replay должен относиться к Водителям.')
    _require_nonempty_text(
        scope.get('profession_label'),
        'scope.profession_label',
    )
    _require_nonempty_text(
        scope.get('shift_type_label'),
        'scope.shift_type_label',
    )
    rating_period = _require_exact_keys(
        scope.get('rating_period'),
        RATING_PERIOD_KEYS,
        'scope.rating_period',
    )
    watch_composition = _require_exact_keys(
        scope.get('watch_composition'),
        WATCH_COMPOSITION_KEYS,
        'scope.watch_composition',
    )
    for identifier, label in (
        (rating_period.get('id'), 'scope.rating_period.id'),
        (watch_composition.get('id'), 'scope.watch_composition.id'),
    ):
        if (
            not isinstance(identifier, int)
            or isinstance(identifier, bool)
            or identifier >= 0
        ):
            raise RatingTvReplayError(f'{label} должен быть отрицательным QA-ID.')
    period_name = _require_nonempty_text(
        rating_period.get('name'),
        'scope.rating_period.name',
    )
    composition_name = _require_nonempty_text(
        watch_composition.get('name'),
        'scope.watch_composition.name',
    )
    composition_code = _require_nonempty_text(
        watch_composition.get('code'),
        'scope.watch_composition.code',
    )
    if 'тест' not in period_name.casefold():
        raise RatingTvReplayError('Период replay не помечен как тестовый.')
    if 'тест' not in composition_name.casefold():
        raise RatingTvReplayError('Состав replay не помечен как тестовый.')
    if not composition_code.startswith('qa-'):
        raise RatingTvReplayError('Код состава replay должен начинаться с qa-.')
    for is_active, label in (
        (rating_period.get('is_active'), 'scope.rating_period.is_active'),
        (
            watch_composition.get('is_active'),
            'scope.watch_composition.is_active',
        ),
    ):
        if not isinstance(is_active, bool):
            raise RatingTvReplayError(
                f'{label} должен быть логическим значением.',
            )
    period_start = _parse_iso_date(
        rating_period.get('starts_on'),
        'scope.rating_period.starts_on',
    )
    period_end = _parse_iso_date(
        rating_period.get('ends_before'),
        'scope.rating_period.ends_before',
    )
    if period_end - period_start != timedelta(
        days=RATING_TV_REPLAY_DAY_COUNT,
    ):
        raise RatingTvReplayError('Границы replay не образуют ровно 30 дней.')
    if scope.get('shift_type') not in {'day', 'night'}:
        raise RatingTvReplayError('Некорректный scope.shift_type.')

    snapshots = document.get('snapshots')
    if not isinstance(snapshots, list):
        raise RatingTvReplayError('snapshots должен быть массивом.')
    days = [
        item.get('day') if isinstance(item, dict) else None
        for item in snapshots
    ]
    expected_days = list(range(1, RATING_TV_REPLAY_DAY_COUNT + 1))
    if days != expected_days:
        raise RatingTvReplayError(
            'Replay должен содержать последовательные дни 1–30 без пропусков.',
        )
    expected_chain_sha256 = _require_sha256(
        integrity.get('snapshot_chain_sha256'),
        'integrity.snapshot_chain_sha256',
    )
    if expected_chain_sha256 != replay_snapshot_chain_sha256(snapshots):
        raise RatingTvReplayError('Цепочка дневных снимков повреждена.')

    fixed_scope = (
        rating_period,
        watch_composition,
        scope.get('shift_type'),
    )
    baseline_identities = None
    previous_places = None
    previous_shift_counts = None
    previous_payload_sha256 = None
    previous_as_of = None

    for snapshot in snapshots:
        _require_exact_keys(
            snapshot,
            SNAPSHOT_KEYS,
            f'Снимок дня {snapshot.get("day")}',
        )
        day = snapshot['day']
        expected_work_date = period_start + timedelta(days=day - 1)
        work_date = _parse_iso_date(
            snapshot.get('work_date'),
            f'work_date дня {day}',
        )
        if work_date != expected_work_date:
            raise RatingTvReplayError(
                f'work_date снимка дня {day} не соответствует периоду.',
            )
        as_of = _parse_iso_datetime(
            snapshot.get('as_of'),
            f'as_of дня {day}',
        )
        if as_of.date() not in {work_date, work_date + timedelta(days=1)}:
            raise RatingTvReplayError(
                f'as_of снимка дня {day} вышел за рабочую смену.',
            )
        if previous_as_of is not None and as_of <= previous_as_of:
            raise RatingTvReplayError(
                'Время дневных снимков должно строго возрастать.',
            )
        previous_as_of = as_of

        linked_previous_sha256 = snapshot.get('previous_payload_sha256')
        if linked_previous_sha256 != previous_payload_sha256:
            raise RatingTvReplayError(
                f'Нарушена связь снимка дня {day} с предыдущим днём.',
            )
        payload = _require_exact_keys(
            snapshot.get('payload'),
            PAYLOAD_KEYS,
            f'payload дня {day}',
        )
        payload_sha256 = _require_sha256(
            snapshot.get('payload_sha256'),
            f'payload_sha256 дня {day}',
        )
        if payload_sha256 != replay_payload_sha256(payload):
            raise RatingTvReplayError(
                f'Контрольная сумма payload дня {day} не совпала.',
            )
        previous_payload_sha256 = payload_sha256

        for field, expected in (
            ('available', True),
            ('rating_mode', 'qa_saved_replay'),
            ('synthetic', True),
            ('official', False),
            ('official_rating_eligible', False),
            ('scope_type', 'rating_period'),
            ('formula_evaluated', formula_evaluated),
            ('formula_version', formula_version),
            ('formula_label', formula_label),
            ('qa_day', day),
            ('qa_day_count', RATING_TV_REPLAY_DAY_COUNT),
            ('qa_work_date', work_date.isoformat()),
            ('generated_at', snapshot.get('as_of')),
            ('replay_run_id', replay_id),
        ):
            if payload.get(field) != expected:
                raise RatingTvReplayError(
                    f'Поле {field} не совпало в снимке дня {day}.',
                )
        _require_nonempty_text(payload.get('status'), f'status дня {day}')
        if payload.get('shift_type_label') != scope.get('shift_type_label'):
            raise RatingTvReplayError(
                f'Название смены меняется в снимке дня {day}.',
            )
        payload_period = _require_dict(
            payload.get('rating_period'),
            f'rating_period дня {day}',
        )
        payload_composition = _require_dict(
            payload.get('watch_composition'),
            f'watch_composition дня {day}',
        )
        payload_scope = (
            payload_period,
            payload_composition,
            payload.get('shift_type'),
        )
        if payload_scope != fixed_scope:
            raise RatingTvReplayError(
                f'Снимок дня {day} вышел за фиксированную область replay.',
            )
        if payload.get('available_rating_periods') != [rating_period]:
            raise RatingTvReplayError(
                f'Список периодов меняется в снимке дня {day}.',
            )
        if payload.get('available_watch_compositions') != [watch_composition]:
            raise RatingTvReplayError(
                f'Список составов меняется в снимке дня {day}.',
            )

        entries = payload.get('entries')
        if (
            not isinstance(entries, list)
            or len(entries) != RATING_TV_REPLAY_EMPLOYEE_COUNT
        ):
            raise RatingTvReplayError(
                f'Снимок дня {day} должен содержать ровно '
                f'{RATING_TV_REPLAY_EMPLOYEE_COUNT} сотрудников.',
            )
        expected_source_fingerprint = replay_snapshot_source_fingerprint(
            replay_id,
            day,
            entries,
        )
        if payload.get('source_fingerprint') != expected_source_fingerprint:
            raise RatingTvReplayError(
                f'source_fingerprint дня {day} не совпал.',
            )

        seen_employee_ids = set()
        seen_display_orders = set()
        identities = {}
        places = {}
        shift_counts = {}
        ranked_entries = []
        for entry in entries:
            validated = _validate_entry(
                entry,
                day=day,
                seen_employee_ids=seen_employee_ids,
                seen_display_orders=seen_display_orders,
            )
            employee_id = validated['employee_id']
            identities[employee_id] = (
                validated['full_name'],
                validated['equipment'],
            )
            places[employee_id] = validated['place']
            shift_counts[employee_id] = validated['shift_count']
            ranked_entries.append(validated)
            if (
                previous_places is not None
                and employee_id not in previous_places
            ):
                raise RatingTvReplayError(
                    f'Набор сотрудников изменился в снимке дня {day}.',
                )
            expected_delta = (
                0
                if previous_places is None
                else previous_places[employee_id] - validated['place']
            )
            if validated['position_delta'] != expected_delta:
                raise RatingTvReplayError(
                    f'position_delta сотрудника {employee_id} дня {day} '
                    'не совпал с предыдущим сохранённым днём.',
                )
        ranked_entries.sort(key=lambda item: item['display_order'])
        previous_score = None
        dense_place = 0
        for display_order, validated in enumerate(ranked_entries, start=1):
            if validated['display_order'] != display_order:
                raise RatingTvReplayError(
                    f'display_order дня {day} должен быть непрерывным.',
                )
            score = validated['score']
            if previous_score is not None and score > previous_score:
                raise RatingTvReplayError(
                    f'Баллы дня {day} должны идти по убыванию.',
                )
            if previous_score is None or score != previous_score:
                dense_place += 1
            expected_level = DRIVER_RATING_LEVELS.get(dense_place, '')
            if (
                validated['place'] != dense_place
                or validated['shared_score_place'] != dense_place
                or validated['level'] != expected_level
            ):
                raise RatingTvReplayError(
                    f'Равный балл должен давать общее место и уровень '
                    f'сотруднику {validated["employee_id"]} дня {day}.',
                )
            previous_score = score
        if seen_display_orders != set(
            range(1, RATING_TV_REPLAY_EMPLOYEE_COUNT + 1),
        ):
            raise RatingTvReplayError(
                f'display_order дня {day} должен быть непрерывным.',
            )
        if baseline_identities is None:
            baseline_identities = identities
        elif identities != baseline_identities:
            raise RatingTvReplayError(
                f'Состав сотрудников изменился в снимке дня {day}.',
            )
        if set(places) != set(baseline_identities):
            raise RatingTvReplayError(
                f'Набор сотрудников изменился в снимке дня {day}.',
            )
        if previous_shift_counts is not None:
            for employee_id, shift_count in shift_counts.items():
                if shift_count < previous_shift_counts[employee_id]:
                    raise RatingTvReplayError(
                        f'Число смен сотрудника {employee_id} уменьшилось '
                        f'в снимке дня {day}.',
                    )
        summary = _require_exact_keys(
            payload.get('summary'),
            SUMMARY_KEYS,
            f'summary дня {day}',
        )
        if summary.get('employee_count') != RATING_TV_REPLAY_EMPLOYEE_COUNT:
            raise RatingTvReplayError(
                f'summary дня {day} имеет неверное число сотрудников.',
            )
        _require_nonnegative_int(
            summary.get('rated_shift_count'),
            f'summary.rated_shift_count дня {day}',
        )
        _require_nonnegative_int(
            summary.get('withheld_shift_count'),
            f'summary.withheld_shift_count дня {day}',
        )
        withheld_reasons = _require_dict(
            summary.get('withheld_reasons'),
            f'summary.withheld_reasons дня {day}',
        )
        for reason, count in withheld_reasons.items():
            if (
                not isinstance(reason, str)
                or not re.fullmatch(r'[a-z0-9_]+', reason)
                or not isinstance(count, int)
                or isinstance(count, bool)
                or count < 0
            ):
                raise RatingTvReplayError(
                    f'Некорректная причина удержания дня {day}.',
                )
        previous_places = places
        previous_shift_counts = shift_counts


def load_rating_tv_replay(path, *, expected_sha256):
    artifact_path = Path(path).expanduser().resolve()
    if artifact_path.suffix.lower() != '.json':
        raise RatingTvReplayError('Replay-артефакт должен иметь расширение .json.')
    expected_sha256 = _require_sha256(
        expected_sha256,
        'Ожидаемая внешняя SHA-256',
    )
    try:
        raw = artifact_path.read_bytes()
    except OSError as error:
        raise RatingTvReplayError('Replay-артефакт не найден.') from error
    if len(raw) > RATING_TV_REPLAY_MAX_BYTES:
        raise RatingTvReplayError('Replay-артефакт превышает допустимый размер.')
    raw_sha256 = hashlib.sha256(raw).hexdigest().upper()
    if raw_sha256 != expected_sha256:
        raise RatingTvReplayError(
            'Внешняя контрольная сумма replay-артефакта не совпала.',
        )
    document = _strict_json_loads(raw)
    validate_rating_tv_replay(document)
    return deepcopy(document), raw_sha256
