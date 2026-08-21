import hashlib
import json
from dataclasses import FrozenInstanceError, asdict
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection, models
from django.db.models.query import QuerySet
from django.test import TestCase
from django.utils import timezone

from assignments.models import EquipmentAssignment
from settlement.models import (
    SettlementCohort,
    SettlementPreviewApplication,
    SettlementPreviewRun,
    SettlementRevision,
    SettlementSource,
)
from shifts.models import WatchPeriod, WatchPeriodBrigadePhaseVersion
from users.models import (
    Employee,
    EmployeeAccess,
    Role,
    WatchComposition,
    WorkSchedule,
)

from . import employee_watch_profile_changes as service
from .employee_watch_profile_changes import (
    EmployeeWatchProfileChangeError,
    ResolvedEmployeeWatchProfile,
    apply_employee_watch_profile_change,
    create_employee_watch_profile_change_draft,
    resolve_employee_watch_profile,
)
from .models import ArrivalRosterRoutingBatch, EmployeeWatchProfileChange


class EmployeeWatchProfileChangeServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.timekeeper_role, _ = Role.objects.get_or_create(
            code='timekeeper',
            defaults={'name': 'Табельщик', 'is_active': True},
        )
        cls.other_role, _ = Role.objects.get_or_create(
            code='profile-service-other-role',
            defaults={'name': 'Другая роль', 'is_active': True},
        )
        cls.actor = Employee.objects.create(
            full_name='Табельщик проверки профиля',
            phone='+7 900 111-22-33',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.access = EmployeeAccess.objects.create(
            employee=cls.actor,
            role=cls.timekeeper_role,
            access_code='profile-service-timekeeper',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.second_actor = Employee.objects.create(
            full_name='Второй табельщик проверки профиля',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.second_access = EmployeeAccess.objects.create(
            employee=cls.second_actor,
            role=cls.timekeeper_role,
            access_code='profile-service-timekeeper-2',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.old_schedule = WorkSchedule.objects.create(
            code='profile-service-old',
            name='Прежний график сервиса профиля',
            brigade_count=2,
            is_active=True,
        )
        cls.new_schedule = WorkSchedule.objects.create(
            code='profile-service-new',
            name='Новый график сервиса профиля',
            brigade_count=4,
            is_active=True,
        )
        cls.no_brigade_schedule = WorkSchedule.objects.create(
            code='profile-service-no-brigade',
            name='График без бригад сервиса профиля',
            brigade_count=0,
            is_active=True,
        )
        cls.old_composition = WatchComposition.objects.create(
            code='profile-service-old-composition',
            name='Прежний состав сервиса профиля',
            is_active=True,
        )
        cls.new_composition = WatchComposition.objects.create(
            code='profile-service-new-composition',
            name='Новый состав сервиса профиля',
            is_active=True,
        )
        cls.employee = Employee.objects.create(
            full_name='Сотрудник проверки профиля',
            phone='+7 900 444-55-66',
            status=Employee.Status.ACTIVE,
            is_active=True,
            work_schedule=cls.old_schedule,
            brigade_number=1,
            watch_composition=cls.old_composition,
        )
        today = timezone.localdate()
        cls.period = WatchPeriod.objects.create(
            name='Будущий период сервиса профиля',
            watch_composition=cls.new_composition,
            starts_on=today + timedelta(days=30),
            ends_on=today + timedelta(days=59),
            is_active=True,
        )

    def _force_legacy_watch_profile_drift_for_test(
        self,
        employee,
        *,
        work_schedule_id=...,
        brigade_number=...,
        watch_composition_id=...,
        rotation=...,
    ):
        # Test-only corruption: imitates historical/pre-guard or external DB drift.
        with connection.cursor() as cursor:
            if work_schedule_id is not ...:
                cursor.execute(
                    'UPDATE users_employee SET work_schedule_id = %s WHERE id = %s',
                    [work_schedule_id, employee.pk],
                )
            if brigade_number is not ...:
                cursor.execute(
                    'UPDATE users_employee SET brigade_number = %s WHERE id = %s',
                    [brigade_number, employee.pk],
                )
            if watch_composition_id is not ...:
                cursor.execute(
                    'UPDATE users_employee SET watch_composition_id = %s WHERE id = %s',
                    [watch_composition_id, employee.pk],
                )
            if rotation is not ...:
                cursor.execute(
                    'UPDATE users_employee SET rotation = %s WHERE id = %s',
                    [rotation, employee.pk],
                )
        employee.refresh_from_db()

    def _kwargs(self, **overrides):
        values = {
            'employee_id': self.employee.pk,
            'effective_watch_period_id': self.period.pk,
            'new_work_schedule_id': self.new_schedule.pk,
            'new_brigade_number': 2,
            'new_watch_composition_id': self.new_composition.pk,
            'basis_kind': EmployeeWatchProfileChange.BasisKind.EMPLOYEE_APPLICATION,
            'basis_number': '  Заявление   № 15  ',
            'basis_date': timezone.localdate(),
            'basis': '  Прошу   изменить график работы.  ',
            'actor_access_id': self.access.pk,
        }
        values.update(overrides)
        return values

    def _assert_error(self, code, **overrides):
        with self.assertRaises(EmployeeWatchProfileChangeError) as caught:
            create_employee_watch_profile_change_draft(**self._kwargs(**overrides))
        self.assertEqual(caught.exception.code, code)

    def _assert_apply_error(self, code, change, *, actor_access=None):
        with self.assertRaises(EmployeeWatchProfileChangeError) as caught:
            apply_employee_watch_profile_change(
                change_id=change.pk,
                actor_access_id=(actor_access or self.access).pk,
            )
        self.assertEqual(caught.exception.code, code)

    def _assert_resolve_error(self, code, *, employee_id=None, period_id=None):
        with self.assertRaises(EmployeeWatchProfileChangeError) as caught:
            resolve_employee_watch_profile(
                employee_id=employee_id or self.employee.pk,
                watch_period_id=period_id or self.period.pk,
            )
        self.assertEqual(caught.exception.code, code)

    @staticmethod
    def _trusted_insert(row):
        models.QuerySet.bulk_create(
            EmployeeWatchProfileChange._base_manager.all(),
            [row],
        )
        return row

    def _historical_change(
        self,
        *,
        period,
        version_number=1,
        status=EmployeeWatchProfileChange.Status.APPLIED,
        old_schedule=None,
        old_brigade=1,
        old_composition=None,
        new_schedule=None,
        new_brigade=2,
        new_composition=None,
        actor_access=None,
    ):
        old_schedule = old_schedule or self.old_schedule
        old_composition = old_composition or self.old_composition
        new_schedule = new_schedule or self.new_schedule
        new_composition = new_composition or period.watch_composition
        actor_access = actor_access or self.access
        basis_date = min(timezone.localdate(), period.starts_on)
        old_profile = (
            old_schedule.pk if old_schedule else None,
            old_brigade,
            old_composition.pk if old_composition else None,
        )
        new_profile = (
            new_schedule.pk,
            new_brigade,
            new_composition.pk,
        )
        snapshot = service._build_source_snapshot(
            employee=self.employee,
            watch_period=period,
            old_profile=old_profile,
            new_profile=new_profile,
            basis_kind=EmployeeWatchProfileChange.BasisKind.OFFICIAL_ORDER,
            basis_number=f'ПРИКАЗ-{period.pk}-{version_number}',
            basis_date=basis_date,
            basis='Официальное решение для проверки истории.',
        )
        now = timezone.now()
        audit = {
            'applied_by_access': None,
            'applied_at': None,
            'superseded_by_access': None,
            'superseded_at': None,
            'cancelled_by_access': None,
            'cancelled_at': None,
        }
        if status == EmployeeWatchProfileChange.Status.APPLIED:
            audit.update(applied_by_access=actor_access, applied_at=now)
        elif status == EmployeeWatchProfileChange.Status.SUPERSEDED:
            audit.update(
                applied_by_access=actor_access,
                applied_at=now - timedelta(days=1),
                superseded_by_access=actor_access,
                superseded_at=now,
            )
        elif status == EmployeeWatchProfileChange.Status.CANCELLED:
            audit.update(cancelled_by_access=actor_access, cancelled_at=now)
        row = EmployeeWatchProfileChange(
            employee=self.employee,
            effective_watch_period=period,
            effective_on=period.starts_on,
            version_number=version_number,
            old_work_schedule=old_schedule,
            old_brigade_number=old_brigade,
            old_watch_composition=old_composition,
            new_work_schedule=new_schedule,
            new_brigade_number=new_brigade,
            new_watch_composition=new_composition,
            basis_kind=EmployeeWatchProfileChange.BasisKind.OFFICIAL_ORDER,
            basis_number=f'ПРИКАЗ-{period.pk}-{version_number}',
            basis_date=basis_date,
            basis='Официальное решение для проверки истории.',
            source_snapshot=snapshot,
            source_fingerprint=service._canonical_fingerprint(snapshot),
            created_by_access=actor_access,
            status=status,
            **audit,
        )
        return self._trusted_insert(row)

    def _period(self, *, days, composition=None, name=None):
        composition = composition or self.old_composition
        return WatchPeriod.objects.create(
            name=name or f'Период проверки +{days}',
            watch_composition=composition,
            starts_on=timezone.localdate() + timedelta(days=days),
            ends_on=timezone.localdate() + timedelta(days=days + 29),
            is_active=True,
        )

    def _settlement_preview(
        self,
        *,
        period,
        version=1,
        confirmed=False,
        application_shift=None,
    ):
        suffix = f'{period.pk}-{version}'
        now = timezone.now()
        source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.SYSTEM,
            title=f'Источник проверки применения {suffix}',
            status=SettlementSource.Status.CANDIDATE,
        )
        source.status = SettlementSource.Status.CONFIRMED
        source.confirmed_at = now
        source.confirmed_by_label = 'Серверная проверка'
        source.save()
        revision = SettlementRevision.objects.create(
            code=f'profile-apply-{suffix}',
            source=source,
            status=SettlementRevision.Status.DRAFT,
            reason='Проверка authoritative применения расселения.',
        )
        revision.status = SettlementRevision.Status.CONFIRMED
        revision.effective_at = now
        revision.confirmed_at = now
        revision.confirmed_by_label = 'Серверная проверка'
        revision.save()
        cohort = SettlementCohort.objects.create(
            watch_composition=period.watch_composition,
            watch_period=period,
            version=1,
            status=SettlementCohort.Status.DRAFT,
            source_revision=revision,
            source_type='service_test',
            source_id=f'profile-apply-{suffix}',
            source_snapshot={'schema': 1, 'period_id': period.pk},
            input_fingerprint='a' * 64,
            created_by=self.actor,
        )
        cohort.status = SettlementCohort.Status.APPROVED
        cohort.approved_by = self.actor
        cohort.approved_at = now
        cohort.save()
        run = SettlementPreviewRun.objects.create(
            cohort=cohort,
            watch_period=period,
            watch_composition=period.watch_composition,
            version=version,
            status=SettlementPreviewRun.Status.DRAFT,
            resolver_fingerprint='b' * 64,
            result_fingerprint='c' * 64,
            requires_shift_split=True,
            source_snapshot={'schema': 1, 'period_id': period.pk},
            created_by_access=self.access,
        )
        if confirmed or application_shift is not None:
            run.status = SettlementPreviewRun.Status.CONFIRMED
            run.confirmed_by_access = self.access
            run.confirmed_at = now
            run.revision += 1
            run.save()
        application = None
        if application_shift is not None:
            application = SettlementPreviewApplication.objects.create(
                preview_run=run,
                work_shift=application_shift,
                legacy_whole_run=False,
                watch_period=period,
                cohort=cohort,
                applied_by_access=self.access,
                resolver_fingerprint=run.resolver_fingerprint,
                normalized_fingerprint=run.result_fingerprint,
                result_snapshot={
                    'schema': 1,
                    'preview_run_id': run.pk,
                    'work_shift': application_shift,
                },
            )
        return run, application

    def test_creates_exact_normalized_future_draft(self):
        change = create_employee_watch_profile_change_draft(**self._kwargs())

        self.assertEqual(change.employee_id, self.employee.pk)
        self.assertEqual(change.effective_watch_period_id, self.period.pk)
        self.assertEqual(change.effective_on, self.period.starts_on)
        self.assertEqual(change.version_number, 1)
        self.assertEqual(change.status, EmployeeWatchProfileChange.Status.DRAFT)
        self.assertEqual(change.created_by_access_id, self.access.pk)
        self.assertEqual(change.basis_number, 'Заявление № 15')
        self.assertEqual(change.basis, 'Прошу изменить график работы.')
        self.assertIsNone(change.supersedes_id)
        self.assertIsNone(change.applied_by_access_id)
        self.assertIsNone(change.cancelled_by_access_id)

    def test_access_must_be_exact_active_unblocked_timekeeper(self):
        self._assert_error(
            service.ERROR_ACCESS_NOT_FOUND,
            actor_access_id=999999,
        )

        inactive = EmployeeAccess.objects.create(
            employee=self.actor,
            role=self.timekeeper_role,
            access_code='profile-service-inactive',
            status=EmployeeAccess.Status.DEACTIVATED,
            is_active=False,
        )
        self._assert_error(
            service.ERROR_ACCESS_INACTIVE,
            actor_access_id=inactive.pk,
        )

        blocked = EmployeeAccess.objects.create(
            employee=self.actor,
            role=self.timekeeper_role,
            access_code='profile-service-blocked',
            status=EmployeeAccess.Status.BLOCKED,
            is_active=True,
        )
        self._assert_error(
            service.ERROR_ACCESS_BLOCKED,
            actor_access_id=blocked.pk,
        )

        wrong = EmployeeAccess.objects.create(
            employee=self.actor,
            role=self.other_role,
            access_code='profile-service-wrong-role',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self._assert_error(
            service.ERROR_ACCESS_WRONG_ROLE,
            actor_access_id=wrong.pk,
        )
        self.assertFalse(EmployeeWatchProfileChange._base_manager.exists())

    def test_inactive_actor_and_target_are_rejected(self):
        inactive_actor = Employee.objects.create(
            full_name='Неактивный табельщик',
            status=Employee.Status.DEACTIVATED,
            is_active=False,
        )
        inactive_access = EmployeeAccess.objects.create(
            employee=inactive_actor,
            role=self.timekeeper_role,
            access_code='profile-service-inactive-actor',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self._assert_error(
            service.ERROR_ACTOR_EMPLOYEE_INACTIVE,
            actor_access_id=inactive_access.pk,
        )

        inactive_target = Employee.objects.create(
            full_name='Неактивный сотрудник',
            status=Employee.Status.ARCHIVED,
            is_active=False,
        )
        self._assert_error(
            service.ERROR_EMPLOYEE_INACTIVE,
            employee_id=inactive_target.pk,
        )

    def test_only_strictly_future_watch_period_is_allowed(self):
        today = timezone.localdate()
        current = WatchPeriod.objects.create(
            name='Период, начавшийся сегодня',
            watch_composition=self.new_composition,
            starts_on=today,
            ends_on=today + timedelta(days=29),
        )
        past = WatchPeriod.objects.create(
            name='Прошедший период',
            watch_composition=self.new_composition,
            starts_on=today - timedelta(days=30),
            ends_on=today - timedelta(days=1),
        )
        for period in (current, past):
            with self.subTest(period=period.pk):
                self._assert_error(
                    service.ERROR_WATCH_PERIOD_NOT_FUTURE,
                    effective_watch_period_id=period.pk,
                )

    def test_composition_must_be_active_and_equal_period_composition(self):
        self._assert_error(
            service.ERROR_WATCH_COMPOSITION_MISMATCH,
            new_watch_composition_id=self.old_composition.pk,
        )
        inactive = WatchComposition.objects.create(
            code='profile-service-inactive-composition',
            name='Неактивный состав сервиса профиля',
            is_active=False,
        )
        period = self._period(days=80, composition=inactive)
        self._assert_error(
            service.ERROR_WATCH_COMPOSITION_INACTIVE,
            effective_watch_period_id=period.pk,
            new_watch_composition_id=inactive.pk,
        )

    def test_schedule_must_be_active(self):
        inactive = WorkSchedule.objects.create(
            code='profile-service-inactive-schedule',
            name='Неактивный график сервиса профиля',
            brigade_count=2,
            is_active=False,
        )
        self._assert_error(
            service.ERROR_WORK_SCHEDULE_INACTIVE,
            new_work_schedule_id=inactive.pk,
        )

    def test_brigade_policy_uses_structured_brigade_count(self):
        self._assert_error(
            service.ERROR_BRIGADE_REQUIRED,
            new_brigade_number=None,
        )
        self._assert_error(
            service.ERROR_BRIGADE_OUT_OF_RANGE,
            new_brigade_number=5,
        )
        self._assert_error(
            service.ERROR_BRIGADE_NOT_ALLOWED,
            new_work_schedule_id=self.no_brigade_schedule.pk,
            new_brigade_number=1,
        )
        change = create_employee_watch_profile_change_draft(**self._kwargs(
            new_work_schedule_id=self.no_brigade_schedule.pk,
            new_brigade_number=None,
        ))
        self.assertIsNone(change.new_brigade_number)

    def test_old_profile_comes_from_employee_legacy_baseline(self):
        change = create_employee_watch_profile_change_draft(**self._kwargs())
        self.assertEqual(change.old_work_schedule_id, self.old_schedule.pk)
        self.assertEqual(change.old_brigade_number, 1)
        self.assertEqual(change.old_watch_composition_id, self.old_composition.pk)

    def test_old_profile_comes_from_latest_earlier_applied_change(self):
        earlier = self._period(days=10, composition=self.old_composition)
        middle_schedule = WorkSchedule.objects.create(
            code='profile-service-middle',
            name='Промежуточный график сервиса профиля',
            brigade_count=2,
            is_active=True,
        )
        self._historical_change(
            period=earlier,
            new_schedule=middle_schedule,
            new_brigade=2,
            new_composition=self.old_composition,
        )

        change = create_employee_watch_profile_change_draft(**self._kwargs())
        self.assertEqual(change.old_work_schedule_id, middle_schedule.pk)
        self.assertEqual(change.old_brigade_number, 2)
        self.assertEqual(change.old_watch_composition_id, self.old_composition.pk)

    def test_target_period_correction_preserves_original_old_values(self):
        current = self._historical_change(
            period=self.period,
            new_schedule=self.no_brigade_schedule,
            new_brigade=None,
            new_composition=self.new_composition,
        )

        change = create_employee_watch_profile_change_draft(**self._kwargs())
        self.assertEqual(change.version_number, current.version_number + 1)
        self.assertEqual(change.old_work_schedule_id, self.old_schedule.pk)
        self.assertEqual(change.old_brigade_number, 1)
        self.assertEqual(change.old_watch_composition_id, self.old_composition.pk)
        self.assertIsNone(change.supersedes_id)

    def test_draft_cancelled_and_superseded_do_not_change_effective_profile(self):
        periods = [
            self._period(days=5, name='Черновой ранний период'),
            self._period(days=10, name='Отменённый ранний период'),
            self._period(days=15, name='Заменённый ранний период'),
        ]
        statuses = (
            EmployeeWatchProfileChange.Status.DRAFT,
            EmployeeWatchProfileChange.Status.CANCELLED,
            EmployeeWatchProfileChange.Status.SUPERSEDED,
        )
        for period, status in zip(periods, statuses):
            self._historical_change(
                period=period,
                status=status,
                new_composition=self.old_composition,
            )

        change = create_employee_watch_profile_change_draft(**self._kwargs())
        self.assertEqual(change.old_work_schedule_id, self.old_schedule.pk)
        self.assertEqual(change.old_brigade_number, 1)
        self.assertEqual(change.old_watch_composition_id, self.old_composition.pk)

    def test_noop_and_same_target_applied_profile_create_nothing(self):
        matching_period = self._period(days=70, composition=self.old_composition)
        self._assert_error(
            service.ERROR_NO_CHANGE,
            effective_watch_period_id=matching_period.pk,
            new_work_schedule_id=self.old_schedule.pk,
            new_brigade_number=1,
            new_watch_composition_id=self.old_composition.pk,
        )
        self._historical_change(
            period=self.period,
            new_schedule=self.new_schedule,
            new_brigade=2,
            new_composition=self.new_composition,
        )
        self._assert_error(service.ERROR_NO_CHANGE)
        self.assertEqual(EmployeeWatchProfileChange._base_manager.count(), 1)

    def test_basis_is_closed_required_and_not_future_dated(self):
        self._assert_error(
            service.ERROR_INVALID_BASIS,
            basis_kind='free_text',
        )
        self._assert_error(service.ERROR_INVALID_BASIS, basis_number='  ')
        self._assert_error(service.ERROR_INVALID_BASIS, basis_number='X' * 129)
        self._assert_error(service.ERROR_INVALID_BASIS, basis='\n\t')
        self._assert_error(
            service.ERROR_BASIS_DATE_IN_FUTURE,
            basis_date=timezone.localdate() + timedelta(days=1),
        )

    def test_snapshot_is_server_built_canonical_and_has_no_personal_or_access_data(self):
        change = create_employee_watch_profile_change_draft(**self._kwargs())
        serialized = json.dumps(change.source_snapshot, ensure_ascii=False)

        self.assertEqual(
            set(change.source_snapshot),
            {'schema', 'version', 'employee_id', 'watch_period', 'old_profile', 'new_profile', 'basis'},
        )
        self.assertNotIn(self.employee.full_name, serialized)
        self.assertNotIn(self.employee.phone, serialized)
        self.assertNotIn(self.actor.full_name, serialized)
        self.assertNotIn('access', serialized.casefold())
        canonical = json.dumps(
            change.source_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
        self.assertEqual(
            change.source_fingerprint,
            hashlib.sha256(canonical).hexdigest(),
        )

    def test_identical_repeat_by_same_actor_is_idempotent(self):
        first = create_employee_watch_profile_change_draft(**self._kwargs())
        created_at = first.created_at
        second = create_employee_watch_profile_change_draft(**self._kwargs())

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.created_at, created_at)
        self.assertEqual(EmployeeWatchProfileChange._base_manager.count(), 1)

    def test_other_actor_basis_or_profile_gets_next_version(self):
        first = create_employee_watch_profile_change_draft(**self._kwargs())
        second = create_employee_watch_profile_change_draft(**self._kwargs(
            actor_access_id=self.second_access.pk,
        ))
        third = create_employee_watch_profile_change_draft(**self._kwargs(
            basis_number='Другое заявление',
        ))
        fourth = create_employee_watch_profile_change_draft(**self._kwargs(
            new_brigade_number=3,
        ))

        self.assertEqual(
            [first.version_number, second.version_number, third.version_number, fourth.version_number],
            [1, 2, 3, 4],
        )

    def test_service_uses_only_private_trusted_insert(self):
        with (
            patch.object(
                EmployeeWatchProfileChange.objects,
                'create',
                side_effect=AssertionError('public create must not be used'),
            ) as public_create,
            patch.object(
                EmployeeWatchProfileChange.objects,
                'bulk_create',
                side_effect=AssertionError('public bulk_create must not be used'),
            ) as public_bulk_create,
            patch.object(
                service,
                '_trusted_insert_change',
                wraps=service._trusted_insert_change,
            ) as trusted_insert,
        ):
            change = create_employee_watch_profile_change_draft(**self._kwargs())

        self.assertIsNotNone(change.pk)
        public_create.assert_not_called()
        public_bulk_create.assert_not_called()
        trusted_insert.assert_called_once()

    def test_full_clean_failure_rolls_back_without_partial_change(self):
        with patch.object(
            EmployeeWatchProfileChange,
            'full_clean',
            side_effect=ValidationError('Искусственная ошибка проверки.'),
        ):
            self._assert_error(service.ERROR_PROFILE_INCONSISTENT)
        self.assertFalse(EmployeeWatchProfileChange._base_manager.exists())

    def test_command_does_not_mutate_employee_or_downstream_modules(self):
        self.employee.refresh_from_db()
        employee_before = (
            self.employee.work_schedule_id,
            self.employee.brigade_number,
            self.employee.watch_composition_id,
            self.employee.updated_at,
        )
        counts_before = {
            'periods': WatchPeriod.objects.count(),
            'calendars': WatchPeriodBrigadePhaseVersion._base_manager.count(),
            'routing': ArrivalRosterRoutingBatch._base_manager.count(),
            'assignments': EquipmentAssignment._base_manager.count(),
            'cohorts': SettlementCohort._base_manager.count(),
        }

        create_employee_watch_profile_change_draft(**self._kwargs())

        self.employee.refresh_from_db()
        self.assertEqual(
            (
                self.employee.work_schedule_id,
                self.employee.brigade_number,
                self.employee.watch_composition_id,
                self.employee.updated_at,
            ),
            employee_before,
        )
        self.assertEqual(
            {
                'periods': WatchPeriod.objects.count(),
                'calendars': WatchPeriodBrigadePhaseVersion._base_manager.count(),
                'routing': ArrivalRosterRoutingBatch._base_manager.count(),
                'assignments': EquipmentAssignment._base_manager.count(),
                'cohorts': SettlementCohort._base_manager.count(),
            },
            counts_before,
        )

    def test_lock_order_is_actor_access_target_period_profile_and_history(self):
        lock_order = []
        original = QuerySet.select_for_update

        def traced(queryset, *args, **kwargs):
            lock_order.append(queryset.model.__name__)
            return original(queryset, *args, **kwargs)

        with patch.object(QuerySet, 'select_for_update', new=traced):
            create_employee_watch_profile_change_draft(**self._kwargs())

        self.assertEqual(
            lock_order,
            [
                'Employee',
                'EmployeeAccess',
                'Employee',
                'WatchPeriod',
                'WorkSchedule',
                'WatchComposition',
                'EmployeeWatchProfileChange',
                'EmployeeWatchProfileChange',
            ],
        )

    def test_same_actor_and_target_employee_is_locked_only_once(self):
        actor = Employee.objects.create(
            full_name='Табельщик с исходным профилем',
            status=Employee.Status.ACTIVE,
            is_active=True,
            work_schedule=self.old_schedule,
            brigade_number=1,
            watch_composition=self.old_composition,
        )
        access = EmployeeAccess.objects.create(
            employee=actor,
            role=self.timekeeper_role,
            access_code='profile-service-same-actor-target',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        lock_order = []
        original = QuerySet.select_for_update

        def traced(queryset, *args, **kwargs):
            lock_order.append(queryset.model.__name__)
            return original(queryset, *args, **kwargs)

        with patch.object(QuerySet, 'select_for_update', new=traced):
            create_employee_watch_profile_change_draft(**self._kwargs(
                employee_id=actor.pk,
                actor_access_id=access.pk,
            ))

        self.assertEqual(lock_order.count('Employee'), 1)

    def test_corrupt_applied_snapshot_is_rejected_fail_closed(self):
        earlier = self._period(days=10, composition=self.old_composition)
        change = self._historical_change(
            period=earlier,
            new_composition=self.old_composition,
        )
        EmployeeWatchProfileChange._base_manager.filter(pk=change.pk).update(
            source_fingerprint='f' * 64,
        )

        self._assert_error(service.ERROR_PROFILE_INCONSISTENT)
        self.assertEqual(EmployeeWatchProfileChange._base_manager.count(), 1)

    def test_apply_first_draft_sets_exact_actor_and_server_timestamp(self):
        change = create_employee_watch_profile_change_draft(**self._kwargs())
        transition_at = timezone.now() + timedelta(seconds=1)

        with patch.object(service.timezone, 'now', return_value=transition_at):
            applied = apply_employee_watch_profile_change(
                change_id=change.pk,
                actor_access_id=self.second_access.pk,
            )

        self.assertEqual(applied.status, EmployeeWatchProfileChange.Status.APPLIED)
        self.assertEqual(applied.applied_by_access_id, self.second_access.pk)
        self.assertEqual(applied.applied_at, transition_at)
        self.assertIsNone(applied.supersedes_id)
        self.assertEqual(
            EmployeeWatchProfileChange._base_manager.filter(
                employee=self.employee,
                effective_watch_period=self.period,
                status=EmployeeWatchProfileChange.Status.APPLIED,
            ).count(),
            1,
        )

    def test_apply_current_applied_is_idempotent_without_audit_change(self):
        change = create_employee_watch_profile_change_draft(**self._kwargs())
        first = apply_employee_watch_profile_change(
            change_id=change.pk,
            actor_access_id=self.access.pk,
        )
        audit = (
            first.applied_by_access_id,
            first.applied_at,
            first.supersedes_id,
        )

        second = apply_employee_watch_profile_change(
            change_id=change.pk,
            actor_access_id=self.second_access.pk,
        )

        self.assertEqual(second.pk, first.pk)
        self.assertEqual(
            (second.applied_by_access_id, second.applied_at, second.supersedes_id),
            audit,
        )

    def test_superseded_and_cancelled_changes_are_not_reapplied(self):
        cancelled = create_employee_watch_profile_change_draft(**self._kwargs())
        now = timezone.now()
        EmployeeWatchProfileChange._base_manager.filter(pk=cancelled.pk).update(
            status=EmployeeWatchProfileChange.Status.CANCELLED,
            cancelled_by_access_id=self.access.pk,
            cancelled_at=now,
        )
        cancelled.refresh_from_db()
        self._assert_apply_error(service.ERROR_CHANGE_NOT_DRAFT, cancelled)

        other_period = self._period(days=70, composition=self.new_composition)
        superseded = create_employee_watch_profile_change_draft(**self._kwargs(
            effective_watch_period_id=other_period.pk,
        ))
        EmployeeWatchProfileChange._base_manager.filter(pk=superseded.pk).update(
            status=EmployeeWatchProfileChange.Status.SUPERSEDED,
            applied_by_access_id=self.access.pk,
            applied_at=now - timedelta(seconds=1),
            superseded_by_access_id=self.access.pk,
            superseded_at=now,
        )
        superseded.refresh_from_db()
        self._assert_apply_error(service.ERROR_CHANGE_NOT_DRAFT, superseded)

    def test_fresh_correction_supersedes_current_with_one_timestamp(self):
        first = create_employee_watch_profile_change_draft(**self._kwargs())
        first = apply_employee_watch_profile_change(
            change_id=first.pk,
            actor_access_id=self.access.pk,
        )
        correction = create_employee_watch_profile_change_draft(**self._kwargs(
            new_brigade_number=3,
            basis_number='Исправление № 2',
        ))
        self.assertIsNone(correction.supersedes_id)
        transition_at = timezone.now() + timedelta(seconds=1)

        with patch.object(service.timezone, 'now', return_value=transition_at):
            correction = apply_employee_watch_profile_change(
                change_id=correction.pk,
                actor_access_id=self.second_access.pk,
            )

        first.refresh_from_db()
        self.assertEqual(first.status, EmployeeWatchProfileChange.Status.SUPERSEDED)
        self.assertEqual(first.superseded_by_access_id, self.second_access.pk)
        self.assertEqual(first.superseded_at, transition_at)
        self.assertEqual(correction.status, EmployeeWatchProfileChange.Status.APPLIED)
        self.assertEqual(correction.supersedes_id, first.pk)
        self.assertEqual(correction.applied_by_access_id, self.second_access.pk)
        self.assertEqual(correction.applied_at, transition_at)
        self.assertEqual(
            EmployeeWatchProfileChange._base_manager.filter(
                employee=self.employee,
                effective_watch_period=self.period,
                status=EmployeeWatchProfileChange.Status.APPLIED,
            ).count(),
            1,
        )

    def test_competing_draft_becomes_stale_after_other_version_applies(self):
        stale = create_employee_watch_profile_change_draft(**self._kwargs())
        winner = create_employee_watch_profile_change_draft(**self._kwargs(
            new_brigade_number=3,
            basis_number='Конкурирующее заявление',
        ))
        apply_employee_watch_profile_change(
            change_id=winner.pk,
            actor_access_id=self.access.pk,
        )

        self._assert_apply_error(service.ERROR_CHANGE_STALE, stale)
        stale.refresh_from_db()
        self.assertEqual(stale.status, EmployeeWatchProfileChange.Status.DRAFT)
        self.assertIsNone(stale.supersedes_id)

    def test_correction_created_after_current_applied_is_fresh(self):
        current = create_employee_watch_profile_change_draft(**self._kwargs())
        apply_employee_watch_profile_change(
            change_id=current.pk,
            actor_access_id=self.access.pk,
        )
        correction = create_employee_watch_profile_change_draft(**self._kwargs(
            new_brigade_number=4,
            basis_number='Позднее исправление',
        ))

        applied = apply_employee_watch_profile_change(
            change_id=correction.pk,
            actor_access_id=self.access.pk,
        )
        self.assertEqual(applied.status, EmployeeWatchProfileChange.Status.APPLIED)
        self.assertEqual(applied.supersedes_id, current.pk)

    def test_failure_of_second_transition_rolls_back_supersede(self):
        current = create_employee_watch_profile_change_draft(**self._kwargs())
        current = apply_employee_watch_profile_change(
            change_id=current.pk,
            actor_access_id=self.access.pk,
        )
        correction = create_employee_watch_profile_change_draft(**self._kwargs(
            new_brigade_number=3,
            basis_number='Исправление с откатом',
        ))
        original = service._trusted_transition_change
        calls = []

        def fail_second(change, **kwargs):
            calls.append(change.pk)
            if len(calls) == 2:
                raise ValidationError('Искусственная ошибка второго перехода.')
            return original(change, **kwargs)

        with patch.object(
            service,
            '_trusted_transition_change',
            side_effect=fail_second,
        ):
            self._assert_apply_error(service.ERROR_PROFILE_INCONSISTENT, correction)

        current.refresh_from_db()
        correction.refresh_from_db()
        self.assertEqual(current.status, EmployeeWatchProfileChange.Status.APPLIED)
        self.assertIsNone(current.superseded_at)
        self.assertEqual(correction.status, EmployeeWatchProfileChange.Status.DRAFT)
        self.assertIsNone(correction.supersedes_id)

    def test_apply_rejects_damaged_snapshot_and_fingerprint_separately(self):
        damaged_snapshot = create_employee_watch_profile_change_draft(**self._kwargs())
        EmployeeWatchProfileChange._base_manager.filter(
            pk=damaged_snapshot.pk,
        ).update(source_snapshot={'schema': 'tampered'})
        damaged_snapshot.refresh_from_db()
        self._assert_apply_error(service.ERROR_SOURCE_INVALID, damaged_snapshot)

        other_period = self._period(days=70, composition=self.new_composition)
        damaged_fingerprint = create_employee_watch_profile_change_draft(**self._kwargs(
            effective_watch_period_id=other_period.pk,
        ))
        EmployeeWatchProfileChange._base_manager.filter(
            pk=damaged_fingerprint.pk,
        ).update(source_fingerprint='f' * 64)
        damaged_fingerprint.refresh_from_db()
        self._assert_apply_error(
            service.ERROR_SOURCE_FINGERPRINT_INVALID,
            damaged_fingerprint,
        )

    def test_changed_legacy_baseline_makes_draft_stale(self):
        change = create_employee_watch_profile_change_draft(**self._kwargs())
        self._force_legacy_watch_profile_drift_for_test(
            self.employee,
            work_schedule_id=self.no_brigade_schedule.pk,
            brigade_number=None,
        )

        self._assert_apply_error(service.ERROR_CHANGE_STALE, change)
        change.refresh_from_db()
        self.assertEqual(change.status, EmployeeWatchProfileChange.Status.DRAFT)

    def test_changed_baseline_also_stales_explicit_correction(self):
        current = create_employee_watch_profile_change_draft(**self._kwargs())
        apply_employee_watch_profile_change(
            change_id=current.pk,
            actor_access_id=self.access.pk,
        )
        correction = create_employee_watch_profile_change_draft(**self._kwargs(
            new_brigade_number=3,
            basis_number='Исправление после исходного решения',
        ))
        self._force_legacy_watch_profile_drift_for_test(
            self.employee,
            work_schedule_id=self.no_brigade_schedule.pk,
            brigade_number=None,
        )

        self._assert_apply_error(service.ERROR_CHANGE_STALE, correction)
        current.refresh_from_db()
        correction.refresh_from_db()
        self.assertEqual(current.status, EmployeeWatchProfileChange.Status.APPLIED)
        self.assertEqual(correction.status, EmployeeWatchProfileChange.Status.DRAFT)

    def test_apply_revalidates_period_employee_schedule_composition_and_brigade(self):
        current_period = create_employee_watch_profile_change_draft(**self._kwargs())
        with patch.object(
            service.timezone,
            'localdate',
            return_value=self.period.starts_on,
        ):
            self._assert_apply_error(
                service.ERROR_WATCH_PERIOD_NOT_FUTURE,
                current_period,
            )

        inactive_employee_period = self._period(
            days=70,
            composition=self.new_composition,
            name='Период неактивного сотрудника',
        )
        inactive_employee = create_employee_watch_profile_change_draft(**self._kwargs(
            effective_watch_period_id=inactive_employee_period.pk,
        ))
        Employee.objects.filter(pk=self.employee.pk).update(is_active=False)
        self._assert_apply_error(service.ERROR_EMPLOYEE_INACTIVE, inactive_employee)
        Employee.objects.filter(pk=self.employee.pk).update(is_active=True)

        schedule_period = self._period(
            days=100,
            composition=self.new_composition,
            name='Период неактивного графика',
        )
        inactive_schedule = create_employee_watch_profile_change_draft(**self._kwargs(
            effective_watch_period_id=schedule_period.pk,
        ))
        WorkSchedule.objects.filter(pk=self.new_schedule.pk).update(is_active=False)
        self._assert_apply_error(
            service.ERROR_WORK_SCHEDULE_INACTIVE,
            inactive_schedule,
        )
        WorkSchedule.objects.filter(pk=self.new_schedule.pk).update(is_active=True)

        composition_period = self._period(
            days=130,
            composition=self.new_composition,
            name='Период неактивного состава',
        )
        inactive_composition = create_employee_watch_profile_change_draft(**self._kwargs(
            effective_watch_period_id=composition_period.pk,
        ))
        WatchComposition.objects.filter(pk=self.new_composition.pk).update(
            is_active=False,
        )
        self._assert_apply_error(
            service.ERROR_WATCH_COMPOSITION_INACTIVE,
            inactive_composition,
        )
        WatchComposition.objects.filter(pk=self.new_composition.pk).update(
            is_active=True,
        )

        brigade_period = self._period(
            days=160,
            composition=self.new_composition,
            name='Период изменённой бригадной политики',
        )
        changed_brigade_policy = create_employee_watch_profile_change_draft(
            **self._kwargs(effective_watch_period_id=brigade_period.pk)
        )
        WorkSchedule.objects.filter(pk=self.new_schedule.pk).update(brigade_count=1)
        self._assert_apply_error(
            service.ERROR_BRIGADE_OUT_OF_RANGE,
            changed_brigade_policy,
        )

    def test_draft_and_confirmed_preview_without_application_do_not_block(self):
        draft_period = self._period(
            days=70,
            composition=self.new_composition,
            name='Период с черновиком расселения',
        )
        draft_change = create_employee_watch_profile_change_draft(**self._kwargs(
            effective_watch_period_id=draft_period.pk,
        ))
        self._settlement_preview(period=draft_period, confirmed=False)
        applied = apply_employee_watch_profile_change(
            change_id=draft_change.pk,
            actor_access_id=self.access.pk,
        )
        self.assertEqual(applied.status, EmployeeWatchProfileChange.Status.APPLIED)

        confirmed_period = self._period(
            days=100,
            composition=self.new_composition,
            name='Период с подтверждённым планом расселения',
        )
        confirmed_change = create_employee_watch_profile_change_draft(**self._kwargs(
            effective_watch_period_id=confirmed_period.pk,
            new_brigade_number=3,
        ))
        self._settlement_preview(period=confirmed_period, confirmed=True)
        applied = apply_employee_watch_profile_change(
            change_id=confirmed_change.pk,
            actor_access_id=self.access.pk,
        )
        self.assertEqual(applied.status, EmployeeWatchProfileChange.Status.APPLIED)

    def test_authoritative_day_or_night_application_blocks_profile_apply(self):
        for offset, shift in (
            (70, 'day'),
            (100, 'night'),
        ):
            with self.subTest(shift=shift):
                period = self._period(
                    days=offset,
                    composition=self.new_composition,
                    name=f'Период применённой смены {shift}',
                )
                change = create_employee_watch_profile_change_draft(**self._kwargs(
                    effective_watch_period_id=period.pk,
                ))
                _run, application = self._settlement_preview(
                    period=period,
                    confirmed=True,
                    application_shift=shift,
                )
                self.assertEqual(application.watch_period_id, period.pk)

                self._assert_apply_error(
                    service.ERROR_WATCH_PERIOD_ALREADY_SETTLED,
                    change,
                )
                change.refresh_from_db()
                self.assertEqual(
                    change.status,
                    EmployeeWatchProfileChange.Status.DRAFT,
                )

    def test_apply_does_not_mutate_employee_or_downstream_entities(self):
        change = create_employee_watch_profile_change_draft(**self._kwargs())
        self.employee.refresh_from_db()
        employee_before = (
            self.employee.work_schedule_id,
            self.employee.brigade_number,
            self.employee.watch_composition_id,
            self.employee.rotation,
            self.employee.updated_at,
        )
        counts_before = {
            'periods': WatchPeriod.objects.count(),
            'calendars': WatchPeriodBrigadePhaseVersion._base_manager.count(),
            'routing': ArrivalRosterRoutingBatch._base_manager.count(),
            'assignments': EquipmentAssignment._base_manager.count(),
            'cohorts': SettlementCohort._base_manager.count(),
            'previews': SettlementPreviewRun._base_manager.count(),
            'applications': SettlementPreviewApplication._base_manager.count(),
        }

        apply_employee_watch_profile_change(
            change_id=change.pk,
            actor_access_id=self.access.pk,
        )

        self.employee.refresh_from_db()
        self.assertEqual(
            (
                self.employee.work_schedule_id,
                self.employee.brigade_number,
                self.employee.watch_composition_id,
                self.employee.rotation,
                self.employee.updated_at,
            ),
            employee_before,
        )
        self.assertEqual(
            {
                'periods': WatchPeriod.objects.count(),
                'calendars': WatchPeriodBrigadePhaseVersion._base_manager.count(),
                'routing': ArrivalRosterRoutingBatch._base_manager.count(),
                'assignments': EquipmentAssignment._base_manager.count(),
                'cohorts': SettlementCohort._base_manager.count(),
                'previews': SettlementPreviewRun._base_manager.count(),
                'applications': SettlementPreviewApplication._base_manager.count(),
            },
            counts_before,
        )

    def test_apply_uses_exact_access_and_deterministic_lock_order(self):
        change = create_employee_watch_profile_change_draft(**self._kwargs())
        wrong_access = EmployeeAccess.objects.create(
            employee=self.actor,
            role=self.other_role,
            access_code='profile-apply-wrong-role',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self._assert_apply_error(
            service.ERROR_ACCESS_WRONG_ROLE,
            change,
            actor_access=wrong_access,
        )

        lock_order = []
        original = QuerySet.select_for_update

        def traced(queryset, *args, **kwargs):
            lock_order.append(queryset.model.__name__)
            return original(queryset, *args, **kwargs)

        with patch.object(QuerySet, 'select_for_update', new=traced):
            apply_employee_watch_profile_change(
                change_id=change.pk,
                actor_access_id=self.access.pk,
            )

        self.assertEqual(
            lock_order,
            [
                'Employee',
                'EmployeeAccess',
                'Employee',
                'WatchPeriod',
                'WorkSchedule',
                'WatchComposition',
                'EmployeeWatchProfileChange',
                'EmployeeWatchProfileChange',
                'SettlementPreviewApplication',
            ],
        )

    def test_apply_missing_change_is_controlled(self):
        with self.assertRaises(EmployeeWatchProfileChangeError) as caught:
            apply_employee_watch_profile_change(
                change_id=999999,
                actor_access_id=self.access.pk,
            )
        self.assertEqual(caught.exception.code, service.ERROR_CHANGE_NOT_FOUND)

    def test_resolver_returns_frozen_legacy_baseline_without_guesses(self):
        resolved = resolve_employee_watch_profile(
            employee_id=self.employee.pk,
            watch_period_id=self.period.pk,
        )

        self.assertIsInstance(resolved, ResolvedEmployeeWatchProfile)
        self.assertEqual(resolved.employee_id, self.employee.pk)
        self.assertEqual(resolved.watch_period_id, self.period.pk)
        self.assertEqual(resolved.effective_on, self.period.starts_on)
        self.assertEqual(resolved.work_schedule_id, self.old_schedule.pk)
        self.assertEqual(resolved.brigade_number, 1)
        self.assertEqual(resolved.watch_composition_id, self.old_composition.pk)
        self.assertEqual(
            resolved.source_kind,
            service.SOURCE_KIND_LEGACY_BASELINE,
        )
        self.assertIsNone(resolved.change_id)
        self.assertIsNone(resolved.change_version_number)
        self.assertIsNone(resolved.source_fingerprint)
        self.assertRegex(resolved.profile_fingerprint, r'^[0-9a-f]{64}$')
        self.assertFalse(hasattr(resolved, '__dict__'))
        with self.assertRaises(FrozenInstanceError):
            resolved.brigade_number = 2

        employee = Employee.objects.create(
            full_name='Сотрудник с неполным исходным профилем',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        nullable = resolve_employee_watch_profile(
            employee_id=employee.pk,
            watch_period_id=self.period.pk,
        )
        self.assertIsNone(nullable.work_schedule_id)
        self.assertIsNone(nullable.brigade_number)
        self.assertIsNone(nullable.watch_composition_id)

    def test_resolver_replays_applied_history_at_period_boundaries(self):
        first_period = self._period(days=10, composition=self.new_composition)
        second_period = self._period(days=20, composition=self.old_composition)
        before = self._period(days=5)
        between = self._period(days=15)
        after = self._period(days=25)
        first = self._historical_change(period=first_period)
        second = self._historical_change(
            period=second_period,
            old_schedule=self.new_schedule,
            old_brigade=2,
            old_composition=self.new_composition,
            new_schedule=self.no_brigade_schedule,
            new_brigade=None,
            new_composition=self.old_composition,
        )

        before_result = resolve_employee_watch_profile(
            employee_id=self.employee.pk,
            watch_period_id=before.pk,
        )
        first_result = resolve_employee_watch_profile(
            employee_id=self.employee.pk,
            watch_period_id=first_period.pk,
        )
        between_result = resolve_employee_watch_profile(
            employee_id=self.employee.pk,
            watch_period_id=between.pk,
        )
        second_result = resolve_employee_watch_profile(
            employee_id=self.employee.pk,
            watch_period_id=second_period.pk,
        )
        after_result = resolve_employee_watch_profile(
            employee_id=self.employee.pk,
            watch_period_id=after.pk,
        )

        self.assertEqual(before_result.source_kind, 'legacy_baseline')
        self.assertEqual(first_result.change_id, first.pk)
        self.assertEqual(between_result.change_id, first.pk)
        self.assertEqual(second_result.change_id, second.pk)
        self.assertEqual(after_result.change_id, second.pk)
        self.assertEqual(after_result.work_schedule_id, self.no_brigade_schedule.pk)
        self.assertIsNone(after_result.brigade_number)
        self.assertEqual(after_result.watch_composition_id, self.old_composition.pk)

    def test_resolver_ignores_non_applied_rows_and_uses_current_correction(self):
        period = self._period(days=10, composition=self.new_composition)
        superseded = self._historical_change(
            period=period,
            version_number=1,
            status=EmployeeWatchProfileChange.Status.SUPERSEDED,
        )
        correction = self._historical_change(
            period=period,
            version_number=2,
            new_schedule=self.no_brigade_schedule,
            new_brigade=None,
        )
        models.QuerySet.update(
            EmployeeWatchProfileChange._base_manager.filter(pk=correction.pk),
            supersedes_id=superseded.pk,
        )
        self._historical_change(
            period=period,
            version_number=3,
            status=EmployeeWatchProfileChange.Status.DRAFT,
        )
        self._historical_change(
            period=period,
            version_number=4,
            status=EmployeeWatchProfileChange.Status.CANCELLED,
        )

        resolved = resolve_employee_watch_profile(
            employee_id=self.employee.pk,
            watch_period_id=period.pk,
        )

        self.assertEqual(resolved.source_kind, 'applied_change')
        self.assertEqual(resolved.change_id, correction.pk)
        self.assertEqual(resolved.change_version_number, 2)
        self.assertEqual(resolved.work_schedule_id, self.no_brigade_schedule.pk)

    def test_resolver_fails_closed_on_history_gap_or_same_date_ambiguity(self):
        first_period = self._period(days=10, composition=self.new_composition)
        second_period = self._period(days=20, composition=self.old_composition)
        self._historical_change(period=first_period)
        self._historical_change(period=second_period)

        self._assert_resolve_error(
            service.ERROR_PROFILE_INCONSISTENT,
            period_id=second_period.pk,
        )

    def test_resolver_fails_closed_on_two_applied_changes_for_same_date(self):
        first_period = self._period(days=10, composition=self.new_composition)
        second_period = WatchPeriod.objects.create(
            name='Параллельный период той же даты',
            watch_composition=self.old_composition,
            starts_on=first_period.starts_on,
            ends_on=first_period.ends_on,
            is_active=True,
        )
        self._historical_change(period=first_period)
        self._historical_change(
            period=second_period,
            old_schedule=self.new_schedule,
            old_brigade=2,
            old_composition=self.new_composition,
            new_schedule=self.no_brigade_schedule,
            new_brigade=None,
            new_composition=self.old_composition,
        )

        self._assert_resolve_error(
            service.ERROR_PROFILE_INCONSISTENT,
            period_id=first_period.pk,
        )

    def test_resolver_rejects_damaged_snapshot_and_fingerprint_separately(self):
        period = self._period(days=10, composition=self.new_composition)
        change = self._historical_change(period=period)
        models.QuerySet.update(
            EmployeeWatchProfileChange._base_manager.filter(pk=change.pk),
            source_snapshot={'schema': 'tampered'},
        )
        self._assert_resolve_error(
            service.ERROR_SOURCE_INVALID,
            period_id=period.pk,
        )

        models.QuerySet.update(
            EmployeeWatchProfileChange._base_manager.filter(pk=change.pk),
            source_snapshot=service._snapshot_for_change(change),
            source_fingerprint='f' * 64,
        )
        self._assert_resolve_error(
            service.ERROR_SOURCE_FINGERPRINT_INVALID,
            period_id=period.pk,
        )

    def test_resolver_rejects_invalid_correction_lineage(self):
        period = self._period(days=10, composition=self.new_composition)
        invalid_parent = self._historical_change(
            period=period,
            version_number=1,
            status=EmployeeWatchProfileChange.Status.DRAFT,
        )
        correction = self._historical_change(
            period=period,
            version_number=2,
            new_schedule=self.no_brigade_schedule,
            new_brigade=None,
        )
        models.QuerySet.update(
            EmployeeWatchProfileChange._base_manager.filter(pk=correction.pk),
            supersedes_id=invalid_parent.pk,
        )

        self._assert_resolve_error(
            service.ERROR_PROFILE_INCONSISTENT,
            period_id=period.pk,
        )

    def test_resolver_revalidates_employee_and_final_reference_activity(self):
        Employee.objects.filter(pk=self.employee.pk).update(is_active=False)
        self._assert_resolve_error(service.ERROR_EMPLOYEE_INACTIVE)
        Employee.objects.filter(pk=self.employee.pk).update(is_active=True)

        WorkSchedule.objects.filter(pk=self.old_schedule.pk).update(is_active=False)
        self._assert_resolve_error(service.ERROR_WORK_SCHEDULE_INACTIVE)
        WorkSchedule.objects.filter(pk=self.old_schedule.pk).update(is_active=True)

        WatchComposition.objects.filter(pk=self.old_composition.pk).update(
            is_active=False,
        )
        self._assert_resolve_error(service.ERROR_WATCH_COMPOSITION_INACTIVE)

    def test_resolver_revalidates_brigade_policy_with_specific_codes(self):
        self._force_legacy_watch_profile_drift_for_test(
            self.employee,
            brigade_number=None,
        )
        self._assert_resolve_error(service.ERROR_BRIGADE_REQUIRED)

        self._force_legacy_watch_profile_drift_for_test(
            self.employee,
            brigade_number=3,
        )
        self._assert_resolve_error(service.ERROR_BRIGADE_OUT_OF_RANGE)

        WorkSchedule.objects.filter(pk=self.old_schedule.pk).update(brigade_count=0)
        self._force_legacy_watch_profile_drift_for_test(
            self.employee,
            brigade_number=1,
        )
        self._assert_resolve_error(service.ERROR_BRIGADE_NOT_ALLOWED)

    def test_resolver_allows_composition_different_from_target_period(self):
        self.assertNotEqual(
            self.employee.watch_composition_id,
            self.period.watch_composition_id,
        )

        resolved = resolve_employee_watch_profile(
            employee_id=self.employee.pk,
            watch_period_id=self.period.pk,
        )

        self.assertEqual(
            resolved.watch_composition_id,
            self.employee.watch_composition_id,
        )

    def test_profile_fingerprint_is_stable_structural_and_provenance_sensitive(self):
        first = resolve_employee_watch_profile(
            employee_id=self.employee.pk,
            watch_period_id=self.period.pk,
        )
        repeated = resolve_employee_watch_profile(
            employee_id=self.employee.pk,
            watch_period_id=self.period.pk,
        )
        self.assertEqual(first.profile_fingerprint, repeated.profile_fingerprint)

        baseline_snapshot = service._build_profile_snapshot(
            employee_id=self.employee.pk,
            watch_period=self.period,
            profile=(self.old_schedule.pk, 1, self.old_composition.pk),
            source_kind=service.SOURCE_KIND_LEGACY_BASELINE,
            change=None,
        )
        changed_profile = json.loads(json.dumps(baseline_snapshot))
        changed_profile['resolved_profile']['brigade_number'] = 2
        changed_source = json.loads(json.dumps(baseline_snapshot))
        changed_source['source']['kind'] = service.SOURCE_KIND_APPLIED_CHANGE
        changed_source['source']['change_id'] = 999
        changed_source['source']['change_version_number'] = 1
        changed_source['source']['source_fingerprint'] = 'a' * 64
        self.assertNotEqual(
            service._canonical_fingerprint(baseline_snapshot),
            service._canonical_fingerprint(changed_profile),
        )
        self.assertNotEqual(
            service._canonical_fingerprint(baseline_snapshot),
            service._canonical_fingerprint(changed_source),
        )

    def test_resolver_result_and_fingerprint_source_exclude_personal_access_data(self):
        source_period = self._period(days=10, composition=self.new_composition)
        change = self._historical_change(period=source_period)
        resolved = resolve_employee_watch_profile(
            employee_id=self.employee.pk,
            watch_period_id=self.period.pk,
        )
        payload = asdict(resolved)
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        self.assertNotIn(self.employee.full_name, serialized)
        self.assertNotIn(self.employee.phone, serialized)
        self.assertNotIn(change.basis, serialized)
        self.assertEqual(resolved.source_fingerprint, change.source_fingerprint)
        self.assertEqual(
            set(payload),
            {
                'employee_id',
                'watch_period_id',
                'effective_on',
                'work_schedule_id',
                'brigade_number',
                'watch_composition_id',
                'source_kind',
                'change_id',
                'change_version_number',
                'source_fingerprint',
                'profile_fingerprint',
            },
        )

    def test_resolver_is_read_only_and_never_requests_row_locks(self):
        source_period = self._period(days=10, composition=self.new_composition)
        self._historical_change(period=source_period)
        before_employee = Employee.objects.values().get(pk=self.employee.pk)
        before_changes = list(
            EmployeeWatchProfileChange._base_manager.values().order_by('pk')
        )
        before_routing = ArrivalRosterRoutingBatch._base_manager.count()

        with (
            patch.object(
                QuerySet,
                'select_for_update',
                side_effect=AssertionError('resolver must not lock rows'),
            ),
            patch.object(
                service,
                '_trusted_insert_change',
                side_effect=AssertionError('resolver must not insert'),
            ),
            patch.object(
                service,
                '_trusted_transition_change',
                side_effect=AssertionError('resolver must not update'),
            ),
        ):
            first = resolve_employee_watch_profile(
                employee_id=self.employee.pk,
                watch_period_id=self.period.pk,
            )
            second = resolve_employee_watch_profile(
                employee_id=self.employee.pk,
                watch_period_id=self.period.pk,
            )

        self.assertEqual(first, second)
        self.assertEqual(
            Employee.objects.values().get(pk=self.employee.pk),
            before_employee,
        )
        self.assertEqual(
            list(EmployeeWatchProfileChange._base_manager.values().order_by('pk')),
            before_changes,
        )
        self.assertEqual(ArrivalRosterRoutingBatch._base_manager.count(), before_routing)

    def test_resolver_missing_exact_employee_and_period_are_controlled(self):
        self._assert_resolve_error(
            service.ERROR_EMPLOYEE_NOT_FOUND,
            employee_id=999999,
        )
        self._assert_resolve_error(
            service.ERROR_WATCH_PERIOD_NOT_FOUND,
            period_id=999999,
        )
