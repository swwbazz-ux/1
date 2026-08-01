from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from core.production_time import production_work_date
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from portal.auth import PORTAL_EMPLOYEE_SESSION_KEY
from references.models import (
    DumpPoint,
    Equipment,
    EquipmentModel,
    EquipmentType,
    RockType,
)
from shifts.models import EmployeeShift, ShiftType, WatchPeriod
from users.active_role import (
    ACTIVE_ROLE_CODE_SESSION_KEY,
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    ACTIVE_ROLE_SESSION_KEY,
)
from users.models import (
    Employee,
    EmployeeAccess,
    Role,
    WatchComposition,
)

from .driver_watch_rating import (
    DRIVER_RATING_FORMULA_VERSION,
    DRIVER_RATING_WEIGHTS,
    _assignments_score,
    _context_cycle_median,
    _trip_context,
    build_driver_watch_rating,
    get_cached_driver_watch_rating,
)
from .models import (
    DriverRatingPeriodMaterializedSnapshot,
    DriverShiftPassportSnapshot,
    RatingPeriod,
)
from .portal_rating_provider import DriverRatingProductionDataProvider
from .driver_shift_passport_snapshots import _fingerprint


def rating_test_employee_scope_provider(*, queryset, site_code):
    return queryset.exclude(full_name__startswith='ВНЕ ОБЛАСТИ')


def force_snapshot_payload_update(snapshot, payload):
    field = DriverShiftPassportSnapshot._meta.get_field('payload')
    database_value = connection.ops.adapt_json_value(
        payload,
        field.encoder,
    )
    table_name = connection.ops.quote_name(
        DriverShiftPassportSnapshot._meta.db_table,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            f'UPDATE {table_name} SET payload = %s WHERE id = %s',
            [database_value, snapshot.pk],
        )


def refresh_test_rating_materialization(rating_period):
    call_command(
        'refresh_driver_rating_snapshots',
        rating_period=rating_period.id,
        legacy_watch_groups=True,
        strict=True,
        stdout=StringIO(),
        stderr=StringIO(),
        verbosity=0,
    )


class DriverRatingFixtureMixin:
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        now = timezone.now().replace(microsecond=0)
        self.now = now
        self.composition = WatchComposition.objects.create(
            code='rating-test-watch',
            name='Тестовый состав рейтинга',
        )
        self.watch = WatchPeriod.objects.create(
            name='Тестовая вахта рейтинга',
            watch_composition=self.composition,
            starts_on=timezone.localdate(now) - timedelta(days=30),
            ends_on=timezone.localdate(now) + timedelta(days=10),
        )
        truck_type = EquipmentType.objects.create(
            name='Самосвал тест рейтинга',
        )
        self.model = EquipmentModel.objects.create(
            equipment_type=truck_type,
            name='Модель тест рейтинга',
            payload_tons=Decimal('100'),
            body_volume_m3=Decimal('50'),
        )
        self.truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=self.model,
            garage_number='RATING-01',
        )
        self.rock = RockType.objects.create(
            name='Порода тест рейтинга',
            density=Decimal('2.50'),
        )
        self.dump = DumpPoint.objects.create(
            name='Точка тест рейтинга',
        )
        self.driver_role = Role.objects.create(
            code='driver',
            name='Водитель тест рейтинга',
        )

    def employee(self, name):
        employee = Employee.objects.create(
            full_name=name,
            status=Employee.Status.ACTIVE,
            is_active=True,
            work_category=Employee.WorkCategory.DRIVER,
            watch_composition=self.composition,
        )
        EmployeeAccess.objects.create(
            employee=employee,
            role=self.driver_role,
            access_code=f'{employee.id:06d}'[-6:],
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        return employee

    def snapshot(
        self,
        employee,
        *,
        ordinal,
        trip_count,
        cycle_seconds=600,
        quality_flags=('unexplained_time',),
        shift_type=ShiftType.DAY,
        route_matches=True,
        excavator_id=100,
        loading_horizon='',
        loading_block='',
        distance_complete=False,
        include_transport_distance=True,
        transport_distance_km=None,
        include_distance_metrics=True,
        m3_km_known_value='0',
        t_km_known_value='0',
        downtime_review_seconds=0,
        scheduled_window_status='schedule_snapshot_unavailable',
        unjustified_short_shift_seconds=None,
        extra_presence_seconds=0,
        confirmed_extra_productive_seconds=0,
        inferred_schedule_gap_seconds=None,
        work_time_rating_available=False,
        work_time_rating_status=(
            'neutral_structural_schedule_and_reason_policy_unavailable'
        ),
    ):
        opened_at = self.now - timedelta(days=ordinal, hours=12)
        closed_at = opened_at + timedelta(hours=12)
        shift = EmployeeShift.objects.create(
            employee=employee,
            shift_type=shift_type,
            workplace_code='driver',
            watch_period=self.watch,
            equipment=self.truck,
            start_fuel=Decimal('1000'),
            start_mileage=Decimal('10000'),
            start_engine_hours=Decimal('2000'),
            end_fuel=Decimal('900'),
            end_mileage=Decimal('10100'),
            end_engine_hours=Decimal('2010'),
            opened_at=opened_at,
            closed_at=closed_at,
            opened_by=employee,
            closed_by=employee,
        )
        trips = []
        elapsed_seconds = 0
        for index in range(trip_count):
            trip_cycle_seconds = (
                cycle_seconds[index % len(cycle_seconds)]
                if isinstance(cycle_seconds, (tuple, list))
                else cycle_seconds
            )
            created_at = opened_at + timedelta(
                minutes=10,
                seconds=elapsed_seconds,
            )
            completed_at = created_at + timedelta(
                seconds=trip_cycle_seconds
            )
            elapsed_seconds += trip_cycle_seconds + 30
            trip = {
                'id': ordinal * 1000 + index,
                'truck_model_id': self.model.id,
                'truck': {
                    'id': self.truck.id,
                    'model_id': self.model.id,
                },
                'rock_type_id': self.rock.id,
                'rock_type': {
                    'id': self.rock.id,
                    'density': '2.5000',
                },
                'dump_point_id': self.dump.id,
                'excavator_id': excavator_id,
                'loading_horizon': loading_horizon,
                'loading_block': loading_block,
                'actual_dump_point_id': self.dump.id,
                'assigned_dump_point_id': self.dump.id,
                'unloading_shift_id': shift.id,
                'driver_id': employee.id,
                'volume_m3': '50.00',
                'tonnage': '125.00',
                'created_at': created_at.isoformat(),
                'completed_at': completed_at.isoformat(),
                'is_carryover': False,
            }
            if include_transport_distance:
                trip['transport_distance_km'] = transport_distance_km
            trips.append(trip)
        route_match_count = trip_count if route_matches else 0
        route_mismatch_count = 0 if route_matches else trip_count
        production = {
            'completed_trip_count': trip_count,
            'output_attribution': {
                'unloading_shift_trip_count': trip_count,
                'legacy_driver_trip_count': 0,
                'ambiguous_trip_count': 0,
            },
            'volume_m3': {
                'known_value': str(Decimal(trip_count) * Decimal('50')),
                'is_complete': True,
            },
            'tonnage_t': {
                'known_value': str(Decimal(trip_count) * Decimal('125')),
                'is_complete': True,
            },
        }
        if include_distance_metrics:
            production['m3_km'] = {
                'known_value': m3_km_known_value,
                'is_complete': distance_complete,
            }
            production['t_km'] = {
                'known_value': t_km_known_value,
                'is_complete': distance_complete,
            }
        payload = {
            'schema_version': 1,
            'calculator_version': 'rating-test-v1',
            'official': False,
            'shift_id': shift.id,
            'source_manifest': {
                'manifest_schema_version': 1,
                'shift': {
                    'id': shift.id,
                    'employee_id': employee.id,
                    'equipment_id': self.truck.id,
                    'workplace_code': 'driver',
                    'shift_type': shift_type,
                    'opened_at': opened_at.isoformat(),
                    'closed_at': closed_at.isoformat(),
                    'watch_period': {
                        'id': self.watch.id,
                        'watch_composition': {
                            'id': self.composition.id,
                        },
                    },
                },
                'trips': trips,
                'downtimes': [],
                'assignments': [],
                'reading_corrections': [],
            },
            'passport': {
                'production': production,
                'time': {
                    'available_seconds': 43200,
                    'downtime_review_seconds': downtime_review_seconds,
                    'scheduled_window_status': scheduled_window_status,
                    'unjustified_short_shift_seconds': (
                        unjustified_short_shift_seconds
                    ),
                    'extra_presence_seconds': extra_presence_seconds,
                    'confirmed_extra_productive_seconds': (
                        confirmed_extra_productive_seconds
                    ),
                    'inferred_schedule_gap_seconds': (
                        inferred_schedule_gap_seconds
                    ),
                    'work_time_rating_available': (
                        work_time_rating_available
                    ),
                    'work_time_rating_status': work_time_rating_status,
                },
                'routing': {
                    'match_count': route_match_count,
                    'mismatch_count': route_mismatch_count,
                    'missing_actual_count': 0,
                    'missing_assigned_count': 0,
                },
                'open_close': {
                    'window_valid': True,
                    'opened_by_employee': True,
                    'closed_by_employee': True,
                    'service_closed': False,
                    'start_readings_complete': True,
                    'end_readings_complete': True,
                },
                'quality': {
                    'coverage_percent': 45,
                    'flags': list(quality_flags),
                    'quality_metrics': {
                        'trip_without_assignment_seconds': 0,
                        'trip_assignment_mismatch_seconds': 0,
                    },
                    'official_rating_eligible': False,
                },
            },
        }
        return DriverShiftPassportSnapshot.objects.create(
            shift=shift,
            revision=1,
            schema_version=1,
            calculator_version='rating-test-v1',
            source_fingerprint=_fingerprint(payload['source_manifest']),
            payload_fingerprint=_fingerprint(payload),
            payload=payload,
            trigger='driver_close',
        )


class DriverWatchRatingTests(
    DriverRatingFixtureMixin,
    TestCase,
):
    def test_rating_works_without_distance_and_keeps_diagnostic_passport(self):
        first = self.employee('Водитель быстрый')
        second = self.employee('Водитель обычный')
        self.snapshot(first, ordinal=1, trip_count=23)
        self.snapshot(first, ordinal=2, trip_count=22)
        self.snapshot(second, ordinal=3, trip_count=17)
        self.snapshot(second, ordinal=4, trip_count=18)

        result = build_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )

        self.assertTrue(result['available'])
        self.assertFalse(result['official'])
        self.assertEqual(
            result['formula_version'],
            DRIVER_RATING_FORMULA_VERSION,
        )
        self.assertEqual(result['distance_metrics']['weight'], '0')
        self.assertEqual(result['summary']['rated_shift_count'], 4)
        self.assertEqual(result['summary']['withheld_shift_count'], 0)
        self.assertEqual(result['entries'][0]['employee_id'], first.id)
        self.assertEqual(result['entries'][0]['place'], 1)
        self.assertGreater(
            Decimal(result['entries'][0]['score']),
            Decimal(result['entries'][1]['score']),
        )
        snapshots = DriverShiftPassportSnapshot.objects.order_by('shift_id')
        self.assertTrue(all(
            snapshot.payload['official'] is False
            and 'score' not in snapshot.payload
            and snapshot.payload['passport']['quality'][
                'official_rating_eligible'
            ] is False
            for snapshot in snapshots
        ))

    def test_formula_version_declares_neutral_work_time_policy(self):
        self.assertEqual(
            DRIVER_RATING_FORMULA_VERSION,
            'DRIVER_WATCH_V3_NO_DISTANCE_TIME_POLICY_NEUTRAL',
        )

    def test_unjustified_none_stays_neutral_without_reason_policy(self):
        driver = self.employee(
            'Р’РѕРґРёС‚РµР»СЊ СЃ РЅР°Р±Р»СЋРґР°РµРјС‹Рј РѕРєРЅРѕРј СЃРјРµРЅС‹',
        )
        self.snapshot(
            driver,
            ordinal=1,
            trip_count=20,
            scheduled_window_status=(
                'structural_schedule_observed'
            ),
            unjustified_short_shift_seconds=None,
            work_time_rating_available=True,
            work_time_rating_status=(
                'assessed_structural_schedule_and_reason_policy'
            ),
        )

        result = build_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )

        self.assertTrue(result['available'])
        self.assertEqual(
            result['entries'][0]['blocks']['work_time'],
            '50.0000',
        )

    def test_inferred_presence_and_productive_extra_do_not_change_rank(self):
        baseline = self.employee('ТЕСТ водитель без отклонения')
        early = self.employee('ТЕСТ водитель с графиком 06-18')
        self.snapshot(
            baseline,
            ordinal=1,
            trip_count=20,
            scheduled_window_status='standard_production_shift_inferred',
            inferred_schedule_gap_seconds=0,
        )
        self.snapshot(
            early,
            ordinal=2,
            trip_count=20,
            scheduled_window_status='standard_production_shift_inferred',
            extra_presence_seconds=3600,
            confirmed_extra_productive_seconds=1200,
            inferred_schedule_gap_seconds=3600,
        )

        result = build_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )
        by_employee = {
            entry['employee_id']: entry
            for entry in result['entries']
        }

        self.assertEqual(
            by_employee[baseline.id]['blocks']['work_time'],
            '50.0000',
        )
        self.assertEqual(
            by_employee[early.id]['blocks']['work_time'],
            '50.0000',
        )
        self.assertEqual(
            by_employee[baseline.id]['score'],
            by_employee[early.id]['score'],
        )
        self.assertEqual(by_employee[baseline.id]['place'], 1)
        self.assertEqual(by_employee[early.id]['place'], 1)

    def test_unknown_schedule_status_is_neutral_with_zero_confidence_component(
        self,
    ):
        unavailable = self.employee(
            'ТЕСТ водитель без снимка графика',
        )
        unknown = self.employee(
            'ТЕСТ водитель с неизвестным статусом графика',
        )
        self.snapshot(
            unavailable,
            ordinal=1,
            trip_count=20,
        )
        self.snapshot(
            unknown,
            ordinal=2,
            trip_count=20,
            scheduled_window_status='unexpected_schedule_status',
            unjustified_short_shift_seconds=3600,
            work_time_rating_available=True,
            work_time_rating_status=(
                'assessed_structural_schedule_and_reason_policy'
            ),
        )

        result = build_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )
        by_employee = {
            entry['employee_id']: entry
            for entry in result['entries']
        }

        self.assertEqual(
            by_employee[unknown.id]['blocks']['work_time'],
            '50.0000',
        )
        self.assertEqual(
            by_employee[unavailable.id]['confidence'],
            by_employee[unknown.id]['confidence'],
        )
        self.assertEqual(
            by_employee[unknown.id]['confidence'],
            '52.5000',
        )
        self.assertEqual(
            by_employee[unavailable.id]['score'],
            by_employee[unknown.id]['score'],
        )
        self.assertEqual(by_employee[unavailable.id]['place'], 1)
        self.assertEqual(by_employee[unknown.id]['place'], 1)

    def test_zero_weight_distance_is_invariant_for_missing_null_zero_and_arbitrary(
        self,
    ):
        variants = (
            (
                'missing',
                {
                    'include_transport_distance': False,
                    'include_distance_metrics': False,
                },
            ),
            (
                'null',
                {
                    'transport_distance_km': None,
                    'm3_km_known_value': None,
                    't_km_known_value': None,
                },
            ),
            (
                'zero',
                {
                    'transport_distance_km': '0',
                    'distance_complete': True,
                },
            ),
            (
                'arbitrary',
                {
                    'transport_distance_km': '12.5',
                    'm3_km_known_value': '12500',
                    't_km_known_value': '31250',
                    'distance_complete': True,
                },
            ),
        )
        employees = {}
        snapshots = {}
        for ordinal, (variant, options) in enumerate(variants, start=1):
            employee = self.employee(f'Водитель дистанция {variant}')
            employees[variant] = employee
            snapshots[variant] = self.snapshot(
                employee,
                ordinal=ordinal,
                trip_count=20,
                **options,
            )

        result = build_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )

        self.assertTrue(result['available'])
        self.assertEqual(result['summary']['employee_count'], 4)
        self.assertEqual(result['summary']['rated_shift_count'], 4)
        self.assertEqual(result['summary']['withheld_shift_count'], 0)
        self.assertEqual(result['summary']['withheld_reasons'], {})
        self.assertEqual(
            {entry['employee_id'] for entry in result['entries']},
            {employee.id for employee in employees.values()},
        )

        signatures = {
            (
                tuple(sorted(entry['blocks'].items())),
                entry['confidence'],
                entry['score'],
                entry['place'],
                entry['shared_score_place'],
            )
            for entry in result['entries']
        }
        self.assertEqual(len(signatures), 1)
        for entry in result['entries']:
            self.assertEqual(
                set(entry['blocks']),
                set(DRIVER_RATING_WEIGHTS),
            )
            self.assertEqual(len(entry['blocks']), 5)
            self.assertEqual(entry['place'], 1)
            self.assertEqual(entry['shared_score_place'], 1)

        distance_metrics = result['distance_metrics']
        self.assertEqual(distance_metrics['weight'], '0')
        self.assertEqual(
            distance_metrics['label'],
            'м³·км и т·км пока не учитываются',
        )
        self.assertNotIn('value', distance_metrics)
        self.assertNotIn('known_value', distance_metrics)

        missing_payload = snapshots['missing'].payload
        self.assertNotIn(
            'transport_distance_km',
            missing_payload['source_manifest']['trips'][0],
        )
        self.assertNotIn(
            'm3_km',
            missing_payload['passport']['production'],
        )
        self.assertNotIn(
            't_km',
            missing_payload['passport']['production'],
        )
        self.assertIsNone(
            snapshots['null'].payload['passport']['production'][
                'm3_km'
            ]['known_value'],
        )
        self.assertEqual(
            snapshots['zero'].payload['passport']['production'][
                'm3_km'
            ]['known_value'],
            '0',
        )
        self.assertEqual(
            snapshots['arbitrary'].payload['passport']['production'][
                'm3_km'
            ]['known_value'],
            '12500',
        )

    def test_equal_scores_share_the_same_place(self):
        first = self.employee('Водитель одинаковый А')
        second = self.employee('Водитель одинаковый Б')
        self.snapshot(first, ordinal=1, trip_count=20)
        self.snapshot(second, ordinal=2, trip_count=20)

        result = build_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )

        self.assertEqual(
            result['entries'][0]['score'],
            result['entries'][1]['score'],
        )
        self.assertEqual(
            [
                result['entries'][0]['shared_score_place'],
                result['entries'][1]['shared_score_place'],
            ],
            [1, 1],
        )
        self.assertEqual(
            [
                result['entries'][0]['place'],
                result['entries'][1]['place'],
            ],
            [1, 1],
        )
        self.assertEqual(
            [
                result['entries'][0]['employee_id'],
                result['entries'][1]['employee_id'],
            ],
            sorted((first.id, second.id)),
        )

    def test_blocking_quality_flag_withholds_only_affected_shift(self):
        valid_driver = self.employee('Водитель валидный')
        blocked_driver = self.employee('Водитель конфликт')
        self.snapshot(valid_driver, ordinal=1, trip_count=20)
        self.snapshot(
            blocked_driver,
            ordinal=2,
            trip_count=20,
            quality_flags=('data_conflict',),
        )

        result = build_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )

        self.assertTrue(result['available'])
        self.assertEqual(result['summary']['rated_shift_count'], 1)
        self.assertEqual(result['summary']['withheld_shift_count'], 1)
        self.assertEqual(len(result['entries']), 1)
        self.assertEqual(
            result['entries'][0]['employee_id'],
            valid_driver.id,
        )
        self.assertIn(
            'blocking_quality:data_conflict',
            result['summary']['withheld_reasons'],
        )

    def test_unknown_quality_flag_is_fail_closed(self):
        valid = self.employee('Водитель известные флаги')
        unknown = self.employee('Водитель неизвестный флаг')
        self.snapshot(valid, ordinal=1, trip_count=20)
        self.snapshot(
            unknown,
            ordinal=2,
            trip_count=20,
            quality_flags=('new_unclassified_quality_flag',),
        )

        result = build_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )

        self.assertEqual(
            [row['employee_id'] for row in result['entries']],
            [valid.id],
        )
        self.assertIn(
            'unknown_quality:new_unclassified_quality_flag',
            result['summary']['withheld_reasons'],
        )

    def test_payload_fingerprint_mismatch_is_withheld(self):
        valid = self.employee('Водитель целый паспорт')
        tampered = self.employee('Водитель изменённый паспорт')
        self.snapshot(valid, ordinal=1, trip_count=20)
        snapshot = self.snapshot(tampered, ordinal=2, trip_count=20)
        payload = snapshot.payload
        payload['passport']['production']['completed_trip_count'] = 19
        force_snapshot_payload_update(snapshot, payload)

        result = build_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )

        self.assertEqual(
            [row['employee_id'] for row in result['entries']],
            [valid.id],
        )
        self.assertIn(
            'payload_fingerprint_mismatch',
            result['summary']['withheld_reasons'],
        )

    def test_route_mismatch_reduces_assignments_block(self):
        correct = self.employee('Водитель маршрут верный')
        mismatch = self.employee('Водитель маршрут неверный')
        self.snapshot(correct, ordinal=1, trip_count=20)
        self.snapshot(
            mismatch,
            ordinal=2,
            trip_count=20,
            route_matches=False,
        )

        result = build_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )
        by_employee = {
            row['employee_id']: row
            for row in result['entries']
        }

        self.assertGreater(
            Decimal(by_employee[correct.id]['blocks']['assignments']),
            Decimal(by_employee[mismatch.id]['blocks']['assignments']),
        )
        self.assertGreater(
            Decimal(by_employee[correct.id]['score']),
            Decimal(by_employee[mismatch.id]['score']),
        )

    def test_downtime_requiring_review_withholds_affected_shift(self):
        valid = self.employee('Водитель простой подтверждён')
        review = self.employee('Водитель простой на проверке')
        self.snapshot(valid, ordinal=1, trip_count=20)
        self.snapshot(
            review,
            ordinal=2,
            trip_count=20,
            quality_flags=(),
            downtime_review_seconds=1,
        )

        result = build_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )

        self.assertTrue(result['available'])
        self.assertEqual(result['summary']['rated_shift_count'], 1)
        self.assertEqual(result['summary']['withheld_shift_count'], 1)
        self.assertEqual(
            [row['employee_id'] for row in result['entries']],
            [valid.id],
        )
        self.assertIn(
            'downtime_requires_review',
            result['summary']['withheld_reasons'],
        )

    def test_stable_cycles_score_above_variable_cycles(self):
        stable = self.employee('Водитель стабильный цикл')
        variable = self.employee('Водитель нестабильный цикл')
        self.snapshot(
            stable,
            ordinal=1,
            trip_count=20,
            cycle_seconds=600,
        )
        self.snapshot(
            variable,
            ordinal=2,
            trip_count=20,
            cycle_seconds=(300, 900),
        )

        result = build_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )
        by_employee = {
            row['employee_id']: row
            for row in result['entries']
        }

        self.assertGreater(
            Decimal(by_employee[stable.id]['blocks']['stability']),
            Decimal(by_employee[variable.id]['blocks']['stability']),
        )
        self.assertGreater(
            Decimal(by_employee[stable.id]['score']),
            Decimal(by_employee[variable.id]['score']),
        )

    def test_cycle_context_uses_excavator_horizon_and_block(self):
        first_context = _trip_context({
            'truck_model_id': self.model.id,
            'rock_type_id': self.rock.id,
            'excavator_id': 101,
            'actual_dump_point_id': self.dump.id,
            'loading_horizon': '  ГОРИЗОНТ   1 ',
            'loading_block': 'БЛОК 1',
        })
        second_context = _trip_context({
            'truck_model_id': self.model.id,
            'rock_type_id': self.rock.id,
            'excavator_id': 202,
            'actual_dump_point_id': self.dump.id,
            'loading_horizon': 'горизонт 2',
            'loading_block': '  блок   2 ',
        })
        first_key = (
            self.model.id,
            self.rock.id,
            101,
            self.dump.id,
            'горизонт 1',
            'блок 1',
        )
        second_key = (
            self.model.id,
            self.rock.id,
            202,
            self.dump.id,
            'горизонт 2',
            'блок 2',
        )
        cycle_samples = {
            'exact': {
                first_key: [(10, Decimal('600'))] * 20,
                second_key: [(20, Decimal('900'))] * 20,
            },
            'excavator_route': {},
            'route': {},
            'model_rock': {},
            'model': {},
            'peer': {
                'peer': (
                    [(10, Decimal('600'))] * 20
                    + [(20, Decimal('900'))] * 20
                ),
            },
        }
        cycle_medians = {
            'exact': {
                first_key: Decimal('600'),
                second_key: Decimal('900'),
            },
            'excavator_route': {},
            'route': {},
            'model_rock': {},
            'model': {},
            'peer': {'peer': Decimal('750')},
        }

        self.assertEqual(first_context['loading_horizon'], 'горизонт 1')
        self.assertEqual(first_context['loading_block'], 'блок 1')
        self.assertEqual(
            _context_cycle_median(
                first_context,
                cycle_samples,
                cycle_medians,
                excluded_shift_id=999,
            ),
            Decimal('600'),
        )
        self.assertEqual(
            _context_cycle_median(
                second_context,
                cycle_samples,
                cycle_medians,
                excluded_shift_id=999,
            ),
            Decimal('900'),
        )

    def test_missing_distance_does_not_reduce_confidence(self):
        without_distance = self.employee('Водитель без расстояния')
        with_distance = self.employee('Водитель с расстоянием')
        self.snapshot(
            without_distance,
            ordinal=1,
            trip_count=20,
            distance_complete=False,
        )
        self.snapshot(
            with_distance,
            ordinal=2,
            trip_count=20,
            distance_complete=True,
        )

        result = build_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )
        by_employee = {
            row['employee_id']: row
            for row in result['entries']
        }

        self.assertEqual(
            by_employee[without_distance.id]['confidence'],
            by_employee[with_distance.id]['confidence'],
        )

    def test_invalid_shift_withholds_all_shifts_of_same_employee(self):
        complete = self.employee('Водитель полное покрытие')
        partial = self.employee('Водитель неполное покрытие')
        self.snapshot(complete, ordinal=1, trip_count=20)
        self.snapshot(partial, ordinal=2, trip_count=20)
        self.snapshot(
            partial,
            ordinal=3,
            trip_count=20,
            quality_flags=('data_conflict',),
        )

        result = build_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )

        self.assertEqual(
            [row['employee_id'] for row in result['entries']],
            [complete.id],
        )
        self.assertEqual(result['summary']['rated_shift_count'], 1)
        self.assertEqual(result['summary']['withheld_shift_count'], 2)
        self.assertEqual(
            result['summary']['withheld_reasons'][
                'employee_partial_coverage'
            ],
            1,
        )

    def test_unlinked_shift_in_same_dates_does_not_disable_watch(self):
        rated = self.employee('Водитель выбранной вахты')
        unrelated = self.employee('Водитель без вахты')
        self.snapshot(rated, ordinal=1, trip_count=20)
        opened_at = self.now - timedelta(days=2, hours=12)
        EmployeeShift.objects.create(
            employee=unrelated,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            equipment=self.truck,
            opened_at=opened_at,
            closed_at=opened_at + timedelta(hours=12),
            opened_by=unrelated,
            closed_by=unrelated,
        )

        result = build_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )

        self.assertTrue(result['available'])
        self.assertEqual(len(result['entries']), 1)
        self.assertGreater(
            result['linkage_audit']['unlinked_shift_count'],
            0,
        )

    def test_single_shift_does_not_calibrate_its_own_cycle_speed(self):
        first = self.employee('Водитель одиночный быстрый цикл')
        self.snapshot(
            first,
            ordinal=1,
            trip_count=20,
            cycle_seconds=100,
        )
        first_result = build_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )

        second_watch = WatchPeriod.objects.create(
            name='Вторая тестовая вахта рейтинга',
            watch_composition=self.composition,
            starts_on=self.watch.starts_on,
            ends_on=self.watch.ends_on,
        )
        self.watch = second_watch
        second = self.employee('Водитель одиночный медленный цикл')
        self.snapshot(
            second,
            ordinal=2,
            trip_count=20,
            cycle_seconds=2000,
        )
        second_result = build_driver_watch_rating(
            second_watch,
            shift_type=ShiftType.DAY,
        )

        self.assertEqual(
            first_result['entries'][0]['score'],
            second_result['entries'][0]['score'],
        )
        self.assertEqual(
            first_result['entries'][0]['blocks']['production'],
            second_result['entries'][0]['blocks']['production'],
        )

    def test_fully_withheld_employee_reports_both_withheld_shifts(self):
        partial = self.employee('Водитель полностью удержан')
        self.snapshot(partial, ordinal=1, trip_count=20)
        self.snapshot(
            partial,
            ordinal=2,
            trip_count=20,
            quality_flags=('data_conflict',),
        )

        result = build_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )

        self.assertFalse(result['available'])
        self.assertEqual(result['summary']['rated_shift_count'], 0)
        self.assertEqual(result['summary']['withheld_shift_count'], 2)
        self.assertEqual(
            result['summary']['withheld_reasons'],
            {
                'blocking_quality:data_conflict': 1,
                'employee_partial_coverage': 1,
            },
        )

    def test_cached_repeat_preserves_source_and_scores(self):
        driver = self.employee('Водитель повторный расчёт')
        self.snapshot(driver, ordinal=1, trip_count=20)

        first = get_cached_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )
        second = get_cached_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )

        self.assertEqual(
            first['source_fingerprint'],
            second['source_fingerprint'],
        )
        self.assertEqual(first['entries'], second['entries'])

    def test_empty_rating_uses_stable_reason_mapping_schema(self):
        watch_without_composition = WatchPeriod.objects.create(
            name='Вахта без состава',
            starts_on=self.watch.starts_on,
            ends_on=self.watch.ends_on,
        )

        result = build_driver_watch_rating(
            watch_without_composition,
            shift_type=ShiftType.DAY,
        )

        self.assertFalse(result['available'])
        self.assertEqual(
            result['summary']['withheld_reasons'],
            {'watch_composition_missing': 0},
        )
        self.assertEqual(result['summary']['withheld_shift_count'], 0)

    def test_cache_failure_falls_back_to_direct_rating(self):
        driver = self.employee('Водитель без кэша')
        self.snapshot(driver, ordinal=1, trip_count=20)

        with (
            patch(
                'reports.driver_watch_rating.cache.get',
                side_effect=RuntimeError('cache get unavailable'),
            ),
            patch(
                'reports.driver_watch_rating.cache.set',
                side_effect=RuntimeError('cache set unavailable'),
            ),
            self.assertLogs(
                'reports.driver_watch_rating',
                level='ERROR',
            ) as logs,
        ):
            result = get_cached_driver_watch_rating(
                self.watch,
                shift_type=ShiftType.DAY,
            )

        self.assertTrue(result['available'])
        self.assertEqual(result['entries'][0]['employee_id'], driver.id)
        self.assertEqual(len(logs.output), 2)

    def test_payload_tamper_invalidates_cached_rating(self):
        valid = self.employee('Водитель кэш целый')
        tampered = self.employee('Водитель кэш изменён')
        self.snapshot(valid, ordinal=1, trip_count=20)
        snapshot = self.snapshot(tampered, ordinal=2, trip_count=20)
        before = get_cached_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )
        payload = snapshot.payload
        payload['passport']['production']['completed_trip_count'] = 19
        force_snapshot_payload_update(snapshot, payload)

        after = get_cached_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )

        self.assertEqual(len(before['entries']), 2)
        self.assertEqual(
            [entry['employee_id'] for entry in after['entries']],
            [valid.id],
        )
        self.assertNotEqual(
            before['source_fingerprint'],
            after['source_fingerprint'],
        )
        self.assertIn(
            'payload_fingerprint_mismatch',
            after['summary']['withheld_reasons'],
        )

    def test_closed_shift_employee_reassignment_invalidates_cache_and_owner(self):
        original = self.employee('Водитель исходной смены')
        replacement = self.employee('Водитель ошибочной перепривязки')
        unaffected = self.employee('Водитель без перепривязки')
        changed_snapshot = self.snapshot(
            original,
            ordinal=1,
            trip_count=20,
        )
        self.snapshot(original, ordinal=2, trip_count=20)
        self.snapshot(replacement, ordinal=3, trip_count=20)
        self.snapshot(unaffected, ordinal=4, trip_count=20)

        before = get_cached_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )
        EmployeeShift.objects.filter(
            pk=changed_snapshot.shift_id,
        ).update(employee=replacement)
        after = get_cached_driver_watch_rating(
            self.watch,
            shift_type=ShiftType.DAY,
        )

        self.assertEqual(len(before['entries']), 3)
        self.assertEqual(
            [entry['employee_id'] for entry in after['entries']],
            [unaffected.id],
        )
        self.assertNotEqual(
            before['source_fingerprint'],
            after['source_fingerprint'],
        )
        self.assertEqual(after['summary']['withheld_shift_count'], 3)
        self.assertEqual(
            after['summary']['withheld_reasons'],
            {
                'employee_partial_coverage': 2,
                'passport_contract_invalid': 1,
            },
        )

    def test_assignment_mismatch_interval_is_not_counted_twice(self):
        record = {
            'routing': {
                'match_count': 20,
                'mismatch_count': 0,
                'missing_actual_count': 0,
                'missing_assigned_count': 0,
            },
            'quality': {
                'quality_metrics': {
                    'trip_without_assignment_seconds': 3600,
                    'trip_assignment_mismatch_seconds': 3600,
                },
            },
            'available_seconds': Decimal('43200'),
        }
        overlap_score = _assignments_score(record)
        record['quality']['quality_metrics'][
            'trip_assignment_mismatch_seconds'
        ] = 0

        self.assertEqual(overlap_score, _assignments_score(record))


@override_settings(
    PORTAL_WORKING_DRIVER_RATING_ENABLED=True,
    DRIVER_WATCH_RATING_DIAGNOSTIC_API_ENABLED=True,
)
class DriverWatchRatingApiTests(TransactionTestCase):
    def setUp(self):
        self.client = Client()
        self.composition = WatchComposition.objects.create(
            code='rating-api-watch',
            name='Состав API рейтинга',
        )
        self.watch = WatchPeriod.objects.create(
            name='Вахта API рейтинга',
            watch_composition=self.composition,
            starts_on=timezone.localdate(),
            ends_on=timezone.localdate() + timedelta(days=29),
        )
        self.rating_period = RatingPeriod.objects.create(
            name='Период API рейтинга',
            starts_on=production_work_date(),
            ends_before=production_work_date() + timedelta(days=30),
            comment='Технический период проверки API рейтинга.',
        )
        self.scope_driver = Employee.objects.create(
            full_name='Водитель области API рейтинга',
            status=Employee.Status.ACTIVE,
            is_active=True,
            work_category=Employee.WorkCategory.DRIVER,
            watch_composition=self.composition,
        )
        refresh_test_rating_materialization(self.rating_period)

    def access(self, role_code):
        employee = Employee.objects.create(
            full_name=f'Проверяющий {role_code}',
            status=Employee.Status.ACTIVE,
        )
        role = Role.objects.create(
            code=role_code,
            name=f'Роль {role_code}',
        )
        return EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code='123456',
            status=EmployeeAccess.Status.ACTIVATED,
        )

    def login_as(self, access):
        login_at = timezone.now()
        access.last_login_at = login_at
        access.save(update_fields=['last_login_at'])
        session = self.client.session
        session['employee_access_id'] = access.id
        session[ACTIVE_ROLE_SESSION_KEY] = access.id
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = login_at.isoformat()
        session[ACTIVE_ROLE_CODE_SESSION_KEY] = access.role.code
        session.save()

    def test_legacy_watch_rating_http_calculation_is_retired(self):
        for role_code in ('dispatcher', 'admin', 'manager'):
            with self.subTest(role=role_code):
                self.client = Client()
                self.login_as(self.access(role_code))
                cache.clear()
                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get(
                        reverse('driver_watch_rating_api'),
                        {
                            'watch_period': self.watch.id,
                            'shift_type': ShiftType.DAY,
                        },
                    )
                self.assertEqual(response.status_code, 410)
                self.assertEqual(
                    response.json()['snapshot_status'],
                    'diagnostic_http_calculation_retired',
                )
                self.assertIn('no-store', response.headers['Cache-Control'])
                passport_table = (
                    DriverShiftPassportSnapshot._meta.db_table
                )
                self.assertFalse(
                    any(
                        passport_table in query['sql']
                        for query in queries.captured_queries
                    )
                )

    def test_driver_cannot_read_management_rating_api(self):
        self.login_as(self.access('driver'))

        response = self.client.get(
            reverse('driver_watch_rating_api'),
            {'shift_type': ShiftType.DAY},
        )

        self.assertEqual(response.status_code, 403)

    def test_shift_type_is_required(self):
        self.login_as(self.access('dispatcher'))

        response = self.client.get(reverse('driver_watch_rating_api'))

        self.assertEqual(response.status_code, 400)

    @override_settings(PORTAL_WORKING_DRIVER_RATING_ENABLED=False)
    def test_disabled_rating_api_fails_closed_before_data_access(self):
        self.login_as(self.access('dispatcher'))

        response = self.client.get(
            reverse('driver_watch_rating_api'),
            {'shift_type': ShiftType.DAY},
        )

        self.assertEqual(response.status_code, 404)
        self.assertIn('не включён', response.json()['error'])

    def test_period_rating_api_is_private_and_available_to_management_roles(self):
        for role_code in ('dispatcher', 'admin', 'manager'):
            with self.subTest(role=role_code):
                self.client = Client()
                self.login_as(self.access(role_code))
                response = self.client.get(
                    reverse('driver_period_rating_api'),
                    {'shift_type': ShiftType.DAY},
                )

                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertFalse(payload['official'])
                self.assertFalse(payload['official_rating_eligible'])
                self.assertEqual(payload['scope_type'], 'rating_period')
                self.assertEqual(
                    payload['rating_period']['id'],
                    self.rating_period.id,
                )
                self.assertEqual(
                    payload['watch_composition']['id'],
                    self.composition.id,
                )
                self.assertEqual(
                    [row['id'] for row in payload['available_rating_periods']],
                    [self.rating_period.id],
                )
                self.assertEqual(
                    [
                        row['id']
                        for row in payload['available_watch_compositions']
                    ],
                    [self.composition.id],
                )
                self.assertIn(
                    'private',
                    response.headers['Cache-Control'],
                )
                self.assertIn(
                    'no-store',
                    response.headers['Cache-Control'],
                )

    def test_period_rating_api_never_runs_formula_from_http_request(self):
        self.login_as(self.access('dispatcher'))

        with patch(
            (
                'reports.driver_rating_materialization.'
                'build_driver_rating_period'
            )
        ) as calculator:
            response = self.client.get(
                reverse('driver_period_rating_api'),
                {'shift_type': ShiftType.DAY},
            )

        self.assertEqual(response.status_code, 200)
        calculator.assert_not_called()
        self.assertEqual(response.json()['snapshot_status'], 'fresh')

    def test_period_rating_api_returns_503_when_shared_snapshot_is_missing(self):
        DriverRatingPeriodMaterializedSnapshot.objects.all().delete()
        self.login_as(self.access('dispatcher'))

        with patch(
            (
                'reports.driver_rating_materialization.'
                'build_driver_rating_period'
            )
        ) as calculator:
            response = self.client.get(
                reverse('driver_period_rating_api'),
                {'shift_type': ShiftType.DAY},
            )

        self.assertEqual(response.status_code, 503)
        calculator.assert_not_called()
        self.assertEqual(
            response.json()['snapshot_status'],
            'snapshot_missing',
        )
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_period_rating_api_requires_auth_role_and_shift_type(self):
        unauthenticated = self.client.get(
            reverse('driver_period_rating_api'),
            {'shift_type': ShiftType.DAY},
        )
        self.assertEqual(unauthenticated.status_code, 401)

        self.login_as(self.access('driver'))
        forbidden = self.client.get(
            reverse('driver_period_rating_api'),
            {'shift_type': ShiftType.DAY},
        )
        self.assertEqual(forbidden.status_code, 403)

        self.client = Client()
        self.login_as(self.access('dispatcher'))
        missing_shift = self.client.get(
            reverse('driver_period_rating_api'),
        )
        self.assertEqual(missing_shift.status_code, 400)

    def test_period_rating_api_returns_stable_empty_without_current_period(self):
        self.rating_period.is_active = False
        self.rating_period.save()
        self.login_as(self.access('dispatcher'))

        response = self.client.get(
            reverse('driver_period_rating_api'),
            {'shift_type': ShiftType.NIGHT},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['available'])
        self.assertIsNone(payload['rating_period'])
        self.assertEqual(payload['entries'], [])
        self.assertEqual(payload['available_rating_periods'], [])
        self.assertEqual(payload['summary']['employee_count'], 0)
        self.assertEqual(
            payload['linkage_audit'],
            {
                'candidate_closed_shift_count': 0,
                'linked_to_selected_composition_count': 0,
                'unlinked_shift_count': 0,
                'linked_to_other_composition_count': 0,
                'selected_watch_date_mismatch_count': 0,
                'covered_watch_period_count': 0,
                'linkage_ready': False,
            },
        )
        self.assertIn('не задан', payload['status'])

    def test_period_rating_api_fails_closed_on_damaged_overlap(self):
        other_period = RatingPeriod.objects.create(
            name='Будущий период для повреждённого пересечения',
            starts_on=self.rating_period.ends_before,
            ends_before=self.rating_period.ends_before + timedelta(days=10),
            comment='Техническая проверка повреждённого пересечения.',
        )
        RatingPeriod._base_manager.filter(pk=other_period.pk).update(
            starts_on=self.rating_period.starts_on,
            ends_before=self.rating_period.ends_before,
        )
        self.login_as(self.access('dispatcher'))

        response = self.client.get(
            reverse('driver_period_rating_api'),
            {'shift_type': ShiftType.DAY},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn('несколько', response.json()['error'])
        self.assertIn('остановлен', response.json()['error'])

    def test_period_rating_api_requires_composition_when_scope_has_several(self):
        other_composition = WatchComposition.objects.create(
            code='rating-api-other-watch',
            name='Другой состав API рейтинга',
        )
        Employee.objects.create(
            full_name='Водитель другого состава API рейтинга',
            status=Employee.Status.ACTIVE,
            is_active=True,
            work_category=Employee.WorkCategory.DRIVER,
            watch_composition=other_composition,
        )
        refresh_test_rating_materialization(self.rating_period)
        self.login_as(self.access('manager'))

        missing_selection = self.client.get(
            reverse('driver_period_rating_api'),
            {
                'rating_period': self.rating_period.id,
                'shift_type': ShiftType.DAY,
            },
        )
        self.assertEqual(missing_selection.status_code, 400)
        self.assertEqual(
            len(missing_selection.json()['available_watch_compositions']),
            2,
        )

        selected = self.client.get(
            reverse('driver_period_rating_api'),
            {
                'rating_period': self.rating_period.id,
                'watch_composition': self.composition.id,
                'shift_type': ShiftType.DAY,
            },
        )
        self.assertEqual(selected.status_code, 200)
        self.assertEqual(
            selected.json()['watch_composition']['id'],
            self.composition.id,
        )

    def test_period_rating_api_rejects_period_and_composition_outside_scope(self):
        inactive_period = RatingPeriod.objects.create(
            name='Отключённый период API рейтинга',
            starts_on=self.rating_period.ends_before,
            ends_before=self.rating_period.ends_before + timedelta(days=10),
            comment='Техническая проверка отключённого периода.',
            is_active=False,
        )
        outside_composition = WatchComposition.objects.create(
            code='rating-api-outside-watch',
            name='Состав вне области API рейтинга',
        )
        self.login_as(self.access('admin'))

        period_response = self.client.get(
            reverse('driver_period_rating_api'),
            {
                'rating_period': inactive_period.id,
                'watch_composition': self.composition.id,
                'shift_type': ShiftType.DAY,
            },
        )
        self.assertEqual(period_response.status_code, 404)

        composition_response = self.client.get(
            reverse('driver_period_rating_api'),
            {
                'rating_period': self.rating_period.id,
                'watch_composition': outside_composition.id,
                'shift_type': ShiftType.DAY,
            },
        )
        self.assertEqual(composition_response.status_code, 404)

    @override_settings(PORTAL_WORKING_DRIVER_RATING_ENABLED=False)
    def test_disabled_period_rating_api_fails_closed(self):
        self.login_as(self.access('dispatcher'))

        response = self.client.get(
            reverse('driver_period_rating_api'),
            {'shift_type': ShiftType.DAY},
        )

        self.assertEqual(response.status_code, 404)
        self.assertIn('не включён', response.json()['error'])


@override_settings(
    PORTAL_WORKING_DRIVER_RATING_ENABLED=True,
    DRIVER_WATCH_RATING_DIAGNOSTIC_API_ENABLED=True,
    PORTAL_EMPLOYEE_SCOPE_PROVIDER=(
        'reports.test_driver_watch_rating.'
        'rating_test_employee_scope_provider'
    ),
)
class DriverRatingApiScopeTests(
    DriverRatingFixtureMixin,
    TransactionTestCase,
):
    def setUp(self):
        super().setUp()
        self.rating_period = RatingPeriod.objects.create(
            name='Тестовый период API состава',
            starts_on=self.watch.starts_on,
            ends_before=self.watch.ends_on + timedelta(days=1),
            comment='Технический период проверки API состава.',
        )
        manager = Employee.objects.create(
            full_name='Руководитель области рейтинга',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        manager_role = Role.objects.create(
            code='manager',
            name='Руководитель области рейтинга',
        )
        self.manager_access = EmployeeAccess.objects.create(
            employee=manager,
            role=manager_role,
            access_code='654321',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    def snapshot(self, *args, **kwargs):
        snapshot = super().snapshot(*args, **kwargs)
        refresh_test_rating_materialization(self.rating_period)
        return snapshot

    def login_manager(self):
        login_at = timezone.now()
        self.manager_access.last_login_at = login_at
        self.manager_access.save(update_fields=['last_login_at'])
        session = self.client.session
        session['employee_access_id'] = self.manager_access.id
        session[ACTIVE_ROLE_SESSION_KEY] = self.manager_access.id
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = login_at.isoformat()
        session[ACTIVE_ROLE_CODE_SESSION_KEY] = 'manager'
        session.save()

    def test_all_management_rating_apis_enforce_site_scope(self):
        visible = self.employee('Водитель видимого участка API')
        self.snapshot(visible, ordinal=1, trip_count=20)
        visible_watch = self.watch

        hidden_composition = WatchComposition.objects.create(
            code='hidden-api-watch',
            name='Состав вне области API',
        )
        self.composition = hidden_composition
        self.watch = WatchPeriod.objects.create(
            name='Вахта вне области API',
            watch_composition=hidden_composition,
            starts_on=visible_watch.starts_on,
            ends_on=visible_watch.ends_on,
        )
        hidden = self.employee('ВНЕ ОБЛАСТИ Водитель API')
        self.snapshot(hidden, ordinal=2, trip_count=23)
        hidden_watch = self.watch
        self.login_manager()

        retired_rating_response = self.client.get(
            reverse('driver_watch_rating_api'),
            {
                'watch_period': visible_watch.id,
                'shift_type': ShiftType.DAY,
            },
        )
        self.assertEqual(
            retired_rating_response.status_code,
            410,
        )

        period_rating_response = self.client.get(
            reverse('driver_period_rating_api'),
            {
                'rating_period': self.rating_period.id,
                'watch_composition': visible_watch.watch_composition_id,
                'shift_type': ShiftType.DAY,
            },
        )
        self.assertEqual(period_rating_response.status_code, 200)
        period_rating_payload = period_rating_response.json()
        self.assertEqual(
            [
                entry['employee_id']
                for entry in period_rating_payload['entries']
            ],
            [visible.id],
        )
        self.assertEqual(
            [
                item['id']
                for item in period_rating_payload[
                    'available_watch_compositions'
                ]
            ],
            [visible_watch.watch_composition_id],
        )

        hidden_period_rating_response = self.client.get(
            reverse('driver_period_rating_api'),
            {
                'rating_period': self.rating_period.id,
                'watch_composition': hidden_watch.watch_composition_id,
                'shift_type': ShiftType.DAY,
            },
        )
        self.assertEqual(hidden_period_rating_response.status_code, 404)

        observation_response = self.client.get(
            reverse('driver_watch_observation_api'),
            {
                'watch_period': visible_watch.id,
                'shift_type': ShiftType.DAY,
            },
        )
        self.assertEqual(observation_response.status_code, 200)
        observation_payload = observation_response.json()
        self.assertEqual(
            {row['employee_id'] for row in observation_payload['rows']},
            {visible.id},
        )
        self.assertNotIn(
            hidden_watch.name,
            {
                period['name']
                for period in observation_payload[
                    'available_watch_periods'
                ]
            },
        )

        date_from = timezone.localdate(self.now) - timedelta(days=5)
        date_to = timezone.localdate(self.now)
        shadow_response = self.client.get(
            reverse('driver_period_shadow_observation_api'),
            {
                'date_from': date_from.isoformat(),
                'date_to': date_to.isoformat(),
                'shift_type': ShiftType.DAY,
            },
        )
        self.assertEqual(shadow_response.status_code, 200)
        self.assertEqual(
            {
                row['employee_id']
                for row in shadow_response.json()['rows']
            },
            {visible.id},
        )

    def test_period_api_uses_current_day_night_scope_without_passports(self):
        day_driver = self.employee('Дневной водитель materialized API')
        night_driver = self.employee('Ночной водитель materialized API')
        self.snapshot(
            day_driver,
            ordinal=1,
            trip_count=20,
            shift_type=ShiftType.DAY,
        )
        self.snapshot(
            night_driver,
            ordinal=2,
            trip_count=20,
            shift_type=ShiftType.NIGHT,
        )
        self.login_manager()

        with (
            patch(
                (
                    'reports.driver_rating_scope_membership.'
                    'linked_driver_snapshot_scopes'
                )
            ) as linked_snapshot_scan,
            patch(
                (
                    'reports.driver_rating_scope_membership.'
                    'driver_rating_group_membership'
                )
            ) as historical_membership_scan,
        ):
            day_response = self.client.get(
                reverse('driver_period_rating_api'),
                {
                    'rating_period': self.rating_period.id,
                    'watch_composition': self.composition.id,
                    'shift_type': ShiftType.DAY,
                },
            )
            night_response = self.client.get(
                reverse('driver_period_rating_api'),
                {
                    'rating_period': self.rating_period.id,
                    'watch_composition': self.composition.id,
                    'shift_type': ShiftType.NIGHT,
                },
            )

        self.assertEqual(day_response.status_code, 200)
        self.assertEqual(night_response.status_code, 200)
        self.assertEqual(
            [
                entry['employee_id']
                for entry in day_response.json()['entries']
            ],
            [day_driver.id],
        )
        self.assertEqual(
            [
                entry['employee_id']
                for entry in night_response.json()['entries']
            ],
            [night_driver.id],
        )
        self.assertNotEqual(
            day_response.json().get('snapshot_status'),
            'snapshot_scope_mismatch',
        )
        self.assertNotEqual(
            night_response.json().get('snapshot_status'),
            'snapshot_scope_mismatch',
        )
        linked_snapshot_scan.assert_not_called()
        historical_membership_scan.assert_not_called()

    def test_period_api_keeps_historical_composition_after_transfer(self):
        visible = self.employee('Водитель после перевода состава API')
        self.snapshot(visible, ordinal=1, trip_count=20)
        historical_composition = self.composition
        current_composition = WatchComposition.objects.create(
            code='rating-api-current-after-transfer',
            name='Текущий пустой состав после перевода API',
        )
        visible.watch_composition = current_composition
        visible.save(update_fields=['watch_composition'])
        historical_composition.is_active = False
        historical_composition.save(update_fields=['is_active'])
        refresh_test_rating_materialization(self.rating_period)
        self.login_manager()

        response = self.client.get(
            reverse('driver_period_rating_api'),
            {
                'rating_period': self.rating_period.id,
                'watch_composition': historical_composition.id,
                'shift_type': ShiftType.DAY,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            [entry['employee_id'] for entry in payload['entries']],
            [visible.id],
        )
        self.assertEqual(
            {
                item['id']
                for item in payload['available_watch_compositions']
            },
            {
                historical_composition.id,
                current_composition.id,
            },
        )
        historical_payload = next(
            item
            for item in payload['available_watch_compositions']
            if item['id'] == historical_composition.id
        )
        self.assertFalse(historical_payload['is_active'])


@override_settings(PORTAL_WORKING_DRIVER_RATING_ENABLED=True)
class DriverRatingPortalProviderTests(
    DriverRatingFixtureMixin,
    TransactionTestCase,
):
    def setUp(self):
        super().setUp()
        self.rating_period = RatingPeriod.objects.create(
            name='Тестовый период рейтинга портала',
            starts_on=self.watch.starts_on,
            ends_before=self.watch.ends_on + timedelta(days=1),
            comment='Технический период проверки портала.',
        )

    def snapshot(self, *args, **kwargs):
        snapshot = super().snapshot(*args, **kwargs)
        refresh_test_rating_materialization(self.rating_period)
        return snapshot

    def test_internal_portal_shows_places_and_only_personal_score(self):
        first = self.employee('Водитель портала первый')
        second = self.employee('Водитель портала второй')
        self.snapshot(first, ordinal=1, trip_count=23)
        self.snapshot(second, ordinal=2, trip_count=17)
        provider = DriverRatingProductionDataProvider()
        ranking = provider.ranking(first)
        personal = provider.personal_kpis(first)
        public = provider.public_ranking()

        self.assertTrue(ranking.available)
        self.assertTrue(personal.available)
        self.assertFalse(public.available)
        self.assertEqual(ranking.employee_entry.employee_id, first.id)
        self.assertEqual(len(ranking.entries), 2)

        session = self.client.session
        session[PORTAL_EMPLOYEE_SESSION_KEY] = first.id
        session.save()
        response = self.client.get(reverse('portal:rating'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Рабочий рейтинг v2')
        self.assertContains(response, first.full_name)
        self.assertContains(response, second.full_name)
        self.assertContains(response, 'м³·км и т·км пока не учитываются')
        self.assertNotContains(response, '0 м³·км')
        self.assertNotContains(response, '0 т·км')
        first_score = next(
            metric['value']
            for metric in personal.metrics
            if metric['label'] == 'Рабочий балл'
        )
        second_score = next(
            row['score']
            for row in build_driver_watch_rating(
                self.watch,
                shift_type=ShiftType.DAY,
            )['entries']
            if row['employee_id'] == second.id
        ).replace('.', ',')
        self.assertContains(response, first_score)
        self.assertNotContains(response, second_score)
        content = response.content.decode('utf-8')
        self.assertLess(
            content.index('class="personal-kpi"'),
            content.index('class="full-ranking"'),
        )

    def test_portal_reads_shared_snapshot_without_formula_or_passport_scan(self):
        employee = self.employee('Водитель готового снимка портала')
        self.snapshot(employee, ordinal=1, trip_count=20)
        provider = DriverRatingProductionDataProvider()

        with (
            patch(
                (
                    'reports.driver_rating_materialization.'
                    'build_driver_rating_period'
                )
            ) as calculator,
            patch(
                (
                    'reports.portal_rating_provider.'
                    'linked_driver_snapshot_scopes'
                )
            ) as passport_scan,
            patch(
                (
                    'reports.driver_rating_scope_membership.'
                    'driver_rating_group_membership'
                )
            ) as historical_membership_scan,
            CaptureQueriesContext(connection) as queries,
        ):
            ranking = provider.ranking(employee)

        self.assertTrue(ranking.available)
        calculator.assert_not_called()
        passport_scan.assert_not_called()
        historical_membership_scan.assert_not_called()
        materialized_table = (
            DriverRatingPeriodMaterializedSnapshot._meta.db_table
        )
        metadata_queries = [
            query['sql']
            for query in queries.captured_queries
            if materialized_table in query['sql']
        ]
        self.assertEqual(len(metadata_queries), 2)
        self.assertNotIn(
            f'"{materialized_table}"."payload"',
            metadata_queries[0],
        )
        self.assertIn(
            f'"{materialized_table}"."payload"',
            metadata_queries[1],
        )
        passport_table = DriverShiftPassportSnapshot._meta.db_table
        self.assertFalse(
            any(
                passport_table in query['sql']
                for query in queries.captured_queries
            )
        )

    def test_portal_rejects_tampered_group_membership_metadata(self):
        employee = self.employee('Водитель повреждённого состава снимка')
        self.snapshot(employee, ordinal=1, trip_count=20)
        snapshot = DriverRatingPeriodMaterializedSnapshot.objects.get(
            rating_period=self.rating_period,
            watch_composition=self.composition,
            shift_type=ShiftType.DAY,
        )
        DriverRatingPeriodMaterializedSnapshot.objects.filter(
            pk=snapshot.pk,
        ).update(
            member_latest_closed_at={
                str(employee.id): (
                    self.now + timedelta(days=1)
                ).isoformat(),
            },
        )
        provider = DriverRatingProductionDataProvider()

        self.assertIsNone(
            provider._latest_snapshot_scope(
                self.rating_period,
                employee,
            )
        )
        self.assertFalse(provider.ranking(employee).available)

    def test_home_preview_is_limited_to_five_rows_even_with_ties(self):
        employees = [
            self.employee(f'Водитель одинаковый портал {index}')
            for index in range(6)
        ]
        for ordinal, employee in enumerate(employees, start=1):
            self.snapshot(employee, ordinal=ordinal, trip_count=20)

        ranking = DriverRatingProductionDataProvider().ranking(employees[0])

        self.assertEqual(len(ranking.entries), 6)
        self.assertEqual(len(ranking.top_five), 5)
        self.assertEqual(
            {entry.place for entry in ranking.entries},
            {1},
        )

    def test_provider_uses_employee_watch_not_global_latest_watch(self):
        employee = self.employee('Водитель своей вахты')
        self.snapshot(employee, ordinal=1, trip_count=20)
        employee_watch = self.watch

        other_composition = WatchComposition.objects.create(
            code='rating-other-watch',
            name='Другой состав рейтинга',
        )
        self.composition = other_composition
        self.watch = WatchPeriod.objects.create(
            name='Более поздняя чужая вахта',
            watch_composition=other_composition,
            starts_on=employee_watch.starts_on,
            ends_on=employee_watch.ends_on + timedelta(days=1),
            is_active=True,
        )
        other_employee = self.employee('Водитель чужой вахты')
        self.snapshot(other_employee, ordinal=2, trip_count=23)

        ranking = DriverRatingProductionDataProvider().ranking(employee)

        self.assertTrue(ranking.available)
        self.assertIn(self.rating_period.name, ranking.period_label)
        self.assertNotIn(employee_watch.name, ranking.period_label)
        self.assertEqual(
            [entry.employee_id for entry in ranking.entries],
            [employee.id],
        )

    def test_provider_keeps_historical_composition_after_employee_transfer(self):
        employee = self.employee('Водитель с историческим составом')
        self.snapshot(employee, ordinal=1, trip_count=20)
        historical_composition = self.composition
        current_composition = WatchComposition.objects.create(
            code='rating-provider-current-watch',
            name='Новый текущий состав водителя',
        )
        employee.watch_composition = current_composition
        employee.save(update_fields=['watch_composition'])
        historical_composition.is_active = False
        historical_composition.save(update_fields=['is_active'])
        refresh_test_rating_materialization(self.rating_period)
        provider = DriverRatingProductionDataProvider()

        snapshot_scope = provider._latest_snapshot_scope(
            self.rating_period,
            employee,
        )
        ranking = provider.ranking(employee)

        self.assertEqual(
            snapshot_scope['watch_composition_id'],
            historical_composition.id,
        )
        self.assertTrue(ranking.available)
        self.assertEqual(
            [entry.employee_id for entry in ranking.entries],
            [employee.id],
        )

    def test_provider_discovers_manifest_after_live_watch_link_is_lost(self):
        employee = self.employee('Водитель с потерянной живой вахтой')
        snapshot = self.snapshot(employee, ordinal=1, trip_count=20)
        historical_composition = self.composition
        EmployeeShift.objects.filter(pk=snapshot.shift_id).update(
            watch_period=None,
        )
        refresh_test_rating_materialization(self.rating_period)
        provider = DriverRatingProductionDataProvider()

        snapshot_scope = provider._latest_snapshot_scope(
            self.rating_period,
            employee,
        )
        ranking = provider.ranking(employee)

        self.assertEqual(
            snapshot_scope['watch_composition_id'],
            historical_composition.id,
        )
        self.assertFalse(ranking.available)

    def test_provider_discovers_manifest_after_live_watch_dates_move(self):
        employee = self.employee('Водитель со сдвинутой живой вахтой')
        self.snapshot(employee, ordinal=1, trip_count=20)
        historical_composition = self.composition
        WatchPeriod.objects.filter(pk=self.watch.pk).update(
            starts_on=self.rating_period.ends_before + timedelta(days=10),
            ends_on=self.rating_period.ends_before + timedelta(days=40),
        )
        refresh_test_rating_materialization(self.rating_period)
        provider = DriverRatingProductionDataProvider()

        snapshot_scope = provider._latest_snapshot_scope(
            self.rating_period,
            employee,
        )
        ranking = provider.ranking(employee)

        self.assertEqual(
            snapshot_scope['watch_composition_id'],
            historical_composition.id,
        )
        self.assertFalse(ranking.available)

    def test_provider_discovers_manifest_after_shift_and_watch_dates_move(self):
        employee = self.employee(
            'Водитель с одновременным сдвигом смены и вахты',
        )
        snapshot = self.snapshot(employee, ordinal=1, trip_count=20)
        historical_composition = self.composition
        shift = EmployeeShift.objects.get(pk=snapshot.shift_id)
        live_shift_offset = timedelta(days=90)
        EmployeeShift.objects.filter(pk=shift.pk).update(
            opened_at=shift.opened_at + live_shift_offset,
            closed_at=shift.closed_at + live_shift_offset,
        )
        WatchPeriod.objects.filter(pk=self.watch.pk).update(
            starts_on=self.rating_period.ends_before + timedelta(days=10),
            ends_on=self.rating_period.ends_before + timedelta(days=40),
        )
        refresh_test_rating_materialization(self.rating_period)
        provider = DriverRatingProductionDataProvider()

        snapshot_scope = provider._latest_snapshot_scope(
            self.rating_period,
            employee,
        )
        ranking = provider.ranking(employee)

        self.assertEqual(
            snapshot_scope['watch_composition_id'],
            historical_composition.id,
        )
        self.assertFalse(ranking.available)

    def test_provider_withholds_malformed_manifest_instead_of_http_500(self):
        malformed_payloads = (
            [],
            {'source_manifest': []},
            {'source_manifest': {'shift': []}},
        )
        provider = DriverRatingProductionDataProvider()

        for ordinal, malformed_payload in enumerate(
            malformed_payloads,
            start=1,
        ):
            with self.subTest(malformed_payload=malformed_payload):
                employee = self.employee(
                    f'Водитель с повреждённым паспортом {ordinal}',
                )
                snapshot = self.snapshot(
                    employee,
                    ordinal=ordinal,
                    trip_count=20,
                )
                force_snapshot_payload_update(snapshot, malformed_payload)
                refresh_test_rating_materialization(self.rating_period)

                ranking = provider.ranking(employee)
                session = self.client.session
                session[PORTAL_EMPLOYEE_SESSION_KEY] = employee.id
                session.save()
                response = self.client.get(reverse('portal:rating'))

                self.assertFalse(ranking.available)
                self.assertEqual(response.status_code, 200)

    def test_provider_current_period_uses_inclusive_exclusive_boundaries(self):
        provider = DriverRatingProductionDataProvider()

        with patch(
            'reports.portal_rating_provider.production_work_date',
            return_value=self.rating_period.starts_on,
        ):
            self.assertEqual(
                provider._current_rating_period(),
                self.rating_period,
            )
        with patch(
            'reports.portal_rating_provider.production_work_date',
            return_value=self.rating_period.ends_before,
        ):
            self.assertIsNone(provider._current_rating_period())

    def test_provider_uses_latest_actual_shift_type_inside_rating_period(self):
        employee = self.employee('Водитель с фактической сменной группой')
        self.snapshot(
            employee,
            ordinal=2,
            trip_count=20,
            shift_type=ShiftType.DAY,
        )
        self.snapshot(
            employee,
            ordinal=1,
            trip_count=20,
            shift_type=ShiftType.NIGHT,
        )

        ranking = DriverRatingProductionDataProvider().ranking(employee)

        self.assertTrue(ranking.available)
        self.assertIn(
            dict(ShiftType.choices)[ShiftType.NIGHT],
            ranking.period_label,
        )
        self.assertEqual(
            [entry.employee_id for entry in ranking.entries],
            [employee.id],
        )

    def test_provider_fails_closed_without_current_rating_period(self):
        employee = self.employee('Водитель без периода рейтинга')
        self.snapshot(employee, ordinal=1, trip_count=20)
        self.rating_period.is_active = False
        self.rating_period.save()

        ranking = DriverRatingProductionDataProvider().ranking(employee)
        personal = DriverRatingProductionDataProvider().personal_kpis(
            employee,
        )

        self.assertFalse(ranking.available)
        self.assertFalse(personal.available)
        self.assertIn('период рейтинга не задан', ranking.status)
        self.assertIn('период рейтинга не задан', personal.status)

    def test_provider_fails_closed_on_damaged_period_overlap(self):
        employee = self.employee('Водитель при конфликте периодов')
        self.snapshot(employee, ordinal=1, trip_count=20)
        other_period = RatingPeriod.objects.create(
            name='Будущий конфликтующий период портала',
            starts_on=self.rating_period.ends_before,
            ends_before=self.rating_period.ends_before + timedelta(days=10),
            comment='Техническая проверка конфликта периодов портала.',
        )
        RatingPeriod._base_manager.filter(pk=other_period.pk).update(
            starts_on=self.rating_period.starts_on,
            ends_before=self.rating_period.ends_before,
        )
        provider = DriverRatingProductionDataProvider()

        self.assertIsNone(provider._current_rating_period())
        ranking = provider.ranking(employee)

        self.assertFalse(ranking.available)
        self.assertIn('период рейтинга не задан', ranking.status)

    @override_settings(
        PORTAL_EMPLOYEE_SCOPE_PROVIDER=(
            'reports.test_driver_watch_rating.'
            'rating_test_employee_scope_provider'
        ),
    )
    def test_provider_ranking_is_limited_to_portal_employee_scope(self):
        visible = self.employee('Водитель видимого участка')
        hidden = self.employee('ВНЕ ОБЛАСТИ Водитель')
        self.snapshot(visible, ordinal=1, trip_count=20)
        self.snapshot(hidden, ordinal=2, trip_count=23)

        ranking = DriverRatingProductionDataProvider().ranking(visible)

        self.assertTrue(ranking.available)
        self.assertEqual(
            [entry.employee_id for entry in ranking.entries],
            [visible.id],
        )
