import hashlib
import json
import tempfile
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase

from .driver_watch_rating import (
    DRIVER_RATING_FORMULA_VERSION,
    DRIVER_RATING_LEVELS,
    DRIVER_RATING_WEIGHTS,
)
from .rating_tv_formula_replay import (
    RATING_TV_FORMULA_REPLAY_DATA_CLASSIFICATION,
    RATING_TV_FORMULA_REPLAY_DAY_COUNT,
    RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT,
    RATING_TV_FORMULA_REPLAY_MODE,
    RATING_TV_FORMULA_REPLAY_SCHEMA,
    RATING_TV_FORMULA_REPLAY_SCHEMA_VERSION,
    RatingTvFormulaReplayError,
    attach_formula_replay_integrity,
    formula_replay_cohort_sha256,
    load_rating_tv_formula_replay,
    validate_rating_tv_formula_replay,
)


QA_TZ = timezone(timedelta(hours=10))


def _sha(label):
    return hashlib.sha256(label.encode('utf-8')).hexdigest().upper()


def _q2(value):
    return str(Decimal(value).quantize(Decimal('0.01')))


def _q4(value):
    return str(Decimal(value).quantize(Decimal('0.0001')))


class FormulaReplayDocumentFactory:
    period_start = date(2026, 5, 1)
    run_id = 'QA-FORMULA-REPLAY-20260730-V1'

    def __init__(self):
        self.rating_period = {
            'id': -3101,
            'name': 'Тестовый формульный период 01.05–31.05',
            'starts_on': self.period_start.isoformat(),
            'ends_before': (
                self.period_start
                + timedelta(days=RATING_TV_FORMULA_REPLAY_DAY_COUNT)
            ).isoformat(),
            'is_active': True,
        }
        self.watch_composition = {
            'id': -3201,
            'code': 'qa-formula-replay-night',
            'name': 'Тестовый ночной состав формульного replay',
            'is_active': True,
        }
        self.cohort = [
            {
                'employee_id': -ordinal,
                'full_name': f'ТЕСТ_Водитель {ordinal:02d}',
            }
            for ordinal in range(
                1,
                RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT + 1,
            )
        ]

    @staticmethod
    def _row_status(ordinal, day):
        if ordinal == 51:
            return 'withheld'
        if ordinal == 52 and day == 3:
            return 'withheld'
        if ordinal == 53 and day < 5:
            return 'not_observed'
        return 'rated'

    @staticmethod
    def _score(ordinal):
        if ordinal == 1:
            return Decimal('99.0001')
        if ordinal == 2:
            return Decimal('99.0000')
        if ordinal in {3, 4}:
            return Decimal('98.0000')
        return Decimal('97.0000') - Decimal(ordinal - 5)

    @staticmethod
    def _source_shift_ids(ordinal, shift_count):
        return sorted(
            -(ordinal * 1000 + shift_index)
            for shift_index in range(1, shift_count + 1)
        )

    def _rated_row(
        self,
        *,
        ordinal,
        day,
        display_order,
        place,
        previous_statuses,
        previous_places,
    ):
        employee_id = -ordinal
        shift_count = day
        trip_count = shift_count * (17 + ordinal % 7)
        return {
            'employee_id': employee_id,
            'full_name': f'ТЕСТ_Водитель {ordinal:02d}',
            'equipment': [f'БелАЗ №{ordinal:02d}'],
            'row_status': 'rated',
            'ranking_eligible': True,
            'shift_count': shift_count,
            'withheld_shift_count': 0,
            'withheld_reasons': {},
            'quality_flags': (
                ['unexplained_time']
                if ordinal == 5
                else []
            ),
            'quality_flags_status': 'captured',
            'trip_count': trip_count,
            'volume_m3': _q2(Decimal(trip_count) * Decimal('22.50')),
            'tonnage_t': _q2(Decimal(trip_count) * Decimal('55.25')),
            'score': _q4(self._score(ordinal)),
            'blocks': {
                key: _q4(
                    Decimal('60')
                    + Decimal((ordinal + index * 7) % 35)
                )
                for index, key in enumerate(DRIVER_RATING_WEIGHTS)
            },
            'confidence': _q4(
                Decimal('82') + Decimal(ordinal % 13),
            ),
            'source_shift_ids': self._source_shift_ids(
                ordinal,
                shift_count,
            ),
            'place': place,
            'shared_score_place': place,
            'display_order': display_order,
            'level': DRIVER_RATING_LEVELS.get(place, ''),
            'position_delta': (
                previous_places[employee_id] - place
                if previous_statuses.get(employee_id) == 'rated'
                else None
            ),
        }

    def _unrated_row(self, *, ordinal, day, display_order, row_status):
        if row_status == 'not_observed':
            shift_count = 0
            withheld_shift_count = 0
            withheld_reasons = {}
            quality_flags = []
            equipment = []
        elif ordinal == 51:
            shift_count = day
            withheld_shift_count = day
            withheld_reasons = {
                'blocking_quality:data_conflict': day,
            }
            quality_flags = ['data_conflict']
            equipment = [f'БелАЗ №{ordinal:02d}']
        else:
            shift_count = day
            withheld_shift_count = day
            withheld_reasons = {
                'employee_partial_coverage': day,
            }
            quality_flags = ['invalid_shift_window']
            equipment = [f'БелАЗ №{ordinal:02d}']
        return {
            'employee_id': -ordinal,
            'full_name': f'ТЕСТ_Водитель {ordinal:02d}',
            'equipment': equipment,
            'row_status': row_status,
            'ranking_eligible': False,
            'shift_count': shift_count,
            'withheld_shift_count': withheld_shift_count,
            'withheld_reasons': withheld_reasons,
            'quality_flags': quality_flags,
            'quality_flags_status': (
                'not_applicable'
                if row_status == 'not_observed'
                else 'captured'
            ),
            'trip_count': None,
            'volume_m3': None,
            'tonnage_t': None,
            'score': None,
            'blocks': None,
            'confidence': None,
            'source_shift_ids': self._source_shift_ids(
                ordinal,
                shift_count,
            ),
            'place': None,
            'shared_score_place': None,
            'display_order': display_order,
            'level': '',
            'position_delta': None,
        }

    def build(self):
        snapshots = []
        previous_statuses = {}
        previous_places = {}
        for day in range(1, RATING_TV_FORMULA_REPLAY_DAY_COUNT + 1):
            statuses = {
                ordinal: self._row_status(ordinal, day)
                for ordinal in range(
                    1,
                    RATING_TV_FORMULA_REPLAY_EMPLOYEE_COUNT + 1,
                )
            }
            rated_ordinals = sorted(
                (
                    ordinal
                    for ordinal, status in statuses.items()
                    if status == 'rated'
                ),
                key=lambda ordinal: (-self._score(ordinal), ordinal),
            )
            place_by_ordinal = {}
            previous_score = None
            dense_place = 0
            for ordinal in rated_ordinals:
                score = self._score(ordinal)
                if previous_score is None or score != previous_score:
                    dense_place += 1
                place_by_ordinal[ordinal] = dense_place
                previous_score = score
            unrated_ordinals = sorted(
                ordinal
                for ordinal, status in statuses.items()
                if status != 'rated'
            )
            rows = []
            for display_order, ordinal in enumerate(
                rated_ordinals + unrated_ordinals,
                start=1,
            ):
                row_status = statuses[ordinal]
                if row_status == 'rated':
                    row = self._rated_row(
                        ordinal=ordinal,
                        day=day,
                        display_order=display_order,
                        place=place_by_ordinal[ordinal],
                        previous_statuses=previous_statuses,
                        previous_places=previous_places,
                    )
                else:
                    row = self._unrated_row(
                        ordinal=ordinal,
                        day=day,
                        display_order=display_order,
                        row_status=row_status,
                    )
                rows.append(row)

            rated_rows = [
                row for row in rows if row['row_status'] == 'rated'
            ]
            withheld_rows = [
                row for row in rows if row['row_status'] == 'withheld'
            ]
            not_observed_rows = [
                row for row in rows if row['row_status'] == 'not_observed'
            ]
            withheld_reasons = {}
            for row in withheld_rows:
                for reason, count in row['withheld_reasons'].items():
                    withheld_reasons[reason] = (
                        withheld_reasons.get(reason, 0) + count
                    )
            withheld_reasons = dict(sorted(withheld_reasons.items()))
            work_date = self.period_start + timedelta(days=day - 1)
            as_of = datetime.combine(
                work_date,
                time(hour=22),
                tzinfo=QA_TZ,
            )
            rated_shift_count = sum(
                row['shift_count']
                for row in rated_rows
            )
            withheld_shift_count = sum(
                row['withheld_shift_count']
                for row in withheld_rows
            )
            payload = {
                'available': True,
                'calculation_available': True,
                'official': False,
                'official_rating_eligible': False,
                'synthetic': True,
                'formula_evaluated': True,
                'rating_mode': RATING_TV_FORMULA_REPLAY_MODE,
                'scope_type': 'rating_period',
                'formula_version': DRIVER_RATING_FORMULA_VERSION,
                'formula_label': (
                    'Рабочая формула без м³·км и т·км'
                ),
                'status': (
                    f'Синтетический формульный расчёт дня {day}. '
                    'Не является официальным рейтингом.'
                ),
                'generated_at': as_of.isoformat(),
                'source_raw_path': (
                    f'raw_formula/night/day_{day:02d}.json'
                ),
                'source_raw_sha256': _sha(f'raw-{day}'),
                'source_fingerprint': _sha(f'source-{day}'),
                'shift_score_fingerprint': _sha(f'scores-{day}'),
                'rating_period': self.rating_period,
                'watch_composition': self.watch_composition,
                'shift_type': 'night',
                'shift_type_label': 'Ночная',
                'available_rating_periods': [self.rating_period],
                'available_watch_compositions': [self.watch_composition],
                'calculation_window': {
                    'starts_on': self.period_start.isoformat(),
                    'ends_before': (
                        work_date + timedelta(days=1)
                    ).isoformat(),
                },
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
                    'candidate_closed_shift_count': (
                        rated_shift_count + withheld_shift_count
                    ),
                    'linked_to_selected_composition_count': (
                        rated_shift_count + withheld_shift_count
                    ),
                    'unlinked_shift_count': 0,
                    'linked_to_other_composition_count': 0,
                    'selected_watch_date_mismatch_count': 0,
                    'covered_watch_period_count': 1,
                    'linkage_ready': True,
                },
                'calculation_summary': {
                    'employee_count': len(rated_rows),
                    'rated_shift_count': rated_shift_count,
                    'withheld_shift_count': withheld_shift_count,
                    'withheld_reasons': withheld_reasons,
                    'trip_count': sum(
                        row['trip_count']
                        for row in rated_rows
                    ),
                    'volume_m3': _q2(sum(
                        (
                            Decimal(row['volume_m3'])
                            for row in rated_rows
                        ),
                        Decimal('0'),
                    )),
                    'tonnage_t': _q2(sum(
                        (
                            Decimal(row['tonnage_t'])
                            for row in rated_rows
                        ),
                        Decimal('0'),
                    )),
                },
                'display_summary': {
                    'cohort_employee_count': len(rows),
                    'rated_employee_count': len(rated_rows),
                    'withheld_employee_count': len(withheld_rows),
                    'not_observed_employee_count': len(not_observed_rows),
                },
                'entries': rows,
                'qa_day': day,
                'qa_day_count': RATING_TV_FORMULA_REPLAY_DAY_COUNT,
                'qa_work_date': work_date.isoformat(),
                'replay_run_id': self.run_id,
            }
            snapshots.append({
                'day': day,
                'work_date': work_date.isoformat(),
                'as_of': as_of.isoformat(),
                'payload': payload,
            })
            previous_statuses = {
                row['employee_id']: row['row_status']
                for row in rows
            }
            previous_places = {
                row['employee_id']: row['place']
                for row in rated_rows
            }

        document = {
            'schema': RATING_TV_FORMULA_REPLAY_SCHEMA,
            'schema_version': RATING_TV_FORMULA_REPLAY_SCHEMA_VERSION,
            'data_classification': (
                RATING_TV_FORMULA_REPLAY_DATA_CLASSIFICATION
            ),
            'synthetic': True,
            'formula_evaluated': True,
            'official': False,
            'official_rating_eligible': False,
            'warning': (
                'СИНТЕТИЧЕСКИЙ ФОРМУЛЬНЫЙ QA-ПРОГОН. '
                'Не использовать для премирования.'
            ),
            'replay': {
                'id': self.run_id,
                'label': 'Тестовый 30-дневный формульный replay',
                'scenario_version': 'DRIVER_RATING_FORMULA_QA_V1',
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
                'created_at': '2026-07-30T12:00:00+04:00',
                'formula_version': DRIVER_RATING_FORMULA_VERSION,
                'formula_label': (
                    'Рабочая формула без м³·км и т·км'
                ),
                'timezone': 'Asia/Vladivostok',
                'source_commit': (
                    '22ba31f4ce7759423450b2403b58509c115ee0bf'
                ),
                'source_run_id': 'QA-PG-30D-20260730-01',
                'source_manifest_sha256': _sha('source-manifest'),
                'source_database_classification': (
                    'isolated_synthetic_postgresql_qa'
                ),
                'notice': (
                    'Формула выполнена на изолированной синтетической базе. '
                    'Результат неофициальный.'
                ),
            },
            'scope': {
                'scope_type': 'rating_period',
                'profession': 'driver',
                'profession_label': 'Водитель самосвала',
                'rating_period': self.rating_period,
                'watch_composition': self.watch_composition,
                'shift_type': 'night',
                'shift_type_label': 'Ночная',
                'cohort_sha256': formula_replay_cohort_sha256(self.cohort),
                'cohort': self.cohort,
            },
            'snapshots': snapshots,
        }
        return attach_formula_replay_integrity(document)


class RatingTvFormulaReplayContractTests(SimpleTestCase):
    maxDiff = None

    def setUp(self):
        self.document = FormulaReplayDocumentFactory().build()

    def _reattach(self, document):
        document = deepcopy(document)
        document.pop('integrity', None)
        for snapshot in document['snapshots']:
            snapshot.pop('previous_payload_sha256', None)
            snapshot.pop('payload_sha256', None)
        return attach_formula_replay_integrity(document)

    def test_valid_formula_replay_keeps_30_days_and_full_53_row_cohort(self):
        validate_rating_tv_formula_replay(self.document)

        self.assertEqual(len(self.document['snapshots']), 30)
        self.assertTrue(self.document['formula_evaluated'])
        self.assertFalse(self.document['official'])
        for snapshot in self.document['snapshots']:
            rows = snapshot['payload']['entries']
            self.assertEqual(len(rows), 53)
            self.assertEqual(
                {row['row_status'] for row in rows},
                {'rated', 'withheld', 'not_observed'}
                if snapshot['day'] < 5
                else {'rated', 'withheld'},
            )

    def test_dense_places_use_exact_four_decimal_score_not_display_rounding(self):
        first_day = self.document['snapshots'][0]['payload']['entries']
        first = next(row for row in first_day if row['employee_id'] == -1)
        second = next(row for row in first_day if row['employee_id'] == -2)
        third = next(row for row in first_day if row['employee_id'] == -3)
        fourth = next(row for row in first_day if row['employee_id'] == -4)

        self.assertEqual((first['score'], second['score']), (
            '99.0001',
            '99.0000',
        ))
        self.assertEqual((first['place'], second['place']), (1, 2))
        self.assertEqual(third['score'], fourth['score'])
        self.assertEqual((third['place'], fourth['place']), (3, 3))

    def test_unrated_rows_have_nullable_kpi_and_never_receive_place(self):
        third_day = self.document['snapshots'][2]['payload']['entries']
        for employee_id in (-51, -52, -53):
            row = next(
                item
                for item in third_day
                if item['employee_id'] == employee_id
            )
            for field in (
                'trip_count',
                'volume_m3',
                'tonnage_t',
                'score',
                'blocks',
                'confidence',
                'place',
                'shared_score_place',
                'position_delta',
            ):
                self.assertIsNone(row[field])
            self.assertFalse(row['ranking_eligible'])
            self.assertEqual(row['level'], '')

    def test_transition_rated_withheld_rated_has_no_false_position_delta(self):
        second_day = self.document['snapshots'][1]['payload']['entries']
        third_day = self.document['snapshots'][2]['payload']['entries']
        fourth_day = self.document['snapshots'][3]['payload']['entries']
        second = next(row for row in second_day if row['employee_id'] == -52)
        third = next(row for row in third_day if row['employee_id'] == -52)
        fourth = next(row for row in fourth_day if row['employee_id'] == -52)

        self.assertEqual(second['row_status'], 'rated')
        self.assertEqual(third['row_status'], 'withheld')
        self.assertEqual(fourth['row_status'], 'rated')
        self.assertIsNone(third['position_delta'])
        self.assertIsNone(fourth['position_delta'])

    def test_rejects_fake_zero_score_or_place_for_withheld_row(self):
        broken = deepcopy(self.document)
        row = next(
            item
            for item in broken['snapshots'][0]['payload']['entries']
            if item['employee_id'] == -51
        )
        row['score'] = '0.0000'
        row['place'] = 51
        broken = self._reattach(broken)

        with self.assertRaises(RatingTvFormulaReplayError):
            validate_rating_tv_formula_replay(broken)

    def test_rejects_blocking_quality_on_rated_row_and_confidence_mutation(self):
        broken = deepcopy(self.document)
        row = next(
            item
            for item in broken['snapshots'][0]['payload']['entries']
            if item['employee_id'] == -1
        )
        row['quality_flags'] = ['data_conflict']
        broken = self._reattach(broken)
        with self.assertRaises(RatingTvFormulaReplayError):
            validate_rating_tv_formula_replay(broken)

        broken = deepcopy(self.document)
        row = next(
            item
            for item in broken['snapshots'][0]['payload']['entries']
            if item['employee_id'] == -1
        )
        row['confidence'] = '85.00'
        broken = self._reattach(broken)
        with self.assertRaises(RatingTvFormulaReplayError):
            validate_rating_tv_formula_replay(broken)

    def test_rejects_summary_linkage_and_cumulative_window_drift(self):
        for mutate in (
            lambda payload: payload['display_summary'].__setitem__(
                'cohort_employee_count',
                52,
            ),
            lambda payload: payload['linkage_audit'].__setitem__(
                'linkage_ready',
                False,
            ),
            lambda payload: payload['calculation_window'].__setitem__(
                'ends_before',
                '2026-05-30',
            ),
        ):
            broken = deepcopy(self.document)
            mutate(broken['snapshots'][0]['payload'])
            broken = self._reattach(broken)
            with self.assertRaises(RatingTvFormulaReplayError):
                validate_rating_tv_formula_replay(broken)

    def test_rejects_extra_field_pii_nan_and_integrity_tampering(self):
        broken = deepcopy(self.document)
        broken['snapshots'][0]['payload']['entries'][0]['unexpected'] = True
        broken = self._reattach(broken)
        with self.assertRaises(RatingTvFormulaReplayError):
            validate_rating_tv_formula_replay(broken)

        broken = deepcopy(self.document)
        broken['snapshots'][0]['payload']['calculation_summary'][
            'withheld_reasons'
        ] = {'phone': 0}
        broken = self._reattach(broken)
        with self.assertRaises(RatingTvFormulaReplayError):
            validate_rating_tv_formula_replay(broken)

        broken = deepcopy(self.document)
        broken['snapshots'][0]['payload']['entries'][0]['score'] = float('nan')
        with self.assertRaises(RatingTvFormulaReplayError):
            attach_formula_replay_integrity(broken)

        broken = deepcopy(self.document)
        broken['snapshots'][0]['payload']['status'] = 'Подмена'
        with self.assertRaises(RatingTvFormulaReplayError):
            validate_rating_tv_formula_replay(broken)

    def test_loader_requires_exact_external_sha_and_rejects_duplicate_keys(self):
        raw = (
            json.dumps(
                self.document,
                ensure_ascii=False,
                indent=2,
            )
            + '\n'
        ).encode('utf-8')
        expected_sha256 = hashlib.sha256(raw).hexdigest().upper()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'formula-replay.json'
            path.write_bytes(raw)
            loaded, actual_sha256 = load_rating_tv_formula_replay(
                path,
                expected_sha256=expected_sha256,
            )
            self.assertEqual(loaded, self.document)
            self.assertEqual(actual_sha256, expected_sha256)
            with self.assertRaises(RatingTvFormulaReplayError):
                load_rating_tv_formula_replay(
                    path,
                    expected_sha256='0' * 64,
                )

            duplicate_path = Path(temp_dir) / 'duplicate.json'
            duplicate_raw = b'{"schema":"first","schema":"second"}'
            duplicate_path.write_bytes(duplicate_raw)
            with self.assertRaises(RatingTvFormulaReplayError):
                load_rating_tv_formula_replay(
                    duplicate_path,
                    expected_sha256=hashlib.sha256(
                        duplicate_raw,
                    ).hexdigest().upper(),
                )
