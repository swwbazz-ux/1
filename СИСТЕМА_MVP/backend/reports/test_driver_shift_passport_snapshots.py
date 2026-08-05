import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import transaction
from django.db.migrations.loader import MigrationLoader
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from references.models import (
    DumpPoint,
    Equipment,
    EquipmentModel,
    EquipmentType,
    RockType,
)
from shifts.admin import EmployeeShiftAdmin
from shifts.models import EmployeeShift, ShiftType
from shifts.services import close_driver_shift
from trips.models import Trip, TripStatus
from users.active_role import activate_role_session
from users.models import Employee, EmployeeAccess, Role

from .driver_shift_passport_snapshots import (
    DRIVER_SHIFT_PASSPORT_CALCULATOR_VERSION,
    LEGACY_REQUEST_SUPERSEDED_PREFIX,
    enqueue_driver_shift_passport_capture,
    enqueue_driver_shift_passport_rebuild,
    process_driver_shift_passport_request,
    safe_process_driver_shift_passport_request,
)
from .models import (
    DriverShiftPassportCaptureRequest,
    DriverShiftPassportRequestStatus,
    DriverShiftPassportSnapshot,
    DriverShiftPassportTrigger,
)


class DriverPassportMigrationDependencyTests(SimpleTestCase):
    def test_reports_0006_requires_trip_cancelled_at_schema(self):
        target = ('reports', '0006_driver_shift_passport_snapshots')
        prerequisite = ('trips', '0008_trip_cancelled_at')
        loader = MigrationLoader(None, ignore_no_migrations=True)

        self.assertIn(
            prerequisite,
            loader.disk_migrations[target].dependencies,
        )
        self.assertIn(prerequisite, loader.graph.forwards_plan(target))


class DriverShiftPassportSnapshotTests(TestCase):
    def assert_diagnostic_only(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                self.assertNotIn(
                    str(key).lower(),
                    {'score', 'place', 'weight'},
                )
                if str(key).lower() == 'official':
                    self.assertIs(item, False)
                if str(key).lower().startswith('official_'):
                    self.assertIs(item, False)
                self.assert_diagnostic_only(item)
        elif isinstance(value, list):
            for item in value:
                self.assert_diagnostic_only(item)

    def setUp(self):
        self.driver_role, _ = Role.objects.get_or_create(
            code='driver',
            defaults={'name': 'Водитель'},
        )
        self.dispatcher_role, _ = Role.objects.get_or_create(
            code='dispatcher',
            defaults={'name': 'Диспетчер'},
        )
        self.driver = Employee.objects.create(
            full_name='Водитель snapshot',
            status=Employee.Status.ACTIVE,
        )
        self.dispatcher = Employee.objects.create(
            full_name='Диспетчер snapshot',
            status=Employee.Status.ACTIVE,
        )
        self.truck_type = EquipmentType.objects.create(
            name='Самосвал snapshot',
        )
        self.truck_model = EquipmentModel.objects.create(
            equipment_type=self.truck_type,
            name='Модель самосвала snapshot',
            payload_tons=Decimal('130.00'),
            body_volume_m3=Decimal('55.00'),
            fuel_capacity_limit_l=2000,
        )
        self.truck = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number='SNAP-TRUCK',
        )
        self.excavator_type = EquipmentType.objects.create(
            name='Экскаватор snapshot',
        )
        self.excavator_model = EquipmentModel.objects.create(
            equipment_type=self.excavator_type,
            name='Модель экскаватора snapshot',
        )
        self.excavator = Equipment.objects.create(
            equipment_type=self.excavator_type,
            model=self.excavator_model,
            garage_number='SNAP-EXC',
        )
        self.rock = RockType.objects.create(
            name='Порода snapshot',
            density=Decimal('2.5000'),
        )
        self.assigned_dump_point = DumpPoint.objects.create(
            name='Назначенная разгрузка snapshot',
        )
        self.actual_dump_point = DumpPoint.objects.create(
            name='Фактическая разгрузка snapshot',
        )

    def create_open_shift(self, *, employee=None, end_readings=False):
        employee = employee or self.driver
        values = {
            'employee': employee,
            'shift_type': ShiftType.DAY,
            'workplace_code': 'driver',
            'equipment': self.truck,
            'start_fuel': Decimal('100.00'),
            'start_mileage': Decimal('1000.00'),
            'start_engine_hours': Decimal('100.00'),
            'opened_at': timezone.now() - timedelta(hours=2),
            'opened_by': employee,
        }
        if end_readings:
            values.update({
                'end_fuel': Decimal('90.00'),
                'end_mileage': Decimal('1001.00'),
                'end_engine_hours': Decimal('101.00'),
            })
        return EmployeeShift.objects.create(**values)

    def close_readings(self):
        return {
            'end_fuel': Decimal('90.00'),
            'end_mileage': Decimal('1001.00'),
            'end_engine_hours': Decimal('101.00'),
        }

    def close_without_passport_capture(self):
        shift = self.create_open_shift(end_readings=True)
        EmployeeShift.objects.filter(pk=shift.pk).update(
            closed_at=timezone.now(),
            closed_by=self.driver,
        )
        shift.refresh_from_db()
        return shift

    def close_normally(self, shift, *, action_id='snapshot-close'):
        with self.captureOnCommitCallbacks(execute=True):
            result = close_driver_shift(
                shift=shift,
                employee=self.driver,
                readings=self.close_readings(),
                client_action_id=action_id,
            )
        return result

    def create_cancelled_trip(self, shift):
        return Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            driver=self.driver,
            unloading_shift=shift,
            rock_type=self.rock,
            dump_point=self.assigned_dump_point,
            assigned_dump_point=self.assigned_dump_point,
            actual_dump_point=self.actual_dump_point,
            volume_m3=Decimal('20.50'),
            tonnage=Decimal('51.25'),
            loading_horizon='Горизонт 1',
            loading_block='Блок А',
            transport_distance_km=Decimal('1.75'),
            status=TripStatus.CANCELLED,
            cancelled_at=timezone.now() - timedelta(minutes=10),
        )

    def test_normal_close_persists_json_safe_diagnostic_snapshot(self):
        shift = self.create_open_shift()
        cancelled_trip = self.create_cancelled_trip(shift)

        closed_shift, created = self.close_normally(shift)

        self.assertTrue(created)
        closed_shift.refresh_from_db()
        capture_request = DriverShiftPassportCaptureRequest.objects.get(
            shift=closed_shift,
        )
        self.assertEqual(
            capture_request.status,
            DriverShiftPassportRequestStatus.COMPLETED,
        )
        snapshot = capture_request.snapshot
        self.assertEqual(snapshot.revision, 1)
        self.assertEqual(
            snapshot.trigger,
            DriverShiftPassportTrigger.DRIVER_CLOSE,
        )
        self.assertFalse(snapshot.captured_late)
        self.assertEqual(snapshot.captured_by, self.driver)
        self.assertFalse(snapshot.payload['official'])
        self.assertFalse(
            snapshot.payload['passport']['quality'][
                'official_rating_eligible'
            ]
        )
        self.assert_diagnostic_only(snapshot.payload)
        self.assertIn(
            'cycle_samples',
            snapshot.payload['aggregation_inputs'],
        )
        json.dumps(snapshot.payload)
        self.assertEqual(
            snapshot.payload['source_manifest']['shift']['start_fuel'],
            '100.00',
        )
        self.assertEqual(
            snapshot.payload['source_manifest']['overlap_shifts'],
            [],
        )

        trip_manifest = snapshot.payload['source_manifest']['trips']
        self.assertEqual(
            [item['id'] for item in trip_manifest],
            [cancelled_trip.pk],
        )
        trip_source = trip_manifest[0]
        self.assertEqual(
            trip_source['truck']['model_name'],
            self.truck_model.name,
        )
        self.assertEqual(
            trip_source['truck']['model_payload_tons'],
            '130.00',
        )
        self.assertEqual(
            trip_source['truck']['model_body_volume_m3'],
            '55.00',
        )
        self.assertEqual(
            trip_source['excavator']['garage_number'],
            self.excavator.garage_number,
        )
        self.assertEqual(
            trip_source['rock_type']['density'],
            '2.5000',
        )
        self.assertEqual(
            trip_source['assigned_dump_point']['name'],
            self.assigned_dump_point.name,
        )
        self.assertEqual(
            trip_source['actual_dump_point']['name'],
            self.actual_dump_point.name,
        )
        self.assertEqual(
            snapshot.payload['passport']['trip_states']['cancelled_count'],
            1,
        )

    def test_carry_in_cancelled_after_close_stays_in_manifest_and_counts(self):
        shift = self.create_open_shift()
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            driver=self.driver,
            rock_type=self.rock,
            dump_point=self.assigned_dump_point,
            status=TripStatus.CANCELLED,
            cancelled_at=timezone.now() + timedelta(hours=1),
            is_carryover=True,
        )
        Trip.objects.filter(pk=trip.pk).update(
            created_at=shift.opened_at - timedelta(minutes=30),
        )

        self.close_normally(shift)
        snapshot = DriverShiftPassportSnapshot.objects.get(shift=shift)
        trip_manifest = snapshot.payload['source_manifest']['trips']

        self.assertEqual([item['id'] for item in trip_manifest], [trip.pk])
        self.assertEqual(
            snapshot.payload['passport']['trip_states'][
                'open_at_close_count'
            ],
            1,
        )
        self.assertEqual(
            snapshot.payload['passport']['quality']['source_counts'][
                'trip_count'
            ],
            1,
        )
        self.assertEqual(
            snapshot.payload['passport']['quality']['source_counts'][
                'carryover_trip_count'
            ],
            1,
        )

    def test_overlap_manifest_contains_only_the_other_shift(self):
        shift = self.create_open_shift()
        other_driver = Employee.objects.create(
            full_name='Другой водитель overlap snapshot',
            status=Employee.Status.ACTIVE,
        )
        overlapping_shift = EmployeeShift.objects.create(
            employee=other_driver,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            equipment=self.truck,
            opened_at=shift.opened_at + timedelta(minutes=10),
            closed_at=shift.opened_at + timedelta(minutes=30),
        )

        self.close_normally(shift)
        snapshot = DriverShiftPassportSnapshot.objects.get(shift=shift)

        self.assertEqual(
            [
                item['id']
                for item in snapshot.payload['source_manifest'][
                    'overlap_shifts'
                ]
            ],
            [overlapping_shift.pk],
        )

    def test_model_capacity_change_creates_new_historical_revision(self):
        shift = self.create_open_shift()
        self.close_normally(shift)
        first = DriverShiftPassportSnapshot.objects.get(shift=shift)

        self.truck_model.payload_tons = Decimal('140.00')
        self.truck_model.body_volume_m3 = Decimal('60.00')
        self.truck_model.save(
            update_fields=['payload_tons', 'body_volume_m3'],
        )
        shift.refresh_from_db()
        with transaction.atomic():
            rebuild_request = enqueue_driver_shift_passport_rebuild(
                shift=shift,
            )
        second = process_driver_shift_passport_request(rebuild_request.pk)

        self.assertEqual(second.revision, 2)
        self.assertNotEqual(
            second.source_fingerprint,
            first.source_fingerprint,
        )
        first_equipment = first.payload['source_manifest']['shift'][
            'equipment'
        ]
        second_equipment = second.payload['source_manifest']['shift'][
            'equipment'
        ]
        self.assertEqual(first_equipment['model_payload_tons'], '130.00')
        self.assertEqual(first_equipment['model_body_volume_m3'], '55.00')
        self.assertEqual(second_equipment['model_payload_tons'], '140.00')
        self.assertEqual(second_equipment['model_body_volume_m3'], '60.00')

    def test_identical_retry_is_idempotent_and_source_change_adds_revision(self):
        shift = self.create_open_shift()
        cancelled_trip = self.create_cancelled_trip(shift)
        self.close_normally(shift)
        first = DriverShiftPassportSnapshot.objects.get(shift=shift)
        first_payload_fingerprint = first.payload_fingerprint

        first_request = DriverShiftPassportCaptureRequest.objects.get(
            shift=shift,
        )
        repeated = process_driver_shift_passport_request(first_request.pk)
        self.assertEqual(repeated.pk, first.pk)
        self.assertEqual(
            DriverShiftPassportSnapshot.objects.filter(shift=shift).count(),
            1,
        )

        changed_cancelled_at = (
            shift.opened_at + timedelta(minutes=30)
        )
        Trip.objects.filter(pk=cancelled_trip.pk).update(
            cancelled_at=changed_cancelled_at,
        )
        shift.refresh_from_db()
        with transaction.atomic():
            rebuild_request = enqueue_driver_shift_passport_rebuild(
                shift=shift,
            )
        second = process_driver_shift_passport_request(rebuild_request.pk)

        self.assertEqual(second.revision, 2)
        self.assertNotEqual(
            second.source_fingerprint,
            first.source_fingerprint,
        )
        self.assertNotEqual(
            second.payload_fingerprint,
            first_payload_fingerprint,
        )
        self.assertTrue(second.captured_late)
        first.refresh_from_db()
        self.assertEqual(
            first.payload_fingerprint,
            first_payload_fingerprint,
        )

        with transaction.atomic():
            unchanged_request = enqueue_driver_shift_passport_rebuild(
                shift=shift,
            )
        unchanged = process_driver_shift_passport_request(
            unchanged_request.pk,
        )
        self.assertEqual(unchanged.pk, second.pk)
        self.assertEqual(
            DriverShiftPassportSnapshot.objects.filter(shift=shift).count(),
            2,
        )

    def test_ready_snapshot_is_append_only(self):
        shift = self.create_open_shift()
        self.close_normally(shift)
        snapshot = DriverShiftPassportSnapshot.objects.get(shift=shift)

        snapshot.trigger = DriverShiftPassportTrigger.BACKFILL
        with self.assertRaises(ValidationError):
            snapshot.save(update_fields=['trigger'])
        with self.assertRaises(ValidationError):
            snapshot.delete()
        with self.assertRaises(ValidationError):
            DriverShiftPassportSnapshot.objects.filter(
                pk=snapshot.pk,
            ).update(trigger=DriverShiftPassportTrigger.BACKFILL)
        with self.assertRaises(ValidationError):
            DriverShiftPassportSnapshot.objects.filter(
                pk=snapshot.pk,
            ).delete()

    def test_direct_legacy_request_is_superseded_by_current_request(self):
        shift = self.close_without_passport_capture()
        with transaction.atomic():
            legacy_request = enqueue_driver_shift_passport_capture(
                shift=shift,
                trigger=DriverShiftPassportTrigger.DRIVER_CLOSE,
                calculator_version='driver-shift-passport-v1',
                schedule_on_commit=False,
            )
        snapshot = process_driver_shift_passport_request(
            legacy_request.pk,
        )

        legacy_request.refresh_from_db()
        current_request = (
            DriverShiftPassportCaptureRequest.objects
            .exclude(pk=legacy_request.pk)
            .get(shift=shift)
        )
        self.assertEqual(
            legacy_request.status,
            DriverShiftPassportRequestStatus.FAILED,
        )
        self.assertTrue(
            legacy_request.last_error.startswith(
                LEGACY_REQUEST_SUPERSEDED_PREFIX,
            )
        )
        self.assertIn(
            f'current_request_id={current_request.pk}',
            legacy_request.last_error,
        )
        self.assertEqual(
            current_request.trigger,
            DriverShiftPassportTrigger.CALCULATOR_UPGRADE,
        )
        self.assertEqual(
            current_request.status,
            DriverShiftPassportRequestStatus.COMPLETED,
        )
        self.assertEqual(snapshot, current_request.snapshot)
        self.assertEqual(
            snapshot.calculator_version,
            DRIVER_SHIFT_PASSPORT_CALCULATOR_VERSION,
        )
        self.assertEqual(
            snapshot.payload['calculator_version'],
            DRIVER_SHIFT_PASSPORT_CALCULATOR_VERSION,
        )
        self.assertFalse(
            DriverShiftPassportSnapshot.objects.filter(
                shift=shift,
                calculator_version='driver-shift-passport-v1',
            ).exists()
        )

    def test_safe_on_commit_supersedes_legacy_request_once(self):
        shift = self.close_without_passport_capture()
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                legacy_request = enqueue_driver_shift_passport_capture(
                    shift=shift,
                    trigger=DriverShiftPassportTrigger.DRIVER_CLOSE,
                    calculator_version='driver-shift-passport-v1',
                )

        legacy_request.refresh_from_db()
        current_request = (
            DriverShiftPassportCaptureRequest.objects
            .exclude(pk=legacy_request.pk)
            .get(shift=shift)
        )
        self.assertEqual(
            legacy_request.status,
            DriverShiftPassportRequestStatus.FAILED,
        )
        self.assertEqual(
            current_request.status,
            DriverShiftPassportRequestStatus.COMPLETED,
        )
        snapshot = safe_process_driver_shift_passport_request(
            legacy_request.pk,
        )
        legacy_request.refresh_from_db()
        current_request.refresh_from_db()
        self.assertEqual(snapshot, current_request.snapshot)
        self.assertEqual(legacy_request.attempt_count, 1)
        self.assertEqual(
            DriverShiftPassportCaptureRequest.objects.filter(
                shift=shift,
            ).count(),
            2,
        )
        self.assertEqual(
            DriverShiftPassportSnapshot.objects.filter(
                shift=shift,
            ).count(),
            1,
        )

    def test_rebuild_command_supersedes_all_legacy_queue_states_once(self):
        pending_shift = self.close_without_passport_capture()
        processing_shift = self.close_without_passport_capture()
        failed_shift = self.close_without_passport_capture()
        with transaction.atomic():
            pending_request = enqueue_driver_shift_passport_capture(
                shift=pending_shift,
                trigger=DriverShiftPassportTrigger.DRIVER_CLOSE,
                calculator_version='driver-shift-passport-v1',
                schedule_on_commit=False,
            )
            processing_request = enqueue_driver_shift_passport_capture(
                shift=processing_shift,
                trigger=DriverShiftPassportTrigger.DRIVER_CLOSE,
                calculator_version='driver-shift-passport-v1',
                schedule_on_commit=False,
            )
            failed_request = enqueue_driver_shift_passport_capture(
                shift=failed_shift,
                trigger=DriverShiftPassportTrigger.DRIVER_CLOSE,
                calculator_version='driver-shift-passport-v1',
                schedule_on_commit=False,
            )
        DriverShiftPassportCaptureRequest.objects.filter(
            pk=processing_request.pk,
        ).update(
            status=DriverShiftPassportRequestStatus.PROCESSING,
            attempt_count=1,
            last_error='',
        )
        DriverShiftPassportCaptureRequest.objects.filter(
            pk=failed_request.pk,
        ).update(
            status=DriverShiftPassportRequestStatus.FAILED,
            attempt_count=1,
            last_error='legacy transient failure',
        )

        stdout = StringIO()
        stderr = StringIO()
        call_command(
            'rebuild_driver_shift_passports',
            shift_ids=[
                pending_shift.pk,
                processing_shift.pk,
                failed_shift.pk,
            ],
            stdout=stdout,
            stderr=stderr,
        )

        pending_request.refresh_from_db()
        processing_request.refresh_from_db()
        failed_request.refresh_from_db()
        for legacy_request in (
            pending_request,
            processing_request,
            failed_request,
        ):
            self.assertEqual(
                legacy_request.status,
                DriverShiftPassportRequestStatus.FAILED,
            )
            self.assertTrue(
                legacy_request.last_error.startswith(
                    LEGACY_REQUEST_SUPERSEDED_PREFIX,
                )
            )
            current_request = (
                DriverShiftPassportCaptureRequest.objects
                .filter(
                    shift=legacy_request.shift,
                    calculator_version=(
                        DRIVER_SHIFT_PASSPORT_CALCULATOR_VERSION
                    ),
                )
                .get()
            )
            self.assertEqual(
                current_request.status,
                DriverShiftPassportRequestStatus.COMPLETED,
            )
            self.assertEqual(
                current_request.trigger,
                DriverShiftPassportTrigger.CALCULATOR_UPGRADE,
            )
        self.assertEqual(stderr.getvalue(), '')

        state_before_repeat = {
            shift.pk: (
                DriverShiftPassportCaptureRequest.objects.filter(
                    shift=shift,
                ).count(),
                DriverShiftPassportSnapshot.objects.get(
                    shift=shift,
                ).pk,
            )
            for shift in (
                pending_shift,
                processing_shift,
                failed_shift,
            )
        }
        pending_attempts = pending_request.attempt_count
        processing_attempts = processing_request.attempt_count
        failed_attempts = failed_request.attempt_count
        repeat_stdout = StringIO()
        repeat_stderr = StringIO()
        call_command(
            'rebuild_driver_shift_passports',
            shift_ids=[
                pending_shift.pk,
                processing_shift.pk,
                failed_shift.pk,
            ],
            stdout=repeat_stdout,
            stderr=repeat_stderr,
        )
        pending_request.refresh_from_db()
        processing_request.refresh_from_db()
        failed_request.refresh_from_db()

        self.assertEqual(pending_request.attempt_count, pending_attempts)
        self.assertEqual(
            processing_request.attempt_count,
            processing_attempts,
        )
        self.assertEqual(failed_request.attempt_count, failed_attempts)
        self.assertEqual(repeat_stderr.getvalue(), '')
        for shift in (
            pending_shift,
            processing_shift,
            failed_shift,
        ):
            self.assertEqual(
                (
                    DriverShiftPassportCaptureRequest.objects.filter(
                        shift=shift,
                    ).count(),
                    DriverShiftPassportSnapshot.objects.get(
                        shift=shift,
                    ).pk,
                ),
                state_before_repeat[shift.pk],
            )

    def test_current_request_is_processed_without_supersede(self):
        shift = self.close_without_passport_capture()
        with transaction.atomic():
            current_request = enqueue_driver_shift_passport_capture(
                shift=shift,
                trigger=DriverShiftPassportTrigger.BACKFILL,
                schedule_on_commit=False,
            )

        snapshot = process_driver_shift_passport_request(
            current_request.pk,
        )
        repeated = process_driver_shift_passport_request(
            current_request.pk,
        )
        current_request.refresh_from_db()

        self.assertEqual(snapshot, repeated)
        self.assertEqual(
            current_request.status,
            DriverShiftPassportRequestStatus.COMPLETED,
        )
        self.assertEqual(
            DriverShiftPassportCaptureRequest.objects.filter(
                shift=shift,
            ).count(),
            1,
        )
        self.assertEqual(
            DriverShiftPassportSnapshot.objects.filter(
                shift=shift,
            ).count(),
            1,
        )

    def test_snapshot_failure_does_not_block_close_and_command_retries(self):
        shift = self.create_open_shift()
        with (
            patch(
                'reports.driver_shift_passport_snapshots.'
                'build_driver_shift_timeline',
                side_effect=RuntimeError('snapshot calculator failed'),
            ),
            patch(
                'reports.driver_shift_passport_snapshots.logger.exception',
            ) as log_exception,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                closed_shift, created = close_driver_shift(
                    shift=shift,
                    employee=self.driver,
                    readings=self.close_readings(),
                    client_action_id='snapshot-failure-close',
                )
        log_exception.assert_called_once()

        self.assertTrue(created)
        closed_shift.refresh_from_db()
        self.assertIsNotNone(closed_shift.closed_at)
        capture_request = DriverShiftPassportCaptureRequest.objects.get(
            shift=closed_shift,
        )
        self.assertEqual(
            capture_request.status,
            DriverShiftPassportRequestStatus.FAILED,
        )
        self.assertIn('snapshot calculator failed', capture_request.last_error)
        self.assertFalse(
            DriverShiftPassportSnapshot.objects.filter(
                shift=closed_shift,
            ).exists()
        )

        stdout = StringIO()
        stderr = StringIO()
        call_command(
            'rebuild_driver_shift_passports',
            shift_ids=[closed_shift.pk],
            stdout=stdout,
            stderr=stderr,
        )
        capture_request.refresh_from_db()
        self.assertEqual(
            capture_request.status,
            DriverShiftPassportRequestStatus.COMPLETED,
        )
        self.assertTrue(capture_request.snapshot.captured_late)
        self.assertEqual(stderr.getvalue(), '')

    def test_rollback_removes_close_and_outbox_request(self):
        shift = self.create_open_shift()

        with self.assertRaisesMessage(RuntimeError, 'rollback close'):
            with transaction.atomic():
                shift.closed_at = timezone.now()
                shift.closed_by = self.driver
                shift.save(update_fields=['closed_at', 'closed_by'])
                enqueue_driver_shift_passport_capture(
                    shift=shift,
                    trigger=DriverShiftPassportTrigger.DRIVER_CLOSE,
                    captured_by=self.driver,
                )
                raise RuntimeError('rollback close')

        shift.refresh_from_db()
        self.assertIsNone(shift.closed_at)
        self.assertFalse(
            DriverShiftPassportCaptureRequest.objects.filter(
                shift=shift,
            ).exists()
        )

    def test_service_close_enqueues_after_driver_carryover(self):
        dispatcher_access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=self.dispatcher_role,
            access_code='snapshot-dispatcher',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        EmployeeShift.objects.create(
            employee=self.dispatcher,
            shift_type=ShiftType.DAY,
            workplace_code='dispatcher',
            opened_at=timezone.now() - timedelta(hours=1),
            opened_by=self.dispatcher,
        )
        shift = self.create_open_shift()
        active_trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            driver=self.driver,
            rock_type=self.rock,
            dump_point=self.assigned_dump_point,
            status=TripStatus.ACTIVE,
        )
        session = self.client.session
        session['employee_access_id'] = dispatcher_access.pk
        session.save()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse(
                    'dispatcher_service_close_shift',
                    args=[shift.pk],
                ),
                {
                    'reason': 'Служебный тест snapshot',
                    'end_fuel': '90.00',
                    'end_mileage': '1001.00',
                    'end_engine_hours': '101.00',
                },
                HTTP_HOST='localhost',
            )

        self.assertEqual(response.status_code, 302)
        shift.refresh_from_db()
        self.assertTrue(shift.is_service_closed)
        capture_request = DriverShiftPassportCaptureRequest.objects.get(
            shift=shift,
        )
        self.assertEqual(
            capture_request.trigger,
            DriverShiftPassportTrigger.SERVICE_CLOSE,
        )
        self.assertEqual(
            capture_request.status,
            DriverShiftPassportRequestStatus.COMPLETED,
        )
        self.assertEqual(capture_request.captured_by, self.dispatcher)
        active_trip.refresh_from_db()
        self.assertTrue(active_trip.is_carryover)
        trip_source = next(
            item
            for item in capture_request.snapshot.payload[
                'source_manifest'
            ]['trips']
            if item['id'] == active_trip.pk
        )
        self.assertTrue(trip_source['is_carryover'])

    def test_role_switch_auto_close_enqueues_without_signal_dependency(self):
        driver_access = EmployeeAccess.objects.create(
            employee=self.driver,
            role=self.driver_role,
            access_code='snapshot-driver',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            last_login_at=timezone.now() - timedelta(minutes=1),
        )
        dispatcher_access = EmployeeAccess.objects.create(
            employee=self.driver,
            role=self.dispatcher_role,
            access_code='snapshot-role-target',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        shift = self.create_open_shift(end_readings=True)
        request = RequestFactory().post('/role/switch/')
        middleware = SessionMiddleware(lambda current_request: None)
        middleware.process_request(request)
        request.session['employee_access_id'] = driver_access.pk
        request.session.save()

        with self.captureOnCommitCallbacks(execute=True):
            activate_role_session(request, dispatcher_access)

        shift.refresh_from_db()
        self.assertIsNotNone(shift.closed_at)
        capture_request = DriverShiftPassportCaptureRequest.objects.get(
            shift=shift,
        )
        self.assertEqual(
            capture_request.trigger,
            DriverShiftPassportTrigger.ROLE_SWITCH,
        )
        self.assertEqual(
            capture_request.status,
            DriverShiftPassportRequestStatus.COMPLETED,
        )

    def test_admin_cannot_edit_closure_fields_directly(self):
        model_admin = EmployeeShiftAdmin(EmployeeShift, AdminSite())

        self.assertIn('closed_at', model_admin.readonly_fields)
        self.assertIn('closed_by', model_admin.readonly_fields)
        self.assertIn('is_service_closed', model_admin.readonly_fields)
