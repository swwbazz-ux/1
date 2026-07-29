from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase

from reports.driver_watch_rating import (
    DRIVER_RATING_FORMULA_VERSION,
    DRIVER_RATING_LEVELS,
    DRIVER_RATING_WEIGHTS,
)
from reports.rating_tv_formula_replay import (
    RATING_TV_FORMULA_REPLAY_DATA_CLASSIFICATION,
    RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT,
    RATING_TV_FORMULA_REPLAY_MODE,
    load_rating_tv_formula_replay,
)
from tools.build_driver_rating_30d_formula_replay import (
    FormulaReplayBuildError,
    OUTPUT_NAMES,
    convert_formula_replays,
)


SOURCE_COMMIT = 'a' * 40
START_DATE = date(2026, 6, 14)
END_DATE = date(2026, 7, 13)
ENDS_BEFORE = date(2026, 7, 14)
COMPOSITION = {
    'id': 201,
    'code': 'qa-rating-30d',
    'name': 'ТЕСТ_СОСТАВ_30_ДНЕЙ',
}
RATING_PERIOD = {
    'id': 301,
    'name': 'ТЕСТ_ПЕРИОД_30_ДНЕЙ',
    'starts_on': START_DATE.isoformat(),
    'ends_before': ENDS_BEFORE.isoformat(),
}


def _json_bytes(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + '\n'
    ).encode('utf-8')


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest().upper()


def _fingerprint(label):
    return hashlib.sha256(label.encode('utf-8')).hexdigest().upper()


def _score_for(ordinal, day_number):
    if ordinal == 1:
        return Decimal('99.0001') if day_number % 2 else Decimal('99.0000')
    if ordinal == 2:
        return Decimal('99.0000') if day_number % 2 else Decimal('99.0001')
    if ordinal in {3, 4}:
        return Decimal('98.0000')
    return Decimal(102 - ordinal).quantize(Decimal('0.0001'))


def _source_employee_id(shift_type, ordinal):
    return (1_000 if shift_type == 'day' else 2_000) + ordinal


def _source_shift_id(shift_type, ordinal, shift_day):
    offset = 100_000 if shift_type == 'day' else 200_000
    return offset + (ordinal * 100) + shift_day


def _raw_formula(day_number, shift_type):
    entries = []
    for ordinal in range(1, RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT + 1):
        score = _score_for(ordinal, day_number)
        score_text = f'{score:.4f}'
        trip_count = day_number * (10 + (ordinal % 4))
        volume_m3 = Decimal(trip_count * 12).quantize(Decimal('0.01'))
        tonnage_t = (
            Decimal(trip_count) * Decimal('20.50')
        ).quantize(Decimal('0.01'))
        entries.append({
            'employee_id': _source_employee_id(shift_type, ordinal),
            'full_name': (
                f'ТЕСТ_{shift_type.upper()}_ВОДИТЕЛЬ_{ordinal:02d}'
            ),
            'equipment': [f'ТЕСТ_САМОСВАЛ_{((ordinal - 1) % 12) + 1:02d}'],
            'shift_count': day_number,
            'trip_count': trip_count,
            'volume_m3': f'{volume_m3:.2f}',
            'tonnage_t': f'{tonnage_t:.2f}',
            'score': score_text,
            'blocks': {
                key: score_text
                for key in DRIVER_RATING_WEIGHTS
            },
            'confidence': '95.4321',
            'source_shift_ids': [
                _source_shift_id(shift_type, ordinal, shift_day)
                for shift_day in range(1, day_number + 1)
            ],
        })

    entries.sort(
        key=lambda item: (
            -Decimal(item['score']),
            item['employee_id'],
        ),
    )
    dense_place_by_score = {}
    for display_order, entry in enumerate(entries, start=1):
        score = entry['score']
        if score not in dense_place_by_score:
            dense_place_by_score[score] = len(dense_place_by_score) + 1
        place = dense_place_by_score[score]
        entry.update({
            'place': place,
            'shared_score_place': place,
            'display_order': display_order,
            'level': DRIVER_RATING_LEVELS.get(place, ''),
        })

    generated_at = (
        datetime(
            START_DATE.year,
            START_DATE.month,
            START_DATE.day,
            tzinfo=timezone(timedelta(hours=4)),
        )
        + timedelta(
            days=day_number,
            hours=8 if shift_type == 'day' else 20,
        )
    ).isoformat()
    source_fingerprint = _fingerprint(
        f'{shift_type}:{day_number}:source',
    )
    shift_score_fingerprint = _fingerprint(
        f'{shift_type}:{day_number}:scores',
    )
    rated_shift_count = (
        RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT * day_number
    )
    return {
        'available': True,
        'official': False,
        'rating_mode': 'working',
        'scope_type': 'rating_period',
        'formula_version': DRIVER_RATING_FORMULA_VERSION,
        'formula_label': 'Рабочая формула рейтинга водителей',
        'status': 'Рабочий рейтинг рассчитан.',
        'generated_at': generated_at,
        'source_fingerprint': source_fingerprint,
        'shift_score_fingerprint': shift_score_fingerprint,
        'rating_period': RATING_PERIOD,
        'watch_composition': COMPOSITION,
        'shift_type': shift_type,
        'shift_type_label': 'Дневная' if shift_type == 'day' else 'Ночная',
        'weights': {
            key: str(value)
            for key, value in DRIVER_RATING_WEIGHTS.items()
        },
        'distance_metrics': {
            'weight': '0',
            'status': 'planned',
            'label': 'м³·км и т·км пока не учитываются',
        },
        'linkage_audit': {
            'candidate_closed_shift_count': rated_shift_count,
            'linked_to_selected_composition_count': rated_shift_count,
            'unlinked_shift_count': 0,
            'linked_to_other_composition_count': 0,
            'selected_watch_date_mismatch_count': 0,
            'covered_watch_period_count': 1,
            'linkage_ready': True,
        },
        'summary': {
            'employee_count': RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT,
            'rated_shift_count': rated_shift_count,
            'withheld_shift_count': 0,
            'withheld_reasons': {},
            'trip_count': sum(entry['trip_count'] for entry in entries),
            'volume_m3': f'{sum(
                (Decimal(entry["volume_m3"]) for entry in entries),
                Decimal("0"),
            ):.2f}',
            'tonnage_t': f'{sum(
                (Decimal(entry["tonnage_t"]) for entry in entries),
                Decimal("0"),
            ):.2f}',
        },
        'entries': entries,
    }


def _artifact_record(day_number, shift_type, raw, raw_sha256):
    return {
        'day': day_number,
        'shift_type': shift_type,
        'path': f'raw_formula/{shift_type}/day_{day_number:02d}.json',
        'sha256': raw_sha256,
        'generated_at': raw['generated_at'],
        'source_fingerprint': raw['source_fingerprint'],
        'shift_score_fingerprint': raw['shift_score_fingerprint'],
        'employee_count': len(raw['entries']),
        'rated_shift_count': raw['summary']['rated_shift_count'],
        'withheld_shift_count': raw['summary']['withheld_shift_count'],
    }


def _manifest(artifacts):
    return {
        'schema': 'copper.driver-rating-30d-qa-run',
        'schema_version': 1,
        'data_classification': 'synthetic_qa_only',
        'synthetic': True,
        'official': False,
        'official_rating_eligible': False,
        'warning': (
            'Синтетический технический прогон. Не является калибровкой KPI.'
        ),
        'run': {
            'id': 'ТЕСТ_DRIVER_RATING_30D_20260730',
            'marker': 'ТЕСТ_РЕЙТИНГ_30Д_20260730',
            'day_count': 30,
            'start_date': START_DATE.isoformat(),
            'end_date': END_DATE.isoformat(),
            'ends_before': ENDS_BEFORE.isoformat(),
            'day_brigade': 1,
            'night_brigade': 3,
            'formula_version': DRIVER_RATING_FORMULA_VERSION,
            'formula_call_mode': 'direct_after_each_completed_day',
            'duration_seconds': 12.345,
        },
        'database': {
            'configured': {
                'engine': 'django.db.backends.postgresql',
                'name': 'copper_rating_30d_qa_20260730',
                'user': 'copper_rating30_qa_runner',
                'host': '127.0.0.1',
                'port': '55434',
            },
            'actual': {
                'name': 'copper_rating_30d_qa_20260730',
                'host': '127.0.0.1',
                'port': '55434',
                'user': 'copper_rating30_qa_runner',
            },
            'business_counts': {'employees': 106},
        },
        'references': {'truck_count': 12},
        'staff': {'driver': 106},
        'scope': {
            'watch_composition': COMPOSITION,
            'watch_period': {
                'id': 401,
                'starts_on': START_DATE.isoformat(),
                'ends_on': END_DATE.isoformat(),
            },
            'rating_period': {
                'id': RATING_PERIOD['id'],
                'starts_on': START_DATE.isoformat(),
                'ends_before': ENDS_BEFORE.isoformat(),
            },
            'day_employee_count': RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT,
            'night_employee_count': RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT,
        },
        'generation': {
            'shift_count': 60,
            'loaded_trip_count': 1,
            'unloaded_trip_count': 1,
        },
        'formula_artifacts': artifacts,
        'final_state': {
            'counts': {'formula_snapshots': 60},
            'expected': {'formula_snapshots': 60},
        },
        'replay_conversion': {
            'performed': False,
            'reason': 'Преобразование выполняется отдельным контрактом.',
        },
    }


def _write_run(run_dir):
    artifacts = []
    for day_number in range(1, 31):
        for shift_type in ('day', 'night'):
            raw = _raw_formula(day_number, shift_type)
            raw_bytes = _json_bytes(raw)
            relative_path = Path(
                f'raw_formula/{shift_type}/day_{day_number:02d}.json',
            )
            absolute_path = run_dir / relative_path
            absolute_path.parent.mkdir(parents=True, exist_ok=True)
            absolute_path.write_bytes(raw_bytes)
            artifacts.append(
                _artifact_record(
                    day_number,
                    shift_type,
                    raw,
                    _sha256(raw_bytes),
                ),
            )
    manifest = _manifest(artifacts)
    (run_dir / 'run_manifest.json').write_bytes(_json_bytes(manifest))
    return manifest


def _rewrite_raw(run_dir, manifest, shift_type, day_number, mutate):
    relative_path = Path(
        f'raw_formula/{shift_type}/day_{day_number:02d}.json',
    )
    absolute_path = run_dir / relative_path
    raw = json.loads(absolute_path.read_text(encoding='utf-8'))
    mutate(raw)
    raw_bytes = _json_bytes(raw)
    absolute_path.write_bytes(raw_bytes)
    for artifact in manifest['formula_artifacts']:
        if (
            artifact['shift_type'] == shift_type
            and artifact['day'] == day_number
        ):
            artifact.update(
                _artifact_record(
                    day_number,
                    shift_type,
                    raw,
                    _sha256(raw_bytes),
                ),
            )
            break
    (run_dir / 'run_manifest.json').write_bytes(_json_bytes(manifest))


class DriverRating30dFormulaReplayBuilderTests(SimpleTestCase):
    maxDiff = None

    def test_converts_verified_60_raw_grid_into_two_formula_replays(self):
        with TemporaryDirectory(prefix='rating-formula-source-') as source:
            with TemporaryDirectory(prefix='rating-formula-target-parent-') as target:
                run_dir = Path(source)
                output_dir = Path(target) / 'published'
                manifest = _write_run(run_dir)
                _rewrite_raw(
                    run_dir,
                    manifest,
                    'day',
                    3,
                    lambda raw: raw['entries'].reverse(),
                )

                results = convert_formula_replays(
                    run_dir=run_dir,
                    output_dir=output_dir,
                    source_commit=SOURCE_COMMIT,
                )

                self.assertEqual(set(results), {'day', 'night'})
                self.assertEqual(
                    {path.name for path in output_dir.iterdir()},
                    set(OUTPUT_NAMES.values()),
                )
                documents = {}
                for shift_type, result in results.items():
                    document, actual_sha256 = load_rating_tv_formula_replay(
                        result['path'],
                        expected_sha256=result['raw_sha256'],
                    )
                    documents[shift_type] = document
                    self.assertEqual(actual_sha256, result['raw_sha256'])
                    self.assertEqual(
                        result['canonical_sha256'],
                        document['integrity']['canonical_sha256'],
                    )
                    self.assertEqual(
                        document['data_classification'],
                        RATING_TV_FORMULA_REPLAY_DATA_CLASSIFICATION,
                    )
                    self.assertEqual(
                        document['replay']['rating_mode'],
                        RATING_TV_FORMULA_REPLAY_MODE,
                    )
                    self.assertTrue(document['formula_evaluated'])
                    self.assertFalse(document['official'])
                    self.assertFalse(document['official_rating_eligible'])
                    self.assertEqual(len(document['snapshots']), 30)
                    self.assertEqual(len(document['scope']['cohort']), 53)
                    self.assertTrue(
                        all(
                            item['employee_id'] < 0
                            for item in document['scope']['cohort']
                        ),
                    )
                    self.assertTrue(
                        all(
                            shift_id < 0
                            for snapshot in document['snapshots']
                            for row in snapshot['payload']['entries']
                            for shift_id in row['source_shift_ids']
                        ),
                    )
                    self.assertEqual(
                        document['replay']['source_manifest_sha256'],
                        _sha256(
                            (run_dir / 'run_manifest.json').read_bytes(),
                        ),
                    )
                    for day_number, snapshot in enumerate(
                        document['snapshots'],
                        start=1,
                    ):
                        source_artifact = next(
                            artifact
                            for artifact in manifest['formula_artifacts']
                            if artifact['shift_type'] == shift_type
                            and artifact['day'] == day_number
                        )
                        payload = snapshot['payload']
                        self.assertEqual(
                            payload['source_raw_path'],
                            source_artifact['path'],
                        )
                        self.assertEqual(
                            payload['source_raw_sha256'],
                            source_artifact['sha256'],
                        )
                        self.assertTrue(
                            all(
                                row['quality_flags_status']
                                == 'not_exposed_by_formula_payload'
                                and row['quality_flags'] == []
                                for row in payload['entries']
                            ),
                        )

                day_ids = {
                    item['employee_id']
                    for item in documents['day']['scope']['cohort']
                }
                night_ids = {
                    item['employee_id']
                    for item in documents['night']['scope']['cohort']
                }
                self.assertFalse(day_ids & night_ids)

                day_one = documents['day']['snapshots'][0]['payload']['entries']
                self.assertEqual(day_one[0]['score'], '99.0001')
                self.assertEqual(day_one[1]['score'], '99.0000')
                self.assertEqual(day_one[2]['score'], '98.0000')
                self.assertEqual(day_one[3]['score'], '98.0000')
                self.assertEqual(day_one[2]['place'], 3)
                self.assertEqual(day_one[3]['place'], 3)
                self.assertEqual(day_one[0]['confidence'], '95.4321')
                self.assertEqual(
                    day_one[0]['blocks'],
                    {
                        key: '99.0001'
                        for key in DRIVER_RATING_WEIGHTS
                    },
                )
                day_two = documents['day']['snapshots'][1]['payload']['entries']
                moved_up = next(
                    row
                    for row in day_two
                    if row['full_name'] == 'ТЕСТ_DAY_ВОДИТЕЛЬ_02'
                )
                moved_down = next(
                    row
                    for row in day_two
                    if row['full_name'] == 'ТЕСТ_DAY_ВОДИТЕЛЬ_01'
                )
                self.assertEqual(moved_up['position_delta'], 1)
                self.assertEqual(moved_down['position_delta'], -1)

    def test_rejects_changed_raw_sha_without_publishing(self):
        with TemporaryDirectory(prefix='rating-formula-source-') as source:
            with TemporaryDirectory(prefix='rating-formula-target-parent-') as target:
                run_dir = Path(source)
                output_dir = Path(target) / 'published'
                _write_run(run_dir)
                raw_path = run_dir / 'raw_formula/day/day_17.json'
                raw_path.write_bytes(raw_path.read_bytes() + b' ')

                with self.assertRaises(FormulaReplayBuildError):
                    convert_formula_replays(
                        run_dir=run_dir,
                        output_dir=output_dir,
                        source_commit=SOURCE_COMMIT,
                    )

                self.assertFalse(output_dir.exists())

    def test_rejects_changed_cohort_even_with_updated_raw_sha(self):
        with TemporaryDirectory(prefix='rating-formula-source-') as source:
            with TemporaryDirectory(prefix='rating-formula-target-parent-') as target:
                run_dir = Path(source)
                output_dir = Path(target) / 'published'
                manifest = _write_run(run_dir)

                def change_employee(raw):
                    raw['entries'][0]['employee_id'] = 999_999

                _rewrite_raw(run_dir, manifest, 'night', 12, change_employee)

                with self.assertRaises(FormulaReplayBuildError):
                    convert_formula_replays(
                        run_dir=run_dir,
                        output_dir=output_dir,
                        source_commit=SOURCE_COMMIT,
                    )

                self.assertFalse(output_dir.exists())

    def test_rejects_withheld_raw_instead_of_inventing_employee_rows(self):
        with TemporaryDirectory(prefix='rating-formula-source-') as source:
            with TemporaryDirectory(prefix='rating-formula-target-parent-') as target:
                run_dir = Path(source)
                output_dir = Path(target) / 'published'
                manifest = _write_run(run_dir)

                def add_withheld(raw):
                    raw['summary']['rated_shift_count'] -= 1
                    raw['summary']['withheld_shift_count'] = 1
                    raw['summary']['withheld_reasons'] = {
                        'invalid_shift_window': 1,
                    }

                _rewrite_raw(run_dir, manifest, 'day', 8, add_withheld)

                with self.assertRaises(FormulaReplayBuildError):
                    convert_formula_replays(
                        run_dir=run_dir,
                        output_dir=output_dir,
                        source_commit=SOURCE_COMMIT,
                    )

                self.assertFalse(output_dir.exists())

    def test_rejects_linkage_counts_that_do_not_prove_full_binding(self):
        with TemporaryDirectory(prefix='rating-formula-source-') as source:
            with TemporaryDirectory(prefix='rating-formula-target-parent-') as target:
                run_dir = Path(source)
                output_dir = Path(target) / 'published'
                manifest = _write_run(run_dir)

                def break_linkage(raw):
                    raw['linkage_audit'][
                        'linked_to_selected_composition_count'
                    ] -= 1
                    raw['linkage_audit']['unlinked_shift_count'] = 1

                _rewrite_raw(run_dir, manifest, 'night', 21, break_linkage)

                with self.assertRaises(FormulaReplayBuildError):
                    convert_formula_replays(
                        run_dir=run_dir,
                        output_dir=output_dir,
                        source_commit=SOURCE_COMMIT,
                    )

                self.assertFalse(output_dir.exists())

    def test_refuses_existing_output_directory_without_overwrite(self):
        with TemporaryDirectory(prefix='rating-formula-source-') as source:
            with TemporaryDirectory(prefix='rating-formula-target-parent-') as target:
                run_dir = Path(source)
                output_dir = Path(target) / 'published'
                _write_run(run_dir)
                first = convert_formula_replays(
                    run_dir=run_dir,
                    output_dir=output_dir,
                    source_commit=SOURCE_COMMIT,
                )
                original_hashes = {
                    shift_type: _sha256(result['path'].read_bytes())
                    for shift_type, result in first.items()
                }

                with self.assertRaises(FormulaReplayBuildError):
                    convert_formula_replays(
                        run_dir=run_dir,
                        output_dir=output_dir,
                        source_commit=SOURCE_COMMIT,
                    )

                self.assertEqual(
                    {
                        shift_type: _sha256(result['path'].read_bytes())
                        for shift_type, result in first.items()
                    },
                    original_hashes,
                )

    def test_rejects_level_inconsistent_with_exact_dense_place(self):
        with TemporaryDirectory(prefix='rating-formula-source-') as source:
            with TemporaryDirectory(prefix='rating-formula-target-parent-') as target:
                run_dir = Path(source)
                output_dir = Path(target) / 'published'
                manifest = _write_run(run_dir)

                def corrupt_level(raw):
                    raw['entries'][0]['level'] = ''

                _rewrite_raw(run_dir, manifest, 'day', 3, corrupt_level)

                with self.assertRaises(FormulaReplayBuildError):
                    convert_formula_replays(
                        run_dir=run_dir,
                        output_dir=output_dir,
                        source_commit=SOURCE_COMMIT,
                    )

                self.assertFalse(output_dir.exists())
