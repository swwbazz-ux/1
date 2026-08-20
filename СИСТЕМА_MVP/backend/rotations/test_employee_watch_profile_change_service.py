import hashlib
import json
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.query import QuerySet
from django.test import TestCase
from django.utils import timezone

from assignments.models import EquipmentAssignment
from settlement.models import SettlementCohort
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
    create_employee_watch_profile_change_draft,
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
        Employee.objects.filter(pk=self.actor.pk).update(
            work_schedule=self.old_schedule,
            brigade_number=1,
            watch_composition=self.old_composition,
        )
        lock_order = []
        original = QuerySet.select_for_update

        def traced(queryset, *args, **kwargs):
            lock_order.append(queryset.model.__name__)
            return original(queryset, *args, **kwargs)

        with patch.object(QuerySet, 'select_for_update', new=traced):
            create_employee_watch_profile_change_draft(**self._kwargs(
                employee_id=self.actor.pk,
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
