#!/usr/bin/env python
"""Создать неизменяемый визуальный QA-replay для проверки TV-экрана.

Этот генератор не выполняет формулу KPI и не обращается к базе данных.
Его результат нужен только для проверки управления сохранёнными снимками
до единственного полноценного 30-дневного виртуального прогона.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django  # noqa: E402

django.setup()

from reports.driver_watch_rating import DRIVER_RATING_LEVELS  # noqa: E402
from reports.rating_tv_replay import (  # noqa: E402
    RATING_TV_REPLAY_DAY_COUNT,
    RATING_TV_REPLAY_DATA_CLASSIFICATION,
    RATING_TV_REPLAY_EMPLOYEE_COUNT,
    RATING_TV_REPLAY_SCHEMA,
    RATING_TV_REPLAY_SCHEMA_VERSION,
    attach_replay_integrity,
    replay_snapshot_source_fingerprint,
    validate_rating_tv_replay,
)


RUN_ID = 'QA-TV-VISUAL-REPLAY-20260729-V1'
PERIOD_START = date(2026, 5, 1)
PERIOD_END = PERIOD_START + timedelta(days=RATING_TV_REPLAY_DAY_COUNT)
VLAT_OFFSET = timezone(timedelta(hours=10))

SURNAMES = (
    'Тестов',
    'Проверкин',
    'Сценариев',
    'Макетов',
    'Контрольный',
    'Виртуальный',
    'Снимков',
    'Рейтингов',
    'Динамиков',
)
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    return parser.parse_args()


def q2(value):
    return Decimal(value).quantize(
        Decimal('0.01'),
        rounding=ROUND_HALF_UP,
    )


def full_name(ordinal):
    surname = SURNAMES[(ordinal - 1) % len(SURNAMES)]
    return f'ТЕСТ_{surname} {ordinal:02d}'


def equipment_label(ordinal):
    family = ('БелАЗ', 'БелАЗ', 'NHL')[(ordinal - 1) % 3]
    return f'{family} №{ordinal:02d}'


def daily_observation(ordinal, day):
    stable_level = 58 + ((ordinal * 37 + 11) % 34)
    daily_wave = ((ordinal * 13 + day * 17 + day * day) % 25) - 12
    recovery = 4 if (ordinal + day) % 11 == 0 else 0
    disturbance = -6 if (ordinal * day) % 17 == 0 else 0
    return max(
        25,
        min(100, stable_level + daily_wave + recovery + disturbance),
    )


def build_document():
    rating_period = {
        'id': -1001,
        'name': 'Тестовый период 01.05.2026 — 31.05.2026',
        'starts_on': PERIOD_START.isoformat(),
        'ends_before': PERIOD_END.isoformat(),
        'is_active': True,
    }
    watch_composition = {
        'id': -2001,
        'code': 'qa-tv-visual-replay',
        'name': 'Тестовый состав сохранённого показа',
        'is_active': True,
    }
    cumulative = {
        ordinal: Decimal('0')
        for ordinal in range(1, RATING_TV_REPLAY_EMPLOYEE_COUNT + 1)
    }
    previous_places = {}
    snapshots = []

    for day in range(1, RATING_TV_REPLAY_DAY_COUNT + 1):
        scored = []
        for ordinal in range(1, RATING_TV_REPLAY_EMPLOYEE_COUNT + 1):
            cumulative[ordinal] += Decimal(
                daily_observation(ordinal, day),
            )
            score = q2(cumulative[ordinal] / Decimal(day))
            scored.append((ordinal, score))
        scored.sort(key=lambda item: (-item[1], item[0]))

        entries = []
        current_places = {}
        dense_place_by_score = {}
        for display_order, (ordinal, score) in enumerate(scored, start=1):
            if score not in dense_place_by_score:
                dense_place_by_score[score] = len(dense_place_by_score) + 1
            place = dense_place_by_score[score]
            employee_id = -ordinal
            current_places[employee_id] = place
            previous_place = previous_places.get(employee_id)
            entries.append({
                'employee_id': employee_id,
                'full_name': full_name(ordinal),
                'equipment': [equipment_label(ordinal)],
                'shift_count': day,
                'score': str(score),
                'place': place,
                'shared_score_place': place,
                'display_order': display_order,
                'level': DRIVER_RATING_LEVELS.get(place, ''),
                'position_delta': (
                    0
                    if previous_place is None
                    else previous_place - place
                ),
            })
        previous_places = current_places

        generated_at = datetime.combine(
            PERIOD_START + timedelta(days=day - 1),
            time(hour=22),
            tzinfo=VLAT_OFFSET,
        )
        source_fingerprint = replay_snapshot_source_fingerprint(
            RUN_ID,
            day,
            entries,
        )
        payload = {
            'available': True,
            'official': False,
            'official_rating_eligible': False,
            'synthetic': True,
            'formula_evaluated': False,
            'rating_mode': 'qa_saved_replay',
            'scope_type': 'rating_period',
            'formula_version': 'TV_VISUAL_REPLAY_NOT_KPI',
            'formula_label': (
                'Визуальное воспроизведение, формула KPI не выполнялась'
            ),
            'status': (
                f'Сохранённый визуальный снимок дня {day}. '
                'Это не результат KPI.'
            ),
            'generated_at': generated_at.isoformat(),
            'source_fingerprint': source_fingerprint,
            'rating_period': rating_period,
            'watch_composition': watch_composition,
            'shift_type': 'night',
            'shift_type_label': 'Ночная',
            'available_rating_periods': [rating_period],
            'available_watch_compositions': [watch_composition],
            'summary': {
                'employee_count': len(entries),
                'rated_shift_count': len(entries) * day,
                'withheld_shift_count': 0,
                'withheld_reasons': {},
            },
            'entries': entries,
            'qa_day': day,
            'qa_day_count': RATING_TV_REPLAY_DAY_COUNT,
            'qa_work_date': (
                PERIOD_START + timedelta(days=day - 1)
            ).isoformat(),
            'replay_run_id': RUN_ID,
        }
        snapshots.append({
            'day': day,
            'work_date': (
                PERIOD_START + timedelta(days=day - 1)
            ).isoformat(),
            'as_of': generated_at.isoformat(),
            'payload': payload,
        })

    document = {
        'schema': RATING_TV_REPLAY_SCHEMA,
        'schema_version': RATING_TV_REPLAY_SCHEMA_VERSION,
        'data_classification': RATING_TV_REPLAY_DATA_CLASSIFICATION,
        'synthetic': True,
        'official': False,
        'official_rating_eligible': False,
        'warning': (
            'СИНТЕТИЧЕСКОЕ QA-МОДЕЛИРОВАНИЕ. '
            'Не является реальным или официальным рейтингом.'
        ),
        'replay': {
            'id': RUN_ID,
            'label': 'Визуальная проверка 30-дневного воспроизведения',
            'scenario_version': 'DRIVER_RATING_VISUAL_REPLAY_V1',
            'rating_mode': 'qa_saved_replay',
            'synthetic': True,
            'formula_evaluated': False,
            'official': False,
            'day_count': RATING_TV_REPLAY_DAY_COUNT,
            'expected_employee_count': RATING_TV_REPLAY_EMPLOYEE_COUNT,
            'initial_day': 1,
            'base_step_ms': 3000,
            'created_at': '2026-07-29T12:00:00+04:00',
            'formula_version': 'TV_VISUAL_REPLAY_NOT_KPI',
            'formula_label': (
                'Визуальное воспроизведение, формула KPI не выполнялась'
            ),
            'timezone': 'Asia/Vladivostok',
            'seed': '20260729',
            'source_commit': 'de7a902758d805722b57df10e9bdddf4a362b21f',
            'notice': (
                'Детерминированная синтетика только для проверки экрана. '
                'Не использовать для оценки сотрудников или калибровки KPI.'
            ),
        },
        'scope': {
            'scope_type': 'rating_period',
            'profession': 'driver',
            'profession_label': 'Водитель самосвала',
            'rating_period': rating_period,
            'watch_composition': watch_composition,
            'shift_type': 'night',
            'shift_type_label': 'Ночная',
        },
        'snapshots': snapshots,
    }
    result = attach_replay_integrity(document)
    validate_rating_tv_replay(result)
    return result


def main():
    args = parse_args()
    output = args.output.expanduser().resolve()
    if output.suffix.lower() != '.json':
        raise SystemExit('Выходной файл должен иметь расширение .json.')
    if not output.parent.is_dir():
        raise SystemExit(f'Родительская папка не найдена: {output.parent}')
    payload = (
        json.dumps(
            build_document(),
            ensure_ascii=False,
            indent=2,
        )
        + '\n'
    ).encode('utf-8')
    try:
        with output.open('xb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise SystemExit(
            f'Файл уже существует и не будет перезаписан: {output}',
        ) from error
    print(f'REPLAY_ARTIFACT={output}')
    print(f'BYTES={len(payload)}')
    print(f'RAW_SHA256={hashlib.sha256(payload).hexdigest().upper()}')


if __name__ == '__main__':
    main()
