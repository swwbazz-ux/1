from copy import deepcopy
from datetime import timedelta

from django.test import TestCase

from core.production_time import production_day_bounds, production_work_date
from shifts.models import EmployeeShift, ShiftType, WatchPeriod
from users.models import WatchComposition

from .driver_watch_observation import (
    build_driver_rating_period_linkage_audit,
    build_driver_rating_period_observation,
)
from .driver_watch_rating import (
    build_driver_rating_period,
    get_cached_driver_rating_period,
)
from .driver_shift_passport_snapshots import _fingerprint
from .models import DriverShiftPassportSnapshot, RatingPeriod
from .test_driver_watch_rating import DriverRatingFixtureMixin


class DriverRatingPeriodTests(
    DriverRatingFixtureMixin,
    TestCase,
):
    def setUp(self):
        super().setUp()
        self.production_date = production_work_date(self.now)
        self.rating_period = RatingPeriod.objects.create(
            name='Тестовый период расчёта рейтинга',
            starts_on=self.production_date - timedelta(days=30),
            ends_before=self.production_date + timedelta(days=11),
            comment='Только техническая проверка.',
        )

    @staticmethod
    def _structural_snapshot_revision(snapshot):
        snapshot.shift.refresh_from_db()
        payload = deepcopy(snapshot.payload)
        shift_manifest = payload['source_manifest']['shift']
        shift_manifest.update({
            'opened_at': snapshot.shift.opened_at,
            'closed_at': snapshot.shift.closed_at,
            'workplace_code': snapshot.shift.workplace_code,
            'equipment_id': snapshot.shift.equipment_id,
        })
        source_fingerprint = _fingerprint(payload['source_manifest'])
        payload_fingerprint = _fingerprint(payload)
        return DriverShiftPassportSnapshot.objects.create(
            shift=snapshot.shift,
            revision=snapshot.revision + 1,
            schema_version=snapshot.schema_version,
            calculator_version=snapshot.calculator_version,
            source_fingerprint=source_fingerprint,
            payload_fingerprint=payload_fingerprint,
            payload=payload,
            trigger=snapshot.trigger,
        )

    def snapshot(self, *args, **kwargs):
        snapshot = super().snapshot(*args, **kwargs)
        return self._structural_snapshot_revision(snapshot)

    @staticmethod
    def _move_shift_to_production_date(snapshot, production_date):
        opened_at = (
            production_day_bounds(production_date)[0]
            + timedelta(minutes=15)
        )
        EmployeeShift.objects.filter(pk=snapshot.shift_id).update(
            opened_at=opened_at,
            closed_at=opened_at + timedelta(hours=12),
        )
        snapshot.shift.refresh_from_db()

    def _snapshot_on_production_date(
        self,
        employee,
        *,
        production_date,
        **kwargs,
    ):
        snapshot = super().snapshot(employee, **kwargs)
        self._move_shift_to_production_date(snapshot, production_date)
        return self._structural_snapshot_revision(snapshot)

    def _plain_closed_shift(
        self,
        employee,
        *,
        production_date,
        watch_period=None,
        shift_type=ShiftType.DAY,
    ):
        opened_at = (
            production_day_bounds(production_date)[0]
            + timedelta(minutes=15)
        )
        return EmployeeShift.objects.create(
            employee=employee,
            shift_type=shift_type,
            workplace_code='driver',
            watch_period=watch_period,
            equipment=self.truck,
            opened_at=opened_at,
            closed_at=opened_at + timedelta(hours=12),
            opened_by=employee,
            closed_by=employee,
        )

    def test_starts_on_is_inclusive_and_ends_before_is_exclusive(self):
        starts_on = self.production_date - timedelta(days=2)
        ends_before = self.production_date + timedelta(days=1)
        self.rating_period.starts_on = starts_on
        self.rating_period.ends_before = ends_before
        self.rating_period.save(
            update_fields=['starts_on', 'ends_before', 'updated_at'],
        )
        at_start = self.employee('Водитель на начале периода')
        before_end = self.employee('Водитель перед концом периода')
        at_end = self.employee('Водитель на исключённой границе')
        start_snapshot = self._snapshot_on_production_date(
            at_start,
            production_date=starts_on,
            ordinal=1,
            trip_count=20,
        )
        before_end_snapshot = self._snapshot_on_production_date(
            before_end,
            production_date=ends_before - timedelta(days=1),
            ordinal=2,
            trip_count=20,
        )
        end_snapshot = self._snapshot_on_production_date(
            at_end,
            production_date=ends_before,
            ordinal=3,
            trip_count=20,
        )

        result = build_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )

        self.assertTrue(result['available'])
        self.assertEqual(
            {
                entry['employee_id']
                for entry in result['entries']
            },
            {at_start.id, before_end.id},
        )
        self.assertNotIn(
            at_end.id,
            {
                entry['employee_id']
                for entry in result['entries']
            },
        )

    def _assert_structural_mutation_is_withheld(
        self,
        field_name,
        changed_value,
        *,
        use_cache=False,
    ):
        driver = self.employee(
            f'Водитель проверки immutable {field_name}',
        )
        snapshot = self.snapshot(
            driver,
            ordinal=1,
            trip_count=20,
        )
        calculator = (
            get_cached_driver_rating_period
            if use_cache
            else build_driver_rating_period
        )
        before = calculator(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )
        self.assertTrue(before['available'])

        EmployeeShift.objects.filter(pk=snapshot.shift_id).update(
            **{field_name: changed_value},
        )
        snapshot.shift.refresh_from_db()
        after = calculator(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )

        self.assertFalse(after['available'])
        self.assertEqual(
            after['summary']['withheld_reasons'],
            {'snapshot_shift_structural_mismatch': 1},
        )
        self.assertEqual(after['summary']['withheld_shift_count'], 1)

    def test_post_capture_opened_at_mutation_outside_period_is_withheld(self):
        outside_opened_at = (
            production_day_bounds(self.rating_period.ends_before)[0]
            + timedelta(days=3, minutes=15)
        )
        self._assert_structural_mutation_is_withheld(
            'opened_at',
            outside_opened_at,
            use_cache=True,
        )

    def test_post_capture_workplace_mutation_is_withheld(self):
        self._assert_structural_mutation_is_withheld(
            'workplace_code',
            'excavator_operator',
        )

    def test_post_capture_equipment_mutation_is_withheld(self):
        self._assert_structural_mutation_is_withheld(
            'equipment_id',
            None,
        )

    def test_post_capture_closed_at_mutation_is_withheld(self):
        changed_closed_at = self.now + timedelta(days=2)
        self._assert_structural_mutation_is_withheld(
            'closed_at',
            changed_closed_at,
        )

    def test_period_aggregates_two_watch_periods_of_same_composition(self):
        first_watch = WatchPeriod.objects.create(
            name='Первая часть периода рейтинга',
            watch_composition=self.composition,
            starts_on=self.production_date - timedelta(days=30),
            ends_on=self.production_date - timedelta(days=10),
        )
        second_watch = WatchPeriod.objects.create(
            name='Вторая часть периода рейтинга',
            watch_composition=self.composition,
            starts_on=self.production_date - timedelta(days=9),
            ends_on=self.production_date + timedelta(days=10),
        )
        driver = self.employee('Водитель двух вахт')
        self.watch = first_watch
        self.snapshot(driver, ordinal=20, trip_count=20)
        self.watch = second_watch
        self.snapshot(driver, ordinal=2, trip_count=21)

        result = build_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )

        self.assertTrue(result['available'])
        self.assertEqual(result['summary']['rated_shift_count'], 2)
        self.assertEqual(result['entries'][0]['shift_count'], 2)
        self.assertEqual(
            result['linkage_audit']['covered_watch_period_count'],
            2,
        )

    def test_same_dates_other_composition_is_not_mixed(self):
        selected_composition = self.composition
        selected_driver = self.employee('Водитель выбранного состава')
        self.snapshot(selected_driver, ordinal=1, trip_count=20)
        other_composition = WatchComposition.objects.create(
            code='other-rating-period-composition',
            name='Другой состав периода рейтинга',
        )
        self.composition = other_composition
        self.watch = WatchPeriod.objects.create(
            name='Чужая вахта в тех же датах',
            watch_composition=other_composition,
            starts_on=self.rating_period.starts_on,
            ends_on=self.rating_period.ends_before - timedelta(days=1),
        )
        other_driver = self.employee('Водитель другого состава')
        self.snapshot(other_driver, ordinal=2, trip_count=23)

        result = build_driver_rating_period(
            self.rating_period,
            selected_composition,
            shift_type=ShiftType.DAY,
        )

        self.assertTrue(result['available'])
        self.assertEqual(
            [entry['employee_id'] for entry in result['entries']],
            [selected_driver.id],
        )

    def test_day_and_night_remain_separate_groups(self):
        day_driver = self.employee('Дневной водитель периода')
        night_driver = self.employee('Ночной водитель периода')
        self.snapshot(
            day_driver,
            ordinal=1,
            trip_count=20,
            shift_type=ShiftType.DAY,
        )
        self.snapshot(
            night_driver,
            ordinal=2,
            trip_count=23,
            shift_type=ShiftType.NIGHT,
        )

        day = build_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )
        night = build_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.NIGHT,
        )

        self.assertEqual(
            [entry['employee_id'] for entry in day['entries']],
            [day_driver.id],
        )
        self.assertEqual(
            [entry['employee_id'] for entry in night['entries']],
            [night_driver.id],
        )

    def test_watch_date_mismatch_withholds_rating_period(self):
        self.watch = WatchPeriod.objects.create(
            name='Ошибочная календарная вахта',
            watch_composition=self.composition,
            starts_on=self.production_date,
            ends_on=self.production_date + timedelta(days=2),
        )
        driver = self.employee('Водитель с ошибочной вахтой')
        self.snapshot(driver, ordinal=5, trip_count=20)

        result = build_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )

        self.assertFalse(result['available'])
        self.assertEqual(
            result['linkage_audit'][
                'selected_watch_date_mismatch_count'
            ],
            1,
        )
        self.assertEqual(
            result['summary']['withheld_reasons'],
            {'watch_period_date_mismatch': 1},
        )

    def test_unlinked_shift_of_selected_employee_is_not_guessed(self):
        driver = self.employee('Водитель с несвязанной сменой')
        self.snapshot(driver, ordinal=1, trip_count=20)
        self._plain_closed_shift(
            driver,
            production_date=self.production_date - timedelta(days=3),
        )

        result = build_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )

        self.assertFalse(result['available'])
        self.assertEqual(
            result['linkage_audit']['unlinked_shift_count'],
            1,
        )
        self.assertEqual(
            result['summary']['withheld_reasons'],
            {'rating_period_unlinked_shift': 1},
        )

    def test_explicit_scope_keeps_solely_unlinked_employee_in_audit(self):
        linked_driver = self.employee('Связанный водитель явной области')
        unlinked_driver = self.employee(
            'Только несвязанный водитель явной области'
        )
        self.snapshot(linked_driver, ordinal=1, trip_count=20)
        self._plain_closed_shift(
            unlinked_driver,
            production_date=self.production_date - timedelta(days=3),
        )

        result = build_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
            allowed_employee_ids=(
                linked_driver.id,
                unlinked_driver.id,
            ),
            expected_employee_ids=(
                linked_driver.id,
                unlinked_driver.id,
            ),
        )

        self.assertFalse(result['available'])
        self.assertEqual(
            result['linkage_audit']['candidate_closed_shift_count'],
            2,
        )
        self.assertEqual(
            result['linkage_audit']['unlinked_shift_count'],
            1,
        )
        self.assertEqual(
            result['summary']['withheld_reasons'],
            {'rating_period_unlinked_shift': 1},
        )

    def test_other_composition_shift_of_selected_employee_withholds(self):
        driver = self.employee('Водитель со сменой другого состава')
        self.snapshot(driver, ordinal=1, trip_count=20)
        other_composition = WatchComposition.objects.create(
            code='mismatch-rating-period-composition',
            name='Ошибочно связанный другой состав',
        )
        other_watch = WatchPeriod.objects.create(
            name='Ошибочная другая вахта',
            watch_composition=other_composition,
            starts_on=self.rating_period.starts_on,
            ends_on=self.rating_period.ends_before - timedelta(days=1),
        )
        self._plain_closed_shift(
            driver,
            production_date=self.production_date - timedelta(days=3),
            watch_period=other_watch,
        )

        result = build_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )

        self.assertFalse(result['available'])
        self.assertEqual(
            result['linkage_audit'][
                'linked_to_other_composition_count'
            ],
            1,
        )
        self.assertEqual(
            result['summary']['withheld_reasons'],
            {'rating_period_other_composition_shift': 1},
        )

    def test_period_observation_uses_same_scope_and_stays_unofficial(self):
        driver = self.employee('Водитель наблюдения периода')
        self.snapshot(driver, ordinal=1, trip_count=20)

        observation = build_driver_rating_period_observation(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
            as_of=self.now + timedelta(days=1),
        )

        self.assertEqual(observation['scope_type'], 'rating_period')
        self.assertFalse(observation['official_rating_eligible'])
        self.assertEqual(
            observation['watch_composition']['id'],
            self.composition.id,
        )
        self.assertEqual(
            [row['employee_id'] for row in observation['rows']],
            [driver.id],
        )

    def test_cache_is_invalidated_when_period_dates_change(self):
        driver = self.employee('Водитель изменяемого периода')
        self.snapshot(driver, ordinal=5, trip_count=20)
        before = get_cached_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )
        self.rating_period.starts_on = (
            self.production_date - timedelta(days=2)
        )
        self.rating_period.save(
            update_fields=['starts_on', 'updated_at'],
        )

        after = get_cached_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )

        self.assertTrue(before['available'])
        self.assertFalse(after['available'])
        self.assertEqual(
            after['summary']['withheld_reasons'],
            {'rating_period_has_no_linked_driver_shifts': 0},
        )

    def test_cache_is_invalidated_by_shift_from_second_watch(self):
        driver = self.employee('Водитель обновляемой второй вахты')
        self.snapshot(driver, ordinal=20, trip_count=20)
        before = get_cached_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )
        self.watch = WatchPeriod.objects.create(
            name='Добавленная вторая вахта',
            watch_composition=self.composition,
            starts_on=self.production_date - timedelta(days=10),
            ends_on=self.production_date + timedelta(days=10),
        )
        self.snapshot(driver, ordinal=2, trip_count=21)

        after = get_cached_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )

        self.assertEqual(before['summary']['rated_shift_count'], 1)
        self.assertEqual(after['summary']['rated_shift_count'], 2)
        self.assertNotEqual(
            before['source_fingerprint'],
            after['source_fingerprint'],
        )

    def test_cache_invalidates_when_unlinked_cohort_shift_appears(self):
        driver = self.employee('Водитель кэша с новой несвязанной сменой')
        self.snapshot(driver, ordinal=1, trip_count=20)
        before = get_cached_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )
        self._plain_closed_shift(
            driver,
            production_date=self.production_date - timedelta(days=3),
        )

        after = get_cached_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )

        self.assertTrue(before['available'])
        self.assertFalse(after['available'])
        self.assertEqual(
            after['summary']['withheld_reasons'],
            {'rating_period_unlinked_shift': 1},
        )

    def test_cache_invalidates_for_new_solely_unlinked_employee_in_scope(self):
        linked_driver = self.employee(
            'Связанный водитель явной области кэша'
        )
        unlinked_driver = self.employee(
            'Будущий несвязанный водитель явной области кэша'
        )
        self.snapshot(linked_driver, ordinal=1, trip_count=20)
        allowed_employee_ids = (
            linked_driver.id,
            unlinked_driver.id,
        )
        before = get_cached_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
            allowed_employee_ids=allowed_employee_ids,
            expected_employee_ids=allowed_employee_ids,
        )
        self._plain_closed_shift(
            unlinked_driver,
            production_date=self.production_date - timedelta(days=3),
        )

        after = get_cached_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
            allowed_employee_ids=allowed_employee_ids,
            expected_employee_ids=allowed_employee_ids,
        )

        self.assertTrue(before['available'])
        self.assertFalse(after['available'])
        self.assertEqual(
            after['summary']['withheld_reasons'],
            {'rating_period_unlinked_shift': 1},
        )

    def test_cache_invalidates_when_other_composition_shift_appears(self):
        driver = self.employee('Водитель кэша с новой чужой сменой')
        self.snapshot(driver, ordinal=1, trip_count=20)
        before = get_cached_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )
        other_composition = WatchComposition.objects.create(
            code='cache-late-other-composition',
            name='Поздний чужой состав для кэша',
        )
        other_watch = WatchPeriod.objects.create(
            name='Поздняя чужая вахта для кэша',
            watch_composition=other_composition,
            starts_on=self.rating_period.starts_on,
            ends_on=self.rating_period.ends_before - timedelta(days=1),
        )
        self._plain_closed_shift(
            driver,
            production_date=self.production_date - timedelta(days=3),
            watch_period=other_watch,
        )

        after = get_cached_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )

        self.assertTrue(before['available'])
        self.assertFalse(after['available'])
        self.assertEqual(
            after['summary']['withheld_reasons'],
            {'rating_period_other_composition_shift': 1},
        )

    def test_cache_is_isolated_by_watch_composition(self):
        first_composition = self.composition
        first_driver = self.employee('Водитель первой кэш-группы')
        self.snapshot(first_driver, ordinal=1, trip_count=20)
        second_composition = WatchComposition.objects.create(
            code='second-cache-rating-composition',
            name='Второй состав кэш-группы',
        )
        self.composition = second_composition
        self.watch = WatchPeriod.objects.create(
            name='Вторая кэш-вахта',
            watch_composition=second_composition,
            starts_on=self.rating_period.starts_on,
            ends_on=self.rating_period.ends_before - timedelta(days=1),
        )
        second_driver = self.employee('Водитель второй кэш-группы')
        self.snapshot(second_driver, ordinal=2, trip_count=23)

        first = get_cached_driver_rating_period(
            self.rating_period,
            first_composition,
            shift_type=ShiftType.DAY,
        )
        second = get_cached_driver_rating_period(
            self.rating_period,
            second_composition,
            shift_type=ShiftType.DAY,
        )

        self.assertEqual(
            [entry['employee_id'] for entry in first['entries']],
            [first_driver.id],
        )
        self.assertEqual(
            [entry['employee_id'] for entry in second['entries']],
            [second_driver.id],
        )
        self.assertNotEqual(
            first['source_fingerprint'],
            second['source_fingerprint'],
        )
        self.assertFalse(first['official'])
        self.assertFalse(second['official'])

    def test_linkage_audit_reports_two_covered_watches(self):
        driver = self.employee('Водитель аудита периода')
        self.snapshot(driver, ordinal=10, trip_count=20)
        self.watch = WatchPeriod.objects.create(
            name='Вторая вахта аудита периода',
            watch_composition=self.composition,
            starts_on=self.production_date - timedelta(days=5),
            ends_on=self.production_date + timedelta(days=10),
        )
        self.snapshot(driver, ordinal=2, trip_count=20)

        audit = build_driver_rating_period_linkage_audit(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )

        self.assertTrue(audit['linkage_ready'])
        self.assertEqual(audit['covered_watch_period_count'], 2)
