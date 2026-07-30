import io
from datetime import timedelta
from unittest import skipIf
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.production_time import production_work_date
from shifts.models import EmployeeShift, ShiftType
from users.models import WatchComposition

from .driver_rating_materialization import (
    DriverRatingMaterializationError,
    DriverRatingSnapshotUnavailable,
    _driver_rating_refresh_lock,
    _lock_identity,
    get_materialized_driver_rating_period,
    refresh_driver_rating_group,
)
from .driver_rating_scope_membership import (
    discover_driver_rating_current_scope,
    discover_driver_rating_group_scope,
)
from .models import (
    DriverRatingPeriodMaterializedSnapshot,
    DriverRatingSnapshotRefreshStatus,
    DriverShiftPassportCaptureRequest,
    DriverShiftPassportRequestStatus,
    DriverShiftPassportTrigger,
    RatingPeriod,
)
from .test_driver_watch_rating import DriverRatingFixtureMixin


@override_settings(
    PORTAL_SITE_CODE='rating-materialization-tests',
    DRIVER_RATING_SNAPSHOT_REFRESH_SECONDS=300,
    DRIVER_RATING_SNAPSHOT_SOFT_STALE_SECONDS=600,
    DRIVER_RATING_SNAPSHOT_HARD_EXPIRE_SECONDS=1800,
)
class DriverRatingMaterializationTests(
    DriverRatingFixtureMixin,
    TransactionTestCase,
):
    def setUp(self):
        super().setUp()
        self.rating_period = RatingPeriod.objects.create(
            name='Период проверки общего снимка рейтинга',
            starts_on=self.watch.starts_on,
            ends_before=self.watch.ends_on + timedelta(days=1),
            comment='Изолированная техническая проверка материализации.',
        )
        self.driver = self.employee('Водитель общего снимка рейтинга')

    def _refresh(self, **overrides):
        kwargs = {'shift_type': ShiftType.DAY}
        kwargs.update(overrides)
        return refresh_driver_rating_group(
            self.rating_period,
            self.composition,
            **kwargs,
        )

    def _read(self, **overrides):
        kwargs = {
            'shift_type': ShiftType.DAY,
            'allowed_employee_ids': (self.driver.id,),
            'expected_employee_ids': (self.driver.id,),
        }
        kwargs.update(overrides)
        return get_materialized_driver_rating_period(
            self.rating_period,
            self.composition,
            **kwargs,
        )

    def _closed_driver_shift(
        self,
        employee,
        *,
        shift_type=ShiftType.DAY,
        watch_period=None,
        equipment=None,
    ):
        opened_at = self.now - timedelta(days=1, hours=12)
        return EmployeeShift.objects.create(
            employee=employee,
            shift_type=shift_type,
            workplace_code='driver',
            watch_period=watch_period,
            equipment=equipment,
            opened_at=opened_at,
            closed_at=opened_at + timedelta(hours=12),
            opened_by=employee,
            closed_by=employee,
        )

    def test_publish_and_read_use_one_shared_database_row(self):
        self.snapshot(self.driver, ordinal=1, trip_count=20)

        result = self._refresh()

        self.assertEqual(result.status, 'published')
        self.assertEqual(result.revision, 1)
        with patch(
            (
                'reports.driver_rating_materialization.'
                'build_driver_rating_period'
            )
        ) as calculator:
            with self.assertNumQueries(1):
                payload = self._read()
        calculator.assert_not_called()
        self.assertTrue(payload['available'])
        self.assertEqual(payload['snapshot_status'], 'fresh')
        self.assertEqual(payload['snapshot_revision'], 1)
        self.assertEqual(
            [entry['employee_id'] for entry in payload['entries']],
            [self.driver.id],
        )

    def test_unchanged_refresh_only_moves_success_heartbeat(self):
        self.snapshot(self.driver, ordinal=1, trip_count=20)
        first = self._refresh()
        before = DriverRatingPeriodMaterializedSnapshot.objects.get(
            pk=first.snapshot_id,
        )
        before_payload = before.payload
        before_published_at = before.published_at

        with CaptureQueriesContext(connection) as queries:
            second = self._refresh()

        after = DriverRatingPeriodMaterializedSnapshot.objects.get(
            pk=first.snapshot_id,
        )
        self.assertEqual(second.status, 'verified')
        self.assertFalse(second.changed)
        self.assertEqual(after.revision, 1)
        self.assertEqual(after.payload, before_payload)
        self.assertEqual(after.published_at, before_published_at)
        self.assertGreaterEqual(after.last_success_at, before.last_success_at)
        update_queries = [
            query['sql']
            for query in queries.captured_queries
            if (
                query['sql'].lstrip().upper().startswith('UPDATE')
                and DriverRatingPeriodMaterializedSnapshot._meta.db_table
                in query['sql']
            )
        ]
        self.assertEqual(len(update_queries), 1)
        self.assertNotIn('"payload"', update_queries[0])
        self.assertNotIn('"member_employee_ids"', update_queries[0])

    def test_changed_sources_publish_next_revision(self):
        self.snapshot(self.driver, ordinal=1, trip_count=20)
        first = self._refresh()
        self.snapshot(self.driver, ordinal=2, trip_count=23)

        second = self._refresh()
        payload = self._read()

        self.assertTrue(second.changed)
        self.assertEqual(second.revision, first.revision + 1)
        self.assertEqual(payload['summary']['rated_shift_count'], 2)

    def test_day_and_night_current_and_member_scopes_are_separate(self):
        day_driver = self.employee('Дневной водитель раздельного состава')
        night_driver = self.employee('Ночной водитель раздельного состава')
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

        day_scope = discover_driver_rating_current_scope(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )
        night_scope = discover_driver_rating_current_scope(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.NIGHT,
        )
        self._refresh(shift_type=ShiftType.DAY)
        self._refresh(shift_type=ShiftType.NIGHT)
        day_snapshot = (
            DriverRatingPeriodMaterializedSnapshot.objects.get(
                rating_period=self.rating_period,
                watch_composition=self.composition,
                shift_type=ShiftType.DAY,
            )
        )
        night_snapshot = (
            DriverRatingPeriodMaterializedSnapshot.objects.get(
                rating_period=self.rating_period,
                watch_composition=self.composition,
                shift_type=ShiftType.NIGHT,
            )
        )

        self.assertEqual(
            day_scope.expected_employee_ids,
            (day_driver.id,),
        )
        self.assertEqual(
            night_scope.expected_employee_ids,
            (night_driver.id,),
        )
        self.assertEqual(
            day_snapshot.member_employee_ids,
            [day_driver.id],
        )
        self.assertEqual(
            night_snapshot.member_employee_ids,
            [night_driver.id],
        )
        self.assertEqual(
            [
                entry['employee_id']
                for entry in day_snapshot.payload['entries']
            ],
            [day_driver.id],
        )
        self.assertEqual(
            [
                entry['employee_id']
                for entry in night_snapshot.payload['entries']
            ],
            [night_driver.id],
        )

    def test_pending_closed_shift_is_in_current_expected_scope(self):
        driver = self.employee('Водитель ожидающего паспорта')
        shift = self._closed_driver_shift(
            driver,
            watch_period=self.watch,
            equipment=None,
        )
        DriverShiftPassportCaptureRequest.objects.create(
            shift=shift,
            request_key=f'pending-current-scope-{shift.id}',
            trigger=DriverShiftPassportTrigger.DRIVER_CLOSE,
            calculator_version='pending-current-scope-v1',
            closed_at=shift.closed_at,
            status=DriverShiftPassportRequestStatus.PENDING,
        )

        scope = discover_driver_rating_current_scope(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )
        full_scope = discover_driver_rating_group_scope(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )

        self.assertEqual(
            scope.expected_employee_ids,
            (driver.id,),
        )
        self.assertEqual(
            full_scope.expected_employee_ids,
            (driver.id,),
        )
        self.assertEqual(
            full_scope.historical_employee_ids,
            (),
        )
        self.assertEqual(
            full_scope.latest_closed_at[driver.id],
            shift.closed_at,
        )
        self.assertTrue(
            shift.passport_capture_requests.filter(
                status=DriverShiftPassportRequestStatus.PENDING,
            ).exists()
        )

    def test_linked_shift_stays_with_its_composition_after_transfer(self):
        driver = self.employee(
            'Водитель с исторической связью состава',
        )
        self._closed_driver_shift(
            driver,
            watch_period=self.watch,
            equipment=None,
        )
        current_composition = WatchComposition.objects.create(
            code='current-composition-after-linked-shift',
            name='Текущий состав после перевода',
        )
        driver.watch_composition = current_composition
        driver.save(update_fields=['watch_composition'])

        historical_scope = discover_driver_rating_current_scope(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )
        current_scope = discover_driver_rating_current_scope(
            self.rating_period,
            current_composition,
            shift_type=ShiftType.DAY,
        )

        self.assertEqual(
            historical_scope.expected_employee_ids,
            (driver.id,),
        )
        self.assertEqual(
            current_scope.expected_employee_ids,
            (),
        )

    def test_command_discovers_pending_old_group_after_transfer(self):
        driver = self.employee(
            'Переведённый водитель с ожидающим паспортом',
        )
        historical_composition = self.composition
        shift = self._closed_driver_shift(
            driver,
            watch_period=self.watch,
            equipment=None,
        )
        DriverShiftPassportCaptureRequest.objects.create(
            shift=shift,
            request_key=f'pending-command-group-{shift.id}',
            trigger=DriverShiftPassportTrigger.DRIVER_CLOSE,
            calculator_version='pending-command-group-v1',
            closed_at=shift.closed_at,
            status=DriverShiftPassportRequestStatus.PENDING,
        )
        current_composition = WatchComposition.objects.create(
            code='pending-command-current-composition',
            name='Новый состав переведённого водителя',
        )
        driver.watch_composition = current_composition
        driver.save(update_fields=['watch_composition'])

        call_command(
            'refresh_driver_rating_snapshots',
            rating_period=self.rating_period.id,
            shift_types=[ShiftType.DAY],
            strict=True,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            no_color=True,
            verbosity=0,
        )

        historical_snapshot = (
            DriverRatingPeriodMaterializedSnapshot.objects.get(
                rating_period=self.rating_period,
                watch_composition=historical_composition,
                shift_type=ShiftType.DAY,
            )
        )
        self.assertEqual(
            historical_snapshot.member_employee_ids,
            [driver.id],
        )
        self.assertEqual(
            historical_snapshot.member_latest_closed_at[
                str(driver.id)
            ],
            shift.closed_at.isoformat(),
        )
        self.assertTrue(
            shift.passport_capture_requests.filter(
                status=DriverShiftPassportRequestStatus.PENDING,
                snapshot__isnull=True,
            ).exists()
        )

    def test_no_shift_colleague_is_not_expected_member_or_entry(self):
        driver = self.employee('Водитель со сменой для текущей группы')
        colleague = self.employee('Коллега состава без закрытой смены')
        self.snapshot(driver, ordinal=1, trip_count=20)

        scope = discover_driver_rating_current_scope(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )
        result = self._refresh()
        snapshot = (
            DriverRatingPeriodMaterializedSnapshot.objects.get(
                pk=result.snapshot_id,
            )
        )

        self.assertIn(colleague.id, scope.allowed_employee_ids)
        self.assertEqual(
            scope.expected_employee_ids,
            (driver.id,),
        )
        self.assertEqual(
            snapshot.member_employee_ids,
            [driver.id],
        )
        self.assertEqual(
            [
                entry['employee_id']
                for entry in snapshot.payload['entries']
            ],
            [driver.id],
        )

    def test_unlinked_current_candidate_is_withheld_fail_closed(self):
        linked_driver = self.employee(
            'Связанный водитель текущего состава',
        )
        driver = self.employee('Водитель текущего состава без вахты')
        self.snapshot(linked_driver, ordinal=2, trip_count=20)
        self._closed_driver_shift(
            driver,
            watch_period=None,
            equipment=self.truck,
        )

        scope = discover_driver_rating_current_scope(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
        )
        result = self._refresh()
        snapshot = (
            DriverRatingPeriodMaterializedSnapshot.objects.get(
                pk=result.snapshot_id,
            )
        )

        self.assertEqual(
            scope.expected_employee_ids,
            (linked_driver.id, driver.id),
        )
        self.assertFalse(snapshot.payload['available'])
        self.assertEqual(
            snapshot.payload['linkage_audit']['unlinked_shift_count'],
            1,
        )
        self.assertEqual(
            snapshot.payload['summary']['withheld_reasons'],
            {'rating_period_unlinked_shift': 1},
        )

    def test_missing_scope_and_integrity_fail_closed_without_calculation(self):
        with self.assertRaises(DriverRatingSnapshotUnavailable) as missing:
            self._read()
        self.assertEqual(missing.exception.code, 'snapshot_missing')
        self.assertEqual(missing.exception.http_status, 503)

        self.snapshot(self.driver, ordinal=1, trip_count=20)
        result = self._refresh()
        other = self.employee('Новый водитель после готового снимка')
        with self.assertRaises(DriverRatingSnapshotUnavailable) as mismatch:
            self._read(
                allowed_employee_ids=(self.driver.id, other.id),
                expected_employee_ids=(self.driver.id, other.id),
            )
        self.assertEqual(mismatch.exception.code, 'snapshot_scope_mismatch')

        snapshot = DriverRatingPeriodMaterializedSnapshot.objects.get(
            pk=result.snapshot_id,
        )
        damaged = dict(snapshot.payload)
        damaged['entries'] = []
        DriverRatingPeriodMaterializedSnapshot.objects.filter(
            pk=snapshot.pk,
        ).update(payload=damaged)
        with self.assertRaises(DriverRatingSnapshotUnavailable) as damaged_read:
            self._read()
        self.assertEqual(
            damaged_read.exception.code,
            'snapshot_integrity_failed',
        )

        DriverRatingPeriodMaterializedSnapshot.objects.filter(
            pk=snapshot.pk,
        ).update(payload=snapshot.payload)
        DriverRatingPeriodMaterializedSnapshot.objects.filter(
            pk=snapshot.pk,
        ).update(
            member_latest_closed_at={
                str(self.driver.id): timezone.now().isoformat(),
            },
        )
        with self.assertRaises(
            DriverRatingSnapshotUnavailable
        ) as damaged_members:
            self._read()
        self.assertEqual(
            damaged_members.exception.code,
            'snapshot_member_integrity_failed',
        )

        DriverRatingPeriodMaterializedSnapshot.objects.filter(
            pk=snapshot.pk,
        ).update(member_employee_ids=['broken'])
        with self.assertRaises(
            DriverRatingSnapshotUnavailable
        ) as malformed_members:
            self._read()
        self.assertEqual(
            malformed_members.exception.code,
            'snapshot_member_integrity_failed',
        )

    def test_soft_stale_is_served_but_hard_expired_is_hidden(self):
        self.snapshot(self.driver, ordinal=1, trip_count=20)
        result = self._refresh()
        snapshot = DriverRatingPeriodMaterializedSnapshot.objects.get(
            pk=result.snapshot_id,
        )

        delayed = self._read(
            now=snapshot.last_success_at + timedelta(seconds=601),
        )

        self.assertEqual(delayed['snapshot_status'], 'delayed')
        self.assertIn('задерживается', delayed['snapshot_warning'])
        with self.assertRaises(DriverRatingSnapshotUnavailable) as expired:
            self._read(
                now=snapshot.last_success_at
                + timedelta(seconds=1801),
            )
        self.assertEqual(expired.exception.code, 'snapshot_expired')
        self.assertEqual(expired.exception.http_status, 409)

    def test_failed_refresh_keeps_last_good_payload_and_marks_delay(self):
        self.snapshot(self.driver, ordinal=1, trip_count=20)
        result = self._refresh()
        before = DriverRatingPeriodMaterializedSnapshot.objects.get(
            pk=result.snapshot_id,
        )

        with (
            patch(
                (
                    'reports.driver_rating_materialization.'
                    'build_driver_rating_period'
                ),
                side_effect=RuntimeError('synthetic refresh failure'),
            ),
            CaptureQueriesContext(connection) as queries,
        ):
            with self.assertRaises(DriverRatingMaterializationError):
                self._refresh()

        after = DriverRatingPeriodMaterializedSnapshot.objects.get(
            pk=result.snapshot_id,
        )
        self.assertEqual(after.revision, before.revision)
        self.assertEqual(
            after.payload_fingerprint,
            before.payload_fingerprint,
        )
        self.assertEqual(
            after.last_refresh_status,
            DriverRatingSnapshotRefreshStatus.FAILED,
        )
        self.assertEqual(after.consecutive_failure_count, 1)
        self.assertNotIn(
            'synthetic refresh failure',
            str(self._read()),
        )
        self.assertEqual(self._read()['snapshot_status'], 'delayed')
        update_queries = [
            query['sql']
            for query in queries.captured_queries
            if (
                query['sql'].lstrip().upper().startswith('UPDATE')
                and DriverRatingPeriodMaterializedSnapshot._meta.db_table
                in query['sql']
            )
        ]
        self.assertEqual(len(update_queries), 1)
        self.assertNotIn('"payload"', update_queries[0])
        self.assertNotIn('"member_employee_ids"', update_queries[0])

    def test_failed_refresh_for_changed_scope_does_not_relabel_old_payload(self):
        self.snapshot(self.driver, ordinal=1, trip_count=20)
        result = self._refresh()
        other = self.employee('Водитель изменившейся области')
        with patch(
            (
                'reports.driver_rating_materialization.'
                'build_driver_rating_period'
            ),
            side_effect=RuntimeError('synthetic changed scope failure'),
        ):
            with self.assertRaises(DriverRatingMaterializationError):
                self._refresh()

        snapshot = DriverRatingPeriodMaterializedSnapshot.objects.get(
            pk=result.snapshot_id,
        )
        self.assertEqual(snapshot.revision, 1)
        with self.assertRaises(DriverRatingSnapshotUnavailable) as error:
            self._read(
                allowed_employee_ids=(self.driver.id, other.id),
                expected_employee_ids=(self.driver.id, other.id),
            )
        self.assertEqual(error.exception.code, 'snapshot_scope_mismatch')

    @skipIf(
        connection.vendor == 'postgresql',
        (
            'PostgreSQL advisory locks belong to a database session and are '
            'covered by the two-connection regression.'
        ),
    )
    def test_process_lock_skips_concurrent_refresh_for_same_group(self):
        identity = _lock_identity(
            scope_code='rating-materialization-tests',
            rating_period=self.rating_period,
            watch_composition=self.composition,
            shift_type=ShiftType.DAY,
        )
        with _driver_rating_refresh_lock(identity) as acquired:
            self.assertTrue(acquired)
            result = self._refresh()

        self.assertEqual(result.status, 'locked')
        self.assertEqual(
            DriverRatingPeriodMaterializedSnapshot.objects.count(),
            0,
        )

    def test_management_command_builds_day_and_night_rows(self):
        self.snapshot(self.driver, ordinal=1, trip_count=20)
        first_output = io.StringIO()

        call_command(
            'refresh_driver_rating_snapshots',
            rating_period=self.rating_period.id,
            strict=True,
            stdout=first_output,
            no_color=True,
            verbosity=0,
        )

        rows = DriverRatingPeriodMaterializedSnapshot.objects.filter(
            rating_period=self.rating_period,
            watch_composition=self.composition,
        )
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            set(rows.values_list('shift_type', flat=True)),
            {ShiftType.DAY, ShiftType.NIGHT},
        )
        before = {
            row.shift_type: (row.revision, row.published_at)
            for row in rows
        }
        self.assertIn('/day: published, revision=1', first_output.getvalue())
        self.assertIn(
            '/night: published, revision=1',
            first_output.getvalue(),
        )

        second_output = io.StringIO()
        call_command(
            'refresh_driver_rating_snapshots',
            rating_period=self.rating_period.id,
            strict=True,
            stdout=second_output,
            no_color=True,
            verbosity=0,
        )
        after = {
            row.shift_type: (row.revision, row.published_at)
            for row in DriverRatingPeriodMaterializedSnapshot.objects.filter(
                rating_period=self.rating_period,
                watch_composition=self.composition,
            )
        }
        self.assertEqual(after, before)
        self.assertIn('/day: verified, revision=1', second_output.getvalue())
        self.assertIn(
            '/night: verified, revision=1',
            second_output.getvalue(),
        )

    def test_management_command_rejects_foreign_site_code(self):
        with self.assertRaisesMessage(
            CommandError,
            'не совпадает с областью сотрудников',
        ):
            call_command(
                'refresh_driver_rating_snapshots',
                rating_period=self.rating_period.id,
                site_code='foreign-rating-site',
                strict=True,
                verbosity=0,
            )
        self.assertFalse(
            DriverRatingPeriodMaterializedSnapshot.objects.exists()
        )

    def test_command_without_current_period_can_be_noop_or_strict_error(self):
        self.rating_period.is_active = False
        self.rating_period.save()
        today = production_work_date()
        self.assertFalse(
            RatingPeriod.objects.filter(
                is_active=True,
                starts_on__lte=today,
                ends_before__gt=today,
            ).exists()
        )

        call_command(
            'refresh_driver_rating_snapshots',
            verbosity=0,
        )
        with self.assertRaisesMessage(
            CommandError,
            'активный период рейтинга не задан',
        ):
            call_command(
                'refresh_driver_rating_snapshots',
                strict=True,
                verbosity=0,
            )
